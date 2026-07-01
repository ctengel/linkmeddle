"""LMDB API (V4): thing/rel/run CRUD + add-a-thing-by-URL + job dispatch/ingest.

The thing-centric surface from LM-V4-DESIGN.md §3.3. Everything is a `thing`
(playlist / video / channel). The `/jobs/...` endpoints are the Phase-1 fan-out core:
`POST /jobs/claim` is the prioritized dispatch (§4.2/§4.5) and `POST /jobs/{run_id}/result`
is the Stage-1 ingest (§3.3). URL-classify is deferred to 4.x: `POST /things/` just records
the URL; the worker fills extractor/native_id/real type on result ingest later.

Note the SQLModel select gotcha (LM-V4-DESIGN.md §6.4): filters on nullable columns
use SQL `== None` / `!= None`, never Python `is None` (which silently evaluates wrong).
"""

import os
import uuid
import datetime
from typing import Optional
from contextlib import asynccontextmanager
import sqlalchemy as sa
from sqlalchemy import func, or_, case
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert as pg_insert
from fastapi import FastAPI, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, create_engine, select
from . import models, xform
from .models import (Thing, Rel, Run, ThingRead, RelatedThing,
                     RunRead, RunActivity, ThingAdd, ThingPatch, ClaimRequest, JobClaim,
                     RunResultIn)

# Effective-rating floor for fetching a video's *media* (Stage-2 download); below it (C band)
# a video only gets a metadata-only `meta` job. The §2.4 band boundaries live in xform.
_VIDEO_DOWNLOAD_FLOOR = xform.BAND_FLOOR["B"]
# Run-eligibility floor for playlists/other (the C band): below it nothing is dispatched.
_PLAYLIST_FLOOR = xform.BAND_FLOOR["C"]
# Claim lease: a thing with an in-progress run newer than this is excluded from dispatch, so
# concurrent workers never double-claim it (§4.5). A full day, consistent with the minimum
# `try_on` backoff granularity (dispatch works in whole days, `try_on <= today`): a worker that
# crashes hard (kill -9, which `report_failure` can't catch) leaves a zombie in-progress run,
# and the lease lets the thing return on the next day — the same cadence a backed-off thing would.
CLAIM_LEASE = datetime.timedelta(days=1)

