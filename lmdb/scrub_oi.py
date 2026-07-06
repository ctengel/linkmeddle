#!/usr/bin/env python3
"""V4 OI scrubber (#111): reconcile LM ratings against OI/SO media holdings.

The 4.1 home of actual media deletion + replication (LM-V4-DESIGN.md Appendix A; 4.0
records ratings but touches nothing):

- deletion: a *human*-rated D/F thing with acquired media gets its OI object tombstoned
  (PUT deleted=True). OI currently refuses to delete a *completed* object (objectindex#23),
  so the attempt is verified and reported as pending until that lands -- rerunning
  converges once it does. `best_oi` needs no repoint: it is the OI *file* UUID, whose
  object simply becomes the tombstone. Byte removal in simpler-objects (no DELETE verb
  yet) is likewise pending, and the F-only metadata purge is a separate 4.x feature.
- replication: a thing assessing in the A band (effective rating >= +1.5) must hold
  A_COPIES copies in simpler-objects; under-replicated objects are copied to additional
  object servers straight through the locator (simpler_objects.async_replicate machinery).
  Copy-count *reduction* (B/C tolerate 1 copy) needs an SO DELETE and stays out of scope.

The scrubber never writes the LM database. Dry-run by default; pass --apply to act.
Exits nonzero iff anomalies were found (pending deletions are not anomalies).
Env: DATABASE_URL, OBJIDX_URL, OBJIDX_AUTH, and OBJIDX_S3 (locator; replication only).
"""

import argparse
import os
import random
import httpx
import requests
from sqlmodel import Session, create_engine, select
from obj_idx import client as oic
from obj_idx.cli import get_s3_base
from simpler_objects.async_replicate import find_space, replicate_object
from . import xform
from .api import _effective_rating_expr
from .models import Thing

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg:///lmdb")

# Copy policy (LM-V4-DESIGN.md §2.4): A = 2 copies. B/C tolerate their acquired single
# copy, D/F want 0 (the deletion branch), so the A floor is the only count enforced here.
A_COPIES = 2


class Tally:
    """Outcome counters for one scrub branch; `anomalies` drive the nonzero exit."""

    def __init__(self):
        self.checked = 0
        self.ok = 0        # already converged (tombstoned / enough copies)
        self.acted = 0     # deletions/copies applied (or dry-run "would")
        self.pending = 0   # blocked on OI/SO capability (objectindex#23); not an anomaly
        self.anomalies = 0

    def anomaly(self, thing: Thing, msg: str) -> None:
        self.anomalies += 1
        print(f"ANOMALY thing {thing.id} ({thing.extractor_key} {thing.native_id}): {msg}")


def deletion_candidates(session: Session) -> list[Thing]:
    """Things whose media the scrubber should drop: acquired + human-rated D/F.

    Human rating only (§2.4/Appendix A): a machine rating never triggers deletion.
    """
    return session.exec(select(Thing).where(
        Thing.human_rating < xform.BAND_FLOOR["C"],
        Thing.best_oi != None)).all()  # noqa: E711


def replication_candidates(session: Session) -> list[Thing]:
    """Things owed the A-band copy count: acquired + effective rating in the A band."""
    return session.exec(select(Thing).where(
        _effective_rating_expr(0.0) >= xform.BAND_FLOOR["A"],
        Thing.best_oi != None)).all()  # noqa: E711


def _fetch_object(objidx, thing: Thing, tally: Tally):
    """The full OI object dict behind thing.best_oi (an OI *file* UUID), or None (anomaly)."""
    try:
        oif = objidx.get_file(thing.best_oi)
    except requests.HTTPError as exc:
        tally.anomaly(thing, f"best_oi {thing.best_oi} not fetchable from OI: {exc}")
        return None
    if not oif.object:
        tally.anomaly(thing, f"OI file {thing.best_oi} has no object")
        return None
    return oif.object


def _delete_media(objidx, obj: dict) -> bool:
    """Tombstone a media object in OI; True iff OI honored it.

    Today OI silently refuses to delete a *completed* object (returns it unchanged;
    objectindex#23), so the caller treats False as pending, not failure. This is the one
    seam to update if #23 ships a different verb (e.g. DELETE /object/{uuid}/data) --
    removing the bytes in simpler-objects (which has no DELETE) belongs behind it too.
    """
    updated = objidx.put_object(obj["uuid"], {"deleted": True})
    return bool(updated.get("deleted"))


