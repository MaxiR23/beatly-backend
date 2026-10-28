-- 044: claim_image_colors also takes back an abandoned key (#213, ADR 022)
--
-- Replaces public.claim_image_colors so it also takes back a key with no
-- color and no failed_at whose claim is older than p_abandoned_before: a job
-- that was abandoned (forced shutdown, a failed write, an exception outside
-- the ones the job catches, or a job still in the queue). The caller passes
-- now - IMAGE_COLOR_ABANDONED_AFTER. A failed key (failed_at set) keeps
-- waiting for p_retry_before, as before. Issue #213, ADR 022.
--
-- The definition it replaces is the one in 042, the only one there is. The
-- body is the same except for the added parameter and the WHERE.
--
-- DEFAULT NULL: with NULL, c.attempted_at < NULL is NULL, the OR reduces to
-- the condition of 042 and a call with three arguments behaves exactly as
-- before. That is why the code deployed today keeps working after this file
-- is applied.
--
-- DROP + CREATE and not CREATE OR REPLACE: adding a parameter with CREATE OR
-- REPLACE creates an overload, and the named call with three arguments
-- becomes ambiguous (ERROR: function public.claim_image_colors(p_keys =>
-- text[], p_attempted_at => timestamp with time zone, p_retry_before =>
-- timestamp with time zone) is not unique). The precedent is 028. A DROP
-- without CASCADE fails loudly if something depends on the function, which
-- is what is wanted.
--
-- DROP + CREATE resets the ACL, and the default privileges of 017 (lines
-- 3110-3113, 3120-3123, 3130-3133 and 3140-3143) grant EXECUTE to anon and
-- authenticated on a new function, so the three REVOKE lines of 042 are
-- repeated with the new signature. Same warning as 025, 031, 032, 042 and
-- 043.
--
-- Atomicity: as in 042, the condition lives in the WHERE of the ON CONFLICT
-- DO UPDATE. Two simultaneous claims of an abandoned key: the second waits
-- for the row lock, evaluates the WHERE again on the new attempted_at and
-- gets nothing.
--
-- get_image_colors and the table are not touched.
--
-- SECURITY INVOKER (the default), SET search_path TO 'public' in the bare
-- form, as 042. No guards. BEGIN/COMMIT. Normal migration: it applies to the
-- live database and also runs when building a new database, after 043. Apply
-- it BEFORE deploying the code that passes p_abandoned_before.

BEGIN;

DROP FUNCTION public.claim_image_colors(text[], timestamp with time zone, timestamp with time zone);

CREATE FUNCTION public.claim_image_colors(
  p_keys text[],
  p_attempted_at timestamp with time zone,
  p_retry_before timestamp with time zone,
  p_abandoned_before timestamp with time zone DEFAULT NULL
)
 RETURNS TABLE(image_key text)
 LANGUAGE sql
 VOLATILE
 SET search_path TO 'public'
AS $function$
  INSERT INTO public.image_colors AS c (image_key, attempted_at)
  SELECT DISTINCT k, p_attempted_at FROM unnest(p_keys) AS k
  ON CONFLICT (image_key) DO UPDATE
    SET attempted_at = excluded.attempted_at, failed_at = NULL
    WHERE c.color IS NULL
      AND (
        c.attempted_at < p_retry_before
        OR (c.failed_at IS NULL AND c.attempted_at < p_abandoned_before)
      )
  RETURNING c.image_key;
$function$;

REVOKE ALL ON FUNCTION public.claim_image_colors(text[], timestamp with time zone, timestamp with time zone, timestamp with time zone) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_image_colors(text[], timestamp with time zone, timestamp with time zone, timestamp with time zone) FROM anon;
REVOKE ALL ON FUNCTION public.claim_image_colors(text[], timestamp with time zone, timestamp with time zone, timestamp with time zone) FROM authenticated;

COMMIT;
