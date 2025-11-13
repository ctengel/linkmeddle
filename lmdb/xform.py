"""analytics and transformation"""

import statistics
import datetime
from typing import Optional
from . import models

FIB = [1, 2, 3, 5, 8, 13, 21, 34]

def compare_pl_runs(old: models.PlaylistStats, new: models.PlaylistStats) -> bool:
    """Determine whether a playlist changed between runs"""
    assert new.timestamp >= old.timestamp
    assert new.timestamp >= new.modified_date
    assert new.timestamp >= new.newest_item
    for ck in ['modified_date', 'playlist_count', 'entries_hash', 'newest_item', 'success']:
        if getattr(new, ck) != getattr(old, ck):
            return True
    if new.download_count:
        return True
    if new.modified_date > old.timestamp:
        return True
    if new.newest_item > old.timestamp:
        return True
    return False

def next_fib(existing: int | float, up: bool) -> int:
    """Next fibonacci number up or down"""
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

def rec_adjust_freq(runs: list[models.PlaylistStats]) -> Optional[int]:
    """Recommend a different frequency"""
    intervals = [x.interval for x in runs]
    if all(not x.success for x in runs):
        return adjust(intervals, True)
    if all(not x.different for x in runs):
        return adjust(intervals, True)
    if all(x.different for x in runs):
        return adjust(intervals, False)
    return None

def sort_runs(runs: list[models.PlaylistStats]) -> list[models.PlaylistStats]:
    """Sort run stats"""
    runs.sort(key=lambda x: x.timestamp)
    return runs

def next_run(runs: list[models.PlaylistStats], interval: int) -> datetime.date:
    """Determine when next run should be based on recent runs and some stats"""
    runs = sort_runs(runs)
    last_date = runs[-1].timestamp.date()
    if not runs[-1].success and not all(not x.success for x in runs):
        if runs[-2].success:
            return last_date + datetime.timedelta(days=1)
        return last_date + datetime.timedelta(days=next_fib(runs[-1].interval, True))
    return last_date + datetime.timedelta(days=interval)

def rum_deltas(old: models.PlaylistStats, new: models.PlaylistStats) -> models.PlaylistStats:
    """Determine difference between two runs and populate the new with stats"""
    assert new.timestamp >= old.timestamp
    new.interval = (new.timestamp.date() - old.timestamp.date()).days
    new.different = compare_pl_runs(old, new)
    return new

def add_new_run(schedule: models.PlaylistSched,
                existing: list[models.PlaylistStats],
                new: models.PlaylistStats) -> tuple[models.PlaylistSched,
                                                    list[models.PlaylistStats],
                                                    models.PlaylistStats]:
    """Go through motions of adding a new run stats summary"""
    existing = sort_runs(existing)
    new = rum_deltas(existing[-1], new)
    existing.append(new)
    new_freq = rec_adjust_freq(existing[-3:])
    if new_freq and new_freq != schedule.freq_days:
        schedule.freq_days = next_fib(schedule.freq_days, new_freq > schedule.freq_days)
    schedule.next_run = next_run(existing[-3:], schedule.freq_days)
    return schedule, existing, new
