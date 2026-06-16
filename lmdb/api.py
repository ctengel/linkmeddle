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
from .models import (Thing, Rel, Run, ThingRead, ThingWithRelated, RelatedThing,
                     RunRead, ThingAdd, ThingPatch, ClaimRequest, JobClaim, RunResultIn)

# Effective-rating floor for fetching a video's *media* (Stage-2 download); below it (C band)
# a video only gets a metadata-only `meta` job.
_VIDEO_DOWNLOAD_FLOOR = 0.5
# Run-eligibility floor for playlists/other (the C band): below it nothing is dispatched.
_PLAYLIST_FLOOR = -0.5

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg:///lmdb")
engine = create_engine(DATABASE_URL, echo=False)

# Letter grade <-> signed float (-2..+2). D/F are not addable (you don't add to suppress).
GRADE_VALUES = {"A": 2.0, "B": 1.0, "C": 0.0, "D": -1.0, "F": -2.0}
ADD_GRADES = {"A", "B", "C"}


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


def _machine_rating_value(session: Session, thing: Thing) -> Optional[float]:
    """Computed machine rating for one thing (None if no human-rated relatives apply)."""
    return session.exec(select(_machine_rating_expr()).where(Thing.id == thing.id)).one()


def _effective_rating_value(session: Session, thing: Thing) -> float:
    """Effective rating of a thing instance = human, else computed machine, else 0.0 (C).

    The instance form of `_effective_rating_expr(default=0.0)`, for callers that hold a loaded
    `thing` and need its rating for a Python decision (dispatch action, backoff band, §2.4).
    """
    if thing.human_rating is not None:
        return thing.human_rating
    machine = _machine_rating_value(session, thing)
    return machine if machine is not None else 0.0


def _read_with_ratings(thing: Thing, machine: Optional[float]) -> ThingRead:
    """ThingRead with computed machine/effective ratings. Human rating is authoritative: when
    present, machine is treated as NULL and effective is the human rating (§2.4)."""
    tr = ThingRead.model_validate(thing)
    if thing.human_rating is not None:
        tr.machine_rating, tr.effective_rating = None, thing.human_rating
    else:
        tr.machine_rating = tr.effective_rating = machine
    return tr


def _container_from_type(type_hint: Optional[str]) -> tuple[Optional[bool], bool]:
    """Map the optional ergonomic add-time `type` hint to (container, is_channel).

    'channel'/'playlist' -> container True (channel also tags attrs.kind='channel'); 'video'
    -> False; omitted -> None (unknown; the first pull classifies it, #153).
    """
    if type_hint is None:
        return None, False
    mapping = {"video": (False, False), "playlist": (True, False), "channel": (True, True)}
    if type_hint.lower() not in mapping:
        raise HTTPException(status_code=422,
                            detail="type must be one of channel/playlist/video")
    return mapping[type_hint.lower()]


def _action_for(thing: Thing, eff_rating: float) -> str:
    """What the worker should do with this claimed thing (§4.5 dispatch result).

    Container or unknown (`container` is True/NULL) -> 'pull' (Stage-1 metadata fan-out; an
    unknown URL is classified by the result). A leaf video (`container is False`) ->
    'download' (Stage-2 media+metadata) when its effective rating clears the B floor, else
    'meta' (Stage-2 metadata-only enrichment for a C-band video the flat pull under-described).
    """
    if thing.container is False:
        return "download" if eff_rating >= _VIDEO_DOWNLOAD_FLOOR else "meta"
    return "pull"


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


def _run_read(run: Run) -> RunRead:
    """Serialize a run, hex-encoding the binary entries_hash for JSON."""
    return RunRead(id=run.id, thing_id=run.thing_id, worker=run.worker,
                   input_json=run.input_json, data_json=run.data_json,
                   entries_hash=run.entries_hash.hex() if run.entries_hash else None,
                   playlist_count=run.playlist_count, starttime=run.starttime,
                   endtime=run.endtime, success=run.success)


