"""analytics and transformation"""

import statistics
import datetime
from typing import Optional
import hashlib
import warnings
from . import models

FIB = [1, 2, 3, 5, 8, 13, 21, 34]

def compare_pl_runs(old: models.PlaylistStatsBinHash, new: models.PlaylistStatsBinHash) -> bool:
    """Determine whether a playlist changed between runs"""
    assert new.timestamp >= old.timestamp
    assert new.timestamp >= new.modified_date
    if new.newest_item is not None:
        assert new.timestamp >= new.newest_item
    for ck in ['modified_date', 'playlist_count', 'entries_hash', 'newest_item', 'success']:
        if getattr(new, ck) != getattr(old, ck):
            return True
    if new.download_count:
        return True
    if new.modified_date > old.timestamp:
        return True
    if new.newest_item and new.newest_item > old.timestamp:
        return True
    return False

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
    """Change interval uo or down"""
    med = statistics.median(existing)
    return next_fib(med, up)

def rec_adjust_freq(runs: list[models.PlaylistStatsBinHash]) -> Optional[int]:
    """Recommend a different frequency"""
    intervals = [x.interval for x in runs if x.interval is not None]
    if all(not x.success for x in runs):
        return adjust(intervals, True)
    if all(not x.different for x in runs):
        return adjust(intervals, True)
    if all(x.different for x in runs):
        return adjust(intervals, False)
    return None

def sort_runs(runs: list[models.PlaylistStatsBinHash]) -> list[models.PlaylistStatsBinHash]:
    """Sort run stats"""
    runs.sort(key=lambda x: x.timestamp)
    return runs

def next_run(runs: list[models.PlaylistStatsBinHash], interval: int) -> datetime.date:
    """Determine when next run should be based on recent runs and some stats"""
    runs = sort_runs(runs)
    last_date = runs[-1].timestamp.date()
    if not runs[-1].success and not all(not x.success for x in runs):
        if runs[-2].success:
            return last_date + datetime.timedelta(days=1)
        return last_date + datetime.timedelta(days=next_fib(runs[-1].interval, True))
    return last_date + datetime.timedelta(days=interval)

def rum_deltas(old: models.PlaylistStatsBinHash,
               new: models.PlaylistStatsBinHash) -> models.PlaylistStatsBinHash:
    """Determine difference between two runs and populate the new with stats"""
    assert new.timestamp >= old.timestamp
    new.interval = (new.timestamp.date() - old.timestamp.date()).days
    new.different = compare_pl_runs(old, new)
    return new

def add_new_run(schedule: models.PlaylistSched,
                existing: list[models.PlaylistStatsBinHash],
                new: models.PlaylistStatsBinHash) -> tuple[models.PlaylistSched,
                                                    list[models.PlaylistStatsBinHash],
                                                    models.PlaylistStatsBinHash]:
    """Go through motions of adding a new run stats summary"""
    existing = sort_runs(existing)
    if existing:
        new = rum_deltas(existing[-1], new)
    else:
        new.different = True
        new.interval = 0
    existing.append(new)
    new_freq = rec_adjust_freq(existing[-3:])
    assert schedule.freq_days is not None
    if new_freq and new_freq != schedule.freq_days:
        schedule.freq_days = next_fib(schedule.freq_days, new_freq > schedule.freq_days)
    schedule.next_run = next_run(existing[-3:], schedule.freq_days)
    return schedule, existing, new

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

def full2stats(inputpl: models.PlaylistFull,
               download_count: int) -> models.PlaylistStatsBinHash:
    """Convert a 'full' LM-Native playlist into stats
    
    The stats can be easily stored in a DB and used for future analysis
    """
    count = len(inputpl.entries)
    if inputpl.playlist_count is None:
        warnings.warn(f'No provided playlist_count; leveraging length of {count}.')
    elif count != inputpl.playlist_count:
        warnings.warn(f"Provided playlist count {inputpl.playlist_count} doesn't match actual length of {count}; will record provided.")
        count = inputpl.playlist_count
    # TODO model rework based on below analysis
    if not inputpl.modified_date:
        inputpl.modified_date = datetime.datetime.now()
    return models.PlaylistStatsBinHash(modified_date=inputpl.modified_date,
                                playlist_count=count,
                                entries_hash=pl_hash(inputpl.entries),
                                success=True,  # assuming True since we have a playlist (indiv vid retry seperate flow?)
                                download_count=download_count,  # extend VidFull to indicate dl or not?
                                input_params='',  # allow arg override! optional?
                                output_params='',  # new func arg??? optional?
                                timestamp=datetime.datetime.now(),  # allow arg override?
                                newest_item=newest(inputpl.entries).upload_date if inputpl.entries else None,
                                different=True,  # TODO feed it to him later
                                interval=0)  # TODO feed it to him later

#def failed_stat() -> models.PlaylistStats:
#    """Create a 'failed' playlist stat entry"""
#    return models.PlaylistStats(playlist_count=0,
#                                entries_hash=pl_hash([]),
#                                success=False,
#                                download_count=0,
#                                timestamp=datetime.datetime.now())

def full2sum(inputpl: models.PlaylistFull) -> models.PlaylistSumWithVids:
    """Summarize playlist"""
    # TODO use model_validate?
    # TODO validate count, carefully
    # TODO generate pseudo playlists for channels
    assert inputpl.extractor.extractor is not None
    return models.PlaylistSumWithVids(id=inputpl.id,
                              title=inputpl.title,
                              modified_date=inputpl.modified_date,
                              webpage_url=inputpl.webpage_url,
                              playlist_count=inputpl.playlist_count,  # validate, carefully
                              channel=inputpl.channel.uploader_url or inputpl.channel.channel_url,
                              entries=[entry2text(x) for x in inputpl.entries],
                              extractor_id=inputpl.extractor.extractor)  # is this right?

def pl_dlp2lm(dlpin: models.PlaylistDLP) -> models.PlaylistFull:
    """Raw DLP playlist to LM-native playlist"""
    assert dlpin.webpage_url is not None  # we need this until we ger lmpl id
    # TODO use model_validate?
    retv = models.PlaylistFull(id=dlpin.id,
                               title=dlpin.title,
                               modified_date=datetime.datetime.fromtimestamp(dlpin.epoch),  # TODO
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
