# linkmeddle
LINKed MEDia DL — downloads media and tracks/schedules playlists.

## V4 (current)

V4 decouples playlist metadata pulls from video downloads ("fan-out") and adds per-item ratings to drive what gets fetched. The backend is FastAPI + PostgreSQL; the frontend (`lmfe`) is a separate FastAPI app for the human GUI/bookmarklet.

### Install

```bash
# System deps
dnf install postgresql-server ffmpeg   # or apt equivalent

# Python deps
pip install -U -r requirements.txt

# Also needed for tests (requires system initdb/pg_ctl)
pip install pytest-postgresql
```

### Environment variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL DSN; defaults to `postgresql+psycopg:///lmdb` |
| `LINKMEDDLE_PLAPI` | Backend URL (used by CLI, frontend, yt-dlp postprocessor) |
| `OBJIDX_URL` | Object Index base URL (required for uploads) |
| `OBJIDX_AUTH` | Object Index auth token |
| `OBJIDX_BUCKET_DEFAULT` | Default OI bucket (lmfe) |
| `CRUSTULA_URL` | Cookie/auth microservice (when a schedule sets `use_cookies`) |

### Running

```bash
# Backend API + DB (creates tables on first start)
DATABASE_URL=postgresql+psycopg:///lmdb fastapi dev lmdb/api.py --port 29072

# Frontend GUI
OBJIDX_URL=http://127.0.0.1/ OBJIDX_AUTH=user LINKMEDDLE_PLAPI=http://localhost:29072/ \
    fastapi dev lmfe/api.py --port 29062

# Schedule a playlist
LINKMEDDLE_PLAPI=http://localhost:29072/ python -m lmdb.cli --help

# Run all due jobs (pull-one-and-loop; claims jobs from the API)
OBJIDX_URL=http://127.0.0.1/ OBJIDX_AUTH=user LINKMEDDLE_PLAPI=http://localhost:29072/ \
    python -m lmdb.job_runner

# Download one URL directly (bypasses the job queue)
OBJIDX_URL=http://127.0.0.1/ OBJIDX_AUTH=user \
    python -m lmdb.run_bknd --oibucket bucket --no-playlist "https://example.com/video"

# Tests
pytest lmdb/test_api.py
```

### Backup and restore

The V4 DB is plain PostgreSQL, so `pg_dump`/`pg_restore` work directly against it. They accept the same connection URI as `DATABASE_URL`, except they don't recognize SQLAlchemy's `+psycopg` driver tag — drop that piece (e.g. `postgresql+psycopg://user:pass@host:5432/lmdb` becomes `postgresql://user:pass@host:5432/lmdb`) before using it.

```bash
# Backup (custom format: compressed, supports selective restore)
pg_dump -Fc "postgresql://user:pass@host:5432/lmdb" > lmdb-$(date +%F).dump

# Restore into a fresh DB (create it first)
createdb lmdb
pg_restore -d "postgresql://user:pass@host:5432/lmdb" lmdb-2026-07-03.dump
```

### Migrating from V3

V3 used SQLite; V4 uses PostgreSQL. The migration script reads your V3 `.db` file, looks up each downloaded video in Object Index, and writes things/rels/runs into the V4 PostgreSQL database.

```bash
# 1. Stand up the V4 DB and let the API create tables
DATABASE_URL=postgresql+psycopg:///lmdb fastapi dev lmdb/api.py --port 29072
# (start it once, then stop it)

# 2. Dry run to check counts and spot skipped items
DATABASE_URL=postgresql+psycopg:///lmdb \
OBJIDX_URL=http://... OBJIDX_AUTH=user \
    python lmdb/schema/155.py --v3-db /path/to/v3.db --dry-run

# 3. Run for real (add --default-bucket if some playlists had no schedule)
DATABASE_URL=postgresql+psycopg:///lmdb \
OBJIDX_URL=http://... OBJIDX_AUTH=user \
    python lmdb/schema/155.py --v3-db /path/to/v3.db [--default-bucket BUCKET]
```

What the script maps:

- `playlistsched` rows → `thing(container=True, human_rating=1.0, try_on=today)`
- other `playlistsum` → `thing(container=True, human_rating=None, try_on=today)`
- `playlistvid` entries → `thing(container=False)`; OI is searched to backfill `best_oi`
- `playlistvid` rows → `rel(parent=playlist, child=video, channel=False)`
- Every migrated thing gets one synthetic `run(worker="v3-migration", success=True)`

Playlists whose bucket cannot be determined (no schedule and no `--default-bucket`) are skipped with a warning.

## Deno

See https://github.com/yt-dlp/yt-dlp/wiki/EJS

### Installing Deno

See the [official install docs](https://docs.deno.com/runtime/getting_started/installation/). On Fedora/RHEL, build from cargo:

```bash
dnf install cargo clang
cargo install deno --locked
ln -s ~/.cargo/bin/deno ~/.local/bin/deno   # or wherever is on your PATH
```

**Known issue:** building via cargo requires a recent `rustc`. If you see:

```
error[E0658]: `let` expressions in this position are unstable
  --> .../v8-.../build.rs:1034:8
```

your rustc is too old to compile the `v8` crate. Options:
- Install a newer rustc via [rustup](https://rust-lang.github.io/rustup/concepts/channels.html)
- Use a prebuilt Deno binary from the official installer instead

## V1–V2 (legacy)

`scripts/` is V1-era ad-hoc scrapers. `apiqueue/` is V2-era Celery (dead since ~2021); its deps (flask/celery/redis/rabbitmq) are no longer needed for V4.

## Raspberry Pi hardware tips

- Monitor CPU temp: `vcgencmd measure_temp`; problems start around 65 °C
- Use an SSD for downloads (lots of temp files)
- Move swap to SSD: set `CONF_SWAPFILE` and `CONF_SWAPSIZE=2048` in `/etc/dphys-swapfile`
- Halve dirty page ratios: set `vm.dirty_background_ratio=5` and `vm.dirty_ratio=10` in `/etc/sysctl.d/local.conf`
- See also the [objectindex README](https://github.com/ctengel/objectindex)
