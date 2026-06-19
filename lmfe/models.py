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


class ThingPage(ThingSummary):
    """One-call page view-model: a thing, its neighbors, and (for an acquired
    video) a resolved OI playback URL handed straight to the consumer — the data
    path is not proxied."""
    related: list[RelatedSummary] = []
    download_url: Optional[str] = None


class PlaybackInfo(pydantic.BaseModel):
    """On-demand resolution of a thing's acquired media to a consumer-usable URL."""
    best_oi: Optional[uuid.UUID] = None
    download_url: Optional[str] = None
    object_url: Optional[str] = None


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
