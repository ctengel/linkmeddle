-- #196: demote facet native_ids squatting on non-channel containers.
--
-- Pre-#190 rows (V3-migrated channel-URL containers) and tabs that self-pulled before their
-- channel hold native_id = the channel's shared (facet) id without the attrs.kind='channel'
-- merge guard. Post-#190, every sibling tab's self-pull proposes that same id via
-- null_backfill; the clash merge then deletes the holder into the tab (tab-eats-tab cascade:
-- distinct tabs serially merged, run histories mixed). The code fix (facet_native guard in
-- _apply_backfill) stops the merges; this one-off frees the squatting ids (each kept as the
-- soft attrs.channel_id hint) so the rows match the post-#190 convention (containers
-- URL-keyed, facet id as hint), the id is claimable by the next self-owned pull (#160), and
-- the analyzer's duplicate-pair backlog reflects reality.
--
-- Evidence that an id is a facet (shared) id comes from run history: some OTHER container of
-- the same (backend, extractor_key) has a run whose raw data_json reports the id as its own
-- channel_id/uploader_id -- xform.owns_native_id's two-namespace test in SQL. Run-derived on
-- purpose:
--   * it works on deploy day: the attrs.channel_id hints an earlier draft matched are only
--     written by the code fix shipping WITH this file (0 rows would match at deploy), and
--     sibling-row evidence inside thing itself is impossible (the thing_native partial unique
--     index forbids two rows sharing an id);
--   * a row's own runs are never evidence against it (h.id <> t.id), so a channel that
--     legitimately claimed a free facet id cannot demote itself;
--   * h.container keeps leaf runs out: a video's data_json carries its uploader's id, which
--     would otherwise falsely demote the single per-channel containers (twitchvideos,
--     vkuservideos) that legitimately hold the uploader id (no tab siblings, no cascade);
--   * r.success is NOT required -- a failed run's data_json is kept for debugging and its
--     identity fields are still valid evidence; ->> on a non-object data_json (v3-migration
--     synthetic payloads) is SQL NULL, never an error.
--
-- Idempotent / safe to re-run: a demoted row no longer matches native_id IS NOT NULL, and a
-- post-fix free-claimer demoted by fresh sibling evidence simply re-claims the freed id on
-- its next self-pull (claim-when-free, #160).

-- Preview first:
-- SELECT t.id, t.extractor_key, t.url, t.native_id, t.attrs->>'channel_id' AS hint, t.try_on
-- FROM thing t
-- WHERE t.container AND t.native_id IS NOT NULL AND t.url IS NOT NULL
--   AND coalesce(t.attrs->>'kind', '') <> 'channel'
--   AND EXISTS (SELECT 1 FROM run r JOIN thing h ON h.id = r.thing_id
--               WHERE h.id <> t.id
--                 AND h.container
--                 AND h.backend = t.backend
--                 AND h.extractor_key = t.extractor_key
--                 AND (r.data_json->>'channel_id' = t.native_id
--                      OR r.data_json->>'uploader_id' = t.native_id));

UPDATE thing t SET
    attrs = coalesce(t.attrs, '{}'::jsonb) || jsonb_build_object('channel_id', t.native_id),
    native_id = NULL
WHERE t.container AND t.native_id IS NOT NULL AND t.url IS NOT NULL
  AND coalesce(t.attrs->>'kind', '') <> 'channel'
  AND EXISTS (SELECT 1 FROM run r JOIN thing h ON h.id = r.thing_id
              WHERE h.id <> t.id
                AND h.container
                AND h.backend = t.backend
                AND h.extractor_key = t.extractor_key
                AND (r.data_json->>'channel_id' = t.native_id
                     OR r.data_json->>'uploader_id' = t.native_id));
