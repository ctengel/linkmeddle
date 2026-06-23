#!/usr/bin/env python3
"""V3 SQLite → V4 PostgreSQL migration script. See GitHub issue #155.

Usage:
    DATABASE_URL=postgresql+psycopg:///lmdb \\
    OBJIDX_URL=http://... OBJIDX_AUTH=user \\
    python lmdb/schema/155.py --v3-db /path/to/v3.db [--default-bucket BUCKET] [--dry-run]

Mapping:
  - playlistsched rows    → thing(container=True, human_rating=1.0, try_on=today)
  - other playlistsum     → thing(container=True, human_rating=None,  try_on=today)
  - playlistvid entries   → thing(container=False, human_rating=None)
  - OI lookup per video   → thing.best_oi (acquired: try_on=None, last_success_dt=now; else today)
  - playlistvid rows      → rel(parent=playlist, child=video, channel=False)
  - every thing           → one synthetic run(worker="v3-migration", success=True)
"""

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, date

from sqlmodel import Session, create_engine
import os

from obj_idx import client as oic
from lmdb.models import Thing, Rel, Run, naive_utcnow


def _get_oi_uuid(oi, extractor_key: str, native_id: str):
    """Return OI file UUID for a downloaded video, or None if not found."""
    search_key = f"{extractor_key.lower()} {native_id}"
    try:
        files = oi.search_files({"extra": f"ytdl-id={search_key}"})
    except Exception as exc:
        print(f"  WARN: OI search failed for {search_key!r}: {exc}", file=sys.stderr)
        return None, None
    for f in files:
        if not f.object or not f.object.get("completed"):
            continue
        if f.info.get("partial"):
            continue
        ek = f.info.get("extra", {}).get("ytdl-extractor", "")
        if ek.lower() == extractor_key.lower():
            #print(f.info)
            raw = f.uuid
            url = f.info.get('url')
            return (uuid.UUID(str(raw)) if not isinstance(raw, uuid.UUID) else raw, url)
    return None, None

def _ves(inp):
    if inp == 'youtube:tab':
        return 'youtube'
    return inp

def _pes(inp):
    if inp == 'youtube:tab':
        return 'youtubetab'
    return inp


