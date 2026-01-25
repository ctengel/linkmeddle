"""LinkMeddle data models

Includes DLP-compat and LM-native
"""

import datetime
from typing import Optional, List
from pydantic import BaseModel
from sqlmodel import Field, Relationship, SQLModel

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
    #__tablename__ = "playlist_sums"
    playlist_id: int | None =Field(primary_key=True, default=None)
    entries: List['PlaylistVid'] = Relationship(back_populates="playlist")

class PlaylistVid(SQLModel, table=True):
    """Link between vids and playlists"""
    vid_id: str = Field(primary_key=True)
    playlist_id: int = Field(foreign_key="playlistsum.playlist_id", primary_key=True)
    playlist: PlaylistSum = Relationship(back_populates="entries")

class PlayylistSumWithVids(PlaylistSumBase):
    """Playlist summary with vids included"""
    entries: list[str]

class PlaylistStatsBase(SQLModel):
    """Base stats of a playlist run"""
    # TODO failure or success count?
    modified_date: datetime.datetime
    playlist_count: int
    entries_hash: bytes
    different: Optional[bool]
    success: bool
    download_count: int
    input_params: Optional[str]  # TODO JSON-encoded dict
    output_params: Optional[str]  # TODO JSON-encoded dict
    timestamp: datetime.datetime
    newest_item: datetime.datetime
    interval: Optional[int]


class PlaylistStats(PlaylistStatsBase, table=True):
    """Stats of a playlist run"""
    sched_id: int = Field(default=None, foreign_key="playlistsched.sched_id")
    # TODO consider composite key of sched_id + timestamp
    stat_id: int | None = Field(primary_key=True, default=None)
    schedule: 'PlaylistSched' = Relationship(back_populates="runs")

class PlaylistSchedBase(SQLModel):
    """A scedule of when to attempt a playlist"""
    extractor_id: str
    id: str
    next_run: datetime.date
    freq_days: int
    input_params: str  # TODO JSON-encoded dict
    webpage_url: str

class PlaylistSched(PlaylistSchedBase, table=True):
    """A schedule of when to attempt a playlist"""
    #__tablename__ = "playlist_scheds"
    sched_id: int | None = Field(primary_key=True, default=None)
    runs: list[PlaylistStats] = Relationship(back_populates="schedule")
    playlist_id: Optional[int] = Field(default=None, foreign_key="playlistsum.playlist_id")

#class PlaylistSchedWithStats(PlaylistSched):
#    """Playlist schedule with stats included"""
#    # TODO consider collapsing with PlaylistSched

class PlaylistSchedWithStatsAndSum(PlaylistSchedBase):
    """Playlist schedule with stats and summary included"""
    sched_id: int
    runs: list[PlaylistStatsBase] = []
    summary: PlaylistSumBase | None = None

class PlaylistSumWithSched(PlayylistSumWithVids):
    """Playlist summary with schedule included"""
    schedules: list[PlaylistSchedBase]

class PlaylistRunResult(BaseModel):
    """Result of a playlist run"""
    summary: PlayylistSumWithVids
    schedule: Optional[PlaylistSched]
    new_stats: Optional[PlaylistStats]
