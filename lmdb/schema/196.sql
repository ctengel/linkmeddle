-- #196: demote facet native_ids squatting on non-channel containers.
--
-- Pre-#190 rows (V3-migrated channel-URL containers) and tabs that self-pulled before their
-- channel hold native_id = the channel's shared (facet) id without the attrs.kind='channel'
-- merge guard. Post-#190, every sibling tab's self-pull proposes that same id via
-- null_backfill; the clash merge then deletes the holder into the tab (tab-eats-tab cascade:
-- distinct tabs serially merged, run histories mixed). The code fix (facet_native guard in
-- _apply_backfill) stops the merges; this one-off moves the squatting ids into the soft
-- attrs.channel_id hint so the rows match the post-#190 convention (containers URL-keyed,
-- facet id as hint) and the analyzer's duplicate-pair backlog reflects reality.
--
-- Scope: a container, URL-keyed-able (url present — never demote a url-less row, the id is
-- its only key), not a guarded channel, whose native_id is known to be a facet id: some thing
-- of the same extractor carries it as an attrs.channel_id hint (the post-#190 fan-out stores
-- the hint on every sibling tab). Extractor-scoped on purpose: matching on the bare value
-- would also sweep e.g. twitchvideos containers, where the single per-channel container
-- legitimately holds the uploader id (no tab siblings, no cascade).

-- Preview first:
-- SELECT id, extractor_key, url, native_id, attrs->>'channel_id' AS hint, try_on
-- FROM thing t
-- WHERE t.container AND t.native_id IS NOT NULL AND t.url IS NOT NULL
--   AND coalesce(t.attrs->>'kind', '') <> 'channel'
--   AND EXISTS (SELECT 1 FROM thing h
--               WHERE h.attrs->>'channel_id' = t.native_id
--                 AND h.extractor_key = t.extractor_key);

UPDATE thing t SET
    attrs = coalesce(t.attrs, '{}'::jsonb) || jsonb_build_object('channel_id', t.native_id),
    native_id = NULL
WHERE t.container AND t.native_id IS NOT NULL AND t.url IS NOT NULL
  AND coalesce(t.attrs->>'kind', '') <> 'channel'
  AND EXISTS (SELECT 1 FROM thing h
              WHERE h.attrs->>'channel_id' = t.native_id
                AND h.extractor_key = t.extractor_key);
