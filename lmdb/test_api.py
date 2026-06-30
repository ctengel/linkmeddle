"""pytest-based tests for lmdb.api FastAPI endpoints (V4 thing/rel/run surface).

Runs against a throwaway PostgreSQL instance spun up by pytest-postgresql (using the
system pg binaries). The V4 schema is Postgres+JSONB, so SQLite is no longer usable.
"""

import datetime
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine, Session, SQLModel
from pytest_postgresql import factories

from lmdb import api
from lmdb import models

# Fedora keeps pg_ctl in /usr/bin (pytest-postgresql's default assumes a Debian path).
postgresql_proc = factories.postgresql_proc(executable="/usr/bin/pg_ctl")
postgresql = factories.postgresql("postgresql_proc")


@pytest.fixture
def client(postgresql):
    info = postgresql.info
    auth = f"{info.user}:{info.password}@" if info.password else f"{info.user}@"
    url = f"postgresql+psycopg://{auth}{info.host}:{info.port}/{info.dbname}"
    engine = create_engine(url)
    # replace api engine and recreate tables
    api.engine = engine
    SQLModel.metadata.create_all(engine)

    def get_session_override():
        with Session(engine) as s:
            yield s

    api.app.dependency_overrides[api.get_session] = get_session_override
    with TestClient(api.app) as c:
        yield c
    api.app.dependency_overrides.clear()


def _session() -> Session:
    """A session on the test engine, for seeding rows the (Phase 1) ingest can't yet."""
    return Session(api.engine)


# --- add-a-thing-by-URL ----------------------------------------------------------------

def test_add_thing_defaults(client):
    r = client.post("/things/", json={"url": "http://example/pl/1", "bucket": "b1"})
    assert r.status_code == 201
    t = r.json()
    assert t["container"] is None           # unknown -> NULL until the first pull classifies it
    assert t["human_rating"] == 0.0         # default C
    assert t["try_on"] == models.naive_utcnow().date().isoformat()  # app is UTC, not local
    assert t["bucket"] == "b1"              # required, round-trips ([A10])
    assert t["attrs"] is None               # no cookies/lpm_lib hints supplied
    assert t["extractor_key"] is None and t["native_id"] is None  # worker fills later


def test_add_thing_bucket_required(client):
    # bucket has no server default; omitting it is a validation error ([A10])
    r = client.post("/things/", json={"url": "http://example/pl/nobucket"})
    assert r.status_code == 422


def test_add_thing_hints_stored_in_attrs(client):
    # cookies/lpm_lib are optional soft hints stored in attrs ([A11])
    r = client.post("/things/", json={"url": "http://example/pl/hints", "bucket": "b",
                                      "cookies": True, "lpm_lib": "mylib"})
    assert r.status_code == 201
    assert r.json()["attrs"] == {"cookies": True, "lpm_lib": "mylib"}


@pytest.mark.parametrize("value", [2.0, 1.0, 0.0])
def test_add_thing_rating_override(client, value):
    r = client.post("/things/", json={"url": f"http://example/pl/{value}", "rating": value,
                                      "bucket": "b"})
    assert r.status_code == 201
    assert r.json()["human_rating"] == value
    # add response carries computed ratings like a read: human is authoritative (§2.4)
    assert r.json()["effective_rating"] == value
    assert r.json()["machine_rating"] is None


def test_add_thing_container_override(client):
    r = client.post("/things/", json={"url": "http://example/v/1", "container": False,
                                      "bucket": "b"})
    assert r.status_code == 201
    assert r.json()["container"] is False   # leaf
    assert r.json()["attrs"] is None        # channel-ness is discovered on the pull, not at add


def test_add_thing_invalid_rating(client):
    # numeric ratings only; D/F (< 0) are rejected at add time (ge=0)
    r = client.post("/things/", json={"url": "http://example/pl/x", "rating": -1.0,
                                      "bucket": "b"})
    assert r.status_code == 422


def test_add_thing_idempotent(client):
    # #142: duplicate URL must not create a second row
    r1 = client.post("/things/", json={"url": "http://example/dup", "bucket": "first"})
    assert r1.status_code == 201
    r2 = client.post("/things/", json={"url": "http://example/dup", "rating": 2.0,
                                       "bucket": "second"})
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["human_rating"] == 0.0  # unchanged; existing returned as-is
    assert r2.json()["bucket"] == "first"    # bucket is immutable ([A10])


def test_add_thing_existing_ignores_container_hint(client):
    # Idempotent on URL returns the existing thing as-is; container hint in re-add is ignored.
    r1 = client.post("/things/", json={"url": "http://example/bf", "bucket": "b"})
    assert r1.status_code == 201 and r1.json()["container"] is None
    r2 = client.post("/things/", json={"url": "http://example/bf", "bucket": "b",
                                       "container": True})
    assert r2.status_code == 200 and r2.json()["container"] is None  # unchanged; use PATCH to modify


# --- list / search ---------------------------------------------------------------------

def test_list_things_empty(client):
    r = client.get("/things/")
    assert r.status_code == 200
    assert r.json() == []


def test_list_filters(client):
    client.post("/things/", json={"url": "http://example/p1", "container": True, "bucket": "b"})
    client.post("/things/", json={"url": "http://example/p2", "container": True,
                                  "rating": 2.0, "bucket": "b"})
    client.post("/things/", json={"url": "http://example/v1", "container": False, "bucket": "b"})

    assert len(client.get("/things/", params={"container": True}).json()) == 2
    assert len(client.get("/things/", params={"container": False}).json()) == 1
    assert len(client.get("/things/", params={"rating": 2.0}).json()) == 1
    one = client.get("/things/", params={"url": "http://example/v1"}).json()
    assert len(one) == 1 and one[0]["container"] is False
    # everything added via POST has a human_rating, so needs_rating is empty
    assert client.get("/things/", params={"needs_rating": True}).json() == []
    # all added with try_on=today -> all due
    assert len(client.get("/things/", params={"due": True}).json()) == 3


def test_extractor_native_lookup(client):
    # the V4 replacement for GET /videos/{extractor}/{id}; extractor/native are set by
    # the worker (Phase 1), so seed directly here.
    with _session() as s:
        s.add(models.Thing(url="http://example/vid", container=False, bucket="testbucket",
                           extractor_key="youtube", native_id="abc123"))
        s.commit()
    r = client.get("/things/", params={"extractor": "YouTube", "native_id": "abc123"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["native_id"] == "abc123"


# --- get / related / runs --------------------------------------------------------------

def test_get_thing_404(client):
    r = client.get(f"/things/{uuid.uuid4()}")
    assert r.status_code == 404


def test_get_thing_and_related(client):
    pl = models.Thing(url="http://example/pl", container=True, title="PL", bucket="testbucket")
    vid = models.Thing(url="http://example/vid2", container=False, title="V", bucket="testbucket")
    with _session() as s:
        s.add(pl)
        s.add(vid)
        s.commit()
        s.refresh(pl)
        s.refresh(vid)
        s.add(models.Rel(parent=pl.id, child=vid.id, channel=False))
        s.commit()
        pl_id, vid_id = str(pl.id), str(vid.id)

    # plain get: just the thing (neighbors are a separate /related call)
    base = client.get(f"/things/{pl_id}").json()
    assert base["id"] == pl_id and "related" not in base

    # /related from the playlist -> the video as a child
    full = client.get(f"/things/{pl_id}/related").json()
    assert len(full) == 1
    edge = full[0]
    assert edge["direction"] == "child"
    assert edge["channel"] is False         # plain membership, not an uploader edge
    assert edge["thing"]["id"] == vid_id

    # from the video's side it's a parent edge
    rel = client.get(f"/things/{vid_id}/related").json()
    assert rel[0]["direction"] == "parent"
    assert rel[0]["thing"]["id"] == pl_id
    # direction filter narrows it
    assert client.get(f"/things/{vid_id}/related", params={"direction": "child"}).json() == []


def test_thing_runs(client):
    pl = models.Thing(url="http://example/plruns", container=True, bucket="testbucket")
    with _session() as s:
        s.add(pl)
        s.commit()
        s.refresh(pl)
        s.add(models.Run(thing_id=pl.id, entries_hash=b"\x01\x02", playlist_count=3,
                         success=True))
        s.commit()
        pl_id = str(pl.id)
    r = client.get(f"/things/{pl_id}/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["entries_hash"] == "0102"  # hex-encoded
    assert runs[0]["playlist_count"] == 3
    assert runs[0]["success"] is True


# --- patch -----------------------------------------------------------------------------

def test_patch_rating_positive(client):
    tid = client.post("/things/", json={"url": "http://example/patch1", "bucket": "b"}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"human_rating": 2.0})
    assert r.status_code == 200
    assert r.json()["human_rating"] == 2.0
    assert r.json()["effective_rating"] == 2.0  # patch response computes ratings like a read


def test_patch_rating_numeric(client):
    tid = client.post("/things/", json={"url": "http://example/patch2", "bucket": "b"}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"human_rating": -2.0})
    assert r.json()["human_rating"] == -2.0


def test_patch_permafail_ack(client):
    tid = client.post("/things/", json={"url": "http://example/patch3", "bucket": "b"}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"try_on": None})
    assert r.status_code == 200
    assert r.json()["try_on"] is None


def test_patch_404(client):
    r = client.patch(f"/things/{uuid.uuid4()}", json={"human_rating": 2.0})
    assert r.status_code == 404


def test_patch_soft_hints(client):
    # V3 PATCH-schedule parity: edit cookies/lpm_lib hints after creation ([A11]).
    tid = client.post("/things/", json={"url": "http://example/patch-hints",
                                        "bucket": "b", "rating": 1.0}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"cookies": True, "lpm_lib": "x"})
    assert r.status_code == 200
    assert r.json()["attrs"] == {"cookies": True, "lpm_lib": "x"}
    # Flipping one hint preserves the other and leaves the rating untouched.
    r = client.patch(f"/things/{tid}", json={"cookies": False})
    assert r.json()["attrs"] == {"cookies": False, "lpm_lib": "x"}
    assert r.json()["human_rating"] == 1.0


def test_patch_container_classifies_then_affirms(client):
    # NULL->value classifies an unknown; re-asserting the same value is a no-op (200).
    tid = client.post("/things/", json={"url": "http://example/pc", "bucket": "b"}).json()["id"]
    assert client.get(f"/things/{tid}").json()["container"] is None
    r = client.patch(f"/things/{tid}", json={"container": True})
    assert r.status_code == 200 and r.json()["container"] is True
    r = client.patch(f"/things/{tid}", json={"container": True})  # affirm same value
    assert r.status_code == 200 and r.json()["container"] is True


def test_patch_container_switch_conflict(client):
    # Switching a set value (True<->False) is a 409; the stored value is untouched.
    tid = client.post("/things/", json={"url": "http://example/pcsw", "container": False,
                                        "bucket": "b"}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"container": True})
    assert r.status_code == 409
    assert client.get(f"/things/{tid}").json()["container"] is False


