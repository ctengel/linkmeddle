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
    print(created)
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