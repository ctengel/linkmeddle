-- #196b: repair attrs corrupted by 196.sql's `||` on scalar-null attrs.
-- 196.sql did `coalesce(attrs,'{}') || jsonb_build_object('channel_id', native_id)`; when the
-- row's attrs was a JSONB scalar `null` (coalesce doesn't catch it -- it only replaces SQL NULL),
-- `||` treats each non-array operand as a one-element array, so the result was the array
-- [null, {"channel_id": ...}] instead of an object. That breaks ThingRead validation
-- (attrs: Optional[dict]) and would crash xform.merge_attr ({**(thing.attrs or {})}).
--
-- Array rows -> collapse to their single object element (the {channel_id: ...} hint), dropping
-- the null; an array with no object element -> NULL. Scalar-null / other non-object rows -> NULL.
-- Idempotent / safe to re-run: afterwards every attrs is a JSONB object or SQL NULL, so both
-- WHERE clauses match nothing.

-- Preview:
-- SELECT id, extractor_key, url, attrs FROM thing
-- WHERE attrs IS NOT NULL AND jsonb_typeof(attrs) <> 'object';

-- Corrupted arrays -> the object element (channel_id hint preserved).
UPDATE thing t SET attrs = (
    SELECT elem
    FROM jsonb_array_elements(t.attrs) AS elem
    WHERE jsonb_typeof(elem) = 'object'
    ORDER BY 1
    LIMIT 1)
WHERE jsonb_typeof(t.attrs) = 'array';

-- Any remaining non-object attrs (scalar null/string/number, or an array with no object element
-- that the update above set to NULL) -> SQL NULL.
UPDATE thing SET attrs = NULL
WHERE attrs IS NOT NULL AND jsonb_typeof(attrs) <> 'object';
