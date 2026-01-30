"""LinkMeddle data models

Includes DLP-compat and LM-native
"""

import datetime
from typing import Optional, List
from pydantic import BaseModel
from sqlmodel import Field, Relationship, SQLModel

class CommonDLP(BaseModel):
    """DLP: Elements fon in both playlists and entries thereof"""
    channel_id: Optional[str] = None
    channel_url: Optional[str] = None
    description: Optional[str] = None
    extractor_key: str
    extractor: str
    id: str
    original_url: str
    playlist_count: int
    title: str
    uploader_id: Optional[str] = None
    uploader: Optional[str] = None
    uploader_url: Optional[str] = None
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
    modified_date: Optional[str] = None  # YYYYMMDD
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
    id: Optional[str] = None
    title: Optional[str] = None
    modified_date: Optional[datetime.datetime] = None
    webpage_url: str
    # TODO do we ever update playlist count in DB?
    playlist_count: Optional[int] = None

class PlaylistFull(PlaylistCommon):
    """LM-native full playlist"""
    channel: UlChan
    entries: list[VidFull]
    extractor: DLPIE

class PlaylistSumBase(PlaylistCommon):
    """Suitable for simple pl lookup table"""
    channel: Optional[str] = None
    extractor_id: str
    pseudo_channel: bool = False

class PlaylistSum(PlaylistSumBase, table=True):
    """LM-native summarized playlist"""
    #__tablename__ = "playlist_sums"
    playlist_id: int | None =Field(primary_key=True, default=None)
    entries: List['PlaylistVid'] = Relationship(back_populates="playlist")

class PlaylistVid(SQLModel, table=True):
    """Link between vids and playlists"""
    vid_id: str = Field(primary_key=True)
    playlist_id: int = Field(foreign_key="playlistsum.playlist_id", primary_key=True)
    playlist: PlaylistSum = Relationship(back_populates="entries")

class PlaylistSumWithVids(PlaylistSumBase):
    """Playlist summary with vids included"""
    entries: list[str]
    playlist_id: Optional[int] = None

class PlaylistStatsBase(SQLModel):
    """Base stats of a playlist run"""
    modified_date: datetime.datetime
    playlist_count: int
    different: Optional[bool]
    success: bool
    download_count: Optional[int] = None
    failed_count: Optional[int] = None
    input_params: Optional[str]  # TODO JSON-encoded dict
    output_params: Optional[str]  # TODO JSON-encoded dict
    timestamp: datetime.datetime
    newest_item: Optional[datetime.datetime]
    interval: Optional[int]

class PlaylistStatsBinHash(PlaylistStatsBase):
    """Playlist stats with binary hash"""
    entries_hash: bytes

class PlaylistStatsStrHash(PlaylistStatsBase):
    """Playlist stats with string hash"""
    entries_hash: str
    sched_id: int
    stat_id: int

class PlaylistStats(PlaylistStatsBinHash, table=True):
    """Stats of a playlist run"""
    sched_id: int = Field(default=None, foreign_key="playlistsched.sched_id")
    stat_id: int | None = Field(primary_key=True, default=None)
    schedule: 'PlaylistSched' = Relationship(back_populates="runs")

class PlaylistSchedBase(SQLModel):
    """A schedule of when to attempt a playlist"""
    # TODO add sched_id?
    extractor_id: Optional[str] = None
    id: Optional[str] = None
    next_run: Optional[datetime.date] = None
    freq_days: Optional[int] = None
    input_params: Optional[str] = None  # TODO JSON-encoded dict
    webpage_url: Optional[str] = None
    lpm_lib: Optional[str] = None
    oi_bucket: Optional[str] = None
    use_cookies: Optional[bool] = None

class PlaylistSched(PlaylistSchedBase, table=True):
    """A schedule of when to attempt a playlist"""
    #__tablename__ = "playlist_scheds"
    sched_id: int | None = Field(primary_key=True, default=None)
    runs: list[PlaylistStats] = Relationship(back_populates="schedule")
    playlist_id: Optional[int] = Field(default=None, foreign_key="playlistsum.playlist_id")

class PlaylistSchedWithStatsAndSum(PlaylistSchedBase):
    """Playlist schedule with stats and summary included"""
    sched_id: int
    runs: list[PlaylistStatsStrHash] = []
    summary: PlaylistSumBase | None = None

class PlaylistSumWithSched(PlaylistSumWithVids):
    """Playlist summary with schedule included"""
    schedules: list[PlaylistSchedBase]

class PlaylistRunResult(BaseModel):
    """Result of a playlist run"""
    summary: PlaylistSumWithVids
    schedule: Optional[PlaylistSched]
    new_stats: Optional[PlaylistStatsStrHash]

class PlaylistRunCreate(BaseModel):
    """Info needed to create a playlist run"""
    playlist: PlaylistFull
    schedule_id: Optional[int] = None
    download_count: int = 0
    failed_count: int = 0
