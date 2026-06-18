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


@pytest.mark.parametrize("download", [False, True])
def test_initiate_job_forwards_info_json_both_stages(monkeypatch, download):
    calls = _capture_init_download(monkeypatch)
    payload = {"id": "abc", "webpage_url": "https://example.com/v/abc"}
    job = {"run_id": "r1", "download": download,
           "thing": {"id": "t1", "url": "https://example.com/v/abc", "bucket": "b",
                     "attrs": {"info_json": payload}}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls["info_dict"] == payload


def test_initiate_job_metadata_only_when_not_download(monkeypatch):
    # A non-download job (container pull or C-band video enrich) fetches metadata only — no
    # media download / OI bucket.
    calls = _capture_init_download(monkeypatch)
    job = {"run_id": "r1", "download": False,
           "thing": {"id": "t1", "url": "https://example.com/v/c", "bucket": "b", "attrs": None}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls["download"] is False and calls["oibucket"] is None


@pytest.mark.parametrize("download", [False, True])
def test_initiate_job_always_flat(monkeypatch, download):
    # One extraction mode for every job: always flat. It is a no-op on a single video, so a
    # download still gets a full extract while a container pull is enumerated cheaply.
    calls = _capture_init_download(monkeypatch)
    job = {"run_id": "r1", "download": download,
           "thing": {"id": "t1", "url": "https://example.com/x", "bucket": "b", "attrs": None}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls["flat"] is True


class _Resp:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return {}


def test_post_result_metadata_only_sends_video(monkeypatch):
    # A non-download single-video result posts a `video` body — not a `playlist`, no `best_oi`.
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "vid9", "webpage_url": "https://e/v/9", "extractor": "YouTube",
            "title": "T", "thumbnail": "th", "description": "d"}
    job_runner.post_result("http://api/", "r9", info, download=False)
    body = captured["body"]
    assert "video" in body and "playlist" not in body and "best_oi" not in body
    assert body["video"]["native_id"] == "vid9" and body["success"] is True


def test_post_result_download_sends_video_and_best_oi(monkeypatch):
    # A download forwards the same `video` metadata as a metadata-only run (so the server
    # enriches identically) plus the OI uuid; success is gated on the upload (oi_uuid), not
    # just extraction.
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "vid9", "webpage_url": "https://e/v/9", "extractor": "YouTube",
            "title": "T", "oi_uuid": "11111111-1111-1111-1111-111111111111"}
    job_runner.post_result("http://api/", "r9", info, download=True)
    body = captured["body"]
    assert "video" in body and "playlist" not in body
    assert body["video"]["native_id"] == "vid9"
    assert body["best_oi"] == "11111111-1111-1111-1111-111111111111" and body["success"] is True


def test_extract_pull_video_matches_entry_mapping():
    # extract_pull_video maps a single-video info dict like extract_pull's per-entry path.
    info = {"id": "vidx", "webpage_url": "https://e/v/x", "extractor_key": "Youtube",
            "extractor": "youtube", "title": "X", "thumbnail": "th"}
    vid = run_bknd.extract_pull_video(info)
    assert vid.native_id == "vidx" and vid.url == "https://e/v/x"
    assert vid.extractor_key == "youtube" and vid.title == "X"
    assert vid.info_json["id"] == "vidx"


def test_norm_extractor_matches_download_archive_convention():
    # The stored extractor_key must be the yt-dlp download-archive id (IE class key, lowercased):
    # `extractor_key` -> flat-entry `ie_key`, and `ie_key` must win over the IE_NAME `extractor`
    # (e.g. "YoutubeTab" -> "youtubetab", not "youtube:tab").
    assert run_bknd._norm_extractor({"extractor_key": "YoutubeTab",
                                     "extractor": "youtube:tab"}) == "youtubetab"
    assert run_bknd._norm_extractor({"ie_key": "YoutubeTab",
                                     "extractor": "youtube:tab"}) == "youtubetab"
    assert run_bknd._norm_extractor({"extractor": "youtube:tab"}) == "youtube:tab"
    assert run_bknd._norm_extractor({}) is None


def test_initiate_job_forwards_ids_on_download(monkeypatch):
    # On a download job, run_id and thing_id are forwarded so the OI object gets lm-* tags.
    calls = _capture_init_download(monkeypatch)
    import uuid
    rid = uuid.uuid4()
    tid = uuid.uuid4()
    job = {"run_id": rid, "download": True,
           "thing": {"id": tid, "url": "https://example.com/v/x", "bucket": "b", "attrs": None}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls["run_id"] == str(rid)
    assert calls["thing_id"] == str(tid)


def test_initiate_job_no_ids_on_non_download(monkeypatch):
    # Non-download jobs don't produce an OI object, so no IDs are forwarded.
    calls = _capture_init_download(monkeypatch)
    job = {"run_id": "r1", "download": False,
           "thing": {"id": "t1", "url": "https://example.com/v/x", "bucket": "b", "attrs": None}}
    job_runner.initiate_job("http://api/", job, "w")
    assert calls.get("run_id") is None
    assert calls.get("thing_id") is None


def test_initiate_job_info_json_absent_is_none(monkeypatch):
    calls = _capture_init_download(monkeypatch)
    job = {"run_id": "r1", "download": True,
           "thing": {"id": "t1", "url": "https://example.com/v/abc", "bucket": "b", "attrs": None}}
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


# --- main(): a raising job is reported as a failure, not just logged --------------------

def test_main_reports_failure_on_raising_job(monkeypatch):
    # A job that raises while running must be reported as a failure (info=None -> success=False)
    # so the run is finalized and the thing backs off; otherwise it is re-claimed forever.
    jobs = iter([{"run_id": "rX", "download": False, "cookies": False,
                  "thing": {"id": "tX", "url": "u", "bucket": "b", "attrs": None}}])
    monkeypatch.setattr(job_runner, "claim_job", lambda *a, **k: next(jobs, None))

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(job_runner, "initiate_job", boom)

    reported = {}
    monkeypatch.setattr(job_runner, "post_result",
                        lambda api, run_id, info, **k: reported.update(run_id=run_id, info=info))
    rc = job_runner.main()
    assert rc == 1
    assert reported["run_id"] == "rX" and reported["info"] is None  # posted a failure


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


def test_ydl_noplaylist_opt():
    # #164: a download fetch sets noplaylist so a list URL resolves to its single leaf.
    assert run_bknd._ydl(noplaylist=True).params.get("noplaylist") is True
    assert not run_bknd._ydl().params.get("noplaylist")


def test_init_download_threads_noplaylist_on_download(monkeypatch):
    # #164: only a Stage-2 acquire (download=True) sets noplaylist; pulls/meta leave it off so
    # container enumeration still works.
    captured = {}
    monkeypatch.setattr(run_bknd, "_ydl",
                        lambda **kw: captured.update(kw) or _FakeYDL())
    monkeypatch.setattr(run_bknd.time, "sleep", lambda *_: None)
    monkeypatch.setenv("OBJIDX_URL", "http://oi/")
    monkeypatch.setenv("OBJIDX_AUTH", "user")
    monkeypatch.setattr(run_bknd.ytdl_arch_oi, "ObjIdxDlArch", lambda **kw: None)
    monkeypatch.setattr(run_bknd.oic, "get_obj_idx_env", lambda: None)
    run_bknd.init_download("https://x/v", download=True)  # no oibucket -> skips upload PP
    assert captured["noplaylist"] is True
    captured.clear()
    run_bknd.init_download("https://x/pl", download=False, flat=True)
    assert captured["noplaylist"] is False


def test_extract_pull_handles_flat_entries():
    # extract_flat='in_playlist' entries carry `url`/`ie_key` (not webpage_url/extractor).
    flat_pl = {"webpage_url": "https://x/pl", "id": "pl", "extractor_key": "YouTube",
               "entries": [{"_type": "url", "ie_key": "Youtube", "id": "v1",
                            "url": "https://x/v/1", "title": "V1"}]}
    v = run_bknd.extract_pull(flat_pl).entries[0]
    assert v.native_id == "v1" and v.url == "https://x/v/1"
    assert v.extractor_key == "youtube" and v.title == "V1"


def test_extract_pull_url_result_marks_unknown_container():
    # A flat url-result is only a URL pointer. A container/playlist-like one is never a leaf
    # (container=None, classified by its own later pull). A plain video url-result is either a
    # known leaf (container=False) or, conservatively, ambiguous (container=None) depending on
    # whether the ie_key leaf heuristic is enabled (see _is_container_ie_key's TODO).
    raw = {"webpage_url": "https://x/chan", "id": "chan", "extractor_key": "YouTube",
           "entries": [
               {"_type": "url", "ie_key": "Youtube", "id": "v1",
                "url": "https://x/v/1", "title": "V1"},
               {"_type": "url", "ie_key": "YoutubeTab", "id": "tab1",
                "url": "https://x/chan/videos", "title": "Videos"},
           ]}
    by_id = {v.native_id: v for v in run_bknd.extract_pull(raw).entries}
    assert by_id["v1"].container in (None, False)   # video url-result: leaf or (conservatively) unknown
    assert by_id["tab1"].container is None          # container ie_key -> unknown, classified later


def test_extract_pull_splits_videos_and_child_playlists():
    # A channel's flat pull lists both videos and playlist-typed entries (tabs/sub-playlists):
    # videos -> entries, playlist entries -> child_playlists stubs (not flattened).
    raw = {"webpage_url": "https://x/chan", "id": "chan", "extractor_key": "YouTube",
           "entries": [
               {"_type": "url", "ie_key": "Youtube", "id": "v1",
                "url": "https://x/v/1", "title": "V1"},
               {"_type": "playlist", "ie_key": "Youtube", "id": "subpl",
                "url": "https://x/pl/sub", "title": "Sub PL"},
           ]}
    pl = run_bknd.extract_pull(raw)
    assert [v.native_id for v in pl.entries] == ["v1"]
    assert [c.native_id for c in pl.child_playlists] == ["subpl"]
    assert pl.child_playlists[0].url == "https://x/pl/sub"


def test_post_result_single_video_discovers_leaf(monkeypatch):
    # #153: a non-download pull that resolves to a single video (no entries) is posted as
    # `video`, not `playlist`, so the server classifies the unknown thing as a leaf
    # (container=False).
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "solo", "webpage_url": "https://e/v/solo", "extractor": "YouTube",
            "title": "Solo"}
    job_runner.post_result("http://api/", "r", info, download=False)
    body = captured["body"]
    assert "video" in body and "playlist" not in body
    assert body["video"]["native_id"] == "solo"


def test_post_result_container_sends_playlist(monkeypatch):
    # A result that resolves to a playlist/channel (has entries) is posted as `playlist`.
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "pl", "webpage_url": "https://e/pl", "extractor": "YouTube",
            "_type": "playlist",
            "entries": [{"_type": "url", "ie_key": "Youtube", "id": "v1",
                         "url": "https://e/v/1", "title": "V1"}]}
    job_runner.post_result("http://api/", "r", info, download=False)
    body = captured["body"]
    assert "playlist" in body and "video" not in body


