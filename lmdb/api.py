"""LMDB API implementation using FastAPI

Impports FastAPI and SQLModel to provide a RESTful API for managing playlist schedules and summaries.
"""

# TODO factor out complicated return logic into separate functions, sometimes xform.py

from typing import List
import os
import datetime
from fastapi import FastAPI, Depends, HTTPException, status
from sqlmodel import SQLModel, Session, create_engine, select
from .models import PlaylistSchedBase, PlaylistSchedWithStatsAndSum, PlaylistSum, PlaylistSched, PlaylistStats, PlaylistSumBase, PlaylistSumWithSched, PlaylistRunResult, PlaylistSumWithVids, PlaylistVid, PlaylistRunCreate, PlaylistStatsStrHash
from . import xform

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./lmdb.db")
engine = create_engine(DATABASE_URL, echo=False)

app = FastAPI(title="LinkMeddle LMDB API")


def get_session():
    """get a DB session"""
    with Session(engine) as session:
        yield session


@app.on_event("startup")
def on_startup():
    """Create DB tables on startup"""
    SQLModel.metadata.create_all(engine)


# --- Generic helpers -----------------------------------------------------------------
def get_or_404(session: Session, model, item_id: int):
    """Get an item or raise 404"""
    statement = select(model).where(model.sched_id == item_id)
    result = session.exec(statement).one_or_none()
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{model.__name__} not found")
    return result


def apply_update(instance, update_data: dict):
    """Apply update data to an instance"""
    # TODO replace this with sqlmodel's built-in update mechanism if possible
    for key, value in update_data.items():
        if key == "sched_id":
            continue
        if value is None:
            continue
        setattr(instance, key, value)
    return instance


# --- PlaylistSum CRUD ----------------------------------------------------------------



@app.get("/playlists/", response_model=List[PlaylistSumBase])
def list_playlist_sums(extractor: str,
                       channel: str,
                       session: Session = Depends(get_session)):
    """List of known playlists for a given channel
    
    :param extractor: yt-dlp extractor ID
    :type extractor: str
    :param channel: channel identifier
    :type channel: str
    :param session: auto-injected DB session
    :type session: Session
    """
    assert extractor and channel, "extractor and channel are required"
    statement = select(PlaylistSum).where(PlaylistSum.extractor_id == extractor,
                                         PlaylistSum.channel == channel)
    return session.exec(statement).all()


@app.get("/playlists/{url:path}", response_model=PlaylistSumWithSched)
def get_playlist_sum(url: str, session: Session = Depends(get_session)):
    """Get a playlist summary by URL
    
    :param url: UTL of the playlist
    :type url: str
    :param session: auto-injected DB session
    :type session: Session
    """
    pl = session.exec(select(PlaylistSum).where(PlaylistSum.webpage_url == url)).one_or_none()
    if not pl:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    sched = session.exec(select(PlaylistSched).where(PlaylistSched.webpage_url == pl.webpage_url)).all()
    pl_with_sched = PlaylistSumWithSched.model_validate(pl, update={'schedules': list(sched), 'entries': [pv.vid_id for pv in pl.entries]})
    return pl_with_sched


# --- PlaylistSched CRUD --------------------------------------------------------------
@app.post("/schedules/", response_model=PlaylistSchedWithStatsAndSum, status_code=status.HTTP_201_CREATED)
def create_playlist_sched(item: PlaylistSchedBase,
                          session: Session = Depends(get_session)):
    """Create a new playlist schedule"""
    item = PlaylistSched.model_validate(item)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@app.get("/schedules/", response_model=List[PlaylistSchedBase])
def list_playlist_scheds(next_run: datetime.date | None = None,
                         extractor: str | None = None,
                         session: Session = Depends(get_session)):
    """List of playlist schedules, optionally filtered by next_run date and/or extractor ID"""
    statement = select(PlaylistSched)
    if next_run is not None:
        statement = statement.where(PlaylistSched.next_run == next_run)
    if extractor is not None:
        statement = statement.where(PlaylistSched.extractor_id == extractor)
    return session.exec(statement).all()

@app.get("/schedules/{item_id}", response_model=PlaylistSchedWithStatsAndSum)
def get_playlist_sched(item_id: int, session: Session = Depends(get_session)):
    """Get a playlist schedule by ID, including stats and summary"""
    pl = get_or_404(session, PlaylistSched, item_id)
    #stats = session.exec(select(PlaylistStats).where(PlaylistStats.playlist_id == pl.id)).all()
    summary = session.exec(select(PlaylistSum).where(PlaylistSum.webpage_url == pl.webpage_url)).one()
    stats = session.exec(select(PlaylistStats).where(PlaylistStats.sched_id == pl.sched_id)).all()
    return PlaylistSchedWithStatsAndSum(**pl.dict(),
                                     runs=[PlaylistStatsStrHash.model_validate(s, update={"entries_hash": s.entries_hash.hex()}) for s in stats],
                                     summary=summary)


