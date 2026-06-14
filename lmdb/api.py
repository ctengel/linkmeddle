"""LMDB API (V4): thing/rel/run CRUD + add-a-thing-by-URL.

The thing-centric surface from LM-V4-DESIGN.md §3.3. Everything is a `thing`
(playlist / video / channel). Job dispatch + result-ingest endpoints (`/jobs/...`)
are Phase 1, not here. URL-classify is deferred to 4.x: `POST /things/` just records
the URL; the worker fills extractor/native_id/real type on result ingest later.

Note the SQLModel select gotcha (LM-V4-DESIGN.md §6.4): filters on nullable columns
use SQL `== None` / `!= None`, never Python `is None` (which silently evaluates wrong).
"""

import os
import uuid
import datetime
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, create_engine, select
from . import models
from .models import (Thing, Rel, Run, ThingRead, ThingWithRelated, RelatedThing,
                     RunRead, ThingAdd, ThingPatch)

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
    'playlist' ("unknown -> assume playlist"). extractor_key/native_id are filled in
    later by the worker. Idempotent on URL (returns the existing thing with 200).
    """
    existing = session.exec(select(Thing).where(Thing.url == item.url)).one_or_none()
    if existing:
        response.status_code = status.HTTP_200_OK
        return existing
    grade = (item.rating or "B").upper()
    if grade not in ADD_GRADES:
        raise HTTPException(status_code=422,
                            detail=f"rating must be one of {sorted(ADD_GRADES)} at add time")
    thing = Thing(url=item.url, type=item.type, human_rating=GRADE_VALUES[grade])
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

    (The 'raise-to-eligible -> try_on=today' side-effect is Task 2.1; title backfill is
    Task 1.1.)
    """
    thing = get_thing_or_404(session, thing_id)
    data = item.model_dump(exclude_unset=True)
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
    session.add(thing)
    session.commit()
    session.refresh(thing)
    return thing
