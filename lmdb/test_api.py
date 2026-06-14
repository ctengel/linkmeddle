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
    r = client.post("/things/", json={"url": "http://example/pl/1"})
    assert r.status_code == 201
    t = r.json()
    assert t["type"] == "playlist"          # unknown -> assume playlist
    assert t["human_rating"] == 1.0         # default B
    assert t["try_on"] == datetime.date.today().isoformat()
    assert t["extractor_key"] is None and t["native_id"] is None  # worker fills later
    assert t["id"] and t["created_dt"]


@pytest.mark.parametrize("grade,value", [("A", 2.0), ("B", 1.0), ("C", 0.0)])
def test_add_thing_rating_override(client, grade, value):
    r = client.post("/things/", json={"url": f"http://example/pl/{grade}", "rating": grade})
    assert r.status_code == 201
    assert r.json()["human_rating"] == value


def test_add_thing_type_override(client):
    r = client.post("/things/", json={"url": "http://example/v/1", "type": "video"})
    assert r.status_code == 201
    assert r.json()["type"] == "video"


def test_add_thing_invalid_rating(client):
    r = client.post("/things/", json={"url": "http://example/pl/x", "rating": "D"})
    assert r.status_code == 422


def test_add_thing_idempotent(client):
    # #142: duplicate URL must not create a second row
    r1 = client.post("/things/", json={"url": "http://example/dup"})
    assert r1.status_code == 201
    r2 = client.post("/things/", json={"url": "http://example/dup", "rating": "A"})
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["human_rating"] == 1.0  # unchanged; existing returned as-is


# --- list / search ---------------------------------------------------------------------

def test_list_things_empty(client):
    r = client.get("/things/")
    assert r.status_code == 200
    assert r.json() == []


def test_list_filters(client):
    client.post("/things/", json={"url": "http://example/p1", "type": "playlist"})
    client.post("/things/", json={"url": "http://example/p2", "type": "playlist", "rating": "A"})
    client.post("/things/", json={"url": "http://example/v1", "type": "video"})

    assert len(client.get("/things/", params={"type": "playlist"}).json()) == 2
    assert len(client.get("/things/", params={"type": "video"}).json()) == 1
    assert len(client.get("/things/", params={"rating": "A"}).json()) == 1
    one = client.get("/things/", params={"url": "http://example/v1"}).json()
    assert len(one) == 1 and one[0]["type"] == "video"
    # everything added via POST has a human_rating, so needs_rating is empty
    assert client.get("/things/", params={"needs_rating": True}).json() == []
    # all added with try_on=today -> all due
    assert len(client.get("/things/", params={"due": True}).json()) == 3


def test_extractor_native_lookup(client):
    # the V4 replacement for GET /videos/{extractor}/{id}; extractor/native are set by
    # the worker (Phase 1), so seed directly here.
    with _session() as s:
        s.add(models.Thing(url="http://example/vid", type="video",
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
    pl = models.Thing(url="http://example/pl", type="playlist", title="PL")
    vid = models.Thing(url="http://example/vid2", type="video", title="V")
    with _session() as s:
        s.add(pl)
        s.add(vid)
        s.commit()
        s.refresh(pl)
        s.refresh(vid)
        s.add(models.Rel(parent=pl.id, child=vid.id, type="playlist_video"))
        s.commit()
        pl_id, vid_id = str(pl.id), str(vid.id)

    # plain get: related omitted by default
    base = client.get(f"/things/{pl_id}").json()
    assert base["related"] == []

    # include=related from the playlist -> the video as a child
    full = client.get(f"/things/{pl_id}", params={"include": "related"}).json()
    assert len(full["related"]) == 1
    edge = full["related"][0]
    assert edge["direction"] == "child"
    assert edge["rel_type"] == "playlist_video"
    assert edge["thing"]["id"] == vid_id

    # from the video's side it's a parent edge
    rel = client.get(f"/things/{vid_id}/related").json()
    assert rel[0]["direction"] == "parent"
    assert rel[0]["thing"]["id"] == pl_id
    # direction filter narrows it
    assert client.get(f"/things/{vid_id}/related", params={"direction": "child"}).json() == []


def test_thing_runs(client):
    pl = models.Thing(url="http://example/plruns", type="playlist")
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

def test_patch_rating_grade(client):
    tid = client.post("/things/", json={"url": "http://example/patch1"}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"grade": "A"})
    assert r.status_code == 200
    assert r.json()["human_rating"] == 2.0


def test_patch_rating_numeric(client):
    tid = client.post("/things/", json={"url": "http://example/patch2"}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"human_rating": -2.0})
    assert r.json()["human_rating"] == -2.0


def test_patch_permafail_ack(client):
    tid = client.post("/things/", json={"url": "http://example/patch3"}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"try_on": None})
    assert r.status_code == 200
    assert r.json()["try_on"] is None


def test_patch_404(client):
    r = client.patch(f"/things/{uuid.uuid4()}", json={"grade": "A"})
    assert r.status_code == 404


# --- jobs: dispatch (Task 1.2) + Stage-1 ingest (Task 1.1) ------------------------------

def _seed_thing(**kw) -> str:
    """Insert a thing directly with explicit fields; returns its id (str).

    try_on is re-applied after insert so an explicit value (incl. None for permafail)
    overrides the column's server_default.
    """
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


