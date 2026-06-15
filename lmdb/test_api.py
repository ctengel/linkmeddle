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
from lmdb import xform

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
    assert t["type"] == "playlist"          # unknown -> assume playlist
    assert t["human_rating"] == 1.0         # default B
    assert t["try_on"] == models.naive_utcnow().date().isoformat()  # app is UTC, not local
    assert t["bucket"] == "b1"              # required, round-trips ([A10])
    assert t["attrs"] is None               # no cookies/lpm_lib hints supplied
    assert t["extractor_key"] is None and t["native_id"] is None  # worker fills later
    assert t["id"] and t["created_dt"]


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


@pytest.mark.parametrize("grade,value", [("A", 2.0), ("B", 1.0), ("C", 0.0)])
def test_add_thing_rating_override(client, grade, value):
    r = client.post("/things/", json={"url": f"http://example/pl/{grade}", "rating": grade,
                                      "bucket": "b"})
    assert r.status_code == 201
    assert r.json()["human_rating"] == value


def test_add_thing_type_override(client):
    r = client.post("/things/", json={"url": "http://example/v/1", "type": "video",
                                      "bucket": "b"})
    assert r.status_code == 201
    assert r.json()["type"] == "video"


def test_add_thing_invalid_rating(client):
    r = client.post("/things/", json={"url": "http://example/pl/x", "rating": "D",
                                      "bucket": "b"})
    assert r.status_code == 422


def test_add_thing_idempotent(client):
    # #142: duplicate URL must not create a second row
    r1 = client.post("/things/", json={"url": "http://example/dup", "bucket": "first"})
    assert r1.status_code == 201
    r2 = client.post("/things/", json={"url": "http://example/dup", "rating": "A",
                                       "bucket": "second"})
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["human_rating"] == 1.0  # unchanged; existing returned as-is
    assert r2.json()["bucket"] == "first"    # bucket is immutable ([A10])


# --- list / search ---------------------------------------------------------------------

def test_list_things_empty(client):
    r = client.get("/things/")
    assert r.status_code == 200
    assert r.json() == []


