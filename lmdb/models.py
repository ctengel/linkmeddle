"""LinkMeddle data models

The thin worker->API "pull" contract (UlChan/PullThing), the frozen V4
thing/rel/run schema, and the API I/O views. The worker extracts the pull contract
straight from the raw yt-dlp info dict (run_bknd.extract_node), so nothing here mirrors
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
# yt-dlp info dict (run_bknd.extract_node), so the API and xform never see raw yt-dlp
# shapes — only this stable contract. Fields the DB never stored (duration, ext, formats,
# categories, ...) are intentionally absent; they remain in the raw blob (run.data_json
# and each video's attrs.info_json) if a future column ever needs them.

class UlChan(BaseModel):
    """Minimal uploader/channel identity for channel fan-out (worker pre-resolves)."""
    url: Optional[str] = None         # best uploader/channel URL (uploader_url or channel_url)
    native_id: Optional[str] = None   # uploader_id or channel_id
    # Raw channel_id, carried alongside because yt-dlp's uploader_id and channel_id are
    # different namespaces (youtube: @handle vs UC…) and a container's own id can match either
    # (both shapes seen live) — self-ownership tests (xform.owns_native_id) must check both.
    channel_id: Optional[str] = None
    title: Optional[str] = None       # uploader

class PullThing(BaseModel):
    """One discovered node — the pull-contract counterpart of the DB `Thing`.

    A single recursive model for every member shape: a leaf video (`container=False`), a
    sub-container (a channel's tab/playlist, `container=True`), an unknown flat url-result
    (`container=None`, classified on its own pull, #158), or the top-level container itself.
    The old VidFull/PlaylistFull split was misleading — a sub-playlist was already sent as a
    `VidFull` with `container=True` — so `container` is the only discriminator now.

    `entries` is normally empty (a flat pull lists members one level deep), but when yt-dlp
    inlines a sub-playlist's own members they are carried here verbatim so the API can ingest
    them instead of dropping them. Likewise `info_json` is the raw yt-dlp dict kept verbatim
    (entries included): it becomes a leaf's Stage-2 load-info hint (needs real `formats`) and,
    for an inlined sub-container, the recorded `data_json` of its synthetic run.
    """
    url: Optional[str] = None              # webpage_url (or flat-entry url)
    native_id: Optional[str] = None        # yt-dlp id (the pl_hash key for leaves)
    extractor_key: Optional[str] = None    # normalized lowercase
    title: Optional[str] = None
    thumbnail_url: Optional[str] = None
    modified: Optional[datetime.datetime] = None   # from timestamp/upload_date
    playlist_count: Optional[int] = None   # containers only
    channel: UlChan = UlChan()
    # False = a known leaf video; True = a container (playlist/channel); None = unknown (a flat
    # url-result the pull can't classify as video-vs-sub-playlist) resolved on its own pull (#158).
    container: Optional[bool] = False
    info_json: Optional[dict] = None       # raw yt-dlp dict, verbatim (see class docstring)
    entries: list["PullThing"] = []        # members, when yt-dlp inlined them (else empty)

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


# --- V4.x additions: tagging (#126) ----------------------------------------------------
# Copied verbatim from v5-design models_v5.py — delete the duplicates there on the V5
# merge. Only the human slice (source='human') is written in 4.x; the source/confidence
# columns are kept so V5's ML suggester writes machine rows into the same tables.

class Tag(SQLModel, table=True):
    """Free-form tag vocabulary (human-created)."""
    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(postgresql.UUID(as_uuid=True), primary_key=True))
    name: str = Field(sa_column=Column(sa.Text, nullable=False, unique=True))
    created_dt: Optional[datetime.datetime] = Field(
        default_factory=naive_utcnow,
        sa_column=Column(sa.DateTime, nullable=False, server_default=_UTC_NOW))


class ThingTag(SQLModel, table=True):
    __tablename__ = "thing_tag"
    thing_id: uuid.UUID = Field(
        sa_column=Column(postgresql.UUID(as_uuid=True),
                         ForeignKey("thing.id"), primary_key=True))
    tag_id: uuid.UUID = Field(
        sa_column=Column(postgresql.UUID(as_uuid=True),
                         ForeignKey("tag.id"), primary_key=True, index=True))
    source: str = Field(
        default="human",
        sa_column=Column(sa.Text, nullable=False, server_default=text("'human'")))
    confidence: Optional[float] = Field(
        default=None, sa_column=Column(sa.Float, nullable=True))
    created_dt: Optional[datetime.datetime] = Field(
        default_factory=naive_utcnow,
        sa_column=Column(sa.DateTime, nullable=False, server_default=_UTC_NOW))


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


class Facet(BaseModel):
    """One GET /things/facets row: an extractor and how many things carry it.

    View-model only (not a table). `extractor_key` is None for things no extractor has
    identified yet (never pulled, or added by bare URL)."""
    extractor_key: Optional[str] = None
    count: int


class TagRead(BaseModel):
    """One tag on a thing (a thing_tag row joined to its tag's name)."""
    name: str
    source: str = "human"  # 'human' | 'machine' (machine rows arrive with V5)
    confidence: Optional[float] = None  # NULL for human-set
    created_dt: Optional[datetime.datetime] = None


class TagAssign(BaseModel):
    """PUT /things/{id}/tags body: tag names to assert (additive, create-on-assign)."""
    names: list[str]


class TagCreate(BaseModel):
    """POST /tags/ body: one vocabulary entry (usually implicit via assign)."""
    name: str


class TagFacet(BaseModel):
    """One GET /tags/ row: a vocabulary tag and how many things carry it."""
    name: str
    count: int


class ClaimRequest(BaseModel):
    """Body for POST /jobs/claim. `worker` records which runner claimed the job; `extractor`/
    `no_extractor` are worker self-selection (§4.5) — either pin this worker to one extractor's
    jobs, or (mutually exclusive with `extractor`) claim only things no extractor has identified
    yet (`extractor_key IS NULL`, #210). The remaining self-selection filters (type/site/backend
    — §4.5) stay 4.x."""
    worker: Optional[str] = None
    extractor: Optional[str] = None  # self-selection: only claim this extractor's jobs (§4.5)
    no_extractor: bool = False  # self-selection: only claim things with extractor_key IS NULL (#210)


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


class JobPreview(BaseModel):
    """One row of GET /jobs/upcoming (#193): what dispatch *would* hand out, in order.

    A read-only view-model (not a table): the same eligibility predicate and ordering as
    POST /jobs/claim, but nothing is locked or claimed — no run is created. `kind` names the
    §4.2 branch the thing matched: 'pull' (Stage-1 container/unknown), 'download' (>= B video
    acquire), or 'meta' (C-band video metadata-only enrich)."""
    thing: ThingRead
    download: bool = False  # same knob claim would send (True only for kind='download')
    kind: str  # 'pull' | 'download' | 'meta'


class RunResultIn(BaseModel):
    """Worker-owned result push for a run (the V4 rewrite of V3's POST /playlist-run).

    Stage-1 (playlist pull): `playlist` is the LM-native pull result (required on success).
    Stage-2 (video download): `video` is the full single-video extract (display + identity +
    channel) and `best_oi` is the OI file UUID from the upload (info['oi_uuid']). `data_json`
    carries the raw yt-dlp output; `input_json` records the per-run decisions (e.g. cookies).
    Stage-2 (video *meta*): `video` is the single-video metadata fetched for a C-band video
    that the flat pull couldn't describe richly enough for a human to rate (no media, no
    `best_oi`) — the meta-job counterpart of `playlist`.

    Both fields are `PullThing` (the unified node); they stay separate to drive the
    Stage-1-vs-Stage-2 dispatch and the both-shape guard in the endpoint.
    """
    playlist: Optional[PullThing] = None
    video: Optional[PullThing] = None
    best_oi: Optional[uuid.UUID] = None
    success: bool = True
    data_json: Optional[dict] = None
    input_json: Optional[dict] = None
    worker: Optional[str] = None
