"""Tests for lmdb.scrub_oi (the V4 OI scrubber, #111).

Selection tests are DB-backed (throwaway PostgreSQL via pytest-postgresql, as in
test_api.py) because the A-band set rides the compute-on-read machine-rating SQL.
Action tests are pure logic (no DB, no network): a fake OI client plus monkeypatched
locator/replication functions, in the test_job_runner.py style.
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


# --- fakes for the action tests ----------------------------------------------------------

class FakeOI:
    """obj_idx client stand-in: file UUID -> object dict; records PUTs.

    `honor_delete` mimics a future OI that really tombstones (objectindex#23); False
    mimics today's silent refusal for completed objects (the PUT returns it unchanged).
    """

    def __init__(self, objects: dict, honor_delete: bool = False):
        self.objects = objects
        self.honor_delete = honor_delete
        self.puts = []

    def get_file(self, fil_uuid):
        obj = self.objects.get(fil_uuid)
        if obj == "missing":
            raise requests.HTTPError("404 File not found")
        return SimpleNamespace(object=obj)

    def put_object(self, obj_uuid, info):
        self.puts.append((obj_uuid, info))
        obj = next(o for o in self.objects.values()
                   if o not in (None, "missing") and o["uuid"] == obj_uuid)
        if self.honor_delete:
            obj = {**obj, "deleted": True}
        return obj


def _mem_thing(rating=None) -> Thing:
    return Thing(bucket="b", container=False, human_rating=rating, best_oi=uuid.uuid4())


def _obj(completed=True, deleted=False, bucket="b", key="k", size=100):
    return {"uuid": str(uuid.uuid4()), "bucket": bucket, "key": key,
            "obj_size": size, "completed": completed, "deleted": deleted}


# --- deletion actions ---------------------------------------------------------------------

def test_delete_applied_and_honored():
    thing = _mem_thing(rating=-1.0)
    oi = FakeOI({thing.best_oi: _obj()}, honor_delete=True)
    tally = scrub_oi.scrub_deletions([thing], oi, apply=True)
    assert [p[1] for p in oi.puts] == [{"deleted": True}]
    assert (tally.acted, tally.pending, tally.anomalies) == (1, 0, 0)


def test_delete_refused_is_pending_not_anomaly():
    # Today's OI: PUT deleted=True on a completed object comes back unchanged.
    thing = _mem_thing(rating=-2.0)
    oi = FakeOI({thing.best_oi: _obj()}, honor_delete=False)
    tally = scrub_oi.scrub_deletions([thing], oi, apply=True)
    assert len(oi.puts) == 1
    assert (tally.acted, tally.pending, tally.anomalies) == (0, 1, 0)


def test_delete_already_tombstoned_is_noop():
    thing = _mem_thing(rating=-1.0)
    oi = FakeOI({thing.best_oi: _obj(deleted=True)})
    tally = scrub_oi.scrub_deletions([thing], oi, apply=True)
    assert oi.puts == []
    assert (tally.ok, tally.acted, tally.anomalies) == (1, 0, 0)


def test_delete_dry_run_touches_nothing():
    thing = _mem_thing(rating=-1.0)
    oi = FakeOI({thing.best_oi: _obj()}, honor_delete=True)
    tally = scrub_oi.scrub_deletions([thing], oi, apply=False)
    assert oi.puts == []
    assert tally.acted == 1  # reported as "would delete"


def test_delete_missing_oi_file_is_anomaly():
    thing = _mem_thing(rating=-1.0)
    tally = scrub_oi.scrub_deletions([thing], FakeOI({thing.best_oi: "missing"}), apply=True)
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
