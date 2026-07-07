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
from urllib.parse import urljoin
import fastapi
from fastapi import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
import httpx
from obj_idx import client as oi_client
from lmdb import models as pl_models
from . import models as fe_models

LINKMEDDLE_PLAPI = os.getenv("LINKMEDDLE_PLAPI", "http://localhost:29072/")
OI_BUCKET = os.getenv("OBJIDX_BUCKET_DEFAULT")
OBJIDX_URL = os.getenv("OBJIDX_URL")
PERVELLAM_URL = os.getenv("PERVELLAM_URL")
# Cap on OI files a tag search maps back to things (each becomes a best_oi= query param).
TAG_SEARCH_MAX = 100

app = fastapi.FastAPI()

app.mount("/static", StaticFiles(directory="lmfe/static"), name="static")


@app.get("/", include_in_schema=False)
async def root():
    """Serve the SPA at the root (design §3.3 GET /)."""
    return RedirectResponse("/static/index.html")


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


def _resolve_media(best_oi) -> tuple[Optional[str], Optional[str], Optional[dict]]:
    """Resolve an OI file UUID to (download_url, object_url, file info dict). Sync OI calls —
    one GET file/{uuid} (get_file) + one presign (get_s3_url); the info dict rides along free.
    The object URL is absolutized against OBJIDX_URL (the client may return a bare path)."""
    if not best_oi:
        return None, None, None
    oi_file = oi_client.get_obj_idx_env().get_file(str(best_oi))
    object_url = oi_file.get_object_url()
    if object_url and OBJIDX_URL:
        object_url = urljoin(OBJIDX_URL, object_url)
    return oi_file.get_s3_url(), object_url, oi_file.info


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


def _failing_sort_key(t: fe_models.ThingSummary):
    """Order the actionable-failed list (#129): actionable (try_on set) before
    acknowledged permafails (try_on null); within each, highest effective rating
    first, then most-recent failure first. Negated so a plain ascending sort puts
    the most urgent row on top; None rating sorts as lowest, no failure as oldest.
    """
    rating = t.effective_rating if t.effective_rating is not None else float("-inf")
    failed_at = t.last_failure_dt.timestamp() if t.last_failure_dt is not None else 0.0
    return (t.try_on is None, -rating, -failed_at)


@app.get("/things/", response_model=list[fe_models.ThingSummary])
async def list_things(container: Optional[bool] = None, kind: Optional[str] = None,
                      rating: Optional[float] = None, min_rating: Optional[float] = None,
                      due: bool = False, needs_rating: bool = False, new: bool = False,
                      failing: bool = False, watch_soon: bool = False,
                      limit: Optional[int] = None, url: Optional[str] = None,
                      extractor: Optional[str] = None, native_id: Optional[str] = None,
                      q: Optional[str] = None):
    """List/search things; passes every LMDB filter through (incl. the `q` title search).
    Backs all list views and (with `new`/`failing`/`watch_soon`) the status-dashboard +
    Watch Soon panels. No OI round-trips."""
    params = {}
    for name, val in (("container", container), ("kind", kind), ("rating", rating),
                      ("min_rating", min_rating), ("limit", limit), ("url", url),
                      ("extractor", extractor), ("native_id", native_id), ("q", q)):
        if val is not None:
            params[name] = val
    for name, flag in (("due", due), ("needs_rating", needs_rating),
                       ("new", new), ("failing", failing), ("watch_soon", watch_soon)):
        if flag:
            params[name] = True
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(_plapi("/things/"), params=params))
    things = [fe_models.ThingSummary.from_thing_read(pl_models.ThingRead.model_validate(t))
              for t in resp.json()]
    if failing:
        things.sort(key=_failing_sort_key)
    return things


