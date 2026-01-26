import datetime
import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine, Session, SQLModel

import lmdb.api as api
import lmdb.models as models


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

