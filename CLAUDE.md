# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

LinkMeddle (LM = "LINKed MEDia DL") downloads media via yt-dlp into Object Index storage and tracks/schedules playlists. Only the **V3 stack** (`lmdb/` + `lmfe/`) is active. `apiqueue/` is V2-era Celery (dead since ~2021); `scripts/` is V1-era ad-hoc scrapers. V4 (the `v4-fanout` branch) is in design — see "V4 work" below before making structural changes.

## Commands

```bash
# Backend API + DB (V4: PostgreSQL; set DATABASE_URL=postgresql+psycopg:///lmdb or your own)
fastapi dev lmdb/api.py --port 29072

# Frontend (needs env vars set; talks to backend over HTTP)
OBJIDX_URL= OBJIDX_AUTH= LINKMEDDLE_PLAPI= fastapi dev lmfe/api.py --port 29062

# Schedule a playlist (typer CLI -> POSTs /schedules/)
LINKMEDDLE_PLAPI=http://localhost:29072/ python -m lmdb.cli --help

# Run all due jobs (queries /schedules/, downloads each)
OBJIDX_URL=http://127.0.0.1/ OBJIDX_AUTH=user LINKMEDDLE_PLAPI=http://localhost:29072/ python -m lmdb.job_runner

# Download one URL directly through the yt-dlp pipeline
OBJIDX_URL=http://127.0.0.1/ OBJIDX_AUTH=user python -m lmdb.run_bknd --oibucket bucket --no-playlist "https://example.com/video"

# Tests
pytest lmdb/test_api.py                       # all
pytest lmdb/test_api.py::test_name            # single test
```

Install for development (deps not vendored): `pip install -U -r requirements.txt` plus `pytest-postgresql` for tests. Also needs `ffmpeg`. V4 requires **PostgreSQL** (`psycopg`); the test suite spins up a throwaway cluster via `pytest-postgresql` (uses the system `initdb`/`pg_ctl`).

## Architecture (lmdb + lmfe)

**End-to-end flow (V4):** the API owns prioritization; the worker is a thin pull-one-and-loop runner. `job_runner.main()` repeatedly `POST`s `/jobs/claim` to get the single highest-priority due job (a `JobClaim` with the `thing`, a `download` boolean — True only for a ≥B video acquire, False for a container pull or a C-band meta-enrich — and a cookies suggestion; claiming also opens an in-progress `Run`). It calls `run_bknd.init_download()` to drive yt-dlp, then `post_result()` pushes the outcome to `POST /jobs/{run_id}/result`. For a Stage-1 **pull** (run as flat as possible — `extract_flat`, minimal site calls) the worker extracts the thin `PlaylistFull` (`run_bknd.extract_pull`) and the endpoint fans it out into a stub `thing` per entry (+ per-uploader channels) and `rel` edges, records the `run`, and computes the next `try_on` via Fibonacci backoff; the endpoint marks a video stub metadata-complete (`last_success_dt`) when its extracted fields are enough for a human to rate it — the full identity set `title`/`url`/`native_id`/`extractor_key`/`channel` all present (`xform.enough_to_rate`, decided API-side). For a Stage-2 **download** (video ≥ B) `ObjIdxUploadPP` uploads the file to Object Index and the endpoint stores `best_oi` and marks the thing acquired (`try_on=NULL`). For a Stage-2 **meta** (a C-band video the flat pull under-described, `last_success_dt IS NULL`) the worker does a full single-video extract into one `VidFull` (`run_bknd.extract_pull_video`) — metadata only, no media, no `best_oi` — and the endpoint enriches the stub, fans out the video's channel, and sets `last_success_dt` so a human can rate it. `lmfe` is a separate FastAPI app providing the human GUI/bookmarklet; it never touches the DB directly — it calls `lmdb`'s HTTP API and Object Index.

