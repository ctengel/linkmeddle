import uuid
from typing import Optional
import pydantic

class ThingBase(pydantic.BaseModel):
    url: Optional[str] = None
    dlp_id: Optional[str] = None
    extractor_key: Optional[str] = None
    title: Optional[str] = None
    type: str
    channel: Optional[str] = None


class PlaylistCreate(pydantic.BaseModel):
    url: str

class PlaylistBase(ThingBase):
    url: str
    extractor_key: str
    type: str = 'playlist'
    is_channel: bool = False
    lm_id: int


class VideoBase(ThingBase):
    url: str
    oi_file_uuid: uuid.UUID
    oi_obj_uuid: Optional[uuid.UUID] = None
    object_url: Optional[str] = None
    type: str = 'video'

class Playlist(PlaylistBase):
    videos: list[VideoBase]


class Video(VideoBase):
    playlists: list[PlaylistBase] = []
    oi_obj_uuid: uuid.UUID
    object_url: str