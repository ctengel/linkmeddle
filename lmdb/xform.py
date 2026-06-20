"""analytics and transformation"""

import statistics
import datetime
from typing import NamedTuple, Optional
import hashlib
import warnings
from . import models

FIB = [1, 2, 3, 5, 8, 13, 21, 34]

INFO_JSON_KEY = "info_json"   # attrs key carrying a video stub's raw yt-dlp entry (Stage-2 hint)

def next_fib(existing: int | float | None, up: bool) -> int:
    """Next fibonacci number up or down"""
    if existing is None:
        return FIB[0]
    if up:
        for i in FIB:
            if i > existing:
                return i
        return FIB[-1]
    for i in sorted(FIB, reverse=True):
        if i < existing:
            return i
    return FIB[0]

def entry2text(entry: models.VidFull) -> str:
    """Change a pl member into a single unique string.

    Sub-containers (container=True) get a 'pl:' prefix (keyed by native_id, else url) so a
    video and a sub-playlist sharing an id can never collide; videos key by native_id.
    """
    if entry.container is True:
        return f"pl:{entry.native_id or entry.url or ''}"
    return entry.native_id

def pl2txt(entries: list[models.VidFull]) -> str:
    """Change a container's members into a string

    Note that we sort and uniq it (order does not matter).
    """
    keys = {entry2text(x) for x in entries}
    return "\n".join(sorted(keys))

def pl_hash(entries: list[models.VidFull]) -> bytes:
    """Hash a container's membership (videos + sub-containers)

    Note that the order does not matter
    """
    hash_object = hashlib.sha256()
    hash_object.update(pl2txt(entries).encode())
    return hash_object.digest()

# --- V4 layer: DLP/LM-native -> thing/rel/run ------------------------------------------
# These convert the (reused) DLP boundary models into the frozen thing/rel/run schema
# (LM-V4-DESIGN.md Part 2). They are pure constructors — no DB/session — so the actual
# upsert/dedup against existing rows is the Stage-1 ingest's job (Task 1.1). Note the
# SQLModel select gotcha for the query side (LM-V4-DESIGN.md §6.4): use `col == None` /
# `is_(None)`, never Python `is not None`, in filters on nullable columns.

def thing_from_vid(vid: models.VidFull) -> models.Thing:
    """Build a stub `thing` from a playlist entry: a leaf video (container=False) or, for an
    ambiguous flat url-result, an unknown stub (container=None) classified on its own pull."""
    return models.Thing(url=vid.url,
                        extractor_key=vid.extractor_key,
                        native_id=vid.native_id,
                        container=vid.container,
                        title=vid.title,
                        channel=vid.channel.url,
                        thumbnail_url=vid.thumbnail_url,
                        modified=vid.modified)


def thing_from_pl(pl: models.PlaylistFull) -> models.Thing:
    """Build the container `thing` (container=True) for a playlist/channel."""
    return models.Thing(url=pl.url,
                        extractor_key=pl.extractor_key,
                        native_id=pl.native_id,
                        container=True,
                        title=pl.title,
                        channel=pl.channel.url,
                        modified=pl.modified)


def thing_from_chan(chan: models.UlChan) -> Optional[models.Thing]:
    """Build a channel `thing` from an uploader/channel descriptor, or None if no URL.

    A channel is just a container (container=True); its channel-ness rides the soft
    `attrs.kind='channel'` display hint plus the `channel=True` rel edges pointing at it.
    extractor_key is left None — yt-dlp does not provide the channel's sub-extractor in a
    parent info dict; it is filled in only when a job runs directly on the channel URL.
    """
    if not chan.url:
        return None
    attrs: dict = {'kind': 'channel'}
    if chan.native_id is not None:
        attrs['channel_id'] = chan.native_id
    return models.Thing(url=chan.url,
                        extractor_key=None,
                        native_id=None,
                        container=True,
                        title=chan.title,
                        channel=chan.url,
                        attrs=attrs)


def merge_attr(thing: models.Thing, key: str, value) -> None:
    """Set one key on a thing's `attrs` JSONB, preserving the rest (handles attrs=None)."""
    thing.attrs = {**(thing.attrs or {}), key: value}


def refresh_info_hint(thing: models.Thing, info: Optional[dict]) -> None:
    """Stamp the Stage-2 load-info hint onto a video still pending download (best_oi NULL).

    yt-dlp info dicts go stale, so the hint is refreshed while the media is unacquired and
    left alone once acquired.
    """
    if info is not None and thing.container is False and thing.best_oi is None:
        merge_attr(thing, INFO_JSON_KEY, info)


