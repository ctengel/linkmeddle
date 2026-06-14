#!/usr/bin/env python3
"""Thin job runner (V4): pull one prioritized job, run it, report, loop (§4.5).

Replaces V3's "fetch all due schedules, random.shuffle, run in arbitrary order". The API
owns prioritization: the runner just asks `POST /jobs/claim` for the single top job, runs
it, pushes the result to `POST /jobs/{run_id}/result`, and asks again until nothing is due.
Single worker in 4.0; the claim endpoint's SKIP LOCKED makes it safe once a 2nd appears.
"""

import os
import socket
import warnings
import requests
from yt_dlp.utils import YoutubeDLError
from . import run_bknd

LINKMEDDLE_PLAPI = os.environ.get("LINKMEDDLE_PLAPI", "http://localhost:29072/")
WORKER = os.environ.get("LM_WORKER", socket.gethostname())
CLAIM_TIMEOUT = 30


def claim_job(api_base: str, worker: str) -> dict | None:
    """Claim the single highest-priority due job, or None when nothing is due (204)."""
    resp = requests.post(f"{api_base.rstrip('/')}/jobs/claim",
                         json={"worker": worker}, timeout=CLAIM_TIMEOUT)
    resp.raise_for_status()
    if resp.status_code == 204:
        return None
    return resp.json()


def run_job(api_base: str, job: dict, worker: str) -> None:
    """Run one claimed job and report its result back to the API."""
    run_id, thing, action = job["run_id"], job["thing"], job["action"]
    if action == "pull":
        # Stage-1 playlist metadata pull; fail whole on any error (§4.7).
        try:
            info = run_bknd.pull_playlist(thing["url"])
        except YoutubeDLError as exc:
            warnings.warn(f"Stage-1 pull failed for {thing['url']}: {exc}")
            run_bknd.post_run_result(api_base, run_id, None, success=False, worker=worker)
            return
        run_bknd.post_run_result(api_base, run_id, info, success=True, worker=worker)
    elif action == "download":
        # TODO(1.3): Stage-2 per-video download (init_download(maybe_playlist=False) + OI
        # upload; result sets best_oi, try_on=NULL). Until then, don't crash the loop.
        warnings.warn(f"Stage-2 download not implemented yet (1.3); skipping {thing['url']}")
        run_bknd.post_run_result(api_base, run_id, None, success=False, worker=worker)
    else:
        warnings.warn(f"Unknown job action {action!r}; skipping {thing.get('url')}")


def main() -> int:
    """Pull-one-and-loop: claim -> run -> report, until nothing is due."""
    status = 0
    count = 0
    while True:
        job = claim_job(LINKMEDDLE_PLAPI, WORKER)
        if job is None:
            break
        count += 1
        try:
            run_job(LINKMEDDLE_PLAPI, job, WORKER)
        except Exception as exc:  # never let one job kill the loop
            warnings.warn(f"Job {job.get('run_id')} failed: {exc}")
            status = 1
    print(f"Ran {count} job(s); worker={WORKER}.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
