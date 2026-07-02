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

def entry2text(entry: models.PullThing) -> str:
    """Change a pl member into a single unique string.

    Sub-containers (container=True) get a 'pl:' prefix so a video and a sub-playlist sharing an
    id can never collide, and key by URL first — matching the URL-keyed sub-container convention:
    a channel's tabs all share the channel's native_id, so an id-first key would collapse them to
    one membership entry and a tab appearing/vanishing would not flip change-detection. Videos
    key by native_id.
    """
    if entry.container is True:
        return f"pl:{entry.url or entry.native_id or ''}"
    # A leaf/unknown member keys by native_id, falling back to url (then '') so a member with no
    # yt-dlp id never yields None — sorted()/join() in pl2txt require comparable, joinable strings.
    return entry.native_id or entry.url or ""

def pl2txt(entries: list[models.PullThing]) -> str:
    """Change a container's members into a string

    Note that we sort and uniq it (order does not matter).
    """
    keys = {entry2text(x) for x in entries}
    return "\n".join(sorted(keys))

def pl_hash(entries: list[models.PullThing]) -> bytes:
    """Hash a container's membership (videos + sub-containers)

    Note that the order does not matter
    """
    hash_object = hashlib.sha256()
    hash_object.update(pl2txt(entries).encode())
    return hash_object.digest()

def _subtree_entries(entries: list[models.PullThing]):
    """Flatten a pull's membership through inlined sub-containers (depth-first)."""
    for entry in entries:
        yield entry
        if entry.entries:
            yield from _subtree_entries(entry.entries)

def subtree_hash(pull: models.PullThing) -> bytes:
    """Hash the full inlined subtree (every descendant node), so a change anywhere below a
    container — not just in its direct members — flips the change-detection signal (`_run_stats`
    `different`). This keeps a parent that inlines its sub-playlists "hot" enough to track its
    fastest-changing descendant (hybrid scheduling). Equals `pl_hash(pull.entries)` for a flat
    pull (no inlined sub-containers), so leaf-only playlists are unaffected.
    """
    return pl_hash(list(_subtree_entries(pull.entries)))

# --- V4 layer: DLP/LM-native -> thing/rel/run ------------------------------------------
# These convert the (reused) DLP boundary models into the frozen thing/rel/run schema
# (LM-V4-DESIGN.md Part 2). They are pure constructors — no DB/session — so the actual
# upsert/dedup against existing rows is the Stage-1 ingest's job (Task 1.1). Note the
# SQLModel select gotcha for the query side (LM-V4-DESIGN.md §6.4): use `col == None` /
# `is_(None)`, never Python `is not None`, in filters on nullable columns.

def thing_from_node(node: models.PullThing) -> models.Thing:
    """Build a `thing` from any pull node, carrying its `container` verdict as-is.

    Covers every shape the unified node represents: a leaf video (container=False), an
    unknown flat url-result (container=None) classified on its own pull (#158), and a
    container (container=True) for a playlist/channel.
    """
    return models.Thing(url=node.url,
                        extractor_key=node.extractor_key,
                        native_id=node.native_id,
                        container=node.container,
                        title=node.title,
                        channel=node.channel.url,
                        thumbnail_url=node.thumbnail_url,
                        modified=node.modified)


def thing_from_chan(chan: models.UlChan,
                    source_extractor: Optional[str] = None) -> Optional[models.Thing]:
    """Build a channel `thing` from an uploader/channel descriptor, or None if it has neither
    a URL nor a native id (nothing to key on).

    A channel is just a container (container=True); its channel-ness rides the soft
    `attrs.kind='channel'` display hint plus the `channel=True` rel edges pointing at it.
    extractor_key is left None — yt-dlp does not provide the channel's sub-extractor in a
    parent info dict; it is filled in only when a job runs directly on the channel URL.

    Two shapes (#160):
    - URL present -> the channel is keyed by its URL (native_id nulled, kept as an
      `attrs.channel_id` hint), following the container-keyed-by-webpage_url convention.
    - URL absent but native_id present -> a *url-less* channel keyed by its native id, so a
      video whose extractor only exposes an uploader/channel ID (no URL) is still linked to a
      channel Thing (the V3 regression this fixes). It is a pure graph node the worker can't run
      (no URL to pull); the dispatch (`claim_job` stage1_branch) excludes url-less containers, so
      it is never claimed. `source_extractor` (the discovering video's extractor) is recorded as
      a provenance hint since extractor_key stays NULL — we never inherit the video's extractor
      (usually a different sub-extractor).
    """
    if chan.url:
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
    if chan.native_id is None:
        return None
    attrs = {'kind': 'channel', 'channel_id': chan.native_id}
    if source_extractor is not None:
        attrs['source_extractor'] = source_extractor
    return models.Thing(url=None,
                        extractor_key=None,
                        native_id=chan.native_id,
                        container=True,
                        title=chan.title,
                        channel=None,
                        attrs=attrs)


