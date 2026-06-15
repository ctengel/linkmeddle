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
from . import run_bknd, models, xform

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


def _playlist_full_json(pl: models.PlaylistFull) -> dict:
    """Serialize an LM-native playlist to JSON-safe dict (datetimes -> ISO)."""
    body = pl.model_dump()
    body['modified_date'] = body['modified_date'].isoformat() if body['modified_date'] else None
    for entry in body['entries']:
        entry['upload_date'] = entry['upload_date'].isoformat() if entry['upload_date'] else None
    return body


def post_result(api_base: str, run_id: str, info: dict | None, *,
                download: bool, use_cookies: bool = False,
                worker: str | None = None) -> dict:
    """Push a run's result to POST /jobs/{run_id}/result (one path for both job kinds).

    `info` is the sanitized yt-dlp output, or None when extraction failed. `download`
    distinguishes the two kinds (it's the same boolean that drove init_download — the server
    itself re-derives the kind from thing.type, so no separate 'action' is sent):

    - download=False (Stage-1 pull): info is converted DLP -> PlaylistFull (the fan-out
      body); a non-None info means success (extraction failure already surfaced as None).
    - download=True (Stage-2 download): the OI file UUID is read from info['oi_uuid'] (set by
      ObjIdxUploadPP) as `best_oi`, plus extractor/id for identity backfill. Success is
      `oi_uuid is not None`, NOT merely `info is not None`: _ydl uses
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
        if download:
            oi_uuid = info.get('oi_uuid')
            success = oi_uuid is not None
            body['best_oi'] = str(oi_uuid) if oi_uuid else None
        else:
            pl = xform.pl_dlp2lm(models.PlaylistDLP.model_validate(info))
            body['playlist'] = _playlist_full_json(pl)
    body['success'] = success
    resp = requests.post(f"{api_base.rstrip('/')}/jobs/{run_id}/result",
                         json=body, timeout=RESULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def initiate_job(api_base: str, job: dict, worker: str) -> None:
    """Run one claimed job and report its result — one common path for both job kinds.

    Stage-1 'pull' (metadata only) and Stage-2 'download' (real download + OI upload) differ
    only in a few parameters: `download`, and the download-only `oibucket`/`lpmlib`. `cookies`
    is the server's per-job suggestion (§4.7). A failed run (init_download returns None) posts
    success=False — fail-whole, no partial resume (§4.7).
    """
    run_id, thing, action = job["run_id"], job["thing"], job["action"]
    if action not in ("pull", "download"):
        warnings.warn(f"Unknown job action {action!r}; skipping {thing.get('url')}")
        return
    download = action == "download"
    cookies = job.get("cookies", False)
    info = run_bknd.init_download(
        thing["url"], download=download,
        oibucket=thing["bucket"] if download else None,
        lpmlib=(thing.get("attrs") or {}).get("lpm_lib") if download else None,
        use_cookies=cookies)
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
