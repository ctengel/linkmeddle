"""Unit tests for the V4 xform helpers (pure functions, no DB)."""

import datetime
import uuid
import pytest

from lmdb import models, xform


def _vid(i: int, extractor_key="youtube") -> models.PullThing:
    return models.PullThing(
        native_id=f"vid{i}",
        title=f"Video {i}",
        url=f"http://example/v/{i}",
        thumbnail_url=f"http://example/v/{i}/thumb.jpg",
        modified=datetime.datetime(2026, 1, i + 1),
        extractor_key=extractor_key,
        channel=models.UlChan(native_id="up1", title="Up One",
                                url="http://example/up1"),
    )


def _pl(n=3) -> models.PullThing:
    return models.PullThing(
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
    assert xform.thing_from_node(models.PullThing(native_id="v")).container is False
    assert xform.thing_from_node(
        models.PullThing(native_id="v", container=None)).container is None


def _pending_leaf(hint=None) -> models.Thing:
    return models.Thing(native_id="v", container=False, best_oi=None,
                        attrs=({xform.INFO_JSON_KEY: hint} if hint is not None else None))


def test_refresh_info_hint_no_flat_clobber():
    flat = {"_type": "url", "id": "v"}
    full = {"id": "v", "formats": [{"format_id": "best"}]}

    # full existing + flat incoming (re-pull) -> richer meta hint kept
    t = _pending_leaf(full)
    xform.refresh_info_hint(t, flat)
    assert t.attrs[xform.INFO_JSON_KEY] == full

    # flat existing + flat incoming (re-pull) -> refreshed to the newer flat entry
    newer_flat = {"_type": "url", "id": "v", "refreshed": True}
    t = _pending_leaf(flat)
    xform.refresh_info_hint(t, newer_flat)
    assert t.attrs[xform.INFO_JSON_KEY] == newer_flat

    # flat existing + full incoming (meta) -> upgraded
    t = _pending_leaf(flat)
    xform.refresh_info_hint(t, full)
    assert t.attrs[xform.INFO_JSON_KEY] == full

    # full existing + full incoming -> refreshed
    newer_full = {"id": "v", "formats": [{"format_id": "best"}], "refreshed": True}
    t = _pending_leaf(full)
    xform.refresh_info_hint(t, newer_full)
    assert t.attrs[xform.INFO_JSON_KEY] == newer_full

    # no existing + flat incoming -> stored
    t = _pending_leaf()
    xform.refresh_info_hint(t, flat)
    assert t.attrs[xform.INFO_JSON_KEY] == flat

    # acquired (best_oi set) -> left alone regardless of incoming
    t = _pending_leaf(full)
    t.best_oi = uuid.uuid4()
    xform.refresh_info_hint(t, {"id": "v", "formats": [{"format_id": "x"}]})
    assert t.attrs[xform.INFO_JSON_KEY] == full


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
    assert all(v.last_success_dt is None for v in g.members)


def _sub(native_id="sub1", url="http://example/pl/sub") -> models.PullThing:
    """A sub-container member (container=True), as extract_pull now yields in `entries`."""
    return models.PullThing(native_id=native_id, url=url, title="Sub PL",
                          extractor_key="youtube", container=True,
                          channel=models.UlChan(native_id="up1", title="Up One",
                                                url="http://example/up1"))


def test_pl_hash_video_and_subcontainer_keys_distinct():
    # A video and a sub-container sharing an id must not collide (the 'pl:' prefix).
    vid = models.PullThing(native_id="x", url="http://example/v/x", extractor_key="youtube")
    sub = models.PullThing(native_id="x", url="http://example/pl/x", extractor_key="youtube",
                         container=True)
    assert xform.pl_hash([vid]) != xform.pl_hash([sub])
    # Order independence still holds across mixed membership.
    assert xform.pl_hash([vid, sub]) == xform.pl_hash([sub, vid])


def test_pl_full2things_curated_subcontainer_is_membership_with_owner():
    # A curated playlist (node pl1, owned by up1) listing up1's sub-playlist: the parent node
    # is NOT the owner, so the sub gets a channel=False membership edge plus the owner's
    # channel=True edge -- exactly like a curated video.
    pl = _pl(0)                       # curated playlist (owner up1), no video entries
    pl.entries = [_sub()]
    g = xform.pl_full2things(pl, bucket="b")
    sub_thing = next(m for m in g.members if m.container is True)
    sub_edges = {(r.parent, r.channel) for r in g.rels if r.child == sub_thing.id}
    owner = next(c for c in g.channels if c.channel == "http://example/up1")
    assert (g.playlist.id, False) in sub_edges    # membership edge from the parent node
    assert (owner.id, True) in sub_edges          # owner (up1) channel=True edge


def test_pl_full2things_owned_subcontainer_is_channel():
    # A channel pull (parent node IS the owner) -> one channel=True edge, no owner node.
    chan = models.UlChan(native_id="chan1", title="Chan", url="http://example/chan1")
    pl = models.PullThing(url="http://example/chan1", native_id="chan1", title="Chan",
                             extractor_key="youtube", channel=chan,
                             entries=[_sub(native_id="tab1", url="http://example/chan1/vids")])
    pl.entries[0].channel = chan      # the tab is owned by the channel itself
    g = xform.pl_full2things(pl, bucket="b")
    sub_thing = next(m for m in g.members if m.container is True)
    sub_edges = [r for r in g.rels if r.child == sub_thing.id]
    assert len(sub_edges) == 1
    assert sub_edges[0].parent == g.playlist.id and sub_edges[0].channel is True
    assert g.channels == []           # parent is its own owner -> no separate node


def test_pl_full2things_unknown_owner_subcontainer_is_membership():
    # No-guess: a sub-container with no discernible owner -> channel=False membership (its
    # ownership edge is established later, when it is pulled itself), and no owner node.
    pl = _pl(0)
    pl.entries = [models.PullThing(native_id="sub1", url="http://example/pl/sub",
                                 title="Sub", extractor_key="youtube", container=True)]
    g = xform.pl_full2things(pl, bucket="b")
    sub_thing = next(m for m in g.members if m.container is True)
    sub_edges = [r for r in g.rels if r.child == sub_thing.id]
    assert len(sub_edges) == 1
    assert sub_edges[0].parent == g.playlist.id and sub_edges[0].channel is False
    assert all(c.channel != "http://example/pl/sub" for c in g.channels)  # no node for the sub


def _chan_with_tabs(parent_native="UC", parent_ek="youtubetab",
                    parent_url="http://yt/@chan/featured"):
    """A channel pull whose members are its Videos/Shorts/Live tabs -- each carrying the same
    `id` (channel_id) as the others (and the parent) but a distinct URL."""
    chan = models.UlChan(native_id="UC", title="Chan", url="http://yt/@chan")
    tab = lambda name: models.PullThing(
        native_id="UC", extractor_key="youtubetab", container=True,
        url=f"http://yt/@chan/{name}", title=f"Chan - {name}", channel=chan)
    return models.PullThing(
        url=parent_url, native_id=parent_native, extractor_key=parent_ek, title="Chan",
        channel=chan, container=True,
        entries=[tab("videos"), tab("shorts"), tab("streams")])


def test_facet_tabs_keyed_by_url_not_collapsed():
    # A channel's tabs share one id (channel_id) but differ by URL -> the facet rule nulls their
    # native_id so they stay distinct (URL-keyed) things, while the parent keeps the id.
    g = xform.pl_full2things(_chan_with_tabs(), bucket="b")
    assert g.playlist.native_id == "UC"                       # parent keeps the channel id
    assert all(m.native_id is None for m in g.members)        # tabs URL-keyed
    assert all((m.attrs or {}).get("channel_id") == "UC" for m in g.members)
    assert len({m.url for m in g.members}) == 3               # three distinct things
    # the parent IS the tabs' uploader -> one channel=True edge each, no separate owner node
    for m in g.members:
        edges = [r for r in g.rels if r.child == m.id]
        assert len(edges) == 1 and edges[0].parent == g.playlist.id and edges[0].channel is True
    assert g.channels == []


def test_facet_sibling_collision_when_parent_has_no_id():
    # Even when the parent carries no native_id, sibling sub-containers sharing one id (distinct
    # URLs) are facets and get URL-keyed.
    pl = _chan_with_tabs(parent_native=None, parent_ek=None, parent_url="http://yt/@chan")
    g = xform.pl_full2things(pl, bucket="b")
    assert all(m.native_id is None for m in g.members)
    assert len({m.url for m in g.members}) == 3


def test_facet_rule_leaves_distinct_subcontainers_alone():
    # Sub-containers with genuinely distinct ids are not facets -> native_id preserved.
    pl = _pl(0)
    pl.entries = [_sub(native_id="subA", url="http://example/pl/a"),
                  _sub(native_id="subB", url="http://example/pl/b")]
    g = xform.pl_full2things(pl, bucket="b")
    assert {m.native_id for m in g.members if m.container} == {"subA", "subB"}


def test_subtree_hash_equals_pl_hash_for_flat_pull():
    pl = _pl(3)
    assert xform.subtree_hash(pl) == xform.pl_hash(pl.entries)


def test_subtree_hash_tracks_grandchildren():
    # A parent that inlines a sub-container tracks changes below its direct members: adding a
    # grandchild video flips subtree_hash even though the direct membership is unchanged.
    sub = _sub(native_id="tabV", url="http://yt/@chan/videos")
    sub.entries = [_vid(0), _vid(1)]
    parent = models.PullThing(url="http://yt/@chan", native_id="UC", extractor_key="youtubetab",
                              container=True, entries=[sub])
    before = xform.subtree_hash(parent)
    direct_before = xform.pl_hash(parent.entries)     # direct membership = [sub]
    sub.entries.append(_vid(2))                       # a new grandchild video
    assert xform.subtree_hash(parent) != before       # subtree change is detected
    assert xform.pl_hash(parent.entries) == direct_before  # ...while direct membership is unchanged


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


def test_next_try_on_leaf_meta_loop_backs_off():
    # A leaf video's runs carry the empty-membership hash (api sets pl_hash([])); since it
    # never changes, a repeated-meta loop reads different=False -> back off (5 -> 8), not the
    # pre-fix accelerate-to-1-day caused by null hashes reading as always-different.
    leaf = xform.pl_hash([])
    runs = [_run_on(d, True, leaf) for d in (1, 6, 11, 16)]
    today = datetime.date(2026, 1, 16)
    assert xform.next_try_on(1.0, runs, today) == today + datetime.timedelta(days=8)


def test_next_try_on_same_day_runs_floor_at_one_day():
    # #7: two successful runs on the same calendar day -> _current_interval is 0. With a mixed
    # window (rec=None) the interval stays 0, which would schedule the thing immediately re-due;
    # the floor clamps it to at least 1 day past the last run.
    runs = [_run_on(1, True, b"h1"), _run_on(1, True, b"h2")]
    today = datetime.date(2026, 1, 1)
    assert xform.next_try_on(1.0, runs, today) == datetime.date(2026, 1, 2)


def test_next_try_on_failure_after_success_tomorrow():
    runs = [_run_on(1, True, b"h"), _run_on(6, False)]
    assert xform.next_try_on(1.0, runs, datetime.date(2026, 1, 6)) == datetime.date(2026, 1, 7)


def test_next_try_on_consecutive_failures_back_off():
    # prior success then two failures -> fib backoff from the B initial (5 -> 8)
    runs = [_run_on(1, True, b"h"), _run_on(6, False), _run_on(11, False)]
    today = datetime.date(2026, 1, 11)
    assert xform.next_try_on(1.0, runs, today) == today + datetime.timedelta(days=8)