def test_list_filters(client):
    client.post("/things/", json={"url": "http://example/p1", "type": "playlist", "bucket": "b"})
    client.post("/things/", json={"url": "http://example/p2", "type": "playlist",
                                  "rating": "A", "bucket": "b"})
    client.post("/things/", json={"url": "http://example/v1", "type": "video", "bucket": "b"})

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
        s.add(models.Thing(url="http://example/vid", type="video", bucket="testbucket",
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
    pl = models.Thing(url="http://example/pl", type="playlist", title="PL", bucket="testbucket")
    vid = models.Thing(url="http://example/vid2", type="video", title="V", bucket="testbucket")
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
    pl = models.Thing(url="http://example/plruns", type="playlist", bucket="testbucket")
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
    tid = client.post("/things/", json={"url": "http://example/patch1", "bucket": "b"}).json()["id"]
    r = client.patch(f"/things/{tid}", json={"grade": "A"})
    assert r.status_code == 200
    assert r.json()["human_rating"] == 2.0


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
    r = client.patch(f"/things/{uuid.uuid4()}", json={"grade": "A"})
    assert r.status_code == 404


# --- patch: raise-to-eligible try_on side-effect (Task 2.1, §2.5) ----------------------

def test_patch_raise_resurrects_permafail(client):
    tid = _seed_thing(type="playlist", url="http://e/raise-perma",
                      human_rating=-1.0, try_on=None)  # D, permafail-acked
    r = client.patch(f"/things/{tid}", json={"grade": "B"})
    assert r.status_code == 200
    assert r.json()["try_on"] == _TODAY.isoformat()


def test_patch_raise_pulls_future_forward(client):
    tid = _seed_thing(type="playlist", url="http://e/raise-future",
                      human_rating=0.0, try_on=_FUTURE)  # C, scheduled ahead
    r = client.patch(f"/things/{tid}", json={"grade": "A"})
    assert r.json()["try_on"] == _TODAY.isoformat()


def test_patch_raise_skips_acquired(client):
    tid = _seed_thing(type="video", url="http://e/raise-acq", human_rating=1.0,
                      try_on=None, best_oi=uuid.uuid4())  # already acquired
    r = client.patch(f"/things/{tid}", json={"grade": "A"})
    assert r.json()["try_on"] is None   # best_oi guard: never disturbed


def test_patch_downgrade_does_not_pull_forward(client):
    tid = _seed_thing(type="playlist", url="http://e/downgrade",
                      human_rating=2.0, try_on=_FUTURE)  # A, scheduled ahead
    r = client.patch(f"/things/{tid}", json={"grade": "C"})  # still eligible, but a drop
    assert r.json()["try_on"] == _FUTURE.isoformat()


def test_patch_raise_to_still_ineligible(client):
    tid = _seed_thing(type="video", url="http://e/still-inelig",
                      human_rating=-1.0, try_on=None)  # D video
    r = client.patch(f"/things/{tid}", json={"grade": "C"})  # 0.0 < 0.5 video floor
    assert r.json()["try_on"] is None


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
    """Add a B-rated playlist by url and claim it; returns (thing_id, run_id)."""
    body = {"url": url, "bucket": bucket}
    if cookies is not None:
        body["cookies"] = cookies
    if lpm_lib is not None:
        body["lpm_lib"] = lpm_lib
    tid = client.post("/things/", json=body).json()["id"]
    job = _claim(client)
    assert job and job["thing"]["id"] == tid and job["action"] == "pull"
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
    assert job["thing"]["id"] == p and job["action"] == "pull"  # playlist wins regardless


def test_claim_video_when_no_playlist(client):
    v = _seed_thing(type="video", url="http://e/v2", human_rating=1.0, try_on=_TODAY)
    job = _claim(client)
    assert job["thing"]["id"] == v and job["action"] == "download"


def test_claim_skips_ineligible_videos(client):
    _seed_thing(type="video", url="http://e/vc", human_rating=0.0, try_on=_TODAY)   # C < B
    _seed_thing(type="video", url="http://e/vacq", human_rating=2.0, try_on=_TODAY,
                best_oi=uuid.uuid4())                                               # acquired
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


def _pl_payload(n=3, url="http://example/pl/ingest", native="plingest",
                per_video_uploader=False) -> dict:
    """A JSON-ready LM-native PlaylistFull body for the ingest endpoint.

    By default every entry shares the playlist's uploader (up1). With
    `per_video_uploader`, each entry gets its own uploader (vup{i}) so the channel
    fan-out (`channel_video`, 1.3c) can be exercised with distinct uploaders.
    """
    def vid_channel(i):
        if per_video_uploader:
            return models.UlChan(uploader_id=f"vup{i}", uploader=f"V Up {i}",
                                 uploader_url=f"http://example/vup{i}")
        return models.UlChan(uploader_id="up1", uploader="Up One",
                             uploader_url="http://example/up1")
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
            channel=vid_channel(i),
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
    assert all(v["try_on"] == models.naive_utcnow().date().isoformat() for v in vids)

    # 1.3a: stubs inherit the dispatched playlist's bucket (immutable)
    assert all(v["bucket"] == "plbucket" for v in vids)
    chans = client.get("/things/", params={"type": "channel"}).json()
    assert chans and all(c["bucket"] == "plbucket" for c in chans)


def test_ingest_propagates_hints(client):
    # 1.3b: a playlist's cookies/lpm_lib hints propagate onto its video stubs (attrs);
    # channels do not carry the hints (bucket only).
    url = "http://example/pl/hintprop"
    tid, rid = _claimed_run(client, url, cookies=True, lpm_lib="lib7")
    payload = _pl_payload(2, url=url, native="plhint")
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    vids = client.get("/things/", params={"type": "video"}).json()
    assert vids and all(v["attrs"] == {"cookies": True, "lpm_lib": "lib7"} for v in vids)
    chans = client.get("/things/", params={"type": "channel"}).json()
    assert chans and all(not c["attrs"] for c in chans)


def test_ingest_per_video_uploader_channels(client):
    # 1.3c: each distinct video uploader gets a type='channel' thing + channel_video edge;
    # the playlist keeps its own channel_playlist parent.
    url = "http://example/pl/chans"
    tid, rid = _claimed_run(client, url)
    payload = _pl_payload(3, url=url, native="plchans", per_video_uploader=True)
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200

    # 1 playlist uploader (up1) + 3 distinct video uploaders (vup0..2)
    chans = client.get("/things/", params={"type": "channel"}).json()
    assert len(chans) == 4

    # the playlist's only parent edge is channel_playlist
    pl_parents = [e for e in client.get(f"/things/{tid}", params={"include": "related"})
                  .json()["related"] if e["direction"] == "parent"]
    assert len(pl_parents) == 1 and pl_parents[0]["rel_type"] == "channel_playlist"

    # every video has exactly one channel_video parent (+ its playlist_video parent)
    for vid in client.get("/things/", params={"type": "video"}).json():
        parents = client.get(f"/things/{vid['id']}/related",
                             params={"direction": "parent"}).json()
        rel_types = sorted(e["rel_type"] for e in parents)
        assert rel_types == ["channel_video", "playlist_video"]
        chan_edge = next(e for e in parents if e["rel_type"] == "channel_video")
        assert chan_edge["thing"]["type"] == "channel"


def test_ingest_shared_uploader_one_channel(client):
    # 1.3c: same-uploader videos (default payload: all up1) reuse a single channel node,
    # which carries both channel_playlist (to the pl) and channel_video (to each vid) edges.
    url = "http://example/pl/shared"
    tid, rid = _claimed_run(client, url)
    payload = _pl_payload(3, url=url, native="plshared")  # all share up1 = pl channel
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    chans = client.get("/things/", params={"type": "channel"}).json()
    assert len(chans) == 1
    children = client.get(f"/things/{chans[0]['id']}/related",
                          params={"direction": "child"}).json()
    rel_types = sorted(e["rel_type"] for e in children)
    assert rel_types == ["channel_playlist", "channel_video", "channel_video", "channel_video"]


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


def test_ingest_preserves_existing_bucket(client):
    # 1.3a: a thing added directly keeps its own bucket even when a later playlist pull
    # (carrying a different inherited bucket) re-discovers it — bucket is immutable.
    client.post("/things/", json={"url": "http://example/v/0", "type": "video",
                                  "bucket": "vidbucket"})
    url = "http://example/pl/preserve"
    tid, rid = _claimed_run(client, url, bucket="plbucket")
    payload = _pl_payload(3, url=url, native="plpreserve")  # entries include .../v/0..2
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    by_url = {v["url"]: v for v in client.get("/things/", params={"type": "video"}).json()}
    assert by_url["http://example/v/0"]["bucket"] == "vidbucket"   # kept, not overwritten
    assert by_url["http://example/v/1"]["bucket"] == "plbucket"    # newly inherited


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
    assert job and job["thing"]["id"] == v and job["action"] == "download"
    return v, job["run_id"]


def test_download_result_sets_best_oi(client):
    oi = str(uuid.uuid4())
    v, rid = _claimed_download(client, url="http://e/dl")  # no extractor/native yet
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True, "best_oi": oi,
                          "extractor_key": "youtube", "native_id": "vid42",
                          "input_json": {"cookies": False}})
    assert r.status_code == 200
    assert r.json()["input_json"] == {"cookies": False}     # per-run decision recorded
    t = client.get(f"/things/{v}").json()
    assert t["best_oi"] == oi                                # OI file uuid stored
    assert t["extractor_key"] == "youtube" and t["native_id"] == "vid42"  # identity backfilled
    assert t["try_on"] is None and t["last_success_dt"]      # acquired; never re-fetch
    assert t["last_failure_dt"] is None


