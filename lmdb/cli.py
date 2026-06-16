"""CLI to add things (by URL) to the LinkMeddle API."""

import os
from typing import Optional
import requests
import typer

LINKMEDDLE_PLAPI = os.environ.get("LINKMEDDLE_PLAPI")

app = typer.Typer(help="CLI to add things by URL to the LinkMeddle API (POST /things/)")


@app.command("add-thing")
def add(url: str, rating: str = "C", thing_type: Optional[str] = None) -> None:
    """Add a thing by URL by POSTing to /things/ (V4 form of the old schedule add).

    `thing_type` is an optional hint (channel/playlist/video); omit it and the first pull
    classifies the thing (#153).
    """
    assert LINKMEDDLE_PLAPI is not None, "LINKMEDDLE_PLAPI env var must be set"
    payload = {"url": url, "rating": rating}
    if thing_type is not None:
        payload["type"] = thing_type
    api_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/things/"
    resp = requests.post(api_url, json=payload, timeout=5)
    resp.raise_for_status()
    typer.echo(resp.json())


if __name__ == "__main__":
    app()
