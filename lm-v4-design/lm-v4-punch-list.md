# LinkMeddle V4.0 — Implementation Punch List

Derived from `LM-V4-DESIGN.md` Part 5 (Implementation Subtasks). Check off in order —
the list is sorted along the critical path. Complexity: **S** (hours–day), **M** (a few days),
**L** (the hard, multi-day cores). Issue numbers link each task to the tracker.

> **Critical path:** 0.1 → 0.2 → 0.3 → 1.1 → 1.2 → 1.3 → 1.4 → 2.2 → 2.3 → 3.x
> **2.1 (rating CRUD) is pulled forward** to run alongside Phase 1 so the full
> ingest → human-rate → dispatch → download loop can be tested end-to-end *before* the
> perf-sensitive machine-rating core (2.2) lands.

## Phase 0 — Foundations (must precede everything)

- [x] **0.1** Create the `thing`/`rel`/`run` Postgres schema + indexes; switch `DATABASE_URL` to Postgres+JSONB — **M** — #129 #80 #128 *(close #80/#128 when this lands)*
- [ ] **0.2** Port DLP boundary models + reusable `xform` helpers (Fibonacci, `compare_pl_runs`, `pl_hash`, `pl_dlp2lm`) onto the new layer — **M** — #151
- [ ] **0.3** `thing`/`run` CRUD API + URL-classify; add-a-thing-by-URL (default B/`+1`, override A/C, `try_on=today`); `?url=` lookup; `apply_update`/PATCH — **M** — #140 #142 #147 #102

## Phase 1 — Fan-out core (the heart of V4) — tracker: #81

- [ ] **1.1** Stage-1 ingest endpoint + worker metadata push (replaces the PP POST); upsert `thing`+`rel`+`run`; stub videos w/ denormalized fields; per-site depth flag — **L** — #97 #110 #137 #83
- [ ] **1.2** Prioritized dispatch + thin runner: job-dispatch endpoint (§4.2 predicate + `FOR UPDATE SKIP LOCKED`, single top job); adapt `job_runner.py` to pull one job and loop; `run.success=NULL` in-progress marker — **L** — #115 #19 *(close #19 when this lands)*
- [ ] **1.3** Stage-2 downloader: per-video path (`init_download(maybe_playlist=False)`; OI upload); worker push sets `best_oi`, `try_on=NULL` — **M** — #115
- [ ] **1.4** `try_on` scheduler integration: Fibonacci + initial A3/B5/C8; failure backoff; live re-check edge case — **M** — *(#149 resolved: no guard)*

## Phase 2 — Ratings & decisions (deletion deferred to 4.x)

- [ ] **2.1** Rating CRUD: PATCH `human_rating` (−2..+2) on any `thing`; on raise-to-eligible set `try_on=CURRENT_DATE` where `best_oi IS NULL` — **S** — #36 #23 ⏩ *pull forward, build alongside Phase 1*
- [ ] **2.2** Compute-on-read machine rating: video=MAX(parent playlists), playlist=AVG(members), one hop, grade bands; wire into worker predicate — **L** — #36 *(perf-sensitive core; fallback in Part 6)*
- [ ] **2.3** Deletion — **none in 4.0**. D/F rating only records the rating; the rating gate excluding the thing is the only 4.0 effect. Media delete + F-only purge are 4.x — — #36 #111

## Phase 3 — Observability, frontend, integration close-out

- [ ] **3.1** Status dashboard endpoints: recent activity / new things / failures — **M** — #35 #129
- [ ] **3.2** Frontend: rating UI (A/B/C/D/F), `try_on` + failure display, permafail-ack; remove hardcoded `freq_days:3` — **M** — #23 #129 #143
- [ ] **3.3** Crustula success callback (close `run_bknd.py:138`; DoD #8) — **S** — #118

---

## Blocked on your call (disposition before/at build)

These have open questions (commented on the issues); resolve before milestoning into the build:

- [ ] **#34** Handle permissions — scope unclear (multi-user? file perms? API auth?)
- [ ] **#46** yt channel vs user — still open after the `extractor_id` migration, or resolved?
- [ ] **#102** better videos iface — still needed, or covered by the V4 `thing` API? *(also referenced in 0.3)*
- [ ] **#104** sched-id / lm-job → OI — depends on yt-dlp-obj-idx#5; keep for 4.0 or defer?
- [ ] **#105** download archive import/export — greenfield ([F3]) makes V3 migration moot; keep only for yt-dlp archive-format interop, or drop?
