from fastapi import FastAPI, Depends, HTTPException, status
from sqlmodel import SQLModel, Session, create_engine, select
from typing import List
import os
import datetime
from .models import PlaylistSchedBase, PlaylistSchedWithStatsAndSum, PlaylistSum, PlaylistSched, PlaylistStats, PlaylistFull, PlaylistSumBase, PlaylistSumWithSched, PlaylistRunResult, PlaylistVid, PlayylistSumWithVids
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



@app.get("/playlists/", response_model=List[PlaylistSumBase])
def list_playlist_sums(extractor: str, channel: str, session: Session = Depends(get_session)):
    assert extractor and channel, "extractor and channel are required"
    statement = select(PlaylistSum).where(PlaylistSum.extractor_id == extractor,
                                         PlaylistSum.channel == channel)
    return session.exec(statement).all()


@app.get("/playlists/{url}", response_model=PlaylistSumWithSched)
def get_playlist_sum(url: str, session: Session = Depends(get_session)):
    pl = session.exec(select(PlaylistSum).where(PlaylistSum.webpage_url == url)).first()
    if not pl:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    sched = session.exec(select(PlaylistSched).where(PlaylistSched.webpage_url == pl.webpage_url)).all()
    pl_with_sched = PlaylistSumWithSched(**pl.dict(), schedules=list(sched))
    return pl_with_sched


# --- PlaylistSched CRUD --------------------------------------------------------------
@app.post("/schedules/", response_model=PlaylistSchedWithStatsAndSum, status_code=status.HTTP_201_CREATED)
def create_playlist_sched(item: PlaylistSchedBase, session: Session = Depends(get_session)):
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@app.get("/schedules/", response_model=List[PlaylistSchedBase])
def list_playlist_scheds(next_run: datetime.date | None = None, extractor: str | None = None, session: Session = Depends(get_session)):
    statement = select(PlaylistSched)
    if next_run is not None:
        statement = statement.where(PlaylistSched.next_run == next_run)
    if extractor is not None:
        statement = statement.where(PlaylistSched.extractor_id == extractor)
    return session.exec(statement).all()

@app.get("/schedules/{item_id}", response_model=PlaylistSchedWithStatsAndSum)
def get_playlist_sched(item_id: int, session: Session = Depends(get_session)):
    pl = get_or_404(session, PlaylistSched, item_id)
    #stats = session.exec(select(PlaylistStats).where(PlaylistStats.playlist_id == pl.id)).all()
    summary = session.exec(select(PlaylistSum).where(PlaylistSum.webpage_url == pl.webpage_url)).first()
    return PlaylistSchedWithStatsAndSum(**pl.dict(), summary=summary) # runs=list(stats),


@app.patch("/schedules/{item_id}", response_model=PlaylistSchedWithStatsAndSum)
def update_playlist_sched(item_id: int, item: PlaylistSchedBase, session: Session = Depends(get_session)):
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
    base_summary = xform.full2sum(item)
    summary = PlaylistSum.model_validate(base_summary)
    existing_pl = session.exec(select(PlaylistSum).where(PlaylistSum.webpage_url == summary.webpage_url)).first()
    if not existing_pl:
        session.add(summary)
        existing_pl = summary
    for vid in base_summary.entries:
        pl_vid = PlaylistVid(vid_id=vid, playlist_id=existing_pl.playlist_id)
        session.add(pl_vid)
    session.commit()
    # TODO upsert pseudo playlists for channels
    # TODO allow passing in schedule id and/or matching multiple schedules
    sched = session.exec(select(PlaylistSched).where(PlaylistSched.webpage_url == summary.webpage_url)).first()
    new_stats = None
    if sched:
        # TODO handele following call with join/relationship
        existing_stats = sched.runs
        new_stats = xform.full2stats(item)
        sched, updated_stats, new_stats = xform.add_new_run(sched, list(existing_stats), new_stats)
        session.add(sched)
        session.add(new_stats)
    session.commit()
    # TODO delete old stats???
    session.refresh(summary)
    return PlaylistRunResult(
        summary=PlayylistSumWithVids(**summary.dict()),
        schedule=sched if sched else None,
        new_stats=new_stats if sched else None
    )

@app.get("/videos/{extractor}/{video_id}", response_model=List[PlaylistSumBase])
def get_video(extractor: str, video_id: str, session: Session = Depends(get_session)):
    # TODO fix below query
    statement = select(PlaylistSum).join(PlaylistVid).where(PlaylistVid.vid_id == video_id)
    result = session.exec(statement).all()
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return result