def enough_to_rate(thing: models.Thing) -> bool:
    """Is a video stub described well enough for a human to rate it? (API-side, §1).

    Decided from stored fields only — never by peeking at raw yt-dlp JSON — so the
    API stays stable against yt-dlp shape changes. Drives `last_success_dt`.
    All five identity fields must be present: channel URL, webpage URL, title,
    extractor key, and native ID.
    """
    return bool(thing.title and thing.url and thing.native_id
                and thing.extractor_key and thing.channel)


def container_switch(current: Optional[bool], proposed: Optional[bool]) -> bool:
    """True if `proposed` would flip an already-set container classification.

    A NULL->value transition (first classification) and value->same value (affirm) are allowed;
    True<->False is the forbidden switch. Used to gate both user edits (409) and worker results
    (logged failed job) so a thing's container is set once and never silently changed.
    """
    return current is not None and proposed is not None and current != proposed


class ThingGraph(NamedTuple):
    """The thing/rel graph derived from one container pull, ready to upsert."""
    playlist: models.Thing      # the pulled container thing
    members: list[models.Thing]  # member stubs: leaf videos (False/None) + sub-containers (True)
    channels: list[models.Thing]  # per-video uploader containers (kind='channel')
    rels: list[models.Rel]


def _same_identity(parent: models.Thing, chan: models.UlChan) -> bool:
    """Is `chan` (an entry's uploader/owner) the same node as the pulled `parent` container?

    The channel case: the parent IS the uploader, so the parent->child edge is the
    channel/uploader edge (`rel.channel=True`) and no separate uploader node is needed.
    Matched by `native_id`; URL only when a native_id is absent
    (channel landing-vs-/videos URLs differ — #46 — so native_id is the reliable signal).
    """
    if parent.native_id is not None and chan.native_id is not None:
        return parent.native_id == chan.native_id
    if chan.url is not None:
        return parent.url == chan.url
    return False


# Soft `attrs` hints (§2.1, [A11]) propagated playlist -> video on fan-out. `cookies` is
# copied whenever the parent has it set; `lpm_lib` only when present — both reduce to
# "copy the key if the parent carries it" since we only ever propagate one-way (downward).
_PROPAGATE_HINTS = ("cookies", "lpm_lib")


def propagate_attrs(parent_attrs: Optional[dict]) -> Optional[dict]:
    """The subset of a parent thing's `attrs` inherited by its child stubs (§2.1, [A11])."""
    if not parent_attrs:
        return None
    out = {k: parent_attrs[k] for k in _PROPAGATE_HINTS if k in parent_attrs}
    return out or None