# Hybrid scheduling grace period (§4.x). A sub-container refreshed via its parent's pull
# (parent-fed: inlined in the parent's single yt-dlp call, no run of its own) carries
# `try_on = parent.try_on + SAFETY_MARGIN_DAYS`, so the parent normally re-pulls — and re-feeds —
# it first (fewer yt-dlp calls); the child self-pulls only if the parent is this many days overdue.
SAFETY_MARGIN_DAYS = datetime.timedelta(days=7)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg:///lmdb")
engine = create_engine(DATABASE_URL, echo=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create DB tables on startup"""
    SQLModel.metadata.create_all(engine)
    yield


app = FastAPI(title="LinkMeddle LMDB API", lifespan=lifespan)


def get_session():
    """get a DB session"""
    with Session(engine) as session:
        yield session


def _today() -> datetime.date:
    """Today in UTC (the V4 datetime convention)."""
    return models.naive_utcnow().date()


def _machine_rating_expr():
    """SQL machine rating (§2.4, compute-on-read, Task 2.2): MAX of every parent container's
    human rating (video and container alike); a container with no human-rated parent falls back
    to the AVG of its directly-human-rated children. One hop each side, NULL when nothing applies.

    Reads only *human* ratings one hop away, so video and container never reference each other's
    machine rating -- no recursion. The stored `machine_rating` column is intentionally unread.
    """
    parent, child = aliased(Thing), aliased(Thing)
    parent_mr = (select(func.max(parent.human_rating))
                 .where(Rel.child == Thing.id, Rel.parent == parent.id)
                 .correlate(Thing).scalar_subquery())
    child_mr = (select(func.avg(child.human_rating))
                .where(Rel.parent == Thing.id, Rel.child == child.id,
                       child.human_rating != None)  # noqa: E711
                .correlate(Thing).scalar_subquery())
    return case((Thing.container == False, parent_mr),  # noqa: E712  video: parents only
                (Thing.container == True, func.coalesce(parent_mr, child_mr)),  # noqa: E712
                else_=None)


def _effective_rating_expr(default=None):
    """SQL effective rating = COALESCE(human, machine[, default]) (§2.4)."""
    args = [Thing.human_rating, _machine_rating_expr()]
    if default is not None:
        args.append(default)
    return func.coalesce(*args)


def _effective(human: Optional[float], machine: Optional[float],
               default: Optional[float] = None) -> Optional[float]:
    """COALESCE(human, machine, default) (§2.4): human wins, else machine, else default."""
    if human is not None:
        return human
    if machine is not None:
        return machine
    return default


def _machine_rating_value(session: Session, thing: Thing) -> Optional[float]:
    """Computed machine rating for one thing (None if no human-rated relatives apply)."""
    return session.exec(select(_machine_rating_expr()).where(Thing.id == thing.id)).one()


def _effective_rating_value(session: Session, thing: Thing) -> float:
    """Effective rating of a thing instance = human, else computed machine, else 0.0 (C).

    The instance form of `_effective_rating_expr(default=0.0)`, for callers that hold a loaded
    `thing` and need its rating for a Python decision (dispatch action, backoff band, §2.4).
    """
    return _effective(thing.human_rating, _machine_rating_value(session, thing), 0.0)


def _read_with_ratings(thing: Thing, machine: Optional[float]) -> ThingRead:
    """ThingRead with computed machine/effective ratings. Human rating is authoritative: when
    present, machine is treated as NULL and effective is the human rating (§2.4)."""
    tr = ThingRead.model_validate(thing)
    tr.machine_rating = None if thing.human_rating is not None else machine
    tr.effective_rating = _effective(thing.human_rating, machine)
    return tr


def _read_thing(session: Session, thing: Thing) -> ThingRead:
    """ThingRead for a loaded thing, computing its machine/effective ratings on read.

    The single read-projection shared by every endpoint that returns one thing (get/add/patch),
    so add/patch responses carry the same computed ratings as a GET (not the raw, unrated row)."""
    return _read_with_ratings(thing, _machine_rating_value(session, thing))


def _wants_download(thing: Thing, eff_rating: float) -> bool:
    """Whether the worker should acquire media for this claimed thing (§4.5 dispatch result).

    True only for a leaf video (`container is False`) whose effective rating clears the B floor
    -- the Stage-2 media+metadata download. Everything else is metadata-only: a container/unknown
    pull (Stage-1 fan-out; an unknown URL is classified by the result), or a C-band video the
    flat pull under-described (Stage-2 metadata-only enrichment). The worker uses one extraction
    path either way (flat, a no-op on a single video); this flag just gates the media download.
    """
    return thing.container is False and eff_rating >= _VIDEO_DOWNLOAD_FLOOR


def _set_try_on(session: Session, thing: Thing) -> None:
    """Advance thing.try_on from its run history via the Fibonacci backoff (§4.4, Task 1.4).

    Re-queries the thing's runs (the just-recorded run is autoflushed in, so it counts). The
    backoff band uses the effective rating, so a machine-rated thing schedules correctly.
    """
    runs = session.exec(select(Run).where(Run.thing_id == thing.id)).all()
    thing.try_on = xform.next_try_on(_effective_rating_value(session, thing), runs)


def get_thing_or_404(session: Session, thing_id: uuid.UUID) -> Thing:
    """Fetch a thing by id or raise 404"""
    thing = session.get(Thing, thing_id)
    if not thing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thing not found")
    return thing


def _finish(session: Session, run: Run) -> RunRead:
    """Commit the in-progress run and return its serialized view (the submit_result tail)."""
    session.add(run)
    session.commit()
    session.refresh(run)
    return RunRead.model_validate(run)


def _related(session: Session, thing_id: uuid.UUID,
             direction: Optional[str]) -> list[RelatedThing]:
    """rel neighbors in both directions (or one if direction is 'child'/'parent'), each with
    its computed machine/effective rating (the subquery correlates to the neighbor, §2.4)."""
    machine = _machine_rating_expr().label("machine_rating")
    out: list[RelatedThing] = []
    if direction in (None, "child"):
        for rel, thing, mr in session.exec(
                select(Rel, Thing, machine).where(
                    Rel.parent == thing_id, Rel.child == Thing.id)).all():
            out.append(RelatedThing(direction="child", channel=rel.channel,
                                    thing=_read_with_ratings(thing, mr)))
    if direction in (None, "parent"):
        for rel, thing, mr in session.exec(
                select(Rel, Thing, machine).where(
                    Rel.child == thing_id, Rel.parent == Thing.id)).all():
            out.append(RelatedThing(direction="parent", channel=rel.channel,
                                    thing=_read_with_ratings(thing, mr)))
    return out


# --- Things ---------------------------------------------------------------------------

def _set_container_hint(thing: Thing, proposed: Optional[bool]) -> None:
    """Apply a user container hint: NULL->value (first classification) or affirm same value;
    a switch (True<->False) of a set value is a 409. A None/omitted hint is a no-op."""
    if proposed is None:
        return
    if xform.container_switch(thing.container, proposed):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="container already set to a different value")
    thing.container = proposed


@app.post("/things/", response_model=ThingRead, status_code=status.HTTP_201_CREATED)
def add_thing(item: ThingAdd, response: Response, session: Session = Depends(get_session)):
    """Add a thing by URL (the human entry point).

    Stores the URL with a default numeric rating of 0.0 / C (override 1.0/2.0; the `ge=0`
    bound rejects D/F — you don't add to suppress). `container` is an optional structural hint
    set directly on the row (True/False, omitted -> NULL = unknown, classified on first pull,
    #153); channel-ness is discovered (attrs.kind) on the pull, not declared here. `bucket`
    (OI storage home) is required — no server default ([A10]). Optional `cookies`/`lpm_lib`
    are stored as soft hints in `attrs` ([A11]). extractor_key/native_id are filled in later by
    the worker. Idempotent on URL: returns the existing thing as-is with 200 (use PATCH to modify).
    """
    existing = session.exec(select(Thing).where(Thing.url == item.url)).one_or_none()
    if existing:
        response.status_code = status.HTTP_200_OK
        return _read_thing(session, existing)
    attrs = {k: getattr(item, k) for k in xform._PROPAGATE_HINTS if getattr(item, k) is not None}
    thing = Thing(url=item.url, container=item.container,
                  human_rating=item.rating if item.rating is not None else 0.0,
                  bucket=item.bucket, attrs=attrs or None)
    session.add(thing)
    try:
        session.commit()
    except IntegrityError:  # lost a race on the UNIQUE(url) index (#142)
        session.rollback()
        existing = session.exec(select(Thing).where(Thing.url == item.url)).one_or_none()
        if existing is None:
            raise
        response.status_code = status.HTTP_200_OK
        return _read_thing(session, existing)
    session.refresh(thing)
    return _read_thing(session, thing)


@app.get("/things/", response_model=list[ThingRead])
def list_things(container: Optional[bool] = None, kind: Optional[str] = None,
                rating: Optional[float] = None, min_rating: Optional[float] = None,
                due: bool = False, needs_rating: bool = False, new: bool = False,
                failing: bool = False, url: Optional[str] = None,
                extractor: Optional[str] = None, native_id: Optional[str] = None,
                session: Session = Depends(get_session)):
    """List/search things. Backs every list view + the status dashboard.

    `container` filters the structural boolean (True=container, False=video); `kind` filters
    the `attrs.kind` display hint (e.g. `kind=channel`). `extractor` + `native_id` is the V4
    replacement for V3 GET /videos/{ex}/{id} (#102). `rating` filters the exact *human* rating;
    `min_rating` filters the *effective* rating (human else computed machine, §2.4) at or above
    a numeric threshold — e.g. `min_rating=1.0` returns everything effectively B-or-better.
    """
    stmt = select(Thing, _machine_rating_expr().label("machine_rating"))
    if container is not None:
        stmt = stmt.where(Thing.container == container)
    if kind is not None:
        stmt = stmt.where(Thing.attrs["kind"].astext == kind)
    if url is not None:
        stmt = stmt.where(Thing.url == url)
    if extractor is not None:
        stmt = stmt.where(Thing.extractor_key == extractor.lower())
    if native_id is not None:
        stmt = stmt.where(Thing.native_id == native_id)
    if rating is not None:
        stmt = stmt.where(Thing.human_rating == rating)
    if min_rating is not None:
        stmt = stmt.where(_effective_rating_expr(default=0.0) >= min_rating)
    if needs_rating:
        stmt = stmt.where(Thing.human_rating == None)  # noqa: E711  (SQL IS NULL)
    if due:
        stmt = stmt.where(Thing.try_on != None, Thing.try_on <= _today())  # noqa: E711
    if failing:
        stmt = stmt.where(  # noqa: E711
            Thing.last_failure_dt != None,
            (Thing.last_success_dt == None) | (Thing.last_failure_dt > Thing.last_success_dt))
    if new:
        cutoff = models.naive_utcnow() - datetime.timedelta(days=7)
        stmt = stmt.where(Thing.created_dt >= cutoff)
    if needs_rating:
        # Container/unknown (container IS NOT False) before videos, then most-neutral
        # machine rating first (NULL treated as neutral 0.0), newest as final tiebreak.
        stmt = stmt.order_by(
            sa.desc(Thing.container.isnot(False)),
            func.abs(func.coalesce(_machine_rating_expr(), 0.0)).asc(),
            Thing.created_dt.desc())
    else:
        stmt = stmt.order_by(Thing.created_dt.desc())
    return [_read_with_ratings(thing, machine) for thing, machine in session.exec(stmt).all()]


@app.get("/things/{thing_id}", response_model=ThingRead)
def get_thing(thing_id: uuid.UUID, session: Session = Depends(get_session)):
    """Get one thing; fetch its rel neighbors separately via GET /things/{id}/related."""
    thing = get_thing_or_404(session, thing_id)
    return _read_thing(session, thing)


@app.get("/things/{thing_id}/related", response_model=list[RelatedThing])
def get_related(thing_id: uuid.UUID, direction: Optional[str] = None,
                session: Session = Depends(get_session)):
    """rel neighbors of a thing (children + parents; narrow with ?direction=)."""
    get_thing_or_404(session, thing_id)
    return _related(session, thing_id, direction)


@app.get("/things/{thing_id}/runs", response_model=list[RunRead])
def get_thing_runs(thing_id: uuid.UUID, session: Session = Depends(get_session)):
    """Run history for a thing, newest first."""
    get_thing_or_404(session, thing_id)
    runs = session.exec(
        select(Run).where(Run.thing_id == thing_id).order_by(Run.starttime.desc())).all()
    return [RunRead.model_validate(r) for r in runs]


@app.get("/runs/", response_model=list[RunActivity])
def list_runs(limit: int = 50, success: Optional[bool] = None,
              in_progress: bool = False, session: Session = Depends(get_session)):
    """Global recent-activity feed: every thing's runs newest-first (§3.1 "recent activity").

    Backs the status dashboard's activity panel (new things / failures are served by
    GET /things/?new and ?failing). The default feed includes active/in-progress runs inline
    (success IS NULL, endtime IS NULL). `success` filters to failures (false) or completed
    (true); `in_progress` narrows to only the claimed-but-unfinished runs.
    """
    limit = max(1, min(limit, 200))
    stmt = select(Run, Thing).where(Run.thing_id == Thing.id)
    if in_progress:
        stmt = stmt.where(Run.success == None)  # noqa: E711  (SQL IS NULL)
    elif success is not None:
        stmt = stmt.where(Run.success == success)  # noqa: E712
    stmt = stmt.order_by(Run.starttime.desc()).limit(limit)
    return [RunActivity(id=r.id, thing_id=r.thing_id, thing_title=t.title, thing_url=t.url,
                        container=t.container, best_oi=t.best_oi, worker=r.worker,
                        playlist_count=r.playlist_count, starttime=r.starttime,
                        endtime=r.endtime, success=r.success)
            for r, t in session.exec(stmt).all()]


@app.patch("/things/{thing_id}", response_model=ThingRead)
def patch_thing(thing_id: uuid.UUID, item: ThingPatch,
                session: Session = Depends(get_session)):
    """Update a thing: set the numeric rating (incl. D/F), or ack permafail (try_on=null).

    Raising the human rating to an eligible level re-opens the date gate
    (`try_on = today`, guarded by `best_oi IS NULL`) — resurrecting a permafail or pulling
    a future-scheduled thing forward (§2.5, Task 2.1). An explicit `try_on` in the request
    wins (user intent). (Title backfill is Task 1.1.)
    """
    thing = get_thing_or_404(session, thing_id)
    data = item.model_dump(exclude_unset=True)
    # Machine rating is stable across this patch (rels don't change), so compute it once and
    # derive both the before/after effective ratings from it — no second SQL query.
    machine = _machine_rating_value(session, thing)
    old_rating = _effective(thing.human_rating, machine, 0.0)
    if "human_rating" in data:
        thing.human_rating = data["human_rating"]
    if "try_on" in data:  # explicit; null acknowledges permafail
        thing.try_on = data["try_on"]
    else:  # raise-to-eligible side-effect (§2.5) — explicit try_on overrides this
        new_rating = _effective(thing.human_rating, machine, 0.0)
        # all things are subject only to playlist floor as maybe a metadata job is needed
        if (thing.best_oi is None and new_rating > old_rating
                and new_rating > _PLAYLIST_FLOOR):
            thing.try_on = _today()
    # Soft-hint edits (V3 PATCH-schedule parity): write into attrs JSONB, preserving the rest;
    # an explicit null clears the hint (the merge_attr(..., None) pattern submit_result uses).
    # Same hint set add_thing propagates, so a new hint stays settable on both create and edit.
    for hint in xform._PROPAGATE_HINTS:
        if hint in data:
            xform.merge_attr(thing, hint, data[hint])
    if "container" in data:  # NULL->value sets it; switching a set value is a 409
        _set_container_hint(thing, data["container"])
    session.add(thing)
    session.commit()
    session.refresh(thing)
    # machine rating is stable across the patch (rels unchanged) — reuse it, no extra query.
    return _read_with_ratings(thing, machine)


# --- Jobs / runs: Stage-1 ingest (Task 1.1) -------------------------------------------

def _find_thing(session: Session, thing: Thing) -> Optional[Thing]:
    """Locate an existing thing matching `thing`'s identity: native key, then URL.

    (`col == None` deliberately becomes SQL `IS NULL` here for stubs without an
    extractor_key — see the module docstring gotcha.)
    """
    if thing.native_id is not None:
        found = session.exec(
            select(Thing).where(Thing.backend == thing.backend,
                                Thing.extractor_key == thing.extractor_key,
                                Thing.native_id == thing.native_id)).first()
        if found is not None:
            return found
    if thing.url is not None:
        return session.exec(select(Thing).where(Thing.url == thing.url)).first()
    return None


# Loser columns carried onto the survivor on a merge when the survivor still lacks them. The
# identity fields (url/native_id/extractor_key/backend) are deliberately excluded: they arrive
# from the fresh pull via the caller's null_backfill *after* the loser (which holds the clashing
# unique value) is deleted, so that write can never collide. `bucket` is excluded too (immutable,
# never NULL [A10]); `machine_rating` is excluded (computed on read, the column is unread).
_MERGE_CARRY_FIELDS = ("title", "channel", "thumbnail_url", "modified", "container",
                       "human_rating", "best_oi", "last_success_dt", "last_failure_dt", "try_on")


def _merge_things(session: Session, survivor: Thing, loser: Thing) -> None:
    """Converge a duplicate `loser` row into `survivor` — two rows that turned out to be one
    real thing (incremental key discovery: one created url-first, one native-key-first, now a
    pull carries both keys). Re-points `loser`'s rel/run FKs onto `survivor`, carries over the
    survivor's still-NULL state/display fields + attrs from `loser`, then deletes `loser`.

    `survivor` is the row the pull already matched (`_find_thing`: native key preferred, else
    URL), so external refs already point at it. Identity unique fields are NOT copied here — the
    caller sets them from the pull after this deletes the loser holding the clashing value.
    """
    # Re-point loser's edges onto survivor: drop the old rows, then re-insert remapped, OR-ing
    # `channel` on any edge survivor already has (the monotonic upsert _ingest_pull uses) and
    # dropping a self-loop the rename would create. Delete-then-insert (not UPDATE) so an edge
    # survivor already shares can't trip the (parent, child) PK.
    rels = session.exec(
        select(Rel).where((Rel.parent == loser.id) | (Rel.child == loser.id))).all()
    edges: dict[tuple[uuid.UUID, uuid.UUID], bool] = {}
    for rel in rels:
        parent = survivor.id if rel.parent == loser.id else rel.parent
        child = survivor.id if rel.child == loser.id else rel.child
        session.delete(rel)
        if parent != child:
            key = (parent, child)
            edges[key] = edges.get(key, False) or rel.channel
    session.flush()   # remove the loser's edges before re-inserting the remapped ones
    if edges:
        stmt = pg_insert(Rel).values(
            [{"parent": p, "child": c, "channel": ch} for (p, c), ch in edges.items()])
        stmt = stmt.on_conflict_do_update(
            index_elements=["parent", "child"],
            set_={"channel": Rel.__table__.c.channel.op("OR")(stmt.excluded.channel)})
        session.execute(stmt)
    # Runs have no unique key on thing_id, so a plain re-point is enough.
    session.execute(sa.update(Run).where(Run.thing_id == loser.id)
                    .values(thing_id=survivor.id))
    # Capture what the survivor should inherit, then delete the loser *before* writing it so the
    # unique values it held (and the FKs) are gone first (SQLAlchemy orders deletes after updates
    # within a flush, which would otherwise reintroduce the collision).
    carry = {f: getattr(loser, f) for f in _MERGE_CARRY_FIELDS
             if getattr(survivor, f) is None and getattr(loser, f) is not None}
    loser_attrs = loser.attrs
    session.delete(loser)
    session.flush()
    for field, value in carry.items():
        setattr(survivor, field, value)
    if loser_attrs:   # union attrs, survivor's own keys win
        survivor.attrs = {**loser_attrs, **(survivor.attrs or {})}


def _apply_backfill(session: Session, existing: Thing, incoming: Thing) -> None:
    """Fill NULL fields on `existing` from `incoming` (#147), converging any duplicate row.

    `null_backfill` proposes the still-NULL fields. If proposing `native_id` (thing_native) or
    `url` (thing_url) would collide with a *different* row, that row is a duplicate of the same
    real thing: merge it into `existing` (`_merge_things`) so the proposed value is free, then
    apply it. Both partial-unique indexes flow through the one merge path — except a *channel*
    clash, which is never converged away (see the guard below).
    """
    fields = xform.null_backfill(existing, incoming)
    clashes: dict[uuid.UUID, tuple[Thing, list[str]]] = {}

    def _note(clash: Optional[Thing], field: str) -> None:
        if clash is not None:
            clashes.setdefault(clash.id, (clash, []))[1].append(field)

    if "native_id" in fields:
        ek = fields.get("extractor_key", existing.extractor_key)
        _note(session.exec(
            select(Thing).where(Thing.backend == existing.backend,
                                Thing.extractor_key == ek,
                                Thing.native_id == fields["native_id"],
                                Thing.id != existing.id)).first(), "native_id")
    if "url" in fields:
        _note(session.exec(
            select(Thing).where(Thing.url == fields["url"],
                                Thing.id != existing.id)).first(), "url")
    for clash, clash_fields in clashes.values():
        # REVIEW-DEFERRED-4 #1: never converge a channel away. A sub-container that shares its
        # channel's id (a self-pulling Videos/Shorts/Live tab, #46) would otherwise clash its id
        # onto the channel and merge — deleting — it. Leave the channel (and its native_id, which
        # ID-based video->channel linking relies on) intact and drop the clashing identity backfill,
        # so `existing` (the tab) stays URL-keyed. The pull otherwise succeeds. (When ID-based
        # linking lands, allow a genuine channel->channel convergence: skip only when `existing`
        # is not itself a channel.)
        if (clash.attrs or {}).get("kind") == "channel":
            for field in clash_fields:
                fields.pop(field, None)
            continue
        _merge_things(session, existing, clash)
    for key, value in fields.items():
        setattr(existing, key, value)


def _fanout_video_channel(session: Session, video: Thing, chan: models.UlChan) -> None:
    """Upsert the video's uploader container + a `channel=True` rel (the flat-pull omits it).

    Mirrors the Stage-1 channel fan-out, used when a `meta` job's full extract discovers the
    uploader a flat playlist pull left out. No-op if the uploader has no URL.
    """
    stub = xform.thing_from_chan(chan)
    if stub is None:
        return
    existing = _find_thing(session, stub)
    if existing is None:
        stub.bucket = video.bucket
        session.add(stub)
        session.flush()      # need the id for the rel FK
        chan_id = stub.id
    else:
        chan_id = existing.id
    session.execute(pg_insert(Rel).values(
        parent=chan_id, child=video.id, channel=True).on_conflict_do_nothing())


def _apply_video_metadata(session: Session, video: Thing, pull: models.PullThing) -> None:
    """Enrich a video thing from a full single-video extract — shared by meta + download.

    NULL-backfills identity + display fields (#147) and fans out the uploader's channel
    (thing + channel_video rel) the flat pull omitted. Does NOT touch best_oi/try_on/
    last_success — those per-outcome decisions stay with the caller.
    """
    _apply_backfill(session, video, xform.thing_from_node(pull))
    _fanout_video_channel(session, video, pull.channel)


def _ingest_pull(session: Session, run: Run, pull: models.PullThing,
                 container: Thing, now: datetime.datetime) -> None:
    """Ingest one Stage-1 container pull: record the single run and fan out the (possibly
    inlined) subtree (the body of submit_result's Stage-1 path).

    One yt-dlp call -> one `Run`: the `run`/`container` are the (already-fetched) in-progress run
    and its thing. The run's `entries_hash` covers the *whole* inlined subtree (`subtree_hash`),
    so a change anywhere below keeps the container's backoff hot. The fan-out (`_fanout`) recurses
    into any member yt-dlp handed back already enumerated (inlined `entries`) to (re)establish its
    grandchildren and re-schedule it as parent-fed — without creating further runs. Does not
    commit; the submit_result tail (`_finish`) does the single commit for the whole tree.
    """
    run.entries_hash = xform.subtree_hash(pull)
    run.playlist_count = xform.reconcile_count(pull)
    _fanout(session, pull, container, now, parent_try_on=None)


def _fanout(session: Session, pull: models.PullThing, container: Thing,
            now: datetime.datetime, parent_try_on: Optional[datetime.date]) -> None:
    """Fan out one container pull into the thing/rel graph and (re)schedule the container.

    `parent_try_on` is None for the claimed top-level container — normal Fibonacci backoff over
    its run history — and the parent's scheduled date for an inlined sub-container (parent-fed:
    no run of its own, `try_on = parent_try_on + SAFETY_MARGIN_DAYS` so the parent refreshes it
    first). Recurses into inlined sub-containers, fanning out their grandchildren, but creates no
    further runs (one yt-dlp call -> one run). Idempotent: re-fans an already-known sub-container
    every time the parent inlines it. Does not commit.
    """
    # Stubs inherit the container's bucket (immutable, [A10]) and its propagated soft hints
    # (attrs.cookies/lpm_lib -> video/sub-container stubs, [A11]).
    graph = xform.pl_full2things(pull, bucket=container.bucket, parent_attrs=container.attrs)

    # An inlined sub-container (parent_try_on set) is URL-keyed, just like it was as a member of
    # its parent (pl_full2things nulls every sub-container member's native_id). Its own pull node
    # still carries the shared id, so null it on the container stub *after* the graph (and its
    # edges, which used the id) are built — keeping the URL-keyed identity. Otherwise the id
    # backfills onto the sub-container and, for a channel tab that shares the channel's id, the
    # thing_native index (or the duplicate-row merge) collapses the tab back onto the channel. The
    # id is kept as a soft channel_id hint. The top-level claimed container (parent_try_on None) is
    # left alone — a directly-pulled channel/playlist keeps its own native_id.
    if parent_try_on is not None and graph.playlist.native_id is not None:
        xform.merge_attr(container, "channel_id", graph.playlist.native_id)
        graph.playlist.native_id = None

    # The recorded thing IS the container: backfill it, classify it, mark success.
    _apply_backfill(session, container, graph.playlist)
    container.container = True
    container.last_success_dt = now
    xform.clear_info_hint(container)   # "just a playlist": hint cleared after its own pull
    # A container that is its own videos' uploader or owns sub-containers is acting as a
    # channel — tag the display hint (any channel=True edge it parents, idempotent).
    if any(r.parent == graph.playlist.id and r.channel for r in graph.rels):
        xform.merge_attr(container, "kind", "channel")

    # graph.members is built one-per-entry in pull.entries order, so a member stub maps back to
    # its source node — used to spot a sub-container that came with inlined entries (below).
    node_by_stub = {member.id: node for member, node in zip(graph.members, pull.entries)}
    nested: list[tuple[Thing, models.PullThing]] = []

    remap = {graph.playlist.id: container.id}
    for stub in graph.members + graph.channels:
        existing = _find_thing(session, stub)
        if existing is not None:
            thing = existing
            _apply_backfill(session, existing, stub)
            xform.refresh_info_hint(existing, (stub.attrs or {}).get(xform.INFO_JSON_KEY))
            # Carry a freshly-discovered channel hint onto a pre-existing container.
            if (stub.attrs or {}).get("kind") == "channel":
                xform.merge_attr(existing, "kind", "channel")
        else:
            thing = stub
            session.add(stub)
        # A video is metadata-complete once its fields are enough to rate (a present title);
        # decided API-side (§Stage 1). Set once, when still NULL (a fresh stub always is);
        # else it stays NULL and a `meta` job enriches it later.
        if (thing.container is False and thing.last_success_dt is None
                and xform.enough_to_rate(thing)):
            thing.last_success_dt = now
        # A sub-container yt-dlp handed back already enumerated (inlined `entries`): re-fan it out
        # now as part of this single call (recursion below) so its grandchildren are (re)established
        # and it is rescheduled parent-fed — every time the parent inlines it (idempotent). Never
        # re-type a known leaf (the container-switch guard); the self-guard avoids recursing into a
        # member that resolved to the container itself. A flat sub-playlist pointer (no entries, the
        # normal case) falls through and self-schedules.
        node = node_by_stub.get(stub.id)
        if (node is not None and node.container is True and node.entries
                and thing.id != container.id
                and not xform.container_switch(thing.container, True)):
            nested.append((thing, node))
        remap[stub.id] = thing.id
    session.flush()  # persist new stubs so the rel FKs resolve

    # Bulk upsert the edges in one statement keyed on the (parent, child) PK. On conflict we
    # *monotonically upgrade* channel (existing OR incoming): never downgrades, idempotent on
    # re-pull, and lets a later better-informed run raise a stale False->True — e.g. a
    # sub-container first seen as channel=False membership becomes channel=True once it is
    # pulled itself and reveals the parent as its owner.
    #
    # graph.rels has fresh, distinct child ids, but remap can collapse distinct stubs onto the
    # same thing id (e.g. a playlist listing the same video twice), producing duplicate
    # (parent, child) pairs after remap. Postgres rejects a single ON CONFLICT statement that
    # proposes the same conflict key twice (CardinalityViolation), so merge duplicates here,
    # OR-ing channel (the same monotonic-upgrade rule as the on-conflict below).
    edges: dict[tuple[uuid.UUID, uuid.UUID], bool] = {}
    for rel in graph.rels:
        key = (remap[rel.parent], remap[rel.child])
        if key[0] == key[1]:   # never a self-edge (a member that resolved to the container)
            continue
        edges[key] = edges.get(key, False) or rel.channel
    rows = [{"parent": p, "child": c, "channel": ch} for (p, c), ch in edges.items()]
    if rows:
        stmt = pg_insert(Rel).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["parent", "child"],
            set_={"channel": Rel.__table__.c.channel.op("OR")(stmt.excluded.channel)})
        session.execute(stmt)

    # Schedule: the claimed top-level container backs off normally (§4.4); an inlined,
    # parent-fed sub-container gets a long safety-net date keyed off its parent so the parent
    # re-feeds it first and it self-pulls only if the parent goes quiet (hybrid scheduling).
    if parent_try_on is None:
        _set_try_on(session, container)   # successful container run -> next backoff date (§4.4)
    else:
        container.try_on = parent_try_on + SAFETY_MARGIN_DAYS

    # Inlined sub-playlists: re-fan out (no run — they ride the parent's single call), passing
    # this container's scheduled date so each is parent-fed off it.
    for sub_thing, node in nested:
        _fanout(session, node, sub_thing, now, parent_try_on=container.try_on)


@app.post("/jobs/claim", response_model=JobClaim,
          responses={204: {"description": "Nothing due"}})
def claim_job(item: ClaimRequest, session: Session = Depends(get_session)):
    """Prioritized dispatch: claim the single highest-priority due job (§4.2/§4.5).

    The API owns ordering (the runner never queries `thing`): one ordering spans both job
    types — container/unknown-before-video, then rating DESC, then `try_on` ASC. The row is
    claimed with `SELECT ... FOR UPDATE SKIP LOCKED` so concurrent workers never get the same
    thing (correct the day a 2nd worker appears; single worker in 4.0). On a hit the run is
    created here (`success=NULL` in-progress marker, `worker` set) and its id returned; 204 if
    nothing is due.

    The machine rating is computed on read (Task 2.2, §2.4): an unrated video under a B+
    container assesses as B and reaches `video_branch`; an under-rated container drops out.
    """
    # Effective rating, defaulting an unrated thing to 0.0 (C) — mirrors _effective_rating_value,
    # so an unrated video clears the C-band `meta_branch` (NULL would fail every comparison).
    rating = _effective_rating_expr(default=0.0)
    today = _today()
    # Stage-1 pull: a container or an unknown thing (`container` is True/NULL), grade >= C
    # band, due, not already succeeded today.
    stage1_branch = sa.and_(
        Thing.container.isnot(False), rating >= _PLAYLIST_FLOOR, Thing.try_on <= today,
        or_(Thing.last_success_dt == None,  # noqa: E711  (SQL IS NULL)
            func.date(Thing.last_success_dt) < today))
    # Stage-2 video download: a leaf (container False), grade >= B band, never acquired, due.
    video_branch = sa.and_(
        Thing.container == False, rating >= _VIDEO_DOWNLOAD_FLOOR,  # noqa: E712
        Thing.best_oi == None, Thing.try_on <= today)  # noqa: E711
    # Stage-2 video meta-only: C-band leaf the flat pull under-described (no human-decision
    # metadata yet, last_success_dt NULL). Fetches metadata only — no media, no best_oi.
    meta_branch = sa.and_(
        Thing.container == False, rating >= _PLAYLIST_FLOOR,  # noqa: E712
        rating < _VIDEO_DOWNLOAD_FLOOR,
        Thing.last_success_dt == None, Thing.best_oi == None,  # noqa: E711
        Thing.try_on <= today)
    # Exclude things with a fresh in-progress run (success IS NULL, claimed within the lease):
    # this is what makes concurrent workers safe — once a worker claims a thing and commits its
    # run, the row lock is gone, so SKIP LOCKED alone would let a second worker re-claim the same
    # thing (§4.5 risk #2). The lease lets a hard-crashed worker's zombie run expire (see
    # CLAIM_LEASE). Harmless for the single-worker case: a clean failure is finalized by
    # report_failure/result-ingest, so no in-progress run lingers between loop iterations.
    active = select(Run.thing_id).where(
        Run.success == None,  # noqa: E711  (SQL IS NULL)
        Run.starttime > models.naive_utcnow() - CLAIM_LEASE)
    stmt = (select(Thing)
            .where(or_(stage1_branch, video_branch, meta_branch), Thing.id.notin_(active))
            .order_by(sa.desc(Thing.container.isnot(False)), rating.desc(), Thing.try_on.asc())
            .limit(1).with_for_update(skip_locked=True))
    # Worker self-selection (§4.5): a worker may pin itself to one extractor's jobs.
    if item.extractor is not None:
        stmt = stmt.where(Thing.extractor_key == item.extractor.lower())
    thing = session.exec(stmt).first()
    if thing is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    run = Run(thing_id=thing.id, worker=item.worker,
              starttime=models.naive_utcnow(), success=None)
    session.add(run)
    session.commit()
    session.refresh(run)
    # TODO split this out to a more robust try-other-things-upon-failure logic
    # Per-job cookies suggestion: the attrs.cookies hint, OR escalation after a cookieless
    # failure — the last completed run failed without cookies (§4.7) [A11]. (The just-created
    # in-progress run is excluded by the `success != None` filter.)
    cookies = bool((thing.attrs or {}).get("cookies"))
    if not cookies:
        last_done = session.exec(
            select(Run).where(Run.thing_id == thing.id, Run.success != None)  # noqa: E711
            .order_by(Run.starttime.desc())).first()
        if (last_done is not None and last_done.success is False
                and not (last_done.input_json or {}).get("cookies")):
            cookies = True
    machine = _machine_rating_value(session, thing)
    eff = _effective(thing.human_rating, machine, 0.0)
    return JobClaim(run_id=run.id, thing=_read_with_ratings(thing, machine),
                    download=_wants_download(thing, eff), cookies=cookies)


@app.post("/jobs/{run_id}/result", response_model=RunRead)
def submit_result(run_id: uuid.UUID, item: RunResultIn,
                  session: Session = Depends(get_session)):
    """Stage-1 ingest: record a run's result and upsert the thing/rel graph it found.

    The V4 rewrite of V3's POST /playlist-run, one endpoint for both job kinds. The result
    body decides the path: a `video` body is the Stage-2 path — sets `best_oi` (the OI file
    UUID) on a download, backfills NULL identity, marks acquired (`try_on=NULL`), and classifies
    the thing `container=False` (also how an unknown URL that resolved to a single video gets
    classified, #153). A `playlist` body is the Stage-1 container pull — classifies
    `container=True`, fans out into a stub `thing` per member (videos + sub-containers +
    uploader channels) and the `rel` edges (#137), backfilling NULL fields we already knew
    (#147); on failure it records the failure only (C8).
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    # A run is finalized exactly once: `_finish` stamps `endtime` on every terminal path.
    # Reject a second result for the same run (409) so a worker that posts a result, has the
    # response lost to a transient error, and then reports a failure (job_runner.report_failure)
    # cannot overwrite an already-recorded success — which would demote an acquired thing back
    # to failed + backoff. The worker treats the 409 like any other report error and moves on.
    if run.endtime is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Run already finalized")
    now = models.naive_utcnow()
    run.endtime = now
    run.success = item.success
    if item.worker is not None:
        run.worker = item.worker
    if item.data_json is not None:
        run.data_json = item.data_json
    if item.input_json is not None:  # per-run decisions (e.g. cookies used), §2.3
        run.input_json = item.input_json

    pl_thing = session.get(Thing, run.thing_id)

    def _fail() -> RunRead:
        """Record this run as a failure (+ backoff) and finalize — the shared terminal path
        for every failure kind (reported failure, both-shape guard, container-switch)."""
        run.success = False
        if pl_thing is not None:
            pl_thing.last_failure_dt = now
            _set_try_on(session, pl_thing)
        return _finish(session, run)

    # Failure is handled identically for every job kind (record + backoff), so do it once
    # up front before the per-kind success paths below (§4.4).
    if not item.success:
        return _fail()
        # TODO still try to get metadata from a failed download?

    # Both-shape guard (#164): a successful result must be exactly one shape — a Stage-1 playlist
    # pull XOR a Stage-2 video. The worker never sends both (it reports the ambiguous video+
    # playlist shape as a failure), but a different client could POST both, and the video branch
    # below would silently win and drop the playlist. This is contradictory evidence, not new
    # classification: record a plain failure (+ backoff) and never mutate `container` — a thing's
    # classification is set once and only switched via the guard below (never reset).
    if item.playlist is not None and item.video is not None:
        # Keep best_oi if media was already uploaded — preserve the ref to investigate so the OI
        # object isn't orphaned — but do NOT mark acquired, fan out rels, or re-classify.
        if pl_thing is not None and item.best_oi is not None:
            pl_thing.best_oi = item.best_oi
        return _fail()

    # A successful result must carry exactly one shape. The both-shape case is handled above, so a
    # neither-shape body is malformed (422). Guard it here, before the container-switch check, so an
    # empty body can't be misread as a `video` body proposing container=False on a known leaf.
    if item.playlist is None and item.video is None:
        raise HTTPException(status_code=422,
                            detail="playlist or video is required on a successful run")

    # Container is set once: NULL->value classifies (below), value->same affirms, but a switch
    # (a `video` body proposing False on a known container, or a `playlist` body proposing True
    # on a known leaf) is rejected as a failure (+ backoff) — never silently re-typed.
    if pl_thing is not None and xform.container_switch(pl_thing.container, item.video is None):
        return _fail()

    # Stage-2 video result — one common metadata-ingest path for `meta` and `download`
    # (distinct from the Stage-1 container fan-out below). Identified by a `video` body, which
    # the worker always sends for a successful video job. A `video` body on an unknown thing is
    # also how a single-video discovery is classified (container=False, #153). Both forward the
    # full single-video `video`; outcome diverges on `best_oi` (download vs meta).
    if pl_thing is not None and item.video is not None:
        pl_thing.container = False             # classify (discovery) / affirm a leaf
        run.entries_hash = xform.pl_hash([])   # leaf: empty, unchanging membership (§4.4 backoff)
        _apply_video_metadata(session, pl_thing, item.video)  # display+identity+channel
        pl_thing.last_failure_dt = None
        if item.best_oi is not None:          # download: media acquired → always complete
            pl_thing.last_success_dt = now
            pl_thing.best_oi = item.best_oi
            pl_thing.try_on = None            # acquired; never re-fetch (§2.5)
            xform.clear_info_hint(pl_thing)   # acquired: drop the now-moot load-info hint
        else:                                 # meta: metadata only, still pending acquisition
            pl_thing.last_success_dt = now       # full extract is terminal → complete (§4.2),
                                                 # even if still bare: never re-loop a meta job (#163)
            xform.refresh_info_hint(pl_thing, item.video.info_json)  # keep Stage-2 hint fresh
            # A completed meta is terminal — meta_branch is gated on last_success_dt IS NULL, now
            # set, so it can never re-claim this leaf (#163). Leave it due (try_on=today), exactly
            # like a never-meta'd C-band leaf, so it is claimable the instant it qualifies for
            # download: immediately for a B+ leaf (#191, no day gap), or later when a parent rating
            # lifts its machine rating to B (video_branch needs try_on<=today; a backoff date would
            # only delay that, and a C-band meta backoff is otherwise inert — nothing re-reads it).
            pl_thing.try_on = _today()
        return _finish(session, run)

    if item.playlist is None:
        raise HTTPException(status_code=422,
                            detail="playlist or video is required on a successful run")
    _ingest_pull(session, run, item.playlist, pl_thing, now)
    return _finish(session, run)
