"""BFF tests — currently the thumbnail cache endpoint (GET /things/{id}/thumb).

The lmdb backend and the upstream image host are both mocked with respx; the
TestClient's own ASGI traffic uses httpx's ASGITransport, which respx does not
intercept, so no pass-through routes are needed.
"""
import os
import uuid
import pytest
import respx
import httpx
from fastapi.testclient import TestClient
from lmfe import api as fe_api

THING_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
THUMB_URL = "http://img.test/v1/thumb.jpg"
JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"


@pytest.fixture(name="client")
def client_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(fe_api, "THUMB_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(fe_api, "LINKMEDDLE_PLAPI", "http://lmdb.test/")
    return TestClient(fe_api.app)


def _mock_thing(router, thumbnail_url):
    return router.get(f"http://lmdb.test/things/{THING_ID}").respond(
        json={"id": str(THING_ID), "bucket": "b", "thumbnail_url": thumbnail_url})


@respx.mock
def test_thumb_fetch_cache_and_hit(client):
    thing_route = _mock_thing(respx, THUMB_URL)
    img_route = respx.get(THUMB_URL).respond(
        content=JPEG, headers={"content-type": "image/jpeg"})

    resp = client.get(f"/things/{THING_ID}/thumb")
    assert resp.status_code == 200
    assert resp.content == JPEG
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "public, max-age=86400"
    assert os.path.exists(os.path.join(fe_api.THUMB_DIR, f"{THING_ID}.jpg"))
    assert thing_route.call_count == 1 and img_route.call_count == 1

    # Second request is a pure cache hit: served off disk, zero outbound calls.
    resp = client.get(f"/things/{THING_ID}/thumb")
    assert resp.status_code == 200 and resp.content == JPEG
    assert thing_route.call_count == 1 and img_route.call_count == 1


@respx.mock
def test_thumb_head_matches_get(client):
    _mock_thing(respx, THUMB_URL)
    respx.get(THUMB_URL).respond(content=JPEG, headers={"content-type": "image/jpeg"})
    get_resp = client.get(f"/things/{THING_ID}/thumb")
    head_resp = client.head(f"/things/{THING_ID}/thumb")
    assert head_resp.status_code == 200
    assert head_resp.content == b""
    for header in ("content-type", "cache-control", "content-length", "etag"):
        assert head_resp.headers[header] == get_resp.headers[header]


@respx.mock
def test_thumb_null_url_404(client):
    _mock_thing(respx, None)
    resp = client.get(f"/things/{THING_ID}/thumb")
    assert resp.status_code == 404


@respx.mock
def test_thumb_unknown_thing_mirrored(client):
    respx.get(f"http://lmdb.test/things/{THING_ID}").respond(
        status_code=404, json={"detail": "Thing not found"})
    resp = client.get(f"/things/{THING_ID}/thumb")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Thing not found"


@pytest.mark.parametrize("upstream", [
    dict(status_code=404),
    dict(content=b"<html>not an image</html>", headers={"content-type": "text/html"}),
    dict(side_effect=httpx.ConnectError("boom")),
])
@respx.mock
def test_thumb_bad_upstream_404_not_cached(client, upstream):
    _mock_thing(respx, THUMB_URL)
    route = respx.get(THUMB_URL)
    side_effect = upstream.pop("side_effect", None)
    if side_effect:
        route.side_effect = side_effect
    else:
        route.respond(**upstream)
    resp = client.get(f"/things/{THING_ID}/thumb")
    assert resp.status_code == 404
    assert not os.path.exists(fe_api.THUMB_DIR) or not os.listdir(fe_api.THUMB_DIR)
