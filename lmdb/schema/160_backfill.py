"""One-off #160 backfill: link already-downloaded videos to their uploader channels.

The #160 fan-out links a video to its channel at ingest time (Stage-1 pull, Stage-2
meta/download), so a video acquired before it landed is never linked: it is terminal
(`try_on` NULL, meta gated out) and nothing revisits it — and for the id-only sites
(twitch/vk, ...) a playlist re-pull can't heal it either, since their flat entries carry
no uploader fields at all. But the raw yt-dlp info dict of the download survives in the
run's `data_json`, so this replays just the channel fan-out from there, through the
production path (`run_bknd._pull_chan` -> `api._fanout_video_channel`): identical dedup
(the site-scoped id join), identical url-less stub shape + provenance, and a later direct
channel pull still absorbs the stubs (`_converge_urlless_channel`) exactly as if the
video had been downloaded today.

Idempotent — a re-run finds no orphans (linked videos now have a channel edge). NB parity
cuts both ways: an orphan whose uploader came with a URL spawns a regular URL-keyed
channel container, claimable as a Stage-1 pull (`try_on` today), just as a fresh download
would.

Run once from the repo root, after the #160 code is deployed:

    PYTHONPATH=. DATABASE_URL=postgresql+psycopg:///lmdb python lmdb/schema/160_backfill.py
"""

import sqlalchemy as sa
from sqlmodel import Session, select

from lmdb import run_bknd
from lmdb.api import engine, _fanout_video_channel
from lmdb.models import Rel, Run, Thing


def backfill(session: Session) -> dict[str, int]:
    """Link every metadata-complete leaf that has no channel edge; returns outcome counts."""
    counts = {"linked": 0, "no_uploader": 0, "no_video_run": 0}
    orphan = ~sa.exists().where(sa.and_(Rel.child == Thing.id, Rel.channel == sa.true()))
    vids = session.exec(
        select(Thing).where(Thing.container == False,  # noqa: E712  (SQL IS FALSE, never NULL)
                            Thing.last_success_dt != None,  # noqa: E711
                            orphan)).all()
    for vid in vids:
        run = session.exec(
            select(Run).where(Run.thing_id == vid.id,
                              Run.success == True,  # noqa: E712
                              Run.data_json != None)  # noqa: E711
            .order_by(sa.desc(Run.starttime))).first()
        info = run.data_json if run is not None else None
        # A stray container-shaped data_json (a re-classified thing's old pull) is not this
        # video's extract; skip rather than mis-read a playlist's uploader as the video's.
        if not isinstance(info, dict) or run_bknd.is_container(info):
            counts["no_video_run"] += 1
            continue
        chan = run_bknd._pull_chan(info)
        if not chan.url and chan.native_id is None:
            # Nothing to key a channel on (thing_from_chan would return None) — includes
            # v3-migration synthetic runs whose data_json is just {'source': ...}.
            counts["no_uploader"] += 1
            continue
        _fanout_video_channel(session, vid, chan)
        counts["linked"] += 1
    return counts


def main() -> None:
    with Session(engine) as session:
        counts = backfill(session)
        session.commit()
    print(f"linked {counts['linked']} videos to channels; skipped "
          f"{counts['no_uploader']} with no uploader info, "
          f"{counts['no_video_run']} with no single-video run data")


if __name__ == "__main__":
    main()