# --- patch: raise-to-eligible try_on side-effect (Task 2.1, §2.5) ----------------------

def test_patch_raise_resurrects_permafail(client):
    tid = _seed_thing(type="playlist", url="http://e/raise-perma",
                      human_rating=-1.0, try_on=None)  # D, permafail-acked
    r = client.patch(f"/things/{tid}", json={"human_rating": 1.0})
    assert r.status_code == 200
    assert r.json()["try_on"] == _TODAY.isoformat()


def test_patch_raise_pulls_future_forward(client):
    tid = _seed_thing(type="playlist", url="http://e/raise-future",
                      human_rating=0.0, try_on=_FUTURE)  # C, scheduled ahead
    r = client.patch(f"/things/{tid}", json={"human_rating": 2.0})
    assert r.json()["try_on"] == _TODAY.isoformat()


def test_patch_raise_skips_acquired(client):
    tid = _seed_thing(type="video", url="http://e/raise-acq", human_rating=1.0,
                      try_on=None, best_oi=uuid.uuid4())  # already acquired
    r = client.patch(f"/things/{tid}", json={"human_rating": 2.0})
    assert r.json()["try_on"] is None   # best_oi guard: never disturbed


def test_patch_downgrade_does_not_pull_forward(client):
    tid = _seed_thing(type="playlist", url="http://e/downgrade",
                      human_rating=2.0, try_on=_FUTURE)  # A, scheduled ahead
    r = client.patch(f"/things/{tid}", json={"human_rating": 0.0})  # still eligible, but a drop
    assert r.json()["try_on"] == _FUTURE.isoformat()


def test_patch_raise_d_to_c_no_meta_opens_meta_job(client):
    # D video with no metadata (last_success_dt NULL) → raise to C → eligible for meta job
    tid = _seed_thing(type="video", url="http://e/d-to-c-no-meta",
                      human_rating=-1.0, try_on=None)
    r = client.patch(f"/things/{tid}", json={"human_rating": 0.0})
    assert r.json()["try_on"] == _TODAY.isoformat()


def test_patch_raise_d_to_c_with_meta_still_sets_try_on(client):
    # D video that already has metadata → raise to C → try_on set, but dispatcher won't
    # claim it for meta (last_success_dt IS NOT NULL); harmless until it reaches B for download
    tid = _seed_thing(type="video", url="http://e/d-to-c-with-meta",
                      human_rating=-1.0, try_on=None,
                      last_success_dt=models.naive_utcnow())
    r = client.patch(f"/things/{tid}", json={"human_rating": 0.0})
    assert r.json()["try_on"] == _TODAY.isoformat()


def test_patch_human_rating_out_of_range(client):
    tid = _seed_thing(type="playlist", url="http://e/oor", human_rating=0.0)
    r = client.patch(f"/things/{tid}", json={"human_rating": 3.0})
    assert r.status_code == 422


# --- jobs: dispatch (Task 1.2) + Stage-1 ingest (Task 1.1) ------------------------------

def _seed_thing(**kw) -> str:
    """Insert a thing directly with explicit fields; returns its id (str).

    try_on is re-applied after insert so an explicit value (incl. None for permafail)
    overrides the column's server_default.
    """
    kw.setdefault("bucket", "testbucket")  # bucket is NOT NULL ([A10])
    if "type" in kw:  # ergonomic alias: map the old type kwarg to container (+kind hint)
        kind = kw.pop("type")
        kw["container"] = kind != "video"
        if kind == "channel":
            kw["attrs"] = {**(kw.get("attrs") or {}), "kind": "channel"}
    with _session() as s:
        t = models.Thing(**kw)
        s.add(t)
        s.commit()
        if "try_on" in kw:
            t.try_on = kw["try_on"]
            s.add(t)
            s.commit()
        s.refresh(t)
        return str(t.id)


def _claim(client, worker=None):
    """Claim the top job; returns the JobClaim json, or None on 204 (nothing due)."""
    r = client.post("/jobs/claim", json={"worker": worker} if worker else {})
    if r.status_code == 204:
        return None
    assert r.status_code == 200
    return r.json()


def _claimed_run(client, url, bucket="plbucket", cookies=None, lpm_lib=None):
    """Add a default-rated (C) playlist by url and claim it; returns (thing_id, run_id)."""
    body = {"url": url, "bucket": bucket}
    if cookies is not None:
        body["cookies"] = cookies
    if lpm_lib is not None:
        body["lpm_lib"] = lpm_lib
    tid = client.post("/things/", json=body).json()["id"]
    job = _claim(client)
    assert job and job["thing"]["id"] == tid and job["download"] is False  # container -> pull
    return tid, job["run_id"]


_TODAY = models.naive_utcnow().date()   # UTC, matching the app's date convention (not local)
_FUTURE = _TODAY + datetime.timedelta(days=5)


def test_claim_nothing_due(client):
    assert client.post("/jobs/claim", json={}).status_code == 204


def test_claim_rating_order(client):
    a = _seed_thing(type="playlist", url="http://e/a", human_rating=2.0, try_on=_TODAY)
    _seed_thing(type="playlist", url="http://e/b", human_rating=1.0, try_on=_TODAY)
    assert _claim(client)["thing"]["id"] == a       # A before B


def test_claim_playlist_before_video(client):
    _seed_thing(type="video", url="http://e/v", human_rating=2.0, try_on=_TODAY)     # A video
    p = _seed_thing(type="playlist", url="http://e/p", human_rating=0.0, try_on=_TODAY)  # C pl
    job = _claim(client)
    assert job["thing"]["id"] == p and job["download"] is False  # playlist wins regardless


def test_claim_video_when_no_playlist(client):
    v = _seed_thing(type="video", url="http://e/v2", human_rating=1.0, try_on=_TODAY)
    job = _claim(client)
    assert job["thing"]["id"] == v and job["download"] is True


def test_claim_skips_ineligible_videos(client):
    _seed_thing(type="video", url="http://e/vc", human_rating=0.0, try_on=_TODAY,
                last_success_dt=models.naive_utcnow())   # C + metadata-complete (no meta job)
    _seed_thing(type="video", url="http://e/vd", human_rating=-1.0, try_on=_TODAY)  # D: no meta
    _seed_thing(type="video", url="http://e/vacq", human_rating=2.0, try_on=_TODAY,
                best_oi=uuid.uuid4())                                               # acquired
    _seed_thing(type="video", url="http://e/vfut", human_rating=2.0, try_on=_FUTURE)  # not due
    assert _claim(client) is None


def test_claim_meta_for_underdescribed_c(client):
    # A C-band video the flat pull couldn't describe (last_success_dt NULL) -> metadata-only job
    # (download False; the worker enriches without acquiring media).
    v = _seed_thing(type="video", url="http://e/vmeta", try_on=_TODAY)  # unrated -> C
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["download"] is False


def test_claim_download_outranks_meta(client):
    # A B video (download) outranks a C video (metadata-only) in a single ordering.
    b = _seed_thing(type="video", url="http://e/vb", human_rating=1.0, try_on=_TODAY)
    _seed_thing(type="video", url="http://e/vcm", try_on=_TODAY)        # C -> metadata-only
    job = _claim(client)
    assert job["thing"]["id"] == b and job["download"] is True


def test_claim_unknown_container_never_downloads(client):
    # container=NULL (unknown) at a B+ rating must dispatch as a metadata-only pull, NOT a
    # download — a flat pull first classifies it; download is only dispatched on a later claim
    # once container is known False. Guards the `_wants_download` `is False` check (NULL is
    # not False), so an actual playlist never arms the download archive on its first run.
    v = _seed_thing(url="http://e/unknown-b", human_rating=2.0, try_on=_TODAY)  # container NULL
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["download"] is False


def test_claim_skips_ineligible_playlists(client):
    _seed_thing(type="playlist", url="http://e/done", human_rating=1.0, try_on=_TODAY,
                last_success_dt=models.naive_utcnow())                       # succeeded today
    _seed_thing(type="playlist", url="http://e/fut", human_rating=1.0, try_on=_FUTURE)
    _seed_thing(type="playlist", url="http://e/perma", human_rating=1.0, try_on=None)
    _seed_thing(type="playlist", url="http://e/d", human_rating=-1.0, try_on=_TODAY)  # D
    assert _claim(client) is None


def test_claim_creates_in_progress_run(client):
    p = _seed_thing(type="playlist", url="http://e/run", human_rating=1.0, try_on=_TODAY)
    job = _claim(client, worker="w1")
    assert job["thing"]["id"] == p and job["download"] is False and job["run_id"]
    runs = client.get(f"/things/{p}/runs").json()
    assert len(runs) == 1 and runs[0]["id"] == job["run_id"]
    assert runs[0]["success"] is None and runs[0]["worker"] == "w1"   # in-progress marker


# --- jobs: worker self-selection + concurrent-claim safety (#27, §4.5) ------------------
# (_seed_run / _seed_run_at below insert in-progress / time-offset runs directly.)

def test_claim_extractor_filter(client):
    # Worker self-selection: a claim pinned to an extractor only sees that extractor's jobs.
    yt = _seed_thing(type="playlist", url="http://e/yt", human_rating=1.0, try_on=_TODAY,
                     extractor_key="youtube")
    _seed_thing(type="playlist", url="http://e/vm", human_rating=2.0, try_on=_TODAY,
                extractor_key="vimeo")   # higher-rated, but a different extractor
    r = client.post("/jobs/claim", json={"extractor": "YouTube"})   # case-insensitive
    assert r.status_code == 200 and r.json()["thing"]["id"] == yt
    # An extractor with no eligible job yields 204 even though other jobs are due.
    assert client.post("/jobs/claim", json={"extractor": "dailymotion"}).status_code == 204


def test_claim_excludes_in_progress(client):
    # Concurrent-claim safety: a thing with a fresh in-progress run is not re-handed out, so two
    # workers partition the work instead of double-running the same thing (§4.5 risk #2).
    a = _seed_thing(type="playlist", url="http://e/ip-a", human_rating=2.0, try_on=_TODAY)
    b = _seed_thing(type="playlist", url="http://e/ip-b", human_rating=1.0, try_on=_TODAY)
    assert _claim(client)["thing"]["id"] == a   # claim 1 opens an in-progress run on A
    assert _claim(client)["thing"]["id"] == b   # claim 2 skips A, gets B
    assert _claim(client) is None               # both in-flight -> nothing left


def test_claim_lease_recovers_stale_run(client):
    # A hard-crashed worker leaves a zombie in-progress run; once it's older than the lease the
    # thing becomes claimable again (it would otherwise be blocked forever).
    t = _seed_thing(type="playlist", url="http://e/stale", human_rating=1.0, try_on=_TODAY)
    _seed_run_at(t, None, -(int(api.CLAIM_LEASE.total_seconds()) + 60))  # stale in-progress run
    assert _claim(client)["thing"]["id"] == t


