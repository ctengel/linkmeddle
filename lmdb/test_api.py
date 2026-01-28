"""pytest-based tests for lmdb.api FastAPI endpoints."""

import datetime
import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine, Session, SQLModel

from lmdb import api
from lmdb import models


@pytest.fixture
def client(tmp_path):
    # create a fresh sqlite database for tests (shared across threads)
    db_file = tmp_path / "test.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    # replace api engine and recreate tables
    api.engine = engine
    SQLModel.metadata.create_all(engine)

    def get_session_override():
        with Session(engine) as s:
            yield s

    api.app.dependency_overrides[api.get_session] = get_session_override
    with TestClient(api.app) as c:
        yield c


def _make_payload_for(model_cls):
    """Construct a minimal payload dict for a Pydantic/SQLModel class using model_fields."""
    payload = {}
    fields = getattr(model_cls, "model_fields", {})
    for name, info in fields.items():
        # skip if has default value
        if "default" in info and info["default"] is not None:
            continue
        # if not required, skip
        if not info.get("required", False):
            continue
        ann = info.get("annotation", str)
        ann_str = str(ann).lower()
        if "str" in ann_str or ann is str:
            payload[name] = f"test-{name}"
        elif "int" in ann_str or ann is int:
            payload[name] = 1
        elif "date" in ann_str:
            payload[name] = datetime.date.today().isoformat()
        elif "list" in ann_str:
            payload[name] = []
        elif "bool" in ann_str:
            payload[name] = False
        else:
            # fallback to string
            payload[name] = f"test-{name}"
    return payload


def test_list_playlist_sums_empty(client):
    r = client.get("/playlists/", params={"extractor": "x", "channel": "y"})
    assert r.status_code == 200
    assert r.json() == []


def test_list_playlist_scheds_empty(client):
    r = client.get("/schedules/")
    assert r.status_code == 200
    assert r.json() == []


def test_create_and_get_playlist_sched(client):
    #payload = _make_payload_for(models.PlaylistSchedBase)
    payload = models.PlaylistSchedBase(webpage_url="http://example/playlist").model_dump()
    # POST to create
    r = client.post("/schedules/", json=payload)
    assert r.status_code == 201
    created = r.json()
    assert isinstance(created, dict)
    assert created.get("webpage_url") == payload["webpage_url"]
    # Now GET by id
    sched_id = created.get("sched_id")
    assert sched_id is not None
    r2 = client.get(f"/schedules/{sched_id}")
    assert r2.status_code == 200
    got = r2.json()
    # response_model includes stats and summary keys
    assert "summary" in got
    assert "runs" in got


def test_get_playlist_sum_not_found(client):
    r = client.get("/playlists/http://no-such-playlist")
    assert r.status_code == 404


def test_get_video_not_found(client):
    r = client.get("/videos/yt/does-not-exist")
    assert r.status_code == 404


def test_get_playlist_sched_not_found(client):
    r = client.get("/schedules/999999")
    assert r.status_code == 404

def test_playlist_run(client):
    pl_url = "http://example/playlist"
    payload = models.PlaylistRunCreate(playlist=models.PlaylistFull(webpage_url=pl_url,
                                                                    extractor=models.DLPIE(extractor_key="yt",
                                                                                           extractor="yt"),
                                                                    channel=models.UlChan(channel_id="test-channel",
                                                                                          uploader_id="test-uploader",
                                                                                          uploader="Test Uploader",
                                                                                          channel_url="http://example/channel",
                                                                                          uploader_url="http://example/uploader"),
                                                                    entries=[],
                                                                    playlist_count=0))
    r = client.post("/playlist-run/", json=payload.model_dump())
    assert r.status_code == 200
    result = r.json()
    assert "summary" in result
    assert "schedule" in result
    assert "new_stats" in result
    assert result["summary"]["webpage_url"] == pl_url
    assert result["schedule"] is None
    assert result["new_stats"] is None
    # Now try with a schedule
    sched_payload = models.PlaylistSchedBase(extractor_id="yt",
                                            id="test-playlist",
                                            next_run=datetime.date.today(),
                                            freq_days=7,
                                            input_params="",
                                            webpage_url=pl_url)
    prep_sched_payload = sched_payload.model_dump()
    prep_sched_payload['next_run'] = prep_sched_payload['next_run'].isoformat()
    r2 = client.post("/schedules/", json=prep_sched_payload)
    assert r2.status_code == 201
    sched_created = r2.json()
    sched_id = sched_created.get("sched_id")
    assert sched_id is not None
    payload.schedule_id = sched_id
    r3 = client.post("/playlist-run/", json=payload.model_dump())
    assert r3.status_code == 200
    result2 = r3.json()
    assert "summary" in result2
    assert "schedule" in result2
    assert "new_stats" in result2
    assert result2["summary"]["webpage_url"] == pl_url
    assert result2["schedule"] is not None
    assert result2["schedule"]["sched_id"] == sched_id
    assert result2["new_stats"] is not None

