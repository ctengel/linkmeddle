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
    # TODO merge with list_playlists and filtering by next_run date
    async with httpx.AsyncClient(timeout=5) as client:
        url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/"
        resp = await client.get(url)
        resp.raise_for_status()
        data = [pl_models.PlaylistSchedPublic.model_validate(x) for x in resp.json()]
        return data
    
@app.post("/playlists/", response_model=pl_models.PlaylistSumWithSched, status_code=201)
async def create_schedule(schedule: fe_models.PlaylistCreate):
    """Simple upsert playlist schedule by URL. If a schedule for the URL already exists, update its next_run to today. Otherwise, create a new schedule."""
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

@app.get("/playlists/{playlist_id}", response_model=fe_models.Playlist)
async def get_playlist(playlist_id: int):
    """Proxy GET /playlists/{id}/ from LinkMeddle API."""
    # TODO add basic video info - we want OI file UUID at least
    async with httpx.AsyncClient(timeout=5) as client:
        url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/playlists/"
        resp = await client.get(url, params={"playlist_id": playlist_id})
        resp.raise_for_status()
        js = resp.json()
    if not js:
        raise fastapi.HTTPException(status_code=404, detail="Playlist not found")
    assert len(js) == 1, f"Expected exactly one playlist with ID {playlist_id}, got {len(js)}"
    assert js[0]['playlist_id'] == playlist_id, f"Expected playlist ID {playlist_id}, got {js[0]['playlist_id']}"
    playlist_url = js[0].get('webpage_url')
    async with httpx.AsyncClient(timeout=5) as client:
        url2 = f"{LINKMEDDLE_PLAPI.rstrip('/')}/playlists/{playlist_url}"
        resp2 = await client.get(url2)
        resp2.raise_for_status()
        js2 = resp2.json()    
    my_playlist = pl_models.PlaylistSumWithVids.model_validate(js2)
    # TODO need to pull in OI file UUID from OI - right now limited in that PLAPI doesn't return extractor ID per-video...
    return fe_models.Playlist(url=my_playlist.webpage_url,
                              dlp_id=my_playlist.id,
                              extractor_key=my_playlist.extractor_id,
                              title=my_playlist.title,
                              channel=my_playlist.channel,
                              is_channel=my_playlist.pseudo_channel,
                              lm_id=my_playlist.playlist_id,
                              videos=[fe_models.VideoBase(dlp_id=x) for x in my_playlist.entries])

@app.get("/playlists/", response_model=list[fe_models.PlaylistBase])
async def list_playlists(url: Optional[str] = None, sched_id: Optional[int] = None):
    """Proxy GET /playlists/ from LinkMeddle API."""
    # TODO merge get_schedules and filter by next_run date
    # TODO consider adding if 404
    async with httpx.AsyncClient(timeout=5) as client:
        if url:
            req_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/playlists/{url}"
            resp = await client.get(req_url)
            resp.raise_for_status()
            x = resp.json()
            return [fe_models.PlaylistBase(dlp_id=x['id'],
                                           extractor_key=x.get('extractor_id'),
                                           title=x.get('title'),
                                           url=x.get('webpage_url'),
                                           channel=x.get('channel'),
                                           is_channel=x.get('pseudo_channel', False),
                                           lm_id=x.get('playlist_id'))]
        if sched_id:
            req_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/{sched_id}"
            resp = await client.get(req_url)
            resp.raise_for_status()
            sched_resp = pl_models.PlaylistSchedWithStatsAndSum.model_validate(resp.json())
            # TODO retest after PLAPI updated to include playlist_id in schedule response
            return [fe_models.PlaylistBase(dlp_id=sched_resp.summary.id,
                                           extractor_key=sched_resp.summary.extractor_id,
                                           url=sched_resp.webpage_url,
                                           lm_id=sched_resp.summary.playlist_id)]
    raise fastapi.HTTPException(status_code=400, detail="Need URL or schedule ID")

@app.get("/videos/{file_id}", response_model=fe_models.Video)
async def get_video(file_id: str):
    """Proxy GET /videos/{file_id}/ to ObjectIndex and add playlist info"""
    oic = oi_client.get_obj_idx_env()
    oi_file = oic.get_file(file_id)
    extractor_id = None
    dlp_id = None
    playlists = []
    # TODO add channel and title to video
    if file_str := oi_file.info.get('extra', {}).get('ytdl-id'):
        extractor_id, dlp_id = file_str.split(" ", 1)
        async with httpx.AsyncClient(timeout=5) as client:
            url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/videos/{extractor_id}/{dlp_id}"
            resp = await client.get(url)
            resp.raise_for_status()
            # TODO retest after PLAPI updated to return playlist_id in video response
            playlists = [fe_models.PlaylistBase(dlp_id=x['id'],
                                                extractor_key=x.get('extractor_id'),
                                                title=x.get('title'),
                                                url=x.get('webpage_url'),
                                                channel=x.get('channel'),
                                                is_channel=x.get('pseudo_channel', False),
                                                lm_id=x.get('playlist_id')
                                                ) for x in
                         resp.json()]
    return fe_models.Video(url=oi_file.info['url'],
                           extractor_key=extractor_id,
                           dlp_id=dlp_id,
                           oi_file_uuid=oi_file.uuid,
                           oi_obj_uuid=oi_file.object['uuid'],
                           object_url=oi_file.get_s3_url(),
                           playlists=playlists)


@app.get("/videos/", response_model=list[fe_models.VideoBase])
async def list_videos(url: Optional[str] = None, extractor_id: Optional[str] = None, dlp_id: Optional[str] = None):
    """Proxy GET /videos/ to ObjectIndex search"""
    oic = oi_client.get_obj_idx_env()
    params = {}
    if url:
        params['url'] = url
    if extractor_id:
        params['extra'] = f"ytdl-id={extractor_id} {dlp_id}"
    search_result = oic.search_files(params=params)
    return [fe_models.VideoBase(url=oi_file.info['url'],
                                oi_file_uuid=oi_file.uuid,
                                oi_obj_uuid=oi_file.object['uuid'],
                                object_url=oi_file.get_s3_url()) for oi_file in search_result]

@app.get("/url")
async def get_url(url: str):
    """Redirect to the appropriate playlist or video URL."""
    # TODO consider adding if 404
    try:
        if pl := await list_playlists(url=url):
            return RedirectResponse(url=f"/playlists/{pl[0]['playlist_id']}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
    if vids := await list_videos(url=url):
        return RedirectResponse(url=f"/videos/{vids[0].oi_file_uuid}")
    raise fastapi.HTTPException(status_code=404, detail="URL not found")