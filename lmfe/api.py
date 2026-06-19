"""LinkMeddle frontend BFF.

A thin Backend-for-Frontend for the SPA: it does the work in Python — talking to
the LMDB V4 `thing` API (`LINKMEDDLE_PLAPI`) and Object Index on the SPA's
behalf — so the JavaScript stays minimal (LM-V4-DESIGN.md §3.2/§3.3). It owns no
durable data and is free to break/evolve across 4.x.

The data path is *not* proxied: media is handed to the consumer as a resolved OI
download URL (`best_oi` -> presigned S3 URL), never streamed through here.
"""
import os
import asyncio
from typing import Optional
import fastapi
from fastapi import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
import httpx
from obj_idx import client as oi_client
from lmdb import models as pl_models
from . import models as fe_models

LINKMEDDLE_PLAPI = os.getenv("LINKMEDDLE_PLAPI", "http://localhost:29072/")
OI_BUCKET = os.getenv("OBJIDX_BUCKET_DEFAULT")

app = fastapi.FastAPI()

app.mount("/static", StaticFiles(directory="lmfe/static"), name="static")


def _plapi(path: str) -> str:
    """Build a LMDB PLAPI URL from a leading-slash path."""
    return f"{LINKMEDDLE_PLAPI.rstrip('/')}{path}"


def _checked(resp: httpx.Response) -> httpx.Response:
    """Pass a backend error status (e.g. 404) straight through to our caller.

    Without this an httpx error would surface as an opaque 500; instead we mirror
    the backend's status code and `detail` so the SPA sees the real outcome.
    """
    if resp.is_error:
        try:
            detail = resp.json().get("detail")
        except Exception:  # noqa: BLE001 - non-JSON error body
            detail = resp.text or None
        raise fastapi.HTTPException(status_code=resp.status_code, detail=detail)
    return resp


def _resolve_media(best_oi) -> tuple[Optional[str], Optional[str]]:
    """Resolve an OI file UUID to (download_url, object_url). Sync OI calls."""
    if not best_oi:
        return None, None
    oi_file = oi_client.get_obj_idx_env().get_file(str(best_oi))
    return oi_file.get_s3_url(), oi_file.get_object_url()


@app.post("/things/", response_model=fe_models.ThingSummary)
async def add_thing(item: fe_models.ThingCreate, response: Response):
    """Add a thing by URL (the human entry point), defaulting the OI bucket.

    Idempotent on URL: the backend returns 201 on create / 200 on an existing
    thing, and we propagate that status. The bucket is filled from
    OBJIDX_BUCKET_DEFAULT when the caller omits it, so GUI/bookmarklet users
    never pick one.
    """
    bucket = item.bucket or OI_BUCKET
    if not bucket:
        raise fastapi.HTTPException(
            status_code=400,
            detail="No bucket given and OBJIDX_BUCKET_DEFAULT is unset")
    payload = {"url": item.url, "bucket": bucket}
    for field in ("container", "rating", "cookies", "lpm_lib"):
        val = getattr(item, field)
        if val is not None:
            payload[field] = val
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.post(_plapi("/things/"), json=payload))
    response.status_code = resp.status_code
    return fe_models.ThingSummary.from_thing_read(pl_models.ThingRead.model_validate(resp.json()))


@app.get("/things/", response_model=list[fe_models.ThingSummary])
async def list_things(container: Optional[bool] = None, kind: Optional[str] = None,
                      rating: Optional[float] = None, min_rating: Optional[float] = None,
                      due: bool = False, needs_rating: bool = False, new: bool = False,
                      failing: bool = False, url: Optional[str] = None,
                      extractor: Optional[str] = None, native_id: Optional[str] = None):
    """List/search things; passes every LMDB filter through. Backs all list views
    and (with `new`/`failing`) the status-dashboard panels. No OI round-trips."""
    params = {}
    for name, val in (("container", container), ("kind", kind), ("rating", rating),
                      ("min_rating", min_rating), ("url", url), ("extractor", extractor),
                      ("native_id", native_id)):
        if val is not None:
            params[name] = val
    for name, flag in (("due", due), ("needs_rating", needs_rating),
                       ("new", new), ("failing", failing)):
        if flag:
            params[name] = True
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(_plapi("/things/"), params=params))
    return [fe_models.ThingSummary.from_thing_read(pl_models.ThingRead.model_validate(t))
            for t in resp.json()]


