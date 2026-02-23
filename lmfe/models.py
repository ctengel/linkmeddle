import uuid
from typing import Optional
import datetime
import pydantic

class ThingBase(pydantic.BaseModel):
    url: Optional[str] = None
    dlp_id: Optional[str] = None
    extractor_key: Optional[str] = None
    title: Optional[str] = None
    type: str
    # TODO consider making channel be a playlist ID instead of a URL
    channel: Optional[str] = None


class PlaylistCreate(pydantic.BaseModel):
    url: str

class PlaylistBase(ThingBase):
    # TODO in pydantic how is one supposed to have a child make a field required that is optional in the parent?
    url: str
    extractor_key: str
    type: str = 'playlist'
    is_channel: bool = False
    lm_id: int

class VideoBase(ThingBase):
    url: Optional[str] = None
    oi_file_uuid: Optional[uuid.UUID] = None
    oi_obj_uuid: Optional[uuid.UUID] = None
    object_url: Optional[str] = None
    type: str = 'video'
    file_available: bool = False

class Playlist(PlaylistBase):
    videos: list[VideoBase]
    total_videos: int
    next_run: Optional[datetime.date] = None
    last_run: Optional[datetime.date] = None
    lm_sched_id: Optional[int] = None

class Video(VideoBase):
    playlists: list[PlaylistBase] = []
    oi_obj_uuid: uuid.UUID
    object_url: str

class PlaylistCreateResult(pydantic.BaseModel):
    url: str
    type: str = 'playlist'
    lm_id: Optional[int] = None
    lm_sched_id: int