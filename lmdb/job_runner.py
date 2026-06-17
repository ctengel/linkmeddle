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
                download: bool, use_cookies: bool = False,
                worker: str | None = None) -> dict:
    """Push a run's result to POST /jobs/{run_id}/result (one path for all job kinds).

    `info` is the sanitized yt-dlp output, or None when extraction failed. The body shape is
    derived from `info` itself — the server keys off which field is set (`playlist` vs `video`):

    - A container result (yt-dlp `_type == 'playlist'` or any `entries`) is extracted into the
      thin PlaylistFull (the Stage-1 fan-out body). Non-None info = success.
    - Otherwise the result is a single video, extracted into a thin VidFull (`video`) so the
      server enriches the stub identically (display + channel). This covers both an under-
      described C-band video's metadata-only enrichment and an unknown URL that resolved to a
      single video (the server classifies it as a leaf, #153). Success is `info is not None`.

    `download` adds the Stage-2 media outcome: the OI file UUID from info['oi_uuid'] (set by
    ObjIdxUploadPP) as `best_oi`, and success becomes `oi_uuid is not None`, NOT merely
    `info is not None` — _ydl uses ignoreerrors='only_download', so yt-dlp returns an info dict
    even when the media download failed (the upload PP then never ran, leaving no oi_uuid).
    `download` is only ever set for a single-video job, so it never collides with a playlist body.

    `data_json` carries the raw output (kept even on a failed download for debugging);
    `input_json` records the per-run cookies decision.
    """
    body: dict = {'worker': worker, 'input_json': {'cookies': use_cookies}}
    success = info is not None
    if info is not None:
        body['data_json'] = info
        if run_bknd.is_container(info):
            body['playlist'] = run_bknd.extract_pull(info).model_dump(mode="json")
        else:
            body['video'] = run_bknd.extract_pull_video(info).model_dump(mode="json")
            if download:
                oi_uuid = info.get('oi_uuid')
                success = oi_uuid is not None
                body['best_oi'] = str(oi_uuid) if oi_uuid else None
    body['success'] = success
    resp = requests.post(f"{api_base.rstrip('/')}/jobs/{run_id}/result",
                         json=body, timeout=RESULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def initiate_job(api_base: str, job: dict, worker: str) -> None:
    """Run one claimed job and report its result — one common path for every job kind.

    The only knob is `download`: when False (a container/unknown pull, or a C-band video the
    flat pull under-described) the worker fetches metadata only; when True (a video >= B) it
    also fetches media + uploads to OI (the download-only `oibucket`/`lpmlib`). The extract is
    always flat (`extract_flat='in_playlist'`) — a no-op on a single video, so a download still
    gets a full extract — letting one yt-dlp call serve both playlist enumeration and per-video
    fetch. `cookies` is the server's per-job suggestion (§4.7). A failed run (init_download
    returns None) posts success=False — fail-whole, no partial resume (§4.7).

    `attrs.info_json`, when present, is a pre-extracted yt-dlp info dict the worker downloads
    straight from (like `yt-dlp --load-info-json`) instead of re-extracting `thing.url`; it
    applies to all stages.
    """
    run_id, thing, download = job["run_id"], job["thing"], job["download"]
    cookies = job.get("cookies", False)
    attrs = thing.get("attrs") or {}
    info = run_bknd.init_download(
        thing["url"], download=download,
        oibucket=thing["bucket"] if download else None,
        lpmlib=attrs.get("lpm_lib") if download else None,
        use_cookies=cookies,
        flat=True,   # flat is a no-op on a single video, so a download still gets a full extract
        info_dict=attrs.get(xform.INFO_JSON_KEY))
    post_result(api_base, run_id, info, download=download,
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