@app.get("/things/{thing_id}", response_model=fe_models.ThingPage)
async def get_thing(thing_id: str):
    """One-call page view-model: the thing, its rel neighbors, and (for an
    acquired video) a resolved playback URL — all inline, no per-child OI calls."""
    async with httpx.AsyncClient(timeout=5) as client:
        thing_resp, related_resp = await asyncio.gather(
            client.get(_plapi(f"/things/{thing_id}")),
            client.get(_plapi(f"/things/{thing_id}/related")))
    thing = pl_models.ThingRead.model_validate(_checked(thing_resp).json())
    related = [pl_models.RelatedThing.model_validate(r) for r in _checked(related_resp).json()]
    page = fe_models.ThingPage(**fe_models.ThingSummary.from_thing_read(thing).model_dump())
    page.related = [
        fe_models.RelatedSummary(direction=r.direction, channel=r.channel,
                                 thing=fe_models.ThingSummary.from_thing_read(r.thing))
        for r in related]
    # download_url is resolved lazily by the SPA via /things/{id}/playback (current video +
    # a 1-2 prefetch), not eagerly here — a container page must not presign every child's OI URL.
    return page


@app.get("/things/{thing_id}/playback", response_model=fe_models.PlaybackInfo)
async def get_playback(thing_id: str):
    """On-demand resolve a thing's acquired media to consumer-usable URLs, so a
    container page need not resolve every child's URL up front."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(_plapi(f"/things/{thing_id}")))
    best_oi = pl_models.ThingRead.model_validate(resp.json()).best_oi
    if not best_oi:
        raise fastapi.HTTPException(status_code=404, detail="No media acquired for this thing")
    download_url, object_url = await run_in_threadpool(_resolve_media, best_oi)
    return fe_models.PlaybackInfo(best_oi=best_oi, download_url=download_url,
                                  object_url=object_url)


@app.get("/things/{thing_id}/runs", response_model=list[fe_models.RunSummary])
async def get_thing_runs(thing_id: str):
    """Run history for a thing, newest first (thin proxy of LMDB)."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(_plapi(f"/things/{thing_id}/runs")))
    return [fe_models.RunSummary(**pl_models.RunRead.model_validate(r).model_dump())
            for r in resp.json()]


@app.patch("/things/{thing_id}", response_model=fe_models.ThingSummary)
async def patch_thing(thing_id: str, item: fe_models.RatingPatch):
    """Set a rating, acknowledge a permanent failure (try_on=null), or edit
    cookies/lpm_lib soft hints. The primary path for exposing V4 ratings."""
    payload = item.model_dump(exclude_unset=True, mode="json")
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.patch(_plapi(f"/things/{thing_id}"), json=payload))
    return fe_models.ThingSummary.from_thing_read(pl_models.ThingRead.model_validate(resp.json()))


@app.get("/runs/", response_model=list[fe_models.RunSummary])
async def list_runs(limit: int = 50, success: Optional[bool] = None,
                    in_progress: bool = False):
    """Global recent-activity feed for the status dashboard (proxy of LMDB)."""
    params = {"limit": limit}
    if success is not None:
        params["success"] = success
    if in_progress:
        params["in_progress"] = True
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(_plapi("/runs/"), params=params))
    return [fe_models.RunSummary(**pl_models.RunActivity.model_validate(r).model_dump())
            for r in resp.json()]


@app.get("/url", response_model=fe_models.ThingSummary)
async def resolve_url(u: str):
    """Resolve a pasted URL to an existing thing (dedupe / add-helper). 404 if we
    don't have it yet, which the SPA turns into an add prompt."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(_plapi("/things/"), params={"url": u}))
    things = resp.json()
    if not things:
        raise fastapi.HTTPException(status_code=404, detail="URL not found")
    return fe_models.ThingSummary.from_thing_read(pl_models.ThingRead.model_validate(things[0]))
