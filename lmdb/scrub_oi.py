#!/usr/bin/env python3
"""V4 OI scrubber (#111): reconcile LM ratings against OI/SO media holdings.

The 4.1 home of actual media deletion + replication (LM-V4-DESIGN.md Appendix A; 4.0
records ratings but touches nothing). Copy policy by effective rating band (§2.4):
A holds a floor of A_COPIES, B is left alone, C and below (including unrated and
machine-D/F) hold a ceiling of one copy, and human-D/F media is deleted outright.

- deletion: a *human*-rated D/F thing with acquired media has its bytes removed from
  simpler-objects and its OI record marked completed+deleted (obj_idx
  client.delete_object_data: locator fan-out DELETE until every copy is confirmed gone,
  then the combined PUT). `best_oi` needs no repoint: it is the OI *file* UUID, whose
  object simply becomes the tombstone. Needs obj_idx >= 0.3.8 (objectindex#23) and
  simpler-objects >= 0.6.0. The F-only metadata purge is a separate 4.x feature.
- replication: a thing assessing in the A band (effective rating >= +1.5) must hold
  A_COPIES copies in simpler-objects; under-replicated objects are copied to additional
  object servers straight through the locator (simpler_objects.async_replicate machinery).
  Extra A copies are never trimmed.
- reduction: a thing assessing below the B band (and not human-D/F, which deletes above)
  tolerates a single copy; excess copies are DELETEd directly on their object server --
  the one with the most used bytes overall goes first. Each such delete leaves SO's
  write-once checksum tombstone on that server (the key can never be re-placed there).

The scrubber never writes the LM database. Dry-run by default; pass --apply to act.
Exits nonzero iff anomalies were found (pending = store busy, not an anomaly).
Env: DATABASE_URL, OBJIDX_URL, OBJIDX_AUTH, and OBJIDX_S3 (locator; all branches).
"""

import argparse
import os
import random
import httpx
import requests
from sqlmodel import Session, create_engine, or_, select
from obj_idx import client as oic
from obj_idx.cli import get_s3_base
from simpler_objects import auth
from simpler_objects.async_replicate import find_space, replicate_object, signed_suffix
from . import xform
from .api import _effective_rating_expr
from .models import Thing

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg:///lmdb")

# Copy policy (LM-V4-DESIGN.md §2.4): A = 2 copies (floor, replication branch), B keeps
# whatever it has, C and below = 1 (ceiling, reduction branch), human-D/F = 0 (deletion).
A_COPIES = 2


class Tally:
    """Outcome counters for one scrub branch; `anomalies` drive the nonzero exit."""

    def __init__(self):
        self.checked = 0
        self.ok = 0        # already converged (tombstoned / at copy count)
        self.acted = 0     # deletions/copies applied (or dry-run "would")
        self.pending = 0   # store busy/unreachable/read-only; not an anomaly
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