def test_download_result_backfill_no_overwrite(client):
    # identity backfill is NULL-only: an already-known extractor/native is not overwritten
    v, rid = _claimed_download(client, url="http://e/dl2",
                               extractor_key="vimeo", native_id="orig")
    client.post(f"/jobs/{rid}/result",
                json={"success": True, "best_oi": str(uuid.uuid4()),
                      "extractor_key": "youtube", "native_id": "new"})
    t = client.get(f"/things/{v}").json()
    assert t["extractor_key"] == "vimeo" and t["native_id"] == "orig"


def test_download_result_failure(client):
    v, rid = _claimed_download(client, url="http://e/dlf")
    r = client.post(f"/jobs/{rid}/result", json={"success": False})
    assert r.status_code == 200 and r.json()["success"] is False
    t = client.get(f"/things/{v}").json()
    assert t["last_failure_dt"] and t["best_oi"] is None
    assert t["try_on"] is not None        # failure backoff applied (1.4), not left at today/null


# --- try_on backoff (Task 1.4): pure xform ---------------------------------------------

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


# --- try_on backoff (Task 1.4): wired through the API ----------------------------------

def test_playlist_success_sets_backoff(client):
    url = "http://example/pl/backoff"
    tid, rid = _claimed_run(client, url)          # B-rated playlist, claimed
    assert client.post(f"/jobs/{rid}/result",
                       json={"playlist": _pl_payload(2, url=url, native="plbo")}
                       ).status_code == 200
    t = client.get(f"/things/{tid}").json()
    expect = (models.naive_utcnow().date() + datetime.timedelta(days=5)).isoformat()  # B initial
    assert t["try_on"] == expect


def test_playlist_failure_sets_backoff(client):
    url = "http://example/pl/failbo"
    tid, rid = _claimed_run(client, url)
    assert client.post(f"/jobs/{rid}/result", json={"success": False}).status_code == 200
    t = client.get(f"/things/{tid}").json()
    # first-ever failure, no prior success -> fib backoff from the B initial: next_fib(5) = 8
    expect = (models.naive_utcnow().date() + datetime.timedelta(days=8)).isoformat()
    assert t["try_on"] == expect and t["last_failure_dt"]


def test_claim_cookies_escalation(client):
    # last completed run failed cookielessly -> the next claim suggests cookies (§4.7)
    v = _seed_thing(type="video", url="http://e/esc", human_rating=1.0, try_on=_TODAY)
    with _session() as s:
        s.add(models.Run(thing_id=uuid.UUID(v), success=False,
                         starttime=models.naive_utcnow(), input_json={"cookies": False}))
        s.commit()
    job = _claim(client)
    assert job["thing"]["id"] == v and job["cookies"] is True
