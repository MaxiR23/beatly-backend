-- 053: new_releases and replace_new_releases, the atomic write of
-- POST /ingest/releases
--
-- The list of new releases is filled by an ingest: a collector (ingest/device/
-- collector.py) reads the new releases page of the external provider and posts
-- the cards that pass its checks to POST /ingest/releases, which calls
-- replace_new_releases below. GET /releases reads the table. PostgREST runs one
-- call per transaction, so the only way to delete and insert as one unit is a
-- function (the reason of 047, 049 and 052).
--
-- Scope: the table has a source column and the function replaces only the list
-- of its own source ('provider_new_releases', a literal here and a constant in
-- services/release_service.py: a test ties both). A future second source is
-- not touched by this function. The primary key is (source, album_id) and the
-- order of the list is position, unique per source. There is no uuid and no
-- timestamp: the same payload leaves byte for byte the same rows.
--
-- replace_new_releases(p_releases jsonb) returns the number of rows inserted.
-- p_releases is an array of {album_id, audio_playlist_id, type, title,
-- artists, thumbnail_url, position}. A failure (a repeated album or position,
-- a CHECK) rolls the DELETE back and the previous list is left intact. The
-- Python validation of the endpoint is the first line; the checks here and the
-- CHECKs are the net under it.
--
-- It takes pg_advisory_xact_lock first (the form of 047 and 052, with its own
-- fixed key): two ingests at the same time are serialized instead of one
-- failing on the primary key with the rows the other inserted.
--
-- This migration does not use or modify feed_current, upcoming_releases or
-- release_sync_errors.
--
-- Privileges: the default privileges of 017 (lines 3110-3113 and 3120-3123)
-- grant ALL on a new table and EXECUTE on a new function to anon and
-- authenticated. The table is only touched by the backend with the
-- service-role client: RLS enabled with no policy and ALL revoked from anon
-- and authenticated. On the function, ALL is revoked from PUBLIC, anon and
-- authenticated; service_role keeps the ALL it gets from the defaults. Same
-- warning as 025, 031, 032, 042, 047, 049 and 052: a future DROP + CREATE of
-- the function resets the ACL and the REVOKEs must be repeated.
--
-- SECURITY INVOKER (the default), SET search_path TO 'public' in the bare
-- form, as 019, 020, 038, 047, 049 and 052.
--
-- No guards, no CREATE OR REPLACE, no IF NOT EXISTS: if a name exists
-- already that is drift and the file must fail loudly. BEGIN/COMMIT, like
-- every file that has changed privileges since 025. Normal migration: live and
-- new database, after 052. Apply it BEFORE deploying the code that calls it.

BEGIN;

CREATE TABLE public.new_releases (
  source text NOT NULL,
  album_id text NOT NULL,
  audio_playlist_id text NOT NULL,
  type text NOT NULL,
  title text NOT NULL,
  artists jsonb NOT NULL,
  thumbnail_url text NOT NULL,
  position integer NOT NULL,
  CONSTRAINT new_releases_pkey PRIMARY KEY (source, album_id),
  CONSTRAINT new_releases_source_position_key UNIQUE (source, position),
  CONSTRAINT new_releases_position_check CHECK (position >= 1),
  CONSTRAINT new_releases_album_id_check CHECK (album_id ~ '^MPREb_[A-Za-z0-9_-]+$'),
  CONSTRAINT new_releases_audio_playlist_id_check CHECK (audio_playlist_id ~ '^OLAK5uy_[A-Za-z0-9_-]+$'),
  CONSTRAINT new_releases_type_check CHECK (type IN ('album', 'single', 'ep')),
  CONSTRAINT new_releases_artists_is_array CHECK (jsonb_typeof(artists) = 'array'),
  CONSTRAINT new_releases_artists_not_empty CHECK (jsonb_array_length(artists) > 0),
  CONSTRAINT new_releases_thumbnail_url_check CHECK (thumbnail_url ~ '^https://')
);

ALTER TABLE public.new_releases ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.new_releases FROM anon;
REVOKE ALL ON public.new_releases FROM authenticated;

CREATE FUNCTION public.replace_new_releases(p_releases jsonb)
 RETURNS integer
 LANGUAGE plpgsql
 VOLATILE
 SET search_path TO 'public'
AS $function$
DECLARE
  v_inserted integer;
BEGIN
  -- 0. one ingest at a time (047, 052)
  PERFORM pg_advisory_xact_lock(hashtextextended('replace_new_releases', 0));

  -- 1. nets under the Python validation (two IFs: jsonb_array_length
  --    raises on a non-array)
  IF jsonb_typeof(p_releases) IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION 'p_releases must be a non-empty array' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF jsonb_array_length(p_releases) = 0 THEN
    RAISE EXCEPTION 'p_releases must be a non-empty array' USING ERRCODE = 'invalid_parameter_value';
  END IF;

  -- 2. only the list of this source; a future source is not touched
  DELETE FROM public.new_releases WHERE source = 'provider_new_releases';

  INSERT INTO public.new_releases
    (source, album_id, audio_playlist_id, type, title, artists, thumbnail_url, position)
  SELECT 'provider_new_releases', r.album_id, r.audio_playlist_id, r.type, r.title,
    r.artists, r.thumbnail_url, r.position
  FROM jsonb_to_recordset(p_releases) AS r(album_id text, audio_playlist_id text,
    type text, title text, artists jsonb, thumbnail_url text, position integer);

  GET DIAGNOSTICS v_inserted = ROW_COUNT;
  RETURN v_inserted;
END;
$function$;

REVOKE ALL ON FUNCTION public.replace_new_releases(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.replace_new_releases(jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.replace_new_releases(jsonb) FROM authenticated;

COMMIT;
