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


def pl_full2things(pl: models.PlaylistFull) -> ThingGraph:
    """Convert an LM-native playlist into its thing/rel graph.

    Produces the playlist thing, a stub thing per entry, the playlist's own channel
    thing, and the edges between them (`playlist_video`, `channel_playlist`). The
    returned objects carry client-side UUIDs, so the edges already reference real ids.
    """
    pl_thing = thing_from_pl(pl)
    videos: list[models.Thing] = []
    channels: list[models.Thing] = []
    rels: list[models.Rel] = []
    for vid in pl.entries:
        vid_thing = thing_from_vid(vid)
        videos.append(vid_thing)
        rels.append(models.Rel(parent=pl_thing.id, child=vid_thing.id,
                               type='playlist_video'))
    chan = thing_from_chan(pl.channel, pl_thing.extractor_key)
    if chan is not None:
        channels.append(chan)
        rels.append(models.Rel(parent=chan.id, child=pl_thing.id,
                               type='channel_playlist'))
    return ThingGraph(playlist=pl_thing, videos=videos, channels=channels, rels=rels)


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
