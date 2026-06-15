"""LinkMeddle data models

Includes DLP-compat and LM-native
"""

import datetime
import uuid
from typing import Optional
from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Index, text
from sqlalchemy.dialects import postgresql
from sqlmodel import Field, SQLModel

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

# --- V4 schema (thing / rel / run) -----------------------------------------------------
# Frozen 4.0 schema per LM-V4-DESIGN.md Part 2. All datetimes are naive UTC
# (`timestamp`, not `timestamptz`); the app uses UTC everywhere. The canonical DDL is
# mirrored in lmdb/schema/v4.0.sql. 4.x changes must be additive (nullable cols / new
# tables), never migrations.

def naive_utcnow() -> datetime.datetime:
    """Current UTC time as a naive datetime (the V4 convention; see LM-V4-DESIGN.md §2)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# UTC server defaults yield naive `timestamp` values (timezone('utc', now())).
_UTC_NOW = text("(now() at time zone 'utc')")
_UTC_TODAY = text("(now() at time zone 'utc')::date")


class Thing(SQLModel, table=True):
    """The universal entity: playlist, video, or channel [A1, A2, A6, A7]"""
    __table_args__ = (
        Index("thing_native", "backend", "extractor_key", "native_id",
              unique=True, postgresql_where=text("native_id IS NOT NULL")),
        Index("thing_url", "url", unique=True,
              postgresql_where=text("url IS NOT NULL")),
        Index("thing_try_on", "type", "try_on"),
    )
    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(postgresql.UUID(as_uuid=True), primary_key=True))
    url: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    backend: int = Field(
        default=0, sa_column=Column(sa.SmallInteger, nullable=False, server_default=text("0")))
    site: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    extractor_key: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    native_id: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    type: str = Field(sa_column=Column(sa.Text, nullable=False))
    title: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    channel: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    thumbnail_url: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    modified: Optional[datetime.datetime] = Field(
        default=None, sa_column=Column(sa.DateTime, nullable=True))
    human_rating: Optional[float] = Field(
        default=None, sa_column=Column(sa.Float, nullable=True))
    machine_rating: Optional[float] = Field(
        default=None, sa_column=Column(sa.Float, nullable=True))
    last_success_dt: Optional[datetime.datetime] = Field(
        default=None, sa_column=Column(sa.DateTime, nullable=True))
    last_failure_dt: Optional[datetime.datetime] = Field(
        default=None, sa_column=Column(sa.DateTime, nullable=True))
    try_on: Optional[datetime.date] = Field(
        default_factory=lambda: naive_utcnow().date(),
        sa_column=Column(sa.Date, nullable=True, server_default=_UTC_TODAY))
    bucket: str = Field(sa_column=Column(sa.Text, nullable=False))  # OI bucket; required, inherited, immutable [A10]
    best_oi: Optional[uuid.UUID] = Field(  # OI file UUID; set by worker from info['oi_uuid'] [A5-A]
        default=None, sa_column=Column(postgresql.UUID(as_uuid=True), nullable=True))
    attrs: Optional[dict] = Field(
        default=None, sa_column=Column(postgresql.JSONB, nullable=True))
    created_dt: Optional[datetime.datetime] = Field(
        default_factory=naive_utcnow,
        sa_column=Column(sa.DateTime, nullable=False, server_default=_UTC_NOW))


class Rel(SQLModel, table=True):
    """Graph edge between things (playlist<->video, channel<->playlist) [A4]"""
    __table_args__ = (Index("rel_child", "child"),)
    parent: uuid.UUID = Field(
        sa_column=Column(postgresql.UUID(as_uuid=True),
                         ForeignKey("thing.id"), primary_key=True))
    child: uuid.UUID = Field(
        sa_column=Column(postgresql.UUID(as_uuid=True),
                         ForeignKey("thing.id"), primary_key=True))
    type: str = Field(sa_column=Column(sa.Text, primary_key=True))


class Run(SQLModel, table=True):
    """Append-only history of every pull/download attempt + raw yt-dlp JSONB [A9, F2]"""
    __table_args__ = (Index("run_thing", "thing_id", text("starttime DESC")),)
    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(postgresql.UUID(as_uuid=True), primary_key=True))
    thing_id: uuid.UUID = Field(
        sa_column=Column(postgresql.UUID(as_uuid=True),
                         ForeignKey("thing.id"), nullable=False))
    worker: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    input_json: Optional[dict] = Field(
        default=None, sa_column=Column(postgresql.JSONB, nullable=True))
    data_json: Optional[dict] = Field(
        default=None, sa_column=Column(postgresql.JSONB, nullable=True))
    entries_hash: Optional[bytes] = Field(
        default=None, sa_column=Column(sa.LargeBinary, nullable=True))
    playlist_count: Optional[int] = Field(
        default=None, sa_column=Column(sa.Integer, nullable=True))
    starttime: datetime.datetime = Field(
        default_factory=naive_utcnow,
        sa_column=Column(sa.DateTime, nullable=False))
    endtime: Optional[datetime.datetime] = Field(
        default=None, sa_column=Column(sa.DateTime, nullable=True))
    success: Optional[bool] = Field(
        default=None, sa_column=Column(sa.Boolean, nullable=True))


# --- V4 API I/O models -----------------------------------------------------------------

class ThingRead(SQLModel):
    """Public view of a thing (all columns, id as uuid)."""
    id: uuid.UUID
    url: Optional[str] = None
    backend: int = 0
    site: Optional[str] = None
    extractor_key: Optional[str] = None
    native_id: Optional[str] = None
    type: str
    title: Optional[str] = None
    channel: Optional[str] = None
    thumbnail_url: Optional[str] = None
    modified: Optional[datetime.datetime] = None
    human_rating: Optional[float] = None
    machine_rating: Optional[float] = None
    last_success_dt: Optional[datetime.datetime] = None
    last_failure_dt: Optional[datetime.datetime] = None
    try_on: Optional[datetime.date] = None
    bucket: str
    best_oi: Optional[uuid.UUID] = None
    attrs: Optional[dict] = None
    created_dt: Optional[datetime.datetime] = None


class RelatedThing(SQLModel):
    """A thing reachable across one `rel` edge, with the edge's type and direction."""
    direction: str  # 'parent' = the given thing is parent (this is a child), or 'child'
    rel_type: str
    thing: ThingRead


