"""CLI to add things (by URL) to the LinkMeddle API."""

import os
from typing import Optional
import requests
import typer

LINKMEDDLE_PLAPI = os.environ.get("LINKMEDDLE_PLAPI")

app = typer.Typer(help="CLI to add things by URL to the LinkMeddle API (POST /things/)")


@app.command("add-thing")
def add(bucket: str, url: str, rating: float = 0.0, container: Optional[bool] = None,
        use_cookies: bool = False, lpmlib: Optional[str] = None) -> None:
    """Add a thing by URL by POSTing to /things/ (V4 form of the old schedule add).

    `bucket` is the required OI storage bucket (no server default; mirrors V3's `oibucket`).
    `rating` is numeric (0..2; default 0.0 = C; D/F not allowed at add). `container` is an
    optional structural hint (True=container, False=video); omit it and the first pull
    classifies the thing (#153). `use_cookies`/`lpmlib` are optional soft hints stored in
    `attrs` (the V4 form of V3's `use_cookies`/`lpm_lib`).
    """
    assert LINKMEDDLE_PLAPI is not None, "LINKMEDDLE_PLAPI env var must be set"
    payload: dict = {"url": url, "bucket": bucket, "rating": rating}
    if container is not None:
        payload["container"] = container
    if use_cookies:
        payload["cookies"] = True
    if lpmlib is not None:
        payload["lpm_lib"] = lpmlib
    api_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/things/"
    resp = requests.post(api_url, json=payload, timeout=5)
    resp.raise_for_status()
    typer.echo(resp.json())


if __name__ == "__main__":
    app()
