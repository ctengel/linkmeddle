#!/usr/bin/env python3
"""Thin job runner (V4): pull one prioritized job, run it, report, loop (§4.5).

Replaces V3's "fetch all due schedules, random.shuffle, run in arbitrary order". The API
owns prioritization: the runner just asks `POST /jobs/claim` for the single top job, runs
it, pushes the result to `POST /jobs/{run_id}/result`, and asks again until nothing is due.
Single worker in 4.0; the claim endpoint's SKIP LOCKED makes it safe once a 2nd appears.
"""

# TODO allow specifying a given extractor
# TODO use models from lmdb.models where appropriate

import os
import socket
import warnings
import requests
from . import run_bknd, xform

LINKMEDDLE_PLAPI = os.environ.get("LINKMEDDLE_PLAPI", "http://localhost:29072/")
# TODO add pid
WORKER = os.environ.get("LM_WORKER", socket.gethostname())
CLAIM_TIMEOUT = 30
RESULT_TIMEOUT = 64  # large playlists make a big POST body (mirrors the old plugin)

# TODO prepare for bearer auth

def claim_job(api_base: str, worker: str) -> dict | None:
    """Claim the single highest-priority due job, or None when nothing is due (204)."""
    resp = requests.post(f"{api_base.rstrip('/')}/jobs/claim",
                         json={"worker": worker}, timeout=CLAIM_TIMEOUT)
    resp.raise_for_status()
    if resp.status_code == 204:
        return None
    # TODO model validation?
    return resp.json()


def post_result(api_base: str, run_id: str, info: dict | None, *,
                action: str, use_cookies: bool = False,
                worker: str | None = None) -> dict:
    """Push a run's result to POST /jobs/{run_id}/result (one path for all job kinds).

    `info` is the sanitized yt-dlp output, or None when extraction failed. `action` shapes the
    body (the server derives the kind from which body field is set — `playlist` vs `video`):

    - 'pull' (Stage-1 metadata): info is extracted into the thin PlaylistFull (the fan-out
      body) when it is a container; an unknown URL that resolves to a single video is sent as
      `video` instead so the server classifies it as a leaf (#153). Non-None info = success.
    - 'meta' / 'download' (Stage-2): both extract the full single-video metadata into a thin
      VidFull (`video`) so the server enriches the stub identically (display + channel). 'meta'
      stops there (no media, no OI; success is `info is not None`). 'download' additionally
      reads the OI file UUID from info['oi_uuid'] (set by ObjIdxUploadPP) as `best_oi`; its
      success is `oi_uuid is not None`, NOT merely `info is not None`: _ydl uses
      ignoreerrors='only_download', so yt-dlp returns an info dict even when the media
      download failed (the upload PP then never ran, leaving no oi_uuid).

    `data_json` carries the raw output (kept even on a failed download for debugging);
    `input_json` records the per-run cookies decision.
    """
    body: dict = {'worker': worker, 'input_json': {'cookies': use_cookies}}
    success = info is not None
    if info is not None:
        body['data_json'] = info
        body['extractor_key'] = (info.get('extractor') or '').lower() or None
        body['native_id'] = info.get('id')
        if action == 'pull':
            # A pull of a known container yields entries; a pull of an unknown thing
            # (container=None) may resolve to a single video -> send it as `video` so the
            # server classifies it as a leaf (container=False, the #153 correction).
            if info.get("_type") == "playlist" or info.get("entries"):
                body['playlist'] = run_bknd.extract_pull(info).model_dump(mode="json")
            else:
                body['video'] = run_bknd.extract_pull_video(info).model_dump(mode="json")
        else:  # 'download' or 'meta' -> single-video metadata (common); download adds best_oi
            body['video'] = run_bknd.extract_pull_video(info).model_dump(mode="json")
            if action == 'download':
                oi_uuid = info.get('oi_uuid')
                success = oi_uuid is not None
                body['best_oi'] = str(oi_uuid) if oi_uuid else None
    body['success'] = success
    resp = requests.post(f"{api_base.rstrip('/')}/jobs/{run_id}/result",
                         json=body, timeout=RESULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def initiate_job(api_base: str, job: dict, worker: str) -> None:
    """Run one claimed job and report its result — one common path for both job kinds.

    Three job kinds differ only in a few parameters: Stage-1 'pull' and Stage-2 'meta' both
    fetch metadata only (`download=False`); Stage-2 'download' also fetches media + uploads to
    OI (the download-only `oibucket`/`lpmlib`). `cookies` is the server's per-job suggestion
    (§4.7). A failed run (init_download returns None) posts success=False — fail-whole, no
    partial resume (§4.7).

    `attrs.info_json`, when present, is a pre-extracted yt-dlp info dict the worker downloads
    straight from (like `yt-dlp --load-info-json`) instead of re-extracting `thing.url`; it
    applies to all stages.
    """
    run_id, thing, action = job["run_id"], job["thing"], job["action"]
    if action not in ("pull", "download", "meta"):
        warnings.warn(f"Unknown job action {action!r}; skipping {thing.get('url')}")
        return
    download = action == "download"
    cookies = job.get("cookies", False)
    attrs = thing.get("attrs") or {}
    info = run_bknd.init_download(
        thing["url"], download=download,
        oibucket=thing["bucket"] if download else None,
        lpmlib=attrs.get("lpm_lib") if download else None,
        use_cookies=cookies,
        flat=action == "pull",   # flatten only the playlist pull; meta/download stay full
        info_dict=attrs.get(xform.INFO_JSON_KEY))
    post_result(api_base, run_id, info, action=action,
                use_cookies=cookies, worker=worker)


def main() -> int:
    # TODO style is like a cronjob then?
    """Pull-one-and-loop: claim -> run -> report, until nothing is due."""
    status = 0
    count = 0
    while True:
        job = claim_job(LINKMEDDLE_PLAPI, WORKER)
        if job is None:
            break
        count += 1
        try:
            initiate_job(LINKMEDDLE_PLAPI, job, WORKER)
        except Exception as exc:  # never let one job kill the loop
            warnings.warn(f"Job {job.get('run_id')} failed: {exc}")
            status = 1
    print(f"Ran {count} job(s); worker={WORKER}.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
