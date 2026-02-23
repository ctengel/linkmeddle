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
OI_BUCKET = os.getenv("OBJIDX_BUCKET_DEFAULT")

app = fastapi.FastAPI()
    
@app.post("/playlists/", response_model=fe_models.PlaylistCreateResult)
async def create_schedule(schedule: fe_models.PlaylistCreate):
    """Simple upsert playlist schedule by URL. If a schedule for the URL already exists, update its next_run to today. Otherwise, create a new schedule."""
    pl_by_url = None
    async with httpx.AsyncClient(timeout=5) as client:
        # TODO technically this is a race condition; allows multiple schedules to be created #125
        req_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/playlists/{schedule.url}"
        resp = await client.get(req_url)
        if resp.status_code != 404:
            resp.raise_for_status()
            pl_by_url = pl_models.PlaylistSumWithSched.model_validate(resp.json())
        if pl_by_url and pl_by_url.schedules:
            sched = pl_by_url.schedules[0]
            url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/{sched.sched_id}"
            resp = await client.patch(url, json={"next_run": datetime.date.today().isoformat()})
            resp.raise_for_status()
            return fe_models.PlaylistCreateResult(url=schedule.url,
                                                  lm_id=pl_by_url.playlist_id,
                                                  lm_sched_id=sched.sched_id)
        url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/"
        my_schedule = pl_models.PlaylistSchedBase(webpage_url=schedule.url,
                                                  oi_bucket=OI_BUCKET,
                                                  next_run=datetime.date.today(),
                                                  freq_days=3)
        payload = my_schedule.model_dump()
        payload['next_run'] = payload['next_run'].isoformat()
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        my_schedule_resp = pl_models.PlaylistSchedWithStatsAndSum.model_validate(resp.json())
        return fe_models.PlaylistCreateResult(url=schedule.url,
                                              lm_id=my_schedule_resp.summary.playlist_id if my_schedule_resp.summary else None,
                                              lm_sched_id=my_schedule_resp.sched_id)

def oi_file_to_video(oi_file: Optional[oi_client.clilib.File] = None, extractor_id: Optional[str] = None, dlp_id: Optional[str] = None) -> fe_models.VideoBase:
    if not extractor_id or not dlp_id:
        if oi_file and (file_str := oi_file.info.get('extra', {}).get('ytdl-id')):
            extractor_id, dlp_id = file_str.split(" ", 1)
    # TODO set file_available based on OI attributes #123
    return fe_models.VideoBase(url=oi_file.info['url'] if oi_file else None,
                               extractor_key=extractor_id,
                               dlp_id=dlp_id,
                               oi_file_uuid=oi_file.uuid if oi_file else None,
                               oi_obj_uuid=oi_file.object['uuid'] if oi_file else None,
                               object_url=oi_file.get_s3_url() if oi_file else None,
                               file_available=bool(oi_file),
                               title=oi_file.info.get('extra', {}).get('ytdl-info', {}).get('title') if oi_file else None,
                               channel=(oi_file.info.get('extra', {}).get('ytdl-info', {}).get('channel_url') or oi_file.info.get('extra', {}).get('ytdl-info', {}).get('uploader_id')) if oi_file else None)

@app.get("/playlists/{playlist_id}", response_model=fe_models.Playlist)
async def get_playlist(playlist_id: int, random_videos: Optional[int] = None):
    """Proxy GET /playlists/{id}/ from LinkMeddle API."""
    # TODO implement random_videos #123
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
    async with httpx.AsyncClient(timeout=5) as client2:
        url2 = f"{LINKMEDDLE_PLAPI.rstrip('/')}/playlists/{playlist_url}"
        resp2 = await client2.get(url2)
        resp2.raise_for_status()
        js2 = resp2.json()    
    my_playlist = pl_models.PlaylistSumWithSched.model_validate(js2)
    videos = []
    # TODO async #123
    oic = oi_client.get_obj_idx_env()
    for entry in my_playlist.entries:
        extractor_id = entry[1]
        dlp_id = entry[0]
        oic_files = oic.search_files(params={"extra": f"ytdl-id={extractor_id} {dlp_id}"})
        # TODO if multiple, find the best #123
        videos.append(oi_file_to_video(oi_file=oic_files[0] if oic_files else None,
                                       extractor_id=extractor_id,
                                       dlp_id=dlp_id))
    sched_id = None
    next_run = None
    last_run = None
    if my_playlist.schedules:
        sched_id = my_playlist.schedules[0].sched_id
        next_run = my_playlist.schedules[0].next_run
        # TODO actual last_run #125
        last_run = datetime.date.today() - datetime.timedelta(days=1)
    return fe_models.Playlist(url=my_playlist.webpage_url,
                              dlp_id=my_playlist.id,
                              extractor_key=my_playlist.extractor_id,
                              title=my_playlist.title,
                              channel=my_playlist.channel,
                              is_channel=my_playlist.pseudo_channel,
                              lm_id=playlist_id,
                              videos=videos,
                              total_videos=len(my_playlist.entries),
                              next_run=next_run,
                              last_run=last_run,
                              lm_sched_id=sched_id)