def reduction_candidates(session: Session) -> list[Thing]:
    """Things owed the one-copy ceiling: acquired + effective below B + not human-D/F.

    B tolerates whatever it holds; human-D/F is the deletion branch's cohort. Unrated
    (effective defaults to 0) and machine-D/F things assess at/below C and shrink to one.
    """
    return session.exec(select(Thing).where(
        _effective_rating_expr(0.0) < xform.BAND_FLOOR["B"],
        or_(Thing.human_rating == None,  # noqa: E711
            Thing.human_rating >= xform.BAND_FLOOR["C"]),
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


def _delete_media(objidx, obj: dict, locator: str) -> bool:
    """Delete a media object's bytes and mark its OI record; True iff confirmed.

    delete_object_data DELETEs on the SO locator (every copy must go, so the fan-out
    verb, not a per-server one) until the store confirms 204/404, then marks the record
    completed+deleted -- the durable tombstone best_oi points at. False means the retry
    budget ran out with a copy possibly surviving (server busy/unreachable); the record
    is left unmarked and rerunning converges. Raises ValueError for a never-completed
    object and requests exceptions otherwise (e.g. missing `delete` RBAC).
    """
    return oic.delete_object_data(objidx, obj["uuid"], locator)


def scrub_deletions(things: list[Thing], objidx, locator: str, apply: bool) -> Tally:
    """Delete the OI/SO media of human-D/F things (LM DB untouched; see module doc)."""
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
            continue
        try:
            done = _delete_media(objidx, obj, locator)
        except ValueError as exc:
            tally.anomaly(thing, f"object {obj['uuid']} not deletable: {exc}")
            continue
        except requests.RequestException as exc:
            tally.anomaly(thing, f"deletion of object {obj['uuid']} failed: {exc}")
            continue
        if done:
            tally.acted += 1
        else:
            tally.pending += 1
            print(f"        store did not confirm all copies of {obj['uuid']} gone "
                  f"(a server busy/unreachable) -- will converge on a future scrub")
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


def _copy_locations(thing: Thing, obj: dict, locator: str, listings: dict,
                    tally: Tally) -> list | None:
    """Current SO copy locations of obj, or None after recording an anomaly.

    Shared by the replication and reduction branches: resolves (and caches, in
    `listings`) the bucket listing and validates the key's entry -- lost media and
    locator failures are anomalies, as is a per-key `error` flag, since a copy may sit
    on an unreachable/sleeping server and acting now could produce an extra copy
    (simpler-objects#76) or delete a survivor's twin.
    """
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
        return None
    entry = listing.get(key)
    if not entry or not entry.get("locations"):
        tally.anomaly(thing, f"media {bucket}/{key} has no copies in simpler-objects (lost?)")
        return None
    if entry.get("error"):
        tally.anomaly(thing, f"{bucket}/{key}: locator reports an error; skipping")
        return None
    return entry["locations"]


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
        locations = _copy_locations(thing, obj, locator, listings, tally)
        if locations is None:
            continue
        if len(locations) >= A_COPIES:
            tally.ok += 1
            continue
        needed = A_COPIES - len(locations)
        bucket, key = obj["bucket"], obj["key"]
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


def _server_used_bytes(locator: str) -> dict:
    """Per-object-server quota-used-bytes from the locator health endpoint.

    An unreachable server reports 0 (the locator's own fallback), sorting it last --
    which also means its copy is never the deletion victim while it cannot answer.
    """
    res = httpx.get(locator + "health", timeout=8)
    res.raise_for_status()
    return {server: stats.get("quota-used-bytes", 0)
            for server, stats in res.json()["servers"].items()}


def _delete_copy(server: str, bucket: str, key: str) -> int:
    """DELETE one copy directly on an object server; returns the HTTP status.

    The locator's DELETE is deliberately not used here: it fans out to every server
    (every replica must go), while reduction removes exactly one chosen copy. Signed
    like async_replicate's other direct object-server requests (CLUSTER_SECRET).
    """
    url = server + f"{bucket}/{key}" + signed_suffix(auth.OP_DELETE, bucket, key)
    return httpx.delete(url, timeout=32).status_code


def scrub_reduction(things: list[Thing], objidx, locator: str, apply: bool) -> Tally:
    """Trim below-B things back to a single simpler-objects copy (the C ceiling).

    The victim is the copy on the object server with the most used bytes overall
    (locator health); a mid-upload (503) or read-only (405) server just shifts the
    delete to the next-fullest copy. At most len(locations)-1 deletes are ever issued,
    so one copy always survives. Each delete leaves SO's write-once checksum tombstone
    on that server, permanently blocking the key from returning there.
    """
    tally = Tally()
    mode = "APPLY" if apply else "DRY "
    listings: dict[str, dict | None] = {}
    used: dict | None = None  # one health fetch per run, first time a trim is due
    for thing in things:
        tally.checked += 1
        obj = _fetch_object(objidx, thing, tally)
        if obj is None:
            continue
        if obj["deleted"]:
            tally.ok += 1  # nothing held; re-acquire is the rating-change path's job
            continue
        if not obj["completed"]:
            tally.anomaly(thing, f"OI object {obj['uuid']} is incomplete")
            continue
        locations = _copy_locations(thing, obj, locator, listings, tally)
        if locations is None:
            continue
        if len(locations) <= 1:
            tally.ok += 1
            continue
        excess = len(locations) - 1
        bucket, key = obj["bucket"], obj["key"]
        print(f"{mode} thing {thing.id} ({thing.extractor_key} {thing.native_id}): "
              f"{bucket}/{key} has {len(locations)} copies, ceiling 1")
        if not apply:
            tally.acted += 1
            continue
        if used is None:
            try:
                used = _server_used_bytes(locator)
            except httpx.HTTPError as exc:
                tally.anomaly(thing, f"locator health unavailable: {exc}")
                continue
        removed = 0
        failed = False
        for server in sorted(locations, key=lambda s: used.get(s, 0), reverse=True):
            if removed >= excess:
                break
            try:
                status = _delete_copy(server, bucket, key)
            except httpx.HTTPError as exc:
                tally.anomaly(thing, f"delete of {bucket}/{key} on {server} failed: {exc}")
                failed = True
                break
            if status in (204, 404):  # 404: listing was stale, copy already gone
                print(f"        removed {server}{bucket}/{key}")
                removed += 1
            elif status in (503, 405):
                # mid-upload or read-only: leave this copy, try the next-fullest
                print(f"        {server} busy/read-only (HTTP {status}); trying next copy")
            else:
                tally.anomaly(thing, f"delete of {bucket}/{key} on {server}: HTTP {status}")
                failed = True
                break
        if failed:
            continue
        if removed >= excess:
            tally.acted += 1
        else:
            tally.pending += 1
            print(f"        {excess - removed} excess copy(ies) of {bucket}/{key} remain "
                  f"on busy/read-only servers -- will converge on a future scrub")
    verb = "reduced" if apply else "would reduce"
    print(f"Reduction: checked {tally.checked}, at copy count {tally.ok}, "
          f"{verb} {tally.acted}, pending {tally.pending}, anomalies {tally.anomalies}")
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
    branch.add_argument("--reduce-only", action="store_true",
                        help="only the below-B copy-reduction branch")
    parser.add_argument("--s3", help="simpler-objects locator base URL (default: $OBJIDX_S3)")
    args = parser.parse_args()
    only = args.delete_only or args.replicate_only or args.reduce_only
    do_delete = args.delete_only or not only
    do_replicate = args.replicate_only or not only
    do_reduce = args.reduce_only or not only

    objidx = oic.get_obj_idx_env()
    locator = get_s3_base(args.s3)
    engine = create_engine(DATABASE_URL, echo=False)
    anomalies = 0
    with Session(engine) as session:
        if do_delete:
            anomalies += scrub_deletions(deletion_candidates(session), objidx,
                                         locator, args.apply).anomalies
        if do_replicate:
            anomalies += scrub_replication(replication_candidates(session), objidx,
                                           locator, args.apply).anomalies
        if do_reduce:
            anomalies += scrub_reduction(reduction_candidates(session), objidx,
                                         locator, args.apply).anomalies
    return 1 if anomalies else 0


if __name__ == "__main__":
    raise SystemExit(main())
