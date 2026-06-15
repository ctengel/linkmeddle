"""analytics and transformation"""

import statistics
import datetime
import uuid
from typing import NamedTuple, Optional
import hashlib
import warnings
from . import models

FIB = [1, 2, 3, 5, 8, 13, 21, 34]

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

def adjust(existing: list[int], up: bool) -> int:
    """Change interval up or down (reused by the Task 1.4 try_on backoff)"""
    med = statistics.median(existing)
    return next_fib(med, up)

def entry2text(entry: models.VidFull) -> str:
    """Change a pl entry into single unique string"""
    return entry.id

def pl2txt(entries: list[models.VidFull]) -> str:
    """Change playlist entries into a string
    
    Note that we sort and uniq it
    """
    return "\n".join(sorted({entry2text(x) for x in entries}))

def pl_hash(entries: list[models.VidFull]) -> bytes:
    """Hash a playlist
    
    Note that the order does not matter
    """
    hash_object = hashlib.sha256()
    hash_object.update(pl2txt(entries).encode())
    return hash_object.digest()

def newest(entries: list[models.VidFull]) -> models.VidFull:
    """Find newest playlist entry"""
    return sorted(entries, key=lambda x: x.upload_date or datetime.datetime.min, reverse=True)[0]

def pl_dlp2lm(dlpin: models.PlaylistDLP) -> models.PlaylistFull:
    """Raw DLP playlist to LM-native playlist"""
    assert dlpin.webpage_url is not None  # we need this until we ger lmpl id
    # TODO use model_validate?
    retv = models.PlaylistFull(id=dlpin.id,
                               title=dlpin.title,
                               modified_date=datetime.datetime.strptime(dlpin.modified_date, "%Y%m%d") if dlpin.modified_date else None,
                               webpage_url=dlpin.webpage_url,
                               playlist_count=dlpin.playlist_count,  # TODO
                               channel=models.UlChan(channel_id=dlpin.channel_id,
                                                     uploader_id=dlpin.uploader_id,
                                                     uploader=dlpin.uploader,
                                                     channel_url=dlpin.channel_url,
                                                     uploader_url=dlpin.uploader_url),
                               entries=[models.VidFull(channel=models.UlChan(channel_id=x.channel_id,
                                                                             uploader_id=x.uploader_id,
                                                                             uploader=x.uploader,
                                                                             channel_url=x.channel_url,
                                                                             uploader_url=x.uploader_url),
                                                       description=x.description,
                                                       id=x.id,
                                                       title=x.title,
                                                       webpage_url=x.webpage_url,
                                                       duration=x.duration,
                                                       ext=x.ext,
                                                       format=x.format,
                                                       height=x.height,
                                                       width=x.width,
                                                       extractor=models.DLPIE(extractor_key=x.extractor_key,
                                                                              extractor=x.extractor),
                                                       categories=x.categories,
                                                       is_live=x.is_live,
                                                       was_live=x.was_live,
                                                       language=x.language,
                                                       n_entries=x.n_entries,  # huh,
                                                       thumbnail=x.thumbnail,
                                                       upload_date=datetime.datetime.fromtimestamp(x.timestamp) if x.timestamp else None)  # is this right
                                        for x in dlpin.entries if isinstance(x, models.PlVidDLP)],
                               extractor=models.DLPIE(extractor_key=dlpin.extractor_key,
                                                      extractor=dlpin.extractor))
    for pl_entry in dlpin.entries:
        if not isinstance(pl_entry, models.PlaylistDLP):
            continue
        for sub_entry in pl_entry.entries:
            if sub_entry is None:
                continue
            assert isinstance(sub_entry, models.PlVidDLP)
            retv.entries.append(models.VidFull(channel=models.UlChan(channel_id=sub_entry.channel_id,
                                                                     uploader_id=sub_entry.uploader_id,
                                                                     uploader=sub_entry.uploader,
                                                                     channel_url=sub_entry.channel_url,
                                                                     uploader_url=sub_entry.uploader_url),
                                               description=sub_entry.description,
                                               id=sub_entry.id,
                                               title=sub_entry.title,
                                               webpage_url=sub_entry.webpage_url,
                                               duration=sub_entry.duration,
                                               ext=sub_entry.ext,
                                               format=sub_entry.format,
                                               height=sub_entry.height,
                                               width=sub_entry.width,
                                               extractor=models.DLPIE(extractor_key=sub_entry.extractor_key,
                                                                      extractor=sub_entry.extractor),
                                               categories=sub_entry.categories,
                                               is_live=sub_entry.is_live,
                                               was_live=sub_entry.was_live,
                                               language=sub_entry.language,
                                               n_entries=sub_entry.n_entries,  # huh,
                                               thumbnail=sub_entry.thumbnail,
                                               upload_date=datetime.datetime.fromtimestamp(sub_entry.timestamp) if sub_entry.timestamp else None)  # is this right
                             )
    return retv

