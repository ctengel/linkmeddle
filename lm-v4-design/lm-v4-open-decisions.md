# LM V4 — Open Engineering Decisions for the Chief Architect

**Generated:** 2026-06-12
**Purpose:** A single decision log that collects every open engineering decision, conflict, gap, and improvement opportunity across the V4 design corpus, so the chief architect can resolve each one in place. Once the `Architect Decision` fields below are filled in, this document feeds the production of a single finalized V4 design document.

**Source documents consolidated here:**
- `LM-V4-DESIGN-SUMMARY.md` (DS) — Feb 12 voice-transcript synthesis
- `lm-specs-2026-06-11-notes.md` (NOTES) — handwritten PDF, pp. 105–124
- `lm-v4-existing-work.md` (WORK) — code-reuse inventory
- `lm-v4-issues-analysis.md` (ISSUES) — GitHub issue triage

**How to read each entry:** every item states where it shows up, the realistic options with trade-offs, an optional lean recommendation, and a blank `Architect Decision:` field. A `[tag]` on each heading marks provenance:
- `[flagged]` — already called out as open in one of the source docs
- `[conflict]` — two source docs (or an issue and a doc) disagree
- `[surfaced]` — newly apparent from reading the docs side by side; not previously called out
- `[improvement]` — a simpler/cleaner path that may not have been fully considered

---

## A. Data Model & Identity

### A1. `thing.id` — surrogate UUID vs. extractor-native compound key `[conflict]`

WORK contradicts itself and DS on the most fundamental schema question — what is the primary key of `thing`?

- DS §thing table: `id` = "Stable internal **UUID**."
- WORK §1: maps `PlaylistSum.id → thing.id (as **DLP-native ID**)`, then one paragraph later says "V4 uses a **UUID** (`thing.id`). … old `playlist_id` stored as a migration lookup column."
- NOTES p119: `id` = "Stable internal ID" (silent on UUID vs native).
- Related: `xform.py` TODO "we need this until we get lmpl id"; `GET /videos/{extractor}/{video_id}` already treats `(extractor_key, native_id)` as the lookup key.

**Options:**
- **A — Surrogate UUID PK, native ID as attribute.** `thing.id` is an opaque LM-assigned UUID; `(extractor_key, native_id)` is a unique secondary index. Stable across re-extraction, supports URL-less things (see A6), satisfies the "lmpl id" TODO. Cost: every external lookup goes through the secondary index; migration must mint and remember UUIDs.
- **B — Compound natural key `(extractor_key, native_id)`.** No surrogate; identity is the extractor's own ID. Matches the V3 `(vid_id, playlist_id, extractor_id)` direction and the existing video endpoint. Cost: breaks for URL-less/LPM things, and a thing that changes extractor or native ID (rare but happens) loses identity.

**Claude Opus Recommendation:** **A (surrogate UUID, native ID as a unique secondary index).** It is what "stable internal ID" and the "lmpl id" TODO are both asking for, and it is the only option compatible with A6 (URL-less things). Carry the old `playlist_id` as a drop-after-migration lookup column.

**Architect Decision:** agree, A

---

### A2. Display metadata (title, channel, thumbnail) — columns on `thing` vs. only in `run.data_json` `[surfaced]`

The `thing` schema (NOTES p119) has **no title/channel/thumbnail columns**. WORK §1 maps `PlaylistSum.title → "metadata on thing **OR** in run.data_json"` — explicitly undecided. But every list view in the GUI (main job list, playlist page, video tiles) needs titles and channels for things that may have no recent successful `run` (e.g., a video discovered via fan-out but never individually pulled). Issue #147 ("allow playlist title to be updated if NULL") only makes sense if title is a stored column.

