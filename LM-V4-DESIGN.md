# LinkMeddle V4 — Final Design Document

**Status:** Authoritative design for the V4.0 MVP and the V4.x line.
**Date:** 2026-06-13
**Supersedes (as the single source of truth):** `LM-V4-DESIGN-SUMMARY.md`, `lm-specs-2026-06-11-notes.md`, `lm-v4-open-decisions.md`, `lm-v4-issues-analysis.md`, `lm-v4-existing-work.md`, `lm4-start-stop-continue.md`.

This document folds every resolved architect decision into one place. Where a decision overrides an earlier doc, the earlier doc is historical context only. Decision provenance is cited inline as `[A1]`, `[B4]`, etc., keyed to `lm-v4-open-decisions.md`.

---

## Part 1 — High-Level Design

### 1.1 Purpose

V4 exists to answer one question the earlier versions never could: **do we actually want this thing, and when?**

The V3 architecture downloads everything from every scheduled playlist, indiscriminately. That wastes bandwidth, storage, and — most painfully — rate-limit budget against unreliable upstream sites. The single clearest statement of intent in the design corpus is from the handwritten notes (p105):

> **"FAN OUT IS the way. The assumption we want to DL every video is fundamentally flawed."**

V4 inserts a *decision* between *discovering* content and *acquiring* it. It does this by decoupling playlist metadata pulls from individual video downloads ("fan-out"), and by attaching a **rating** to every item that drives what gets fetched, how often, in what priority, and whether it is kept or deleted.

### 1.2 The lifecycle V4 implements

A video moves through six stages; LM owns three transitions between them (notes p105):

| Stage | State | Phase | LM responsibility |
|---|---|---|---|
| 0 | Doesn't exist yet | — | — |
| 1 | Exists, we don't have it | **I — Learn** (1→2) | discover it exists |
| 2 | We know about it | **II — Decide** (2→3) | rate it; decide to acquire |
| 3 | We've decided we want it | **III — Acquire** (3→4) | download + store |
| 4 | We have it | — | serve / view |
| 5 | We've decided to delete it | — | deletion (rating-driven) |

V3's defect: it merges Phase I and Phase III and skips Phase II entirely. V4's central change is to make Phase II a real, persisted state — the `thing` table is the place where "we know about it but haven't decided" lives.

### 1.3 Architectural principles

1. **No queue, no state machine, no graph DB — just well-indexed Postgres tables.** Workers derive their work by querying the data directly; they do not consume a queue. The breakthrough insight from the Feb 12 sessions: *"you just want a way to hold stuff between queries."* State is the data, not an orchestration layer. `[D2, F1]`
2. **Everything is a `thing`.** Playlists, videos, and channels are rows in one table differentiated by `type`. We do **not** split playlist-handling from video-handling into separate services or schemas. `[D2]`
3. **Human rating is authoritative; machine rating is advisory.** A user rating always wins over any propagated/computed rating. `[B2]`
4. **Decouple Learn from Acquire (fan-out).** Pulling a playlist's metadata never triggers downloads. Acquisition is a separate, independently-prioritized pass. `[C4]`
5. **Single source of truth per concern.** Media lives in OI/SO; LM talks only to OI. Metadata and relationships live in Postgres. We do not duplicate OI's replication bookkeeping into LM. `[F1, A5]`
6. **Reuse V3 code wherever the *logic* survives — but V3 code does not define V4 design.** The Fibonacci backoff, change detection, DLP boundary models, OI archive, and Crustula integration are all reused. The coupled download model, the schedule/stats schema, and the push-style job runner are replaced.
7. **Build for the current scale (single Raspberry-Pi-class host, single worker) but leave clean seams for V4.x.** Forward-compatibility is bought only where it is nearly free (e.g. `SELECT ... FOR UPDATE SKIP LOCKED`). `[D1, D2]`
8. **Nail the schema in 4.0.** The 4.0 schema is designed so that 4.x features add *new* tables or *nullable* columns at most — never a migration of existing data. V5 is permitted a fresh schema.

### 1.4 What "done" means for V4 (Definition of Done)

From the unified DoD (DESIGN-SUMMARY §"V4 Definition of Done"):

1. Submit a URL → see all downloaded files from that playlist with working links.
2. Rate any playlist or video.
3. Delete content: 4.0 records only the *decision* (rate D/F) — **no media or metadata is removed**; all actual deletion is 4.x (the OI scrubber deletes D/F media; a separate F-only metadata purge).
4. Downloads driven by ratings — A first; C only as capacity allows.
5. Playlist metadata pulls and video downloads are decoupled (fan-out).
6. Failure states visible and user-actionable (incl. permanent-failure acknowledgment).
7. Individual video download can be triggered.
8. Crustula (cookie auth) works end-to-end, including the success callback.

---

## Part 2 — Data Model (the 4.0 schema, frozen)

