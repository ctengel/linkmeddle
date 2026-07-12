"""Frontend (BFF) view-models.

lmfe is a thin Backend-for-Frontend over the V4 LMDB `thing` API + Object Index.
These models are the SPA-facing shapes; they flatten/rename the LMDB `ThingRead`
(`lmdb.models`) into what a page renders, and add presentation-only fields the
SPA would otherwise compute in JavaScript (the letter `grade`, `file_available`,
the resolved playback `download_url`). This layer owns no durable data and is
explicitly *not* frozen (LM-V4-DESIGN.md §3.3) — it may change freely as the SPA
evolves.
"""
import uuid
from typing import Optional
import datetime
import pydantic
from lmdb import models as pl_models


def grade_for(effective_rating: Optional[float]) -> Optional[str]:
    """Round a numeric effective rating to its letter grade band (§2.4).

    Integer grade values are band centers; round to the nearest integer with ties
    going up (toward the more-positive grade), then map to a letter:
    A=+2 (r>=1.5), B=+1 (r>=0.5), C=0 (r>=-0.5), D=-1 (r>=-1.5), F=-2 (r<-1.5).
    Returns None for an unrated thing (no effective rating).
    """
    if effective_rating is None:
        return None
    if effective_rating >= 1.5:
        return "A"
    if effective_rating >= 0.5:
        return "B"
    if effective_rating >= -0.5:
        return "C"
    if effective_rating >= -1.5:
        return "D"
    return "F"


class ThingSummary(pydantic.BaseModel):
    """Flat per-thing card/row the SPA renders, built from an LMDB `ThingRead`.

    Containers (playlists/channels) and videos share one shape — `container`
    (True/False/None) and `attrs.kind` distinguish them. All render fields are
    denormalized on the thing row, so a list or page needs no per-item OI or
    metadata round-trips (the cure for #123).
    """
    id: uuid.UUID
    url: Optional[str] = None
    title: Optional[str] = None
    channel: Optional[str] = None
    thumbnail_url: Optional[str] = None
    container: Optional[bool] = None
    kind: Optional[str] = None  # attrs.kind display hint (e.g. 'channel')
    extractor_key: Optional[str] = None
    native_id: Optional[str] = None
    human_rating: Optional[float] = None
    machine_rating: Optional[float] = None
    effective_rating: Optional[float] = None
    grade: Optional[str] = None  # letter band of effective_rating (§2.4)
    cookies: Optional[bool] = None  # attrs.cookies soft hint
    lpm_lib: Optional[str] = None   # attrs.lpm_lib soft hint
    try_on: Optional[datetime.date] = None
    last_success_dt: Optional[datetime.datetime] = None
    last_failure_dt: Optional[datetime.datetime] = None
    best_oi: Optional[uuid.UUID] = None
    file_available: bool = False  # best_oi present -> media acquired
    bucket: Optional[str] = None
    modified: Optional[datetime.datetime] = None
    created_dt: Optional[datetime.datetime] = None

    @classmethod
    def from_thing_read(cls, tr: pl_models.ThingRead) -> "ThingSummary":
        """Build from a validated LMDB `ThingRead`.

        The backend computes `effective_rating` on every response (reads and the
        add/patch write paths alike), so the grade reads straight off it — no
        re-derivation here.
        """
        attrs = tr.attrs or {}
        return cls(
            id=tr.id,
            url=tr.url,
            title=tr.title,
            channel=tr.channel,
            thumbnail_url=tr.thumbnail_url,
            container=tr.container,
            kind=attrs.get("kind"),
            extractor_key=tr.extractor_key,
            native_id=tr.native_id,
            human_rating=tr.human_rating,
            machine_rating=tr.machine_rating,
            effective_rating=tr.effective_rating,
            grade=grade_for(tr.effective_rating),
            cookies=attrs.get("cookies"),
            lpm_lib=attrs.get("lpm_lib"),
            try_on=tr.try_on,
            last_success_dt=tr.last_success_dt,
            last_failure_dt=tr.last_failure_dt,
            best_oi=tr.best_oi,
            file_available=tr.best_oi is not None,
            bucket=tr.bucket,
            modified=tr.modified,
            created_dt=tr.created_dt,
        )


class RelatedSummary(pydantic.BaseModel):
    """A neighbor thing one `rel` edge away (from LMDB `RelatedThing`)."""
    direction: str  # 'parent' or 'child' relative to the queried thing
    channel: bool   # True iff the edge's parent is the child's channel/uploader
    thing: ThingSummary


class IndirectChild(pydantic.BaseModel):
    """A video reachable through a sub-container, surfaced when all direct children are containers."""
    container_id: uuid.UUID
    container_title: Optional[str] = None
    channel: bool
    thing: ThingSummary


