#!/usr/bin/env python3
"""One-off recovery: relink OI objects to things whose media was uploaded but the run was
recorded as a failure (the pre-fix top-level `info['oi_uuid']` bug).

A Stage-2 download that uploaded to Object Index but reported success=False left the thing
with `best_oi` NULL, `last_failure_dt` set, and `try_on` backed off, while a completed OI
object exists with nothing pointing at it (orphaned). Such things can't self-heal on retry:
the download archive (ytdl_arch_oi.ObjIdxDlArch) sees the completed OI object and skips the
re-download, so no new `oi_uuid` is ever produced.

This finds the orphaned object for each stuck thing — by the `lm-thing-id` tag stamped at
upload (run_bknd.init_download oitags), falling back to the extractor+native_id archive key
ObjIdxDlArch uses — and marks the thing acquired exactly as the server's success branch does
(api.submit_result: set best_oi, last_success_dt, clear last_failure_dt, try_on=NULL, drop
the load-info hint).

Dry-run by default; pass --apply to write. Env: DATABASE_URL, OBJIDX_URL, OBJIDX_AUTH.
"""

import argparse
import os
import uuid
from sqlmodel import Session, create_engine, select
from obj_idx import client as oic
from . import models, xform

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg:///lmdb")


def _completed(oif) -> bool:
    """A usable acquisition: the OI object exists, is completed, not deleted, not partial
    (matches ytdl_arch_oi.oif2archive's notion of a real archive entry)."""
    obj = oif.object
    return bool(obj and obj.get("completed") and not obj.get("deleted")
                and not (oif.info or {}).get("partial"))


def find_oi_uuid(objidx, thing: models.Thing):
    """The UUID of a completed OI object for `thing`, or None. Prefer the explicit upload tag;
    fall back to the extractor+native_id key (the same identity ObjIdxDlArch archives by)."""
    queries = [{"extra": f"lm-thing-id={thing.id}"}]
    if thing.extractor_key and thing.native_id:
        queries.append({"extra": f"ytdl-id={thing.extractor_key} {thing.native_id}"})
    for params in queries:
        for oif in objidx.search_files(params):
            if _completed(oif):
                return uuid.UUID(str(oif.uuid))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry-run report only)")
    args = parser.parse_args()

    objidx = oic.get_obj_idx_env()
    now = models.naive_utcnow()
    engine = create_engine(DATABASE_URL, echo=False)
    checked = relinked = 0
    with Session(engine) as session:
        # Stuck = a leaf marked failed with no acquisition recorded.
        stuck = session.exec(select(models.Thing).where(
            models.Thing.container == False,              # noqa: E712
            models.Thing.best_oi == None,                 # noqa: E711
            models.Thing.last_failure_dt != None)).all()  # noqa: E711
        for thing in stuck:
            checked += 1
            oi_uuid = find_oi_uuid(objidx, thing)
            if oi_uuid is None:
                continue
            relinked += 1
            print(f"{'APPLY' if args.apply else 'DRY '} thing {thing.id} "
                  f"({thing.extractor_key} {thing.native_id}) -> best_oi {oi_uuid}")
            if args.apply:
                thing.best_oi = oi_uuid
                thing.last_success_dt = now
                thing.last_failure_dt = None
                thing.try_on = None
                xform.clear_info_hint(thing)
                session.add(thing)
        if args.apply:
            session.commit()
    verb = "relinked" if args.apply else "would relink"
    print(f"Checked {checked} stuck thing(s); {verb} {relinked}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
