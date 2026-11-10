-- 052: source_playlist_id on genre_playlists and replace_genre_playlists, the
-- atomic write of POST /ingest/genres
--
-- The genre playlists are filled by an ingest: a collector (ingest/device/
-- collector.py) reads source playlists, resolves each video to its audio and
-- posts the result to POST /ingest/genres, which calls replace_genre_playlists
-- below. PostgREST runs one call per transaction, so the only way to delete,
-- insert and recount as one unit is a function (the reason of 047 and 049).
--
-- Scope and identity (decided by the repo owner):
--   1. Only the genres that appear in the payload are touched, with stable
--      ids. The new column genre_playlists.source_playlist_id holds the id of
--      the playlist the rows were built from, unique per genre. A playlist
--      that already exists keeps its id and only its tracks are replaced. The
--      playlists of those genres that are not in the payload are deleted. The
--      existing rows have no source_playlist_id (the historical seed), so they
--      are playlists that are not in the payload and are deleted when their
--      genre appears in it. The genres that are not in the payload are not
--      touched.
--   2. The upsert in tracks is ON CONFLICT (track_id) DO UPDATE of the six
--      metadata columns (title, artists, album, album_id, duration_seconds,
--      thumbnail_url), as like_track() does.
--
-- The UNIQUE (genre_id, source_playlist_id) allows many NULLs, so the
-- historical rows do not collide with each other.
--
-- replace_genre_playlists(p_playlists jsonb, p_tracks jsonb) returns the
-- number of rows inserted into genre_playlist_tracks. p_playlists is an array
-- of {genre_slug, source_playlist_id, title, description, category,
-- sort_order, tracks: [{track_id, position}]}; p_tracks an array of the rows
-- of tracks. Everything runs in one transaction: a failure (unknown slug,
-- repeated position, a track that violates a CHECK) rolls everything back and
-- the previous rows are left intact. The Python validation of the endpoint is
-- the first line; the checks here, the CHECKs, the uniques and the FKs are the
-- net under it.
--
-- It takes pg_advisory_xact_lock first (the form of 047, with its own fixed
-- key): two ingests at the same time are serialized instead of colliding on
-- the uniques. The same payload gives the same result: the same
-- genre_playlists ids, the same tracks in the same positions and the same
-- track_count and thumbnail_url. Only columns the API does not expose change
-- (genre_playlist_tracks.id and added_at, genre_playlists.updated_at).
-- track_count and thumbnail_url are set at the end as absolute values, so
-- they are right with or without trigger_update_genre_playlist_track_count.
--
-- Privileges: the default privileges of 017 (lines 3110-3113 and 3120-3123)
-- grant EXECUTE on a new function to anon and authenticated, so ALL is
-- revoked from PUBLIC, anon and authenticated; service_role keeps the ALL it
-- gets from the defaults. Same warning as 025, 031, 032, 042, 047 and 049: a
-- future DROP + CREATE of the function resets the ACL and the REVOKEs must be
-- repeated.
--
-- SECURITY INVOKER (the default), SET search_path TO 'public' in the bare
-- form, as 019, 020, 038, 047 and 049.
--
-- No guards, no CREATE OR REPLACE, no IF NOT EXISTS: if a name exists
-- already that is drift and the file must fail loudly. BEGIN/COMMIT, like
-- every file that has changed privileges since 025. Normal migration: it
-- applies to the live database and also runs when building a new
-- database, after 051. Apply it BEFORE deploying the code that calls it.

BEGIN;

ALTER TABLE public.genre_playlists ADD COLUMN source_playlist_id text;
ALTER TABLE public.genre_playlists
  ADD CONSTRAINT genre_playlists_source_playlist_key UNIQUE (genre_id, source_playlist_id);

CREATE FUNCTION public.replace_genre_playlists(p_playlists jsonb, p_tracks jsonb)
 RETURNS integer
 LANGUAGE plpgsql
 VOLATILE
 SET search_path TO 'public'
AS $function$
DECLARE
  v_missing text;
  v_linked integer;