def pl_full2things(pl: models.PlaylistFull, *, bucket: str,
                   parent_attrs: Optional[dict] = None) -> ThingGraph:
    """Convert an LM-native container pull into its thing/rel graph.

    Produces the container thing, a member stub per `entries` item (a leaf video, or a
    `container=True` sub-container pulled later), and an uploader container (`kind='channel'`)
    per distinct owner. Edges are one parent->child each, with `rel.channel` chosen the same
    way for videos and sub-containers, by identity (`_same_identity`):

    - parent IS the member's owner (channel case) -> one `channel=True` edge, no separate
      uploader node and no duplicate membership edge.
    - parent is NOT the owner (curated playlist, incl. a curated sub-playlist) -> a
      `channel=False` membership edge, plus the owner node + a `channel=True` edge from it
      (V4's `pseudo_channel`, [A11]).

    A sub-container whose owner is unknown/different from the parent thus starts as a
    `channel=False` membership edge; its true ownership edge is established when the
    sub-container is pulled itself (its own uploader edge), and the monotonic rel upsert
    raises a same-parent edge `False->True` then.

    The returned objects carry client-side UUIDs, so the edges already reference real ids.
    Every constructed thing inherits the parent's `bucket` (required, immutable, [A10]) and
    the propagated soft hints (`attrs.cookies`/`attrs.lpm_lib`, §2.1). Pure constructor (no DB).
    """
    hints = propagate_attrs(parent_attrs)
    pl_thing = thing_from_pl(pl)
    pl_thing.bucket = bucket
    members: list[models.Thing] = []
    rels: list[models.Rel] = []
    # One channel node per uploader URL, shared across the container + its videos.
    channels_by_url: dict[str, models.Thing] = {}

    def channel_for(chan: models.UlChan) -> Optional[models.Thing]:
        if not chan.url:
            return None
        existing = channels_by_url.get(chan.url)
        if existing is None:
            existing = thing_from_chan(chan)
            existing.bucket = bucket
            channels_by_url[chan.url] = existing
        return existing

    # The container's own uploader -> a channel=True edge, but only for a *curated playlist*
    # owned by someone else; when the container IS its own uploader (a channel) skip the
    # self-edge.
    if pl.channel.url and not _same_identity(pl_thing, pl.channel):
        pl_chan = channel_for(pl.channel)
        if pl_chan is not None:
            rels.append(models.Rel(parent=pl_chan.id, child=pl_thing.id, channel=True))

    for vid in pl.entries:
        vid_thing = thing_from_vid(vid)   # container carried from vid.container (True for subs)
        vid_thing.bucket = bucket
        attrs = dict(hints) if hints is not None else {}
        if vid.info_json is not None:
            attrs[INFO_JSON_KEY] = vid.info_json   # load-info hint (§2.1; subs: entries-stripped)
        if attrs:
            vid_thing.attrs = attrs
        members.append(vid_thing)
        # Identity-based for every member (videos and sub-containers alike): parent IS the
        # owner -> a channel=True edge, no separate uploader node; a different/unknown owner
        # -> a channel=False membership edge plus the owner's channel=True edge ([A11]). A
        # sub-container's ownership edge is (re)asserted when it is pulled itself (its own
        # uploader edge, above); the monotonic rel upsert upgrades a stale False->True then.
        # So curated nesting (a container listing someone else's sub-playlist) records a
        # channel=False membership edge here, exactly like a curated video.
        if _same_identity(pl_thing, vid.channel):
            rels.append(models.Rel(parent=pl_thing.id, child=vid_thing.id, channel=True))
        else:
            rels.append(models.Rel(parent=pl_thing.id, child=vid_thing.id, channel=False))
            vid_chan = channel_for(vid.channel)
            if vid_chan is not None:
                rels.append(models.Rel(parent=vid_chan.id, child=vid_thing.id, channel=True))

    return ThingGraph(playlist=pl_thing, members=members,
                      channels=list(channels_by_url.values()), rels=rels)


def reconcile_count(pl: models.PlaylistFull) -> int:
    """Reconcile a container's reported `playlist_count` against its actual members.

    Members = all `entries` (leaf videos + sub-containers, so a channel's count covers both).
    Returns the count to record (provided count wins on mismatch), warning on disagreement.
    Used by the Stage-1 ingest endpoint.
    """
    count = len(pl.entries)
    if pl.playlist_count is None:
        warnings.warn(f'No provided playlist_count; leveraging length of {count}.')
    elif count != pl.playlist_count:
        warnings.warn(f"Provided playlist count {pl.playlist_count} doesn't match actual "
                      f"length of {count}; will record provided.")
        count = pl.playlist_count
    return count


# Fields backfilled onto an existing thing from a fresher pull when they are still NULL
# (#147). Never overwrites a value already present; `container` is classified separately
# by the ingest endpoint (NULL -> True/False on first pull), not here.
_BACKFILL_FIELDS = ("title", "extractor_key", "native_id",
                    "channel", "thumbnail_url", "modified")


def null_backfill(existing: models.Thing, incoming: models.Thing) -> dict:
    """Fields to set on `existing` from `incoming` where `existing` is NULL (#147).

    Pure: returns `{field: value}` for whitelist fields that are unset on the stored thing
    but known from the fresh pull. The caller applies them (so it can guard the unique
    native-key index). Never returns a field that would overwrite a present value.
    """
    out: dict = {}
    for field in _BACKFILL_FIELDS:
        if getattr(existing, field) is None and getattr(incoming, field) is not None:
            out[field] = getattr(incoming, field)
    return out


# --- V4 try_on backoff (Task 1.4) ------------------------------------------------------
# Reworks V3's add_new_run/next_run/rec_adjust_freq onto the V4 `run` table: there is no
# stored freq_days, so the "current interval" is derived from run.starttime gaps and the
# result is written to thing.try_on (§4.4, §2.5). Pure: operates on `run` rows, no DB.

