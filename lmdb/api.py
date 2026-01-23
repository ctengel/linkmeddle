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



@app.get("/playlists/", response_model=List[PlaylistSum])
def list_playlist_sums(session: Session = Depends(get_session)):
    # TODO allow search/filtering by channel
    return session.exec(select(PlaylistSum)).all()


@app.get("/playlists/{url}", response_model=PlaylistSum)
def get_playlist_sum(item_id: int, session: Session = Depends(get_session)):
    # TODO include schedules
    return get_or_404(session, PlaylistSum, item_id)


# --- PlaylistSched CRUD --------------------------------------------------------------
@app.post("/schedules/", response_model=PlaylistSched, status_code=status.HTTP_201_CREATED)
def create_playlist_sched(item: PlaylistSched, session: Session = Depends(get_session)):
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@app.get("/schedules/", response_model=List[PlaylistSched])
def list_playlist_scheds(session: Session = Depends(get_session)):
    # TODO allow search/filtering by next_run
    return session.exec(select(PlaylistSched)).all()


@app.get("/schedules/{item_id}", response_model=PlaylistSched)
def get_playlist_sched(item_id: int, session: Session = Depends(get_session)):
    return get_or_404(session, PlaylistSched, item_id)


@app.patch("/schedules/{item_id}", response_model=PlaylistSched)
def update_playlist_sched(item_id: int, item: PlaylistSched, session: Session = Depends(get_session)):
    db_item = get_or_404(session, PlaylistSched, item_id)
    apply_update(db_item, item.dict())
    session.add(db_item)
    session.commit()
    session.refresh(db_item)
    return db_item


#@app.delete("/playlist_scheds/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
#def delete_playlist_sched(item_id: int, session: Session = Depends(get_session)):
#    db_item = get_or_404(session, PlaylistSched, item_id)
#    session.delete(db_item)
#    session.commit()
#    return None

@app.post("/playlist-run", response_model=PlaylistRunResult, status_code=status.HTTP_201_CREATED)
def create_playlist_run(item: PlaylistFull, session: Session = Depends(get_session)):
    """Designed to be called upon playlist completion by postprocessor
    
    Fulfills user story #1, requirement 4

    Also enables user story #2
    """
    # TODO allow partial
    # TODO download count???
    summary = xform.full2sum(item)
    # TODO check for existing summary and update
    # TODO upsert pseudo playlists for channels
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
    # TODO delete old stats???
    session.refresh(summary)
    return PlaylistRunResult(
        summary=summary,
        schedule=sched,
        new_stats=new_stats
    )

# TODO videos endpoint
@app.get("/videos/{extractor}/{video_id}", response_model=VidFull)
def get_video(extractor: str, video_id: str, session: Session = Depends(get_session)):
    statement = select(PlaylistSum).where(PlaylistSum.entries.any(video_id))
    result = session.exec(statement).all()
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return result