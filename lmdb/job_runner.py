#!/usr/bin/env python3
"""Thin job runner (V4): pull one prioritized job, run it, report, loop (§4.5).

Replaces V3's "fetch all due schedules, random.shuffle, run in arbitrary order". The API
owns prioritization: the runner just asks `POST /jobs/claim` for the single top job, runs
it, pushes the result to `POST /jobs/{run_id}/result`, and asks again until nothing is due.
Single worker in 4.0; the claim endpoint's SKIP LOCKED makes it safe once a 2nd appears.
"""

# TODO use models from lmdb.models where appropriate

import os
import shutil
import socket
import argparse
import traceback
import warnings
import requests
from . import run_bknd, xform

LINKMEDDLE_PLAPI = os.environ.get("LINKMEDDLE_PLAPI", "http://localhost:29072/")
# Include the pid so concurrent workers on one host are distinguishable in run.worker (§4.5).
WORKER = os.environ.get("LM_WORKER", f"{socket.gethostname()}/{os.getpid()}")
CLAIM_TIMEOUT = 30
RESULT_TIMEOUT = 64  # large playlists make a big POST body (mirrors the old plugin)
# Free-space floor below which the worker refuses to claim. Shared env var/default with
# pervellam (pervellam 6cffa41, #43) so the two tools share one knob.
DEFAULT_MIN_FREE_BYTES = 32 * 1024**3  # 32 GiB
MIN_FREE_ENV = "WORKER_MIN_FREE_BYTES"

# TODO prepare for bearer auth


def enough_free_space(path: str = ".") -> bool:
    """False (and prints why) when free space under `path` is below the WORKER_MIN_FREE_BYTES
    floor (default 32 GiB; 0 disables). Shared env var with pervellam (#195). `path` is the
    cwd because yt-dlp writes the media file there before ObjIdxUploadPP uploads it to OI, so
    a near-full scratch disk fails the download mid-write and orphans partial files."""
    min_free = int(os.environ.get(MIN_FREE_ENV, DEFAULT_MIN_FREE_BYTES))
    if min_free <= 0:
        return True
    free = shutil.disk_usage(path).free
    if free < min_free:
        print(f"Refusing to claim job: only {free} bytes free in {path}, "
              f"need {min_free} (set {MIN_FREE_ENV}=0 to disable)")
        return False
    return True