# Effective-rating floor for each grade band (§2.4): the single source of truth for the
# band boundaries, consumed by the backoff here and the dispatch floors in api.py.
BAND_FLOOR = {"A": 1.5, "B": 0.5, "C": -0.5}

INITIAL_INTERVAL = {"A": 3, "B": 5, "C": 8}   # 2nd-run interval by rating band (§4.4)


def initial_interval(rating: float) -> int:
    """Initial backoff interval (days) for a rating, by grade band (§2.4/§4.4)."""
    if rating >= BAND_FLOOR["A"]:
        return INITIAL_INTERVAL["A"]
    if rating >= BAND_FLOOR["B"]:
        return INITIAL_INTERVAL["B"]
    return INITIAL_INTERVAL["C"]   # C and below


class _RunStat(NamedTuple):
    """A completed run reduced to the quantities the backoff needs."""
    success: bool
    different: bool          # membership changed vs the most-recent prior successful run
    interval: int            # day-gap from the immediately-prior completed run (0 for the first)
    date: datetime.date      # run.starttime date


def _run_stats(runs: list[models.Run]) -> list[_RunStat]:
    """Reduce a thing's runs to ordered `_RunStat`s (drops in-progress success=None runs)."""
    done = sorted((r for r in runs if r.success is not None), key=lambda r: r.starttime)
    out: list[_RunStat] = []
    prev_date: Optional[datetime.date] = None
    prev_success_hash: Optional[bytes] = None
    for run in done:
        run_date = run.starttime.date()
        interval = 0 if prev_date is None else (run_date - prev_date).days
        # First run counts as "different" (no prior successful run to compare, §2.3).
        different = True if prev_success_hash is None else run.entries_hash != prev_success_hash
        out.append(_RunStat(success=bool(run.success), different=different,
                            interval=interval, date=run_date))
        prev_date = run_date
        if run.success and run.entries_hash is not None:
            prev_success_hash = run.entries_hash
    return out


def _current_interval(stats: list[_RunStat]) -> Optional[int]:
    """Median day-gap across up to the 3 most recent successful run pairs; None if <2."""
    succ = [s.date for s in stats if s.success]
    if len(succ) < 2:
        return None
    gaps = [(succ[i] - succ[i-1]).days for i in range(1, len(succ))]
    return round(statistics.median(gaps[-3:]))


def _rec_adjust(window: list[_RunStat]) -> Optional[bool]:
    """Recommend backoff direction over a recent window (port of V3 rec_adjust_freq).

    True = back off (up): everything failed, or nothing changed. False = speed up (down):
    every run found new content. None = keep the current interval.
    """
    if all(not s.success for s in window):
        return True
    if all(not s.different for s in window):
        return True
    if all(s.different for s in window):
        return False
    return None


def next_try_on(rating: float, runs: list[models.Run],
                today: Optional[datetime.date] = None) -> datetime.date:
    """The next `try_on` date for a thing, from its run history (§4.4; reworked add_new_run).

    No `freq_days`: the interval is derived from `run.starttime` gaps. 2nd run uses the
    rating's initial interval; subsequent successful runs adapt it via Fibonacci (back off
    when nothing changes / all fail, speed up when every run finds new content). A failure
    backs off short — tomorrow after a success, else a Fibonacci step up (§2.5).
    """
    if today is None:
        today = models.naive_utcnow().date()
    stats = _run_stats(runs)
    if not stats:
        return today
    last = stats[-1]
    window = stats[-3:]

    if not last.success:                       # failure backoff (§2.5/§4.7)
        if len(window) >= 2 and window[-2].success:
            return last.date + datetime.timedelta(days=1)   # first failure -> retry tomorrow
        step = _current_interval(stats)
        if step is None:
            step = initial_interval(rating)
        return last.date + datetime.timedelta(days=next_fib(step, True))

    successful = [s for s in stats if s.success]
    if len(successful) < 2:                    # 2nd run uses the rating initial (§4.4)
        interval = initial_interval(rating)
    else:
        interval = _current_interval(stats)
        if interval is None:
            interval = initial_interval(rating)
        rec = _rec_adjust(window)
        if rec is not None:
            interval = next_fib(interval, rec)
    # Floor at 1 day: _current_interval can be 0 (two successful runs on the same calendar day)
    # and a 0-day reschedule would make the thing immediately re-due. The only same-day result
    # is the never-run case above (`return today`).
    return last.date + datetime.timedelta(days=max(1, interval))
