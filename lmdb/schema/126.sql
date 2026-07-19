-- #126: human tagging (V4.x additive; DDL lifted verbatim from the v5-design draft
-- schema so the V5 merge is a no-op). create_all also creates these on a fresh DB.

CREATE TABLE tag (
  id          uuid PRIMARY KEY,
  name        text NOT NULL UNIQUE,
  created_dt  timestamp NOT NULL DEFAULT (now() at time zone 'utc')
);

CREATE TABLE thing_tag (
  thing_id    uuid NOT NULL REFERENCES thing(id),
  tag_id      uuid NOT NULL REFERENCES tag(id),
  source      text NOT NULL DEFAULT 'human',   -- 'machine' | 'human'
  confidence  double precision,                -- NULL for human-set
  created_dt  timestamp NOT NULL DEFAULT (now() at time zone 'utc'),
  PRIMARY KEY (thing_id, tag_id)
);
CREATE INDEX thing_tag_tag ON thing_tag (tag_id);