def test_claim_fresh_in_progress_blocks(client):
    # The lease's counterpart: a recent in-progress run (within the lease) hides the thing.
    t = _seed_thing(type="playlist", url="http://e/fresh", human_rating=1.0, try_on=_TODAY)
    _seed_run(t)   # fresh in-progress run
    assert _claim(client) is None


# --- machine ratings: compute-on-read (Task 2.2 / §2.4) --------------------------------

def _seed_rel(parent: str, child: str, channel: bool = False) -> None:
    """Insert one parent->child rel edge directly (the Phase-1 ingest can't be poked piecemeal)."""
    with _session() as s:
        s.add(models.Rel(parent=uuid.UUID(parent), child=uuid.UUID(child), channel=channel))
        s.commit()


def test_machine_rating_propagates_to_dispatch(client):
    # An unrated video under a B-rated playlist assesses as B -> claimed for download (§2.4).
    pl = _seed_thing(type="playlist", url="http://e/mr-pl", human_rating=1.0, try_on=None)
    v = _seed_thing(type="video", url="http://e/mr-v", try_on=_TODAY)   # unrated
    _seed_rel(pl, v)
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["download"] is True


def test_unrated_video_no_parent_is_meta(client):
    # No rated relative -> effective C -> metadata-only, never a download.
    v = _seed_thing(type="video", url="http://e/mr-orphan", try_on=_TODAY)
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["download"] is False


def test_video_machine_rating_max_across_parents(client):
    # MAX over all parents, including the channel edge: C playlist + A channel -> A.
    cpl = _seed_thing(type="playlist", url="http://e/max-c", human_rating=0.0)
    achan = _seed_thing(type="channel", url="http://e/max-a", human_rating=2.0)
    v = _seed_thing(type="video", url="http://e/max-v")
    _seed_rel(cpl, v, channel=False)
    _seed_rel(achan, v, channel=True)
    got = client.get(f"/things/{v}").json()
    assert got["machine_rating"] == 2.0 and got["effective_rating"] == 2.0


def test_container_inherits_parent_over_children(client):
    # A playlist owned by an A channel inherits A even though its own child averages lower.
    achan = _seed_thing(type="channel", url="http://e/ci-chan", human_rating=2.0)
    pl = _seed_thing(type="playlist", url="http://e/ci-pl")                   # unrated
    child = _seed_thing(type="video", url="http://e/ci-child", human_rating=0.0)  # C
    _seed_rel(achan, pl, channel=True)
    _seed_rel(pl, child)
    got = client.get(f"/things/{pl}").json()
    assert got["machine_rating"] == 2.0 and got["effective_rating"] == 2.0


def test_container_child_avg_fallback(client):
    # No human-rated parent -> AVG of human-rated children (A + C -> 1.0); unrated ignored.
    pl = _seed_thing(type="playlist", url="http://e/avg-pl")
    _seed_rel(pl, _seed_thing(type="video", url="http://e/avg-a", human_rating=2.0))
    _seed_rel(pl, _seed_thing(type="video", url="http://e/avg-c", human_rating=0.0))
    _seed_rel(pl, _seed_thing(type="video", url="http://e/avg-none"))  # unrated -> ignored
    got = client.get(f"/things/{pl}").json()
    assert got["machine_rating"] == 1.0 and got["effective_rating"] == 1.0


def test_human_rating_hides_machine(client):
    # Human rating is authoritative: machine reported NULL, effective = human (§2.4).
    t = _seed_thing(type="video", url="http://e/hr", human_rating=2.0)
    got = client.get(f"/things/{t}").json()
    assert got["machine_rating"] is None and got["effective_rating"] == 2.0


def test_machine_rating_null_when_no_relatives(client):
    t = _seed_thing(type="video", url="http://e/no-rel")
    got = client.get(f"/things/{t}").json()
    assert got["machine_rating"] is None and got["effective_rating"] is None


def test_needs_rating_orders_container_first_then_neutral(client):
    # Needs-rating order: containers/unknowns before videos, then most-neutral machine
    # rating first (NULL machine rating sorts as neutral 0.0). All seeds are unrated so
    # they show in needs_rating; the rated parents are excluded by the human_rating filter.
    achan = _seed_thing(type="channel", url="http://e/nr-achan", human_rating=2.0)
    apl = _seed_thing(type="playlist", url="http://e/nr-apl", human_rating=2.0)
    cpl = _seed_thing(type="playlist", url="http://e/nr-cpl", human_rating=0.0)
    c_strong = _seed_thing(type="playlist", url="http://e/nr-c-strong")  # machine 2.0 via A channel
    _seed_rel(achan, c_strong, channel=True)
    c_null = _seed_thing(type="playlist", url="http://e/nr-c-null")      # no relatives -> machine NULL
    v_neutral = _seed_thing(type="video", url="http://e/nr-v-neutral")   # machine 0.0 via C playlist
    _seed_rel(cpl, v_neutral)
    v_strong = _seed_thing(type="video", url="http://e/nr-v-strong")     # machine 2.0 via A playlist
    _seed_rel(apl, v_strong)
    ids = [t["id"] for t in client.get("/things/", params={"needs_rating": True}).json()]
    assert ids == [c_null, c_strong, v_neutral, v_strong]


def test_related_things_carry_computed_ratings(client):
    # A neighbor's machine/effective rating is computed too (the subquery follows the neighbor).
    bpl = _seed_thing(type="playlist", url="http://e/rel-pl", human_rating=1.0)
    v = _seed_thing(type="video", url="http://e/rel-v")
    _seed_rel(bpl, v)
    rel = client.get(f"/things/{v}/related").json()
    assert len(rel) == 1 and rel[0]["thing"]["id"] == bpl
    assert rel[0]["thing"]["effective_rating"] == 1.0   # the parent's own human rating


def test_min_rating_filter_uses_effective(client):
    # human-A and machine-derived-B clear min_rating=B; a C video does not (§2.4 band floor).
    a = _seed_thing(type="video", url="http://e/min-a", human_rating=2.0)
    bpl = _seed_thing(type="playlist", url="http://e/min-bpl", human_rating=1.0)
    bv = _seed_thing(type="video", url="http://e/min-bv")               # machine B via parent
    _seed_rel(bpl, bv)
    c = _seed_thing(type="video", url="http://e/min-c", human_rating=0.0)  # C, excluded
    ids = {t["id"] for t in client.get("/things/", params={"min_rating": 1.0}).json()}
    assert a in ids and bv in ids and c not in ids


def test_min_rating_non_numeric(client):
    # min_rating is numeric now; a non-numeric value is a FastAPI query-validation error
    assert client.get("/things/", params={"min_rating": "Z"}).status_code == 422


def _pl_payload(n=3, url="http://example/pl/ingest", native="plingest",
                per_video_uploader=False, info_json=False) -> dict:
    """A JSON-ready thin PlaylistFull body for the ingest endpoint (what the worker posts).

    `extractor_key` is already normalized (lowercase) the way run_bknd.extract_pull emits
    it. By default every entry shares the playlist's uploader (up1). With
    `per_video_uploader`, each entry gets its own uploader (vup{i}) so the channel
    fan-out (`channel_video`, 1.3c) can be exercised with distinct uploaders. With
    `info_json`, each entry carries a raw-yt-dlp-style info dict -> attrs.info_json hint.
    """
    def vid_channel(i):
        if per_video_uploader:
            return models.UlChan(native_id=f"vup{i}", title=f"V Up {i}",
                                   url=f"http://example/vup{i}")
        return models.UlChan(native_id="up1", title="Up One",
                               url="http://example/up1")
    def vid_info(i):
        if not info_json:
            return None
        return {"id": f"vid{i}", "webpage_url": f"http://example/v/{i}",
                "formats": [{"format_id": "best", "url": f"http://cdn/{i}.mp4"}]}
    pl = models.PullThing(
        url=url, native_id=native, title="Ingest PL",
        modified=datetime.datetime(2026, 1, 31), playlist_count=n,
        extractor_key="youtube",
        channel=models.UlChan(native_id="up1", title="Up One",
                                url="http://example/up1"),
        entries=[models.PullThing(
            native_id=f"vid{i}", title=f"Video {i}", url=f"http://example/v/{i}",
            thumbnail_url=f"http://example/v/{i}/t.jpg",
            modified=datetime.datetime(2026, 1, i + 1),
            extractor_key="youtube",
            channel=vid_channel(i), info_json=vid_info(i),
        ) for i in range(n)],
    )
    return pl.model_dump(mode="json")


def _chan_payload(url="http://example/chan/ingest", native="chanX",
                  n_videos=2, n_playlists=2) -> dict:
    """A channel pull: the container IS its own uploader, with direct videos + sub-playlists.

    Each direct video's uploader matches the container's identity (the channel uploaded it),
    so fan-out emits a single uploader (`channel=True`) edge, no membership edge. Sub-playlists
    are `container=True` members in the same `entries` list, pulled on their own later.
    """
    chan = models.UlChan(native_id=native, title="The Channel", url=url)
    pl = models.PullThing(
        url=url, native_id=native, title="The Channel", extractor_key="youtube",
        playlist_count=n_videos + n_playlists,          # members = videos + sub-containers
        channel=chan,                                   # the channel is its own uploader
        entries=[models.PullThing(
            native_id=f"cv{i}", title=f"CVid {i}", url=f"http://example/cv/{i}",
            extractor_key="youtube", channel=chan,      # uploaded BY this channel
        ) for i in range(n_videos)] + [models.PullThing(
            native_id=f"{native}pl{j}", title=f"Sub PL {j}", container=True,
            url=f"http://example/chan/{native}/pl{j}",
            extractor_key="youtube", channel=chan,
        ) for j in range(n_playlists)],
    )
    return pl.model_dump(mode="json")


def _seed_run(thing_id: str) -> str:
    """Insert an in-progress run for a thing directly; returns run_id (str).

    Used where claim can't mint a second run (e.g. re-ingesting a playlist that already
    succeeded today, which the dispatch predicate would now skip).
    """
    with _session() as s:
        run = models.Run(thing_id=uuid.UUID(thing_id), success=None,
                         starttime=models.naive_utcnow())
        s.add(run)
        s.commit()
        s.refresh(run)
        return str(run.id)


