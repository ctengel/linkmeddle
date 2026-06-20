"""LinkMeddle data models

The thin worker->API "pull" contract (UlChan/VidFull/PlaylistFull), the frozen V4
thing/rel/run schema, and the API I/O views. The worker extracts the pull contract
straight from the raw yt-dlp info dict (run_bknd.extract_pull), so nothing here mirrors
yt-dlp's unstable shape.
"""

import datetime
import uuid
from typing import Optional
from pydantic import BaseModel, field_validator
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
    """A discovered container member: just what thing/rel need + the load-info hint.

    Usually a leaf video (`container=False`/`None`), but a sub-container member (a
    channel's tab/playlist) is also a `VidFull` with `container=True` — pulled on its
    own later. The split into a separate stub type is gone; `container` is the only
    discriminator.
    """
    url: Optional[str] = None              # webpage_url
    native_id: str                         # yt-dlp entry id (also the pl_hash key)
    extractor_key: Optional[str] = None    # normalized lowercase
    title: Optional[str] = None
    thumbnail_url: Optional[str] = None
    modified: Optional[datetime.datetime] = None   # from timestamp/upload_date
    channel: UlChan = UlChan()
    # False = a known leaf video; None = unknown (a flat url-result the pull can't classify as
    # video-vs-sub-playlist) so the stub's own pull resolves it later (#158).
    container: Optional[bool] = False
    # Faithful raw yt-dlp entry dict, carried so it can become the Stage-2 load-info hint
    # (attrs.info_json -> process_ie_result). Kept raw because the download needs `formats`.
    info_json: Optional[dict] = None