BEGIN
  -- 0. one ingest at a time: a concurrent call waits here until this
  --    transaction ends, instead of colliding on the uniques (047)
  PERFORM pg_advisory_xact_lock(hashtextextended('replace_genre_playlists', 0));

  -- 1. nets under the Python validation
  IF jsonb_typeof(p_playlists) IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION 'p_playlists must be a non-empty array' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF jsonb_array_length(p_playlists) = 0 THEN
    RAISE EXCEPTION 'p_playlists must be a non-empty array' USING ERRCODE = 'invalid_parameter_value';
  END IF;

  IF EXISTS (
    SELECT 1 FROM jsonb_to_recordset(p_playlists) AS p(source_playlist_id text, tracks jsonb)
    WHERE coalesce(p.source_playlist_id, '') = ''
       OR CASE WHEN jsonb_typeof(p.tracks) = 'array'
               THEN jsonb_array_length(p.tracks) = 0
               ELSE true END
  ) THEN
    RAISE EXCEPTION 'a playlist has no source_playlist_id or no tracks' USING ERRCODE = 'invalid_parameter_value';
  END IF;

  -- 2. every slug exists
  SELECT p.genre_slug INTO v_missing
  FROM jsonb_to_recordset(p_playlists) AS p(genre_slug text)
  WHERE NOT EXISTS (SELECT 1 FROM public.genres g WHERE g.slug = p.genre_slug)
  LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION 'genre % does not exist', v_missing USING ERRCODE = 'no_data_found';
  END IF;

  -- 3. catalog rows: the six metadata columns are overwritten
  INSERT INTO public.tracks (track_id, title, artists, album, album_id, duration_seconds, thumbnail_url)
  SELECT r.track_id, r.title, r.artists, r.album, r.album_id, r.duration_seconds, r.thumbnail_url
  FROM jsonb_to_recordset(p_tracks) AS r(track_id text, title text, artists jsonb, album text,
    album_id text, duration_seconds integer, thumbnail_url text)
  ON CONFLICT (track_id) DO UPDATE SET
    title = EXCLUDED.title, artists = EXCLUDED.artists, album = EXCLUDED.album,
    album_id = EXCLUDED.album_id, duration_seconds = EXCLUDED.duration_seconds,
    thumbnail_url = EXCLUDED.thumbnail_url;

  -- 4. playlists of the genres in scope that are not in the payload, the
  --    historical rows with a null source included; their
  --    genre_playlist_tracks go with them (ON DELETE CASCADE, 017)
  DELETE FROM public.genre_playlists gp
  USING public.genres g
  WHERE gp.genre_id = g.id
    AND g.slug IN (SELECT p.genre_slug FROM jsonb_to_recordset(p_playlists) AS p(genre_slug text))
    AND NOT EXISTS (
      SELECT 1 FROM jsonb_to_recordset(p_playlists) AS p(genre_slug text, source_playlist_id text)
      WHERE p.genre_slug = g.slug AND p.source_playlist_id = gp.source_playlist_id
    );

  -- 5. upsert by (genre_id, source_playlist_id): an existing playlist keeps its id
  INSERT INTO public.genre_playlists (genre_id, source_playlist_id, title, description, category, sort_order)
  SELECT g.id, p.source_playlist_id, p.title, p.description, p.category, p.sort_order
  FROM jsonb_to_recordset(p_playlists) AS p(genre_slug text, source_playlist_id text, title text,
    description text, category text, sort_order integer)
  JOIN public.genres g ON g.slug = p.genre_slug
  ON CONFLICT ON CONSTRAINT genre_playlists_source_playlist_key DO UPDATE SET
    title = EXCLUDED.title, description = EXCLUDED.description,
    category = EXCLUDED.category, sort_order = EXCLUDED.sort_order;

  -- 6. replace the tracks of the payload playlists
  DELETE FROM public.genre_playlist_tracks gpt
  USING public.genre_playlists gp, public.genres g,
    jsonb_to_recordset(p_playlists) AS p(genre_slug text, source_playlist_id text)
  WHERE gpt.playlist_id = gp.id AND gp.genre_id = g.id
    AND g.slug = p.genre_slug AND gp.source_playlist_id = p.source_playlist_id;

  INSERT INTO public.genre_playlist_tracks (playlist_id, track_id, position)
  SELECT gp.id, t.track_id, t.position
  FROM jsonb_to_recordset(p_playlists) AS p(genre_slug text, source_playlist_id text, tracks jsonb)
  JOIN public.genres g ON g.slug = p.genre_slug
  JOIN public.genre_playlists gp ON gp.genre_id = g.id AND gp.source_playlist_id = p.source_playlist_id
  CROSS JOIN LATERAL jsonb_to_recordset(p.tracks) AS t(track_id text, position integer);

  GET DIAGNOSTICS v_linked = ROW_COUNT;

  -- 7. absolute values, right with or without trigger_update_genre_playlist_track_count
  UPDATE public.genre_playlists gp SET
    track_count = (SELECT count(*) FROM public.genre_playlist_tracks x WHERE x.playlist_id = gp.id),
    thumbnail_url = (
      SELECT t.thumbnail_url FROM public.genre_playlist_tracks x
      JOIN public.tracks t ON t.track_id = x.track_id
      WHERE x.playlist_id = gp.id AND t.thumbnail_url <> ''
      ORDER BY x.position LIMIT 1)
  FROM public.genres g, jsonb_to_recordset(p_playlists) AS p(genre_slug text, source_playlist_id text)
  WHERE gp.genre_id = g.id AND g.slug = p.genre_slug AND gp.source_playlist_id = p.source_playlist_id;

  RETURN v_linked;
END;
$function$;

REVOKE ALL ON FUNCTION public.replace_genre_playlists(jsonb, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.replace_genre_playlists(jsonb, jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.replace_genre_playlists(jsonb, jsonb) FROM authenticated;

COMMIT;