def _finish(session: Session, run: Run) -> RunRead:
    """Commit the in-progress run and return its serialized view (the submit_result tail)."""
    session.add(run)
    session.commit()
    session.refresh(run)
    return _run_read(run)


def _refresh_info_hint(thing: Thing, info: Optional[dict]) -> None:
    """Stamp the Stage-2 load-info hint onto a video still pending download (best_oi NULL).

    yt-dlp info dicts go stale, so the hint is refreshed while the media is unacquired and
    left alone once acquired. (Belongs in xform with INFO_JSON_KEY/enough_to_rate once those
    land there; kept here while xform is mid-reconciliation.)
    """
    if info is not None and thing.container is False and thing.best_oi is None:
        thing.attrs = {**(thing.attrs or {}), xform.INFO_JSON_KEY: info}


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

@app.post("/things/", response_model=ThingRead, status_code=status.HTTP_201_CREATED)
def add_thing(item: ThingAdd, response: Response, session: Session = Depends(get_session)):
    """Add a thing by URL (the human entry point).

    Stores the URL with a default rating of C (override A/B). The user need not know the
    kind: `type` is an optional hint mapped to `container` (channel/playlist -> True, video
    -> False, omitted -> NULL = unknown, classified on first pull, #153); 'channel' also
    tags `attrs.kind='channel'`. `bucket` (OI storage home) is required — no server default
    ([A10]). Optional `cookies`/`lpm_lib` are stored as soft hints in `attrs` ([A11]).
    extractor_key/native_id are filled in later by the worker. Idempotent on URL (returns
    the existing thing with 200, bucket unchanged — bucket is immutable).
    """
    existing = session.exec(select(Thing).where(Thing.url == item.url)).one_or_none()
    if existing:
        response.status_code = status.HTTP_200_OK
        return existing
    grade = (item.rating or "C").upper()
    if grade not in ADD_GRADES:
        raise HTTPException(status_code=422,
                            detail=f"rating must be one of {sorted(ADD_GRADES)} at add time")
    container, is_channel = _container_from_type(item.type)
    attrs: dict = {}
    if is_channel:
        attrs["kind"] = "channel"
    if item.cookies is not None:
        attrs["cookies"] = item.cookies
    if item.lpm_lib is not None:
        attrs["lpm_lib"] = item.lpm_lib
    thing = Thing(url=item.url, container=container, human_rating=GRADE_VALUES[grade],
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
        return existing
    session.refresh(thing)
    return thing


@app.get("/things/", response_model=list[ThingRead])
def list_things(type: Optional[str] = None, rating: Optional[str] = None,
                min_rating: Optional[str] = None,
                due: bool = False, needs_rating: bool = False, new: bool = False,
                failing: bool = False, url: Optional[str] = None,
                extractor: Optional[str] = None, native_id: Optional[str] = None,
                session: Session = Depends(get_session)):
    """List/search things. Backs every list view + the status dashboard.

    `extractor` + `native_id` is the V4 replacement for V3 GET /videos/{ex}/{id} (#102).
    `rating` filters the exact *human* grade; `min_rating` filters the *effective* rating
    (human else computed machine, §2.4) at the grade's band floor — e.g. `min_rating=B`
    returns everything effectively B-or-better.
    """
    stmt = select(Thing, _machine_rating_expr().label("machine_rating"))
    if type is not None:
        # 'type' is an ergonomic alias over the boolean + display hint: 'video' -> leaf,
        # 'playlist' -> container, 'channel' -> container tagged attrs.kind='channel'.
        t = type.lower()
        if t == "video":
            stmt = stmt.where(Thing.container == False)  # noqa: E712
        elif t == "playlist":
            stmt = stmt.where(Thing.container == True)  # noqa: E712
        elif t == "channel":
            stmt = stmt.where(Thing.container == True,  # noqa: E712
                              Thing.attrs["kind"].astext == "channel")
        else:
            raise HTTPException(status_code=422,
                                detail="type must be one of channel/playlist/video")
    if url is not None:
        stmt = stmt.where(Thing.url == url)
    if extractor is not None:
        stmt = stmt.where(Thing.extractor_key == extractor.lower())
    if native_id is not None:
        stmt = stmt.where(Thing.native_id == native_id)
    if rating is not None:
        grade = rating.upper()
        if grade not in GRADE_VALUES:
            raise HTTPException(status_code=422,
                                detail="invalid rating grade")
        stmt = stmt.where(Thing.human_rating == GRADE_VALUES[grade])
    if min_rating is not None:
        grade = min_rating.upper()
        if grade not in GRADE_VALUES:
            raise HTTPException(status_code=422, detail="invalid rating grade")
        # Band floor: round-direction-safe form of "effective grade >= X" (§2.4).
        stmt = stmt.where(_effective_rating_expr(default=0.0) >= GRADE_VALUES[grade] - 0.5)
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
    stmt = stmt.order_by(Thing.created_dt.desc())
    return [_read_with_ratings(thing, machine) for thing, machine in session.exec(stmt).all()]


@app.get("/things/{thing_id}", response_model=ThingWithRelated)
def get_thing(thing_id: uuid.UUID, include: Optional[str] = None,
              session: Session = Depends(get_session)):
    """Get one thing; `?include=related` also returns its rel neighbors."""
    thing = get_thing_or_404(session, thing_id)
    machine = _machine_rating_value(session, thing)
    related = _related(session, thing_id, None) if include == "related" else []
    return ThingWithRelated(**_read_with_ratings(thing, machine).model_dump(), related=related)


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
    return [_run_read(r) for r in runs]


@app.patch("/things/{thing_id}", response_model=ThingRead)
def patch_thing(thing_id: uuid.UUID, item: ThingPatch,
                session: Session = Depends(get_session)):
    """Update a thing: set the rating (incl. D/F), or ack permafail (try_on=null).

    Raising the human rating to an eligible level re-opens the date gate
    (`try_on = today`, guarded by `best_oi IS NULL`) — resurrecting a permafail or pulling
    a future-scheduled thing forward (§2.5, Task 2.1). An explicit `try_on` in the request
    wins (user intent). (Title backfill is Task 1.1.)
    """
    thing = get_thing_or_404(session, thing_id)
    data = item.model_dump(exclude_unset=True)
    old_rating = _effective_rating_value(session, thing)
    grade = data.pop("grade", None)
    if grade is not None:
        if grade.upper() not in GRADE_VALUES:
            raise HTTPException(status_code=422,
                                detail="invalid grade")
        thing.human_rating = GRADE_VALUES[grade.upper()]
    if "human_rating" in data:
        thing.human_rating = data["human_rating"]
    if "try_on" in data:  # explicit; null acknowledges permafail
        thing.try_on = data["try_on"]
    else:  # raise-to-eligible side-effect (§2.5) — explicit try_on overrides this
        new_rating = _effective_rating_value(session, thing)
        # all things are subject only to playlist floor as maybe a metadata job is needed
        if (thing.best_oi is None and new_rating > old_rating
                and new_rating > _PLAYLIST_FLOOR):
            thing.try_on = _today()
    session.add(thing)
    session.commit()
    session.refresh(thing)
    return thing


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


def _apply_backfill(session: Session, existing: Thing, incoming: Thing) -> None:
    """Fill NULL fields on `existing` from `incoming` (#147), guarding the native-key index.

    If backfilling `native_id` would collide with a different existing row, that one field
    is skipped (true cross-row merge is out of 4.0 scope).
    """
    fields = xform.null_backfill(existing, incoming)
    if "native_id" in fields:
        ek = fields.get("extractor_key", existing.extractor_key)
        clash = session.exec(
            select(Thing).where(Thing.backend == existing.backend,
                                Thing.extractor_key == ek,
                                Thing.native_id == fields["native_id"],
                                Thing.id != existing.id)).first()
        if clash is not None:
            fields.pop("native_id")
    for key, value in fields.items():
        setattr(existing, key, value)


def _fanout_video_channel(session: Session, video: Thing, chan: models.UlChan,
                          extractor_key: Optional[str]) -> None:
    """Upsert the video's uploader container + a `channel=True` rel (the flat-pull omits it).

    Mirrors the Stage-1 channel fan-out, used when a `meta` job's full extract discovers the
    uploader a flat playlist pull left out. No-op if the uploader has no URL.
    """
    stub = xform.thing_from_chan(chan, extractor_key)
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


def _apply_video_metadata(session: Session, video: Thing, pull: models.VidFull) -> None:
    """Enrich a video thing from a full single-video extract — shared by meta + download.

    NULL-backfills identity + display fields (#147) and fans out the uploader's channel
    (thing + channel_video rel) the flat pull omitted. Does NOT touch best_oi/try_on/
    last_success — those per-outcome decisions stay with the caller.
    """
    _apply_backfill(session, video, xform.thing_from_vid(pull))
    _fanout_video_channel(session, video, pull.channel, pull.extractor_key)


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
    stmt = (select(Thing)
            .where(or_(stage1_branch, video_branch, meta_branch))
            .order_by(sa.desc(Thing.container.isnot(False)), rating.desc(), Thing.try_on.asc())
            .limit(1).with_for_update(skip_locked=True))
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
    eff = thing.human_rating if thing.human_rating is not None else (
        machine if machine is not None else 0.0)
    return JobClaim(run_id=run.id, thing=_read_with_ratings(thing, machine),
                    action=_action_for(thing, eff), cookies=cookies)


@app.post("/jobs/{run_id}/result", response_model=RunRead)
def submit_result(run_id: uuid.UUID, item: RunResultIn,
                  session: Session = Depends(get_session)):
    """Stage-1 ingest: record a run's result and upsert the thing/rel graph it found.

    The V4 rewrite of V3's POST /playlist-run, one endpoint for both job kinds. The result
    body decides the path: a `video` body (or a legacy known leaf) is the Stage-2 path —
    sets `best_oi` (the OI file UUID) on a download, backfills NULL identity, marks acquired
    (`try_on=NULL`), and classifies the thing `container=False` (also how an unknown URL that
    resolved to a single video gets classified, #153). A `playlist` body is the Stage-1
    container pull — classifies `container=True`, fans out into a stub `thing` per member
    (videos + sub-containers + uploader channels) and the `rel` edges (#137), backfilling
    NULL fields we already knew (#147); on failure it records the failure only (C8).
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
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

    # Failure is handled identically for every job kind (record + backoff), so do it once
    # up front before the per-kind success paths below (§4.4).
    if not item.success:
        if pl_thing is not None:
            pl_thing.last_failure_dt = now
            _set_try_on(session, pl_thing)
        return _finish(session, run)
        # TODO still try to get metadata from a failed download?

    # Stage-2 video result — one common metadata-ingest path for `meta` and `download`
    # (distinct from the Stage-1 container fan-out below). Identified by a `video` body, or
    # legacy by the dispatched thing already being a known leaf. A `video` body on an unknown
    # thing is also how a single-video discovery is classified (container=False, #153). Both
    # forward the full single-video `video`; outcome diverges on `best_oi` (download vs meta).
    is_video_result = item.video is not None or (
        item.playlist is None and pl_thing is not None and pl_thing.container is False)
    if pl_thing is not None and is_video_result:
        pl_thing.container = False             # classify (discovery) / affirm a leaf
        if item.video is not None:
            _apply_video_metadata(session, pl_thing, item.video)  # display+identity+channel
        else:  # legacy / video omitted: identity-only fallback (#147)
            _apply_backfill(session, pl_thing,
                            Thing(container=False, bucket=pl_thing.bucket,
                                  extractor_key=item.extractor_key, native_id=item.native_id))
        pl_thing.last_failure_dt = None
        if item.best_oi is not None:          # download: media acquired → always complete
            pl_thing.last_success_dt = now
            pl_thing.best_oi = item.best_oi
            pl_thing.try_on = None            # acquired; never re-fetch (§2.5)
            # clears thing hints since we don't need it anymore
            pl_thing.attrs = {**(pl_thing.attrs or {}), xform.INFO_JSON_KEY: None}
        else:                                 # meta: metadata only, still pending acquisition
            if xform.enough_to_rate(pl_thing):   # all five identity fields present
                pl_thing.last_success_dt = now   # else stays NULL → re-dispatch on backoff
            info = item.video.info_json if item.video is not None else None
            _refresh_info_hint(pl_thing, info)   # keep the Stage-2 load-info hint fresh
            _set_try_on(session, pl_thing)       # backoff (§4.4)
        return _finish(session, run)

    if item.playlist is None:
        raise HTTPException(status_code=422,
                            detail="playlist or video is required on a successful run")
    # Stubs inherit the dispatched container's bucket (immutable, [A10]) and its propagated
    # soft hints (attrs.cookies/lpm_lib -> video/sub-container stubs, [A11]).
    graph = xform.pl_full2things(item.playlist, bucket=pl_thing.bucket,
                                 parent_attrs=pl_thing.attrs)

    run.entries_hash = xform.pl_hash(item.playlist.entries, item.playlist.child_playlists)
    run.playlist_count = xform.reconcile_count(item.playlist)

    # The dispatched thing IS the container: backfill it, classify it, mark success.
    _apply_backfill(session, pl_thing, graph.playlist)
    pl_thing.container = True
    pl_thing.last_success_dt = now
    # A container that is its own videos' uploader or owns sub-containers is acting as a
    # channel — tag the display hint (any channel=True edge it parents, idempotent).
    if (any(r.parent == graph.playlist.id and r.channel for r in graph.rels)
            and (pl_thing.attrs or {}).get("kind") != "channel"):
        pl_thing.attrs = {**(pl_thing.attrs or {}), "kind": "channel"}

    remap = {graph.playlist.id: pl_thing.id}
    for stub in graph.videos + graph.channels + graph.child_playlists:
        existing = _find_thing(session, stub)
        if existing is not None:
            _apply_backfill(session, existing, stub)
            _refresh_info_hint(existing, (stub.attrs or {}).get(xform.INFO_JSON_KEY))
            # Carry a freshly-discovered channel hint onto a pre-existing container.
            if ((stub.attrs or {}).get("kind") == "channel"
                    and (existing.attrs or {}).get("kind") != "channel"):
                existing.attrs = {**(existing.attrs or {}), "kind": "channel"}
            # A video is metadata-complete once its fields are enough to rate (a present
            # title); decided API-side (§Stage 1). Set once, when still NULL.
            if (existing.container is False and existing.last_success_dt is None
                    and xform.enough_to_rate(existing)):
                existing.last_success_dt = now
            remap[stub.id] = existing.id
        else:
            if stub.container is False and xform.enough_to_rate(stub):
                stub.last_success_dt = now   # else NULL -> a `meta` job enriches it later
            session.add(stub)
            remap[stub.id] = stub.id
    session.flush()  # persist new stubs so the rel FKs resolve

    # Bulk upsert the edges: the (parent, child) PK dedups, so no per-edge SELECT.
    for rel in graph.rels:
        session.execute(pg_insert(Rel).values(
            parent=remap[rel.parent], child=remap[rel.child],
            channel=rel.channel).on_conflict_do_nothing())

    _set_try_on(session, pl_thing)   # successful container run -> next backoff date (§4.4)
    return _finish(session, run)