def test_playlist_with_entries(client):
    pl_url = "http://example/playlist-with-entries"
    entries = []
    for i in range(3):
        video = models.VidFull(
            extractor=models.DLPIE(extractor_key="yt", extractor="yt"),
            id=f"video-{i}",
            title=f"Test Video {i}",
            webpage_url=f"http://example/video-{i}",
            upload_date=datetime.datetime.now(),
            channel=models.UlChan(
                channel_id="test-channel",
                uploader_id="test-uploader",
                uploader="Test Uploader",
                channel_url="http://example/channel",
                uploader_url="http://example/uploader"
            ),
            duration=300 + i * 60,
            description=f"This is a description for Test Video {i}.",
            categories=["Test", "Video"],
            ext="mp4",
            format="1080p",
            height=1080,
            is_live=False,
            language="en",
            thumbnail=f"http://example/video-{i}/thumbnail.jpg",
            n_entries=i,
            was_live=False,
            width=1920
        )
        entries.append(video)
    playlist = models.PlaylistFull(
        webpage_url=pl_url,
        extractor=models.DLPIE(extractor_key="yt", extractor="yt"),
        channel=models.UlChan(
            channel_id="test-channel",
            uploader_id="test-uploader",
            uploader="Test Uploader",
            channel_url="http://example/channel",
            uploader_url="http://example/uploader"
        ),
        entries=entries,
        playlist_count=len(entries)
    )
    payload = models.PlaylistRunCreate(playlist=playlist)
    payload_prep = payload.model_dump()
    # Convert datetime fields to isoformat strings
    for entry in payload_prep['playlist']['entries']:
        if 'upload_date' in entry and isinstance(entry['upload_date'], datetime.datetime):
            entry['upload_date'] = entry['upload_date'].isoformat()
    r = client.post("/playlist-run/", json=payload_prep)
    assert r.status_code == 200
    result = r.json()
    assert "summary" in result
    assert result["summary"]["webpage_url"] == pl_url
    assert len(result["summary"]["entries"]) == len(entries)
    for i, entry in enumerate(result["summary"]["entries"]):
        assert entry == entries[i].id
    video_id_to_get = entries[1].id
    r2 = client.get(f"/videos/yt/{video_id_to_get}")
    assert r2.status_code == 200
    video_got = r2.json()
    assert any(x['webpage_url'] == pl_url for x in video_got)

def test_sched_id_round_trip(client):
    # create two schedules with same playlist ID but different params
    pl_url = "http://example/playlist-for-sched-id"
    sched_payload1 = models.PlaylistSchedBase(
        extractor_id="yt",
        id="test-playlist-sched-id",
        next_run=datetime.date.today(),
        freq_days=3,
        input_params="",
        webpage_url=pl_url
    )
    prep_sched_payload1 = sched_payload1.model_dump()
    prep_sched_payload1['next_run'] = prep_sched_payload1['next_run'].isoformat()
    sched1 = client.post("/schedules/", json=prep_sched_payload1)
    assert sched1.status_code == 201
    sched_created = sched1.json()
    sched_id = sched_created.get("sched_id")
    assert sched_id is not None
    sched_payload2 = sched_payload1.model_copy()
    sched_payload2.use_cookies = True
    prep_sched_payload2 = sched_payload2.model_dump()
    prep_sched_payload2['next_run'] = prep_sched_payload2['next_run'].isoformat()
    sched2 = client.post("/schedules/", json=prep_sched_payload2)
    assert sched2.status_code == 201
    sched_created2 = sched2.json()
    sched_id2 = sched_created2.get("sched_id")
    assert sched_id2 is not None
    # get first schedule by id
    get_sched = client.get(f"/schedules/{sched_id}")
    assert get_sched.status_code == 200
    assert get_sched.json()["sched_id"] == sched_id
    assert get_sched.json()["webpage_url"] == pl_url
    # get all scheduled runs for today and ensure first is there
    get_all = client.get("/schedules/", params={"next_run": datetime.date.today().isoformat()})
    assert get_all.status_code == 200
    all_scheds = get_all.json()
    # NOTE this is where it fails today - no sched_id in output model
    filtered = [s for s in all_scheds if s['sched_id'] == sched_id]
    assert len(filtered) == 1
    # now let's simulate a run with the first schedule ID sent in
    playlist = models.PlaylistFull(
        webpage_url=pl_url,
        extractor=models.DLPIE(extractor_key="yt", extractor="yt"),
        channel=models.UlChan(
            channel_id="test-channel",
            uploader_id="test-uploader",
            uploader="Test Uploader",
            channel_url="http://example/channel",
            uploader_url="http://example/uploader"
        ),
        entries=[],
        playlist_count=0
    )
    run_payload = models.PlaylistRunCreate(playlist=playlist, schedule_id=sched_id)
    r = client.post("/playlist-run/", json=run_payload.model_dump())
    assert r.status_code == 200
    result = r.json()
    assert "summary" in result
    assert "schedule" in result
    assert result["schedule"] is not None
    assert result["schedule"]["sched_id"] == sched_id
    assert "new_stats" in result
    assert result["new_stats"] is not None
    stat_id = result["new_stats"]["stat_id"]
    # now get the schedule again and ensure the run with stats is there
    get_sched2 = client.get(f"/schedules/{sched_id}")
    assert get_sched2.status_code == 200
    sched2_got = get_sched2.json()
    assert len(sched2_got["runs"]) == 1
    run_got = sched2_got["runs"][0]
    assert run_got["stat_id"] == stat_id
