"""Tests for lmdb.scrub_oi (the V4 OI scrubber, #111).

Selection tests are DB-backed (throwaway PostgreSQL via pytest-postgresql, as in
test_api.py) because the band cohorts ride the compute-on-read machine-rating SQL.
Action tests are pure logic (no DB, no network): a fake OI client plus monkeypatched
locator/replication/deletion functions, in the test_job_runner.py style.
"""

import uuid
from types import SimpleNamespace
import httpx
import pytest
import requests
from sqlmodel import Session, SQLModel, create_engine
from pytest_postgresql import factories

from lmdb import scrub_oi
from lmdb.models import Thing, Rel

# Fedora keeps pg_ctl in /usr/bin (pytest-postgresql's default assumes a Debian path).
postgresql_proc = factories.postgresql_proc(executable="/usr/bin/pg_ctl")
postgresql = factories.postgresql("postgresql_proc")


@pytest.fixture
def session(postgresql):
    info = postgresql.info
    auth = f"{info.user}:{info.password}@" if info.password else f"{info.user}@"
    url = f"postgresql+psycopg://{auth}{info.host}:{info.port}/{info.dbname}"
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _thing(session, *, rating=None, best_oi=False, container=False, url=None) -> Thing:
    thing = Thing(url=url or f"http://example/{uuid.uuid4()}", bucket="b",
                  container=container, human_rating=rating,
                  best_oi=uuid.uuid4() if best_oi else None)
    session.add(thing)
    session.commit()
    return thing


# --- selection (DB-backed) --------------------------------------------------------------

def test_deletion_candidates_human_df_with_media_only(session):
    d = _thing(session, rating=-1.0, best_oi=True)
    f = _thing(session, rating=-2.0, best_oi=True)
    _thing(session, rating=-1.0, best_oi=False)   # D but never acquired
    _thing(session, rating=0.0, best_oi=True)     # C
    _thing(session, rating=1.0, best_oi=True)     # B
    got = {t.id for t in scrub_oi.deletion_candidates(session)}
    assert got == {d.id, f.id}


def test_deletion_ignores_machine_rating(session):
    # A video whose parent playlist is rated D assesses D by machine rating, but deletion
    # is driven by *human* ratings only (§2.4/Appendix A).
    parent = _thing(session, rating=-1.0, container=True)
    video = _thing(session, rating=None, best_oi=True)
    session.add(Rel(parent=parent.id, child=video.id))
    session.commit()
    assert scrub_oi.deletion_candidates(session) == []


def test_replication_candidates_effective_a_band(session):
    human_a = _thing(session, rating=2.0, best_oi=True)
    parent = _thing(session, rating=2.0, container=True)
    machine_a = _thing(session, rating=None, best_oi=True)
    session.add(Rel(parent=parent.id, child=machine_a.id))
    session.commit()
    _thing(session, rating=1.0, best_oi=True)     # B: 1 copy is fine
    _thing(session, rating=2.0, best_oi=False)    # A but never acquired
    got = {t.id for t in scrub_oi.replication_candidates(session)}
    assert got == {human_a.id, machine_a.id}


def test_replication_human_rating_overrides_machine(session):
    # Human F beats a machine A (human is authoritative, §2.4): not owed copies.
    parent = _thing(session, rating=2.0, container=True)
    video = _thing(session, rating=-2.0, best_oi=True)
    session.add(Rel(parent=parent.id, child=video.id))
    session.commit()
    assert scrub_oi.replication_candidates(session) == []


def test_reduction_candidates_below_b_not_human_df(session):
    human_c = _thing(session, rating=0.0, best_oi=True)
    unrated = _thing(session, rating=None, best_oi=True)   # effective defaults to 0 = C
    parent = _thing(session, rating=-1.0, container=True)
    machine_d = _thing(session, rating=None, best_oi=True)  # machine-D reduces, not deletes
    session.add(Rel(parent=parent.id, child=machine_d.id))
    session.commit()
    _thing(session, rating=1.0, best_oi=True)    # B: untouched either way
    _thing(session, rating=2.0, best_oi=True)    # A: the replication branch's cohort
    _thing(session, rating=-1.0, best_oi=True)   # human D: the deletion branch's cohort
    _thing(session, rating=0.0, best_oi=False)   # C but never acquired
    got = {t.id for t in scrub_oi.reduction_candidates(session)}
    assert got == {human_c.id, unrated.id, machine_d.id}


