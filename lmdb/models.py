"""LinkMeddle data models

The thin worker->API "pull" contract (UlChan/VidFull/PlaylistFull), the frozen V4
thing/rel/run schema, and the API I/O views. The worker extracts the pull contract
straight from the raw yt-dlp info dict (run_bknd.extract_pull), so nothing here mirrors
yt-dlp's unstable shape.
"""

import datetime
import uuid
from typing import Optional
from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Index, text
from sqlalchemy.dialects import postgresql
from sqlmodel import Field, SQLModel

# --- The thin "pull" contract (worker -> API) ------------------------------------------
# A Stage-1 playlist pull, reduced to exactly the fields that land in thing/rel plus the
# per-video Stage-2 load-info hint. The worker extracts these straight from the unstable
# yt-dlp info dict (run_bknd.extract_pull), so the API and xform never see raw yt-dlp
# shapes — only this stable contract. Fields the DB never stored (duration, ext, formats,
# categories, ...) are intentionally absent; they remain in the raw blob (run.data_json
# and each video's attrs.info_json) if a future column ever needs them.

class UlChan(BaseModel):
    """Minimal uploader/channel identity for channel fan-out (worker pre-resolves)."""
    url: Optional[str] = None         # best uploader/channel URL (uploader_url or channel_url)
    native_id: Optional[str] = None   # uploader_id or channel_id
    title: Optional[str] = None       # uploader

class VidFull(BaseModel):
    """A discovered video: just what thing/rel need + the Stage-2 load-info hint."""
    url: Optional[str] = None              # webpage_url
    native_id: str                         # yt-dlp entry id (also the pl_hash key)
    extractor_key: Optional[str] = None    # normalized lowercase
    title: Optional[str] = None
    thumbnail_url: Optional[str] = None
    modified: Optional[datetime.datetime] = None   # from timestamp/upload_date
    channel: UlChan = UlChan()
    # Faithful raw yt-dlp entry dict, carried so it can become the Stage-2 load-info hint
    # (attrs.info_json -> process_ie_result). Kept raw because the download needs `formats`.
    info_json: Optional[dict] = None

class PlaylistFull(BaseModel):
    """A discovered playlist + its entries (the POST /jobs/{id}/result body on a pull)."""
    url: str                               # webpage_url
    native_id: Optional[str] = None
    extractor_key: Optional[str] = None
    title: Optional[str] = None
    modified: Optional[datetime.datetime] = None
    playlist_count: Optional[int] = None
    channel: UlChan = UlChan()
    entries: list[VidFull] = []

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
    rating: Optional[str] = None  # grade letter A/B/C (default C); D/F not allowed at add
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
    Stage-2 (video *meta*): `video` is the single-video metadata fetched for a C-band video
    that the flat pull couldn't describe richly enough for a human to rate (no media, no
    `best_oi`) — the meta-job counterpart of `playlist`.
    """
    playlist: Optional[PlaylistFull] = None
    video: Optional[VidFull] = None
    best_oi: Optional[uuid.UUID] = None
    extractor_key: Optional[str] = None
    native_id: Optional[str] = None
    success: bool = True
    data_json: Optional[dict] = None
    input_json: Optional[dict] = None
    worker: Optional[str] = None
