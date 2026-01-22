from fastapi import FastAPI, Depends, HTTPException, status
from sqlmodel import SQLModel, Session, create_engine, select
from typing import List
import os
from .models import PlaylistSum, PlaylistSched, PlaylistStats, PlaylistFull
from . import xform

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lmdb.db")
engine = create_engine(DATABASE_URL, echo=False)

app = FastAPI(title="LinkMeddle LMDB API")


def get_session():
    with Session(engine) as session:
        yield session


@app.on_event("startup")
def on_startup():
    SQLModel.metadata.create_all(engine)


# --- Generic helpers -----------------------------------------------------------------
def get_or_404(session: Session, model, id: int):
    statement = select(model).where(model.id == id)
    result = session.exec(statement).first()
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{model.__name__} not found")
    return result


def apply_update(instance, update_data: dict):
    for key, value in update_data.items():
        if key == "id":
            continue
        setattr(instance, key, value)
    return instance


# --- PlaylistSum CRUD ----------------------------------------------------------------



@app.get("/playlist_sums/", response_model=List[PlaylistSum])
def list_playlist_sums(session: Session = Depends(get_session)):
    # TODO allow search/filtering by video ids, channel, extractor, etc
    return session.exec(select(PlaylistSum)).all()


@app.get("/playlist_sums/{item_id}", response_model=PlaylistSum)
def get_playlist_sum(item_id: int, session: Session = Depends(get_session)):
    return get_or_404(session, PlaylistSum, item_id)


@app.put("/playlist_sums/{item_id}", response_model=PlaylistSum)
def update_playlist_sum(item_id: int, item: PlaylistSum, session: Session = Depends(get_session)):
    db_item = get_or_404(session, PlaylistSum, item_id)
    apply_update(db_item, item.dict())
    session.add(db_item)
    session.commit()
    session.refresh(db_item)
    return db_item


# --- PlaylistSched CRUD --------------------------------------------------------------
@app.post("/playlist_scheds/", response_model=PlaylistSched, status_code=status.HTTP_201_CREATED)
def create_playlist_sched(item: PlaylistSched, session: Session = Depends(get_session)):
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@app.get("/playlist_scheds/", response_model=List[PlaylistSched])
def list_playlist_scheds(session: Session = Depends(get_session)):
    return session.exec(select(PlaylistSched)).all()


@app.get("/playlist_scheds/{item_id}", response_model=PlaylistSched)
def get_playlist_sched(item_id: int, session: Session = Depends(get_session)):
    return get_or_404(session, PlaylistSched, item_id)


@app.put("/playlist_scheds/{item_id}", response_model=PlaylistSched)
def update_playlist_sched(item_id: int, item: PlaylistSched, session: Session = Depends(get_session)):
    db_item = get_or_404(session, PlaylistSched, item_id)
    apply_update(db_item, item.dict())
    session.add(db_item)
    session.commit()
    session.refresh(db_item)
    return db_item


@app.delete("/playlist_scheds/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_playlist_sched(item_id: int, session: Session = Depends(get_session)):
    db_item = get_or_404(session, PlaylistSched, item_id)
    session.delete(db_item)
    session.commit()
    return None


# --- PlaylistStats CRUD --------------------------------------------------------------
# TODO put playlist stats under playlist sched


@app.get("/playlist_stats/", response_model=List[PlaylistStats])
def list_playlist_stats(session: Session = Depends(get_session)):
    return session.exec(select(PlaylistStats)).all()


@app.get("/playlist_stats/{item_id}", response_model=PlaylistStats)
def get_playlist_stats(item_id: int, session: Session = Depends(get_session)):
    return get_or_404(session, PlaylistStats, item_id)


@app.delete("/playlist_stats/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_playlist_stats(item_id: int, session: Session = Depends(get_session)):
    # TODO better way to prune old stats?
    db_item = get_or_404(session, PlaylistStats, item_id)
    session.delete(db_item)
    session.commit()
    return None

@app.post("/playlist_run", response_model=PlaylistRunResult, status_code=status.HTTP_201_CREATED)
def create_playlist_run(item: PlaylistFull, session: Session = Depends(get_session)):
    """Designed to be called upon playlist completion by postprocessor
    
    Fulfills user story #1, requirement 4

    Also enables user story #2
    """
    # TODO allow partial
    # TODO download count???
    summary = xform.full2sum(item)
    # TODO check for existing summary and update
    session.add(summary)
    sched = session.exec(select(PlaylistSched).where(PlaylistSched.playlist_id == summary.id)).first()
    # TODO consider creating schedule or inserting without sched if none exists
    if sched:
        existing_stats = session.exec(select(PlaylistStats).where(PlaylistStats.playlist_id == summary.id)).all()
        new_stats = xform.full2stats(item)
        sched, updated_stats, new_stat = xform.add_new_run(sched, list(existing_stats), new_stats)
        session.add(sched)
        session.add(new_stats)
    session.commit()
    session.refresh(summary)
    return PlaylistRunResult(
        summary=summary,
        schedule=sched,
        new_stats=new_stats
    )