# --- fakes for the action tests ----------------------------------------------------------

class FakeOI:
    """obj_idx client stand-in: file UUID -> object dict."""

    def __init__(self, objects: dict):
        self.objects = objects

    def get_file(self, fil_uuid):
        obj = self.objects.get(fil_uuid)
        if obj == "missing":
            raise requests.HTTPError("404 File not found")
        return SimpleNamespace(object=obj)


def _mem_thing(rating=None) -> Thing:
    return Thing(bucket="b", container=False, human_rating=rating, best_oi=uuid.uuid4())


def _obj(completed=True, deleted=False, bucket="b", key="k", size=100):
    return {"uuid": str(uuid.uuid4()), "bucket": bucket, "key": key,
            "obj_size": size, "completed": completed, "deleted": deleted}


# --- deletion actions ---------------------------------------------------------------------

def _patch_delete(monkeypatch, result=True):
    """Fake oic.delete_object_data (raising=False: the installed obj_idx may predate
    0.3.8, where the function first appears); an Exception result is raised."""
    calls = []

    def fake(objidx, objid, locator):
        calls.append((objid, locator))
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(scrub_oi.oic, "delete_object_data", fake, raising=False)
    return calls


def test_delete_applied_and_confirmed(monkeypatch):
    thing = _mem_thing(rating=-1.0)
    obj = _obj()
    calls = _patch_delete(monkeypatch, result=True)
    tally = scrub_oi.scrub_deletions([thing], FakeOI({thing.best_oi: obj}),
                                     "http://loc/", apply=True)
    assert calls == [(obj["uuid"], "http://loc/")]
    assert (tally.acted, tally.pending, tally.anomalies) == (1, 0, 0)


def test_delete_unconfirmed_is_pending_not_anomaly(monkeypatch):
    # delete_object_data ran out of retries (a store server busy/unreachable): the
    # record is left unmarked and a future scrub converges.
    thing = _mem_thing(rating=-2.0)
    calls = _patch_delete(monkeypatch, result=False)
    tally = scrub_oi.scrub_deletions([thing], FakeOI({thing.best_oi: _obj()}),
                                     "http://loc/", apply=True)
    assert len(calls) == 1
    assert (tally.acted, tally.pending, tally.anomalies) == (0, 1, 0)


def test_delete_never_completed_is_anomaly(monkeypatch):
    # best_oi behind an incomplete upload: delete_object_data refuses (ValueError,
    # scrub --clear territory) rather than tombstoning the key mid-upload.
    thing = _mem_thing(rating=-1.0)
    _patch_delete(monkeypatch, result=ValueError("upload not completed"))
    tally = scrub_oi.scrub_deletions([thing], FakeOI({thing.best_oi: _obj()}),
                                     "http://loc/", apply=True)
    assert (tally.anomalies, tally.acted, tally.pending) == (1, 0, 0)


def test_delete_http_error_is_anomaly(monkeypatch):
    # e.g. the locator identity lacks the `delete` RBAC permission (401/403).
    thing = _mem_thing(rating=-1.0)
    _patch_delete(monkeypatch, result=requests.HTTPError("403 Forbidden"))
    tally = scrub_oi.scrub_deletions([thing], FakeOI({thing.best_oi: _obj()}),
                                     "http://loc/", apply=True)
    assert (tally.anomalies, tally.acted, tally.pending) == (1, 0, 0)


def test_delete_already_tombstoned_is_noop(monkeypatch):
    thing = _mem_thing(rating=-1.0)
    calls = _patch_delete(monkeypatch)
    tally = scrub_oi.scrub_deletions([thing], FakeOI({thing.best_oi: _obj(deleted=True)}),
                                     "http://loc/", apply=True)
    assert calls == []
    assert (tally.ok, tally.acted, tally.anomalies) == (1, 0, 0)


def test_delete_dry_run_touches_nothing(monkeypatch):
    thing = _mem_thing(rating=-1.0)
    calls = _patch_delete(monkeypatch)
    tally = scrub_oi.scrub_deletions([thing], FakeOI({thing.best_oi: _obj()}),
                                     "http://loc/", apply=False)
    assert calls == []
    assert tally.acted == 1  # reported as "would delete"


