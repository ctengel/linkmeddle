import os
import datetime
from typing import Optional
import fastapi
from fastapi.responses import RedirectResponse
import httpx
from obj_idx import client as oi_client
from lmdb import models as pl_models
from . import models as fe_models

LINKMEDDLE_PLAPI = os.getenv("LINKMEDDLE_PLAPI", "http://localhost:29072/")

app = fastapi.FastAPI()

@app.get("/schedules/", response_model=list[pl_models.PlaylistSchedPublic])
async def get_schedules():
    """Proxy GET /schedules/ from LinkMeddle API."""
    # TODO consider merging with list_playlists and filtering by next_run date
    async with httpx.AsyncClient(timeout=5) as client:
        url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/"
        resp = await client.get(url)
        resp.raise_for_status()
        data = [pl_models.PlaylistSchedPublic.model_validate(x) for x in resp.json()]
        return data
    
@app.post("/playlists/", response_model=pl_models.PlaylistSumWithSched, status_code=201)
async def create_schedule(schedule: fe_models.PlaylistCreate):
    """Simple upsert playlist schedule by URL. If a schedule for the URL already exists, update its next_run to today. Otherwise, create a new schedule."""
    # TODO modify callsign???
    pl_by_url = await list_playlists(url=schedule.url)
    if pl_by_url and pl_by_url[0].schedules:
        async with httpx.AsyncClient(timeout=5) as client:
            sched = pl_by_url[0].schedules[0]
            url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/{sched.id}"
            resp = await client.patch(url, json={"next_run": datetime.date.today().isoformat()})
            resp.raise_for_status()
            return resp.json()
        return fastapi.RedirectResponse(url=f"/playlists/{sched[0].playlist_id}")
    async with httpx.AsyncClient(timeout=5) as client:
        url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/"
        payload = schedule.model_dump()
        payload['next_run'] = payload['next_run'].isoformat()
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return fastapi.Response(status_code=201)

@app.get("/playlists/{playlist_id}", response_model=pl_models.PlaylistSumWithVids)
async def get_playlist(playlist_id: str):
    """Proxy GET /playlists/{id}/ from LinkMeddle API."""
    async with httpx.AsyncClient(timeout=5) as client:
        url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/playlists/"
        resp = await client.get(url)
        resp.raise_for_status()
        js = resp.json()
        for item in js:
            if item['playlist_id'] == playlist_id:
                return item
        raise fastapi.HTTPException(status_code=404, detail="Playlist not found")

@app.get("/playlists/", response_model=list[pl_models.PlaylistSum])
async def list_playlists(url: Optional[str] = None, sched_id: Optional[int] = None):
    """Proxy GET /playlists/ from LinkMeddle API."""
    # TODO consider merging get_schedules and filtering by next_run date
    # TODO consider adding if 404
    async with httpx.AsyncClient(timeout=5) as client:
        if url:
            req_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/playlists/{url}"
            resp = await client.get(req_url)
            resp.raise_for_status()
            return [resp.json()]
        req_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/playlists/"
        resp = await client.get(req_url)
        resp.raise_for_status()
        if sched_id:
            return [item for item in resp.json() if any(x.get("sched_id") == sched_id for x in item.get("schedules", []))]
        return []

@app.get("/videos/{file_id}")
async def get_video(file_id: str):
    """Proxy GET /videos/{file_id}/ to ObjectIndex and add playlist info"""
    oic = oi_client.get_obj_idx_env()
    oi_file = oic.get_file(file_id)
    oi_file['playlists'] = []
    if file_str := oi_file.info.get('ytdl-id'):
        extractor_id, dlp_id = file_str.split(" ", 1)
        async with httpx.AsyncClient(timeout=5) as client:
            url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/videos/{extractor_id}/{dlp_id}"
            resp = await client.get(url)
            resp.raise_for_status()
            oi_file['playlists'] = resp.json().get('playlists', [])
    return oi_file

@app.get("/videos/")
async def list_videos(url: Optional[str] = None, extractor_id: Optional[str] = None, dlp_id: Optional[str] = None):
    """Proxy GET /videos/ to ObjectIndex search"""
    oic = oi_client.get_obj_idx_env()
    params = {}
    if url:
        params['url'] = url
    if extractor_id:
        params['ytdl-id'] = f"{extractor_id} {dlp_id}"
    return oic.search_files(params=params)

@app.get("/url")
async def get_url(url: str):
    """Redirect to the appropriate playlist or video URL."""
    # TODO consider adding if 404
    if pl := await list_playlists(url=url):
        return RedirectResponse(url=f"/playlists/{pl[0]['playlist_id']}")
    if vids := await list_videos(url=url):
        return RedirectResponse(url=f"/videos/{vids[0]['file_id']}")
    raise fastapi.HTTPException(status_code=404, detail="URL not found")