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
from sqlalchemy import func, or_
from fastapi import FastAPI, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, create_engine, select
from . import models, xform
from .models import (Thing, Rel, Run, ThingRead, ThingWithRelated, RelatedThing,
                     RunRead, ThingAdd, ThingPatch, ClaimRequest, JobClaim, RunResultIn)

# What the worker should do with a claimed thing, by its type (§4.5 dispatch result).
ACTION_BY_TYPE = {"playlist": "pull", "video": "download"}

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


def _effective_rating(thing: Thing) -> float:
    """Effective rating = human, else machine, else 0.0 (C) — COALESCE in Python (§2.4)."""
    if thing.human_rating is not None:
        return thing.human_rating
    if thing.machine_rating is not None:
        return thing.machine_rating
    return 0.0


def _is_eligible(thing_type: str, rating: float) -> bool:
    """Does `rating` clear the run-eligibility floor for this thing type (§4.5)?"""
    floor = 0.5 if thing_type == "video" else -0.5   # video=B, playlist/other=C
    return rating >= floor


def _set_try_on(session: Session, thing: Thing) -> None:
    """Advance thing.try_on from its run history via the Fibonacci backoff (§4.4, Task 1.4).

    Re-queries the thing's runs (the just-recorded run is autoflushed in, so it counts).
    """
    runs = session.exec(select(Run).where(Run.thing_id == thing.id)).all()
    thing.try_on = xform.next_try_on(_effective_rating(thing), runs)


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


def _related(session: Session, thing_id: uuid.UUID,
             direction: Optional[str]) -> list[RelatedThing]:
    """rel neighbors in both directions (or one if direction is 'child'/'parent')."""
    out: list[RelatedThing] = []
    if direction in (None, "child"):
        for rel, thing in session.exec(
                select(Rel, Thing).where(Rel.parent == thing_id, Rel.child == Thing.id)).all():
            out.append(RelatedThing(direction="child", rel_type=rel.type,
                                    thing=ThingRead.model_validate(thing)))
    if direction in (None, "parent"):
        for rel, thing in session.exec(
                select(Rel, Thing).where(Rel.child == thing_id, Rel.parent == Thing.id)).all():
            out.append(RelatedThing(direction="parent", rel_type=rel.type,
                                    thing=ThingRead.model_validate(thing)))
    return out


# --- Things ---------------------------------------------------------------------------

@app.post("/things/", response_model=ThingRead, status_code=status.HTTP_201_CREATED)
def add_thing(item: ThingAdd, response: Response, session: Session = Depends(get_session)):
    """Add a thing by URL (the human entry point).

    Stores the URL with a default rating of B (override A/C); `type` defaults to
    'playlist' ("unknown -> assume playlist"). `bucket` (OI storage home) is required —
    no server default ([A10]). Optional `cookies`/`lpm_lib` are stored as soft hints in
    `attrs` ([A11]). extractor_key/native_id are filled in later by the worker. Idempotent
    on URL (returns the existing thing with 200, bucket unchanged — bucket is immutable).
    """
    existing = session.exec(select(Thing).where(Thing.url == item.url)).one_or_none()
    if existing:
        response.status_code = status.HTTP_200_OK
        return existing
    grade = (item.rating or "B").upper()
    if grade not in ADD_GRADES:
        raise HTTPException(status_code=422,
                            detail=f"rating must be one of {sorted(ADD_GRADES)} at add time")
    attrs: dict = {}
    if item.cookies is not None:
        attrs["cookies"] = item.cookies
    if item.lpm_lib is not None:
        attrs["lpm_lib"] = item.lpm_lib
    thing = Thing(url=item.url, type=item.type, human_rating=GRADE_VALUES[grade],
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
                due: bool = False, needs_rating: bool = False, new: bool = False,
                failing: bool = False, url: Optional[str] = None,
                extractor: Optional[str] = None, native_id: Optional[str] = None,
                session: Session = Depends(get_session)):
    """List/search things. Backs every list view + the status dashboard.

    `extractor` + `native_id` is the V4 replacement for V3 GET /videos/{ex}/{id} (#102).
    """
    stmt = select(Thing)
    if type is not None:
        stmt = stmt.where(Thing.type == type)
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
    return session.exec(stmt).all()


@app.get("/things/{thing_id}", response_model=ThingWithRelated)
def get_thing(thing_id: uuid.UUID, include: Optional[str] = None,
              session: Session = Depends(get_session)):
    """Get one thing; `?include=related` also returns its rel neighbors."""
    thing = get_thing_or_404(session, thing_id)
    related = _related(session, thing_id, None) if include == "related" else []
    return ThingWithRelated(**ThingRead.model_validate(thing).model_dump(), related=related)


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
    old_rating = _effective_rating(thing)
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
        new_rating = _effective_rating(thing)
        if (thing.best_oi is None and new_rating > old_rating
                and _is_eligible(thing.type, new_rating)):
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


@app.post("/jobs/claim", response_model=JobClaim,
          responses={204: {"description": "Nothing due"}})