class OIFileInfo(pydantic.BaseModel):
    """Details of a thing's acquired OI file (flattened from OI's `FileRead`): the file card
    the SPA renders. `mime` picks the preview element (video/audio/img); `extra` is the OI
    tag dict (e.g. ytdl-id/ytdl-extractor) rendered as clickable search chips."""
    mime: Optional[str] = None
    size: Optional[int] = None       # object obj_size, bytes
    checksum: Optional[str] = None   # object checksum, hex
    source_url: Optional[str] = None  # the URL OI recorded as the file's source
    object_uuid: Optional[uuid.UUID] = None
    extra: Optional[dict] = None

    @classmethod
    def from_oi_info(cls, info: Optional[dict]) -> Optional["OIFileInfo"]:
        """Flatten a GET file/{uuid} info dict; None in (no file fetched) -> None out."""
        if not info:
            return None
        obj = info.get("file_object") or {}
        return cls(mime=obj.get("mime"), size=obj.get("obj_size"),
                   checksum=obj.get("checksum"), source_url=info.get("url"),
                   object_uuid=obj.get("uuid"), extra=info.get("extra"))


class ThingPage(ThingSummary):
    """One-call page view-model: a thing, its neighbors, and (for an acquired
    video) a resolved OI playback URL handed straight to the consumer — the data
    path is not proxied."""
    related: list[RelatedSummary] = []
    indirect_children: list[IndirectChild] = []
    download_url: Optional[str] = None
    oi_info: Optional[OIFileInfo] = None


class PlaybackInfo(pydantic.BaseModel):
    """On-demand resolution of a thing's acquired media to a consumer-usable URL."""
    best_oi: Optional[uuid.UUID] = None
    download_url: Optional[str] = None
    object_url: Optional[str] = None
    oi_info: Optional[OIFileInfo] = None


class TagHit(pydantic.BaseModel):
    """An OI file matching a tag search that no LM thing claims (best_oi miss) — the SPA can
    only link out to it."""
    file_uuid: uuid.UUID
    source_url: Optional[str] = None


class TagSearchResult(pydantic.BaseModel):
    """GET /search/tags result: OI files tagged key=value, mapped back to things where LM
    knows them (via best_oi), plus the leftover OI-only hits."""
    things: list[ThingSummary] = []
    unmatched: list[TagHit] = []


class UpcomingJob(pydantic.BaseModel):
    """One dashboard "Upcoming Jobs" row (#193), from LMDB's `JobPreview`: the dispatch-order
    preview. `kind` is 'pull' | 'download' | 'meta'."""
    kind: str
    download: bool = False
    thing: ThingSummary


class PervellamJob(pydantic.BaseModel):
    """One Pervellam (live-stream tool) job for the read-only dashboard panel. `fname` is an
    OI URL once the upload finished (filt=finished guarantees it); before that it's a bare
    local filename. `oi_file` is the OI file UUID parsed off that URL's tail (Pervellam's
    worker writes `{oi_url}file/{uuid}`) — the SPA's route into the OI player page."""
    id: Optional[int] = None
    url: Optional[str] = None
    dler: Optional[str] = None
    fname: Optional[str] = None
    status: Optional[str] = None
    size: Optional[int] = None
    started: Optional[datetime.datetime] = None
    updated: Optional[datetime.datetime] = None
    oi_file: Optional[uuid.UUID] = None

    @pydantic.model_validator(mode="after")
    def _oi_file_from_fname(self):
        if self.oi_file is None and self.fname and self.fname.startswith("http"):
            try:
                self.oi_file = uuid.UUID(self.fname.rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                pass  # not an OI link after all — leave un-navigable
        return self


class ThingCreate(pydantic.BaseModel):
    """Add-a-thing-by-URL (the human entry point). `bucket` is optional here — the
    BFF fills it from OBJIDX_BUCKET_DEFAULT when omitted so GUI/bookmarklet users
    never pick one. Maps to LMDB `ThingAdd`."""
    url: str
    container: Optional[bool] = None
    rating: Optional[float] = pydantic.Field(default=None, ge=0, le=2)
    cookies: Optional[bool] = None
    lpm_lib: Optional[str] = None
    bucket: Optional[str] = None


class RatingPatch(pydantic.BaseModel):
    """Edit a thing: set rating, acknowledge permafail (try_on=null), or edit soft
    hints. Maps 1:1 to LMDB `ThingPatch`."""
    human_rating: Optional[float] = pydantic.Field(default=None, ge=-2, le=2)
    try_on: Optional[datetime.date] = None
    cookies: Optional[bool] = None
    lpm_lib: Optional[str] = None


class RunSummary(pydantic.BaseModel):
    """Slim run for the activity feed / a thing's run history (from LMDB
    `RunActivity` or `RunRead`)."""
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
    success: Optional[bool] = None  # None=in-progress, False=failed, True=done