def test_ingest_fans_out(client):
    url = "http://example/pl/fan"
    tid, rid = _claimed_run(client, url)   # url-only stub, claimed -> in-progress run
    r = client.post(f"/jobs/{rid}/result",
                    json={"playlist": _pl_payload(3, url=url, native="plfan"),
                          "data_json": {"raw": 1}})
    assert r.status_code == 200
    run = r.json()
    assert run["success"] is True and run["endtime"]
    assert run["playlist_count"] == 3 and run["entries_hash"]
    assert run["data_json"] == {"raw": 1}

    # #147: the url-only playlist thing is backfilled from the pull
    pl = client.get(f"/things/{tid}").json()
    assert pl["native_id"] == "plfan"
    assert pl["extractor_key"] == "youtube"   # normalized by the worker (extract_pull)
    assert pl["title"] == "Ingest PL"
    assert pl["last_success_dt"]

    related = client.get(f"/things/{tid}/related").json()
    kids = [e for e in related if e["direction"] == "child"]
    parents = [e for e in related if e["direction"] == "parent"]
    assert len(kids) == 3
    assert all(e["channel"] is False for e in kids)        # plain membership edges
    assert all(e["thing"]["container"] is False for e in kids)  # leaf videos
    assert len(parents) == 1
    assert parents[0]["channel"] is True                   # the uploader (channel) edge
    assert parents[0]["thing"]["container"] is True
    assert parents[0]["thing"]["attrs"]["kind"] == "channel"

    # video stubs carry denormalized fields and are eligible for Stage-2 (try_on=today)
    vids = client.get("/things/", params={"container": False}).json()
    assert len(vids) == 3
    assert all(v["title"] for v in vids)
    assert all(v["try_on"] == models.naive_utcnow().date().isoformat() for v in vids)

    # 1.3a: stubs inherit the dispatched playlist's bucket (immutable)
    assert all(v["bucket"] == "plbucket" for v in vids)
    chans = client.get("/things/", params={"kind": "channel"}).json()
    assert chans and all(c["bucket"] == "plbucket" for c in chans)


def test_ingest_duplicate_entries(client):
    # A playlist that lists the same video twice: both stubs remap to one thing id, which used
    # to emit duplicate (parent, child) rel rows in one ON CONFLICT batch -> CardinalityViolation
    # (HTTP 500). The ingest must dedupe, succeed, and leave a single edge per (parent, child).
    url = "http://example/pl/dup"
    tid, rid = _claimed_run(client, url)
    up = models.UlChan(native_id="dupup", title="Dup Up", url="http://example/dupup")
    dup_vid = dict(native_id="dupvid", title="Dup Video", url="http://example/v/dup",
                   extractor_key="youtube", channel=up)
    payload = models.PullThing(
        url=url, native_id="pldup", title="Dup PL", extractor_key="youtube",
        playlist_count=2, channel=up,
        entries=[models.PullThing(**dup_vid), models.PullThing(**dup_vid)],
    ).model_dump(mode="json")

    r = client.post(f"/jobs/{rid}/result", json={"playlist": payload})
    assert r.status_code == 200       # pre-fix this was a 500 CardinalityViolation
    assert r.json()["success"] is True

    # The duplicate collapses to one video thing, reachable by exactly one membership edge.
    related = client.get(f"/things/{tid}/related").json()
    kids = [e for e in related if e["direction"] == "child"]
    assert len(kids) == 1 and kids[0]["channel"] is False
    vids = client.get("/things/", params={"container": False}).json()
    assert len(vids) == 1

    # The video's uploader (channel=True) edge is also deduped to a single edge.
    vid_related = client.get(f"/things/{vids[0]['id']}/related").json()
    chan_parents = [e for e in vid_related if e["direction"] == "parent" and e["channel"]]
    assert len(chan_parents) == 1


def _url_clash_pull(client, clash_url):
    """Run a pull whose single member carries both the native key of row A and the url of row B,
    so the member matches A by native key but its url belongs to B. Returns (tid, response)."""
    url = "http://example/pl/clash"
    tid, rid = _claimed_run(client, url)
    up = models.UlChan(native_id="clashup", title="Clash Up", url="http://example/clashup")
    payload = models.PullThing(
        url=url, native_id="plclash", title="Clash PL", extractor_key="youtube",
        playlist_count=1, channel=up,
        entries=[models.PullThing(
            native_id="clashvid", title="Clash Video", url=clash_url,
            extractor_key="youtube", channel=up)],
    ).model_dump(mode="json")
    return tid, client.post(f"/jobs/{rid}/result", json={"playlist": payload})


def test_ingest_url_clash_merges(client):
    # Two rows already describe one video: B holds the url (no native key), A was created
    # native-key-first with url still NULL. A pull carries BOTH keys, so the member matches A by
    # native key while its url belongs to B -> pre-fix thing_url UniqueViolation (HTTP 500).
    # Convergence: B is merged into the survivor A (rel/run FKs re-pointed, state carried), then
    # A takes the url and B is gone.
    clash_url = "http://example/v/clash"
    # try_on=None on the seeded rows keeps them out of dispatch so _claimed_run claims the pull.
    a_id = _seed_thing(type="video", extractor_key="youtube",       # native key, url NULL
                       native_id="clashvid", try_on=None)
    b_id = _seed_thing(type="video", url=clash_url, human_rating=2.0,  # url + rating, no native key
                       try_on=None)
    # B carries an edge + a run that must survive the merge by following B onto A.
    par_id = _seed_thing(type="playlist", url="http://example/clashpar", try_on=None)
    with _session() as s:
        s.add(models.Rel(parent=uuid.UUID(par_id), child=uuid.UUID(b_id), channel=False))
        s.add(models.Run(thing_id=uuid.UUID(b_id), success=True,
                         starttime=models.naive_utcnow(), endtime=models.naive_utcnow()))
        s.commit()

    tid, r = _url_clash_pull(client, clash_url)
    assert r.status_code == 200            # pre-fix: 500 thing_url UniqueViolation
    assert r.json()["success"] is True

    # Survivor A now holds the url (merged from B) and inherited B's rating; B is deleted.
    a = client.get(f"/things/{a_id}").json()
    assert a["url"] == clash_url
    assert a["native_id"] == "clashvid"
    assert a["human_rating"] == 2.0        # carried from the loser (A had none)
    assert a["title"] == "Clash Video"
    assert client.get(f"/things/{b_id}").status_code == 404   # loser converged away

    # B's edge + run followed it onto A.
    a_related = client.get(f"/things/{a_id}/related").json()
    assert any(e["direction"] == "parent" and e["thing"]["id"] == par_id for e in a_related)
    assert client.get(f"/things/{a_id}/runs").json()          # A has B's run now (plus none of its own)


def test_ingest_native_clash_merges(client):
    # The native_id clash branch: the *container* is claimed by URL (native_id NULL), and its pull
    # reveals a native_id that a stray row already holds. `_apply_backfill(container, ...)` would
    # backfill that native_id and hit thing_native -> pre-fix UniqueViolation. The stray row is
    # merged into the container (the survivor), which then takes the native key.
    url = "http://example/pl/nclash"
    stray = _seed_thing(type="playlist", extractor_key="youtube",   # native key, url NULL
                        native_id="plnclash", try_on=None)          # not due -> claim the pull
    tid, rid = _claimed_run(client, url)                            # container matched by URL
    r = client.post(f"/jobs/{rid}/result",
                    json={"playlist": _pl_payload(2, url=url, native="plnclash")})
    assert r.status_code == 200            # pre-fix: 500 thing_native UniqueViolation
    assert r.json()["success"] is True

    container = client.get(f"/things/{tid}").json()
    assert container["native_id"] == "plnclash"   # gained the stray row's native key
    assert client.get(f"/things/{stray}").status_code == 404   # stray converged away


def test_ingest_empty_playlist(client):
    # An empty playlist (0 entries) must be classified container=True so it is re-pulled.
    url = "http://example/pl/empty"
    tid, rid = _claimed_run(client, url)
    r = client.post(f"/jobs/{rid}/result",
                    json={"playlist": _pl_payload(0, url=url, native="plempty")})
    assert r.status_code == 200
    run = r.json()
    assert run["success"] is True and run["playlist_count"] == 0

    pl = client.get(f"/things/{tid}").json()
    assert pl["container"] is True
    assert pl["last_success_dt"]

    vids = client.get("/things/", params={"container": False}).json()
    assert vids == []


def test_ingest_single_entry_playlist(client):
    # A 1-entry playlist must be classified container=True (not mistaken for a single video).
    url = "http://example/pl/single"
    tid, rid = _claimed_run(client, url)
    r = client.post(f"/jobs/{rid}/result",
                    json={"playlist": _pl_payload(1, url=url, native="plsingle")})
    assert r.status_code == 200
    run = r.json()
    assert run["success"] is True and run["playlist_count"] == 1

    pl = client.get(f"/things/{tid}").json()
    assert pl["container"] is True
    assert pl["last_success_dt"]

    vids = client.get("/things/", params={"container": False}).json()
    assert len(vids) == 1
    assert vids[0]["container"] is False


def test_ingest_last_success_from_required_fields(client):
    # API decides "enough to rate" from extracted fields: a stub with all 5 identity fields
    # (channel, url, title, extractor_key, native_id) is metadata-complete (last_success_dt
    # set); a stub missing any field stays NULL and is claimable as a `meta` job.
    url = "http://example/pl/rate"
    tid, rid = _claimed_run(client, url)
    pl = models.PullThing(
        # no playlist channel here: a fanned-out uploader channel is itself a claimable
        # container now, which would outrank the incomplete video below — out of scope for
        # this required-fields->last_success test (channel fan-out is covered by its own tests).
        url=url, native_id="plrate", title="Rate PL", extractor_key="youtube",
        playlist_count=2, channel=models.UlChan(),
        entries=[
            models.PullThing(native_id="complete", title="Has Title",
                           url="http://example/v/ht", extractor_key="youtube",
                           channel=models.UlChan(url="http://example/chan/ht")),
            models.PullThing(native_id="notitle", url="http://example/v/nt",
                           extractor_key="youtube"),
        ])
    assert client.post(f"/jobs/{rid}/result",
                       json={"playlist": pl.model_dump(mode="json")}).status_code == 200
    vids = {v["native_id"]: v for v in client.get("/things/", params={"container": False}).json()}
    assert vids["complete"]["last_success_dt"]            # all 5 fields -> metadata-complete
    assert vids["notitle"]["last_success_dt"] is None     # missing any field -> needs a meta job


def test_meta_result_fans_out_channel(client):
    # A full meta extract reveals the uploader a flat pull omitted -> channel thing + rel.
    v, rid = _claimed_meta(client, url="http://e/mc")
    client.post(f"/jobs/{rid}/result",
                json={"success": True,
                      "video": {"native_id": "mcv", "title": "MC", "extractor_key": "youtube",
                                "channel": {"url": "http://e/chan9", "title": "Chan 9"},
                                "info_json": {"id": "mcv"}}})
    related = client.get(f"/things/{v}/related").json()
    chan = [e for e in related if e["channel"]]
    assert len(chan) == 1 and chan[0]["thing"]["container"] is True
    assert chan[0]["thing"]["attrs"]["kind"] == "channel"
    assert chan[0]["thing"]["url"] == "http://e/chan9"


