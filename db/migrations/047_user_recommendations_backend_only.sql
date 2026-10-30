-- 047: user_recommendations becomes backend-only, and four functions for it
--
-- The personalized recommendations (ADR 025, #221) are generated and read by
-- the backend alone, with the service-role client. This file closes the
-- table to the client roles and adds the four functions the generation
-- needs.
--
-- Privileges: 017 grants ALL on public.user_recommendations to anon and
-- authenticated (lines 3072-3073) and keeps a SELECT policy for the owner
-- (line 2637). Nothing reads the table with those roles, so ALL is revoked
-- from both. service_role keeps the ALL it gets from the defaults and is not
-- touched. The policy "user_recommendations readable by owner" stays: with
-- the privilege revoked it can no longer be reached, and deleting it is a
-- change nobody asked for. The four functions get the same treatment as 042:
-- the default privileges of 017 (lines 3111-3112 and 3121-3122) hand EXECUTE
-- to anon and authenticated on every new function, so PUBLIC, anon and
-- authenticated are revoked from each. Same warning as 025, 031, 032 and
-- 042: a future DROP + CREATE of any of them resets the ACL and the
-- REVOKEs must be repeated.
--
-- The functions, all SECURITY INVOKER (the default), SET search_path TO
-- 'public' in the bare form as 019, 020 and 038. Every one takes the user id
-- as a parameter: the caller is the service-role client, so the filter on
-- p_user_id is the only barrier, and it comes from the token.
--
-- get_top_played_artists and get_top_played_albums group play_events of the
-- user since p_since by the primary artist (metadata->'artists'->0->>'id')
-- and by metadata->>'album_id', the keys log_play() writes. They skip the
-- plays without that key (NULL or empty). They do not read album_name or
-- track_name, the broken keys of 008. The order is plays, then the most
-- recent play, then the id, so a tie is always resolved the same way. They
-- use pe_user_time_idx. PostgREST cannot group by a jsonb path, and the
-- ranking has to run in the database over a table that grows.
--
-- count_plays_since counts the plays of the user since an instant. It is a
-- function and not count="exact" because the tier of a set is decided by it
-- and the call stays next to the other three.
--
-- replace_user_recommendations is the atomic replacement: it deletes EVERY
-- row of the user (any bucket, the legacy rows of other buckets included)
-- and inserts the new set from a jsonb array, in one transaction. A
-- failure of the INSERT rolls the DELETE back, so the previous set is
-- never left half written. A transaction advisory lock keyed on the user
-- serializes two generations of the same user: without it the second
-- DELETE does not see the rows the first one inserted and the INSERT hits
-- user_recommendations_unique. It is the first advisory lock in the repo
-- (the precedent for serializing writers is the parent-row lock of 012 and
-- 013, and here there is no parent row per user). The dates come from
-- Python, the same clock that stamps played_at in log_play(), so the count
-- since generated_at compares one clock. It returns the number of rows
-- inserted.
--
-- Nothing is dropped: these are new names.
-- No guards, no CREATE OR REPLACE, no IF NOT EXISTS: if a name exists
-- already that is drift and the file must fail loudly. BEGIN/COMMIT, like
-- every file that has changed privileges since 025. Normal migration: it
-- applies to the live database and also runs when building a new
-- database, after 046. Apply it BEFORE deploying the code that calls it.

BEGIN;

REVOKE ALL ON public.user_recommendations FROM anon;
REVOKE ALL ON public.user_recommendations FROM authenticated;

CREATE FUNCTION public.get_top_played_artists(
  p_user_id uuid,
  p_since timestamp with time zone,
  p_limit integer
)
 RETURNS TABLE(artist_id text, play_count bigint)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT
    pe.metadata->'artists'->0->>'id' AS artist_id,
    count(*) AS play_count
  FROM public.play_events pe
  WHERE pe.user_id = p_user_id
    AND pe.played_at >= p_since
    AND coalesce(pe.metadata->'artists'->0->>'id', '') <> ''
  GROUP BY pe.metadata->'artists'->0->>'id'
  ORDER BY count(*) DESC, max(pe.played_at) DESC, pe.metadata->'artists'->0->>'id'
  LIMIT p_limit;
$function$;

CREATE FUNCTION public.get_top_played_albums(
  p_user_id uuid,
  p_since timestamp with time zone,
  p_limit integer
)
 RETURNS TABLE(album_id text, play_count bigint)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT
    pe.metadata->>'album_id' AS album_id,
    count(*) AS play_count
  FROM public.play_events pe
  WHERE pe.user_id = p_user_id
    AND pe.played_at >= p_since
    AND coalesce(pe.metadata->>'album_id', '') <> ''
  GROUP BY pe.metadata->>'album_id'
  ORDER BY count(*) DESC, max(pe.played_at) DESC, pe.metadata->>'album_id'
  LIMIT p_limit;
$function$;

CREATE FUNCTION public.count_plays_since(
  p_user_id uuid,
  p_since timestamp with time zone
)
 RETURNS bigint
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT count(*)
  FROM public.play_events pe
  WHERE pe.user_id = p_user_id
    AND pe.played_at >= p_since;
$function$;

CREATE FUNCTION public.replace_user_recommendations(
  p_user_id uuid,
  p_bucket text,
  p_source text,
  p_generated_at timestamp with time zone,
  p_valid_until timestamp with time zone,
  p_rows jsonb
)
 RETURNS integer
 LANGUAGE plpgsql
 VOLATILE
 SET search_path TO 'public'
AS $function$
DECLARE
  v_inserted integer;
BEGIN
  PERFORM pg_advisory_xact_lock(
    hashtextextended('user_recommendations:' || p_user_id::text, 0)
  );

  DELETE FROM public.user_recommendations
  WHERE user_id = p_user_id;

  INSERT INTO public.user_recommendations (
    user_id, bucket, item_type, item_id, rank,
    seed_type, seed_id, seed_name, seed_thumb,
    payload, source, generated_at, valid_until
  )
  SELECT
    p_user_id, p_bucket, r.item_type, r.item_id, r.rank,
    r.seed_type, r.seed_id, r.seed_name, r.seed_thumb,
    COALESCE(r.payload, '{}'::jsonb), p_source, p_generated_at, p_valid_until
  FROM jsonb_to_recordset(p_rows) AS r(
    item_type public.app_entity_type,
    item_id text,
    rank smallint,
    seed_type public.app_entity_type,
    seed_id text,
    seed_name text,
    seed_thumb text,
    payload jsonb
  );

  GET DIAGNOSTICS v_inserted = ROW_COUNT;
  RETURN v_inserted;
END;
$function$;

REVOKE ALL ON FUNCTION public.get_top_played_artists(uuid, timestamp with time zone, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_top_played_artists(uuid, timestamp with time zone, integer) FROM anon;
REVOKE ALL ON FUNCTION public.get_top_played_artists(uuid, timestamp with time zone, integer) FROM authenticated;
REVOKE ALL ON FUNCTION public.get_top_played_albums(uuid, timestamp with time zone, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_top_played_albums(uuid, timestamp with time zone, integer) FROM anon;
REVOKE ALL ON FUNCTION public.get_top_played_albums(uuid, timestamp with time zone, integer) FROM authenticated;
REVOKE ALL ON FUNCTION public.count_plays_since(uuid, timestamp with time zone) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.count_plays_since(uuid, timestamp with time zone) FROM anon;
REVOKE ALL ON FUNCTION public.count_plays_since(uuid, timestamp with time zone) FROM authenticated;
REVOKE ALL ON FUNCTION public.replace_user_recommendations(uuid, text, text, timestamp with time zone, timestamp with time zone, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.replace_user_recommendations(uuid, text, text, timestamp with time zone, timestamp with time zone, jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.replace_user_recommendations(uuid, text, text, timestamp with time zone, timestamp with time zone, jsonb) FROM authenticated;

COMMIT;
