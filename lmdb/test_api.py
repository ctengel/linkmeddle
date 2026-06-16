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
    assert r.json()["container"] is False   # 'video' hint -> leaf


def test_add_thing_channel_tags_kind(client):
    # 'channel' hint -> a container tagged attrs.kind='channel' (no separate type)
    r = client.post("/things/", json={"url": "http://example/chan/1", "type": "channel",
                                      "bucket": "b"})
    assert r.status_code == 201
    assert r.json()["container"] is True
    assert r.json()["attrs"] == {"kind": "channel"}
    assert client.get("/things/", params={"type": "channel"}).json()[0]["id"] == r.json()["id"]


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
    assert r2.json()["human_rating"] == 0.0  # unchanged; existing returned as-is
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

    # plain get: related omitted by default
    base = client.get(f"/things/{pl_id}").json()
    assert base["related"] == []

    # include=related from the playlist -> the video as a child
    full = client.get(f"/things/{pl_id}", params={"include": "related"}).json()
    assert len(full["related"]) == 1
    edge = full["related"][0]
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


def test_patch_raise_d_to_c_no_meta_opens_meta_job(client):
    # D video with no metadata (last_success_dt NULL) → raise to C → eligible for meta job
    tid = _seed_thing(type="video", url="http://e/d-to-c-no-meta",
                      human_rating=-1.0, try_on=None)
    r = client.patch(f"/things/{tid}", json={"grade": "C"})
    assert r.json()["try_on"] == _TODAY.isoformat()


def test_patch_raise_d_to_c_with_meta_still_sets_try_on(client):
    # D video that already has metadata → raise to C → try_on set, but dispatcher won't
    # claim it for meta (last_success_dt IS NOT NULL); harmless until it reaches B for download
    tid = _seed_thing(type="video", url="http://e/d-to-c-with-meta",
                      human_rating=-1.0, try_on=None,
                      last_success_dt=models.naive_utcnow())
    r = client.patch(f"/things/{tid}", json={"grade": "C"})
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
    _seed_thing(type="video", url="http://e/vc", human_rating=0.0, try_on=_TODAY,
                last_success_dt=models.naive_utcnow())   # C + metadata-complete (no meta job)
    _seed_thing(type="video", url="http://e/vd", human_rating=-1.0, try_on=_TODAY)  # D: no meta
    _seed_thing(type="video", url="http://e/vacq", human_rating=2.0, try_on=_TODAY,
                best_oi=uuid.uuid4())                                               # acquired
    _seed_thing(type="video", url="http://e/vfut", human_rating=2.0, try_on=_FUTURE)  # not due
    assert _claim(client) is None


def test_claim_meta_for_underdescribed_c(client):
    # A C-band video the flat pull couldn't describe (last_success_dt NULL) -> metadata-only job.
    v = _seed_thing(type="video", url="http://e/vmeta", try_on=_TODAY)  # unrated -> C
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["action"] == "meta"