def test_ingest_propagates_hints(client):
    # 1.3b: a playlist's cookies/lpm_lib hints propagate onto its video stubs (attrs);
    # channels do not carry the hints (bucket only).
    url = "http://example/pl/hintprop"
    tid, rid = _claimed_run(client, url, cookies=True, lpm_lib="lib7")
    payload = _pl_payload(2, url=url, native="plhint")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    vids = client.get("/things/", params={"container": False}).json()
    assert vids and all(v["attrs"] == {"cookies": True, "lpm_lib": "lib7"} for v in vids)
    chans = client.get("/things/", params={"kind": "channel"}).json()
    # channels carry kind + optional channel_id hint but NOT propagated cookies/lpm_lib
    assert chans and all(c["attrs"].get("kind") == "channel" for c in chans)
    assert all("cookies" not in c["attrs"] and "lpm_lib" not in c["attrs"] for c in chans)


def test_ingest_stores_info_json_hint(client):
    # Producer side: each entry's raw info dict lands as attrs.info_json on the video stub.
    url = "http://example/pl/infojson"
    tid, rid = _claimed_run(client, url)
    payload = _pl_payload(2, url=url, native="plij", info_json=True)
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    vids = client.get("/things/", params={"container": False}).json()
    assert vids
    for v in vids:
        info = v["attrs"]["info_json"]
        assert info["id"] == v["native_id"]
        assert "formats" in info
    chans = client.get("/things/", params={"kind": "channel"}).json()
    assert chans and all(not (c["attrs"] or {}).get("info_json") for c in chans)


def test_ingest_refreshes_info_json_until_acquired(client):
    # Re-pull updates info_json while best_oi is NULL; an acquired video is left untouched.
    url = "http://example/pl/ijrefresh"
    tid, rid = _claimed_run(client, url)
    assert client.post(f"/jobs/{rid}/result",
                       json={"playlist": _pl_payload(2, url=url, native="plijr",
                                                     info_json=True)}).status_code == 200
    by_url = {v["url"]: v for v in client.get("/things/", params={"container": False}).json()}
    # mark vid0 acquired (best_oi set), leave vid1 pending
    with _session() as s:
        acquired = s.get(models.Thing, uuid.UUID(by_url["http://example/v/0"]["id"]))
        acquired.best_oi = uuid.uuid4()
        s.add(acquired)
        s.commit()

    # second pull with a *changed* info dict (extra key) — same day, so seed the run
    rid2 = _seed_run(tid)
    payload2 = _pl_payload(2, url=url, native="plijr", info_json=True)
    for entry in payload2["entries"]:
        entry["info_json"]["refreshed"] = True
    assert client.post(f"/jobs/{rid2}/result", json={"playlist": payload2}).status_code == 200

    by_url = {v["url"]: v for v in client.get("/things/", params={"container": False}).json()}
    assert by_url["http://example/v/1"]["attrs"]["info_json"].get("refreshed") is True   # updated
    assert "refreshed" not in by_url["http://example/v/0"]["attrs"]["info_json"]          # frozen


def test_repull_does_not_clobber_meta_hint_with_flat(client):
    # A direct meta extract stores a full info_json hint; a later playlist re-pull carrying the
    # thin flat entry (_type=url) must not overwrite that richer hint.
    url = "http://example/pl/noclobber"
    tid, rid = _claimed_run(client, url)

    def flat_payload():
        # one under-described member (empty channel) so it stays meta-claimable (last_success
        # NULL); empty playlist channel so no rival channel container is claimable.
        return models.PullThing(
            url=url, native_id="ncpl", title="PL", extractor_key="youtube", playlist_count=1,
            channel=models.UlChan(),
            entries=[models.PullThing(
                native_id="ncv", url="http://example/v/nc", title="NC",
                extractor_key="youtube", channel=models.UlChan(),
                info_json={"_type": "url", "id": "ncv"})],
        ).model_dump(mode="json")

    assert client.post(f"/jobs/{rid}/result", json={"playlist": flat_payload()}).status_code == 200
    vid_id = client.get("/things/", params={"container": False}).json()[0]["id"]

    # meta job: a full extract stores a richer hint (has formats, no _type=url)
    job = _claim(client)
    assert job["thing"]["id"] == vid_id and job["download"] is False
    assert client.post(f"/jobs/{job['run_id']}/result", json={"success": True, "video": {
        "native_id": "ncv", "title": "NC Full", "extractor_key": "youtube",
        "channel": {"url": "http://example/chan/nc"},
        "info_json": {"id": "ncv", "formats": [{"format_id": "best"}]}}}).status_code == 200
    assert client.get(f"/things/{vid_id}").json()["attrs"]["info_json"].get("formats")

    # re-pull the playlist with the thin flat entry again (same day -> seed the run)
    rid2 = _seed_run(tid)
    assert client.post(f"/jobs/{rid2}/result", json={"playlist": flat_payload()}).status_code == 200

    hint = client.get(f"/things/{vid_id}").json()["attrs"]["info_json"]
    assert hint.get("formats")          # the richer meta hint survived
    assert hint.get("_type") != "url"   # not clobbered by the flat re-pull entry


def test_subcontainer_info_hint_cleared_on_own_pull(client):
    # A sub-container keeps its info_json hint (it feeds the sub-playlist's own pull); once the
    # sub-playlist is pulled itself (its result is a playlist), that now-moot hint is cleared.
    url = "http://example/pl/subhint"
    tid, rid = _claimed_run(client, url)
    sub_url = "http://example/pl/subhint/sub"
    payload = models.PullThing(
        url=url, native_id="subhintpl", title="Parent", extractor_key="youtube",
        playlist_count=1, channel=models.UlChan(),
        entries=[models.PullThing(
            native_id="subh1", url=sub_url, title="Sub PL", container=True,
            extractor_key="youtube", channel=models.UlChan(),
            info_json={"id": "subh1", "webpage_url": sub_url})],
    ).model_dump(mode="json")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200

    # the sub-container stub carries the load-info hint for its own pull
    kids = [e["thing"] for e in client.get(f"/things/{tid}/related").json()
            if e["direction"] == "child"]
    sub = next(s for s in kids if s["container"] is True)
    assert sub["attrs"]["info_json"]["id"] == "subh1"

    # pull the sub-playlist itself -> its hint is now moot and cleared
    job = _claim(client)
    assert job["thing"]["id"] == sub["id"] and job["download"] is False
    sub_payload = _pl_payload(2, url=sub_url, native="subh1pl")
    assert client.post(f"/jobs/{job['run_id']}/result",
                       json={"playlist": sub_payload}).status_code == 200
    refreshed = client.get(f"/things/{sub['id']}").json()
    assert not (refreshed["attrs"] or {}).get("info_json")


def test_ingest_per_video_uploader_channels(client):
    # 1.3c: each distinct video uploader gets a type='channel' thing + channel_video edge;
    # the playlist keeps its own channel_playlist parent.
    url = "http://example/pl/chans"
    tid, rid = _claimed_run(client, url)
    payload = _pl_payload(3, url=url, native="plchans", per_video_uploader=True)
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200

    # 1 playlist uploader (up1) + 3 distinct video uploaders (vup0..2)
    chans = client.get("/things/", params={"kind": "channel"}).json()
    assert len(chans) == 4

    # the playlist's only parent edge is its uploader (channel=True)
    pl_parents = [e for e in client.get(f"/things/{tid}/related").json()
                  if e["direction"] == "parent"]
    assert len(pl_parents) == 1 and pl_parents[0]["channel"] is True

    # every video has exactly one uploader (channel=True) parent + its membership (False) parent
    for vid in client.get("/things/", params={"container": False}).json():
        parents = client.get(f"/things/{vid['id']}/related",
                             params={"direction": "parent"}).json()
        assert sorted(e["channel"] for e in parents) == [False, True]
        chan_edge = next(e for e in parents if e["channel"])
        assert chan_edge["thing"]["attrs"]["kind"] == "channel"


def test_ingest_shared_uploader_one_channel(client):
    # 1.3c: same-uploader videos (default payload: all up1) reuse a single channel node,
    # which carries both channel_playlist (to the pl) and channel_video (to each vid) edges.
    url = "http://example/pl/shared"
    tid, rid = _claimed_run(client, url)
    payload = _pl_payload(3, url=url, native="plshared")  # all share up1 = pl channel
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    chans = client.get("/things/", params={"kind": "channel"}).json()
    assert len(chans) == 1
    children = client.get(f"/things/{chans[0]['id']}/related",
                          params={"direction": "child"}).json()
    # one shared channel owns the playlist + each video, all via channel=True edges
    assert len(children) == 4 and all(e["channel"] is True for e in children)


def test_channel_pull_videos_and_subplaylists(client):
    # A channel pull (parent IS its own uploader) emits a single channel=True edge per direct
    # video (no membership edge), no self-edge, and a container=True stub per sub-playlist.
    url = "http://example/chan/c1"
    tid, rid = _claimed_run(client, url)        # added unknown, claimed as a pull
    payload = _chan_payload(url=url, native="chanc1", n_videos=2, n_playlists=2)
    run = client.post(f"/jobs/{rid}/result", json={"playlist": payload})
    assert run.status_code == 200
    assert run.json()["playlist_count"] == 4    # videos + sub-containers (channel-aware count)

    chan = client.get(f"/things/{tid}").json()
    assert chan["container"] is True
    assert chan["attrs"]["kind"] == "channel"               # acts as a channel -> tagged

    related = client.get(f"/things/{tid}/related").json()
    kids = [e for e in related if e["direction"] == "child"]
    assert len(kids) == 4 and all(e["channel"] is True for e in kids)   # uploader/owner edges
    assert not any(e["direction"] == "parent" for e in related)         # no self channel edge

    # each direct video has exactly one parent edge, channel=True (no channel=False membership)
    for v in client.get("/things/", params={"container": False}).json():
        parents = client.get(f"/things/{v['id']}/related", params={"direction": "parent"}).json()
        assert len(parents) == 1 and parents[0]["channel"] is True

    # sub-playlists are container stubs, claimable -> a follow-up claim pulls one (recursion)
    subs = [e["thing"] for e in kids if e["thing"]["container"] is True]
    assert len(subs) == 2 and all(s["try_on"] for s in subs)
    job = _claim(client)
    assert job["download"] is False and job["thing"]["id"] in {s["id"] for s in subs}


def test_subcontainer_edge_upgrades_on_own_pull(client):
    # A sub-container first seen with an unknown owner is a channel=False membership edge;
    # when the sub-container is pulled itself and reveals the listing parent as its owner,
    # the monotonic rel upsert raises that same (parent, child) edge False -> True.
    c_url = "http://example/chanupg"
    cid, rid = _claimed_run(client, c_url)
    sub_url = "http://example/chanupg/subpl"
    c_payload = models.PullThing(
        url=c_url, native_id="cupg", title="Chan", extractor_key="youtube",
        playlist_count=1,
        channel=models.UlChan(),                          # no top-level owner
        entries=[models.PullThing(native_id="subupg", url=sub_url, title="Sub",
                                extractor_key="youtube", container=True)],  # unknown owner
    ).model_dump(mode="json")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": c_payload}).status_code == 200

    # discovery edge: C -> S is channel=False membership
    kids = [e for e in client.get(f"/things/{cid}/related").json() if e["direction"] == "child"]
    sub = next(e for e in kids if e["thing"]["container"] is True)
    assert sub["channel"] is False
    sid = sub["thing"]["id"]

    # pull S itself; its own uploader resolves (by url) to C -> owner edge C -> S channel=True
    sjob = _claim(client)
    assert sjob["thing"]["id"] == sid and sjob["download"] is False
    s_payload = models.PullThing(
        url=sub_url, native_id="subupg", title="Sub", extractor_key="youtube",
        playlist_count=0,
        channel=models.UlChan(native_id="cupg", url=c_url),   # owned by C
        entries=[],
    ).model_dump(mode="json")
    assert client.post(f"/jobs/{sjob['run_id']}/result",
                       json={"playlist": s_payload}).status_code == 200

    # the same C -> S edge is upgraded to channel=True (monotonic OR-upsert)
    parents = client.get(f"/things/{sid}/related", params={"direction": "parent"}).json()
    c_edge = next(e for e in parents if e["thing"]["id"] == cid)
    assert c_edge["channel"] is True


