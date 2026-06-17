"""Unit tests for the V4 xform helpers (pure functions, no DB)."""

import datetime
import uuid
import pytest

from lmdb import models, xform


def _vid(i: int, extractor_key="youtube") -> models.VidFull:
    return models.VidFull(
        native_id=f"vid{i}",
        title=f"Video {i}",
        url=f"http://example/v/{i}",
        thumbnail_url=f"http://example/v/{i}/thumb.jpg",
        modified=datetime.datetime(2026, 1, i + 1),
        extractor_key=extractor_key,
        channel=models.UlChan(native_id="up1", title="Up One",
                                url="http://example/up1"),
    )


def _pl(n=3) -> models.PlaylistFull:
    return models.PlaylistFull(
        url="http://example/pl/1",
        native_id="pl1",
        title="My Playlist",
        modified=datetime.datetime(2026, 1, 31),
        playlist_count=n,
        extractor_key="youtube",
        channel=models.UlChan(native_id="up1", title="Up One",
                                url="http://example/up1"),
        entries=[_vid(i) for i in range(n)],
    )


def test_thing_from_vid_passes_container_through():
    # A known leaf defaults to container=False; an ambiguous flat url-result (container=None)
    # is carried through so its own pull classifies it later (#158).
    assert xform.thing_from_vid(models.VidFull(native_id="v")).container is False
    assert xform.thing_from_vid(
        models.VidFull(native_id="v", container=None)).container is None


def test_pl_full2things_no_channel_url():
    pl = _pl(1)
    pl.channel = models.UlChan()  # no urls on the playlist...
    for vid in pl.entries:
        vid.channel = models.UlChan()  # ...nor any entry
    g = xform.pl_full2things(pl, bucket="b")
    assert g.channels == []
    assert not any(r.channel for r in g.rels)   # no uploader edges without a channel url


def test_pl_hash_order_independent():
    pl = _pl(3)
    h1 = xform.pl_hash(pl.entries)
    h2 = xform.pl_hash(list(reversed(pl.entries)))
    assert h1 == h2
    assert isinstance(h1, bytes)


def test_pl_hash_changes_on_membership():
    base = _pl(3)
    more = _pl(4)
    assert xform.pl_hash(base.entries) != xform.pl_hash(more.entries)


def test_reconcile_count_mismatch_warns():
    pl = _pl(3)
    pl.playlist_count = 5
    with pytest.warns(UserWarning):
        count = xform.reconcile_count(pl)
    assert count == 5  # provided wins


def test_pl_full2things_does_not_set_last_success():
    # The builder is pure construction now; the API endpoint owns the last_success decision.
    g = xform.pl_full2things(_pl(2), bucket="b")
    assert all(v.last_success_dt is None for v in g.videos)


# --- try_on backoff (Task 1.4): pure math ----------------------------------------------

def _run_on(day, success, h=None):
    """A bare models.Run for the pure backoff helpers (no DB)."""
    return models.Run(thing_id=uuid.uuid4(), success=success, entries_hash=h,
                      starttime=datetime.datetime(2026, 1, day, 12, 0))


def test_initial_interval_bands():
    assert xform.initial_interval(2.0) == 3    # A
    assert xform.initial_interval(1.0) == 5    # B
    assert xform.initial_interval(0.0) == 8    # C
    assert xform.initial_interval(-1.0) == 8   # D/below falls in the C interval


def test_next_try_on_first_success_uses_initial():
    today = datetime.date(2026, 1, 10)
    runs = [_run_on(10, True, b"h1")]
    assert xform.next_try_on(1.0, runs, today) == today + datetime.timedelta(days=5)   # B
    assert xform.next_try_on(2.0, runs, today) == today + datetime.timedelta(days=3)   # A


def test_next_try_on_backs_off_when_unchanged():
    # 5-day cadence, identical membership hash -> back off (fib up: 5 -> 8)
    runs = [_run_on(d, True, b"same") for d in (1, 6, 11, 16)]
    today = datetime.date(2026, 1, 16)
    assert xform.next_try_on(1.0, runs, today) == today + datetime.timedelta(days=8)


def test_next_try_on_speeds_up_when_changing():
    # every run finds new content -> speed up (fib down: 5 -> 3)
    runs = [_run_on(d, True, bytes([i])) for i, d in enumerate((1, 6, 11, 16))]
    today = datetime.date(2026, 1, 16)
    assert xform.next_try_on(1.0, runs, today) == today + datetime.timedelta(days=3)


def test_next_try_on_failure_after_success_tomorrow():
    runs = [_run_on(1, True, b"h"), _run_on(6, False)]
    assert xform.next_try_on(1.0, runs, datetime.date(2026, 1, 6)) == datetime.date(2026, 1, 7)


def test_next_try_on_consecutive_failures_back_off():
    # prior success then two failures -> fib backoff from the B initial (5 -> 8)
    runs = [_run_on(1, True, b"h"), _run_on(6, False), _run_on(11, False)]
    today = datetime.date(2026, 1, 11)
    assert xform.next_try_on(1.0, runs, today) == today + datetime.timedelta(days=8)