All three tables live in **PostgreSQL**; raw yt-dlp metadata is stored in `JSONB` columns — there is no separate document store. `[F1]` The database starts **greenfield**: there is no V3→V4 data migration (no populated V3 instance exists to migrate from). `[F3]` **All datetime columns are naive UTC (`timestamp`, not `timestamptz`); the application works in UTC everywhere** (matching V3's existing naive `datetime.now()`), keeping timezone handling out of the data layer entirely. The canonical DDL is mirrored in `lmdb/schema/v4.0.sql`.

### 2.1 `thing`

The universal entity: playlist, video, or channel.

```sql
CREATE TABLE thing (
  id              uuid PRIMARY KEY,             -- surrogate, LM-assigned  [A1-A]
  url             text,                         -- canonical URL; NULLABLE [A6-A]
  backend         smallint NOT NULL DEFAULT 0,  -- acquisition engine code; 0 = yt-dlp; dispatch + identity [A7]
  site            text,                         -- rate-limit bucket / host; nullable; join key to future per-site table [A7]
  extractor_key   text,                         -- backend source key (yt-dlp: extractor, lowercase)
  native_id       text,                         -- backend-native id (yt-dlp: extractor id)
  type            text NOT NULL,                -- 'video' | 'playlist' | 'channel'; plain text, so 4.x can add e.g. 'photo' with no migration
  title           text,                         -- denormalized display    [A2-A]
  channel         text,                         -- denormalized display    [A2-A]
  thumbnail_url   text,                         -- nullable; populated when available (4.x UI)
  modified        timestamp,                    -- naive UTC; content modified/upload time; playlist = derived from items, video = site-reported; nullable [A2-A]
  human_rating    double precision,             -- -2.0..+2.0, user-set; authoritative [B1,B2]
  machine_rating  double precision,             -- nullable; computed-on-read in 4.0   [B3,B4]
  last_success_dt timestamp,                    -- naive UTC
  last_failure_dt timestamp,                    -- naive UTC; nulled on success [C3-A]
  try_on          date DEFAULT (now() at time zone 'utc')::date,  -- backoff oracle, see §2.5; defaults to today (UTC) so the date-gate is open from creation [C1,C2]
  bucket          text NOT NULL,                -- OI storage bucket; required at creation, inherited from the relative that first discovers a thing, immutable thereafter [A10]
  best_oi         uuid,                         -- OI file UUID; set by the worker on download from the upload's info['oi_uuid']; never cleared (4.x scrubber repoints it to the OI tombstone when it deletes media) [A5-A]
  attrs           jsonb,                        -- 4.x escape hatch + 4.0 soft hints: attrs.cookies (suggest cookies), attrs.lpm_lib (optional library tag) [A11]
  created_dt      timestamp NOT NULL DEFAULT (now() at time zone 'utc')  -- naive UTC; backs "new things" dashboard query
);

CREATE UNIQUE INDEX thing_native ON thing (backend, extractor_key, native_id)
  WHERE native_id IS NOT NULL;                  -- secondary lookup key     [A1-A,A7]
CREATE UNIQUE INDEX thing_url ON thing (url)
  WHERE url IS NOT NULL;                         -- pre-extraction / paste-time dedup [A8]
CREATE INDEX thing_try_on ON thing (type, try_on);   -- worker selection
```

Notes on the contested columns:

- **`id` is a surrogate UUID** `[A1-A]`. `(extractor_key, native_id)` is a *unique secondary index*, not the primary key. This satisfies the "stable internal id"/"lmpl id" intent, survives re-extraction, and is the only choice compatible with nullable `url`.
- **`url` is nullable** `[A6-A]` purely as cheap insurance for V5's URL-less LPM things (libraries/people). No 4.0 feature creates URL-less things; this just avoids a forced V5-era migration of the column.
- **`url` is also uniquely indexed where present** `[A8]` (`UNIQUE (url) WHERE url IS NOT NULL`). This is the dedup guard for the **pre-extraction / paste-time** window: the native-key index is `WHERE native_id IS NOT NULL`, so until extraction fills in `extractor_key`/`native_id` it offers no protection against two rows for the same pasted URL. The URL index also makes re-discovery of an F-suppressed thing (whose `url` is retained) match the existing row rather than mint a new one. **Implementor note:** canonicalize URLs on write — this constraint only catches byte-identical strings, so URL variants (`&list=`, short links, trailing slash) must be normalized before insert or they'll slip past here and only collide later at the native-key index. Kept global (not per-backend): one canonical URL = one resource.
- **`backend` re-admits non-yt-dlp acquisition at the schema layer only** `[A7]`. V2 carried scaffolding for multiple backends; V3 pivoted to 100% yt-dlp. V4 keeps that pivot for *implementation* — `backend` is `0` (yt-dlp) for every row in 4.0 and the worker has exactly one dispatch branch — but reserves the column so a 4.x backend (e.g. `gallery-dl`, direct HTTP, a bespoke scraper) needs no migration. It is dispatch + identity data, not config: the worker must know the engine before acting, and it participates in the re-acquisition uniqueness key `(backend, extractor_key, native_id)`.
  - **Integer code, `smallint NOT NULL DEFAULT 0`, where `0` = yt-dlp.** Compact, and `NOT NULL` keeps the unique index plain (no `NULL`-distinctness pitfall, no `COALESCE`). New backends take positive codes as they are added. The code→name mapping is a small app-side constant (or a column on the future per-site/backend config table); raw DB rows show an opaque integer, which is the accepted trade for the compactness. (`smallint` is Postgres's smallest integer type, 2 bytes — there is no native 1-byte int — giving ample headroom for the <100 backends ever expected.)
- **`site` is the rate-limit bucket** `[A7]`, nullable. It is *not* identity (it stays out of the uniqueness key) and *not* the same as `extractor_key`: rate limits apply per network host, which the yt-dlp extractor doesn't always identify 1:1 (generic extractor across hosts, or several extractors sharing one CDN). It is the natural join key into the future per-site config/rate-limit table (Appendix A); 4.0 may populate it but does no per-site enforcement yet.
- **Display columns (`title`, `channel`, `thumbnail_url`) are denormalized onto `thing`** `[A2-A]`, updated opportunistically on each run. The exact set may be tuned at implementation; the door to a JSON-only approach stays open if it bloats, but A is the chosen default. This is also what resolves cross-metadata references (#137 / `[G1]`): a video discovered only inside a playlist gets a **stub `thing`** created with whatever we know (title/channel pulled out of the parent's `run.data_json`, plus the inherited `bucket` and any propagated `attrs.cookies`/`attrs.lpm_lib` hints — §4.1), so we never need a "this video's metadata lives at playlist X item Y" pointer. Fan-out likewise auto-creates a `type='channel'` `thing` per **distinct uploader** — the playlist's *and* each video's — so videos stay reachable by channel even outside an explicit channel playlist (full parity with V3's per-video `pseudo_channel`). The playlist's channel gets a `rel type='channel_playlist'` edge to the playlist; each video's uploader channel gets a `rel type='channel_video'` edge to that video (a video whose uploader matches the playlist's reuses the one channel node). This is the V4 form of V3's `pseudo_channel` auto-creation, needing **no boolean flag** (the `rel` type is the marker) `[A11]`. 4.0 only creates these channel nodes/edges; channel *display/navigation* in the FE is 4.x (#120).
- **There is no `status` column and no `deleted_dt`** `[A3-B]`. State is still fully derived, with one refinement now that `best_oi` is never cleared (see deletion in §2.4): *never acquired* = `best_oi IS NULL` (no OI object at all, not even a tombstone); *acquired at least once* = `best_oi IS NOT NULL`, where the pointed-to OI object is an **active media object** unless the thing was deleted, in which case it is a persistent **deletion tombstone**. **OI is the source of truth** for which — the intentional-deletion record lives in OI (the tombstone), so LM still needs no status column. (**In 4.0 nothing is ever deleted**, so the object is always active media; tombstones appear only once the 4.x scrubber runs.) Deletion *intent* is carried by the rating (D/F, see §2.4) — which in 4.0 only gates acquisition, it does not delete anything.
- **`modified` is a single denormalized content-timestamp on `thing`** `[A2-A]`, updated opportunistically on each run, with type-specific meaning: for a **playlist** it is derived from the member items (e.g. the newest item's time) when that can be figured out, else `NULL`; for a **video** it is the site-reported modified/upload time when available, else `NULL`. It replaces the per-run `newest_item` and the `data_json` `modified_date` — one column, valid across types, for "what's new"/freshness display and sorting. **Nullable by design** (V3's `141.sql` bug came from treating a modified-date as non-nullable). It is *not* a change-detection key — that is `run.entries_hash` (§2.3).
- **`machine_rating` exists but is not maintained in 4.0** `[B4-B]`. It is computed on read (§2.3). The nullable column is retained for a future, more expensive (e.g. ML) computation method that may want to persist results.
- **`best_oi` is denormalized** `[A5-A]` (a `uuid` column) and set by the worker on a successful download — it is the **OI file UUID**, read straight off the upload's `info['oi_uuid']` (set by `ObjIdxUploadPP`), so no separate OI lookup is needed. **In 4.0 nothing else touches it** — rating D/F does not delete media, so `best_oi` keeps pointing at the live object. The **4.x OI scrubber** is what deletes D/F media and repoints `best_oi` to the tombstone (never cleared to NULL). OI does not change on its own, so the worker setting it at download time is all 4.0 needs.
- **`bucket` is a first-class, required, immutable column** `[A10]`, replacing V3's per-schedule `PlaylistSched.oi_bucket`. It names the OI storage bucket the thing's media goes into. V3 attached the bucket to the *schedule*; V4 has no schedule layer, so the bucket lives on the `thing` itself and is fixed at creation:
  - **Required on `POST /things/`** (the human entry point, §3.3). The core PLAPI imposes no default — a create with no bucket is rejected — exactly as V3's CLI demanded `oibucket` as a positional argument. The frontend (`lmfe`) supplies a **default for the common case** via `OBJIDX_BUCKET_DEFAULT`, exactly as V3's `lmfe` did, so GUI/bookmarklet users never pick a bucket.
  - **Inherited on fan-out, then frozen.** A thing discovered during a Stage 1 playlist pull (§4.1) inherits the `bucket` of the **relative that first discovered it** — i.e. the parent playlist whose run created the stub — and it is **never changed afterward**. Re-discovery via another relative with a different bucket does not move it; a thing added directly by URL keeps the bucket it was created with even when a later playlist pull finds it (idempotent on `url`, §2.1). One thing, one storage home, decided once.
  - **`NOT NULL`, no DB default.** Inheritance and the API requirement together guarantee every 4.0 thing has a bucket, so the column is strictly `NOT NULL`. (Unlike `url`, which is deliberately nullable as V5 insurance, `bucket` is required now; a V5 URL-less LPM thing that genuinely has no OI bucket is a V5-schema concern, not a 4.x one.) It is not part of any uniqueness key and not changeable via `PATCH` (no rating-style mutation) in 4.0.
- **`attrs` also carries two *soft, optional* 4.0 hints** `[A11]` (alongside its 4.x escape-hatch role), the loose counterparts to the strict `bucket` column — both for the other two V3 `PlaylistSched` download-config fields that, unlike `oi_bucket`, do *not* warrant a required column:
  - **`attrs.cookies`** — a *suggestion* to consider cookies for this thing (cookies are avoided whenever possible and never permanently "attached"). It is only a hint: the actual per-run *decision* to use cookies is recorded in `run.input_json` (§2.3), and the worker may also opt into cookies on its own after a cookieless failure (§4.7). Propagated playlist→video on fan-out: a playlist that needed cookies sets `attrs.cookies` on the video stubs it creates (§4.1). A per-site cookie policy is a 4.x concern (the future per-site table, Appendix A).
  - **`attrs.lpm_lib`** — an optional LPM library tag, the V4.0 home for V3's `PlaylistSched.lpm_lib` (passed to `ObjIdxUploadPP(lpmlib=)` at download). Always optional; helpful today but slated for **full deprecation in V5** (the broader LPM-as-things work, §Appendix A / #136). Propagated playlist→video **only if present**, and **never** video→playlist. 4.x may fold it into the per-site table.
- **`type` is plain `text`, not a DB enum**, so 4.x can add new kinds — e.g. **`photo`** — with no migration; 4.0 uses `video`/`playlist`/`channel`.
- **`created_dt` is needed** — it records when LM first learned of the thing and backs the **"new things" dashboard** (recently-discovered items awaiting a rating, §3.1). Keep it.

### 2.2 `rel`

Graph edges between things (playlist↔video, channel↔playlist).

```sql
CREATE TABLE rel (
  parent     uuid NOT NULL REFERENCES thing(id),
  child      uuid NOT NULL REFERENCES thing(id),
  type       text NOT NULL,         -- 'playlist_video' | 'channel_playlist' | 'channel_video' | ...; 4.x may add 'same' (two things are the same media)
  PRIMARY KEY (parent, child, type)
);
CREATE INDEX rel_child ON rel (child);
```

**There is no `order` column** `[A4-B]`. Display order is derived on demand from the most recent `run.data_json` when needed; we don't usually care about order, and storing it signs us up for perpetual drift-maintenance on every fan-out update. (The door is open to add an order signal in 4.x as a nullable column or in `attrs` — additive, no migration.)

**`type` is plain `text`** — 4.x may add a **`same`** edge to link two things that are the *same media* when URL canonicalization (`[A8]`) can't merge them (e.g. two distinct URLs that resolve to one video); additive, no migration.

**`rel` has no `created_dt`** (deliberately dropped). A per-edge timestamp would only back a future "recently added to this playlist" view, which no 4.0 feature needs; if 4.x ever wants it, it's an additive nullable column then. (Contrast `thing.created_dt`, which *is* used by the 4.0 dashboard, §2.1.)

### 2.3 `run`

Append-only history of every pull/download attempt, and the JSONB home of raw yt-dlp output.

```sql
CREATE TABLE run (
  id         uuid PRIMARY KEY,
  thing_id   uuid NOT NULL REFERENCES thing(id),
  worker     text,                  -- nullable; identifies the runner instance that claimed/ran this [D2]
  input_json jsonb,                 -- the per-run DECISION params: e.g. whether cookies were used this run (distinct from the thing.attrs.cookies hint, §2.1/§4.7). The OI bucket is NOT here — it lives on thing.bucket (§2.1), read off the dispatched thing
  data_json  jsonb,                 -- raw yt-dlp output + computed stats (see below)
  entries_hash bytea,               -- nullable; membership fingerprint of a playlist run; change-detection key
  playlist_count integer,           -- nullable; entry count the playlist reports (resume progress + sanity)
  starttime  timestamp NOT NULL,    -- naive UTC; assignment/start instant (worker starts immediately on assignment)
  endtime    timestamp,             -- naive UTC
  success    boolean                -- T = success, F = failure, NULL = assigned/in-progress  [F2]
);
CREATE INDEX run_thing ON run (thing_id, starttime DESC);
```

- **`success` is a T/F/NULL tristate** `[F2]`. `NULL` means **assigned/in-progress**; updated to T/F at completion. With `endtime` this expresses the job lifecycle *without* a status column (consistent with `[A3-B]`): *assigned/running* = `success NULL` + `endtime NULL`; *done* = `success` T/F + `endtime` set.
- **`starttime` is the assignment/start instant, kept `NOT NULL`.** The worker starts a job immediately on assignment, so assignment time and start time are one instant — no separate "assigned-but-not-started" state is needed. A stale `assigned/running` row (`success NULL` + old `starttime`) is the natural signal for a future crashed-worker reaper. (If a 4.x ever genuinely needs to split the two, an *additive* nullable `assigned_dt` column covers it — but the assumption here is it won't.)
- **`worker` is a nullable schema provision for prioritized dispatch** `[D2]`. The API owns prioritization and hands the runner one top job (§4.5); `worker` records which runner instance claimed/ran a row. **In 4.0 it is barely exercised** (single worker, may be a constant), but reserving it now keeps the schema frozen for 4.x multi-worker coordination and dispatch filtering (Appendix A).
- **`entries_hash` is promoted to a real column** (it does *not* live in `data_json`). It is the membership fingerprint of a playlist run, computed by the reused `pl_hash`, and it is the **change-detection key**: a run differs from the previous one iff its `entries_hash` differs from the most recent prior *successful* run for the same `thing_id` (fetched cheaply via the `run_thing` index — no extra index needed). This drives the Fibonacci backoff (§4.4: same hash ⇒ back off; new hash ⇒ speed up / there's new content) and reuses `compare_pl_runs`'s logic. Nullable: set on playlist (Stage 1) runs, `NULL` for single-video download runs. Stored as **`bytea`** (matching V3's `pl_hash`/`compare_pl_runs`, which already deal in raw bytes) — its purpose is to be compared, not displayed, so no conversion is needed. The derived `different` flag V3 stored is no longer persisted — it falls out of the comparison.
- **`playlist_count` is also promoted to a column** (nullable `integer`, per V3 `PlaylistStatsBase`): it backs partial-resume progress (§4.6, "got item X of N") and a cheap sanity cross-check. The "newest item / freshness" concept is *not* a per-run field — it is the single `thing.modified` column (§2.1).
- **`data_json` holds the raw yt-dlp output** (which carries the site's raw modified/upload dates that feed `thing.modified`, plus any incidental fields). V3's `download_count` / `failed_count` are **dropped entirely** — a direct consequence of decoupling playlist pulls from downloads: a Stage 1 playlist run attempts no downloads (so both would always be 0), and a Stage 2 run is a single video whose outcome is the `success` column.

### 2.4 Rating semantics (the core organizing principle)

**Scale: a signed float, −2.0 .. +2.0** `[B1]`.

| Grade | Value | Meaning |
|---|---|---|
| A | +2 | Always get; highest priority |
| B | +1 | Get if capacity allows |
| C | 0 | Metadata only — hold; let the user decide |
| D | −1 | **Soft delete (effect is 4.x):** drop active media, keep all metadata |
| F | −2 | **Hard delete (effect is 4.x):** keep only minimal identity; purge media + bulk metadata |

**Grade bands — round a float rating to the nearest grade.** Because machine ratings are averages/maxes, an effective rating is rarely a clean integer. The rule is simply: **round the float to the nearest integer grade** (the integer grade values are the **band centers**; ties round **up**, toward the more-positive grade) and apply that integer grade as the threshold. Equivalently, the half-unit bands are:

| Grade | Center | Band (effective rating *r*) |
|---|---|---|
| A | +2 | *r* ≥ 1.5 |
| B | +1 | 0.5 ≤ *r* < 1.5 |
| C | 0 | −0.5 ≤ *r* < 0.5 |
| D | −1 | −1.5 ≤ *r* < −0.5 |
| F | −2 | *r* < −1.5 |

So 1.99 → A, 0.6 → B, −0.3 → C, −0.7 → D, and a tie at −0.5 rounds up to C. This is the **single definition** used everywhere a (possibly averaged) rating is assessed — both the acquisition thresholds (§4.2) and deletion/copy assessment (deletion below + the 4.x scrub) — and it replaces any per-value epsilon rule. (In SQL, comparing the raw float to a band floor — `r >= 0.5` for "grade ≥ B" — is the equivalent, round-direction-safe form.)

- **`human_rating`** is user-assigned and takes one of the five discrete grade values. **D and F are both user-assignable.** `[B1]` Human rating is **authoritative**: when a `thing` has a human rating, its machine rating is irrelevant (and should be left/treated as NULL). `[B2]`
- **A user-*added* thing defaults to `human_rating = +1` (B).** When a human adds a thing (§3.2 add-by-URL), they are explicitly asking for it, so **B ("acquire") is the default**, overridable to **A (+2)** or **C (0)** at add time (D/F are not add-time options — you don't add something in order to suppress it). The propagation makes this do the right thing: since a video's machine rating is the MAX of its parent playlists' human ratings, adding a *playlist* at B makes its members assess as B and download; C makes them tracked-but-not-acquired; A adds replication.
- **`machine_rating`** is a continuous float, **computed on read** in 4.0 `[B3, B4]`:
  - **Video:** the **MAX** of the human ratings of the playlists it directly belongs to (one hop). Matches the "bumped up if found in a higher-rated playlist" override semantics.
  - **Playlist:** the **AVERAGE** of the human ratings of its directly-rated member items (one hop).
  - **One hop only in 4.0.** Multi-hop (channel→playlist→video) and weighting are explicitly left for 4.x; the door is open. `[B3]`
  - **Float-to-grade mapping** uses the **grade bands** above (½ unit above/below each grade center), not the bare integer — so a machine rating of 1.99 or 1.6 both assess as A, and 0.6 assesses as B. `[B3]`
- **Effective rating for any decision** = `COALESCE(human_rating, machine_rating)`.

**Deletion semantics** `[B1]` — **4.0 performs no deletion at all.** Rating a thing **D** or **F** in 4.0 only *records the rating*; its sole effect is that the rating gate excludes the thing from future acquisition (§4.2). No media is dropped, no metadata is purged, `best_oi` is untouched, and no tombstone exists. The actual effects are two **4.x** enhancements:

- **D (−1) — soft delete (4.x):** the **OI scrubber** deletes the active media in OI and repoints `best_oi` to the resulting **tombstone** (OI's durable record of intentional deletion, distinct from never-acquired = `best_oi IS NULL`); **all metadata is retained** (the `thing` row, its `run` history, its `rel` edges). Re-acquire later if re-rated — a 4.x rating-path corner case, **not** the scrubber (see below).
- **F (−2) — hard delete (4.x):** the scrubber deletes the media exactly as for D, **and** a separate **metadata purge** strips as much metadata as possible (run rows / `data_json`, `rel` edges, denormalized columns), **retaining only the minimal identity** needed to never fetch it again — the `thing` row with `id`, `url`/`native_id`, `backend`, `extractor_key`, `human_rating = −2`, and `best_oi`→tombstone. No yt-dlp archive entry is needed: re-suppression is enforced by the rating predicate (F is below every threshold), by re-discovery matching the existing F `thing`, and by the OI tombstone.
- **Copy policy** (ratify #36): A = 2 copies, B = 1–2, C = 0–1, D = 0, F = 0 + purge. **All enforcement — replication *and* deletion — is 4.x** (#111); 4.0 only records ratings and reconciles nothing. OI/SO remains the source of truth for what copies actually exist — LM stores no replication ledger.

### 2.5 `try_on` — the backoff oracle

A single nullable date encodes all scheduling intent (replacing V3's fixed `freq_days`, which is **not** carried into the schema — the live cadence is derived from `run.starttime` history instead; §4.4):

- **`try_on` defaults to `CURRENT_DATE` at thing creation** (DB `DEFAULT`). This makes the *date* gate open from birth, so eligibility is governed by the *rating* gate alone until the thing first runs. It is the key to making **compute-on-read machine ratings** work: a video's machine rating can rise (because a parent playlist was rated up) with **no write to the video row**, so there is no event at which to set `try_on` — but it was already `today` at creation, so the moment the rating clears the threshold the thing is eligible. (This also subsumes the "1st run ASAP" rule in §4.4 — a freshly-added playlist at/above its threshold is date-eligible immediately.)
- `try_on = past/today` → eligible to run now.
- `try_on = future` → backed off until then.
- `try_on = NULL` → do not run. **This value is intentionally overloaded** `[C2-A]`: it means both "done — never re-check" (a successfully downloaded video) and "dead — give up" (a perma-failed/acknowledged item). The worker doesn't care which; it just skips NULLs. Any human-facing query that *does* care disambiguates via `last_success_dt` / `last_failure_dt` / `best_oi`, which it consults first anyway.

State transitions:
- After a **successful playlist run:** `try_on` → a future date from the backoff formula (§4.4).
- After a **successful video download:** `try_on` → `NULL` (never re-fetch).
- After a **failure:** `try_on` → a short future date (back off, then retry). `last_failure_dt` is set; it is **nulled on the next success** `[C3-A]` — so `thing.last_failure_dt` means "currently in a failed state," not "ever failed." (Per-site intermittent-failure analysis lives in the `run` table and is a 4.x dashboard, not a 4.0 `thing`-level signal.)
- **Permanent failure:** the user sets `try_on = NULL`.
- **Rating raised to an eligible level (human):** the rating-update code sets `try_on = CURRENT_DATE`, **guarded by `best_oi IS NULL`** (never disturb an already-acquired thing). For a never-run thing this is redundant with the creation default; it matters only when `try_on` has since left today — resurrecting a permafail (`try_on = NULL`) or pulling a future-scheduled playlist forward. **Machine ratings need no equivalent:** in the compute-on-read design there is no rating-write event, and the creation default already covers them. *(If the `[B4]` fallback is ever taken — storing machine ratings via event-driven propagation — that propagation code must set `try_on = CURRENT_DATE` on the same `best_oi IS NULL` guard, exactly like the human path.)*
- **Currently-live content** `[E1-C]`: not a special case. Hitting live content is just a **temporary download failure** — the normal failure path backs `try_on` off to tomorrow (and never nulls it, since nulling is success-only), so it is retried tomorrow when it is a finished recording and downloads in full. No `is_live`-aware logic is needed beyond treating the live-hit as a failure. Live content that will *never* later exist as a recording is Pervellam's job, not LM's.

---

## Part 3 — Components

LinkMeddle V4 is **one logical system** deployed as the two processes that already exist in V3 `[D2]`: the **API** (`lmdb/`, "PLAPI") and the **job runner** (`lmdb/job_runner.py`), plus the **frontend** (`lmfe/`). We do not introduce microservices, and we do not split playlist vs. video handling. 4.0 runs a **single worker**; supporting **multiple simultaneous workers** is deferred to 4.x but the work-claim query is built for it from day one (§4.5).

External systems are unchanged: **OI/SO** (object storage; LM talks only to OI), **Crustula** (cookie/auth — stays **mostly as-is**, already integrated), **yt-dlp** + the LinkMeddle plugin (acquisition), and **`yt-dlp-verschiedenes`** (custom extractors; the "B+" milestone is the acquisition dependency). The verschiedenes extractors are **not an explicit LM dependency** — they plug into yt-dlp transparently; in 4.x, evaluate whether any warrants becoming a first-class `backend` `[A7]` rather than a yt-dlp extractor.

### 3.1 Reuse map (what V3 code becomes)

| V3 artifact | Disposition | V4 role |
|---|---|---|
| `lmdb/models.py` — DLP input models (`CommonDLP`, `PlVidDLP`, `PlaylistDLP`) | **Reuse** | yt-dlp → LM boundary contract; unchanged |
| `lmdb/models.py` — LM-native models (`VidFull`, `PlaylistFull`, `UlChan`) | **Reuse / minor add** | feed `thing`/`rel` upserts; `UlChan` feeds channel edges |
| `lmdb/models.py` — `PlaylistSum` / `PlaylistVid` | **Replace** | become `thing` + `rel` (reference for field names only — greenfield, no data migration) |
| `lmdb/models.py` — `PlaylistSched` | **Replace** | folded into `thing`: cadence → `thing.try_on` (no `freq_days`); `oi_bucket` → `thing.bucket` `[A10]`; `use_cookies`/`lpm_lib` → `thing.attrs` hints `[A11]` |
| `lmdb/models.py` — `PlaylistStats` | **Replace** | becomes the `run` table |
| `lmdb/xform.py` — Fibonacci backoff (`FIB`, `next_fib`, `rec_adjust_freq`) **Reuse**; `add_new_run` **Adapt** | `try_on` interval computation (§4.4); `add_new_run` reworked to derive the current interval from `run.starttime` history (no `schedule.freq_days`) and write `thing.try_on` |
| `lmdb/xform.py` — `compare_pl_runs`, `pl_hash` | **Reuse** | change detection / membership fingerprint → `run.entries_hash` column (compare vs latest prior successful run) |
| `lmdb/xform.py` — `pl_dlp2lm`, `full2sum`, `full2stats` | **Adapt** | DLP → `thing`/`rel`/`run` upsert pipeline (incl. existing recursive nested-playlist logic) |
| `lmdb/api.py` — schedule CRUD, `apply_update`, `get_or_404`, lifespan/session | **Adapt** | `thing`/`run` CRUD; add rating fields; URL-classify |
| `lmdb/api.py` — `GET /schedules/?next_run=` (the pull endpoint) | **Replace** | becomes the prioritized **job-dispatch endpoint** (§4.5): API owns ordering and returns the single top job, not a list to shuffle |
| `lmdb/api.py` — `POST /playlist-run` (completion-reporting, called by the postprocessor) | **Adapt** | rewrite into Phase I ingest + run-completion recording (drop its `# TODO rewrite this whole function` debt); it is a results endpoint, not a push trigger |
| `lmdb/job_runner.py` (already a *pull* loop) | **Adapt** | keep the pull model; **replace `random.shuffle` of all due jobs with requesting one prioritized job** from the dispatch endpoint, then loop |
| `lmdb/run_bknd.py` — `init_download` | **Reuse (Phase I) / adapt (Phase III)** | playlist pull as today; per-video download via `maybe_playlist=False` |
| `lmdb/run_bknd.py` — `_ydl` opts | **Adapt** | Phase I metadata-only (`extract_flat`/`simulate` per site); keep sleep intervals, `_exclude_live`; **drop `playlistrandom: True`** (→ deterministic order for 4.x resume, §4.6); **`skip_playlist_after_errors` → fail-fast** (§4.7) |
| `lmdb/run_bknd.py` — Crustula `get_cookies` | **Reuse (mostly as-is)** | + close the success-callback TODO (`run_bknd.py:138`) |
| `apiqueue/ytdl_arch_oi.py` — `ObjIdxDlArch` | **Reuse (relocate), now redundant** | OI-backed download archive; **redundant in V4** (fan-out + `best_oi`/`thing` existence already prevent re-download) but kept **as-is as a belt-and-suspenders guard against accidentally downloading the same video twice**; relocate out of `apiqueue/` before deleting it |
| yt-dlp plugin `ObjIdxUploadPP` (`yt_dlp_plugins…objidx_upload`) | **Reuse or replace** | keep the upload PP as-is, *or* have the worker upload to OI itself; **either way the worker then does a new metadata push to PLAPI** (sets `best_oi`) |
| yt-dlp plugin `LinkMeddlePlaylistPP` | **Adapt / possibly drop** | it no longer POSTs — cleaner for the **worker** to push metadata to PLAPI for *both* playlist & video runs (§3.2); the PP at most shapes yt-dlp output, or is dropped if the worker reads that output directly |
| `lmfe/api.py` — proxy / OI wiring, `GET /url` redirect, `oi_file_to_video` | **Reuse / adapt** | proxy pattern kept; endpoints follow the new `thing` API; `GET /url` *is* the URL-classify endpoint |
| `lmfe/models.py` — `ThingBase` | **Adapt** | already prefigures V4; add rating/`try_on`/timestamps; `type` → enum |
| `lmfe/static/index.html` | **Adapt** | keep player/shortcuts/routing; add rating UI + failure/`try_on` display; remove hardcoded `freq_days:3` |

### 3.2 New components with no V3 equivalent

- The `thing`/`rel`/`run` Postgres schema and its data-access layer.
- The Phase I **fan-out ingest** path (upsert things + rel + run; stub-video creation).
- The **worker → PLAPI metadata push** — after *every* run (playlist pull **or** video download) the worker posts results to `POST /jobs/{run_id}/result` (§3.3). This is **new**: V3 relied on the in-plugin postprocessor to POST playlist data; V4 unifies it into one worker-owned push for both kinds of thing, so the yt-dlp plugin need not make HTTP calls.
- The Phase III **per-video downloader** job type.
- The **prioritized job-dispatch endpoint** — the API owns prioritization and hands the runner the single highest-priority due job (running the §4.2 predicate + the `SKIP LOCKED` claim). This is the V4 evolution of V3's `GET /schedules/` + `random.shuffle`.
- **Compute-on-read machine rating** in the worker selection query.
- The **add-a-thing-by-URL action** — the primary human entry point. The user supplies a URL (optionally a grade); the `thing` is created with `try_on = today` (creation default, §2.5) and **default `human_rating = +1` / B**, overridable to A or C (§2.4). This is the V4 form of V3's "add a playlist" (`POST /schedules/`), minus `freq_days`. **URL-classify is deferred to 4.x** (implementation decision, Task 0.3): in 4.0 the add just records the URL with `type` defaulting to `playlist` ("unknown → assume playlist"); `extractor_key`, `native_id`, and the real `type` are filled in by the worker on result-ingest (§3.3, Phase 1) when the job actually runs — so no yt-dlp `suitable()`/classify step at add time.
- The **status dashboard** query endpoints (recent activity / new things / failures).
- The **permanent-failure acknowledgment** action (a PATCH setting `try_on = NULL`).

> Two pieces from the V3 reuse inventory are deliberately *not* carried forward as resume mechanisms: the **OI download archive cannot serve as the partial-playlist resume oracle** `[C5]`, because in a fan-out world most discovered things are never downloaded and so never enter the archive. See §4.6.

---

### 3.3 API surface (rough outline)

Illustrative only — verbs, paths, and intent, **not** an OpenAPI contract; exact shapes are settled at implementation. Filters are query params. Two services: the core **LMDB API** (`lmdb`, "PLAPI") and the **frontend BFF** (`lmfe`).

**Core LMDB API (`lmdb` / PLAPI)** — `thing`-centric; a near-total rewrite of V3's playlist-specific surface (no `/schedules/`, no `/playlist-run`, no `/videos/{extractor}/{id}`).

*Things*
- `POST /things/` — **add by URL** (the human entry point): record the URL, **required `bucket`** (OI storage bucket, §2.1 — no server default; the frontend supplies its `OBJIDX_BUCKET_DEFAULT`), `type` default `playlist` (override; classify deferred to 4.x — §3.2), default `human_rating=+1`/B (override A/C), `try_on=today` (§2.4, §3.2). **Optional** `cookies` suggestion and `lpm_lib` tag may also be supplied — both stored as soft hints in `attrs` (§2.1), in contrast to the required `bucket`. Idempotent on `url` (returns the existing thing, with its original `bucket` unchanged) — closes the #142 race.
- `GET /things/` — list/search; query params e.g. `type`, `rating`, `due` (`try_on≤today`), `needs_rating`, `new` (recent `created_dt`), `failing` (`last_failure_dt>last_success_dt`), `url=`, and **`extractor=`&`native_id=` — this lookup is the V4 replacement for V3's `GET /videos/{extractor}/{video_id}`**. Backs every list + the status dashboard.
- `GET /things/{id}` — one thing (+ `rel` summary + latest run). Supports `?include=related` to return the **full page view-model in a single call** — the thing plus its related things already carrying display fields, rating/acquired state, and playback pointer (see the frontend round-trip constraint below).
- `PATCH /things/{id}` — set `human_rating` (including D/F = delete intent) and permanent-failure ack (`try_on=NULL`). *(Title backfill (#147) is the worker's job on result-ingest — Task 1.1, not this endpoint. No `DELETE` verb in 4.0 — normal deletion is via the D/F rating, and the 4.x admin hard-remove can be added then.)*

*Graph & history*
- `GET /things/{id}/related` — `rel` neighbors in **both** directions (children, e.g. playlist→videos, *and* parents) in one call; an optional `direction`/`role` param narrows to one side.
- `GET /things/{id}/runs` — run history for a thing.

*Jobs (dispatch + result ingest)*
- `POST /jobs/claim` — **prioritized dispatch** (§4.5): returns the single highest-priority due job (`{run_id, thing, action, cookies}`, where `cookies` is the server's per-job cookies suggestion — §4.7) via the §4.2 predicate + `SKIP LOCKED`. 4.x accepts worker self-selection filters (`type`/`extractor`/`site`/`backend`).
- `POST /jobs/{run_id}/result` — **report a completed job** (rewrite of V3's `POST /playlist-run`, now run-scoped): **the worker posts here** after running yt-dlp — one metadata-push path for *both* playlist pulls and video downloads, rather than the in-plugin postprocessor POSTing. Stage 1 fan-out ingest (upsert `thing`+`rel`+`run`, `entries_hash`, `playlist_count`, `thing.modified`) or Stage 2 download outcome (`best_oi`, success); the server computes the backoff `try_on`.

**Frontend BFF (`lmfe`)** — likely close to V3's shape (thin proxy + SPA host + OI media proxy), re-pointed at the `thing` API. V3 had rough edges here, so treat as a **starting point, not a contract**. **The frontend API is explicitly *not* frozen** — unlike the 4.0 `thing`/`run` schema, it is a presentation layer that owns no durable data, so it is free to evolve across 4.x as SPA needs dictate, with no migration concerns. (The schema-freeze discipline applies to the database, not this layer.)
- **Design constraint — minimal round trips.** The SPA must render any page (a video page, a playlist page) in a **minimum number of API calls — ideally one** — never "fetch a list, then make a per-item follow-up call." This is precisely what the `thing` denormalization is *for*: `title`/`channel`/`thumbnail_url`/`modified`/`human_rating`/`best_oi` all live on the row (§2.1), so a single query over a thing + its `related` returns everything needed to render, with **no per-item OI or metadata round-trips** (the explicit cure for #123; the cache-like endpoints #124/#148 are 4.x sugar on top).
- `GET /page/playlist/{id}` / `GET /page/video/{id}` — return the **page-ready view-model in one response** (the BFF's job; equivalently `GET /things/{id}?include=related` on PLAPI): the thing plus its related things with display fields, rating/acquired state, and playback pointer all inline.
- `GET /` — serve the SPA (`index.html`).
- `GET /url?u=…` — URL-classify / resolve a pasted URL to a thing (V3's `GET /url`; doubles as the add helper).
- **Default bucket on add.** When the BFF creates a thing it fills the required `bucket` from `OBJIDX_BUCKET_DEFAULT` (the V3 `lmfe` behavior, retained), so GUI/bookmarklet users never have to pick a bucket; a 4.x UI may expose an override field on top of this default.
- `GET /media/{oi…}` — proxy/stream the OI object for playback (V3 `oi_file_to_video`).
- Thin proxies mirroring the core `thing` endpoints the SPA needs (list / get / PATCH-rating / add), or the SPA calls PLAPI directly — split TBD.

---

## Part 4 — How the system runs (mechanics)

### 4.1 Fan-out is two stages, not three `[C4]`

```
   Stage 1: LEARN                Stage 2: ACQUIRE
   (Phase I, 1→2)                (Phase III, 3→4)
   playlist metadata pull   ──►  per-video download
        │                              ▲
        │  creates stub things,        │  selects A/B-rated,
        │  rel edges, run record       │  not-yet-acquired,
        ▼                              │  try_on-due videos
   ┌─────────────────────────────┐    │
   │   Phase II: DECIDE (2→3)     │────┘
   │   interstitial — NOT a pull  │
   │   user/machine rating gates  │
   │   what Stage 2 will acquire  │
   └─────────────────────────────┘
```

**Stage 1 — Learn.** A playlist pull fetches metadata only (no downloads), upserts a `thing` for every discovered member (stub things get denormalized `title`/`channel` from the pull, **inherit the parent playlist's `bucket`**, and inherit the parent's soft hints — `attrs.cookies` when the parent has it, `attrs.lpm_lib` only if present — §2.1), inserts `rel` edges, and records a `run`. It also creates a `type='channel'` `thing` per distinct uploader (the playlist's *and* each video's), linking the playlist's channel via `rel type='channel_playlist'` and each video to its uploader's channel via `rel type='channel_video'` (V4's `pseudo_channel` replacement, §2.1). Extraction **depth is per-site** `[C4]`: the hope is that the **flat playlist pull already carries enough per-video metadata to make the acquire/skip decision** ("C-like"). Where it does **not** — and we **don't already have** that video's metadata — Stage 1 enriches it **inline, as part of the same playlist-learn process** ("B-like"), rather than triggering a separate single-video-metadata job. There is deliberately **no** standalone metadata-pull stage or per-video metadata job type (that was the rejected three-stage model). Whether to enrich and how deep is per-site and **left to the implementor based on live testing**.

**Phase II — Decide.** Not a job. Between Stage 1 and Stage 2, an item sits at its rating. A C/0 item the user hasn't reviewed stays at C indefinitely — the system holds its metadata but does not acquire. The user's periodic check-in (via the status dashboard) is the trigger that confirms C, bumps to A/B (acquire), or drops to D/F (delete). The system **pulls** ratings from the user; it does not push prompts. (Auto-*presenting* the rating queue, #145, is 4.1; the ability to rate is 4.0.)

**Stage 2 — Acquire.** Not a separate timed pass — just the dispatch endpoint (§4.5) returning a *video* job when one outranks the available playlist jobs: a video whose effective rating is ≥ B, not yet acquired, and `try_on`-due. The runner downloads it via yt-dlp (`maybe_playlist=False`; media to OI via `ObjIdxUploadPP` or worker-side upload, **into the bucket carried on the dispatched `thing.bucket`** — §2.1, the V4 replacement for V3's `schedule.oi_bucket`), then **pushes metadata to PLAPI** (`POST /jobs/{run_id}/result`), which sets `best_oi` and `try_on = NULL`.

### 4.2 Job selection predicate `[B2-B, C7]`

This predicate lives **inside the API dispatch endpoint** (§4.5), which owns prioritization; the runner never queries `thing` directly — it asks the endpoint for the next job. Human rating always wins; anything assessing below the **C band** (r < −0.5, i.e. grades D/F) is never acquired. Thresholds use the **grade bands** (§2.4), not bare integers. Rather than two separate passes, a single ordering spans both job types (playlists and videos), so the endpoint returns whichever is highest-priority right now; the two predicates below are the playlist and video branches of that one ordering. Two thresholds by job type:

```sql
-- Stage 1 (playlist metadata pull): grade >= C band
WHERE type = 'playlist'
  AND COALESCE(human_rating, <machine_rating computed on read>) >= -0.5  -- rounds to grade C or higher (§2.4)
  AND try_on <= CURRENT_DATE          -- NULL try_on is NOT eligible (done / permafail), §2.5
  AND (last_success_dt IS NULL OR last_success_dt::date < CURRENT_DATE)
ORDER BY COALESCE(human_rating, machine_rating) DESC, try_on ASC

-- Stage 2 (video download): grade >= B band
WHERE type = 'video'
  AND COALESCE(human_rating, <machine_rating>) >= 0.5   -- rounds to grade B or higher (§2.4)
  AND best_oi IS NULL                 -- "never acquired"; an already-acquired thing (live media, or a 4.x tombstone) is not re-acquired here (§2.4)
  AND try_on <= CURRENT_DATE          -- NULL not eligible; a live-hit is just a failure → try_on backed off to tomorrow (§2.5, [E1-C])
ORDER BY COALESCE(human_rating, machine_rating) DESC, try_on ASC
```

The machine-rating expression is computed on read (one-hop MAX for videos, AVG for playlists). Making this **simple and fast inside the selection query is the single biggest implementation risk** in V4 `[B4]` — see §6.

> **Re-acquiring a deleted-then-re-wanted thing is an out-of-scope 4.x corner case — and notably needs no scrubber.** Because `best_oi` points at a tombstone after deletion, `best_oi IS NULL` selects only never-acquired things, so the 4.0 worker leaves a re-rated deleted item alone. The 4.x mechanism lives in the **rating-change path**, not a scrubber: on a rating raised back to an eligible level, check the item's single `best_oi` object — if it is still an **active object** (the scrubber hasn't actually dropped it yet), it's a no-op (we still have it); if it is a **tombstone**, set `best_oi = NULL`, `last_success_dt = NULL`, and `try_on = CURRENT_DATE`, after which the **normal worker download flow re-acquires it organically**, exactly like a first acquisition. This keeps `[A5-A]` intact (the worker maintains `best_oi`; no scrubber dependency).

### 4.3 Run order — one prioritized dispatch, no phased passes `[C7-A]`

There is **no "playlist pass then video pass."** The dispatch endpoint applies a **soft priority via `ORDER BY`** (rating DESC, `try_on` ASC) with a **playlist-before-video** distinction baked into the same ordering, and hands back the single top job each request — approximating the notes-p112 sequence (A playlists → A videos → B playlists → C playlists → B videos) without any phase barriers or global passes. In practice the top job is usually a due playlist when one exists, then videos fall out as playlists are exhausted. The runner just loops: ask, run, report, repeat. **Capacity is not formalized in 4.0** `[C6-C]`: keep dispatching until nothing is due. Promote to a wall-clock time box (4.x), then per-run/per-extractor quotas (4.2, with #127), only when the host actually saturates.

### 4.4 The `try_on` backoff formula `[C1]`

Reuse the existing Fibonacci logic (`FIB = [1,2,3,5,8,13,21,34]` days, `next_fib`); the V3 `rec_adjust_freq`/`add_new_run`/`next_run` are reworked onto the `run` table as **`xform.next_try_on(rating, runs)`** (Task 1.4, implemented). **No "hold for first 3 runs" guard** (#149): that guard was a workaround for bug #97, whose real fix is fan-out itself. Instead:

- **1st run:** ASAP — implemented by the `try_on = CURRENT_DATE` creation default (§2.5), gated only by rating.
- **2nd run:** the initial interval from the rating table below.
- **Subsequent runs:** adapt naturally via Fibonacci (back off when nothing changes / all-fail; speed up when every run finds new content). "Nothing changed" = this run's `run.entries_hash` equals the most recent prior successful run's (§2.3).

**Where the "current interval" comes from (no `freq_days`).** V3 stored the live cadence in `PlaylistSched.freq_days`; V4 keeps **only `try_on`**, so the interval that `next_fib` steps from is **derived from the `run` table** — the gap (in days) between the last two successful `run.starttime`s for the thing (the same quantity V3's `xform.next_run` already computed from consecutive run timestamps). Consequently `add_new_run` was **reworked, not reused verbatim**, as `xform.next_try_on`: it reads no `schedule.freq_days`/`schedule.next_run` but computes the next interval from run history (`run.starttime` gaps + `entries_hash` change-detection) and the result is written to `thing.try_on` by `submit_result` after each playlist run (and after a video *failure*). (Task 1.4; see Part 5.)

Initial intervals (tunable constants; per-site tuning is 4.x):

| Rating | Initial interval |
|---|---|
| A | 3 days |
| B | 5 days |
| C | 8 days |

### 4.5 Process & concurrency model `[D1, D2]`

- Two processes, **continuing V3's pull architecture** `[D2]`: the **API** and the **job runner**. The runner already pulls (V3 `job_runner.py` calls `GET /schedules/`); V4 keeps that and improves it.
- **The API owns prioritization.** The runner asks a **dispatch endpoint** for the next job; the API runs the §4.2 predicate, picks the single highest-priority due job, and returns it. This replaces V3's "fetch all due schedules, `random.shuffle`, run in random order" — the runner no longer decides order, and no longer queries `thing` directly.
- 4.0 = **single worker**; multi-worker is 4.x. The dispatch endpoint claims work with **`SELECT ... FOR UPDATE SKIP LOCKED`** `[D1-B]` so it's correct the day a second worker appears.

> **Implementor note — `SELECT ... FOR UPDATE SKIP LOCKED`:** In one transaction, **the dispatch endpoint** runs its selection query (§4.2) with `FOR UPDATE SKIP LOCKED`. `FOR UPDATE` takes a row-level lock on each selected row; `SKIP LOCKED` tells Postgres to silently *skip* any row already locked by another transaction instead of blocking on it. The result: two concurrent dispatch calls never hand out the same `thing` — each gets disjoint currently-unlocked rows. The endpoint records the claim by **creating the `run` row at claim time** (`success = NULL`, `worker` set, `starttime` = the assignment instant — see §2.3) and returns its `run_id` to the worker (the `{run_id, thing, action}` of §3.3); it then commits to release the locks. *(Reconciled in the Task 1.2 build: 4.0 creates the run at claim — superseding the earlier "4.0 returns the job; the runner records the run" split — so the result-ingest endpoint always has a `run_id`; what stays 4.x is worker self-selection and multi-worker coordination.)* This is the idiomatic Postgres work-queue pattern, costs a few words of SQL, fixes the real `POST /playlists/` race (#142) at the DB level, and makes the single-worker 4.0 design forward-compatible with multi-worker 4.x at near-zero cost.

- **Job assignment is deliberately thin in 4.0** and grows in 4.x: **worker self-selection** (the worker tells the dispatch endpoint which job kinds it will accept — by `type` playlist/video, `extractor_key`, `site`, or `backend` — and the endpoint returns only matching jobs), and coordinating multiple workers via the `worker` column. (Writing the `run` at claim time is *in* 4.0 — see the reconciliation note above.) The schema already supports all of it (those are existing `thing` columns + `run.worker`) — no migration (Appendix A).
- We do **not** split playlist vs. video into separate services — both are `thing`s handled by one engine `[D2]`.

> **4.0 known behavior — single-worker only (two edge cases that v4.1 hardens):** 4.0 is correct and safe for **one** worker but deliberately omits the guards that make a *second* worker safe. The claim transaction commits and **releases its `FOR UPDATE` lock immediately** (it does not span the job, which runs outside any DB transaction), and the dispatch predicate reads only `thing` columns — it does **not** consult the open `success=NULL` `run`. Two consequences:
> 1. **Crash mid-job → automatic retry, with a lingering run.** If a worker claims a `thing` and dies before posting, its `run` stays `success=NULL` forever (no reaper in 4.0). Because the claim never mutated the `thing` (no `try_on` bump, no assigned flag), the `thing` stays eligible and a new worker simply **re-dispatches it** — nothing is lost or stuck. The orphaned run is harmless (change-detection compares only *successful* runs) but accrues.
> 2. **A second concurrent worker double-runs the same `thing`.** A worker started while another's job is in flight will be handed the *same* `thing` as a *new* `run`, since the first claim's lock is long gone and the in-progress run is invisible to the predicate. If both complete: *staggered* posts are safe (the Task 1.1 ingest upsert is idempotent — no duplicate `thing`/`rel`, just a redundant successful `run` + wasted work); *simultaneous* posts can 500 the loser on a stub `UNIQUE` index.
>
> The fixes — an in-progress dispatch guard, a stale-claim reaper, and ingest race tolerance — are **Concurrent-worker hardening** in Appendix A (Deferred to v4.1); they need no schema change.

### 4.6 Playlist ordering & resume `[C5]`

**4.0 does not partially resume** — a failed playlist pull fails whole (§4.7) and is re-pulled whole on the next `try_on`. Its one preparation for the future is **deterministic ordering: 4.0 drops V3's `playlistrandom` (was `True`)** and processes playlist entries in natural order. Shuffling each run makes lazy-loading and resumption impossible — you can't "get until item X then stop" or "resume where we left off" if the order changes every time — so turning randomization off in 4.0 is the prerequisite for the 4.x work below.

**4.x — lazy-load + partial resume `[C5]`.** For huge playlists (#83), resume is a **combination of (A) parsing `run` history** to reconstruct what was already fetched **and (C) a yt-dlp "get until item X then stop" cap** (`--lazy-playlist`-style), both relying on the deterministic order above. The OI-backed download archive is **not** the resume oracle (it only contains fully-downloaded videos, a minority of discovered things) — though it is kept as a **redundant guard against accidentally downloading the same video twice** (§3.1). Exact mechanism is a game-day decision finalized during implementation and validated against real sites.

### 4.7 Failure handling & anti-rate-limiting

**4.0 — fail the whole playlist on *any* failure.** V3 tolerated up to 3 errors (`skip_playlist_after_errors=3`) because those were mostly *per-video download* problems. In V4 a Stage 1 playlist pull is **metadata-only** (it downloads no videos), so a failure is almost certainly a *whole-playlist* problem — site down, rate-limited, auth/cookies, or a broken extractor — not one bad entry. So 4.0 fails fast: abort the run on the first error (`skip_playlist_after_errors` → 1 / fail-the-run), record the failure, and let the `try_on` backoff retry the whole playlist later (§4.4). No partial resume is attempted in 4.0 (§4.6).

**Cookies are opt-in and failure-escalated** `[A11]`. The worker defaults to **no cookies** (avoid them whenever possible). It opts in when the thing carries an `attrs.cookies` hint (§2.1), and — independently of any hint — when a prior **cookieless attempt failed**, subsequent attempt(s) for that thing should *consider* cookies (read from `run` history: last attempt was cookieless and failed ⇒ try with cookies next). *(Implemented in 4.0: the dispatch endpoint computes the per-job `cookies` suggestion — §4.5 — as the hint **OR** "the most recent completed `run` failed and its `input_json` shows no cookies"; Task 1.4.)* Whatever it chooses, the actual decision is recorded in that run's `run.input_json` (§2.3). This is deliberately looser/best-effort than the strict `bucket` flow; a per-`site` cookie policy is 4.x (the per-site table, below).

**4.x — per-site, per-day failure limit (≈3).** To stop banging our head against a rate limit, 4.x adds a **per-`site`, per-day failure counter** (#127, refined): once a site accrues N failures (e.g. 3) in a day, stop dispatching jobs for that site until the next day. This supersedes V3's per-run consecutive-error tolerance and the original #127 "per-session" framing, and pairs naturally with worker self-selection (§4.5).

---

## Part 5 — Implementation Subtasks

Relative complexity: **S** (hours–day), **M** (a few days), **L** (the hard, multi-day cores). Recommended order is top-to-bottom; tasks within a phase can overlap where noted.

### Phase 0 — Foundations (must precede everything)

| # | Task | Cplx | Issues | Notes |
|---|---|---|---|---|
| 0.1 | Create the `thing`/`rel`/`run` Postgres schema + indexes (§2) | **M** | #129, #80, #128(resolved: no order) | Schema is frozen here; switch `DATABASE_URL` to Postgres+JSONB; close #80/#128 when this lands |
| 0.2 | Port DLP boundary models + reusable `xform` helpers (Fibonacci, `compare_pl_runs`, `pl_hash`, `pl_dlp2lm`) onto the new layer | **M** | — | Reuse-heavy; mind the SQLModel `is_(None)` select gotcha |
| 0.3 | `thing`/`run` CRUD API; **add-a-thing-by-URL** (record URL, default `human_rating=+1`/B, override A/C; `type` default playlist, classify deferred to 4.x; `try_on=today`); `?url=` + `extractor`/`native_id` lookup; `apply_update`/PATCH (rating + permafail-ack); no DELETE | **M** | #140, #142, #102(clarify) | #142 race closed by the `UNIQUE(url)` idempotent add; URL-classify deferred to 4.x |

### Phase 1 — Fan-out core (the heart of V4)

| # | Task | Cplx | Issues | Notes |
|---|---|---|---|---|
| 1.1 | **Stage 1 ingest:** new fan-out ingest endpoint + **worker metadata push** (replaces the PP POST); upsert `thing`+`rel`+`run`; create stub videos with denormalized fields; per-site depth flag; **backfill `thing` title/extractor/native_id/real type when NULL (#147)** | **L** | #97, #110, #137, #83, #147 | #137 sidestepped via stub creation; #97 likely resolves naturally; #147 = worker fills/updates fields on ingest |
| 1.2 | **Prioritized dispatch + thin runner:** API job-dispatch endpoint (§4.2 predicate + `FOR UPDATE SKIP LOCKED`, single top job, soft priority order); adapt `job_runner.py` to pull one prioritized job and loop (replacing `random.shuffle`); `run.success=NULL` in-progress marker | **L** | #115, #19 | API owns ordering; §4.2–4.5; close #19 when this lands |
| 1.3a | **`thing.bucket` end-to-end** (do **before/with 1.3** — the Stage-2 uploader can't run without it). Restores V3's `schedule.oi_bucket` as a first-class `thing` field `[A10]`. **Additive retrofit** of already-built 0.1/0.3/1.1 + new consume work (greenfield, no data migration): (a) **schema** — `bucket text NOT NULL` on the `Thing` model + `lmdb/schema/v4.0.sql`; (b) **API** — required `bucket` on `POST /things/` (no server default), `lmfe` defaults it from `OBJIDX_BUCKET_DEFAULT`; (c) **fan-out** — Stage-1 stub creation inherits the parent playlist's `bucket` (set once, never changed), `/jobs/claim` returns it; (d) **consume** — runner reads `thing.bucket` off the dispatched thing → uploader `oibucket=`, *not* `run.input_json` (§2.1, §2.3, §4.1) | **S/M** | #115 | Bucket travels on the thing, not the schedule/run |
| 1.3b | **`cookies` + `lpm_lib` soft hints** (do **before/with 1.3**) `[A11]`. **No schema change** — both ride in `thing.attrs`. (a) **API** — optional `cookies`/`lpm_lib` on `POST /things/` → `attrs`; (b) **fan-out** (1.1 retrofit) — copy parent→child stubs: `attrs.cookies` always when set, `attrs.lpm_lib` only if present, one-way; (c) **consume** (1.3) — worker records the per-run cookies *decision* in `run.input_json` and passes `attrs.lpm_lib` to `ObjIdxUploadPP(lpmlib=)`. Cookieless-failure escalation lives in 1.4/§4.7 | **S** | #115, #118 | Looser than bucket; per-site cookie policy is 4.x |
| 1.3c | **Channel fan-out** (do **before/with 1.3**) `[A11]`. **No schema change** (new `rel.type` value, free text). Stage-1 ingest (1.1 retrofit) auto-creates a `type='channel'` `thing` per **distinct uploader** (playlist's + each video's) + a `rel type='channel_playlist'` edge (channel→playlist) and a `rel type='channel_video'` edge (channel→video); same-uploader videos share one channel node. V4 form of V3 `pseudo_channel`; no boolean. 4.0 builds the graph only; channel FE is 4.x | **S** | #120, #46 | `rel.type` is the marker; reachable-by-channel |
| 1.3 | **Stage 2 downloader:** per-video path (`init_download(maybe_playlist=False)`; OI upload via `ObjIdxUploadPP` or worker-side); worker metadata push sets `best_oi`, `try_on=NULL` (consumes `thing.bucket` per 1.3a; `attrs.lpm_lib`/cookies-decision per 1.3b) | **M** | #115 | Reuses existing execution primitive |
| 1.4 | **`try_on` scheduler integration:** Fibonacci + initial A3/B5/C8; failure backoff; live re-check edge case. **Compute the current interval dynamically from the `run` table** (`run.starttime` gaps — no `freq_days`); **rework `add_new_run`** accordingly (§4.4). Also owns the **cookieless-failure escalation** (§4.7) | **M** | #149(resolved: no guard) | §4.4, §4.7 |

### Phase 2 — Ratings & decisions (deletion deferred to 4.x)

| # | Task | Cplx | Issues | Notes |
|---|---|---|---|---|
| 2.1 | **Rating CRUD:** PATCH `human_rating` (−2..+2) on any `thing`; on raise-to-eligible set `try_on=CURRENT_DATE` where `best_oi IS NULL` (§2.5) | **S** | #36, #23(rating part) | Can start early, in parallel with Phase 1 — unblocks user testing |
| 2.2 | **Compute-on-read machine rating:** video=MAX(parent playlists), playlist=AVG(member items), one hop, grade bands (§2.4); wire into the worker predicate | **L** | #36 | The perf-sensitive core; fallback in §6 |
| 2.3 | **Deletion — none in 4.0.** Rating D/F (via 2.1) only records the rating; the rating gate (§4.2) excluding the thing is the *only* 4.0 effect. Media delete (D/F) and the F-only metadata purge are **4.x** (Appendix A). | — | #36, #111 | No 4.0 deletion code beyond the rating itself |

### Phase 3 — Observability, frontend, integration close-out

| # | Task | Cplx | Issues | Notes |
|---|---|---|---|---|
| 3.1 | **Status dashboard endpoints:** recent activity / new things / failures (notes p122) | **M** | #35(→reopen as new), #129 | `created_dt`, `last_failure_dt`, `success` back these |
| 3.2 | **Frontend:** rating UI (A/B/C/D/F), `try_on` + failure display, permafail-ack action; remove hardcoded `freq_days:3` | **M** | #23, #129, #143(bug) | Player/shortcuts/routing unchanged |
| 3.3 | **Crustula success callback** (close `run_bknd.py:138`) | **S** | #118(FE flow→4.2) | DoD #8; one `requests.post` on the pattern that already exists |

**Critical path:** 0.1 → 0.2 → 0.3 → 1.1 → 1.2 → 1.3a → 1.3b → 1.3c → 1.3 → 1.4 → 2.2 → 2.3, then 3.x. Rating CRUD (2.1) should be pulled forward and built alongside Phase 1 so end-to-end manual testing is possible before machine rating (2.2) lands.

---

## Part 6 — Key Risks & Things to Validate at Implementation

1. **Compute-on-read machine rating performance (highest risk).** `[B4]` The worker selection predicate joins `thing`→`rel`→`thing` to compute MAX/AVG on every cycle. At single-worker Pi scale this should be fine, but it must be measured. **Fallback if it isn't:** store `machine_rating` and keep it fresh **event-driven only** (propagate on human-rating change and on new `rel` edge) — **no twice-daily batch** (the design never wanted both). The nullable column already exists for this.
2. **Per-site fan-out depth.** `[C4]` Whether a site's playlist payload carries enough per-video metadata to decide on (C-like) or needs a heavier pull (B-like) is empirical. Build the depth as a per-extractor setting (a constant map in 4.0; a `site` table in 4.x).
3. **Partial resume mechanism (now 4.x).** `[C5]` 4.0 fails a playlist whole and re-pulls whole (§4.7), so there is no 4.0 resume risk; the 4.x A+C combination (run-history parse + lazy-playlist cap) still needs real-site testing, and depends on 4.0 having shipped deterministic ordering (`playlistrandom` off).
4. **SQLModel query gotchas.** `[F4]` Keep SQLModel for Pydantic validation, but be ready to drop to plain SQLAlchemy for queries. Never use Python `is not None` in a select filter on a nullable column (silently wrong); use SQLAlchemy `Model.field != None` / `is_(None)`.
5. **F-grade re-suppression.** Verify that a re-discovered F video matches the existing minimal `thing` (via `(extractor_key, native_id)`) rather than creating a new row — this is what prevents re-acquisition once the archive entry is gone.

---

## Appendix A — Descoped from 4.0 MVP (deferred to 4.x)

These are real V4 features, just not in the MVP. The 4.0 schema is built so each lands as additive columns/tables or pure logic — **no migration of existing 4.0 data**.

### Deferred to v4.1
- **OI scrubber** (#111) — a **4.x feature; the home of *all* actual media deletion + replication (4.0 does none).** Reconciling LM ratings against OI holdings, it (a) **replication:** ensures A/B-rated things have their target copy count (A=2, etc.), and (b) **media deletion:** for any human **D or F** thing, deletes its media in OI and repoints `best_oi` to the tombstone. It is **not** a 4.0 dependency, and it does **not** re-acquire re-wanted deleted items (that is the rating-path mechanism, below).
- **F-only metadata purge** (4.x — the *second* deletion enhancement) — for **F**-rated things, strip as much metadata as possible (`run` rows / `data_json`, `rel` edges, denormalized columns), leaving only the `thing` row with its `url`/identifier, keys, and `F` rating. Distinct from the media-deletion scrubber above; both are 4.x, and 4.0 does neither.
- **Re-acquire a deleted-then-re-wanted item** (#70 "redownload deleted") — handled in the **rating-change path, not the scrubber**: on a rating raised back to eligible, inspect the single `best_oi` object — still-active → no-op; tombstone → null `best_oi` + null `last_success_dt` + `try_on = today`, and the normal worker flow redownloads it organically like a first acquisition. Schema-free; pure rating-handler logic.
- **Auto-present the rating queue** (#145) — surface things needing a human rating. *Rating itself is 4.0; auto-presenting is 4.1.*
- **URL canonicalization on write** `[A8]` — normalize URLs before insert (strip `&list=`/tracking params, resolve short links, unify host/scheme/trailing slash, per-extractor rules) so the `UNIQUE (url)` index actually dedups variants instead of only byte-identical strings. 4.0 ships the constraint but stores URLs as-given; this makes the paste-time guard effective and reduces near-duplicate stubs that otherwise only collide later at the native-key index. Schema-free — pure write-path logic, no migration.
- **Direct DL via object store** (#68), **make yt-dlp quieter** (#39), **re-enable subtitles** (#95 part).
- **Multiple simultaneous workers** (#27 partial / #103) — the 4.0 `SKIP LOCKED` claim query is already built for this; this is the step that actually runs more than one worker concurrently. (Full distributed/parallel architecture stays V5 — Appendix B.)
- **Richer job assignment** `[D2]` — **worker self-selection** and multi-worker coordination via the `worker` column. *Worker self-selection:* a worker declares which job kinds it is willing to take — filtering by `type` (playlist vs video), `extractor_key`, `site`, or `backend` — and the dispatch endpoint hands back only the highest-priority job matching that filter. Useful for heterogeneous workers (e.g. one box holds the cookies/IP for a given site, or a worker is dedicated to video downloads vs playlist pulls). All filter dimensions are existing `thing` columns, so this is pure endpoint/runner logic — no migration. *(Note: writing the assigned `run` row at claim time — `success=NULL`, `worker` set, `starttime` = assignment instant — is **already in 4.0**, pulled forward during Task 1.2 so result-ingest always has a `run_id`; see §4.5. What remains deferred here is self-selection + multi-worker coordination — i.e. **deciding** who gets which job, not **recording** the claim.)*
- **Concurrent-worker hardening** `[D1, D2]` — the guards that make >1 worker *safe*, not just *possible* (a prerequisite for "Multiple simultaneous workers" above). 4.0 deliberately omits these because it runs one worker; they are pure endpoint/runner logic on the existing schema (no migration). Three pieces, motivated by the two 4.0 edge cases noted in §4.5:
  1. **In-progress dispatch guard.** The 4.0 claim predicate reads only `thing` columns, so it does **not** see that a `thing` already has an open (`success = NULL`) `run`. With two workers this double-claims the same `thing` (the claim's `FOR UPDATE SKIP LOCKED` lock lasts only the ~ms claim transaction — it does *not* span the job). Fix: exclude things with a live in-progress `run` from dispatch — e.g. `NOT EXISTS (SELECT 1 FROM run WHERE run.thing_id = thing.id AND run.success IS NULL AND run.starttime > now() - <lease>)`, or equivalently treat the open `run` as a soft lease keyed on `run.starttime`/`run.worker`.
  2. **Stale-claim reaper.** A worker that crashes mid-job (edge case 1) leaves its `run` at `success = NULL` forever — harmless to correctness (change-detection only compares *successful* runs) but it accumulates, and without a lease the in-progress guard above would block the thing permanently. Fix: a sweep that fails out in-progress runs older than a lease window (`success = FALSE`, `endtime = now`), freeing the `thing` for re-dispatch. (4.0 has no reaper; the crashed thing is simply re-dispatched on the next claim because nothing marks it.)
  3. **Ingest race tolerance.** If two workers do complete the same `thing` and post *simultaneously*, the second ingest can hit the `UNIQUE(url)` / `(backend, extractor_key, native_id)` index on a stub INSERT (the result endpoint catches `IntegrityError` only on the `native_id` *backfill*, not on stub inserts). Fix: wrap the per-stub upsert in a savepoint + re-lookup (the same lost-race pattern `POST /things/` already uses). *Staggered* posts are already safe — the Task 1.1 upsert is idempotent (no duplicate `thing`/`rel`), leaving only a redundant successful `run` row and wasted bandwidth.

### Deferred to v4.2
- **Thumbnails** (#23 thumb part / #95) — the `thumbnail_url` column already exists; this is UI + extraction work.
- **Channels in FE** (#120), **Pervellam read-only display in FE** (#121 / `[E2-A]` — display only; both already store into OI), **more FE metadata** (#126), **update related playlists with video** (#133), **webapp keys + mobile** (#134), **FE-Crustula curl flow** (#118).
- **Per-site/per-day failure limit** (#127, refined) — once a `site` accrues ≈3 failures in a day, stop dispatching to it until tomorrow (supersedes V3's per-run tolerance and the old per-session framing). §4.7.
- **Lazy-load + partial playlist resume** `[C5]` (#83) — run-history parse + a yt-dlp "get until item X then stop" cap for huge playlists, enabled by 4.0's deterministic ordering (`playlistrandom` off). 4.0 just fails-and-re-pulls-whole; this is the optimization that avoids re-pulling huge lists. §4.6.
- **Per-site tuning** (per-site config — *new additive table*, keyed by the `thing.site` column that already exists `[A7]`): per-site `try_on` intervals `[C1]`, per-site fan-out depth `[C4]`, site-median-longevity heuristic, rate budgets.
- **Capacity controls** `[C6]`: wall-clock time box, then per-run/per-extractor quotas.
- **Multi-hop / weighted machine rating** `[B3]`.
- **A second acquisition backend** `[A7]` — implementing a non-yt-dlp engine (e.g. `gallery-dl`, direct HTTP, a bespoke scraper) behind the worker's dispatch branch. The `thing.backend` column and the `(backend, extractor_key, native_id)` key are already in the 4.0 schema, so this is pure code, no migration.
- **Cache-like endpoints** (#124, #148), **arbitrary OI queries in FE** (#135), **MPV client** (#139), **deno for EJS extractors** (#93), **installable/systemd** (#28), **stop huge upscales** (#114), **Wayback/Memento fallback acquisition** (#59, move out of v5-ml), **email/Android send-to endpoints** (#45, #33).
- **Cross-metadata deferred refs** beyond the stub-creation already done in 4.0 (#137 remainder).

---

## Appendix B — Deferred to V5 (new schema permitted)

- **AI/ML machine ratings** and **probabilistic decision pipeline** (notes p123–124).
- **Auto-spider discovery** (find content without user-supplied URLs) — the dashed "auto spider" edge in the p116 component diagram.
- **LPM (Library Person Media) integration** (#136) — V5. Schema prep is already done: `thing.url` is nullable, so URL-less libraries/people won't force a 4.x migration.
- **Facial recognition / face-lookup with LPM** (#131).
- **Conversational AI frontend** (notes p123).
- **Pervellam deep integration** (shared decision logic / control) `[E2]` — display is 4.2; control is V5.
- **Distributed/parallel worker architecture** (#27 full, #103) beyond the v4.1 multi-worker step.
- **`ytul`/`mediacrawler` discovery integration** (#20), **Wyze** (#11), **Shutterfly** (#10).

---

## Appendix C — Stopped Completely

### C.1 Code to remove
- **`apiqueue/`** — the V2 Celery-based queue/download system. Celery was dropped in V3 and is **not** reintroduced for V4. **Before deletion, relocate `ytdl_arch_oi.py` (`ObjIdxDlArch`)** into `lmdb/` or a shared module — it is reused.
- **`scripts/`** — V1-era per-site scrapers (predate yt-dlp). Site-specific extraction lives in `yt-dlp-verschiedenes` plugins, not here. **Verify against #79 first** — its most recent comment warns we *"may need to partially revert `2b20100`"*, so confirm nothing live still depends on these before deleting.
- *(Not removed — clarification.)* `lmdb/job_runner.py` and `POST /playlist-run` are **carried forward, not deleted** (see §3.1): the runner is already a *pull* loop and is adapted to request one prioritized job; `POST /playlist-run` is a completion-reporting endpoint (not a push trigger) that is rewritten into Phase I ingest + run recording. The only thing being *stopped* here is copying their tech debt — drop the `# TODO rewrite this whole function`. The piece genuinely retired is V3's **"fetch all due + `random.shuffle` + run in arbitrary order"** scheduling, replaced by API-owned prioritized dispatch (§4.5).

### C.2 Issue disposition

**Policy (don't bulk-close):** (a) **old V1/V2-era issues** this design supersedes → **move to the new `v1+v2 cleanup` milestone**, not closed; (b) **issues V4 actually tackles** → **leave open until implemented**, then close as the subtask lands.

**Move to `v1+v2 cleanup` (superseded; not V4 work):**

| Issue | Reason |
|---|---|
| #84 swap out redis | Celery/redis dropped; no broker |
| #81 Fan out tasks | Celery-based fan-out superseded by `thing`/`rel`/`run` |
| #82 Alternatives | architecture decided |
| #69 offload metadata to OI | decided opposite — metadata stays in Postgres `[F1]` |
| #8 Refactor common parts | superseded by the V4 redesign |
| #75, #22, #54, #32, #76, #65, #60, #3 | stale V1/V2 cleanup / "mayfix" rot |

**Leave open until implemented (V4-tackled — close only when the subtask ships):** #80 (Postgres-JSONB metadata store → §0.1, resolves `[F1]`), #19 (smarter refresh/retry → `try_on` scheduler, §1.4), #129 (failure visibility), #115 (downloader), #128 (rel order — resolved "no order" `[A4-B]`; close when the schema lands), plus the rest of the MVP-milestone issues.

Issue-milestone moves consistent with the architect's triage (now ~26 open in the MVP milestone): **into fanout(v4):** #143 (active bug); **stays 4.x not 4.0:** #127, #143-class non-bugs, #145 (rate in 4.0, auto-present in 4.1); **→v5:** #27, #103; **→v4.2:** #93, #28; **→v4.1:** #70 (redownload-deleted, via the rating-path, not #111). Needs clarification before/at build: #34 (permissions scope), #46 (channel-vs-user after extractor migration), #150 (live — answered by `[E1-C]`).

### C.3 Ideas considered and rejected
- **Queues as primary state** (Celery / rq / arq / huey) and **Redis/Valkey as broker** — replaced by querying the DB directly.
- **Separate document store** (CouchDB / MongoDB) — Postgres JSONB instead `[F1]`.
- **Separate graph database** (Neo4j etc.) — the `rel` table instead.
- **Per-resource state-machine table** / explicit `status` column — state is derived `[A3-B]`.
- **`deleted_dt` column** — not needed; deletion is recorded by the **OI tombstone** (which `best_oi` points to) plus the D/F rating `[A3-B, C2-A]`.
- **Storing `rel.order`** — derived on demand instead `[A4-B]`.
- **Compound natural key `(backend, extractor_key, native_id)` as PK** — surrogate UUID instead `[A1-A]`.
- **V3→V4 data migration** — greenfield instead `[F3]`.
- **Twice-daily rating batch + synchronous + incremental (three sync paths)** — compute-on-read (or, fallback, event-driven only) `[B4, B5]`.
- **The "#149 hold-for-first-3-runs" backoff guard** — unnecessary once fan-out fixes #97 `[C1]`.
- **Three-stage fan-out** (separate metadata-pull stage) — two stages with per-site depth `[C4]`.
- **OI download archive as partial-resume oracle** — can't work in a fan-out world `[C5]`.
- **`last_failure_dt` never-nulled (most-recent-ever)** — nulled-on-success "currently-broken" marker instead `[C3-A]`.
- **Time-series stats database** and **full mobile/Android app** — out of scope (mobile UI is a 4.2 concern, not a separate app).

---

## Decision Traceability

Every `[Xn]` tag above maps to an entry in `lm-v4-open-decisions.md` (the authoritative decision log) — including `[A7]` (non-yt-dlp `backend` code + `site` column), `[A8]` (unique `url` index), and `[A9]` (`run`/`thing` first-class columns: `entries_hash`, `playlist_count`, `thing.modified`), which were decided during finalization and back-ported into the log. The Start/Stop/Continue priorities (`lm4-start-stop-continue.md`) are reflected throughout: **Start** — ratings (2.1), individual video downloads (1.3), rate-limit budgets (4.7→4.x), parallel work (4.x), failure visibility (3.1); **Stop** — coupled playlist+download (fan-out, Part 4), over-reliance on yt-dlp structure and on OI for metadata (Postgres-first, `[F1]`); **Continue** — retry logic (4.4), Python, REST, yt-dlp, Crustula (3.3), latest OI/SO, no AI/ML yet, UI focus.
