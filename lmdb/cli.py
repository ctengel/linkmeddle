"""CLI to add things (by URL) to the LinkMeddle API."""

import os
from typing import Optional
import requests
import typer

LINKMEDDLE_PLAPI = os.environ.get("LINKMEDDLE_PLAPI")

app = typer.Typer(help="CLI to add things by URL to the LinkMeddle API (POST /things/)")


@app.command("add-thing")
def add(url: str, rating: float = 0.0, container: Optional[bool] = None) -> None:
    """Add a thing by URL by POSTing to /things/ (V4 form of the old schedule add).

    `rating` is numeric (-2..+2; default 0.0 = C; D/F not allowed at add). `container` is an
    optional structural hint (True=container, False=video); omit it and the first pull
    classifies the thing (#153).
    """
    assert LINKMEDDLE_PLAPI is not None, "LINKMEDDLE_PLAPI env var must be set"
    payload: dict = {"url": url, "rating": rating}
    if container is not None:
        payload["container"] = container
    api_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/things/"
    resp = requests.post(api_url, json=payload, timeout=5)
    resp.raise_for_status()
    typer.echo(resp.json())


if __name__ == "__main__":
    app()