def test_delete_missing_oi_file_is_anomaly(monkeypatch):
    thing = _mem_thing(rating=-1.0)
    calls = _patch_delete(monkeypatch)
    tally = scrub_oi.scrub_deletions([thing], FakeOI({thing.best_oi: "missing"}),
                                     "http://loc/", apply=True)
    assert calls == []
    assert (tally.anomalies, tally.acted) == (1, 0)


# --- replication actions -------------------------------------------------------------------

def _patch_locator(monkeypatch, listing, targets=("http://srv2/",)):
    calls = {"replicated": [], "find_space": []}
    monkeypatch.setattr(scrub_oi, "_bucket_listing", lambda locator, bucket: listing)
    monkeypatch.setattr(scrub_oi, "find_space",
                        lambda *a: calls["find_space"].append(a) or list(targets))
    monkeypatch.setattr(scrub_oi, "replicate_object",
                        lambda src, dst: calls["replicated"].append((src, dst)))
    return calls


def test_replicate_under_replicated(monkeypatch):
    thing = _mem_thing(rating=2.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_locator(monkeypatch,
                           {"k": {"locations": ["http://srv1/"], "error": None}})
    tally = scrub_oi.scrub_replication([thing], oi, "http://loc/", apply=True)
    assert calls["replicated"] == [("http://srv1/b/k", "http://srv2/b/k")]
    assert (tally.acted, tally.anomalies) == (1, 0)


def test_replicate_at_count_is_noop(monkeypatch):
    thing = _mem_thing(rating=2.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_locator(
        monkeypatch, {"k": {"locations": ["http://srv1/", "http://srv2/"], "error": None}})
    tally = scrub_oi.scrub_replication([thing], oi, "http://loc/", apply=True)
    assert calls["replicated"] == []
    assert (tally.ok, tally.acted, tally.anomalies) == (1, 0, 0)


def test_replicate_dry_run_touches_nothing(monkeypatch):
    thing = _mem_thing(rating=2.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_locator(monkeypatch,
                           {"k": {"locations": ["http://srv1/"], "error": None}})
    tally = scrub_oi.scrub_replication([thing], oi, "http://loc/", apply=False)
    assert calls["replicated"] == [] and calls["find_space"] == []
    assert tally.acted == 1  # reported as "would replicate"


@pytest.mark.parametrize("listing", [
    {},                                          # key missing from the locator
    {"k": {"locations": [], "error": None}},     # no copies anywhere
])
def test_replicate_lost_media_is_anomaly(monkeypatch, listing):
    thing = _mem_thing(rating=2.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_locator(monkeypatch, listing)
    tally = scrub_oi.scrub_replication([thing], oi, "http://loc/", apply=True)
    assert calls["replicated"] == []
    assert (tally.anomalies, tally.acted) == (1, 0)


def test_replicate_locator_error_skips(monkeypatch):
    # A copy may sit on a sleeping server (simpler-objects#76): flag, don't add a copy.
    thing = _mem_thing(rating=2.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_locator(monkeypatch,
                           {"k": {"locations": ["http://srv1/"], "error": "down"}})
    tally = scrub_oi.scrub_replication([thing], oi, "http://loc/", apply=True)
    assert calls["replicated"] == []
    assert tally.anomalies == 1


def test_replicate_tombstoned_a_band_is_anomaly(monkeypatch):
    thing = _mem_thing(rating=2.0)
    oi = FakeOI({thing.best_oi: _obj(deleted=True)})
    calls = _patch_locator(monkeypatch, {"k": {"locations": ["http://srv1/"], "error": None}})
    tally = scrub_oi.scrub_replication([thing], oi, "http://loc/", apply=True)
    assert calls["replicated"] == []
    assert tally.anomalies == 1


def test_replicate_no_space_is_anomaly(monkeypatch):
    thing = _mem_thing(rating=2.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_locator(monkeypatch,
                           {"k": {"locations": ["http://srv1/"], "error": None}},
                           targets=())
    tally = scrub_oi.scrub_replication([thing], oi, "http://loc/", apply=True)
    assert calls["replicated"] == []
    assert (tally.anomalies, tally.acted) == (1, 0)


def test_replicate_listing_unavailable_is_anomaly(monkeypatch):
    thing = _mem_thing(rating=2.0)
    oi = FakeOI({thing.best_oi: _obj()})

    def boom(locator, bucket):
        raise httpx.HTTPError("503")
    monkeypatch.setattr(scrub_oi, "_bucket_listing", boom)
    tally = scrub_oi.scrub_replication([thing], oi, "http://loc/", apply=True)
    assert tally.anomalies == 1


# --- reduction actions ---------------------------------------------------------------------

SRV1, SRV2, SRV3 = "http://srv1/", "http://srv2/", "http://srv3/"


def _patch_reduction(monkeypatch, locations, used, statuses=()):
    """Wire a one-key listing, a fake health map, and a status-scripted _delete_copy."""
    calls = {"deleted": []}
    monkeypatch.setattr(scrub_oi, "_bucket_listing",
                        lambda locator, bucket: {"k": {"locations": list(locations),
                                                       "error": None}})
    monkeypatch.setattr(scrub_oi, "_server_used_bytes", lambda locator: used)

    def fake_delete(server, bucket, key):
        calls["deleted"].append(server)
        return dict(statuses).get(server, 204)
    monkeypatch.setattr(scrub_oi, "_delete_copy", fake_delete)
    return calls


def test_reduce_deletes_copy_on_fullest_server(monkeypatch):
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 10, SRV2: 999})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == [SRV2]
    assert (tally.acted, tally.pending, tally.anomalies) == (1, 0, 0)


def test_reduce_three_copies_keeps_least_used(monkeypatch):
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2, SRV3],
                             {SRV1: 5, SRV2: 50, SRV3: 500})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == [SRV3, SRV2]  # fullest first; SRV1 survives
    assert (tally.acted, tally.anomalies) == (1, 0)


def test_reduce_at_one_copy_is_noop(monkeypatch):
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1], {SRV1: 10})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == []
    assert (tally.ok, tally.acted, tally.anomalies) == (1, 0, 0)