class PlaylistFull(BaseModel):
    """A discovered container (playlist/channel) + its members (POST /jobs/{id}/result body).

    A flat pull lists each member's identity in `entries` as a `VidFull`: leaf videos
    (`container=False`/`None`) and sub-containers (a channel's playlists/tabs,
    `container=True`) alike. The API fans each `container=True` member out into its own
    `container` thing to be pulled later. This is the top-level result envelope; members
    are flat (a `VidFull` never nests a sub-pull).
    """
    url: str                               # webpage_url
    native_id: Optional[str] = None
    extractor_key: Optional[str] = None
    title: Optional[str] = None
    modified: Optional[datetime.datetime] = None
    playlist_count: Optional[int] = None
    channel: UlChan = UlChan()
    entries: list[VidFull] = []            # all members; sub-containers carry container=True

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
    """The universal entity: a container (playlist/channel) or a leaf video [A1, A2, A6, A7].

    `container` is the one structural distinction: True = container (has children, refreshed
    periodically), False = video (leaf, fetched once), NULL = unknown until the first pull
    classifies it. "Channel-ness" is not a type — it lives in the rel table (`rel.channel`,
    "this parent is the child's uploader") plus a soft `attrs.kind='channel'` display hint.
    """
    __table_args__ = (
        Index("thing_native", "backend", "extractor_key", "native_id",
              unique=True, postgresql_where=text("native_id IS NOT NULL")),
        Index("thing_url", "url", unique=True,
              postgresql_where=text("url IS NOT NULL")),
        Index("thing_try_on", "container", "try_on"),
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
    container: Optional[bool] = Field(  # True=playlist/channel, False=video, NULL=unknown
        default=None, sa_column=Column(sa.Boolean, nullable=True))
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
    """Graph edge between things: one parent->child containment, with a `channel` flag [A4].

    `channel=True` means the parent is the child's channel/uploader (the "special parent");
    `False` is plain containment / curated membership. One edge per (parent, child) pair.
    """
    __table_args__ = (Index("rel_child", "child"),)
    parent: uuid.UUID = Field(
        sa_column=Column(postgresql.UUID(as_uuid=True),
                         ForeignKey("thing.id"), primary_key=True))
    child: uuid.UUID = Field(
        sa_column=Column(postgresql.UUID(as_uuid=True),
                         ForeignKey("thing.id"), primary_key=True))
    channel: bool = Field(
        default=False, sa_column=Column(sa.Boolean, nullable=False, server_default=text("false")))


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
    container: Optional[bool] = None
    title: Optional[str] = None
    channel: Optional[str] = None
    thumbnail_url: Optional[str] = None
    modified: Optional[datetime.datetime] = None
    human_rating: Optional[float] = None
    machine_rating: Optional[float] = None
    effective_rating: Optional[float] = None  # COALESCE(human, machine); computed on read (§2.4)
    last_success_dt: Optional[datetime.datetime] = None
    last_failure_dt: Optional[datetime.datetime] = None
    try_on: Optional[datetime.date] = None
    bucket: str
    best_oi: Optional[uuid.UUID] = None
    attrs: Optional[dict] = None
    created_dt: Optional[datetime.datetime] = None


class RelatedThing(SQLModel):
    """A thing reachable across one `rel` edge, with the edge's flag and direction."""
    direction: str  # 'parent' = the given thing is parent (this is a child), or 'child'
    channel: bool   # True iff the edge's parent is the child's channel/uploader
    thing: ThingRead


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

    @field_validator("entries_hash", mode="before")
    @classmethod
    def _hex_entries_hash(cls, v):
        """The Run column is raw bytes; hex-encode it for JSON (None passes through)."""
        return v.hex() if isinstance(v, (bytes, bytearray)) else v


class RunActivity(SQLModel):
    """Slim run + its thing's display fields for the recent-activity feed (§3.1).

    Deliberately omits the heavy data_json/input_json JSONB (RunRead carries those); a feed
    ships only what a dashboard row renders. The run's "action" is derived, not stored, so the
    feed exposes container/best_oi and the FE infers the label.
    """
    id: uuid.UUID
    thing_id: uuid.UUID
    thing_title: Optional[str] = None
    thing_url: Optional[str] = None
    container: Optional[bool] = None
    best_oi: Optional[uuid.UUID] = None
    worker: Optional[str] = None
    playlist_count: Optional[int] = None
    starttime: datetime.datetime
    endtime: Optional[datetime.datetime] = None
    success: Optional[bool] = None


class ThingAdd(BaseModel):
    """add-a-thing-by-URL request (the human entry point)."""
    url: str
    bucket: str              # OI storage bucket; required, no server default [A10]
    # Structural hint set directly on `thing.container`: True = container (playlist/channel),
    # False = video (leaf), omitted -> NULL (unknown; the first pull classifies it). The user
    # need not supply it (#153); channel-ness is discovered (attrs.kind) on the pull, not at add.
    container: Optional[bool] = None
    rating: Optional[float] = Field(default=None, ge=0, le=2)  # -2..+2 numeric; default 0 (C); no D/F at add
    cookies: Optional[bool] = None  # soft hint -> attrs.cookies (suggest cookies) [A11]
    lpm_lib: Optional[str] = None   # soft hint -> attrs.lpm_lib (optional library tag) [A11]


class ThingPatch(BaseModel):
    """PATCH a thing: set rating, acknowledge permafail (try_on=null), or edit soft hints."""
    human_rating: Optional[float] = Field(default=None, ge=-2, le=2)  # -2..+2 numeric (§2.4)
    try_on: Optional[datetime.date] = None  # explicit null acknowledges permafail
    cookies: Optional[bool] = None  # soft hint -> attrs.cookies (null clears it) [A11]
    lpm_lib: Optional[str] = None   # soft hint -> attrs.lpm_lib (null clears it) [A11]
    # Structural classification: NULL->True/False is allowed (first/affirming), switching a
    # set value (True<->False) is a 409. Omitted/null leaves it unchanged.
    container: Optional[bool] = None


class ClaimRequest(BaseModel):
    """Body for POST /jobs/claim. 4.x adds self-selection filters (type/extractor/site/
    backend — §4.5); 4.0 only records which worker claimed the job."""
    worker: Optional[str] = None


class JobClaim(BaseModel):
    """Prioritized dispatch result: the single highest-priority due job (§4.5).

    One worker code path for every job: the worker always extracts as flat as possible
    (`extract_flat='in_playlist'`, a no-op on a single video) and shapes its result body from
    what yt-dlp returned (playlist fan-out vs single `video`). `download` is the only knob:
    True means acquire media + upload to OI and set `best_oi` (a video assessing >= B band);
    False means metadata only (a container pull, or a C-band video the flat pull
    under-described). The server decides the flag from the thing's container/rating (§4.2)."""
    run_id: uuid.UUID
    thing: ThingRead
    download: bool = False  # acquire media (>= B video); else metadata-only pull/enrich
    cookies: bool = False  # per-job cookies suggestion the worker acts on (hint-only in 4.0) [A11]


class RunResultIn(BaseModel):
    """Worker-owned result push for a run (the V4 rewrite of V3's POST /playlist-run).

    Stage-1 (playlist pull): `playlist` is the LM-native pull result (required on success).
    Stage-2 (video download): `video` is the full single-video extract (display + identity +
    channel) and `best_oi` is the OI file UUID from the upload (info['oi_uuid']). `data_json`
    carries the raw yt-dlp output; `input_json` records the per-run decisions (e.g. cookies).
    Stage-2 (video *meta*): `video` is the single-video metadata fetched for a C-band video
    that the flat pull couldn't describe richly enough for a human to rate (no media, no
    `best_oi`) — the meta-job counterpart of `playlist`.
    """
    playlist: Optional[PlaylistFull] = None
    video: Optional[VidFull] = None
    best_oi: Optional[uuid.UUID] = None
    success: bool = True
    data_json: Optional[dict] = None
    input_json: Optional[dict] = None
    worker: Optional[str] = None