def scrub_deletions(things: list[Thing], objidx, apply: bool) -> Tally:
    """Tombstone the OI media of human-D/F things (LM DB untouched; see module doc)."""
    tally = Tally()
    mode = "APPLY" if apply else "DRY "
    for thing in things:
        tally.checked += 1
        obj = _fetch_object(objidx, thing, tally)
        if obj is None:
            continue
        if obj["deleted"]:
            tally.ok += 1  # already a tombstone; converged
            continue
        print(f"{mode} thing {thing.id} ({thing.extractor_key} {thing.native_id}) "
              f"rated {thing.human_rating:+.0f}: delete object {obj['uuid']}")
        if not apply:
            tally.acted += 1
        elif _delete_media(objidx, obj):
            tally.acted += 1
        else:
            tally.pending += 1
            print(f"        object {obj['uuid']} is completed; OI cannot tombstone it yet "
                  f"(objectindex#23) -- will converge on a future scrub")
    verb = "deleted" if apply else "would delete"
    print(f"Deletion: checked {tally.checked}, tombstoned already {tally.ok}, "
          f"{verb} {tally.acted}, pending {tally.pending}, anomalies {tally.anomalies}")
    return tally


def _bucket_listing(locator: str, bucket: str) -> dict:
    """Per-key {size, checksum, directory, locations, error} from the SO locator.

    (async_replicate.get_bucket_contents drops `locations`, which is the point here.)
    """
    res = httpx.get(locator + bucket + "/", timeout=32)
    res.raise_for_status()
    return res.json()["objects"]


def scrub_replication(things: list[Thing], objidx, locator: str, apply: bool) -> Tally:
    """Bring every A-band thing's object up to A_COPIES copies in simpler-objects."""
    tally = Tally()
    mode = "APPLY" if apply else "DRY "
    listings: dict[str, dict | None] = {}
    for thing in things:
        tally.checked += 1
        obj = _fetch_object(objidx, thing, tally)
        if obj is None:
            continue
        if obj["deleted"] or not obj["completed"]:
            state = "a tombstone" if obj["deleted"] else "incomplete"
            tally.anomaly(thing, f"A-band but OI object {obj['uuid']} is {state} "
                          "(re-acquire is the rating-change path's job, not the scrubber's)")
            continue
        bucket, key = obj["bucket"], obj["key"]
        if bucket not in listings:
            try:
                listings[bucket] = _bucket_listing(locator, bucket)
            except httpx.HTTPError as exc:
                print(f"WARN locator listing for bucket {bucket} unavailable: {exc}")
                listings[bucket] = None
        listing = listings[bucket]
        if listing is None:
            tally.anomaly(thing, f"locator listing for bucket {bucket} unavailable")
            continue
        entry = listing.get(key)
        if not entry or not entry.get("locations"):
            tally.anomaly(thing, f"media {bucket}/{key} has no copies in simpler-objects (lost?)")
            continue
        if entry.get("error"):
            # A copy may sit on an unreachable/sleeping server; replicating now could
            # produce an extra copy (simpler-objects#76), so just flag it.
            tally.anomaly(thing, f"{bucket}/{key}: locator reports an error; skipping")
            continue
        locations = entry["locations"]
        if len(locations) >= A_COPIES:
            tally.ok += 1
            continue
        needed = A_COPIES - len(locations)
        print(f"{mode} thing {thing.id} ({thing.extractor_key} {thing.native_id}): "
              f"{bucket}/{key} has {len(locations)} of {A_COPIES} copies")
        if not apply:
            tally.acted += 1
            continue
        targets = find_space(locator, bucket, obj["obj_size"], locations, needed)
        if len(targets) < needed:
            tally.anomaly(thing, f"no server with space for {needed} more copy(ies) "
                          f"of {bucket}/{key}")
            if not targets:
                continue
        try:
            for target in targets:
                src = random.choice(locations) + f"{bucket}/{key}"
                dst = target + f"{bucket}/{key}"
                print(f"        {src} => {dst}")
                replicate_object(src, dst)
            tally.acted += 1
        except (httpx.HTTPError, AssertionError) as exc:
            tally.anomaly(thing, f"replication of {bucket}/{key} failed: {exc}")
    verb = "replicated" if apply else "would replicate"
    print(f"Replication: checked {tally.checked}, at copy count {tally.ok}, "
          f"{verb} {tally.acted}, anomalies {tally.anomalies}")
    return tally


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="act on findings (default: dry-run report only)")
    branch = parser.add_mutually_exclusive_group()
    branch.add_argument("--delete-only", action="store_true",
                        help="only the D/F media-deletion branch")
    branch.add_argument("--replicate-only", action="store_true",
                        help="only the A-band replication branch")
    parser.add_argument("--s3", help="simpler-objects locator base URL (default: $OBJIDX_S3)")
    args = parser.parse_args()
    do_delete = not args.replicate_only
    do_replicate = not args.delete_only

    objidx = oic.get_obj_idx_env()
    locator = get_s3_base(args.s3) if do_replicate else None
    engine = create_engine(DATABASE_URL, echo=False)
    anomalies = 0
    with Session(engine) as session:
        if do_delete:
            anomalies += scrub_deletions(deletion_candidates(session), objidx,
                                         args.apply).anomalies
        if do_replicate:
            anomalies += scrub_replication(replication_candidates(session), objidx,
                                           locator, args.apply).anomalies
    return 1 if anomalies else 0


if __name__ == "__main__":
    raise SystemExit(main())
