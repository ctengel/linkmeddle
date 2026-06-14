-- LinkMeddle V4.0 — frozen schema (thing / rel / run)
-- Authoritative reference for the schema auto-created from lmdb/models.py via
-- SQLModel.metadata.create_all. PostgreSQL + JSONB. Greenfield (no V3 migration).
-- All datetimes are NAIVE UTC (`timestamp`, not `timestamptz`); the app uses UTC
-- everywhere. 4.x changes must be additive (nullable cols / new tables), never
-- migrations. See LM-V4-DESIGN.md Part 2. Issues: #129, #80, #128.

CREATE TABLE thing (
  id              uuid PRIMARY KEY,             -- surrogate, LM-assigned  [A1-A]
  url             text,                         -- canonical URL; NULLABLE [A6-A]
  backend         smallint NOT NULL DEFAULT 0,  -- acquisition engine code; 0 = yt-dlp [A7]
  site            text,                         -- rate-limit bucket / host; nullable [A7]
  extractor_key   text,                         -- backend source key (yt-dlp: extractor, lowercase)
  native_id       text,                         -- backend-native id (yt-dlp: extractor id)
  type            text NOT NULL,                -- 'video' | 'playlist' | 'channel'
  title           text,                         -- denormalized display    [A2-A]
  channel         text,                         -- denormalized display    [A2-A]
  thumbnail_url   text,                         -- nullable; populated when available (4.x UI)
  modified        timestamp,                    -- naive UTC content modified/upload time; nullable [A2-A]
  human_rating    double precision,             -- -2.0..+2.0, user-set; authoritative [B1,B2]
  machine_rating  double precision,             -- nullable; computed-on-read in 4.0   [B3,B4]
  last_success_dt timestamp,                    -- naive UTC
  last_failure_dt timestamp,                    -- naive UTC; nulled on success [C3-A]
  try_on          date DEFAULT (now() at time zone 'utc')::date,  -- backoff oracle [C1,C2]
  best_oi         text,                         -- pointer to best OI object; set on download [A5-A]
  attrs           jsonb,                        -- 4.x escape hatch (no migration needed)
  created_dt      timestamp NOT NULL DEFAULT (now() at time zone 'utc')  -- backs "new things" dashboard
);

CREATE UNIQUE INDEX thing_native ON thing (backend, extractor_key, native_id)
  WHERE native_id IS NOT NULL;                  -- secondary lookup key     [A1-A,A7]
CREATE UNIQUE INDEX thing_url ON thing (url)
  WHERE url IS NOT NULL;                         -- pre-extraction / paste-time dedup [A8]
CREATE INDEX thing_try_on ON thing (type, try_on);   -- worker selection

CREATE TABLE rel (
  parent     uuid NOT NULL REFERENCES thing(id),
  child      uuid NOT NULL REFERENCES thing(id),
  type       text NOT NULL,         -- 'playlist_video' | 'channel_playlist' | ...
  PRIMARY KEY (parent, child, type)
);
CREATE INDEX rel_child ON rel (child);

CREATE TABLE run (
  id             uuid PRIMARY KEY,
  thing_id       uuid NOT NULL REFERENCES thing(id),
  worker         text,                  -- nullable; identifies the runner instance [D2]
  input_json     jsonb,                 -- parameters passed in
  data_json      jsonb,                 -- raw yt-dlp output + computed stats
  entries_hash   bytea,                 -- nullable; playlist membership fingerprint; change-detection key
  playlist_count integer,               -- nullable; entry count the playlist reports
  starttime      timestamp NOT NULL,    -- naive UTC; assignment/start instant
  endtime        timestamp,             -- naive UTC
  success        boolean                -- T = success, F = failure, NULL = assigned/in-progress [F2]
);
CREATE INDEX run_thing ON run (thing_id, starttime DESC);
