#!/usr/bin/env python3
# TODO do we allow running like this?

"""
Runnable module that queries the LMDB API /schedules/ endpoint and runs jobs
"""

# TODO allow running n jobs or a % of due jobs
# TODO allow specifying a given extractor
# TODO allow just running a URL
# TODO use models from lmdb.models where appropriate
# TODO tell LMAPI when job is starting?

import os
import logging
import datetime
import requests
from . import models

# Config via environment
LINKMEDDLE_PLAPI = os.environ.get("LMDB_API_BASE", "http://localhost:8000")
TIMEOUT = 5
#LMDB_API_TOKEN = os.environ.get("LMDB_API_TOKEN")  # optional Bearer token


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("job_runner")


#def _get_headers() -> Dict[str, str]:
#    headers = {"Accept": "application/json"}
#    if LMDB_API_TOKEN:
#        headers["Authorization"] = f"Bearer {LMDB_API_TOKEN}"
#    return headers


def fetch_schedules(date: datetime.date) -> list[models.PlaylistSchedBase]:
    """Call GET /schedules/ and return a list of schedule objects (JSON)."""
    url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/"
    # TODO next-run or next_run?
    resp = requests.get(url,
                        params={"next-run": date.isoformat()},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    data = [models.PlaylistSchedBase.model_validate(x) for x in resp.json()]
    return data


#def _parse_iso_datetime(s: Optional[str]) -> Optional[datetime.datetime]:
#    """Parse an ISO datetime string into an aware datetime in UTC."""
#    if not s:
#        return None
#    # TODO consider this???
#    # Handle trailing Z (UTC) and naive offsets
#    if s.endswith("Z"):
#        s = s[:-1] + "+00:00"
#    try:
#        dt = datetime.fromisoformat(s)
#    except ValueError:
#        # fallback: try to parse common format
#        return None
#    if dt.tzinfo is None:
#        # treat naive as UTC
#        dt = dt.replace(tzinfo=timezone.utc)
#    return dt.astimezone(timezone.utc)


#def needs_run_today(schedule: models.PlaylistSchedBase, today_utc: datetime.date) -> bool:
#    """Return True if schedule.next_run falls on today_utc."""
#    next_run_raw = schedule.get("next_run") or schedule.get("nextRun")  # support both keys
#    dt = _parse_iso_datetime(next_run_raw)
#    if not dt:
#        return False
#    return dt.date() == today_utc


def initiate_job(schedule: models.PlaylistSchedBase) -> None:
    """
    Skeleton function that should initiate the actual job execution.
    Currently a placeholder; replace implementation with real runner.
    Receives the full schedule dict so caller can provide required info.
    """
    # Placeholder implementation: log what would be run.
    job_info = {
        #"schedule_id": schedule.scched_id,  # TODO add this to model
        "next_run": schedule.next_run,
        "payload": schedule.webpage_url,
    }
    logger.info("Initiating job: %s", job_info)
    # TODO: integrate with actual job executor, queue, or worker here.


def main():
    """Job runner main loop"""
    today = datetime.date.today()
    try:
        schedules = fetch_schedules(today)
    except Exception as exc:
        logger.exception("Failed to fetch schedules: %s", exc)
        return 2

    logger.info("Found %d schedules to run today.", len(schedules))

    for sched in schedules:
        try:
            initiate_job(sched)
        except Exception:
            logger.exception("Failed to initiate job for schedule id=%s",
                             sched.webpage_url)  # TODO sched.sched_id)
            # TODO return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