**Options:**
- **A — Denormalize a small set of display columns onto `thing`** (`title`, `channel`, `thumbnail_url`), updated opportunistically on each run. Fast list rendering, no JSON parsing, works for discovered-but-unpulled items. Cost: a little duplication with `run.data_json`; need an update rule (#147).
- **B — Keep display fields only in `run.data_json`** and derive on read. No duplication. Cost: list views must parse the latest run's JSON per row (N+1 / JSON-extraction cost — the very thing issue #123 is trying to kill), and discovered-but-never-run things have no title at all.

**Claude Opus Recommendation:** **A (denormalize a few display columns).** The fan-out model guarantees many things exist before they're ever individually run; they still need a display title, and Option B reintroduces exactly the N+1/JSON-extraction cost #123 is trying to kill.

**Architect Decision:** I'm leaning towards agreeing on A but we need to nail down which fields are apporpriate at implementation time. If it adds too much complexity I am leaving the door to B open. There may be ways to optimize in DB (as has already been done in objectindex project for frequently accessed JSONB fields)

---

### A3. Explicit `status`/lifecycle field vs. fully derived state `[surfaced]` `[conflict]`

The entity sketch (NOTES p108) gives `video.status` an enum: `gotten / yes→downloaded / NO / deleted`. The lifecycle (p105) has six stages (0–5) including stage 5 = deleted. But the actual `thing` schema (NOTES p119, DS) has **no status column** — state is meant to be inferred from `try_on`, `last_success_dt`, `last_failure_dt`, and `best_oi`. Meanwhile issue #36's deletion policy and #111 (OI scrub) reference a `deleted=True` state, and the OI scrubber needs to know "we used to have this and it's gone." Nowhere does the schema say how "downloaded," "deleted," or "decided-no" are represented.

**Options:**
- **A — Add an explicit `status` enum** to `thing` (e.g., `known / wanted / acquired / deleted / rejected`). Makes the lifecycle queryable directly, matches p108. Cost: must keep status consistent with the derived signals; the Feb 12 session deliberately moved *away* from a state machine.
- **B — Keep state fully derived** (no status column): "acquired" = `best_oi IS NOT NULL`; "deleted" = `best_oi NULL` + a prior successful run; "rejected/never" = `try_on IS NULL` + no success. Honors the "no state machine" decision. Cost: "deleted-but-was-acquired" is hard to distinguish from "never acquired" without joining `run` history; the scrubber logic gets subtle (see also A13).
- **C — Minimal middle ground:** no general status enum, but one explicit `deleted_dt` (or boolean) so the scrubber and "redownload deleted" (#70/#111) have a clean signal, everything else derived.

**Claude Opus Recommendation:** **C (no general status enum, but one explicit `deleted_dt`).** Keeps the no-state-machine spirit the Feb 12 session chose, while giving the OI scrubber and "redownload deleted" (#70/#111) a clean signal instead of forcing them to reverse-engineer deletion from `best_oi` + run history. Resolve C2 at the same time.

**Architect Decision:** B, we don't really care if it was deleted vs never acquired. scrubber has to pull everything anyway. redownloading is more for stuff that was NOT in LMv4 to begin with.

---

### A4. `rel.order` — store approximate order (Option A) vs. derive on demand (Option B) `[flagged]`

Issue #128; DS open question #3. Order was dropped in V3; fan-out makes it tractable again.

**Options:**
- **A — Store in `rel.order`** with a sparse integer scheme (negative-to-positive so items prepend/append without renumbering); record only first occurrence of a duplicate. Persistent; survives without a fresh metadata pull. Cost: drifts as fan-out discovers items, needs maintenance on every update, never exactly right.
- **B — Derive from the most recent `run.data_json`.** `rel` holds no position. Always ground-truth. Cost: no order when there's no recent successful pull; depends on raw metadata being stored and queryable (which we need anyway).

Note: the schema sketch (NOTES p120) *includes* a `rel.order` column, weakly signaling Option A — but the author's transcript trails off without deciding.

**Claude Opus Recommendation:** Genuine judgment call, but I lean **B (derive on demand)**. Display order tolerates being approximate or briefly absent; Option A signs you up for perpetual drift-maintenance on every fan-out update. Storing nothing and reading order from the latest `run.data_json` is strictly simpler. Caveat: the author's own transcript ("it's a property of the relationship") leans A, so this is the one to confirm with them directly — and it only changes if a future feature needs durable *user-defined* ordering.

**Architect Decision:** agree, B (we don't usually care much about order anyway)

**Addendum (finalization) — `rel` shape notes:** (1) **`rel.type` is plain `text`** (not a DB enum), so 4.x can add a **`same`** edge — linking two `thing`s that are the *same media* when URL canonicalization (`[A8]`) can't merge them (two distinct URLs resolving to one video) — with no migration. (2) **`rel` has *no* `created_dt`** — deliberately dropped: a per-edge "first seen / recently added to this playlist" timestamp is used by no 4.0 feature, and if a 4.x view ever wants it, it's an additive nullable column then. (Contrast `thing.created_dt`, which *is* kept — it backs the 4.0 "new things" dashboard.) (3) Relatedly, **`thing.type` is also plain `text`**, so a 4.x kind like **`photo`** needs no migration either.

---

### A5. `best_oi` pointer on `thing` — denormalize vs. always query OI `[flagged]`

NOTES p119 marks `best_oi` with a literal "?". Related: issues #69, #104, #123 ("return best video from OI if multiple").

**Options:**
- **A — Denormalize `best_oi` onto `thing`.** Eliminates the per-video OI round-trips that issue #123 / tech-debt item #4 are about; fast list/playlist rendering. Cost: must be kept in sync as copies are added/scrubbed/deleted (couples to the OI scrubber).
- **B — Always derive from OI at read time.** Simpler schema, always correct. Cost: the N+1 OI call pattern (#123) persists unless separately batched/cached.

**Claude Opus Recommendation:** **A (denormalize `best_oi`)** — it eliminates the #123 per-video OI round-trips and resolves the "if multiple, find the best" checklist item in one move. Condition: the OI scrubber must own keeping it fresh (so this rides on having a scrubber at all).

**Architect Decision:** I am tempted to agree on A but not following the scrubber dependency. Can't the worker just assign it on successful download? OI is not expected to just change on its own.  I don't see the OI scrubber as a v4.0 MVP depenency, but 4.x enhancement to allow enforcing deletions (for items that get rated D or F after an initial A or B) or replication (for items rated A)

**Addendum (finalization) — `best_oi` is never cleared on delete (amends B1/A3 deletion semantics):** On deletion — which is a **4.x scrubber** action; **4.0 deletes nothing** (see B1 addendum) — dropping media in OI leaves a persistent **tombstone**, and `best_oi` is **repointed to that tombstone, not cleared**. The tombstone is OI's durable record of *intentional* deletion. Consequences: (1) the derived-state model refines to *never acquired* = `best_oi IS NULL` (no OI object at all), vs *acquired-at-least-once* = `best_oi IS NOT NULL` pointing at either an active media object **or** a tombstone — OI is the source of truth for which, so still no `status` column in LM. (2) The 4.0 worker's download predicate `best_oi IS NULL` therefore selects only never-acquired things; **re-acquiring a deleted-then-re-wanted item is an out-of-scope 4.x corner case handled in the rating-change path, not by a scrubber** — on a rating raised back to eligible, check the single `best_oi` object: still-active → no-op (we still have it); tombstone → null `best_oi` + null `last_success_dt` + `try_on = today`, after which the normal worker flow redownloads it organically like a first acquisition (#70). (3) F-grade minimal identity additionally retains `best_oi`→tombstone. This keeps A5-A's "worker maintains `best_oi`, no scrubber dependency in 4.0" intact (the deletion path repoints it).

---

### A6. LPM / URL-less things — make `thing.url` nullable now vs. defer to V5 `[flagged]`

Issue #136 ("Is a Library or Person a URL-less Thing?"). LPM is a V5 integration, but whether `thing.url` is nullable is a V4 *schema* decision with high blast radius if changed later. Tightly coupled to A1 (a natural key on `url`/native-id forecloses URL-less things; a surrogate UUID PK keeps the door open).

**Options:**
- **A — Make `url` nullable in the V4 schema now**, even though no V4 feature creates URL-less things. Cheap insurance against a painful migration. Requires surrogate-UUID identity (A1-A).
- **B — Require `url` NOT NULL for V4**, accept a migration when LPM lands in V5. Simpler invariants now.

**Claude Opus Recommendation:** **A (make `url` nullable now).** It costs nothing in V4, removes a future forced migration when LPM lands, and is the natural partner to A1-A. Decide jointly with A1.

**Architect Decision:** agree, A

---

### A7. Non-yt-dlp backends — provision in the schema now vs. yt-dlp-only `[surfaced]`

V2 carried scaffolding for multiple acquisition backends; V3 deliberately pivoted to **100% yt-dlp**. Question raised during finalization: should the V4 schema reserve room for non-yt-dlp backends (with actual implementation left for 4.x), given the "freeze the schema in 4.0, no 4.x migrations" constraint? Adding a dispatch/identity column later means backfilling every row and branching worker logic mid-stream — exactly the kind of change the freeze rule wants to avoid. A related need surfaced: **rate limiting is per network host**, which the yt-dlp `extractor_key` does not always identify 1:1 (a generic extractor spans hosts; several extractors can share one CDN).

**Options:**
- **A — Add a `backend` column (dispatch + identity) now**, plus an explicit `site` column (rate-limit bucket). Implementation of any second backend is 4.x; 4.0 only ever uses yt-dlp. Cheap, migration-free insurance.
- **B — Stay yt-dlp-only in the schema**, accept a column-add migration if a second backend is ever needed. Simpler now; violates the no-4.x-migration goal if it ever lands.

**Claude Opus Recommendation:** **A.** `backend` is per-row dispatch + identity data the worker needs before it can act, and it's painful to add later — textbook case for the freeze rule. Keep `site` as a *distinct* column for the rate-limit bucket (it carries host info `extractor_key` doesn't) and the future per-site config join key, but keep it *out* of the identity key. Net 4.0 cost is ~zero (one backend, one dispatch branch).

**Architect Decision:** A. Make `backend` a `smallint NOT NULL DEFAULT 0` integer code — `0` = yt-dlp (also the default); future backends get positive ints (a `smallint`/2-byte field is ample for the <100 backends ever expected; Postgres has no native 1-byte int). Integer (not text) keeps the re-acquisition unique index `(backend, extractor_key, native_id)` plain — no `NULL`-distinctness pitfall, no `COALESCE`. Code→name mapping is an app-side constant / a future per-site config column. Also add explicit nullable `site text` (handy for rate limiting; join key to the future per-site table). Implementing an actual second backend is 4.x.

---

### A8. Unique constraint on `url` `[surfaced]`

The re-acquisition guard `UNIQUE (backend, extractor_key, native_id)` is `WHERE native_id IS NOT NULL`, so it offers **no protection during the pre-extraction / paste-time window** — between "user pastes a URL" and "extraction fills in `extractor_key`/`native_id`," two rows for the same URL can both be created. Question raised: add a second unique constraint on `url` where present?

**Options:**
- **A — Add `UNIQUE (url) WHERE url IS NOT NULL`.** Closes the stub/paste-time dedup gap; also makes re-discovery of an F-suppressed thing (whose `url` is retained) match the existing row instead of minting a new one. False-rejection risk ~nil (only byte-identical strings collide, and two distinct things never share an exact URL). Caveat: only as effective as URL canonicalization — variants (`&list=`, short links, trailing slash) are different strings that slip past and only collide later at the native-key index.
- **B — Rely on the native-key index only.** No early guard; duplicate stubs possible until extraction resolves identity.

**Claude Opus Recommendation:** **A**, global (not per-backend) — one canonical URL = one resource. Cheap, near-zero downside, closes the early gap. Pair with an implementor note to canonicalize URLs on write.

**Architect Decision:** A — add `UNIQUE (url) WHERE url IS NOT NULL`. Canonicalizing URLs on write (so the index dedups variants, not just exact strings) is a **4.1** improvement (schema-free, pure write-path logic); 4.0 ships the constraint and stores URLs as-given.

---

### A9. Which `run`/`thing` fields earn first-class columns vs. `data_json` `[surfaced]`

V3's `PlaylistStats` carried a pile of per-run fields (`entries_hash`, `modified_date`, `playlist_count`, `newest_item`, `download_count`, `failed_count`, `different`). The default V4 stance is to absorb them into `run.data_json` and only promote a field to a real column when it is query-hot / fixed-shape / drives scheduling, selection, or change-detection (same bar that promoted `best_oi` and the display columns). Decision: which to promote.

**Resolution (decided during finalization):**
- **`run.entries_hash` → column, `bytea`, nullable.** Membership fingerprint of a playlist run; the **change-detection key**. "Did this run differ?" = compare to the most recent prior *successful* run for the same `thing_id` (cheap via the existing `run_thing (thing_id, starttime DESC)` index — no new index). Drives the Fibonacci backoff and reuses V3 `pl_hash`/`compare_pl_runs`. `bytea` (not text) because its purpose is comparison, not display, and the V3 code already deals in raw bytes. NULL for single-video runs.
- **`run.playlist_count` → column, `integer`, nullable.** Entry count the playlist reports; backs partial-resume progress (§C5/§4.6 "got item X of N") and a sanity cross-check.
- **`thing.modified` → column, `timestamp` (naive UTC), nullable** (denormalized `[A2-A]`-style, refreshed each run). A *single* content-timestamp with type-specific meaning: **playlist** = derived from member items when determinable, else NULL; **video** = site-reported modified/upload time when available, else NULL. For freshness / "what's new" display and sorting — **not** a change-detection key. Explicitly nullable (V3's `141.sql` bug came from treating a modified-date as non-nullable).
- **Dropped (kept out of the schema):** `newest_item` (folded into the single `thing.modified`); `modified_date` as a called-out field (subsumed by `thing.modified`; the raw value still rides in `data_json`'s yt-dlp output); `download_count`/`failed_count` (vestigial V3 coupling — a download-free Stage 1 run is always 0, and a Stage 2 run is one video whose outcome is the `success` column); the derived `different` flag (falls out of the `entries_hash` comparison, not persisted).
- **Net:** `data_json` holds only the raw yt-dlp output. Final `run` = `id, thing_id, worker, input_json, data_json, entries_hash, playlist_count, starttime, endtime, success`.

**Architect Decision:** as above — `entries_hash` (bytea) + `playlist_count` promoted on `run`; a single `thing.modified` (playlist-from-items / video-site-reported, nullable); `newest_item`, `modified_date`-as-column, `download_count`, `failed_count`, and `different` all dropped; `data_json` = raw yt-dlp output only.

**Addendum (Task 0.1 implementation) — naive UTC datetimes everywhere:** all datetime columns are **`timestamp` (without time zone), interpreted as UTC**, not `timestamptz` — `thing.modified`, `thing.last_success_dt`, `thing.last_failure_dt`, `thing.created_dt`, `run.starttime`, `run.endtime`. The app works in UTC throughout (matching V3's existing naive `datetime.now()`), keeping timezone handling out of the data layer. Date defaults use the UTC date/time: `try_on DEFAULT (now() at time zone 'utc')::date`, `created_dt DEFAULT (now() at time zone 'utc')`. Canonical DDL: `lmdb/schema/v4.0.sql`.

---

## B. Rating System

### B1. Rating scale — are D and F human-ratable grades, and what are their deletion semantics? `[conflict]` `[flagged]`

Three documents describe three different scales:
- DS §3: human ratings are **A / B / C / null**; "D-rated or null → eligible for deletion." No F.
- NOTES p119/p412: signed integer **−2..+2** → **F=−2, D=−1, C=0, B=+1, A=+2**; F is a distinct grade.
- Issue #36 (post-Feb-12 comment): A/B/C/**D/F** with copy/purge policy — **D** = 0 copies, retain metadata(?); **F** = 0 copies, **remove metadata + purge archives**.

**Open points to resolve:**
1. Is **D** a thing a *user* can assign, or just the bottom of the machine scale / "below C"?
2. Is the D-vs-F distinction **soft delete** (D: drop media, keep metadata so we don't re-discover/re-download) vs **hard delete** (F: purge metadata and the yt-dlp archive entry so it can be re-found fresh)?
3. Copy policy reconciliation: DS says A=2 / B,C=1 / D,null=delete-eligible; #36 says A=2 / B=1–2 / C=0–1 / D=0 / F=0+purge. These disagree on B and C copy counts.

**Claude Opus Recommendation:** Adopt the **−2..+2 scale** (most recent, makes propagation arithmetic clean), make **D and F user-assignable**, and define **D = soft delete (drop media, retain metadata so we don't re-discover/re-download)** vs **F = hard delete (purge metadata + yt-dlp archive entry)**. Ratify the #36 copy table (A=2, B=1, C=0–1, D=0, F=0+purge) over DS's looser version, since #36 is the most specific and most recent.

**Architect Decision:** yes, adopt -2.0..+2.0 scale, and yes (1) D and F are both user-assignable, and yes (3) do the #36 copy ranges.  however for (2) yes we do want to drop active media but keep all metadata for D, for F we want to keep metatada needed only to ensure we don't get it again (so THING row, the F rating, and whatever info needed to uniquely identify it (like URL), but that's it. no archive etc.

**Addendum (finalization) — default rating when a human adds a thing:** The primary human entry point is **add-a-thing-by-URL**: user supplies a URL (optionally a grade), the URL-classify endpoint sets `type`, and the `thing` is created with `try_on = today` (the creation default, see C2 addendum) and a **default `human_rating = +1` (B)** — overridable to **A (+2)** or **C (0)** at add time. D/F are *not* add-time options (you don't add something to suppress it). Rationale: the user explicitly wants what they add, so B ("acquire") is the sensible default, and propagation does the rest — a B playlist's members machine-rate to B and download, C tracks-without-acquiring, A adds replication. This is the V4 form of V3's `POST /schedules/` (minus `freq_days`).

**Addendum (finalization) — 4.0 does NO deletion; both deletion effects are 4.x:** Rating a thing **D** or **F** in 4.0 only *records the rating*. Its sole 4.0 effect is that the rating gate (B2) excludes the thing from future acquisition — **no media is dropped, no metadata is purged, `best_oi` is untouched, no tombstone exists.** The actual deletion is two **4.x** enhancements: **(1) OI scrubber** — for any human **D or F** thing, delete its media in OI and repoint `best_oi` to the tombstone (this is the *only* place media deletion happens; see A5 addendum); **(2) F-only metadata purge** — for **F** things, strip as much metadata as possible (`run` rows / `data_json`, `rel` edges, denormalized columns), leaving only the `thing` row with its `url`/identifier, keys, and `F` rating. So the earlier "4.0 records the rating and drops media on D/F" framing is superseded: 4.0 drops nothing.

---

### B2. Worker selection predicate is wrong for the signed scale `[surfaced]`

The example worker query (DS §2, repeated in WORK §4) filters:
```sql
WHERE (human_rating IS NOT NULL OR machine_rating > 0)
```
That predicate was written when ratings were `A/B/C/null`. Under the −2..+2 scale it **misbehaves**: a human-rated **F (−2)** or **D (−1)** — items we want to delete or never fetch — satisfies `human_rating IS NOT NULL` and gets **selected for running**. Likewise `machine_rating > 0` silently excludes C-machine-rated (0) items that the run-order (p112) does want to *metadata-pull*.

**Options:**
- **A — Redefine the predicate against the numeric scale**, e.g. select where `GREATEST(COALESCE(human_rating, machine_rating), machine_rating) >= threshold`, with a different threshold for metadata-pull (≥ C/0) vs. download (≥ B/+1), matching the p112 run order.
- **B — Keep human-rating as authoritative override but exclude negatives explicitly:** `WHERE COALESCE(human_rating, machine_rating) >= 0` for playlists, `>= 1` for video downloads.

This is less "pick an option" and more "the canonical query in the design docs is a bug to fix before it's copied into code." Decide the exact predicate so the finalized doc ships a correct one.

**Claude Opus Recommendation:** **A** — one numeric threshold per job type: metadata-pull selects `COALESCE(human_rating, machine_rating) >= 0` (C and up), media-download selects `>= 1` (B and up), negatives are never acted on. This is the minimal correct restatement and it directly encodes the p112 run order. The finalized doc should ship this, not the buggy original.

**Architect Decision:** let's do B - human always has final say (and in fact if we do store machine ratings at all, the machine rating of a thing should be set to NULL anyway as irrelevant if it has a human rating)

**Addendum (finalization):** the thresholds (playlist ≥ C, video ≥ B) are applied to the **rounded grade**, not the bare float — see B3's grade-band addendum. On the raw float that is `r ≥ −0.5` (playlist) and `r ≥ 0.5` (video).

---

### B3. Machine-rating algorithm — weighting, hop count, staleness, numeric type `[flagged]`

DS open question #7; NOTES p109/p122. "Weighted average of human-rated relatives." Underspecified:
- **Weighting:** plain average of parent playlists? Weighted by playlist rating? Does a single A-parent dominate (override semantics from p111 say "found in a higher-rated playlist bumps it up" — that's a MAX, not an average)? Average vs. max is a real semantic fork.
- **Hops:** one hop (direct parents only) or transitive (channel → playlist → video)?
- **Staleness:** when is a stored machine rating recomputed (tied to B5/B6)?
- **Numeric type:** `machine_rating` is declared **integer** −2..+2, but an average of integers is fractional (avg(A=+2, C=0) = +1.0 ok; avg(+2,+1) = +1.5). Store as `numeric`/float, or define rounding?

**Claude Opus Recommendation:** Define machine rating as **max of the human ratings of direct parents** (this matches the p111 "bumped up if found in a higher-rated playlist" override semantics, which is a MAX, not an average), falling back to the average only where no human signal exists. **One hop for V4.** Store as `numeric` to sidestep the integer-rounding ambiguity. (If B4 lands on compute-on-read, this becomes the definition of the view.)

**Architect Decision:** numeric type should be float. (we probably need to adjust any thresholds as well - for instance if a THING is 1.99 , we should essentially consider that an A=2, not a B because it's technically < 2.0). handle staleness via B4+B5. the logic though may differ depending on the TYPE of THING.  For a video yes, let's say it's the MAX of any human-rated playlist it belongs to. For a playlist, however, it should be the average of all it's human-rated items. I think for now in v4.0 we keep it as one-hop but leave the door open to do more hops, where the wieghting may have to come in more.

**Addendum (finalization) — float→grade via round-to-nearest, formalizing the "1.99≈A" intuition:** Every place a (possibly averaged) rating is assessed for an acquire/delete decision, **round the float to the nearest integer grade and apply the integer grade as the threshold** — the integer grade values (A=2, B=1, C=0, D=−1, F=−2) are the **band centers**, ties round **up** (toward the more-positive grade). Equivalent half-unit bands: A `r≥1.5`, B `0.5≤r<1.5`, C `−0.5≤r<0.5`, D `−1.5≤r<−0.5`, F `r<−1.5`. This replaces the per-value epsilon idea. Consequence for B2's thresholds: "grade ≥ C" / "grade ≥ B" become, on the raw float, `r ≥ −0.5` / `r ≥ 0.5` (the round-direction-safe SQL form) — i.e. half a grade looser than the bare-integer cutoffs, by design. 

---

### B4. Machine rating — stored column vs. computed-on-read `[improvement]`

The design stores `machine_rating` as a column kept fresh by a twice-daily batch (B5) **plus** synchronous propagation on human-rating change **plus** an opportunistic upsert when a new `rel` edge connects a rated→unrated thing (NOTES p121). That's three mechanisms keeping one derived value in sync. The morning transcript (DS) even noted machine rating could be "computed on the fly."

**Improvement option:** since `machine_rating` is a pure function of the graph + human ratings, at Pi-5 / single-process scale it may be simpler to **compute it on read** (a SQL view or a query run when the worker selects work) and **not store it at all** — dissolving B5, B6, the staleness question in B3, and the three-way sync entirely.

**Options:**
- **A — Keep it stored** (current design): fast reads, but three sync paths to maintain and keep consistent.
- **B — Compute on read** (view/derived): one source of truth, no staleness, no batch job. Cost: multi-hop traversal on every selection query; need to confirm it's cheap enough at current scale (it almost certainly is for a personal Pi-scale dataset).
- **C — Hybrid:** materialized view refreshed on the same cadence as the batch — stored-read performance with single-definition logic.

**Claude Opus Recommendation:** **B (compute on read)**, or **C (materialized view)** if measurement ever shows read cost matters. The stored column is buying performance a single-process, Pi-scale workload doesn't need, while creating most of the rating-propagation complexity in the design (it dissolves B5, the staleness half of B3, and the three-way sync). This is the single biggest simplification opportunity in the corpus.

**Architect Decision:** Good suggestion. let's try B to implement without storing, but we'll need to think hard about how to make the worker query simple and fast without it. if it doesn't work out we do A (without batching). let's keep it as a nullable field in the db schema; it may come in handy later for more expensive (like ML-based) computation methods than our simple MVP MAX and AVERAGE.

---

### B5. Rating propagation cadence — is the twice-daily batch still needed? `[flagged]` `[improvement]`

DS open question #2. The design currently has propagation happening **three** ways (twice-daily full batch; synchronous on human-rating change; incremental on new `rel` edge). If B4 lands on compute-on-read, the batch disappears. If machine rating stays stored, the question is whether the twice-daily batch is redundant given the two event-driven paths already cover the cases that change ratings.

**Options:**
- **A — Keep the twice-daily batch** as a backstop for multi-hop cascades and missed events. Simple to reason about; bounded staleness.
- **B — Drop the batch, go fully event-driven** (propagate on human-rating change and on rel-edge creation only). Fewer moving parts. Cost: a multi-hop cascade (rating an A-channel that should lift grandchildren) needs the event handler to walk all affected descendants synchronously.
- **C — Moot** if B4 = compute-on-read.

**Claude Opus Recommendation:** Driven by B4. If machine rating becomes compute-on-read, this is **moot (C)**. If it stays stored, **A (keep the twice-daily batch as a backstop)** — fully event-driven propagation is easy to get subtly wrong on multi-hop cascades, and a full pass twice a day is cheap insurance at this scale.

**Architect Decision:** try C but if that doesn't work out do B (no batch) - i don't think the intention was ever to do both batch and event driven together, but we waffled between the two methods.

---

## C. Scheduling & Acquisition

### C1. `try_on` interval formula + the "first 3 runs" edge case `[flagged]`

DS open question #8; issue #149. The Fibonacci backoff (`FIB`, `rec_adjust_freq`, `next_fib` in `xform.py`) is reusable (WORK §2), but: (a) the initial interval per rating must be set (rough: A≈2–3d, B/C≈5d — DS and NOTES p209/p211 give slightly different numbers: DS says A~2/B~5; p209 says A~3/B~5/C~5 — ratify one table); (b) issue #149: with <3 runs the all-different trivial case steps the interval *down* on runs 1→2→3 even for static lists; the fix is to **hold the initial value for the first 3 runs, then adapt**.

**Decision needed:** ratify the initial-interval-by-rating table **and** adopt the #149 guard. (Edge-case fix is uncontroversial; mainly needs a yes.)

**Claude Opus Recommendation:** **Adopt the #149 guard** (hold the initial interval for the first 3 runs, then adapt) — uncontroversial. For the starting table, use the more conservative NOTES p209 values (A≈3d, B≈5d, C≈5d) rather than DS's aggressive A≈2d, to spare rate budget. Because Fibonacci convergence takes over quickly, the exact starting point matters little.

**Architect Decision:** the #149 guard was proposed to work around some of the ill-effects of bug #97.  The real fix for #97 is the fan out core to v4.  So we don't need the first 3 runs workaround/guard. 1st run is ASAP, 2nd run based on the table, and then subsequent runs backoffed (or brought in) naturally. initial #s should be A=3 B=5 C=8, but easily tunable constants. in the future (4.x) maybe this could be adjusted per-site.

---

### C2. `try_on = NULL` is overloaded — "done/never re-fetch" vs. "give up/permanent-fail" `[surfaced]`

`try_on = NULL` is assigned in two semantically opposite situations:
- After a **successful video download**: NULL = "got it, never re-check" (DS §6, §3).
- After a **permanent failure / user perma-ack**: NULL = "dead, stop trying" (DS §6; #129).

Both collapse to the same value. A query like "show me permanently failed things" (the failures dashboard, NOTES p122) cannot distinguish a happily-completed video from a dead link by `try_on` alone — it must also consult `last_success_dt` vs `last_failure_dt`. This is subtle and easy to get wrong.

**Options:**
- **A — Leave NULL overloaded**, always disambiguate via `last_success_dt`/`last_failure_dt` (or `best_oi`) in queries. No schema change; relies on every query author remembering.
- **B — Distinguish the two states** explicitly (e.g., the `deleted_dt`/status from A3, or a `give_up` boolean), so "done-success" and "dead-fail" are not the same value.

**Claude Opus Recommendation:** **B, resolved jointly with A3.** Once the A3-C `deleted_dt` (and "acquired" = `best_oi IS NOT NULL`) exists, "done-success" and "dead-fail" are no longer the same value and the overloading stops being a trap. No separate fix needed beyond A3.

**Architect Decision:** let's do A. `try_on` is only really used by the worker which doesn't care about either successful stuff or permafail. the other use cases consult those other columns first anyway.

**Addendum (finalization) — eligibility + the compute-on-read interaction (touches B4, C1):** A thing runs only when `try_on <= today` **and** its effective rating clears the threshold; `try_on = NULL` is **never eligible** (this also fixes the worker predicate, which had wrongly included `try_on IS NULL` for videos). Problem: a **compute-on-read machine rating** (`[B4-B]`) can rise with *no write to the row* (e.g. a parent playlist is rated up), so there is no event at which to set `try_on` — the thing would never become eligible. **Fix:** `try_on` **defaults to `CURRENT_DATE` at thing creation** (DB `DEFAULT`), so the date-gate is open from birth and the *rating* gate alone governs eligibility until first run. This also subsumes the "1st run ASAP" rule in C1. Complement for the cases where `try_on` has since left today: on a **human** rating raise-to-eligible, set `try_on = CURRENT_DATE` **guarded by `best_oi IS NULL`** (resurrects a permafail / pulls a future-scheduled playlist forward; never disturbs an acquired thing). Machine ratings need no equivalent under compute-on-read; *if* the B4 fallback (stored, event-driven machine rating) is ever taken, that propagation code must do the same `best_oi IS NULL`-guarded `try_on = today` set.

---

### C3. `last_failure_dt` semantics — null-on-success vs. failure visibility `[conflict]` `[surfaced]`

Issue #129 says "on success set `last_failure = null`." But the whole point of `last_failure_dt` (DS, WORK §8) is failure **visibility** on the dashboard. If it's nulled on every success, an intermittently-failing thing looks perfectly healthy between failures, and you lose "fails 1 in 3 runs" signal. The full `run` table retains history regardless, so the question is what `thing.last_failure_dt` *means*.

**Options:**
- **A — `last_failure_dt` = "currently in a failed state" marker**, nulled on success (per #129). Easy "what's broken right now" query; loses intermittent-failure signal at the `thing` level (recover it from `run` history when needed).
- **B — `last_failure_dt` = "timestamp of the most recent failure ever"**, never nulled. Preserves the signal; "currently broken?" becomes `last_failure_dt > last_success_dt`.

**Claude Opus Recommendation:** **B (most-recent-failure-ever, never nulled).** Strictly more information than the #129 null-on-success behavior, and "currently broken?" is the trivial comparison `last_failure_dt > last_success_dt`. Preserves the intermittent-failure signal the dashboard needs.

**Architect Decision:** let's do A.  intermittent failures likely indicate a site/rate-limit issue, not an issue with a given THING.  v4.x (not v4.0 MVP) should have an output of recent failures (basd on runs table) on a per-site basis so admin can investigate

---

### C4. Phase I extraction depth vs. the "rate before download" requirement `[surfaced]` `[improvement]`

WORK §5 recommends Phase I (playlist/learn) use yt-dlp `extract_flat: True` / `simulate: True` to discover members cheaply without downloading. But the rating model (NOTES p114 "2× human rating: rate on **metadata** before downloading"; p105 stage 2 "we know about it" → stage 3 "we decide we want it") needs **per-video metadata** (title, description, duration) to make the download decision. `extract_flat` returns only minimal fields (id, url, sometimes title) — **not** enough to decide on. There's a real gap between "cheap flat discovery" and "rich enough to decide."

**Options:**
- **A — Two acquisition depths, mapped to lifecycle stages:** Phase I = flat discovery (cheap, creates `thing` rows at stage 2); a distinct **metadata-pull stage** (stage 2→3, per-video `info.json` without media) feeds the decision; Phase III = media download. Cleanly matches the p105 three-phase model. Cost: an extra pass and an extra job type.
- **B — Phase I pulls full per-video metadata** (no `extract_flat`), accept the heavier playlist pull. Simpler job topology; defeats some fan-out cost savings on huge playlists (#83).
- **C — Flat discovery + decide from playlist-level/contextual signals only** (site median, playlist rating) without per-video metadata until download. Cheapest; weakest decisions; the "rate on metadata" workflow becomes "rate on title only."

This is the crux of whether fan-out is two stages or three. **Claude Opus Recommendation:** **A (three depths: flat discovery → metadata pull → media download).** It's the model the p105 lifecycle already implies (stage 2 "know about it" = flat; stage 3 "decide" needs metadata; stage 4 "get it" = download), and it's the only option that makes "watch a playlist without get-all" (Goal III) *and* "rate on metadata before downloading" both work. The extra metadata pass is far cheaper than media and only runs for things that pass the first filter.

**Architect Decision:** fan-out should be 2 stages, so either B or C. Actual behavior may differ from site to site. We will need to test more at implementation time, and maybe have different behavior per-site (so more B-like for sites that don't include key per-video metadata in the playlist, and C-like for sites where the playlist does include actionable metadata)

**Addendum (finalization) — enrich inline, never a separate single-video-metadata job:** The hope is the **flat playlist pull already carries enough per-video metadata to make the acquire/skip decision** (C-like). Where it does not — *and we don't already have that video's metadata* — Stage 1 enriches it **inline, as part of the same playlist-learn process** (B-like), rather than triggering a separate single-video-metadata job. There is deliberately **no** standalone metadata-pull stage or per-video metadata job type (that is the rejected three-stage model). Whether to enrich and how deep is per-site and left to the implementor based on live testing.

---

### C5. Partial playlist resume mechanism `[flagged]` `[improvement]`

DS open question #4; issues #83, #97. How does a runner know what it already retrieved when a pull failed midway?

**Options:**
- **A — Parse `run` history** to reconstruct what pages/items were fetched, resume from there. General but fiddly.
- **B — Lean on the existing OI-backed download archive** (`ObjIdxDlArch`, WORK §5) plus **`thing` existence** as the resume oracle: anything already a `thing` / already in the archive is skipped; the runner just re-pulls and naturally no-ops on knowns. Simpler, reuses a completed V3 mechanism, and likely dissolves #97 ("entries not cataloged") as a side effect.
- **C — yt-dlp `--lazy-playlist` / "get until item X then stop"** (#83's framing) for huge playlists, combined with B.

**Claude Opus Recommendation:** **B, plus C for huge lists.** The OI-backed download archive + the `thing` table already *are* the "what do we have" record; reconstructing that from run JSON (Option A) is redundant work and likely resolves #97 as a side effect. Add the "get until item X" cap (C) only where playlist size demands it (#83).

**Architect Decision:** sorry, B is not an option in v4. ObjIdxDlArch only gets populated when a video gets fully downloaded. this would be fine for v3 (where we attempt to dl all videos) but a central point of v4 is to allow decisions to be made around whether to download a video. Things that are not downloaded are never in the archive. we probably need to do some combination of A+C but details/testing left to implementation phase.

**Addendum (finalization) — partial resume is 4.x; 4.0 just prepares for it:** Given 4.0 fails a playlist whole on any failure (see C8) and re-pulls it whole on the next `try_on`, **partial resume itself moves to 4.x** — the A+C combination (run-history parse + yt-dlp "get until item X then stop" cap, for huge playlists #83) is a 4.x optimization, not 4.0. 4.0's one preparation is **deterministic ordering: drop V3's `playlistrandom: True` (`run_bknd.py:44`)** and process entries in natural order — you cannot lazy-load or resume against a list that is reshuffled every run, so turning randomization off in 4.0 is the prerequisite for the 4.x work.

---

### C6. "Capacity" is referenced everywhere but never defined `[surfaced]`

Multiple decisions lean on a notion of **capacity** that has no mechanism: B = "get if capacity allows" (NOTES p111), "B videos — *if time*" (p112), video decision factor "capacity = bandwidth/storage/rate-limit budget" (p110). Nothing in the schema or runner design measures or enforces capacity. Without a definition, the run-order's "if time" steps and the B-rating semantics are unimplementable.

**Options:**
- **A — Time-boxed run:** capacity = wall-clock budget per daily run; lower-priority tiers run only if time remains. Simple, matches "if time" literally. Doesn't model storage/bandwidth.
- **B — Quota-based:** explicit per-run caps (max N downloads, max GB, per-extractor request budget — ties to #127). More precise; more config.
- **C — Drop "capacity" as a formal concept for V4 MVP:** just run the priority-ordered query to exhaustion each cycle; revisit if the Pi actually saturates. Honors "single-process, current scale."

**Claude Opus Recommendation:** **C for V4 MVP** (run the priority-ordered query to exhaustion; don't formalize capacity), promoting to **A (wall-clock time box)** the moment the daily run actually starts overrunning. Full quota-based **B** belongs in v4.2 alongside #127. Whatever is chosen, the finalized doc must define "capacity" concretely so the p112 "if time" steps are implementable.

**Architect Decision:** agree, C for MVP followed by A and then B as needed.

---

### C7. Daily run-order sequence vs. pull-based ORDER BY `[surfaced]`

NOTES p112 prescribes a strict global sequence (A playlists → A videos → B playlists → C playlists → B videos if time). The architecture (DS §2, NOTES p113) is "workers **pull** work by querying live with an ORDER BY." A strict global "do all of X before any of Y, and Y only if time" is hard to express when each worker independently queries — it implies a global phase barrier and a capacity check (C6).

**Options:**
- **A — Soft priority via `ORDER BY`** in the pull query (rating DESC, try_on ASC); no hard phases. Naturally fits the pull model; "B videos last" falls out of ordering. Loses the strict "never start B until all A done" guarantee.
- **B — Hard phased pipeline:** the jobs engine runs discrete passes in p112 order with a barrier between them. Matches p112 literally; reintroduces orchestration the Feb 12 session tried to avoid.
- **C — Soft order + a single capacity gate** (C6-A time box) that stops lower tiers — approximates p112 without hard barriers.

**Claude Opus Recommendation:** **C (soft `ORDER BY` priority + a single capacity gate).** Keep the pull-query ordering — "B videos last" falls out of it naturally — and add only the C6 time/quota gate so the lowest tiers defer. Avoids reintroducing the hard orchestration/barriers the Feb 12 session deliberately removed, while honoring the spirit of the p112 sequence.

**Architect Decision:** I think A is OK but need to also incorporate the playlist/video distinction somehow, else fallback on B.

---

### C8. Failure handling — when to give up on a run / a site `[surfaced]`

V3 set `skip_playlist_after_errors=3` (`run_bknd.py:43`): tolerate up to 3 errors in a playlist run before bailing. That made sense in V3, where a "playlist run" *downloaded all the videos*, so most errors were isolated per-video download failures. In V4 a Stage 1 playlist run is **metadata-only** — it downloads nothing — so the calculus changes: what should make a run fail, and how do we avoid hammering a rate-limited site?

**Resolution (decided during finalization):**
- **4.0 — fail the whole playlist on *any* failure.** Because a metadata-only pull touches no individual videos, a failure is almost certainly a whole-playlist problem (site down, rate-limited, auth/cookies, broken extractor), not one bad entry. So fail fast: `skip_playlist_after_errors → 1` (abort the run on the first error), record the failure, and let the `try_on` backoff (C1) retry the whole playlist later. No partial resume in 4.0 (see C5).
- **4.x — per-`site`, per-day failure limit (≈3).** To avoid banging our head against a rate limit, a per-site/per-day failure counter (refines #127): once a site accrues ~3 failures in a day, stop dispatching jobs for that site until the next day. Supersedes V3's per-run consecutive-error tolerance and the original #127 "per-session" framing; pairs with worker self-selection (D2 addendum). The `site` column (A7) is the key.

**Architect Decision:** as above — 4.0 fails the whole playlist on any failure (metadata-only runs ⇒ failures are whole-playlist, not per-video); 4.x adds a per-site/per-day (~3) failure limit to back off from rate-limited sites.

---

## D. Concurrency & Process Model

### D1. Worker coordination — accept collisions vs. `SELECT … FOR UPDATE SKIP LOCKED` `[flagged]`

DS open question #1; ISSUES decision #4; issue #142 (a real race in `POST /playlists/`). V4 is single-process by default (#27 descoped to V5), but #142 shows concurrent requests already collide.

**Options:**
- **A — Accept collisions at current scale** (the Feb 12 default). Zero added complexity; two workers occasionally do the same pull (mostly harmless, wastes a little rate budget). Doesn't fix the #142 POST race.
- **B — `SELECT … FOR UPDATE SKIP LOCKED`** on the work-claim query (Postgres native). Cheap, idiomatic, makes the design forward-compatible with the V5 distributed goal, and addresses #142 at the DB level. Slightly more query care.

**Claude Opus Recommendation:** **B (`SELECT … FOR UPDATE SKIP LOCKED`).** It's a few words of SQL, Postgres is already the chosen store, it removes the #142 race rather than deferring it, and it makes the design forward-compatible with the V5 distributed goal at near-zero cost. "Accept collisions" buys nothing here.

**Architect Decision:** Agree, B - I am not familiar with this construct but I like it. Add a note on how this works to the implementor

---

### D2. "pl μsvc" / multiple microservices vs. single process with modules `[conflict]` `[surfaced]`

NOTES p113 describes a **"playlist microservice" (pl μsvc)** and separately a jobs engine, video downloader, and OI scrubber — language that implies several deployable services. But the resolved architecture decision (NOTES p114, DS) is **single process for V4**, and WORK treats everything as one FastAPI app (`lmdb`) plus the frontend (`lmfe`). "Microservice" and "single process" need reconciling so the finalized doc isn't ambiguous about deployment topology.

**Options:**
- **A — Single process, logical modules.** One deployable (plus `lmfe`); "pl μsvc," jobs engine, downloader, scrubber are modules/packages, not services. Matches the single-process decision and current code. Recommended for V4.
- **B — Actual separate services** (process/HTTP boundaries) now, to pre-stage V5 distribution. Contradicts the V4 single-process decision; premature.
- **C — Single process now, but draw clean internal module boundaries** along the future service seams so V5 can split them. Pragmatic middle path.

**Claude Opus Recommendation:** **A/C (single process, logical modules with clean seams along the future service boundaries).** "Microservice" in NOTES p113 is aspirational vocabulary, not a V4 commitment, and it directly contradicts the resolved single-process decision. Build modules now; let V5 split them at the seams if distribution ever materializes.

**Architect Decision:** Sort of a hybrid (but closest to option "B") - even existing work already has seperate `api.py` and `job_runner.py` processes. Question is more around whether we can support multiple simultaneous workers. Ideally yes but OK to defer that capability to v4.x.  What we do NOT need to do is break up playlist vs video - they are all just "Things"

**Addendum (finalization):** V4 *continues and improves* V3's existing **pull** model — `job_runner.py` already pulls (`GET /schedules/`), it just runs jobs in `random.shuffle` order with no priority. V4 change: **the API owns prioritization.** A dispatch endpoint runs the §B2/§C7 selection predicate (+ `FOR UPDATE SKIP LOCKED` per D1) and hands the runner the **single highest-priority due job**; the runner becomes a thin puller (ask → run → report → loop) and no longer decides order or queries `thing` directly. Two consequences: (1) the notes-p112 "playlist pass then video pass" phasing **evaporates** — one ordering (rating DESC, `try_on` ASC, playlist-before-video) spans both job types, so the top job is usually a due playlist when one exists, else a video; (2) schema provisions on `run` (frozen now): a nullable **`worker`** column to identify the runner instance, and **`success = NULL`** already encodes "assigned/in-progress" (the assigned-but-not-completed indicator). `starttime` stays **NOT NULL** = the assignment/start instant (worker starts immediately, so the two coincide; no separate "assigned-not-started" state). Creating the `run` at assignment, **worker self-selection of jobs**, and multi-worker coordination via `worker` are **4.x** — the schema supports them with no migration. *Worker self-selection:* a worker declares which job kinds it is willing to take — filtering by `type` (playlist vs video), `extractor_key`, `site`, or `backend` — and the dispatch endpoint returns only the highest-priority job matching that filter (useful for heterogeneous workers, e.g. one box holds a site's cookies/IP, or a worker is dedicated to video downloads vs playlist pulls). All filter dimensions are existing `thing` columns, so it is pure endpoint/runner logic.

*The "report" step — worker-owned unified metadata push (replaces the in-plugin POST):* after **every** run — playlist pull **or** video download — the **worker** posts results to the run-result endpoint (`POST /jobs/{run_id}/result`, the rewrite of V3's `POST /playlist-run`). This is one unified push path for both kinds of thing, owned by the worker, so the yt-dlp plugins need not make HTTP calls. Consequence for the plugin layer: V3's `LinkMeddlePlaylistPP` no longer POSTs (at most shapes output, or is dropped if the worker reads yt-dlp output directly); the OI upload PP may stay or be replaced by worker-side upload, but the worker does the metadata push either way (setting `best_oi`). Related helper dispositions: Crustula stays mostly as-is; the `ObjIdxDlArch` OI download archive is **redundant** in the fan-out model but kept as a belt-and-suspenders guard against double-downloads; verschiedenes extractors stay as yt-dlp extractors (no explicit LM dependency), with 4.x to evaluate any as a `backend` `[A7]`.

---

## E. Live Streams & External Systems

### E1. Handling currently-live content `[flagged]`

Issue #150 (no body); ISSUES decision #5. `_exclude_live` already filters live during download (WORK §5). Pervellam is the live-acquisition tool and is "fully parallel and independent for V4" (NOTES p110). But LM may still need to *know* about live state — e.g., not stamp a recurring live/premiere as `try_on = NULL` "never re-fetch."

**Options:**
- **A — LM ignores live entirely for V4**, delegates all live to Pervellam; keep `_exclude_live`. Simplest. Risk: a recurring live URL mishandled by the `try_on` logic.
- **B — Add `live` as a distinct `thing.type`** (or a flag) so the scheduler treats it specially (don't perma-null; back off and re-check). More correct; small schema addition.
- **C — Handle only the `try_on` edge case** (don't null `try_on` for things yt-dlp marks `is_live`/`was_live`) without a new type. Minimal.

**Claude Opus Recommendation:** **C for V4 MVP** (keep `_exclude_live`; just don't null `try_on` for things yt-dlp marks `is_live`/`was_live`, so recurring lives keep getting re-checked), promoting to **B (a `live` type/flag)** only if live turns out to need first-class scheduling. Full live *acquisition* stays with Pervellam either way.

**Architect Decision:** Agree, C - basically if something is currently love, try to pull it again tomorrow (at which point it won't be live anymore, but a recording). Pervellam is more for things that are live now and will NOT later have an ability to get as recorded.

**Addendum (finalization):** Even simpler than "C" implies — **no `is_live`-aware logic is needed.** A live-hit is just a **temporary download failure**; the normal failure backoff moves `try_on` to tomorrow, and since `try_on` is only nulled on *success*, it is never nulled for a live item anyway. Next run it is a finished recording and downloads in full. Pervellam still owns content that will never later exist as a recording.

---

### E2. Pervellam in the frontend — V4.2 display vs. V5 integration `[conflict]`

Issue #121 (Pervellam in FE) sits in **v4.2**; NOTES p110 says Pervellam integration is a **V5** consideration. ISSUES notes this is really two things: *displaying* Pervellam content in the FE (light, since both store into OI) vs. *sharing decision logic / control* (heavy, V5).

**Options:**
- **A — V4.2: read-only display** of Pervellam content in the FE (OI already shared); defer control/decision-sharing to V5. Resolves the apparent conflict by scope-splitting.
- **B — Keep all Pervellam work in V5**, per NOTES p110 literally.

**Claude Opus Recommendation:** **A (V4.2 read-only display, deeper integration to V5).** Resolves the apparent conflict by scope-splitting: since both systems already store into OI, *showing* Pervellam content is cheap and genuinely V4.2-shaped, while *sharing decision logic / control* is the heavier V5 work NOTES p110 means.

**Architect Decision:** agree, A

---

## F. Storage & Infrastructure Ratifications

These are largely **already decided in spirit** but never formally closed; the finalized doc should state them outright.

### F1. Raw yt-dlp metadata store — ratify PostgreSQL JSONB `[flagged]`

DS open question #6; issue #80. Long NoSQL detour (CouchDB/Mongo) concluded tentatively at **Postgres JSONB** to avoid another dependency. Just needs a formal "yes, JSONB in `run.data_json`, no separate document store." Confirm and close #80 / #69.

**Claude Opus Recommendation:** **Ratify Postgres JSONB; no separate document store.** Adding CouchDB/Mongo for one JSON column violates the "don't add another dependency" principle that drove the whole all-in-Postgres decision. Close #80/#69.

**Architect Decision:** I think this is correct. CouchDB can be considered if we run into issue with our pure-Postgres-approach.

---

### F2. `run.success` encoding — ratify T/F/NULL tristate `[conflict]`

DS used `success_if` (non-null = success); NOTES p120 (newer) uses a **boolean `success` with T/F/NULL** (null = in-progress). Minor; adopt the newer, more legible tristate.

**Claude Opus Recommendation:** **Adopt T/F/NULL.** Newer, more legible, and NULL cleanly serves as the `run.success = null` in-progress start marker the runner loop needs (NOTES p121, runner TODO in WORK §4).

**Architect Decision:** agree

---

### F3. V3→V4 data migration — migrate existing data vs. greenfield `[surfaced]`

WORK §1/§12 lays out detailed V3→V4 column mappings (insert `PlaylistSum` rows as `thing`, etc.) **and** notes the engine currently targets **SQLite** while V4 wants **PostgreSQL + JSONB** (WORK §3). Nobody states whether existing V3 data (real downloaded content, schedules, stats on the Pi) gets **migrated** into the new SQLite→Postgres `thing/rel/run` shape, or whether V4 starts **greenfield** and re-pulls. This matters: re-pulling everything wastes rate budget and may lose content that's since disappeared.

**Options:**
- **A — Migrate:** ETL existing SQLite `playlistsum/sched/vid/stats` into Postgres `thing/rel/run` (mappings already drafted in WORK). Preserves history and avoids re-pulling. Cost: one-time migration script + SQLite→Postgres move.
- **B — Greenfield:** stand up Postgres fresh, re-discover playlists from the schedule list, let fan-out repopulate. Simpler code; loses run history and risks losing vanished content not re-pullable.
- **C — Hybrid:** migrate `thing`/`rel` (identity + relationships + best_oi pointers so existing media stays linked) but not historical `run` rows.

**Claude Opus Recommendation:** **A or C (migrate, don't go greenfield).** The OI media already exists on the Pi; greenfield (B) would orphan it or force a wasteful re-pull, and some vanished content can't be re-pulled at all. The WORK §1 column mappings are already drafted. **C (migrate `thing`/`rel` + `best_oi`, skip historical `run` rows)** is the pragmatic cut if migrating full run history proves fiddly.

**Architect Decision:** disagree, B greenfield. we do not have any currently populated v3 instances to migrate from anyway.

---

### F4. SQLModel — keep, reduce, or replace for the V4 data layer `[flagged]`

NOTES pp.103–104 record SQLModel frustration; the author marked it an **out-of-scope implementation decision**. WORK §3 / tech-debt #5–6 surface concrete SQLModel gotchas (e.g., `is not None` vs `is_(None)` in selects silently failing) that the V4 query layer must avoid. Flagged here only so the architect can confirm it stays an implementation choice, not a design one.

**Options:** (A) keep SQLModel for Pydantic + SQLAlchemy as-is; (B) keep for validation, drop to plain SQLAlchemy for queries; (C) replace with Pydantic + SQLAlchemy. Author already leans "out of scope — decide at build time."

**Claude Opus Recommendation:** **B** — keep SQLModel for Pydantic-side validation (the DLP boundary models are solid and well-exercised), but write queries in explicit SQLAlchemy to dodge the `is_(None)`/select gotchas flagged in tech-debt #5. Agrees with the author that this is a build-time call, not a design blocker.

**Architect Decision:** I think we can keep SQLModel for now but note the gotchas so implementors are aware. Implementors can drop to plain SQLAlchemy if warranted.

---

## G. Cross-Cutting & Project Shape

### G1. Cross-metadata references (`thing` references a not-yet-known `thing`) `[flagged]`

Issue #137: "one metadata ref may be 'see item 42 from playlist ID x.'" The `rel` table models known relationships, but raw metadata can reference items not yet in `thing`. Decide whether V4 (a) ignores such references, (b) stores them as a deferred/pending `rel` to be resolved when the target is discovered, or (c) eagerly creates a stub `thing`. Likely defer to v4.2 unless it blocks fan-out.

**Claude Opus Recommendation:** **Defer the whole item to v4.2** (it doesn't block fan-out); when built, prefer **(b) a deferred/pending `rel`** resolved on discovery over eagerly creating stub `thing` rows, which would pollute the table with un-acquirable entries.

**Architect Decision:** The intent of #137 was around where everything we know about a video is actually from a playlist.  So more like we want to establish the existance of a video as a "thing" so a user or agent can rate it etc. This complex issue is probably the most compelling reason to choose apporach "B" for item A2.  It allows pulling that info from the playlist's run metadata into the video thing to avoid a complex "this video's metadata can be found at playlist x item y." By doing so, this issue is sidestepped.  To answer this question directly, playlist uncovers existance of a video we don't know about yet. We essentially do "c." The 'stub' 'thing' for the video gets created with whatever we know about it included (via A2-B fields). And the rel is created. Note this also touches C4. Indeed some situations may require a second pass after initial stub to get actionable info.

---

### G2. Fanout (v4) milestone scope is unrealistic for the window `[improvement]`

ISSUES flags **36 open issues** in the `fanout (v4)` milestone due **2026-07-03** (~3 weeks from this doc's date), *before* the recommended moves that pull **more** issues (#145, #127, #143) **into** it. ISSUES already contains a concrete close/move/clarify triage. Recommended action: **apply that triage to cut the MVP to a buildable core** before implementation starts, and explicitly move non-MVP items to v4.1/v4.2/v5. This isn't a design choice per se, but the finalized doc should reflect a realistic MVP boundary.

Suggested MVP-critical decisions that **must** be closed before coding (from the entries above): A1 (identity), A4 (`rel.order`, blocks `rel` schema), A5 (`best_oi`), A6 (nullable url), B1 (rating scale), B2 (selection predicate), F1 (JSONB), F3 (migration). Everything else can be sequenced after.

**Claude Opus Recommendation:** **Apply the ISSUES triage and cut the MVP hard** — to fan-out (Phases I–III), rating CRUD, the status dashboard, and the 8 schema-blocking decisions listed above. Pull #127/#145 in only if time allows; push everything else to v4.1+. 36 issues in a 3-week window is not achievable, and a ruthless cut now prevents a slipped, half-built milestone later.

**Architect Decision (MVP boundary / triage approval):** #127 and #143 can remain 4.x (not 4.0). For #145 while user certainly must have a way to rate in 4.0, the actual auto-presenting if items needing rating can move to v4.1. I went ahead and did close/move some issues. Now at 26, but am open to more recommendations to move stuff to 4.x.

**Addendum (finalization) — cleanup policy, don't bulk-close:** There is a new **`v1+v2 cleanup`** milestone. (a) **Old V1/V2-era issues** this design supersedes **move there**, rather than being closed (e.g. #84, #81, #82, #69, #8, #75/#22/#54/#32/#76/#65/#60/#3). (b) **Issues V4 actually tackles stay open until implemented** and are closed only when the corresponding subtask ships — e.g. #80 (Postgres-JSONB store → §0.1), #19 (refresh/retry → `try_on` scheduler, §1.4), #129, #115, #128. So the design doc's former "issues to close" list is now mostly "move to `v1+v2 cleanup`," and V4-relevant issues are *not* pre-emptively closed.

---

## Quick Index

| # | Title | Type | Blocks schema? |
|---|-------|------|----------------|
| A1 | `thing.id` UUID vs compound key | conflict | **yes** |
| A2 | Display metadata columns vs JSON | surfaced | yes |
| A3 | Explicit status vs derived state | surfaced/conflict | yes |
| A4 | `rel.order` store vs derive | flagged | **yes** |
| A5 | `best_oi` denormalize vs query | flagged | **yes** |
| A6 | LPM / nullable `url` | flagged | **yes** |
| A7 | Non-yt-dlp `backend` code + `site` column | surfaced | **yes** |
| A8 | Unique `url` index (paste-time dedup) | surfaced | **yes** |
| A9 | `run`/`thing` columns vs `data_json` (entries_hash, playlist_count, thing.modified) | surfaced | **yes** |
| B1 | Rating scale D/F + deletion policy | conflict | yes |
| B2 | Worker selection predicate bug | surfaced | no (query) |
| B3 | Machine-rating algorithm | flagged | maybe |
| B4 | Machine rating stored vs computed | improvement | maybe |
| B5 | Propagation cadence / drop batch | flagged/improvement | no |
| C1 | `try_on` formula + #149 guard | flagged | no |
| C2 | `try_on = NULL` overloaded | surfaced | maybe |
| C3 | `last_failure_dt` semantics | conflict | no |
| C4 | Phase I extraction depth (2 vs 3 stage) | surfaced/improvement | no |
| C5 | Partial playlist resume | flagged/improvement | no |
| C6 | "Capacity" undefined | surfaced | no |
| C7 | Run-order sequence vs ORDER BY | surfaced | no |
| C8 | Failure handling (fail-whole-playlist 4.0; per-site/day limit 4.x) | surfaced | no |
| D1 | Worker coordination / SKIP LOCKED | flagged | no |
| D2 | Microservice vs single process | conflict | no |
| E1 | Live stream handling | flagged | maybe |
| E2 | Pervellam in FE timing | conflict | no |
| F1 | Ratify JSONB metadata store | flagged | yes |
| F2 | `run.success` tristate | conflict | yes |
| F3 | V3→V4 data migration | surfaced | yes |
| F4 | SQLModel keep/drop | flagged | no |
| G1 | Cross-metadata references | flagged | no |
| G2 | Milestone scope / MVP triage | improvement | n/a |
