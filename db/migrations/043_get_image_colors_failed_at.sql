-- 043: get_image_colors also returns failed_at (#213, ADR 022)
--
-- Replaces public.get_image_colors so it returns failed_at next to the
-- columns it already had. With it the reader tells an image whose color is
-- being computed right now (color null, failed_at null, attempted_at inside
-- the retry window) from one that failed recently (color null, failed_at
-- set): the first one is going to get a color, so the response that carries
-- it gets a short max-age; the second is a permanent null for the window and
-- does not shorten anything.
--
-- The definition it replaces is the one in 042, the only one there is. The
-- body is the same except for the added column.
--
-- DROP + CREATE and not CREATE OR REPLACE: the OUT parameters of a function
-- are its row type and CREATE OR REPLACE cannot change it (ERROR: cannot
-- change return type of existing function, HINT: Use DROP FUNCTION
-- get_image_colors(text[]) first). The precedent for a DROP + CREATE that
-- changes a signature is 028. A DROP without CASCADE fails loudly if
-- something depends on the function, which is what is wanted.
--
-- DROP + CREATE resets the ACL, and the default privileges of 017 (lines
-- 3110-3113, 3120-3123, 3130-3133 and 3140-3143) grant EXECUTE to anon and
-- authenticated on a new function, so the three REVOKE lines of 042 are
-- repeated. Same warning as 025, 031, 032 and 042.
--
-- claim_image_colors and the table are not touched.
--
-- SECURITY INVOKER (the default), SET search_path TO 'public' in the bare
-- form, as 042. No guards. BEGIN/COMMIT, like every file that has changed
-- privileges since 025. Normal migration: it applies to the live database
-- and also runs when building a new database, after 042. Apply it BEFORE
-- deploying the code that reads failed_at.

BEGIN;

DROP FUNCTION public.get_image_colors(text[]);

CREATE FUNCTION public.get_image_colors(p_keys text[])
 RETURNS TABLE(image_key text, color text, attempted_at timestamp with time zone, failed_at timestamp with time zone)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT c.image_key, c.color, c.attempted_at, c.failed_at
  FROM public.image_colors c
  WHERE c.image_key = ANY (p_keys);
$function$;

REVOKE ALL ON FUNCTION public.get_image_colors(text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_image_colors(text[]) FROM anon;
REVOKE ALL ON FUNCTION public.get_image_colors(text[]) FROM authenticated;

COMMIT;