def merge_attr(thing: models.Thing, key: str, value) -> None:
    """Set one key on a thing's `attrs` JSONB, preserving the rest (handles attrs=None)."""
    thing.attrs = {**(thing.attrs or {}), key: value}


# yt-dlp's two flat url-result `_type`s: a bare pointer (no media/formats). Any other shape
# (a full video extract) is a richer hint. The one raw-yt-dlp detail xform inspects — kept
# minimal and here because the load-info hint it guards is itself an opaque yt-dlp blob, and
# `_type` is a long-stable yt-dlp concept.
_FLAT_HINT_TYPES = ("url", "url_transparent")


def _hint_is_flat(info: Optional[dict]) -> bool:
    return bool(info) and info.get("_type") in _FLAT_HINT_TYPES


def refresh_info_hint(thing: models.Thing, info: Optional[dict]) -> None:
    """Stamp the Stage-2 load-info hint onto a video still pending download (best_oi NULL).

    yt-dlp info dicts go stale, so the hint is refreshed while the media is unacquired and
    left alone once acquired. A flat pull pointer (`_type` url/url_transparent) never
    overwrites a fuller existing hint: re-pulling a playlist must not clobber the richer hint
    a direct meta extract already stored.
    """
    if info is None or thing.container is not False or thing.best_oi is not None:
        return
    existing = (thing.attrs or {}).get(INFO_JSON_KEY)
    if _hint_is_flat(info) and existing is not None and not _hint_is_flat(existing):
        return
    merge_attr(thing, INFO_JSON_KEY, info)


def clear_info_hint(thing: models.Thing) -> None:
    """Drop the Stage-2 load-info hint once it is moot (the thing is a pulled container, or
    its media is acquired). The run history (run.data_json) keeps the raw extract, so the
    thing need not. No-op unless a hint is actually present (avoids a null-key on attrs)."""
    if (thing.attrs or {}).get(INFO_JSON_KEY) is not None:
        merge_attr(thing, INFO_JSON_KEY, None)


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


