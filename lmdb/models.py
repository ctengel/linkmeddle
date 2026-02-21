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
    extractor_key: Optional[str] = None
    extractor: Optional[str] = None
    id: str # TODO should this be Optional?
    original_url: Optional[str] = None
    playlist_count: Optional[int] = None
    title: Optional[str] = None
    uploader_id: Optional[str] = None
    uploader: Optional[str] = None
    uploader_url: Optional[str] = None
    webpage_url_basename: Optional[str] = None
    webpage_url_domain: Optional[str] = None
    webpage_url: Optional[str] = None


class PlVidDLP(CommonDLP):
    """DLP: A vid as seen as a playlist entry"""
    categories: list[str] = []
    channel: Optional[str] = None
    display_id: Optional[str] = None
    duration: Optional[int] = None
    epoch: Optional[int] = None  # NOTE this is just NOW
    ext: Optional[str] = None
    format_id: Optional[str] = None
    format: Optional[str] = None
    fulltitle: Optional[str] = None
    _has_drm: Optional[bool] = False
    height: Optional[int] = None
    is_live: Optional[bool] = None
    language: Optional[str] = None
    live_status: Optional[str] = None
    n_entries: Optional[int] = None
    playlist_autonumber: Optional[int] = None
    playlist_channel_id: Optional[str] = None
    playlist_id: Optional[str] = None
    playlist_index: Optional[int] = None
    playlist: Optional[str] = None
    playlist_uploader_id: Optional[str] = None
    playlist_uploader: Optional[str] = None
    playlist_webpage_url: Optional[str] = None
    protocol: Optional[str] = None
    thumbnail: Optional[str] = None
    timestamp: Optional[int] = None
    upload_date: Optional[str] = None  # YYYYMMDD
    was_live: Optional[bool] = None
    width: Optional[int] = None


class DLPVersion(BaseModel):
    """DLP version info"""
    version: str
    current_git_head: Optional[str] = None
    release_git_head: str
    repository: str

class PlaylistDLP(CommonDLP):
    """A DLP root playlist"""
    entries: list['PlVidDLP | PlaylistDLP | None'] = []
    epoch: int  # NOTE this is just NOW
    modified_date: Optional[str] = None  # YYYYMMDD
    _type: str  # "playlist
    _version: DLPVersion

class UlChan(BaseModel):
    """Uploader/Channel description"""
    channel_id: Optional[str] = None
    uploader_id: Optional[str] = None
    uploader: Optional[str] = None
    channel_url: Optional[str] = None
    uploader_url: Optional[str] = None

class DLPIE(BaseModel):
    """DLP extractor used"""
    extractor_key: Optional[str] = None
    extractor: Optional[str] = None

class VidFull(BaseModel):
    """LM-native full video"""
    channel: UlChan
    description: Optional[str] = None
    extractor: DLPIE
    id: str
    title: Optional[str] = None
    webpage_url: Optional[str] = None
    categories: list[str] = []
    duration: Optional[int] = None
    ext: Optional[str] = None
    format: Optional[str] = None
    height: Optional[int] = None
    is_live: Optional[bool] = None
    language: Optional[str] = None
    n_entries: Optional[int] = None
    thumbnail: Optional[str] = None
    upload_date:  Optional[datetime.datetime] = None
    was_live: Optional[bool] = None
    width: Optional[int] = None

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
    playlist_id: int | None = Field(primary_key=True, default=None)
    entries: List['PlaylistVid'] = Relationship(back_populates="playlist")

class PlaylistVid(SQLModel, table=True):
    """Link between vids and playlists"""
    vid_id: str = Field(primary_key=True)
    playlist_id: int = Field(foreign_key="playlistsum.playlist_id", primary_key=True)
    playlist: PlaylistSum = Relationship(back_populates="entries")
    extractor_id: str = Field(primary_key=True)

class PlaylistSumPublic(PlaylistSumBase):
    """Public view of a playlist summary"""
    playlist_id: Optional[int] = None

class PlaylistSumWithVids(PlaylistSumPublic):
    """Playlist summary with vids included"""
    entries: list[str]

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

class PlaylistSchedPublic(PlaylistSchedBase):
    """Public view of a playlist schedule"""
    sched_id: int

class PlaylistSchedWithStatsAndSum(PlaylistSchedPublic):
    """Playlist schedule with stats and summary included"""
    runs: list[PlaylistStatsStrHash] = []
    summary: PlaylistSumPublic | None = None

class PlaylistSumWithSched(PlaylistSumWithVids):
    """Playlist summary with schedule included"""
    schedules: list[PlaylistSchedPublic]

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