def _claimed_run(client, url):
    """Add a B-rated playlist by url and claim it; returns (thing_id, run_id)."""
    tid = client.post("/things/", json={"url": url}).json()["id"]
    job = _claim(client)
    assert job and job["thing"]["id"] == tid and job["action"] == "pull"
    return tid, job["run_id"]


_TODAY = datetime.date.today()
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
    assert job["thing"]["id"] == p and job["action"] == "pull"  # playlist wins regardless


def test_claim_video_when_no_playlist(client):
    v = _seed_thing(type="video", url="http://e/v2", human_rating=1.0, try_on=_TODAY)
    job = _claim(client)
    assert job["thing"]["id"] == v and job["action"] == "download"


def test_claim_skips_ineligible_videos(client):
    _seed_thing(type="video", url="http://e/vc", human_rating=0.0, try_on=_TODAY)   # C < B
    _seed_thing(type="video", url="http://e/vacq", human_rating=2.0, try_on=_TODAY,
                best_oi="oi:1")                                                     # acquired
    _seed_thing(type="video", url="http://e/vfut", human_rating=2.0, try_on=_FUTURE)  # not due
    assert _claim(client) is None


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
    assert job["thing"]["id"] == p and job["action"] == "pull" and job["run_id"]
    runs = client.get(f"/things/{p}/runs").json()
    assert len(runs) == 1 and runs[0]["id"] == job["run_id"]
    assert runs[0]["success"] is None and runs[0]["worker"] == "w1"   # in-progress marker


def _pl_payload(n=3, url="http://example/pl/ingest", native="plingest") -> dict:
    """A JSON-ready LM-native PlaylistFull body for the ingest endpoint."""
    pl = models.PlaylistFull(
        id=native, title="Ingest PL", webpage_url=url,
        modified_date=datetime.datetime(2026, 1, 31), playlist_count=n,
        extractor=models.DLPIE(extractor_key="YouTube", extractor="youtube"),
        channel=models.UlChan(uploader_id="up1", uploader="Up One",
                              uploader_url="http://example/up1"),
        entries=[models.VidFull(
            id=f"vid{i}", title=f"Video {i}", webpage_url=f"http://example/v/{i}",
            thumbnail=f"http://example/v/{i}/t.jpg",
            upload_date=datetime.datetime(2026, 1, i + 1),
            extractor=models.DLPIE(extractor_key="YouTube", extractor="youtube"),
            channel=models.UlChan(uploader_id="up1", uploader="Up One",
                                  uploader_url="http://example/up1"),
        ) for i in range(n)],
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
    assert pl["extractor_key"] == "youtube"   # lowercased
    assert pl["title"] == "Ingest PL"
    assert pl["last_success_dt"]

    related = client.get(f"/things/{tid}", params={"include": "related"}).json()["related"]
    kids = [e for e in related if e["direction"] == "child"]
    parents = [e for e in related if e["direction"] == "parent"]
    assert len(kids) == 3
    assert all(e["rel_type"] == "playlist_video" for e in kids)
    assert all(e["thing"]["type"] == "video" for e in kids)
    assert len(parents) == 1
    assert parents[0]["rel_type"] == "channel_playlist"
    assert parents[0]["thing"]["type"] == "channel"

    # video stubs carry denormalized fields and are eligible for Stage-2 (try_on=today)
    vids = client.get("/things/", params={"type": "video"}).json()
    assert len(vids) == 3
    assert all(v["title"] for v in vids)
    assert all(v["try_on"] == datetime.date.today().isoformat() for v in vids)


def test_ingest_idempotent(client):
    url = "http://example/pl/idem"
    tid, rid = _claimed_run(client, url)
    payload = _pl_payload(3, url=url, native="plidem")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    # second pull the same day: claim would skip it, so seed the run directly
    rid2 = _seed_run(tid)
    assert client.post(f"/jobs/{rid2}/result", json={"playlist": payload}).status_code == 200
    assert len(client.get("/things/", params={"type": "video"}).json()) == 3   # no dup things
    assert len(client.get("/things/", params={"type": "channel"}).json()) == 1
    assert len(client.get(f"/things/{tid}/runs").json()) == 2                   # but two runs
    kids = [e for e in client.get(f"/things/{tid}", params={"include": "related"}).json()
            ["related"] if e["direction"] == "child"]
    assert len(kids) == 3                                                       # no dup rels


def test_ingest_failure_records_only(client):
    tid, rid = _claimed_run(client, "http://example/pl/fail")
    r = client.post(f"/jobs/{rid}/result", json={"success": False})
    assert r.status_code == 200 and r.json()["success"] is False
    pl = client.get(f"/things/{tid}").json()
    assert pl["last_failure_dt"] and pl["last_success_dt"] is None
    assert client.get("/things/", params={"type": "video"}).json() == []       # no fan-out


def test_ingest_success_requires_playlist(client):
    _, rid = _claimed_run(client, "http://example/pl/req")
    r = client.post(f"/jobs/{rid}/result", json={"success": True})
    assert r.status_code == 422


def test_ingest_run_404(client):
    r = client.post(f"/jobs/{uuid.uuid4()}/result", json={"success": False})
    assert r.status_code == 404