def vid_uploader_url(vid: models.VidFull) -> Optional[str]:
    """Get uploader URL from video"""
    if vid.channel.uploader_url:
        return vid.channel.uploader_url
    if vid.channel.channel_url:
        return vid.channel.channel_url
    return None


# --- V4 layer: DLP/LM-native -> thing/rel/run ------------------------------------------
# These convert the (reused) DLP boundary models into the frozen thing/rel/run schema
# (LM-V4-DESIGN.md Part 2). They are pure constructors — no DB/session — so the actual
# upsert/dedup against existing rows is the Stage-1 ingest's job (Task 1.1). Note the
# SQLModel select gotcha for the query side (LM-V4-DESIGN.md §6.4): use `col == None` /
# `is_(None)`, never Python `is not None`, in filters on nullable columns.

def _norm_extractor(extractor: models.DLPIE) -> Optional[str]:
    """Canonical lowercased extractor key for a thing (key, falling back to name)."""
    ek = extractor.extractor_key or extractor.extractor
    return ek.lower() if ek else None


def chan_url(chan: models.UlChan) -> Optional[str]:
    """Best URL for an uploader/channel."""
    return chan.uploader_url or chan.channel_url


def thing_from_vid(vid: models.VidFull) -> models.Thing:
    """Build a stub video `thing` with whatever the playlist pull told us (#137 sidestep)."""
    return models.Thing(url=vid.webpage_url,
                        extractor_key=_norm_extractor(vid.extractor),
                        native_id=vid.id,
                        type='video',
                        title=vid.title,
                        channel=vid_uploader_url(vid),
                        thumbnail_url=vid.thumbnail,
                        modified=vid.upload_date)


def thing_from_pl(pl: models.PlaylistFull) -> models.Thing:
    """Build the playlist `thing`."""
    return models.Thing(url=pl.webpage_url,
                        extractor_key=_norm_extractor(pl.extractor),
                        native_id=pl.id,
                        type='playlist',
                        title=pl.title,
                        channel=chan_url(pl.channel),
                        modified=pl.modified_date)


def thing_from_chan(chan: models.UlChan, extractor_key: Optional[str]) -> Optional[models.Thing]:
    """Build a channel `thing` from an uploader/channel descriptor, or None if no URL."""
    url = chan_url(chan)
    if not url:
        return None
    return models.Thing(url=url,
                        extractor_key=extractor_key,
                        native_id=chan.uploader_id or chan.channel_id,
                        type='channel',
                        title=chan.uploader,
                        channel=url)


class ThingGraph(NamedTuple):
    """The thing/rel graph derived from one playlist pull, ready to upsert."""
    playlist: models.Thing
    videos: list[models.Thing]
    channels: list[models.Thing]
    rels: list[models.Rel]


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
    """Convert an LM-native playlist into its thing/rel graph.

    Produces the playlist thing, a stub thing per entry, a `type='channel'` thing per
    distinct uploader (the playlist's and each video's — V4's `pseudo_channel`, [A11]),
    and the edges between them: `playlist_video`, `channel_playlist` (channel->playlist),
    and `channel_video` (channel->video, so a video is reachable from its own uploader).
    The returned objects carry client-side UUIDs, so the edges already reference real ids.

    Every constructed thing inherits the parent playlist's `bucket` (required, immutable,
    [A10]); video stubs also inherit the parent's propagated soft hints
    (`attrs.cookies`/`attrs.lpm_lib`, §2.1). The caller supplies `bucket`/`parent_attrs`
    from the dispatched playlist thing; this stays a pure constructor (no DB).
    """
    hints = propagate_attrs(parent_attrs)
    pl_thing = thing_from_pl(pl)
    pl_thing.bucket = bucket
    videos: list[models.Thing] = []
    rels: list[models.Rel] = []
    # One channel node per uploader URL, shared across the playlist + its videos.
    channels_by_url: dict[str, models.Thing] = {}

    def channel_for(chan: models.UlChan, extractor_key: Optional[str]) -> Optional[models.Thing]:
        url = chan_url(chan)
        if not url:
            return None
        existing = channels_by_url.get(url)
        if existing is None:
            existing = thing_from_chan(chan, extractor_key)
            existing.bucket = bucket
            channels_by_url[url] = existing
        return existing

    pl_chan = channel_for(pl.channel, pl_thing.extractor_key)
    if pl_chan is not None:
        rels.append(models.Rel(parent=pl_chan.id, child=pl_thing.id,
                               type='channel_playlist'))
    for vid in pl.entries:
        vid_thing = thing_from_vid(vid)
        vid_thing.bucket = bucket
        if hints is not None:
            vid_thing.attrs = dict(hints)
        videos.append(vid_thing)
        rels.append(models.Rel(parent=pl_thing.id, child=vid_thing.id,
                               type='playlist_video'))
        vid_chan = channel_for(vid.channel, _norm_extractor(vid.extractor))
        if vid_chan is not None:
            rels.append(models.Rel(parent=vid_chan.id, child=vid_thing.id,
                                   type='channel_video'))
    return ThingGraph(playlist=pl_thing, videos=videos,
                      channels=list(channels_by_url.values()), rels=rels)