def pl_full2things(pl: models.PullThing, *, bucket: str,
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
    pl_thing = thing_from_node(pl)
    pl_thing.bucket = bucket
    members: list[models.Thing] = []
    rels: list[models.Rel] = []
    # One channel node per uploader, shared across the container + its videos. Keyed by URL
    # when known, else by native id (a url-less uploader, #160) so the same id maps to one node.
    channels_by_key: dict[str, models.Thing] = {}

    def channel_for(chan: models.UlChan,
                    source_extractor: Optional[str] = None) -> Optional[models.Thing]:
        key = chan.url or (f"id:{chan.native_id}" if chan.native_id else None)
        if key is None:
            return None
        existing = channels_by_key.get(key)
        if existing is None:
            existing = thing_from_chan(chan, source_extractor)
            if existing is None:
                return None
            existing.bucket = bucket
            channels_by_key[key] = existing
        return existing

    # The container's own uploader -> a channel=True edge, but only for a *curated playlist*
    # owned by someone else; when the container IS its own uploader (a channel) skip the
    # self-edge. A url-less owner (id only) still links (#160).
    pull_is_channel = _same_identity(pl_thing, pl.channel)   # container IS its own uploader
    if (pl.channel.url or pl.channel.native_id) and not pull_is_channel:
        pl_chan = channel_for(pl.channel, pl.extractor_key)
        if pl_chan is not None:
            rels.append(models.Rel(parent=pl_chan.id, child=pl_thing.id, channel=True))

    for vid in pl.entries:
        vid_thing = thing_from_node(vid)   # container carried from vid.container (True for subs)
        vid_thing.bucket = bucket
        # #156: when pulling a channel, its flat video entries often omit the uploader. Inherit
        # the channel's own identity onto such a leaf so it links channel=True (below) and is
        # rate-able (enough_to_rate needs a channel URL) without a separate Stage-2 meta pull.
        vid_owner = vid.channel
        if (pull_is_channel and vid.container is False
                and not vid.channel.url and not vid.channel.native_id):
            vid_owner = pl.channel
            vid_thing.channel = pl_thing.channel or pl_thing.url
        # Every sub-container member is URL-keyed: null its native_id so `_find_thing` keys it by
        # URL (a distinct thing, never collapsed onto the parent/a sibling). This matches the
        # convention that containers are keyed by webpage_url — a channel's Videos/Shorts/Live
        # tabs all share the channel's `id` but have distinct URLs, and a curated sub-playlist that
        # recurs under two URLs converges later via the dedup merge, not by id-collapse here. The
        # original id is kept as a soft `channel_id` hint.
        attrs = dict(hints) if hints is not None else {}
        if vid.container is True:
            vid_thing.native_id = None
            if vid.native_id is not None:
                attrs["channel_id"] = vid.native_id
        if vid.info_json is not None:
            # Load-info hint (§2.1); the raw dict is kept verbatim — a sub-container's inlined
            # entries ride along but never land on a stub, since refresh_info_hint no-ops for
            # container=True (the endpoint ingests those entries via its own recursion instead).
            attrs[INFO_JSON_KEY] = vid.info_json
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
        if _same_identity(pl_thing, vid_owner):
            rels.append(models.Rel(parent=pl_thing.id, child=vid_thing.id, channel=True))
        else:
            rels.append(models.Rel(parent=pl_thing.id, child=vid_thing.id, channel=False))
            vid_chan = channel_for(vid_owner, vid.extractor_key)
            if vid_chan is not None:
                rels.append(models.Rel(parent=vid_chan.id, child=vid_thing.id, channel=True))

    return ThingGraph(playlist=pl_thing, members=members,
                      channels=list(channels_by_key.values()), rels=rels)


def reconcile_count(pl: models.PullThing) -> int:
    """Reconcile a container's reported `playlist_count` against its leaf membership.

    Counts only *leaf* members (`container is not True`), never sub-containers (a channel's
    Videos/Shorts/Live tabs, nested playlists): yt-dlp's `playlist_count` for a channel is a
    *video* count, so counting all `entries` understates membership and warns spuriously when
    tabs are present (#167). Returns the count to record (provided count wins when present);
    only reconciles/warns against the provided count when this level is pure leaves — with
    sub-containers present the two aren't comparable (this level's tabs vs. the aggregate video
    count). Used by the Stage-1 ingest endpoint.
    """
    leaves = sum(1 for e in pl.entries if e.container is not True)
    if pl.playlist_count is None:
        warnings.warn(f'No provided playlist_count; leveraging length of {leaves}.')
        return leaves
    if not any(e.container is True for e in pl.entries) and leaves != pl.playlist_count:
        warnings.warn(f"Provided playlist count {pl.playlist_count} doesn't match actual "
                      f"length of {leaves}; will record provided.")
    return pl.playlist_count


# Fields backfilled onto an existing thing from a fresher pull when they are still NULL
# (#147). Never overwrites a value already present; `container` is classified separately
# by the ingest endpoint (NULL -> True/False on first pull), not here. `url` is included so a
# stub first created without a webpage URL (e.g. a flat entry that only carried id+ie_key) gets
# it filled on a later pull/meta — required for enough_to_rate. Safe for containers: they are
# keyed by URL, so theirs is never NULL and is never overwritten.
_BACKFILL_FIELDS = ("url", "title", "extractor_key", "native_id",
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
