import uuid
from typing import Optional
import pydantic

class PlaylistCreate(pydantic.BaseModel):
    url: str

class PlaylistBase(pydantic.BaseModel):
    dlp_id: Optional[str] = None
    title: Optional[str] = None
    url: str
    channel: Optional[str] = None
    extractor_key: str
    is_channel: bool = False


class Video(pydantic.BaseModel):
    url: str
    extractor_key: Optional[str] = None
    dlp_id: Optional[str] = None
    oi_file_uuid: uuid.UUID
    oi_obj_uuid: uuid.UUID
    object_url: str
    playlists: list[PlaylistBase] = []