def test_curated_subcontainer_membership_and_owner(client):
    # A curated container listing someone else's sub-playlist: channel=False membership edge
    # from the parent + the sub's real owner as a kind='channel' thing with channel=True.
    p_url = "http://example/curated"
    pid, rid = _claimed_run(client, p_url)
    sub_url = "http://example/x/subpl"
    payload = models.PullThing(
        url=p_url, native_id="curated", title="Curated", extractor_key="youtube",
        playlist_count=1,
        channel=models.UlChan(),
        entries=[models.PullThing(native_id="xsub", url=sub_url, title="X Sub",
                                extractor_key="youtube", container=True,
                                channel=models.UlChan(native_id="xowner", title="X",
                                                      url="http://example/xowner"))],
    ).model_dump(mode="json")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200

    kids = [e for e in client.get(f"/things/{pid}/related").json() if e["direction"] == "child"]
    sub = next(e for e in kids if e["thing"]["container"] is True)
    assert sub["channel"] is False                        # membership: parent doesn't own it
    sid = sub["thing"]["id"]

    # the sub's owner X exists as a channel thing with a channel=True edge to the sub
    parents = client.get(f"/things/{sid}/related", params={"direction": "parent"}).json()
    owner = next(e for e in parents if e["channel"] is True)
    assert owner["thing"]["url"] == "http://example/xowner"
    assert owner["thing"]["attrs"]["kind"] == "channel"


def test_inlined_subplaylist_ingested_as_completed_run(client):
    # yt-dlp sometimes hands back a sub-playlist already enumerated (inlined `entries`). Rather
    # than drop them and re-pull, the endpoint fans that sub-container out as part of the parent's
    # single run (one yt-dlp call -> one run): it is marked complete today, gets a parent-fed
    # safety-net try_on, and its members fan out — but it has NO run of its own. A normal flat
    # sub-playlist pointer (no entries) stays an unpulled stub pulled on its own schedule.
    p_url = "http://example/pl/nested"
    pid, rid = _claimed_run(client, p_url)
    inlined_url = "http://example/pl/nested/subA"
    flat_url = "http://example/pl/nested/subB"
    sub_owner = models.UlChan(native_id="subAown", url="http://example/subAown")
    payload = models.PullThing(
        url=p_url, native_id="nested", title="Nested PL", extractor_key="youtube",
        playlist_count=2, channel=models.UlChan(),
        entries=[
            # a sub-playlist yt-dlp returned already enumerated (carries its own members)
            models.PullThing(
                native_id="subA", url=inlined_url, title="Sub A", container=True,
                extractor_key="youtubetab", channel=sub_owner, playlist_count=2,
                info_json={"id": "subA", "_type": "playlist",
                           "entries": [{"id": "na1"}, {"id": "na2"}]},
                entries=[
                    models.PullThing(native_id="na1", url="http://example/v/na1", title="NA1",
                                     extractor_key="youtube", channel=sub_owner),
                    models.PullThing(native_id="na2", url="http://example/v/na2", title="NA2",
                                     extractor_key="youtube", channel=sub_owner),
                ]),
            # a normal flat sub-playlist pointer (no inlined entries)
            models.PullThing(native_id="subB", url=flat_url, title="Sub B",
                             container=True, extractor_key="youtubetab"),
        ],
    ).model_dump(mode="json")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200

    kids = [e["thing"] for e in client.get(f"/things/{pid}/related").json()
            if e["direction"] == "child"]
    subA = next(t for t in kids if t["native_id"] == "subA")
    subB = next(t for t in kids if t["native_id"] == "subB")

    # the inlined sub-playlist is complete today + a parent-fed safety-net try_on, but rides the
    # parent's single run (no run of its own); its date sits a margin past the parent's.
    assert subA["container"] is True
    assert subA["last_success_dt"] is not None
    assert datetime.date.fromisoformat(subA["last_success_dt"][:10]) == _TODAY
    parent_try_on = datetime.date.fromisoformat(client.get(f"/things/{pid}").json()["try_on"])
    assert datetime.date.fromisoformat(subA["try_on"]) == parent_try_on + api.SAFETY_MARGIN_DAYS
    assert client.get(f"/things/{subA['id']}/runs").json() == []   # no per-sub run (one call, one run)
    # and it shed its load-info hint like any pulled container
    assert (subA["attrs"] or {}).get("info_json") is None

    # its inlined member videos exist as things with edges from the sub-playlist
    sub_kids = [e["thing"] for e in client.get(f"/things/{subA['id']}/related").json()
                if e["direction"] == "child"]
    assert {t["native_id"] for t in sub_kids} >= {"na1", "na2"}

    # the flat pointer stays an unpulled stub: not complete, due today, no run
    assert subB["last_success_dt"] is None
    assert subB["try_on"] == _TODAY.isoformat()
    assert client.get(f"/things/{subB['id']}/runs").json() == []


def test_channel_tabs_one_run_distinct_things(client):
    # A YouTube channel pull arrives with its Videos/Shorts/Live tabs inlined; every tab carries
    # the SAME id (channel_id) but a distinct URL. The endpoint must (a) keep the tabs as four
    # distinct things (facet rule: tabs URL-keyed), (b) record exactly ONE run on the channel
    # covering the whole subtree, and (c) schedule the tabs parent-fed (try_on = channel + margin).
    url = "http://yt/@geerling/featured"
    cid, rid = _claimed_run(client, url)
    chan = models.UlChan(native_id="UC", title="Geerling", url="http://yt/@geerling")

    def tab(name, vids):
        return models.PullThing(
            native_id="UC", extractor_key="youtubetab", container=True,
            url=f"http://yt/@geerling/{name}", title=f"Geerling - {name}", channel=chan,
            playlist_count=len(vids),
            info_json={"id": "UC", "_type": "playlist", "entries": [{"id": v} for v in vids]},
            entries=[models.PullThing(native_id=v, url=f"http://yt/v/{v}", title=v.upper(),
                                      extractor_key="youtube", channel=chan) for v in vids])

    payload = models.PullThing(
        url=url, native_id="UC", extractor_key="youtubetab", title="Geerling", channel=chan,
        container=True, playlist_count=3,
        entries=[tab("videos", ["a", "b", "c"]), tab("shorts", ["d"]), tab("streams", ["e", "f"])],
    ).model_dump(mode="json")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200

    # exactly ONE run, on the channel; nothing duplicated onto another thing at the same instant
    chan_runs = client.get(f"/things/{cid}/runs").json()
    assert len(chan_runs) == 1 and chan_runs[0]["success"] is True

    # four distinct things: the channel (keeps id) + three URL-keyed tabs
    tabs = [e["thing"] for e in client.get(f"/things/{cid}/related").json()
            if e["direction"] == "child" and e["thing"]["container"] is True]
    assert len(tabs) == 3
    assert {t["url"] for t in tabs} == {f"http://yt/@geerling/{n}" for n in ("videos", "shorts", "streams")}
    assert all(t["native_id"] is None for t in tabs)               # tabs URL-keyed, not collapsed
    assert all((t["attrs"] or {}).get("channel_id") == "UC" for t in tabs)
    assert client.get(f"/things/{cid}").json()["native_id"] == "UC"  # channel keeps the id

    # tabs are parent-fed: complete today, no run of their own, try_on = channel + safety margin
    chan_try_on = datetime.date.fromisoformat(client.get(f"/things/{cid}").json()["try_on"])
    for t in tabs:
        assert t["last_success_dt"] is not None
        assert client.get(f"/things/{t['id']}/runs").json() == []
        assert datetime.date.fromisoformat(t["try_on"]) == chan_try_on + api.SAFETY_MARGIN_DAYS
        # tab -> channel is a channel=True edge; no self-edge anywhere
        parents = client.get(f"/things/{t['id']}/related", params={"direction": "parent"}).json()
        assert any(p["thing"]["id"] == cid and p["channel"] for p in parents)
        assert all(p["thing"]["id"] != t["id"] for p in parents)

    # grandchild videos from every tab exist as things, each linked to its tab
    videos = {v["native_id"]: v for v in client.get("/things/", params={"container": False}).json()}
    assert set(videos) == {"a", "b", "c", "d", "e", "f"}
    a_parents = client.get(f"/things/{videos['a']['id']}/related", params={"direction": "parent"}).json()
    videos_tab = next(t for t in tabs if t["url"].endswith("/videos"))
    assert any(p["thing"]["id"] == videos_tab["id"] for p in a_parents)


def test_unknown_url_discovered_as_video(client):
    # #153: an unknown URL (container=None) the pull resolves to a single video is sent as a
    # `video` body and classified as a leaf (container=False), then download/meta-eligible.
    tid, rid = _claimed_run(client, "http://e/unknown-vid")   # unknown, claimed as 'pull'
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True,
                          "video": {"native_id": "uv1", "title": "Surprise Video",
                                    "extractor_key": "youtube",
                                    "channel": {"url": "http://e/uchan"},
                                    "info_json": {"id": "uv1"}}})
    assert r.status_code == 200
    t = client.get(f"/things/{tid}").json()
    assert t["container"] is False                # classified as a leaf, not a container
    assert t["title"] == "Surprise Video" and t["last_success_dt"]
    assert t["best_oi"] is None                   # discovery is metadata-only


def test_unknown_url_discovered_as_container(client):
    # #153 counterpart: an unknown URL the pull resolves to a playlist is classified container.
    url = "http://e/unknown-pl"
    tid, rid = _claimed_run(client, url)
    assert client.post(f"/jobs/{rid}/result",
                       json={"playlist": _pl_payload(2, url=url, native="unkpl")}
                       ).status_code == 200
    assert client.get(f"/things/{tid}").json()["container"] is True