**`lmdb/models.py` has two model groups — know which you're in:**
1. **Thin "pull" contract** (`UlChan`, `VidFull`, `PlaylistFull`) — the worker→API body for a Stage-1 pull (`RunResultIn.playlist`). Carries only the fields that land in `thing`/`rel` plus each video's raw `info_json` (the Stage-2 load-info hint). Nothing here mirrors yt-dlp's unstable shape: the worker extracts this straight from the raw info dict in `run_bknd.extract_pull` (which is the *only* place that touches raw yt-dlp fields).
2. **DB + API** — the frozen V4 SQLModel tables `Thing`/`Rel`/`Run` and their views (`ThingRead`, `RelatedThing`, `RunRead`, `RunActivity`, `ThingAdd`, `ThingPatch`, `ClaimRequest`, `JobClaim`, `RunResultIn`). See "V4 work" for the schema-freeze rules.

**`lmdb/xform.py`** is pure transform/analytics over the thin contract and the `run` table — it has no knowledge of raw yt-dlp shapes. It (a) maps the pull contract into the `thing`/`rel` graph (`pl_full2things`, `thing_from_vid`/`pl`/`chan`, `null_backfill`, `reconcile_count`) and (b) holds scheduling/analytics: `pl_hash`/`runs_differ` for change-detection and `next_fib`/`next_try_on` for the Fibonacci `try_on` backoff (derived from `run` history, no stored `freq_days`). Put transform/analytics logic here, not in `api.py`.

**Key data conventions:**
- Playlists are keyed by `webpage_url` (there is no stable LM playlist ID yet — see the `xform.py` TODO; V4 introduces "LMPL" IDs).
- `extractor_key` is always stored lowercased (the worker normalizes it in `extract_pull`).
- Stage-1 ingest (`POST /jobs/{run_id}/result`) auto-creates a channel thing (a container tagged `attrs.kind='channel'`) per distinct uploader and links it to the playlist and to each of its videos with `channel=True` `rel` edges, so videos are reachable by channel even without an explicit channel playlist.

## Schema migrations

The DB is **PostgreSQL** (V4; JSONB `thing`/`rel`/`run`); tables are auto-created from SQLModel on startup (`SQLModel.metadata.create_all`), and the canonical frozen DDL is mirrored in `lmdb/schema/v4.0.sql`. There is **no migration framework** — schema changes for existing data are hand-written SQL in `lmdb/schema/`, named by the **GitHub issue number** that motivated them (e.g. `141.sql`, `117a.sql`). Follow that convention: write the migration as `<issue>.sql` and apply manually. (Per the V4 schema freeze, 4.x changes should be additive — nullable columns / new tables — not migrations of existing data.)

## Symlinks (intentional)

Several files are symlinks that share code across components — edit the real file, not the link:
- `lmdb/linkmeddle_playlist.py` → `yt-dlp-plugins/.../postprocessor/linkmeddle_playlist.py`
- `lmfe/lmdb` → `../lmdb` (so `lmfe` imports `lmdb.models`)

(`lmdb/ytdl_arch_oi.py` was a symlink to the now-deleted `apiqueue/ytdl_arch_oi.py`; it is a
real file again — the V2-era `apiqueue/` copy is gone.)

## Environment variables

`DATABASE_URL` (V4 defaults to `postgresql+psycopg:///lmdb`; PostgreSQL required), `LINKMEDDLE_PLAPI` (backend URL used by CLI/frontend/postprocessor), `OBJIDX_URL` + `OBJIDX_AUTH` (Object Index, required for uploads), `OBJIDX_BUCKET_DEFAULT` (lmfe), `CRUSTULA_URL` (cookie/auth microservice, used when a schedule sets `use_cookies`), `WORKER_MIN_FREE_BYTES` (job_runner free-space floor in bytes; default 32 GiB, `0` disables — the worker stops claiming when its cwd has less free space; shared with pervellam), `LMFE_THUMB_DIR` (lmfe thumbnail cache directory, default `thumb_cache`).

## V4 work

The authoritative V4 design is `LM-V4-DESIGN.md` in the repo root (tracked). The `lm-v4-design/` directory is **local-only** (gitignored, not in the tracked repo) and holds mostly source material that fed into the design (working notes, issue analyses, the decision log); `lm-v4-design/lm-v4-punch-list.md` tracks implementation progress — check items off as they land. V4 themes: per-item ratings, smarter pre-download prioritization, decoupled "fan-out" download workflow, and replication/deletion control. The V4 schema (Postgres + JSONB `thing`/`rel`/`run`) is **frozen** for 4.0 — 4.x changes must be additive (nullable columns / new tables), never migrations.