def reconcile_count(pl: models.PlaylistFull) -> int:
    """Reconcile a playlist's reported `playlist_count` against its actual entries.

    Returns the count to record (provided count wins on mismatch), warning on disagreement.
    Shared by `full2run` and the Stage-1 ingest endpoint.
    """
    count = len(pl.entries)
    if pl.playlist_count is None:
        warnings.warn(f'No provided playlist_count; leveraging length of {count}.')
    elif count != pl.playlist_count:
        warnings.warn(f"Provided playlist count {pl.playlist_count} doesn't match actual "
                      f"length of {count}; will record provided.")
        count = pl.playlist_count
    return count


def full2run(pl: models.PlaylistFull,
             thing_id: uuid.UUID,
             success: bool = True) -> models.Run:
    """Build a `run` record for a playlist (Stage-1) pull.

    `entries_hash` (reusing `pl_hash`) is the change-detection fingerprint; the raw
    yt-dlp output rides in `data_json` (caller supplies it later if desired).
    """
    return models.Run(thing_id=thing_id,
                     entries_hash=pl_hash(pl.entries),
                     playlist_count=reconcile_count(pl),
                     success=success,
                     starttime=models.naive_utcnow())


# Fields backfilled onto an existing thing from a fresher pull when they are still NULL
# (#147). Never overwrites a value already present; `type` is NOT NULL so it is corrected
# separately by the ingest endpoint, not here.
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


def runs_differ(prev: models.Run, new: models.Run) -> bool:
    """Did a playlist change between runs? (LM-V4-DESIGN.md §2.3)

    True iff the new run's membership fingerprint differs from the most recent prior
    *successful* run's. Caller is responsible for passing that prior successful run.
    """
    return prev.entries_hash != new.entries_hash


# --- V4 try_on backoff (Task 1.4) ------------------------------------------------------
# Reworks V3's add_new_run/next_run/rec_adjust_freq onto the V4 `run` table: there is no
# stored freq_days, so the "current interval" is derived from run.starttime gaps and the
# result is written to thing.try_on (§4.4, §2.5). Pure: operates on `run` rows, no DB.

INITIAL_INTERVAL = {"A": 3, "B": 5, "C": 8}   # 2nd-run interval by rating band (§4.4)


def initial_interval(rating: float) -> int:
    """Initial backoff interval (days) for a rating, by grade band (§2.4/§4.4)."""
    if rating >= 1.5:           # A band
        return INITIAL_INTERVAL["A"]
    if rating >= 0.5:           # B band
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
    """Day-gap between the last two successful runs (the §4.4 'current interval'); None if <2."""
    succ = [s.date for s in stats if s.success]
    if len(succ) < 2:
        return None
    return (succ[-1] - succ[-2]).days


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
        step = _current_interval(stats) or initial_interval(rating)
        return last.date + datetime.timedelta(days=next_fib(step, True))

    successful = [s for s in stats if s.success]
    if len(successful) < 2:                    # 2nd run uses the rating initial (§4.4)
        interval = initial_interval(rating)
    else:
        interval = _current_interval(stats)
        rec = _rec_adjust(window)
        if rec is not None:
            interval = next_fib(interval, rec)
    return last.date + datetime.timedelta(days=interval)
