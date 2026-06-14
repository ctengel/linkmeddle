"""CLI to add things (by URL) to the LinkMeddle API."""

import os
import requests
import typer

LINKMEDDLE_PLAPI = os.environ.get("LINKMEDDLE_PLAPI")

app = typer.Typer(help="CLI to add things by URL to the LinkMeddle API (POST /things/)")


@app.command("add-thing")
def add(url: str, rating: str = "B", thing_type: str = "playlist") -> None:
    """Add a thing by URL by POSTing to /things/ (V4 form of the old schedule add)."""
    assert LINKMEDDLE_PLAPI is not None, "LINKMEDDLE_PLAPI env var must be set"
    payload = {"url": url, "type": thing_type, "rating": rating}
    api_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/things/"
    resp = requests.post(api_url, json=payload, timeout=5)
    resp.raise_for_status()
    typer.echo(resp.json())


if __name__ == "__main__":
    app()