def test_is_both_detects_video_plus_entries():
    # #164: entries coexisting with top-level media (formats / oi_uuid / requested_downloads)
    # is the ambiguous "both" shape; entries-only or media-only is not.
    assert run_bknd.is_both({"entries": [{"id": "v"}], "formats": [{"url": "u"}]}) is True
    assert run_bknd.is_both({"_type": "playlist", "entries": [],
                             "oi_uuid": "11111111-1111-1111-1111-111111111111"}) is True
    assert run_bknd.is_both({"entries": [{"id": "v"}], "requested_downloads": [{}]}) is True
    assert run_bknd.is_both({"_type": "playlist", "entries": [{"id": "v"}]}) is False  # no media
    assert run_bknd.is_both({"id": "v", "formats": [{"url": "u"}]}) is False            # no entries


def test_post_result_both_shape_is_failure(monkeypatch):
    # #164: a result that is both a video and a playlist can't be classified -> report a failure
    # (no body) while keeping data_json for inspection, rather than silently sending a playlist.
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "vid", "webpage_url": "https://e/v", "extractor": "YouTube", "title": "T",
            "entries": [{"_type": "url", "id": "v1", "url": "https://e/v/1"}],
            "oi_uuid": "11111111-1111-1111-1111-111111111111"}
    job_runner.post_result("http://api/", "r", info, download=True)
    body = captured["body"]
    assert body["success"] is False
    assert "playlist" not in body and "video" not in body and "best_oi" not in body
    assert body["data_json"] == info


def test_post_result_empty_playlist_no_type_sends_playlist(monkeypatch):
    # Regression: entries=[] is falsy, so a naive `or info.get("entries")` check sends
    # `video` instead of `playlist`, mis-classifying the container. _type absent here to
    # exercise the fallback arm specifically.
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "empty", "webpage_url": "https://e/empty", "extractor": "YouTube",
            "entries": []}
    job_runner.post_result("http://api/", "r", info, download=False)
    body = captured["body"]
    assert "playlist" in body and "video" not in body


def test_post_result_empty_playlist_with_type_sends_playlist(monkeypatch):
    # Empty playlist with _type: "playlist" — _type arm should take precedence and send playlist.
    captured = {}
    monkeypatch.setattr(job_runner.requests, "post",
                        lambda url, json=None, timeout=None:
                            captured.update(body=json) or _Resp())
    info = {"id": "emptyt", "webpage_url": "https://e/emptyt", "extractor": "YouTube",
            "_type": "playlist", "entries": []}
    job_runner.post_result("http://api/", "r", info, download=False)
    body = captured["body"]
    assert "playlist" in body and "video" not in body


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