def test_ingest_idempotent(client):
    url = "http://example/pl/idem"
    tid, rid = _claimed_run(client, url)
    payload = _pl_payload(3, url=url, native="plidem")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    # second pull the same day: claim would skip it, so seed the run directly
    rid2 = _seed_run(tid)
    assert client.post(f"/jobs/{rid2}/result", json={"playlist": payload}).status_code == 200
    assert len(client.get("/things/", params={"container": False}).json()) == 3   # no dup things
    assert len(client.get("/things/", params={"kind": "channel"}).json()) == 1
    assert len(client.get(f"/things/{tid}/runs").json()) == 2                   # but two runs
    kids = [e for e in client.get(f"/things/{tid}/related").json()
            if e["direction"] == "child"]
    assert len(kids) == 3                                                       # no dup rels


def test_ingest_preserves_existing_bucket(client):
    # 1.3a: a thing added directly keeps its own bucket even when a later playlist pull
    # (carrying a different inherited bucket) re-discovers it — bucket is immutable.
    client.post("/things/", json={"url": "http://example/v/0", "container": False,
                                  "bucket": "vidbucket"})
    url = "http://example/pl/preserve"
    tid, rid = _claimed_run(client, url, bucket="plbucket")
    payload = _pl_payload(3, url=url, native="plpreserve")  # entries include .../v/0..2
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    by_url = {v["url"]: v for v in client.get("/things/", params={"container": False}).json()}
    assert by_url["http://example/v/0"]["bucket"] == "vidbucket"   # kept, not overwritten
    assert by_url["http://example/v/1"]["bucket"] == "plbucket"    # newly inherited


def test_ingest_failure_records_only(client):
    tid, rid = _claimed_run(client, "http://example/pl/fail")
    r = client.post(f"/jobs/{rid}/result", json={"success": False})
    assert r.status_code == 200 and r.json()["success"] is False
    pl = client.get(f"/things/{tid}").json()
    assert pl["last_failure_dt"] and pl["last_success_dt"] is None
    assert client.get("/things/", params={"container": False}).json() == []       # no fan-out


def test_ingest_success_requires_playlist(client):
    _, rid = _claimed_run(client, "http://example/pl/req")
    r = client.post(f"/jobs/{rid}/result", json={"success": True})
    assert r.status_code == 422


def test_container_gets_video_body_is_failure(client):
    # Seed with container=True already set so the guard fires (URL-only stubs are container=None)
    tid = _seed_thing(type="playlist", url="http://example/pl/mismatch1", try_on=_TODAY)
    job = _claim(client)
    assert job and job["thing"]["id"] == tid
    rid = job["run_id"]
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True, "video": {"native_id": "vid1", "extractor_key": "youtube"}})
    assert r.status_code == 200 and r.json()["success"] is False
    t = client.get(f"/things/{tid}").json()
    assert t["container"] is True           # not reclassified
    assert t["last_failure_dt"] is not None
    assert t["last_success_dt"] is None
    assert t["try_on"] is not None          # backoff applied


def test_video_gets_playlist_body_is_failure(client):
    vid, rid = _claimed_download(client, url="http://e/mismatch2")
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True,
                          "playlist": _pl_payload(url="http://e/mismatch2", native="mmpl")})
    assert r.status_code == 200 and r.json()["success"] is False
    t = client.get(f"/things/{vid}").json()
    assert t["container"] is False          # not reclassified
    assert t["last_failure_dt"] is not None
    assert t["best_oi"] is None
    assert t["try_on"] is not None          # backoff applied


def test_both_bodies_is_failure(client):
    # #164: a body carrying both a video and a playlist is contradictory — record a plain failure
    # and never mutate the classification (set once, never reset). The seeded container=False
    # stays False, and no relationships / member stubs are recorded. A best_oi (media already
    # uploaded) is preserved so the OI object isn't orphaned, but the thing is NOT marked acquired.
    oi = str(uuid.uuid4())
    vid, rid = _claimed_download(client, url="http://e/both")   # seeded container=False
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True, "best_oi": oi,
                          "video": {"native_id": "bvid", "extractor_key": "youtube"},
                          "playlist": _pl_payload(url="http://e/both", native="bpl")})
    assert r.status_code == 200 and r.json()["success"] is False
    t = client.get(f"/things/{vid}").json()
    assert t["container"] is False          # unchanged: classification is never reset
    assert t["best_oi"] == oi               # preserved (media uploaded) but not marked acquired
    assert t["last_failure_dt"] is not None
    assert t["try_on"] is not None          # backoff applied (not marked acquired)
    # no relationships fanned out and no member stubs created (the seeded thing is the only one)
    assert client.get(f"/things/{vid}/related").json() == []
    ids = [x["id"] for x in client.get("/things/", params={"container": False}).json()]
    assert ids == [vid]


def test_both_bodies_known_container_stays_classified(client):
    # #164 guard: a known-good container (container=True) that receives a both-shape result must
    # NOT have its classification reset — leave container=True and record a plain failure with
    # backoff. Only unknowns/leaves are reset to NULL on contradictory evidence.
    # Seed directly (type="playlist" → container=True) — POST /things/ starts as unknown.
    pl = _seed_thing(type="playlist", url="http://e/both-c", human_rating=0.0, try_on=_TODAY)
    job = _claim(client)
    assert job and job["thing"]["id"] == pl
    rid = job["run_id"]
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True,
                          "video": {"native_id": "bv1", "extractor_key": "youtube"},
                          "playlist": _pl_payload(url="http://e/both-c", native="bpl-c")})
    assert r.status_code == 200 and r.json()["success"] is False
    t = client.get(f"/things/{pl}").json()
    assert t["container"] is True              # NOT reset — known-good container preserved
    assert t["last_failure_dt"] is not None


def test_ingest_run_404(client):
    r = client.post(f"/jobs/{uuid.uuid4()}/result", json={"success": False})
    assert r.status_code == 404


# --- jobs: cookies suggestion + Stage-2 download result (Task 1.3) ----------------------

def test_claim_cookies_default_false(client):
    _seed_thing(type="playlist", url="http://e/nocook", human_rating=1.0, try_on=_TODAY)
    assert _claim(client)["cookies"] is False


def test_claim_cookies_hint(client):
    # attrs.cookies hint -> the dispatch suggests cookies (hint-only in 1.3)
    _seed_thing(type="playlist", url="http://e/cook", human_rating=1.0, try_on=_TODAY,
                attrs={"cookies": True})
    assert _claim(client)["cookies"] is True


def _claimed_download(client, **kw):
    """Seed a B-rated due video, claim it; returns (thing_id, run_id)."""
    kw.setdefault("human_rating", 1.0)
    kw.setdefault("try_on", _TODAY)
    v = _seed_thing(type="video", **kw)
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["download"] is True
    return v, job["run_id"]


def test_download_result_sets_best_oi(client):
    oi = str(uuid.uuid4())
    v, rid = _claimed_download(client, url="http://e/dl")  # no extractor/native yet
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True, "best_oi": oi,
                          "video": {"native_id": "vid42", "extractor_key": "youtube"},
                          "input_json": {"cookies": False}})
    assert r.status_code == 200
    assert r.json()["input_json"] == {"cookies": False}     # per-run decision recorded
    t = client.get(f"/things/{v}").json()
    assert t["best_oi"] == oi                                # OI file uuid stored
    assert t["extractor_key"] == "youtube" and t["native_id"] == "vid42"  # identity backfilled
    assert t["try_on"] is None and t["last_success_dt"]      # acquired; never re-fetch
    assert t["last_failure_dt"] is None


def test_second_result_on_finalized_run_is_conflict(client):
    # #1: a run is finalized exactly once. A successful download is recorded, then the worker's
    # response is lost to a transient error and it reports a failure for the same run; the second
    # POST must 409 and NOT demote the acquired video back to failed + backoff.
    oi = str(uuid.uuid4())
    v, rid = _claimed_download(client, url="http://e/dl-once")
    assert client.post(f"/jobs/{rid}/result",
                       json={"success": True, "best_oi": oi,
                             "video": {"native_id": "vid99", "extractor_key": "youtube"}}
                       ).status_code == 200
    # the lost-response report_failure shape (job_runner posts info=None -> success=False)
    r2 = client.post(f"/jobs/{rid}/result", json={"success": False})
    assert r2.status_code == 409
    t = client.get(f"/things/{v}").json()
    assert t["best_oi"] == oi                        # acquired media untouched
    assert t["try_on"] is None                       # still acquired (no backoff re-armed)
    assert t["last_success_dt"] and t["last_failure_dt"] is None


def test_download_result_backfill_no_overwrite(client):
    # identity backfill is NULL-only: an already-known extractor/native is not overwritten
    v, rid = _claimed_download(client, url="http://e/dl2",
                               extractor_key="vimeo", native_id="orig")
    client.post(f"/jobs/{rid}/result",
                json={"success": True, "best_oi": str(uuid.uuid4()),
                      "video": {"native_id": "new", "extractor_key": "youtube"}})
    t = client.get(f"/things/{v}").json()
    assert t["extractor_key"] == "vimeo" and t["native_id"] == "orig"


def test_download_result_failure(client):
    v, rid = _claimed_download(client, url="http://e/dlf")
    r = client.post(f"/jobs/{rid}/result", json={"success": False})
    assert r.status_code == 200 and r.json()["success"] is False
    t = client.get(f"/things/{v}").json()
    assert t["last_failure_dt"] and t["best_oi"] is None
    assert t["try_on"] is not None        # failure backoff applied (1.4), not left at today/null


def test_download_result_enriches_and_fans_out_channel(client):
    # stub -> download with no meta/human step in between: the download's full extract is the
    # only metadata we get, so its display fields + channel must be captured (== meta path).
    oi = str(uuid.uuid4())
    v, rid = _claimed_download(client, url="http://e/dlmeta")   # url-only stub
    client.post(f"/jobs/{rid}/result",
                json={"success": True, "best_oi": oi,
                      "video": {"native_id": "dv1", "title": "Downloaded", "extractor_key": "youtube",
                                "thumbnail_url": "http://e/dv1.jpg",
                                "channel": {"url": "http://e/dchan", "title": "DChan"},
                                "info_json": {"id": "dv1"}}})
    t = client.get(f"/things/{v}").json()
    assert t["best_oi"] == oi and t["try_on"] is None and t["last_success_dt"]
    assert t["title"] == "Downloaded" and t["thumbnail_url"] == "http://e/dv1.jpg"  # display captured
    related = client.get(f"/things/{v}/related").json()
    chan = [e for e in related if e["channel"]]
    assert len(chan) == 1 and chan[0]["thing"]["url"] == "http://e/dchan"           # channel fanned out


def _claimed_meta(client, **kw):
    """Seed a C-band, under-described due video (last_success_dt NULL), claim it -> metadata-only
    (download False)."""
    kw.setdefault("try_on", _TODAY)   # unrated -> C
    v = _seed_thing(type="video", **kw)
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["download"] is False
    return v, job["run_id"]