def test_reduce_dry_run_touches_nothing(monkeypatch):
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 10, SRV2: 999})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=False)
    assert calls["deleted"] == []
    assert tally.acted == 1  # reported as "would reduce"


def test_reduce_busy_server_falls_through_to_next(monkeypatch):
    # Fullest copy is mid-upload/read-only: the next-fullest goes instead.
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 999, SRV2: 10},
                             statuses={SRV1: 503})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == [SRV1, SRV2]
    assert (tally.acted, tally.pending, tally.anomalies) == (1, 0, 0)


def test_reduce_all_copies_busy_is_pending_not_anomaly(monkeypatch):
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 999, SRV2: 10},
                             statuses={SRV1: 503, SRV2: 405})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == [SRV1, SRV2]
    assert (tally.acted, tally.pending, tally.anomalies) == (0, 1, 0)


def test_reduce_stale_listing_404_counts_as_removed(monkeypatch):
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 999, SRV2: 10},
                             statuses={SRV1: 404})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == [SRV1]
    assert (tally.acted, tally.anomalies) == (1, 0)


def test_reduce_hard_error_is_anomaly(monkeypatch):
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 999, SRV2: 10},
                             statuses={SRV1: 500})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == [SRV1]
    assert (tally.acted, tally.anomalies) == (0, 1)


def test_reduce_tombstoned_is_converged(monkeypatch):
    # A deleted-then-re-rated-up-to-C thing: nothing held, nothing to trim.
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj(deleted=True)})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 10, SRV2: 999})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == []
    assert (tally.ok, tally.anomalies) == (1, 0)


def test_reduce_incomplete_object_is_anomaly(monkeypatch):
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj(completed=False)})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 10, SRV2: 999})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == []
    assert tally.anomalies == 1


def test_reduce_locator_error_flag_skips(monkeypatch):
    # A copy may sit on a sleeping server (simpler-objects#76): deleting a visible
    # copy could orphan the key onto only the unreachable server. Flag, don't act.
    thing = _mem_thing(rating=0.0)
    oi = FakeOI({thing.best_oi: _obj()})
    calls = _patch_reduction(monkeypatch, [SRV1, SRV2], {SRV1: 10, SRV2: 999})
    monkeypatch.setattr(scrub_oi, "_bucket_listing",
                        lambda locator, bucket: {"k": {"locations": [SRV1, SRV2],
                                                       "error": "down"}})
    tally = scrub_oi.scrub_reduction([thing], oi, "http://loc/", apply=True)
    assert calls["deleted"] == []
    assert tally.anomalies == 1