def test_claim_download_outranks_meta(client):
    # A B video (download) outranks a C video (meta) in a single ordering.
    b = _seed_thing(type="video", url="http://e/vb", human_rating=1.0, try_on=_TODAY)
    _seed_thing(type="video", url="http://e/vcm", try_on=_TODAY)        # C -> meta
    job = _claim(client)
    assert job["thing"]["id"] == b and job["action"] == "download"


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
    pl = models.PlaylistFull(
        url=url, native_id=native, title="Ingest PL",
        modified=datetime.datetime(2026, 1, 31), playlist_count=n,
        extractor_key="youtube",
        channel=models.UlChan(native_id="up1", title="Up One",
                                url="http://example/up1"),
        entries=[models.VidFull(
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
    become `container=True` stubs pulled on their own later (recursion).
    """
    chan = models.UlChan(native_id=native, title="The Channel", url=url)
    pl = models.PlaylistFull(
        url=url, native_id=native, title="The Channel", extractor_key="youtube",
        playlist_count=n_videos + n_playlists,          # members = videos + sub-containers
        channel=chan,                                   # the channel is its own uploader
        entries=[models.VidFull(
            native_id=f"cv{i}", title=f"CVid {i}", url=f"http://example/cv/{i}",
            extractor_key="youtube", channel=chan,      # uploaded BY this channel
        ) for i in range(n_videos)],
        child_playlists=[models.PlaylistFull(
            url=f"http://example/chan/{native}/pl{j}", native_id=f"{native}pl{j}",
            title=f"Sub PL {j}", extractor_key="youtube", channel=chan,
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

    related = client.get(f"/things/{tid}", params={"include": "related"}).json()["related"]
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
    vids = client.get("/things/", params={"type": "video"}).json()
    assert len(vids) == 3
    assert all(v["title"] for v in vids)
    assert all(v["try_on"] == models.naive_utcnow().date().isoformat() for v in vids)

    # 1.3a: stubs inherit the dispatched playlist's bucket (immutable)
    assert all(v["bucket"] == "plbucket" for v in vids)
    chans = client.get("/things/", params={"type": "channel"}).json()
    assert chans and all(c["bucket"] == "plbucket" for c in chans)


def test_ingest_last_success_from_title(client):
    # API decides "enough to rate" from extracted fields: a titled stub is metadata-complete
    # (last_success_dt set); a title-less stub stays NULL and is claimable as a `meta` job.
    url = "http://example/pl/rate"
    tid, rid = _claimed_run(client, url)
    pl = models.PlaylistFull(
        # no playlist channel here: a fanned-out uploader channel is itself a claimable
        # container now, which would outrank the title-less video below — out of scope for
        # this title->last_success test (channel fan-out is covered by its own tests).
        url=url, native_id="plrate", title="Rate PL", extractor_key="youtube",
        playlist_count=2, channel=models.UlChan(),
        entries=[
            models.VidFull(native_id="hastitle", title="Has Title",
                           url="http://example/v/ht", extractor_key="youtube"),
            models.VidFull(native_id="notitle", url="http://example/v/nt",
                           extractor_key="youtube"),
        ])
    assert client.post(f"/jobs/{rid}/result",
                       json={"playlist": pl.model_dump(mode="json")}).status_code == 200
    vids = {v["native_id"]: v for v in client.get("/things/", params={"type": "video"}).json()}
    assert vids["hastitle"]["last_success_dt"]            # title present -> metadata-complete
    assert vids["notitle"]["last_success_dt"] is None     # title-less -> needs a meta job
    job = _claim(client)                                  # the title-less C video is next, as meta
    assert job["thing"]["id"] == vids["notitle"]["id"] and job["action"] == "meta"


def test_meta_result_fans_out_channel(client):
    # A full meta extract reveals the uploader a flat pull omitted -> channel thing + rel.
    v, rid = _claimed_meta(client, url="http://e/mc")
    client.post(f"/jobs/{rid}/result",
                json={"success": True,
                      "video": {"native_id": "mcv", "title": "MC", "extractor_key": "youtube",
                                "channel": {"url": "http://e/chan9", "title": "Chan 9"},
                                "info_json": {"id": "mcv"}}})
    related = client.get(f"/things/{v}", params={"include": "related"}).json()["related"]
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
    vids = client.get("/things/", params={"type": "video"}).json()
    assert vids and all(v["attrs"] == {"cookies": True, "lpm_lib": "lib7"} for v in vids)
    chans = client.get("/things/", params={"type": "channel"}).json()
    # channels carry only the kind hint (no propagated cookies/lpm_lib)
    assert chans and all(c["attrs"] == {"kind": "channel"} for c in chans)


def test_ingest_stores_info_json_hint(client):
    # Producer side: each entry's raw info dict lands as attrs.info_json on the video stub.
    url = "http://example/pl/infojson"
    tid, rid = _claimed_run(client, url)
    payload = _pl_payload(2, url=url, native="plij", info_json=True)
    assert client.post(f"/jobs/{rid}/result", json={"playlist": payload}).status_code == 200
    vids = client.get("/things/", params={"type": "video"}).json()
    assert vids
    for v in vids:
        info = v["attrs"]["info_json"]
        assert info["id"] == v["native_id"]
        assert "formats" in info
    chans = client.get("/things/", params={"type": "channel"}).json()
    assert chans and all(not (c["attrs"] or {}).get("info_json") for c in chans)


def test_ingest_refreshes_info_json_until_acquired(client):
    # Re-pull updates info_json while best_oi is NULL; an acquired video is left untouched.
    url = "http://example/pl/ijrefresh"
    tid, rid = _claimed_run(client, url)
    assert client.post(f"/jobs/{rid}/result",
                       json={"playlist": _pl_payload(2, url=url, native="plijr",
                                                     info_json=True)}).status_code == 200
    by_url = {v["url"]: v for v in client.get("/things/", params={"type": "video"}).json()}
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

    by_url = {v["url"]: v for v in client.get("/things/", params={"type": "video"}).json()}
    assert by_url["http://example/v/1"]["attrs"]["info_json"].get("refreshed") is True   # updated
    assert "refreshed" not in by_url["http://example/v/0"]["attrs"]["info_json"]          # frozen


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

    # the playlist's only parent edge is its uploader (channel=True)
    pl_parents = [e for e in client.get(f"/things/{tid}", params={"include": "related"})
                  .json()["related"] if e["direction"] == "parent"]
    assert len(pl_parents) == 1 and pl_parents[0]["channel"] is True

    # every video has exactly one uploader (channel=True) parent + its membership (False) parent
    for vid in client.get("/things/", params={"type": "video"}).json():
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
    chans = client.get("/things/", params={"type": "channel"}).json()
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

    related = client.get(f"/things/{tid}", params={"include": "related"}).json()["related"]
    kids = [e for e in related if e["direction"] == "child"]
    assert len(kids) == 4 and all(e["channel"] is True for e in kids)   # uploader/owner edges
    assert not any(e["direction"] == "parent" for e in related)         # no self channel edge

    # each direct video has exactly one parent edge, channel=True (no channel=False membership)
    for v in client.get("/things/", params={"type": "video"}).json():
        parents = client.get(f"/things/{v['id']}/related", params={"direction": "parent"}).json()
        assert len(parents) == 1 and parents[0]["channel"] is True

    # sub-playlists are container stubs, claimable -> a follow-up claim pulls one (recursion)
    subs = [e["thing"] for e in kids if e["thing"]["container"] is True]
    assert len(subs) == 2 and all(s["try_on"] for s in subs)
    job = _claim(client)
    assert job["action"] == "pull" and job["thing"]["id"] in {s["id"] for s in subs}


def test_unknown_url_discovered_as_video(client):
    # #153: an unknown URL (container=None) the pull resolves to a single video is sent as a
    # `video` body and classified as a leaf (container=False), then download/meta-eligible.
    tid, rid = _claimed_run(client, "http://e/unknown-vid")   # unknown, claimed as 'pull'
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True,
                          "video": {"native_id": "uv1", "title": "Surprise Video",
                                    "extractor_key": "youtube", "info_json": {"id": "uv1"}}})
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
    related = client.get(f"/things/{v}", params={"include": "related"}).json()["related"]
    chan = [e for e in related if e["channel"]]
    assert len(chan) == 1 and chan[0]["thing"]["url"] == "http://e/dchan"           # channel fanned out


def _claimed_meta(client, **kw):
    """Seed a C-band, under-described due video (last_success_dt NULL), claim it -> meta."""
    kw.setdefault("try_on", _TODAY)   # unrated -> C
    v = _seed_thing(type="video", **kw)
    job = _claim(client)
    assert job and job["thing"]["id"] == v and job["action"] == "meta"
    return v, job["run_id"]


def test_meta_result_enriches_without_acquiring(client):
    v, rid = _claimed_meta(client, url="http://e/m1")
    r = client.post(f"/jobs/{rid}/result",
                    json={"success": True,
                          "video": {"native_id": "mv1", "title": "Fetched Title",
                                    "extractor_key": "youtube",
                                    "info_json": {"id": "mv1", "description": "d"}}})
    assert r.status_code == 200
    t = client.get(f"/things/{v}").json()
    assert t["title"] == "Fetched Title"                 # NULL display backfilled from the fetch
    assert t["extractor_key"] == "youtube" and t["native_id"] == "mv1"
    assert t["attrs"]["info_json"]["description"] == "d"  # Stage-2 load-info hint stored
    assert t["best_oi"] is None                          # metadata only, NOT acquired
    assert t["last_success_dt"]                          # human-decision metadata now in hand
    assert t["last_failure_dt"] is None
    assert t["try_on"] is not None                       # backoff applied (not NULL: not acquired)
    # Now metadata-complete -> no further meta job is dispatched (Phase II hold).
    assert _claim(client) is None


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
