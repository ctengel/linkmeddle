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