def claim_job(api_base: str, worker: str, extractor: str | None = None,
              no_extractor: bool = False) -> dict | None:
    """Claim the single highest-priority due job, or None when nothing is due (204).

    `extractor` pins this worker to one extractor's jobs (worker self-selection, §4.5).
    `no_extractor` (mutually exclusive) pins it to things no extractor has identified yet
    (`extractor_key IS NULL`, #210)."""
    body = {"worker": worker}
    if extractor:
        body["extractor"] = extractor
    if no_extractor:
        body["no_extractor"] = True
    resp = requests.post(f"{api_base.rstrip('/')}/jobs/claim",
                         json=body, timeout=CLAIM_TIMEOUT)
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

    - An ambiguous "both" result (entries AND top-level media, `run_bknd.is_both`) can't be
      classified, so it is reported as a failure (success=False, no body) for a human to inspect
      while `data_json` preserves the raw shape (#164).
    - A container result (yt-dlp `_type == 'playlist'` or any `entries`) is extracted into a
      PullThing (the Stage-1 fan-out body, `playlist`). Non-None info = success.
    - Otherwise the result is a single video, extracted into a PullThing (`video`) so the
      server enriches the stub identically (display + channel). This covers both an under-
      described C-band video's metadata-only enrichment and an unknown URL that resolved to a
      single video (the server classifies it as a leaf, #153). Success is `info is not None`.

    `download` adds the Stage-2 media outcome: the OI file UUID set by ObjIdxUploadPP
    (`run_bknd.result_oi_uuid`, which reads it from `requested_downloads` where yt-dlp leaves
    it) as `best_oi`, and success becomes `oi_uuid is not None`, NOT merely `info is not None`
    — yt-dlp can return an info dict even when the media download failed (the upload PP then
    never ran, leaving no oi_uuid).
    `download` is only ever set for a single-video job, so it never collides with a playlist body.

    `data_json` carries the raw output (kept even on a failed download for debugging);
    `input_json` records the per-run cookies decision.
    """
    body: dict = {'worker': worker, 'input_json': {'cookies': use_cookies}}
    success = info is not None
    if info is not None:
        body['data_json'] = info
        if run_bknd.is_both(info):
            # Ambiguous video+playlist shape we can't classify: fail (data_json kept) for a
            # human to inspect, rather than silently mis-routing it as a playlist (#164).
            success = False
        else:
            container = run_bknd.is_container(info)
            body['playlist' if container else 'video'] = run_bknd.extract_node(info).model_dump(mode="json")
            if not container and download:
                oi_uuid = run_bknd.result_oi_uuid(info)
                success = oi_uuid is not None
                body['best_oi'] = str(oi_uuid) if oi_uuid else None
    body['success'] = success
    resp = requests.post(f"{api_base.rstrip('/')}/jobs/{run_id}/result",
                         json=body, timeout=RESULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def report_failure(api_base: str, job: dict, worker: str) -> None:
    """Finalize a claimed run as a failure (info=None -> success=False).

    Called when running the job raised before `post_result` reported anything, so the API
    never saw a result. Without this the Run stays success=NULL (the in-progress marker)
    forever and the thing is re-claimed on the very next loop — a tight infinite loop on a
    poison job. Posting a failure lets the server record `last_failure_dt` and back `try_on`
    off (§4.4/§4.7), so the worker moves on.
    """
    post_result(api_base, job["run_id"], None, download=job.get("download", False),
                use_cookies=job.get("cookies", False), worker=worker)


def initiate_job(api_base: str, job: dict, worker: str) -> bool:
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
    # cookies_used is the *actual* per-run decision: it drops to False when the cookies were
    # requested but the Crustula fetch 404'd and we fell back to a cookieless run (#198), so the
    # recorded input_json.cookies reflects what really happened (keeps §4.7 escalation accurate).
    info, cookies_used = run_bknd.init_download(
        thing["url"], download=download,
        oibucket=thing["bucket"] if download else None,
        lpmlib=attrs.get("lpm_lib") if download else None,
        run_id=str(run_id) if download else None,
        thing_id=str(thing["id"]) if download else None,
        use_cookies=cookies,
        flat=True,   # flat is a no-op on a single video, so a download still gets a full extract
        info_dict=attrs.get(xform.INFO_JSON_KEY))
    post_result(api_base, run_id, info, download=download,
                use_cookies=cookies_used, worker=worker)
    return bool(info)


def main(argv: list[str] | None = None) -> int:
    # TODO style is like a cronjob then?
    """Pull-one-and-loop: claim -> run -> report, until nothing is due.

    `--extractor` pins this worker to one extractor's jobs (worker self-selection, §4.5);
    `--no-extractor` (mutually exclusive) pins it to things no extractor has identified yet
    (#210). Run the process N times (each with its own filter) for parallel/heterogeneous
    workers."""
    parser = argparse.ArgumentParser(description="LinkMeddle job runner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-e", "--extractor", default=None,
                        help="only claim jobs for this yt-dlp extractor (e.g. youtube)")
    group.add_argument("-E", "--no-extractor", action="store_true",
                        help="only claim jobs whose extractor is not yet known "
                             "(extractor_key IS NULL)")
    args = parser.parse_args(argv)
    status = 0
    count = 0
    fails = 0
    while True:
        succ = False
        # Disk-space backpressure: stop claiming once the scratch disk is near-full (#195),
        # rather than failing the download mid-write and orphaning a partial file.
        if not enough_free_space():
            status = 1
            break
        job = claim_job(LINKMEDDLE_PLAPI, WORKER, args.extractor, args.no_extractor)
        if job is None:
            break
        count += 1
        try:
            succ = initiate_job(LINKMEDDLE_PLAPI, job, WORKER)
        except Exception as exc:  # never let one job kill the loop
            succ = False
            url = (job.get("thing") or {}).get("url")
            # Unexpected crash (NOT a YoutubeDLError, which run_bknd handles cleanly): almost
            # always an unwrapped error from inside yt-dlp. Log type + url + traceback so it
            # reads like an extractor fault, not a mystery one-liner. print the traceback
            # (warnings.warn dedupes by (message, lineno) and would collapse repeats).
            warnings.warn(f"Job {job.get('run_id')} crashed on {url!r}: "
                          f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            status = 1
            # Report the failure so the run is finalized and the thing backs off, rather than
            # being re-claimed forever (the API never saw a result). If reporting itself fails,
            # let the exception propagate and crash the worker — a network error here means
            # something is structurally wrong, not just a bad job.
            report_failure(LINKMEDDLE_PLAPI, job, WORKER)
        if not succ:
            status = 1
            fails += 1
            # Log every failure explicitly: warnings.warn dedupes by (message, lineno) so
            # repeated identical failures (e.g. no-url stubs) print once, and several fail
            # paths are silent. This guarantees `fails` == count of FAIL lines for diagnosis.
            url = (job.get("thing") or {}).get("url")
            print(f"FAIL run={job['run_id']} download={job['download']} url={url!r}")
        if fails >= 3:
            print(f"Stopping after {fails} fails, and {count} jobs; worker={WORKER}.")
            return status
    print(f"Ran {count} job(s); worker={WORKER}.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
