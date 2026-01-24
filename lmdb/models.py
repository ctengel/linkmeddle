"""LinkMeddle data models

Includes DLP-compat and LM-native
"""

import datetime
from typing import Optional, List
from pydantic import BaseModel
from sqlmodel import Column, Field, Session, SQLModel, create_engine, select, ARRAY, TEXT

class CommonDLP(BaseModel):
    """DLP: Elements fon in both playlists and entries thereof"""
    channel_id: str
    channel_url: str
    description: str
    extractor_key: str
    extractor: str
    id: str
    original_url: str
    playlist_count: int
    title: str
    uploader_id: str
    uploader: str
    uploader_url: str
    webpage_url_basename: str
    webpage_url_domain: str
    webpage_url: str


class PlVidDLP(CommonDLP):
    """DLP: A vid as seen as a playlist entry"""
    categories: list[str]
    channel: str
    display_id: str
    duration: int
    epoch: int
    ext: str  # filename?
    format_id: str
    format: str
    fulltitle: str
    _has_drm: bool  # or None
    height: int
    is_live: bool
    language: str
    live_status: str
    n_entries: int
    playlist_autonumber: int
    playlist_channel_id: str
    playlist_id: str
    playlist_index: int
    playlist: str
    playlist_uploader_id: str
    playlist_uploader: str
    playlist_webpage_url: str
    protocol: str
    thumbnail: str
    timestamp: int  # is this a timestamp of what?
    upload_date: str  # YYYYMMDD
    was_live: bool
    width: int


class DLPVersion(BaseModel):
    """DLP version info"""
    version: str
    current_git_head: str  # optional
    release_git_head: str
    repository: str

class PlaylistDLP(CommonDLP):
    """A DLP root playlist"""
    entries: list[PlVidDLP]
    epoch: int  # is this a timestamp of what?
    modified_date: str  # YYYYMMDD
    _type: str  # "playlist
    _version: DLPVersion

class UlChan(BaseModel):
    """Uploader/Channel description"""
    channel_id: str
    uploader_id: str
    uploader: str
    channel_url: str
    uploader_url: str

class DLPIE(BaseModel):
    """DLP extractor used"""
    extractor_key: str
    extractor: str

class VidFull(BaseModel):
    """LM-native full video"""
    channel: UlChan
    description: str
    extractor: DLPIE
    id: str
    title: str
    webpage_url: str
    categories: list[str]
    duration: int
    ext: str  # filename?
    format: str
    height: int
    is_live: bool
    language: str
    n_entries: int
    thumbnail: str
    upload_date:  datetime.datetime
    was_live: bool
    width: int

class PlaylistCommon(SQLModel):
    """Common elements"""
    id: str
    title: str
    modified_date: datetime.datetime
    webpage_url: str
    playlist_count: int

class PlaylistFull(PlaylistCommon):
    """LM-native full playlist"""
    channel: UlChan
    entries: list[VidFull]
    extractor: DLPIE

class PlaylistSumBase(PlaylistCommon):
    """Suitable for simple pl lookup table"""
    channel: str
    extractor_id: str
    pseudo_channel: bool = False

class PlaylistSum(PlaylistSumBase, table=True):
    """LM-native summarized playlist"""
    __tablename__ = "playlist_sums"
    # TODO consider int primary key instead of url
    webpage_url: str = Field(primary_key=True)
    # TODO fix array type
    entries: List[str] = Field(sa_column=Column(ARRAY(TEXT)))

# TODO need a version with and without schedule relationship
class PlaylistStats(SQLModel, table=True):
    """Stats of a playlist run"""
    modified_date: datetime.datetime
    playlist_count: int
    entries_hash: bytes
    different: bool  # optional???
    success: bool
    download_count: int
    input_params: dict
    output_params: dict
    timestamp: datetime.datetime
    newest_item: datetime.datetime
    interval: int  # optional???
    sched_id: Optional[int] = Field(default=None, foreign_key="playlist_scheds.sched_id")
    # TODO relationship
    # schedule: PlaylistSched

class PlaylistSchedBase(SQLModel):
    """A scedule of when to attempt a playlist"""
    extractor_id: str
    id: str
    next_run: datetime.date
    freq_days: int
    input_prams: dict
    webpage_url: str

class PlaylistSched(PlaylistSchedBase, table=True):
    """A schedule of when to attempt a playlist"""
    __tablename__ = "playlist_scheds"
    sched_id: int = Field(primary_key=True, autoincrement=True)
    # TODO relationship
    runs: list[PlaylistStats]

class PlaylistSchedWithStats(PlaylistSched):
    """Playlist schedule with stats included"""
    runs: list[PlaylistStats]

class PlaylistSchedWithStatsAndSum(PlaylistSchedWithStats):
    """Playlist schedule with stats and summary included"""
    summary: PlaylistSumBase | None

class PlaylistSumWithSched(PlaylistSum):
    """Playlist summary with schedule included"""
    schedules: list[PlaylistSchedBase]

class PlaylistRunResult(BaseModel):
    """Result of a playlist run"""
    summary: PlaylistSum
    schedule: Optional[PlaylistSched]
    new_stats: Optional[PlaylistStats]