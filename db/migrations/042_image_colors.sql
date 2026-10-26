-- 042: image colors, one row per image (ADR 021)
--
-- Adds the table public.image_colors and two functions around it. The color
-- of an image (#rrggbb, already adjusted to the dark theme of the app) is
-- computed once, after a response is sent, and read on every later request
-- that carries that image.
--
-- A table and not columns: the images of the API come from five different
-- tables and from the provider directly, so a column per table would store
-- the same color several times and cover only some of the places an image
-- shows up. The key is image_key: the image URL up to the first size suffix
-- (=w<n>-h<n>), or the whole URL when it has none, so every size of one
-- image is one row (core/thumbnails.py image_key).
--
-- Columns: color is null while it has not been computed or when the
-- computation failed. attempted_at is when a request claimed the key
-- (NOT NULL: without it there is no telling "in progress" from "never
-- tried", and no retry window with a single column). failed_at is when a
-- computation failed. A row has a color or a failed_at, never both
-- (image_colors_color_or_failed_check). image_colors_color_check is the net
-- under the Python model (StoredImageColor), the same split 041 uses.
--
-- Two functions, both by RPC (POST): get_image_colors reads a batch of keys.
-- A .in_() filter would travel in the query string of a GET, and GET
-- /library with limit=100 can carry 500 keys of about 140 characters, which
-- is about 70 KB of URL; the array goes in the body instead.
-- claim_image_colors is the atomic claim: one INSERT ... ON CONFLICT DO
-- UPDATE ... WHERE ... RETURNING that returns only the keys the caller now
-- owns, which PostgREST cannot express. It inserts a new key, or takes back
-- a key that has no color and whose last attempt is older than
-- p_retry_before; a key with a color, or one attempted recently (in
-- progress or failed), is not returned. DISTINCT because an ON CONFLICT DO
-- UPDATE cannot touch the same row twice in one statement. Two claims of a
-- new key at the same time: the second waits for the insert of the first,
-- falls into the conflict, and its WHERE no longer holds (attempted_at is
-- recent), so it gets nothing. The dates come from Python (the same clock
-- that stamps attempted_at, as ADR 013 does for error_logs), so the window
-- is compared against that one clock.
--
-- Privileges: only service_role reads and writes. The table has RLS enabled
-- and no policy, and ALL is revoked from anon and authenticated because the
-- default privileges of 017 (lines 3110-3113, 3120-3123, 3130-3133 and
-- 3140-3143) grant ALL on new tables and functions to both; the precedent
-- is 035 for a new relation and 038 for a new function. PUBLIC is revoked
-- from the functions too. service_role bypasses RLS and keeps the ALL it
-- gets from the defaults, so it is not touched. Same warning as 025, 031
-- and 032: a future DROP + CREATE of either function resets the ACL and
-- the default privileges hand EXECUTE back to anon, so the REVOKEs must be
-- repeated.
--
-- SECURITY INVOKER (the default), SET search_path TO 'public' in the bare
-- form, as 019, 020 and 038: nothing here creates a temporary table.
--
-- No guards, no CREATE OR REPLACE, no IF NOT EXISTS: if a name exists
-- already that is drift and the file must fail loudly. BEGIN/COMMIT, like
-- every file that has changed privileges since 025. Normal migration: it
-- applies to the live database and also runs when building a new
-- database, after 041. Apply it BEFORE deploying the code that calls it.

BEGIN;

CREATE TABLE public.image_colors (
  image_key text NOT NULL,
  color text,
  attempted_at timestamp with time zone NOT NULL,
  failed_at timestamp with time zone,
  CONSTRAINT image_colors_pkey PRIMARY KEY (image_key),
  CONSTRAINT image_colors_color_check
    CHECK (color IS NULL OR color ~ '^#[0-9a-f]{6}$'),
  CONSTRAINT image_colors_color_or_failed_check
    CHECK (color IS NULL OR failed_at IS NULL)
);

ALTER TABLE public.image_colors ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.image_colors FROM anon;
REVOKE ALL ON public.image_colors FROM authenticated;

CREATE FUNCTION public.get_image_colors(p_keys text[])
 RETURNS TABLE(image_key text, color text, attempted_at timestamp with time zone)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT c.image_key, c.color, c.attempted_at
  FROM public.image_colors c
  WHERE c.image_key = ANY (p_keys);
$function$;

CREATE FUNCTION public.claim_image_colors(
  p_keys text[],
  p_attempted_at timestamp with time zone,
  p_retry_before timestamp with time zone
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
    WHERE c.color IS NULL AND c.attempted_at < p_retry_before
  RETURNING c.image_key;
$function$;

REVOKE ALL ON FUNCTION public.get_image_colors(text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_image_colors(text[]) FROM anon;
REVOKE ALL ON FUNCTION public.get_image_colors(text[]) FROM authenticated;
REVOKE ALL ON FUNCTION public.claim_image_colors(text[], timestamp with time zone, timestamp with time zone) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_image_colors(text[], timestamp with time zone, timestamp with time zone) FROM anon;
REVOKE ALL ON FUNCTION public.claim_image_colors(text[], timestamp with time zone, timestamp with time zone) FROM authenticated;

COMMIT;
