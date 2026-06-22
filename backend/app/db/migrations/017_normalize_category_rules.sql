-- 017_normalize_category_rules.sql
-- Canonicalize app_category_rules.app_pattern so custom rules saved with raw app
-- names (for example Chrome) match normalized activity events. The category joins
-- compare normalize_app_name(ae.app_name) to r.app_pattern, so a raw pattern would
-- never match. URL-host patterns are unaffected because normalize_app_name passes
-- unknown values through unchanged.
--
-- Step 1 dedupes rows that would collide on the canonical value (the table has a
-- UNIQUE company_id+app_pattern constraint), keeping the lowest id. Step 2
-- normalizes the survivors.

DELETE FROM app_category_rules a
USING app_category_rules b
WHERE a.company_id = b.company_id
  AND normalize_app_name(a.app_pattern) = normalize_app_name(b.app_pattern)
  AND a.id > b.id;

UPDATE app_category_rules
SET app_pattern = normalize_app_name(app_pattern)
WHERE app_pattern <> normalize_app_name(app_pattern);
