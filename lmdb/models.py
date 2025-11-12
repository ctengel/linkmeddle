from pydantic import BaseModel

# base dlpthing

class PlVidDLP(BaseModel):
    id: str
    title: str
    thumbnail: str
    description: str
    channel_id: str
    uploader_id: str
    uploader: str
    channel_url: str
    uploader_url: str
    channel: str
    duration: int
    webpage_url: str
    original_url: str
    webpage_url_basename: str
    webpage_url_domain: str
    epoch: int
    categories: list[str]
    live_status: str
    is_live: bool
    was_live: bool
    upload_date: str  # YYYYMMDD
    timestamp: int  # is this a timestamp of what?
    extractor_key: str
    extractor: str
    playlist_count: int
    playlist: str
    playlist_id: str
    playlist_uploader: str
    playlist_uploader_id: str
    playlist_channel_id: str
    playlist_webpage_url: str
    n_entries: int
    playlist_index: int
    playlist_autonumber: int
    display_id: str
    fulltitle: str
    _has_drm: bool  # or None
    format: str
    format_id: str
    ext: str  # filename?
    protocol: str
    language: str
    width: int
    height: int

class DLPVersion(BaseModel):
    version: str
    current_git_head: str  # optional
    release_git_head: str
    repository: str

class PlaylistDLP(BaseModel):
    id: str
    title: str
    description: str
    modified_date: str  # YYYYMMDD
    playlist_count: int
    channel_id: str
    uploader_id: str
    uploader: str
    channel_url: str
    uploader_url: str
    _type: str  # "playlist
    entries: list[PlVidDLP]
    extractor_key: str
    extractor: str
    webpage_url: str
    original_url: str
    webpage_url_basename: str
    webpage_url_domain: str
    epoch: int  # is this a timestamp of what?
    _version: DLPVersion



