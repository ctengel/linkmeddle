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
    """Run one claimed job and report its result — one common path for both job kinds.

    Stage-1 'pull' (metadata only) and Stage-2 'download' (real download + OI upload) differ
    only in a few parameters: `download`, and the download-only `oibucket`/`lpmlib`. `cookies`
    is the server's per-job suggestion (§4.7). A failed run (extract returns None) posts
    success=False — fail-whole, no partial resume (§4.7).
    """
    run_id, thing, action = job["run_id"], job["thing"], job["action"]
    if action not in ("pull", "download"):
        warnings.warn(f"Unknown job action {action!r}; skipping {thing.get('url')}")
        return
    download = action == "download"
    cookies = job.get("cookies", False)
    info = run_bknd.extract_info(
        thing["url"], download=download,
        oibucket=thing["bucket"] if download else None,
        lpmlib=(thing.get("attrs") or {}).get("lpm_lib") if download else None,
        use_cookies=cookies)
    run_bknd.post_result(api_base, run_id, info, action=action,
                         use_cookies=cookies, worker=worker)


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
