"""CLI to interact with LinkMeddle API for playlist scheduling."""

import os
import datetime
import requests
import typer
from .models import PlaylistSchedBase

LINKMEDDLE_PLAPI = os.environ.get("LINKMEDDLE_PLAPI")

app = typer.Typer(help="CLI to POST /schedules/ to LinkMeddle API")


@app.command("schedule-playlist")
def create(oibucket, webpage_url, use_cookies: bool = False, lpmlib=None) -> None:
    """
    Create a new playlist schedule by POSTing to /schedules/.
    """
    assert LINKMEDDLE_PLAPI is not None, "LINKMEDDLE_PLAPI env var must be set"
    schedule = PlaylistSchedBase(
        oi_bucket=oibucket,
        webpage_url=webpage_url,
        use_cookies=use_cookies,
        lpm_lib=lpmlib,
        next_run=datetime.date.today(),
        freq_days=3
    )
    payload = schedule.model_dump()
    payload['next_run'] = payload['next_run'].isoformat()
    url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/"
    resp = requests.post(url, json=payload, timeout=5)
    resp.raise_for_status()

if __name__ == "__main__":
    app()
