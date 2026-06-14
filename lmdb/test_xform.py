"""Unit tests for the V4 xform helpers (pure functions, no DB)."""

import datetime
import uuid
import pytest

from lmdb import models, xform


def _vid(i: int, extractor_key="YouTube") -> models.VidFull:
    return models.VidFull(
        id=f"vid{i}",
        title=f"Video {i}",
        webpage_url=f"http://example/v/{i}",
        thumbnail=f"http://example/v/{i}/thumb.jpg",
        upload_date=datetime.datetime(2026, 1, i + 1),
        extractor=models.DLPIE(extractor_key=extractor_key, extractor="youtube"),
        channel=models.UlChan(uploader_id="up1", uploader="Up One",
                              uploader_url="http://example/up1"),
    )


def _pl(n=3) -> models.PlaylistFull:
    return models.PlaylistFull(
        id="pl1",
        title="My Playlist",
        webpage_url="http://example/pl/1",
        modified_date=datetime.datetime(2026, 1, 31),
        playlist_count=n,
        extractor=models.DLPIE(extractor_key="YouTube", extractor="youtube"),
        channel=models.UlChan(uploader_id="up1", uploader="Up One",
                              uploader_url="http://example/up1"),
        entries=[_vid(i) for i in range(n)],
    )


def test_pl_full2things_shape():
    g = xform.pl_full2things(_pl(3))
    # playlist thing
    assert g.playlist.type == "playlist"
    assert g.playlist.url == "http://example/pl/1"
    assert g.playlist.native_id == "pl1"
    assert g.playlist.extractor_key == "youtube"  # lowercased
    assert g.playlist.channel == "http://example/up1"
    # video stubs carry denormalized fields
    assert len(g.videos) == 3
    assert {v.type for v in g.videos} == {"video"}
    assert g.videos[0].title == "Video 0"
    assert g.videos[0].native_id == "vid0"
    assert g.videos[0].extractor_key == "youtube"
    # one channel thing
    assert len(g.channels) == 1
    assert g.channels[0].type == "channel"
    assert g.channels[0].url == "http://example/up1"


def test_pl_full2things_edges():
    g = xform.pl_full2things(_pl(3))
    pv = [r for r in g.rels if r.type == "playlist_video"]
    cp = [r for r in g.rels if r.type == "channel_playlist"]
    assert len(pv) == 3
    assert all(r.parent == g.playlist.id for r in pv)
    assert {r.child for r in pv} == {v.id for v in g.videos}
    assert len(cp) == 1
    assert cp[0].parent == g.channels[0].id
    assert cp[0].child == g.playlist.id


def test_pl_full2things_no_channel_url():
    pl = _pl(1)
    pl.channel = models.UlChan()  # no urls
    g = xform.pl_full2things(pl)
    assert g.channels == []
    assert not any(r.type == "channel_playlist" for r in g.rels)


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


def test_full2run():
    tid = uuid.uuid4()
    run = xform.full2run(_pl(3), thing_id=tid)
    assert run.thing_id == tid
    assert run.success is True
    assert run.playlist_count == 3
    assert run.entries_hash == xform.pl_hash(_pl(3).entries)
    assert run.starttime is not None


def test_full2run_count_mismatch_warns():
    pl = _pl(3)
    pl.playlist_count = 5
    with pytest.warns(UserWarning):
        run = xform.full2run(pl, thing_id=uuid.uuid4())
    assert run.playlist_count == 5  # provided wins


def test_runs_differ():
    tid = uuid.uuid4()
    same_a = xform.full2run(_pl(3), thing_id=tid)
    same_b = xform.full2run(_pl(3), thing_id=tid)
    changed = xform.full2run(_pl(4), thing_id=tid)
    assert xform.runs_differ(same_a, same_b) is False
    assert xform.runs_differ(same_a, changed) is True