def claim_job(item: ClaimRequest, session: Session = Depends(get_session)):
    """Prioritized dispatch: claim the single highest-priority due job (§4.2/§4.5).

    The API owns ordering (the runner never queries `thing`): one ordering spans both job
    types — playlist-before-video, then rating DESC, then `try_on` ASC. The row is claimed
    with `SELECT ... FOR UPDATE SKIP LOCKED` so concurrent workers never get the same thing
    (correct the day a 2nd worker appears; single worker in 4.0). On a hit the run is created
    here (`success=NULL` in-progress marker, `worker` set) and its id returned; 204 if
    nothing is due.

    Machine rating is read from the stored column via COALESCE; compute-on-read is Task 2.2.
    """
    rating = func.coalesce(Thing.human_rating, Thing.machine_rating)
    today = _today()
    # Stage-1 playlist pull: grade >= C band, due, not already succeeded today.
    playlist_branch = sa.and_(
        Thing.type == "playlist", rating >= -0.5, Thing.try_on <= today,
        or_(Thing.last_success_dt == None,  # noqa: E711  (SQL IS NULL)
            func.date(Thing.last_success_dt) < today))
    # Stage-2 video download: grade >= B band, never acquired, due.
    video_branch = sa.and_(
        Thing.type == "video", rating >= 0.5,
        Thing.best_oi == None, Thing.try_on <= today)  # noqa: E711
    stmt = (select(Thing)
            .where(or_(playlist_branch, video_branch))
            .order_by(sa.desc(Thing.type == "playlist"), rating.desc(), Thing.try_on.asc())
            .limit(1).with_for_update(skip_locked=True))
    thing = session.exec(stmt).first()
    if thing is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    run = Run(thing_id=thing.id, worker=item.worker,
              starttime=models.naive_utcnow(), success=None)
    session.add(run)
    session.commit()
    session.refresh(run)
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
    return JobClaim(run_id=run.id, thing=ThingRead.model_validate(thing),
                    action=ACTION_BY_TYPE[thing.type], cookies=cookies)


@app.post("/jobs/{run_id}/result", response_model=RunRead)
def submit_result(run_id: uuid.UUID, item: RunResultIn,
                  session: Session = Depends(get_session)):
    """Stage-1 ingest: record a run's result and upsert the thing/rel graph it found.

    The V4 rewrite of V3's POST /playlist-run, one endpoint for both job kinds. A Stage-2
    *video* download result sets `best_oi` (the OI file UUID), backfills NULL identity
    (extractor_key/native_id) from the download, and marks the thing acquired (`try_on=NULL`).
    A Stage-1 *playlist* pull, on success, fans out into a stub `thing` per entry (+ channels)
    and the `rel` edges (#137), backfilling NULL fields we already knew (#147); on failure it
    records the failure only (C8). The Fibonacci `try_on` backoff is Task 1.4.
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

    # Stage-2 (video download) result — distinct from the Stage-1 playlist fan-out below.
    if pl_thing is not None and pl_thing.type == "video":
        if item.success:
            incoming = Thing(type="video", bucket=pl_thing.bucket,
                             extractor_key=item.extractor_key, native_id=item.native_id)
            _apply_backfill(session, pl_thing, incoming)  # NULL-only identity backfill (#147)
            pl_thing.best_oi = item.best_oi
            pl_thing.last_success_dt = now
            pl_thing.last_failure_dt = None
            pl_thing.try_on = None       # acquired; never re-fetch (§2.5)
        else:
            pl_thing.last_failure_dt = now
            _set_try_on(session, pl_thing)   # failure backoff (§4.4)
        session.add(run)
        session.commit()
        session.refresh(run)
        return _run_read(run)

    if not item.success:
        if pl_thing is not None:
            pl_thing.last_failure_dt = now
            _set_try_on(session, pl_thing)   # failure backoff (§4.4)
        session.add(run)
        session.commit()
        session.refresh(run)
        return _run_read(run)

    if item.playlist is None:
        raise HTTPException(status_code=422,
                            detail="playlist is required on a successful playlist run")
    # Stubs inherit the dispatched playlist thing's bucket (immutable, [A10]) and its
    # propagated soft hints (attrs.cookies/lpm_lib -> video stubs, [A11]).
    graph = xform.pl_full2things(item.playlist, bucket=pl_thing.bucket,
                                 parent_attrs=pl_thing.attrs)

    run.entries_hash = xform.pl_hash(item.playlist.entries)
    run.playlist_count = xform.reconcile_count(item.playlist)

    # The dispatched thing IS the playlist: backfill it, correct its type, mark success.
    _apply_backfill(session, pl_thing, graph.playlist)
    pl_thing.type = graph.playlist.type
    pl_thing.last_success_dt = now

    remap = {graph.playlist.id: pl_thing.id}
    for stub in graph.videos + graph.channels:
        existing = _find_thing(session, stub)
        if existing is not None:
            _apply_backfill(session, existing, stub)
            remap[stub.id] = existing.id
        else:
            session.add(stub)
            remap[stub.id] = stub.id
    session.flush()  # persist new stubs so the rel FKs resolve

    for rel in graph.rels:
        parent, child = remap[rel.parent], remap[rel.child]
        if session.get(Rel, (parent, child, rel.type)) is None:
            session.add(Rel(parent=parent, child=child, type=rel.type))

    _set_try_on(session, pl_thing)   # successful playlist run -> next backoff date (§4.4)
    session.commit()
    session.refresh(run)
    return _run_read(run)