class ThingWithRelated(ThingRead):
    """A thing plus its `rel` neighbors (for ?include=related / page view-models)."""
    related: list[RelatedThing] = []


class RunRead(SQLModel):
    """Public view of a run (entries_hash hex-encoded for JSON)."""
    id: uuid.UUID
    thing_id: uuid.UUID
    worker: Optional[str] = None
    input_json: Optional[dict] = None
    data_json: Optional[dict] = None
    entries_hash: Optional[str] = None
    playlist_count: Optional[int] = None
    starttime: datetime.datetime
    endtime: Optional[datetime.datetime] = None
    success: Optional[bool] = None


class ThingAdd(BaseModel):
    """add-a-thing-by-URL request (the human entry point)."""
    url: str
    bucket: str              # OI storage bucket; required, no server default [A10]
    type: str = 'playlist'   # "unknown -> assume playlist"; overridable
    rating: Optional[str] = None  # grade letter A/B/C (default B); D/F not allowed at add
    cookies: Optional[bool] = None  # soft hint -> attrs.cookies (suggest cookies) [A11]
    lpm_lib: Optional[str] = None   # soft hint -> attrs.lpm_lib (optional library tag) [A11]


class ThingPatch(BaseModel):
    """PATCH a thing: set rating, or acknowledge permafail (try_on=null)."""
    human_rating: Optional[float] = Field(default=None, ge=-2, le=2)  # -2..+2 (§2.4)
    grade: Optional[str] = None          # grade letter alternative to human_rating
    try_on: Optional[datetime.date] = None  # explicit null acknowledges permafail


class ClaimRequest(BaseModel):
    """Body for POST /jobs/claim. 4.x adds self-selection filters (type/extractor/site/
    backend — §4.5); 4.0 only records which worker claimed the job."""
    worker: Optional[str] = None


class JobClaim(BaseModel):
    """Prioritized dispatch result: the single highest-priority due job (§4.5)."""
    run_id: uuid.UUID
    thing: ThingRead
    action: str          # 'pull' (Stage-1 playlist) | 'download' (Stage-2 video)
    cookies: bool = False  # per-job cookies suggestion the worker acts on (hint-only in 4.0) [A11]


class RunResultIn(BaseModel):
    """Worker-owned result push for a run (the V4 rewrite of V3's POST /playlist-run).

    Stage-1 (playlist pull): `playlist` is the LM-native pull result (required on success).
    Stage-2 (video download): `best_oi` is the OI file UUID from the upload (info['oi_uuid']),
    with `extractor_key`/`native_id` for identity backfill. `data_json` carries the raw yt-dlp
    output; `input_json` records the per-run decisions (e.g. whether cookies were used).
    """
    playlist: Optional[PlaylistFull] = None
    best_oi: Optional[uuid.UUID] = None
    extractor_key: Optional[str] = None
    native_id: Optional[str] = None
    success: bool = True
    data_json: Optional[dict] = None
    input_json: Optional[dict] = None
    worker: Optional[str] = None
