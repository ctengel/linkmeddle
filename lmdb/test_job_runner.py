"""Unit tests for the worker path: job_runner.initiate_job and run_bknd.init_download.

These are pure-logic tests (no DB, no network, no real yt-dlp) — they monkeypatch the
yt-dlp boundary and the result push, focusing on how the `attrs.info_json` hint flows
through to yt-dlp's load-info-json download path.
"""

import pytest
from lmdb import job_runner, run_bknd, models, xform


# --- job_runner.initiate_job: attrs.info_json forwarding -------------------------------

def _capture_init_download(monkeypatch):
    """Stub run_bknd.init_download + post_result; return the captured init_download kwargs."""
    calls = {}

    def fake_init_download(url, **kwargs):
        calls['url'] = url
        calls.update(kwargs)
        return {'fake': 'info'}

    monkeypatch.setattr(job_runner.run_bknd, "init_download", fake_init_download)
    monkeypatch.setattr(job_runner, "post_result", lambda *a, **k: {})
    return calls


@pytest.mark.parametrize("action", ["pull", "download", "meta"])
def test_initiate_job_forwards_info_json_both_stages(monkeypatch, action):
    calls = _capture_init_download(monkeypatch)
    payload = {"id": "abc", "webpage_url": "https://example.com/v/abc"}
    job = {"run_id": "r1", "action": action,
           "thing": {"url": "https://example.com/v/abc", "bucket": "b",
                     "attrs": {"info_json": payload}}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls["info_dict"] == payload


def test_initiate_job_meta_is_metadata_only(monkeypatch):
    # A 'meta' job fetches metadata only — like 'pull', no media download / OI bucket.
    calls = _capture_init_download(monkeypatch)
    job = {"run_id": "r1", "action": "meta",
           "thing": {"url": "https://example.com/v/c", "bucket": "b", "attrs": None}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls["download"] is False and calls["oibucket"] is None


@pytest.mark.parametrize("action,expected_flat",
                         [("pull", True), ("meta", False), ("download", False)])
def test_initiate_job_flat_only_for_pull(monkeypatch, action, expected_flat):
    # Only the playlist pull flattens; meta/download need a full single-video extract.
    calls = _capture_init_download(monkeypatch)
    job = {"run_id": "r1", "action": action,
           "thing": {"url": "https://example.com/x", "bucket": "b", "attrs": None}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls["flat"] is expected_flat


class _Resp:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return {}


def test_post_result_meta_sends_video(monkeypatch):
    # A meta result posts a single PullVid `video` body — not a `playlist`, no `best_oi`.
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "vid9", "webpage_url": "https://e/v/9", "extractor": "YouTube",
            "title": "T", "thumbnail": "th", "description": "d"}
    job_runner.post_result("http://api/", "r9", info, action="meta")
    body = captured["body"]
    assert "video" in body and "playlist" not in body and "best_oi" not in body
    assert body["video"]["native_id"] == "vid9" and body["success"] is True


def test_post_result_download_sends_video_and_best_oi(monkeypatch):
    # A download forwards the same `video` metadata as meta (so the server enriches identically)
    # plus the OI uuid; success is gated on the upload (oi_uuid), not just extraction.
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "vid9", "webpage_url": "https://e/v/9", "extractor": "YouTube",
            "title": "T", "oi_uuid": "11111111-1111-1111-1111-111111111111"}
    job_runner.post_result("http://api/", "r9", info, action="download")
    body = captured["body"]
    assert "video" in body and "playlist" not in body
    assert body["video"]["native_id"] == "vid9"
    assert body["best_oi"] == "11111111-1111-1111-1111-111111111111" and body["success"] is True


def test_extract_pull_video_matches_entry_mapping():
    # extract_pull_video maps a single-video info dict like extract_pull's per-entry path.
    info = {"id": "vidx", "webpage_url": "https://e/v/x", "extractor": "YouTube",
            "title": "X", "thumbnail": "th"}
    vid = run_bknd.extract_pull_video(info)
    assert vid.native_id == "vidx" and vid.url == "https://e/v/x"
    assert vid.extractor_key == "youtube" and vid.title == "X"
    assert vid.info_json["id"] == "vidx"


def test_initiate_job_info_json_absent_is_none(monkeypatch):
    calls = _capture_init_download(monkeypatch)
    job = {"run_id": "r1", "action": "download",
           "thing": {"url": "https://example.com/v/abc", "bucket": "b", "attrs": None}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls["info_dict"] is None


# --- run_bknd.init_download: process_ie_result vs extract_info branch -------------------

class _FakeYDL:
    """Minimal stand-in for yt_dlp.YoutubeDL covering the calls init_download makes."""
    def __init__(self):
        self.params = {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_post_processor(self, pp):
        self.calls.append(("add_pp", pp))

    def extract_info(self, url, download=True):
        self.calls.append(("extract_info", url, download))
        return {"id": "from_url"}

    def process_ie_result(self, info, download=True, extra_info=None):
        self.calls.append(("process_ie_result", info, download))
        return info

    def sanitize_info(self, info, *a):
        return info


@pytest.fixture
def fake_ydl(monkeypatch):
    ydl = _FakeYDL()
    monkeypatch.setattr(run_bknd, "_ydl", lambda **kwargs: ydl)
    monkeypatch.setattr(run_bknd.time, "sleep", lambda *_: None)
    return ydl


def test_init_download_uses_process_ie_result_when_info_dict(fake_ydl):
    payload = {"id": "abc", "webpage_url": "https://x/v/abc"}
    info = run_bknd.init_download("https://x/v/abc", download=False, info_dict=payload)
    names = [c[0] for c in fake_ydl.calls]
    assert "process_ie_result" in names
    assert "extract_info" not in names
    assert info == payload


def test_init_download_uses_extract_info_without_info_dict(fake_ydl):
    run_bknd.init_download("https://x/v/abc", download=False)
    names = [c[0] for c in fake_ydl.calls]
    assert "extract_info" in names
    assert "process_ie_result" not in names


def test_init_download_threads_flat_into_ydl(monkeypatch):
    captured = {}
    monkeypatch.setattr(run_bknd, "_ydl",
                        lambda **kw: captured.update(kw) or _FakeYDL())
    monkeypatch.setattr(run_bknd.time, "sleep", lambda *_: None)
    run_bknd.init_download("https://x/pl", download=False, flat=True)
    assert captured["extract_flat"] is True
    captured.clear()
    run_bknd.init_download("https://x/v", download=False, flat=False)
    assert captured["extract_flat"] is False


def test_ydl_extract_flat_opt():
    assert run_bknd._ydl(extract_flat=True).params.get("extract_flat") == "in_playlist"
    assert not run_bknd._ydl().params.get("extract_flat")


def test_extract_pull_handles_flat_entries():
    # extract_flat='in_playlist' entries carry `url`/`ie_key` (not webpage_url/extractor).
    flat_pl = {"webpage_url": "https://x/pl", "id": "pl", "extractor_key": "YouTube",
               "entries": [{"_type": "url", "ie_key": "Youtube", "id": "v1",
                            "url": "https://x/v/1", "title": "V1"}]}
    v = run_bknd.extract_pull(flat_pl).entries[0]
    assert v.native_id == "v1" and v.url == "https://x/v/1"
    assert v.extractor_key == "youtube" and v.title == "V1"


# --- producer: raw entry captured -> PullVid.info_json -> thing.attrs -------------------

def _raw_playlist(n=2):
    """A raw yt-dlp playlist info dict (as extract_info would return it)."""
    return {
        "id": "pl1", "title": "PL", "webpage_url": "https://x/pl/1",
        "_type": "playlist",
        "epoch": 1700000000,
        "extractor_key": "YouTube", "extractor": "youtube",
        "_version": {"version": "2026", "release_git_head": "abc", "repository": "yt-dlp"},
        "entries": [
            {"id": f"vid{i}", "title": f"V{i}", "webpage_url": f"https://x/v/{i}",
             "extractor_key": "YouTube", "extractor": "youtube",
             # extra raw keys that PullVid doesn't model but the download needs:
             "formats": [{"format_id": "best", "url": f"https://cdn/{i}.mp4"}],
             "bogus_extra": i}
            for i in range(n)],
    }


def test_extract_pull_captures_raw_entry_into_info_json():
    raw = _raw_playlist(2)
    pl = run_bknd.extract_pull(raw)
    assert pl.extractor_key == "youtube"        # normalized from "YouTube"
    by_id = {v.native_id: v for v in pl.entries}
    for raw_entry in raw["entries"]:
        vid = by_id[raw_entry["id"]]
        assert vid.extractor_key == "youtube"   # normalized
        captured = vid.info_json
        assert captured == raw_entry            # faithful, incl. formats + bogus_extra
        assert "info_json" not in captured      # not self-nested


def test_pl_full2things_puts_info_json_in_video_attrs():
    raw = _raw_playlist(2)
    pl = run_bknd.extract_pull(raw)
    graph = xform.pl_full2things(pl, bucket="b", parent_attrs={"cookies": True})
    for vid in graph.videos:
        assert vid.attrs["cookies"] is True                     # propagated hint preserved
        assert vid.attrs[xform.INFO_JSON_KEY]["id"] == vid.native_id
        assert "formats" in vid.attrs[xform.INFO_JSON_KEY]
