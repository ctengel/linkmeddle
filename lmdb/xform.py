"""analytics and transformation"""

import statistics
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


def adjust(existing: list[int], up: bool) -> int:
    """Change interval uo or down"""
    med = statistics.median(existing)
    if up:
        for i in FIB:
            if i > med:
                return i
        return FIB[-1]
    for i in sorted(FIB, reverse=True):
        if i < med:
            return i
    return FIB[0]


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


def next_run(runs, interval):
    pass