@app.patch("/schedules/{item_id}", response_model=PlaylistSchedWithStatsAndSum)
def update_playlist_sched(item_id: int, item: PlaylistSchedBase, session: Session = Depends(get_session)):
    """Update a playlist schedule by ID"""
    db_item = get_or_404(session, PlaylistSched, item_id)
    apply_update(db_item, item.dict())
    session.add(db_item)
    session.commit()
    session.refresh(db_item)
    summary = session.exec(select(PlaylistSum).where(PlaylistSum.webpage_url == db_item.webpage_url)).one()
    stats = session.exec(select(PlaylistStats).where(PlaylistStats.sched_id == db_item.sched_id)).all()
    return PlaylistSchedWithStatsAndSum(**db_item.dict(),
                                     runs=[PlaylistStatsStrHash.model_validate(s, update={"entries_hash": s.entries_hash.hex()}) for s in stats],
                                     summary=summary)


#@app.delete("/playlist_scheds/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
#def delete_playlist_sched(item_id: int, session: Session = Depends(get_session)):
#    db_item = get_or_404(session, PlaylistSched, item_id)
#    session.delete(db_item)
#    session.commit()
#    return None

def upsert_vid(session: Session, vid_id: str, playlist_id: int):
    """Upsert a PlaylistVid entry"""
    pl_vid = session.exec(select(PlaylistVid).where(PlaylistVid.vid_id == vid_id,
                                                    PlaylistVid.playlist_id == playlist_id)).one_or_none()
    if not pl_vid:
        pl_vid = PlaylistVid(vid_id=vid_id, playlist_id=playlist_id)
        session.add(pl_vid)
        session.commit()
        session.refresh(pl_vid)
    return pl_vid

@app.post("/playlist-run", response_model=PlaylistRunResult)
def create_playlist_run(run_info: PlaylistRunCreate, session: Session = Depends(get_session)):
    """Designed to be called upon playlist completion by postprocessor
    
    Fulfills user story #1, requirement 4

    Also enables user story #2
    """
    # TODO rewrite this whole function to use upserts and relationships better
    item = run_info.playlist
    # TODO allow partial
    base_summary = xform.full2sum(item)
    summary = PlaylistSum.model_validate(base_summary, update={"entries": []})
    existing_pl = session.exec(select(PlaylistSum).where(PlaylistSum.webpage_url == summary.webpage_url)).one_or_none()
    if not existing_pl:
        session.add(summary)
        session.commit()
        session.refresh(summary)
        existing_pl = summary
    for vid in item.entries:
        assert existing_pl.playlist_id is not None
        pl_vid = upsert_vid(session, xform.entry2text(vid), existing_pl.playlist_id)
        # Also create pseudo-channel playlist if needed
        uploader_url = xform.vid_uploader_url(vid)
        ul_pseudo = session.exec(select(PlaylistSum).where(PlaylistSum.webpage_url == uploader_url)).one_or_none()
        if not ul_pseudo:
            ul_pseudo = PlaylistSum(
                extractor_id=existing_pl.extractor_id,
                id=None,
                title=None,
                webpage_url=uploader_url,
                channel=vid.channel.uploader,
                entries=[],
                playlist_id=None,
                pseudo_channel=True,
                modified_date=None,
                playlist_count=None
            )
        ul_pseudo.pseudo_channel = True
        session.add(ul_pseudo)
        session.commit()
        session.refresh(ul_pseudo)
        assert ul_pseudo.playlist_id is not None
        ul_vid = upsert_vid(session, xform.entry2text(vid), ul_pseudo.playlist_id)
    session.commit()
    # TODO allow passing in schedule id and/or matching multiple schedules
    sched = session.exec(select(PlaylistSched).where(PlaylistSched.webpage_url == existing_pl.webpage_url)).first()
    new_stats = None
    new_stats_db = None
    if sched:
        # TODO handele following call with join/relationship
        existing_stats = sched.runs
        new_stats = xform.full2stats(item, download_count=run_info.download_count)
        sched, _, new_stats = xform.add_new_run(sched, list(existing_stats), new_stats)
        session.add(sched)
        new_stats_db = PlaylistStats.model_validate(new_stats)
        assert sched.sched_id is not None
        new_stats_db.sched_id = sched.sched_id
        session.add(new_stats_db)
        session.commit()
        session.refresh(sched)
        session.refresh(new_stats_db)
    # TODO delete old stats???
    session.refresh(existing_pl)
    assert new_stats_db is not None
    return PlaylistRunResult(
        summary=PlaylistSumWithVids.model_validate(existing_pl, update={"entries": [pv.vid_id for pv in existing_pl.entries]}),
        schedule=sched if sched else None,
        new_stats=PlaylistStatsStrHash.model_validate(new_stats_db, update={"entries_hash": new_stats_db.entries_hash.hex()}) if sched else None
    )

@app.get("/videos/{extractor}/{video_id}", response_model=List[PlaylistSumBase])
def get_video(extractor: str, video_id: str, session: Session = Depends(get_session)):
    """Get playlists containing a given video ID for a specific extractor"""
    statement = select(PlaylistSum).join(PlaylistVid).where(PlaylistVid.vid_id == video_id,
                                                            PlaylistSum.extractor_id == extractor)
    result = session.exec(statement).all()
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return result