@app.get("/things/facets", response_model=list[pl_models.Facet])
async def thing_facets():
    """Extractor facets for browse-by-extractor (thin proxy of LMDB). Registered before
    GET /things/{thing_id} so the literal path wins."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(_plapi("/things/facets")))
    return resp.json()


@app.get("/things/{thing_id}", response_model=fe_models.ThingPage)
async def get_thing(thing_id: str):
    """One-call page view-model: the thing, its rel neighbors, and (for an
    acquired thing) its resolved playback URL — all inline, no per-child OI calls."""
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

        direct_children = [r for r in related if r.direction == "child"]
        if direct_children and all(r.thing.container is True for r in direct_children):
            sub_resps = await asyncio.gather(*[
                client.get(_plapi(f"/things/{r.thing.id}/related"))
                for r in direct_children
            ], return_exceptions=True)
            for parent_rel, sub_resp in zip(direct_children, sub_resps):
                # Optional enrichment: a single failed/slow/missing sub-container must not 500 the
                # whole page (the primary thing+related calls already succeeded). Skip it instead.
                if isinstance(sub_resp, Exception) or sub_resp.status_code != 200:
                    continue
                for r in [pl_models.RelatedThing.model_validate(x)
                          for x in sub_resp.json()]:
                    if r.direction == "child" and r.thing.container is False:
                        page.indirect_children.append(fe_models.IndirectChild(
                            container_id=parent_rel.thing.id,
                            container_title=parent_rel.thing.title,
                            channel=r.channel,
                            thing=fe_models.ThingSummary.from_thing_read(r.thing),
                        ))

    # Resolve only the requested thing's OI URL inline (one cheap call; containers have no
    # best_oi so this is a no-op for them). The `related` neighbors are left unresolved — a
    # container page must not presign every child's OI URL; the SPA prefetches those it needs
    # via the dedicated /things/{id}/playback endpoint.
    if thing.best_oi:
        page.download_url, _, oi_info = await run_in_threadpool(_resolve_media, thing.best_oi)
        page.oi_info = fe_models.OIFileInfo.from_oi_info(oi_info)
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
    download_url, object_url, oi_info = await run_in_threadpool(_resolve_media, best_oi)
    return fe_models.PlaybackInfo(best_oi=best_oi, download_url=download_url,
                                  object_url=object_url,
                                  oi_info=fe_models.OIFileInfo.from_oi_info(oi_info))


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


@app.get("/jobs/upcoming", response_model=list[fe_models.UpcomingJob])
async def upcoming_jobs(limit: int = 20):
    """Dashboard "Upcoming Jobs" panel (#193): LMDB's read-only dispatch-order preview."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(_plapi("/jobs/upcoming"), params={"limit": limit}))
    return [fe_models.UpcomingJob(
                kind=j["kind"], download=j["download"],
                thing=fe_models.ThingSummary.from_thing_read(
                    pl_models.ThingRead.model_validate(j["thing"])))
            for j in resp.json()]


@app.get("/search/tags", response_model=fe_models.TagSearchResult)
async def search_tags(key: str, value: str):
    """OI tag search (objectindex gui.py parity): files tagged key=value, mapped back to
    things via the LMDB best_oi filter. OI files LM has no thing for come back as `unmatched`
    link-out rows. Capped at TAG_SEARCH_MAX OI hits (each becomes a query param)."""
    def _oi_search():
        return oi_client.get_obj_idx_env().search_files({"extra": f"{key}={value}"})
    files = (await run_in_threadpool(_oi_search))[:TAG_SEARCH_MAX]
    if not files:
        return fe_models.TagSearchResult()
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(
            _plapi("/things/"), params=[("best_oi", str(f.uuid)) for f in files]))
    things = [fe_models.ThingSummary.from_thing_read(pl_models.ThingRead.model_validate(t))
              for t in resp.json()]
    matched = {str(t.best_oi) for t in things}
    unmatched = [fe_models.TagHit(file_uuid=f.uuid, source_url=(f.info or {}).get("url"))
                 for f in files if str(f.uuid) not in matched]
    return fe_models.TagSearchResult(things=things, unmatched=unmatched)


@app.get("/pervellam/jobs", response_model=list[fe_models.PervellamJob])
async def pervellam_jobs(filt: str = "active"):
    """Read-only Pervellam (live-stream tool) job feed for the dashboard panel. 404 when
    PERVELLAM_URL is unset — the SPA takes that as "hide the panel"."""
    if not PERVELLAM_URL:
        raise fastapi.HTTPException(status_code=404, detail="Pervellam not configured")
    async with httpx.AsyncClient(timeout=5) as client:
        resp = _checked(await client.get(f"{PERVELLAM_URL.rstrip('/')}/jobs/",
                                         params={"filt": filt}))
    return [fe_models.PervellamJob.model_validate(j) for j in resp.json()]


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
