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

Install for development (deps not vendored): `pip install -U "yt-dlp[default]" sqlmodel fastapi typer "psycopg[binary]" pytest-postgresql https://github.com/ctengel/yt-dlp-obj-idx/archive/master.zip https://github.com/ctengel/objectindex/archive/master.zip` plus `requirements.txt` (the latter is mostly V1/V2 legacy: flask/celery/redis). Also needs `ffmpeg`. V4 requires **PostgreSQL** (`psycopg`); the test suite spins up a throwaway cluster via `pytest-postgresql` (uses the system `initdb`/`pg_ctl`).

## Architecture (lmdb + lmfe)

**End-to-end flow:** a *schedule* (`PlaylistSched`) names a playlist URL + OI bucket + cadence. `job_runner.main()` fetches schedules due today (`GET /schedules/?next_run=`), then for each calls `run_bknd.init_download()`, which drives yt-dlp programmatically with two postprocessors: `ObjIdxUploadPP` (uploads each downloaded file to Object Index) and `LinkMeddlePlaylistPP` (after the playlist resolves, POSTs the full playlist back to `POST /playlist-run`). That endpoint records the playlist + its videos + run stats and computes the next run date via Fibonacci backoff. `lmfe` is a separate FastAPI app providing the human GUI/bookmarklet; it never touches the DB directly — it calls `lmdb`'s HTTP API and Object Index.

**`lmdb/models.py` has three model layers — know which you're in:**
1. **DLP-compat** (`CommonDLP`, `PlVidDLP`, `PlaylistDLP`) — mirror raw yt-dlp info-dict shapes for parsing extractor output.
2. **LM-native full** (`PlaylistFull`, `VidFull`) — normalized intermediate form; this is the `/playlist-run` request body.
3. **DB + API** SQLModel tables and their public/nested views (`PlaylistSum`, `PlaylistVid`, `PlaylistSched`, `PlaylistStats`, plus `*Public`, `*WithVids`, `*WithSched`, `*WithStatsAndSum`). Note the deliberate split of `PlaylistStats` into binary-hash (DB) vs string-hash (API) variants.

**`lmdb/xform.py`** is the only place that (a) converts DLP → LM-native (`pl_dlp2lm`, `full2sum`, `full2stats`) and (b) holds the scheduling/analytics logic: `compare_pl_runs` decides whether a playlist changed, `next_fib`/`add_new_run` adjust `freq_days`/`next_run` using a Fibonacci backoff. Put transform/analytics logic here, not in `api.py`.

**Key data conventions:**
- Playlists are keyed by `webpage_url` (there is no stable LM playlist ID yet — see the `xform.py` TODO; V4 introduces "LMPL" IDs).
- `extractor_id` is always stored lowercased.
- `POST /playlist-run` auto-creates a per-uploader `pseudo_channel` playlist for each video so videos are reachable by channel even without an explicit channel playlist.

## Schema migrations

The DB is SQLite; tables are auto-created from SQLModel on startup (`SQLModel.metadata.create_all`). There is **no migration framework** — schema changes for existing data are hand-written SQL in `lmdb/schema/`, named by the **GitHub issue number** that motivated them (e.g. `141.sql`, `117a.sql`). Follow that convention: write the migration as `<issue>.sql` and apply manually.

## Symlinks (intentional)

Several files are symlinks that share code across components — edit the real file, not the link:
- `lmdb/linkmeddle_playlist.py` → `yt-dlp-plugins/.../postprocessor/linkmeddle_playlist.py`
- `lmdb/ytdl_arch_oi.py` → `apiqueue/ytdl_arch_oi.py`
- `lmfe/lmdb` → `../lmdb` (so `lmfe` imports `lmdb.models`)

## Environment variables

`DATABASE_URL` (V4 defaults to `postgresql+psycopg:///lmdb`; PostgreSQL required), `LINKMEDDLE_PLAPI` (backend URL used by CLI/frontend/postprocessor), `OBJIDX_URL` + `OBJIDX_AUTH` (Object Index, required for uploads), `OBJIDX_BUCKET_DEFAULT` (lmfe), `CRUSTULA_URL` (cookie/auth microservice, used when a schedule sets `use_cookies`).

## V4 work

The authoritative V4 design is `LM-V4-DESIGN.md` in the repo root (tracked). The `lm-v4-design/` directory is **local-only** (gitignored, not in the tracked repo) and holds mostly source material that fed into the design (working notes, issue analyses, the decision log); `lm-v4-design/lm-v4-punch-list.md` tracks implementation progress — check items off as they land. V4 themes: per-item ratings, smarter pre-download prioritization, decoupled "fan-out" download workflow, and replication/deletion control. The V4 schema (Postgres + JSONB `thing`/`rel`/`run`) is **frozen** for 4.0 — 4.x changes must be additive (nullable columns / new tables), never migrations.