@app.get("/playlists/", response_model=list[fe_models.PlaylistBase])
async def list_playlists(url: Optional[str] = None, sched_id: Optional[int] = None, current: bool = False):
    """Proxy GET /playlists/ from LinkMeddle API."""
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
            if not sched_resp.summary:
                raise fastapi.HTTPException(status_code=503, detail="Playlist summary not available for this schedule yet")
            assert sched_resp.summary.playlist_id is not None, f"Expected playlist_id in schedule summary for schedule ID {sched_id}, got {sched_resp.summary.playlist_id}"
            assert sched_resp.webpage_url is not None, f"Expected webpage_url in schedule summary for schedule ID {sched_id}, got {sched_resp.webpage_url}"
            return [fe_models.PlaylistBase(dlp_id=sched_resp.summary.id,
                                           extractor_key=sched_resp.summary.extractor_id,
                                           url=sched_resp.webpage_url,
                                           lm_id=sched_resp.summary.playlist_id)]
        if current:
            # TODO also show recent runs in addition to next runs #125
            url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/"
            resp = await client.get(url)
            resp.raise_for_status()
            data = [pl_models.PlaylistSchedPublic.model_validate(x) for x in resp.json()]
            current_schedules = [x for x in data if x.next_run and x.next_run <= (datetime.date.today() + datetime.timedelta(days=1))]
            playlists = []
            for sched in current_schedules:
                full_schedule_url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/schedules/{sched.sched_id}"
                resp = await client.get(full_schedule_url)
                resp.raise_for_status()
                sched_resp = pl_models.PlaylistSchedWithStatsAndSum.model_validate(resp.json())
                if not sched_resp.summary:
                    continue
                assert sched_resp.summary.playlist_id is not None, f"Expected playlist_id in schedule summary for schedule ID {sched.sched_id}, got {sched_resp.summary.playlist_id}"
                assert sched_resp.webpage_url is not None, f"Expected webpage_url in schedule summary for schedule ID {sched.sched_id}, got {sched_resp.webpage_url}"
                playlists.append(fe_models.PlaylistBase(dlp_id=sched_resp.summary.id,
                                                       extractor_key=sched_resp.summary.extractor_id,
                                                       url=sched_resp.webpage_url,
                                                       lm_id=sched_resp.summary.playlist_id))
            return playlists
    raise fastapi.HTTPException(status_code=400, detail="Need URL or schedule ID")

@app.get("/videos/{file_id}", response_model=fe_models.Video)
async def get_video(file_id: str):
    """Proxy GET /videos/{file_id}/ to ObjectIndex and add playlist info"""
    oic = oi_client.get_obj_idx_env()
    oi_file = oic.get_file(file_id)
    extractor_id = None
    dlp_id = None
    playlists = []
    if file_str := oi_file.info.get('extra', {}).get('ytdl-id'):
        extractor_id, dlp_id = file_str.split(" ", 1)
        async with httpx.AsyncClient(timeout=5) as client:
            url = f"{LINKMEDDLE_PLAPI.rstrip('/')}/videos/{extractor_id}/{dlp_id}"
            resp = await client.get(url)
            resp.raise_for_status()
            playlists = [fe_models.PlaylistBase(dlp_id=x['id'],
                                                extractor_key=x.get('extractor_id'),
                                                title=x.get('title'),
                                                url=x.get('webpage_url'),
                                                channel=x.get('channel'),
                                                is_channel=x.get('pseudo_channel', False),
                                                lm_id=x.get('playlist_id')
                                                ) for x in
                         resp.json()]
    base_video = oi_file_to_video(oi_file=oi_file, extractor_id=extractor_id, dlp_id=dlp_id)
    return fe_models.Video(**base_video.model_dump(), playlists=playlists)


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
    return [oi_file_to_video(oi_file=oi_file) for oi_file in search_result]

@app.get("/url")
async def get_url(url: str):
    """Redirect to the appropriate playlist or video URL."""
    # NOTE future versions may have a "Thing" response model
    try:
        if pl := await list_playlists(url=url):
            return RedirectResponse(url=f"/playlists/{pl[0].lm_id}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
    if vids := await list_videos(url=url):
        return RedirectResponse(url=f"/videos/{vids[0].oi_file_uuid}")
    raise fastapi.HTTPException(status_code=404, detail="URL not found")