def test_meta_result_enriches_without_acquiring(client):
    v, rid = _claimed_meta(client, url="http://e/m1")
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True,
                          "video": {"native_id": "mv1", "title": "Fetched Title",
                                    "extractor_key": "youtube",
                                    "channel": {"url": "http://e/chan/m1"},
                                    "info_json": {"id": "mv1", "description": "d"}}})
    assert r.status_code == 200
    assert r.json()["entries_hash"]                      # leaf empty-membership hash -> backs off
    t = client.get(f"/things/{v}").json()
    assert t["title"] == "Fetched Title"                 # NULL display backfilled from the fetch
    assert t["extractor_key"] == "youtube" and t["native_id"] == "mv1"
    assert t["attrs"]["info_json"]["description"] == "d"  # Stage-2 load-info hint stored
    assert t["best_oi"] is None                          # metadata only, NOT acquired
    assert t["last_success_dt"]                          # human-decision metadata now in hand
    assert t["last_failure_dt"] is None
    assert t["try_on"] == _TODAY.isoformat()             # terminal but stays due (today), not backed off
    # last_success_dt being set is sufficient to prove meta_branch won't re-dispatch this video.


def test_meta_result_backfills_null_url(client):
    # A stub first created without a webpage URL (only native_id) gets `url` filled from the
    # fresh extract — url is a NULL-backfill field, required for enough_to_rate.
    v, rid = _claimed_meta(client, native_id="uurl1", url=None)   # url-less stub
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True,
                          "video": {"native_id": "uurl1", "url": "http://e/real-url",
                                    "title": "T", "extractor_key": "youtube",
                                    "channel": {"url": "http://e/chan/u"},
                                    "info_json": {"id": "uurl1"}}})
    assert r.status_code == 200
    t = client.get(f"/things/{v}").json()
    assert t["url"] == "http://e/real-url"               # NULL url backfilled from the fetch


def test_meta_result_incomplete_is_still_terminal(client):
    # A meta result still missing identity fields (here: no channel) is nonetheless TERMINAL:
    # the full single-video extract can't be improved by re-running it, so last_success_dt is
    # set unconditionally (§4.2) — otherwise a still-bare video would re-match meta_branch
    # (last_success_dt IS NULL) and loop forever (#163).
    v, rid = _claimed_meta(client, url="http://e/mincomplete")
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True,
                          "video": {"native_id": "mi1", "title": "No Chan",
                                    "extractor_key": "youtube",
                                    "info_json": {"id": "mi1"}}})
    assert r.status_code == 200
    t = client.get(f"/things/{v}").json()
    assert t["last_success_dt"]             # terminal: set even though channel is missing
    assert t["best_oi"] is None            # metadata only, NOT acquired
    assert t["try_on"] == _TODAY.isoformat()  # terminal but stays due (today); not meta-claimable (last_success set)


def test_meta_b_video_no_backoff_then_downloads(client):
    # #191: a B+ stub the flat pull left container=NULL is claimed as a metadata pull first (to
    # classify it), but must NOT then sit out a meta backoff before the download. The meta result
    # leaves it due (try_on=today) so the very next claim is the Stage-2 download — no multi-day
    # gap between the meta pull and the wanted media. (C-band keeps the normal backoff, above.)
    v = _seed_thing(url="http://e/b-unknown", human_rating=1.0, try_on=_TODAY)  # container NULL, B
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["download"] is False          # stage1 meta first
    r = client.post(f"/jobs/{job['run_id']}/result",
                    json={"success": True,
                          "video": {"native_id": "bv1", "title": "B Vid",
                                    "extractor_key": "youtube",
                                    "channel": {"url": "http://e/chan/b"},
                                    "info_json": {"id": "bv1"}}})
    assert r.status_code == 200
    t = client.get(f"/things/{v}").json()
    assert t["container"] is False                                # classified as a leaf by the meta pull
    assert t["last_success_dt"] and t["best_oi"] is None          # complete metadata, not acquired
    assert datetime.date.fromisoformat(t["try_on"]) == _TODAY     # B+: due now, NOT backed off
    # the video is now download-eligible immediately (no day gap). Containers (the freshly
    # fanned-out channel) sort first, so drain claims until our video is dispatched as a download.
    for _ in range(5):
        job2 = _claim(client)
        assert job2 is not None, "video never dispatched for download"
        if job2["thing"]["id"] == v:
            assert job2["download"] is True
            break


def test_meta_c_then_parent_rating_downloads(client):
    # A C-band leaf, meta-enriched, must stay due (try_on=today) — NOT backed off — so that when
    # its parent is later rated B+ (lifting the child's machine rating to B) it is claimed for
    # download immediately, with no intervening backoff gap. Same behavior as a never-meta'd
    # C-band sibling, which keeps its default try_on=today.
    pl = _seed_thing(type="playlist", url="http://e/cpd-pl", try_on=None)   # unrated parent (not due)
    v = _seed_thing(type="video", url="http://e/cpd-v", try_on=_TODAY)      # unrated -> C leaf
    _seed_rel(pl, v)
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["download"] is False     # C-band -> meta, not download
    r = client.post(f"/jobs/{job['run_id']}/result",
                    json={"success": True,
                          "video": {"native_id": "cpd1", "title": "C Vid",
                                    "extractor_key": "youtube",
                                    "channel": {"url": "http://e/chan/cpd"},
                                    "info_json": {"id": "cpd1"}}})
    assert r.status_code == 200
    t = client.get(f"/things/{v}").json()
    assert t["last_success_dt"] and t["best_oi"] is None        # meta complete, not acquired
    assert t["try_on"] == _TODAY.isoformat()                    # stays due, NOT backed off
    # Rate the parent B+ -> child's machine rating becomes B -> download-eligible right now.
    assert client.patch(f"/things/{pl}", json={"human_rating": 1.0}).status_code == 200
    # Containers sort first, so drain claims until our video is dispatched -- it must be a download.
    for _ in range(5):
        job2 = _claim(client)
        assert job2 is not None, "video never dispatched for download"
        if job2["thing"]["id"] == v:
            assert job2["download"] is True
            break
    else:
        assert False, "video never dispatched for download"


def test_meta_result_failure_backs_off(client):
    # On failure the worker sends no `video` body; handled by the shared video-failure path.
    v, rid = _claimed_meta(client, url="http://e/m3")
    r = client.post(f"/jobs/{rid}/result", json={"success": False})
    assert r.status_code == 200 and r.json()["success"] is False
    t = client.get(f"/things/{v}").json()
    assert t["last_failure_dt"] and t["best_oi"] is None and t["last_success_dt"] is None
    assert t["try_on"] is not None


# --- try_on backoff (Task 1.4): wired through the API ----------------------------------
# (the pure backoff math — initial_interval / next_try_on day arithmetic — lives in
# test_xform.py; these tests only assert the endpoint wires it in.)

def test_playlist_success_sets_backoff(client):
    # The endpoint wires success through the backoff helper: try_on lands in the future
    # (exact day arithmetic is pinned by the pure next_try_on tests in test_xform).
    url = "http://example/pl/backoff"
    tid, rid = _claimed_run(client, url)          # C-rated playlist, claimed
    assert client.post(f"/jobs/{rid}/result",
                       json={"playlist": _pl_payload(2, url=url, native="plbo")}
                       ).status_code == 200
    t = client.get(f"/things/{tid}").json()
    assert datetime.date.fromisoformat(t["try_on"]) > _TODAY
    assert t["last_success_dt"]


def test_playlist_failure_sets_backoff(client):
    # Failure also backs off (and records the failure timestamp); the magnitude is the
    # pure helper's concern, asserted in test_xform.
    url = "http://example/pl/failbo"
    tid, rid = _claimed_run(client, url)
    assert client.post(f"/jobs/{rid}/result", json={"success": False}).status_code == 200
    t = client.get(f"/things/{tid}").json()
    assert datetime.date.fromisoformat(t["try_on"]) > _TODAY
    assert t["last_failure_dt"]


def test_claim_cookies_escalation(client):
    # last completed run failed cookielessly -> the next claim suggests cookies (§4.7)
    v = _seed_thing(type="video", url="http://e/esc", human_rating=1.0, try_on=_TODAY)
    with _session() as s:
        s.add(models.Run(thing_id=uuid.UUID(v), success=False,
                         starttime=models.naive_utcnow(), input_json={"cookies": False}))
        s.commit()
    job = _claim(client)
    assert job["thing"]["id"] == v and job["cookies"] is True


# --- 3.1 recent-activity feed (GET /runs/) --------------------------------------------

def _seed_run_at(thing_id: str, success, offset_secs: int,
                 data_json=None) -> str:
    """Insert a run for a thing with an explicit success state and starttime offset."""
    with _session() as s:
        run = models.Run(thing_id=uuid.UUID(thing_id), success=success, data_json=data_json,
                         starttime=models.naive_utcnow() + datetime.timedelta(seconds=offset_secs),
                         endtime=None if success is None else models.naive_utcnow())
        s.add(run)
        s.commit()
        s.refresh(run)
        return str(run.id)


def test_runs_feed_empty(client):
    assert client.get("/runs/").json() == []


def test_runs_feed_recent_first_with_thing_fields(client):
    older = _seed_thing(type="playlist", url="http://e/act1", title="Older PL")
    newer = _seed_thing(type="playlist", url="http://e/act2", title="Newer PL")
    r_old = _seed_run_at(older, True, 0, data_json={"raw": 1})
    r_new = _seed_run_at(newer, False, 10)

    feed = client.get("/runs/").json()
    assert [r["id"] for r in feed] == [r_new, r_old]        # newest first
    top = feed[0]
    assert top["thing_id"] == newer and top["thing_url"] == "http://e/act2"
    assert top["thing_title"] == "Newer PL"
    assert top["container"] is True and top["success"] is False
    assert "data_json" not in top and "input_json" not in top   # slim feed


def test_runs_feed_includes_active(client):
    # the default (unfiltered) feed includes active/in-progress runs (success/endtime NULL)
    t = _seed_thing(type="playlist", url="http://e/active", title="Active PL")
    r_done = _seed_run_at(t, True, 0)
    r_active = _seed_run_at(t, None, 10)   # claimed, still running

    feed = client.get("/runs/").json()
    assert {r["id"] for r in feed} == {r_done, r_active}
    active = next(r for r in feed if r["id"] == r_active)
    assert active["success"] is None and active["endtime"] is None


def test_runs_feed_success_and_in_progress_filters(client):
    t = _seed_thing(type="playlist", url="http://e/filt", title="Filt PL")
    r_prog = _seed_run_at(t, None, 0)
    r_fail = _seed_run_at(t, False, 10)
    r_ok = _seed_run_at(t, True, 20)

    assert {r["id"] for r in client.get("/runs/", params={"success": False}).json()} == {r_fail}
    assert {r["id"] for r in client.get("/runs/", params={"success": True}).json()} == {r_ok}
    assert {r["id"] for r in client.get("/runs/", params={"in_progress": True}).json()} == {r_prog}


def test_runs_feed_limit(client):
    t = _seed_thing(type="playlist", url="http://e/lim", title="Lim PL")
    for i in range(3):
        _seed_run_at(t, True, i)
    assert len(client.get("/runs/", params={"limit": 2}).json()) == 2
