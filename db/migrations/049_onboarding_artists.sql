-- 049: onboarding_artists, the curated artists of the onboarding (#223)
--
-- The onboarding shows a grid of curated artists. The list lives in a table
-- and not in code so the owner can change it without a deploy: the seed
-- script (scripts/seed_onboarding_artists.py) fills it, and
-- replace_onboarding_artists, below, is the atomic write it uses.
--
-- Columns: id is the artist id of the external provider (the same one
-- GET /artist/{id} takes), name and thumbnail_url what the app shows, and
-- position the order of the grid, from 1, unique. onboarding_artists_id_check
-- is the net under the Python validation (the pattern of routes/artist.py),
-- the same split 041 and 042 use.
--
-- replace_onboarding_artists deletes EVERY row and inserts the new set from a
-- jsonb array, in one transaction: a failure of the INSERT (repeated id,
-- repeated or invalid position, malformed id) rolls the DELETE back and the
-- previous grid is left intact. The pattern is replace_user_recommendations
-- (047). There is no advisory lock: two seeds at the same time collide on the
-- primary key and one fails whole, which is acceptable for a script the owner
-- runs by hand. It returns the number of rows inserted.
--
-- Privileges: only service_role reads and writes. The table has RLS enabled
-- and no policy, and ALL is revoked from anon and authenticated because the
-- default privileges of 017 (lines 3110-3113 and 3120-3123) grant ALL on new
-- tables and functions to both; the precedent is 042 for image_colors and 047
-- for user_recommendations. PUBLIC is revoked from the function too.
-- service_role bypasses RLS and keeps the ALL it gets from the defaults, so
-- it is not touched. Same warning as 025, 031, 032, 042 and 047: a future
-- DROP + CREATE of the function resets the ACL and the default privileges
-- hand EXECUTE back to anon, so the REVOKEs must be repeated.
--
-- SECURITY INVOKER (the default), SET search_path TO 'public' in the bare
-- form, as 019, 020, 038 and 047.
--
-- No guards, no CREATE OR REPLACE, no IF NOT EXISTS: if a name exists
-- already that is drift and the file must fail loudly. BEGIN/COMMIT, like
-- every file that has changed privileges since 025. Normal migration: it
-- applies to the live database and also runs when building a new
-- database, after 048. Apply it BEFORE deploying the code that calls it.

BEGIN;

CREATE TABLE public.onboarding_artists (
  id text NOT NULL,
  name text NOT NULL,
  thumbnail_url text,
  position integer NOT NULL,
  CONSTRAINT onboarding_artists_pkey PRIMARY KEY (id),
  CONSTRAINT onboarding_artists_position_key UNIQUE (position),
  CONSTRAINT onboarding_artists_position_check CHECK (position >= 1),
  CONSTRAINT onboarding_artists_id_check CHECK (id ~ '^(MPLA)?UC')
);

ALTER TABLE public.onboarding_artists ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.onboarding_artists FROM anon;
REVOKE ALL ON public.onboarding_artists FROM authenticated;

CREATE FUNCTION public.replace_onboarding_artists(p_rows jsonb)
 RETURNS integer
 LANGUAGE plpgsql
 VOLATILE
 SET search_path TO 'public'
AS $function$
DECLARE
  v_inserted integer;
BEGIN
  -- WHERE true: an unfiltered DELETE is refused by the safe-update guard
  -- PostgREST sessions may load; the whole table is the target on purpose.
  DELETE FROM public.onboarding_artists WHERE true;

  INSERT INTO public.onboarding_artists (id, name, thumbnail_url, position)
  SELECT r.id, r.name, r.thumbnail_url, r.position
  FROM jsonb_to_recordset(p_rows)
    AS r(id text, name text, thumbnail_url text, position integer);

  GET DIAGNOSTICS v_inserted = ROW_COUNT;
  RETURN v_inserted;
END;
$function$;

REVOKE ALL ON FUNCTION public.replace_onboarding_artists(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.replace_onboarding_artists(jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.replace_onboarding_artists(jsonb) FROM authenticated;

COMMIT;