def main():
    parser = argparse.ArgumentParser(description="Migrate V3 SQLite DB to V4 PostgreSQL")
    parser.add_argument("--v3-db", required=True, help="Path to V3 SQLite database file")
    parser.add_argument("--default-bucket", default=None,
                        help="Fallback OI bucket for playlists with no schedule")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print counts only; do not write to V4 DB")
    args = parser.parse_args()

    now = naive_utcnow()
    today = now.date()

    # --- V3 -------------------------------------------------------------------
    v3 = sqlite3.connect(args.v3_db)
    v3.row_factory = sqlite3.Row

    playlists = v3.execute("SELECT * FROM playlistsum").fetchall()
    schedules = {r["webpage_url"]: r
                 for r in v3.execute("SELECT * FROM playlistsched").fetchall()}
    vid_rows = v3.execute("SELECT * FROM playlistvid").fetchall()

    playlists = [dict(x) for x in playlists]
    for x in playlists:
        x['extractor_id'] = _pes(x['extractor_id'])
    vid_rows = [dict(x) for x in vid_rows]
    for x in vid_rows:
        x['extractor_id'] = _ves(x['extractor_id'])

    # --- OI client ------------------------------------------------------------
    oi = oic.get_obj_idx_env()

    # --- Build playlist things ------------------------------------------------
    pl_thing_map: dict[int, Thing] = {}
    skipped_pl = 0
    for pl in playlists:
        pl_id = pl["playlist_id"]
        sched = schedules.get(pl["webpage_url"])
        bucket = sched["oi_bucket"] if sched else args.default_bucket
        if not bucket:
            print(f"SKIP playlist {pl_id} ({pl['webpage_url']!r}): no bucket (pass --default-bucket)",
                  file=sys.stderr)
            skipped_pl += 1
            continue
        extractor_key = pl["extractor_id"]
        thing = Thing(
            container=True,
            url=pl["webpage_url"] or None,
            native_id=pl["id"] or None,
            extractor_key=extractor_key.lower() if extractor_key else None,
            title=pl["title"] or None,
            bucket=bucket,
            human_rating=1.0 if sched else None,
            try_on=today,
            created_dt=now,
        )
        pl_thing_map[pl_id] = thing
    
    for y in pl_thing_map.values():
        if y.extractor_key == 'youtubetab':
            y.extractor_key = None
            y.native_id = None
    for y in sorted((x.native_id, x.extractor_key, x.url) for x in pl_thing_map.values() if x.native_id is not None):
        print(y)

    # --- Collect per-video bucket (inherit from first scheduled parent) --------
    vid_bucket_map: dict[tuple, str] = {}   # (vid_id, extractor_id) → bucket
    for row in vid_rows:
        key = (row["vid_id"], row["extractor_id"])
        parent = pl_thing_map.get(row["playlist_id"])
        if parent is None:
            continue
        if key not in vid_bucket_map:
            vid_bucket_map[key] = parent.bucket
        elif vid_bucket_map[key] != parent.bucket:
            print(f"WARN: video {key} has conflicting parent buckets "
                  f"({vid_bucket_map[key]!r} vs {parent.bucket!r}); keeping first",
                  file=sys.stderr)

    # --- Build video things (with OI lookup) ----------------------------------
    vid_thing_map: dict[tuple, Thing] = {}
    skipped_vid = 0
    unique_vids = {(r["vid_id"], r['extractor_id']) for r in vid_rows}
    for i, (vid_id, extractor_id) in enumerate(sorted(unique_vids), 1):
        key = (vid_id, extractor_id)
        bucket = vid_bucket_map.get(key)
        if not bucket:
            print(f"DEFAULT BUCKET video {key}: no resolvable parent bucket", file=sys.stderr)
            bucket = args.default_bucket
            #skipped_vid += 1
            #continue
        print(f"  [{i}/{len(unique_vids)}] OI lookup {extractor_id.lower()} {vid_id} ...",
              end=" ", flush=True)
        best_oi, url = _get_oi_uuid(oi, extractor_id, vid_id)
        print("found" if best_oi else "not found")
        #print(url)
        thing = Thing(
            container=False,
            native_id=vid_id or None,
            extractor_key=extractor_id.lower() if extractor_id else None,
            bucket=bucket,
            human_rating=None,
            best_oi=best_oi,
            try_on=None if best_oi else today,
            # An acquired video is terminal (its synthetic run succeeded): mark it complete so it
            # doesn't read as metadata-incomplete. V3 stored no per-video title/url, so display
            # fields are backfilled by identity (extractor_key+native_id) on the parent playlist's
            # next Stage-1 re-pull; a non-acquired video stays last_success_dt=NULL for that pull.
            last_success_dt=now if best_oi else None,
            created_dt=now,
            url=url
        )
        vid_thing_map[key] = thing

    print(len(vid_thing_map), len(set(x[0] for x in vid_thing_map.keys())))
    
    for i in set(x[0] for x in vid_thing_map.keys()):
        same_id = [x[1] for x in vid_thing_map.keys() if x[0] == i]
        if len(same_id) == 1:
            continue
        print(i, same_id)
        for j in same_id:
            print(vid_thing_map[(i,j)])
        # TODO generalize
        #del vid_thing_map[(i, 'youtube:tab')]
    
    print(set(x[1] for x in vid_thing_map.keys()))

    # --- Build rels (deduplicated) --------------------------------------------
    rel_set: set[tuple] = set()
    for row in vid_rows:
        pl = pl_thing_map.get(row["playlist_id"])
        vid = vid_thing_map.get((row["vid_id"], row["extractor_id"]))
        if pl and vid:
            rel_set.add((pl.id, vid.id))
        else:
            print('skip', row["playlist_id"], row["vid_id"], row["extractor_id"], bool(pl), bool(vid))

    rels = [Rel(parent=p, child=c, channel=False) for p, c in rel_set]

    # --- Synthetic runs (one per thing) ---------------------------------------
    all_things = list(pl_thing_map.values()) + list(vid_thing_map.values())
    runs = [
        Run(
            thing_id=t.id,
            worker="v3-migration",
            starttime=now,
            endtime=now,
            success=True,
            data_json={"source": "v3-migration"},
        )
        for t in all_things
    ]

    # --- Summary --------------------------------------------------------------
    print(f"\nMigration summary:")
    print(f"  Playlist things : {len(pl_thing_map)} (skipped {skipped_pl})")
    print(f"  Video things    : {len(vid_thing_map)} (skipped {skipped_vid})")
    print(f"    → acquired    : {sum(1 for t in vid_thing_map.values() if t.best_oi)}")
    print(f"  Rels            : {len(rels)}")
    print(f"  Runs            : {len(runs)}")

    if args.dry_run:
        print("\n--- THINGS ---")
        print(len(all_things), len(set(x.native_id for x in all_things)))
        for t in all_things:
            print(json.dumps(t.model_dump(mode='json')))
        print("\n--- RELS ---")
        for r in rels:
            print(json.dumps(r.model_dump(mode='json')))
        print("\n--- RUNS ---")
        for run in runs:
            print(json.dumps(run.model_dump(mode='json')))
        print("\nDry-run: no changes written.")
        return

    # --- Write to V4 ----------------------------------------------------------
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        for thing in all_things:
            session.add(thing)
        session.commit()
        for thing in all_things:
            session.refresh(thing)
        for rel in rels:
            session.add(rel)
        for run in runs:
            session.add(run)
        session.commit()
    print(f"\nDone. {len(all_things)} things, {len(rels)} rels, {len(runs)} runs written.")


if __name__ == "__main__":
    main()
