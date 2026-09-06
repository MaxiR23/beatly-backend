-- 017: full schema baseline of the public schema (#88)
--
-- Everything below this header is verbatim pg_dump output: a
-- `pg_dump --schema-only` of the public schema of this project's
-- Supabase database, taken on 2026-09-04. It is machine-generated,
-- not authored here, and no line of it is edited by hand. To change
-- the schema, add a new numbered migration. To refresh this
-- baseline, take a new dump.
--
-- This is a complete photograph of the schema (a baseline), NOT an
-- incremental migration meant to run against a database that already
-- has content. It contains `CREATE SCHEMA public;`, which fails
-- against any database where the public schema already exists —
-- including a fresh Supabase project, which already ships one, and any
-- database created from template1. So this statement has to be skipped
-- or adapted at run time; the file is never run against the live
-- database.
--
-- It also assumes a new *Supabase* project rather than a bare
-- Postgres. It references auth.users and auth.uid(), it grants to
-- the anon, authenticated and service_role roles, it calls
-- gen_random_uuid() with no CREATE EXTENSION of its own, and it
-- opens and closes with the psql meta-commands \restrict and
-- \unrestrict, which are not SQL and fail in a non-psql client such
-- as the Supabase SQL editor.
--
-- What this file subsumes, and what it does NOT carry -- notably
-- the on_auth_user_created trigger from 009, which sits on
-- auth.users, outside the public schema, and is therefore absent
-- from this dump -- is documented in db/migrations/README.md, under
-- this file's entry in ## Files.
--

--
-- PostgreSQL database dump
--

\restrict 32BQ3YhaPF7W5ztC7YcM84g9HEhlJWImUajJM5ByYeGbmQArklqnDxO4FuvN0aa

-- Dumped from database version 17.4
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: pg_database_owner
--

CREATE SCHEMA public;


ALTER SCHEMA public OWNER TO pg_database_owner;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: pg_database_owner
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: app_entity_type; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.app_entity_type AS ENUM (
    'track',
    'album',
    'artist',
    'playlist'
);


ALTER TYPE public.app_entity_type OWNER TO postgres;

--
-- Name: app_event_type; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.app_event_type AS ENUM (
    'play',
    'like',
    'unlike'
);


ALTER TYPE public.app_event_type OWNER TO postgres;

--
-- Name: feed_kind; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.feed_kind AS ENUM (
    'most_played',
    'new_releases',
    'seed_tracks',
    'new_singles'
);


ALTER TYPE public.feed_kind OWNER TO postgres;

--
-- Name: media_type; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.media_type AS ENUM (
    'album',
    'track'
);


ALTER TYPE public.media_type OWNER TO postgres;

--
-- Name: add_playlist_track(uuid, uuid, uuid); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid) RETURNS json
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_next_pos int;
  v_row playlist_tracks%ROWTYPE;
BEGIN
  -- serialize writers on this playlist (write protocol, see 013)
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  SELECT COALESCE(MAX(position), 0) + 1
  INTO v_next_pos
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  INSERT INTO playlist_tracks (playlist_id, track_id, position, added_by)
  VALUES (p_playlist_id, p_track_id, v_next_pos, p_added_by)
  RETURNING * INTO v_row;

  RETURN json_build_object('ok', true, 'id', v_row.id, 'position', v_row.position);

EXCEPTION
  WHEN unique_violation THEN
    -- el track ya esta en la playlist (constraint ux_playlist_track)
    RETURN json_build_object('ok', false, 'error', 'track_already_in_playlist');
END;
$$;


ALTER FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid) OWNER TO postgres;

--
-- Name: add_playlist_tracks_bulk(uuid, uuid[], uuid); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.add_playlist_tracks_bulk(p_playlist_id uuid, p_track_ids uuid[], p_added_by uuid) RETURNS json
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_base_pos int;
  v_added int;
  v_unique_requested int;
BEGIN
  -- serialize writers on this playlist + existence check
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  SELECT COALESCE(MAX(position), 0)
  INTO v_base_pos
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  -- input: dedupe the array keeping the first occurrence
  -- to_insert: drop tracks already in the playlist, number the rest 1..N
  --            in input order
  WITH input AS (
    SELECT DISTINCT ON (tid) tid, ord
    FROM unnest(p_track_ids) WITH ORDINALITY AS u(tid, ord)
    ORDER BY tid, ord
  ),
  to_insert AS (
    SELECT i.tid, ROW_NUMBER() OVER (ORDER BY i.ord) AS rn
    FROM input i
    WHERE NOT EXISTS (
      SELECT 1 FROM playlist_tracks pt
      WHERE pt.playlist_id = p_playlist_id
        AND pt.track_id = i.tid
    )
  )
  INSERT INTO playlist_tracks (playlist_id, track_id, position, added_by)
  SELECT p_playlist_id, tid, v_base_pos + rn, p_added_by
  FROM to_insert
  ORDER BY rn
  ON CONFLICT ON CONSTRAINT ux_playlist_track DO NOTHING;

  GET DIAGNOSTICS v_added = ROW_COUNT;

  SELECT COUNT(DISTINCT tid)
  INTO v_unique_requested
  FROM unnest(p_track_ids) AS u(tid);

  RETURN json_build_object(
    'ok', true,
    'added', v_added,
    'skipped', v_unique_requested - v_added
  );
END;
$$;


ALTER FUNCTION public.add_playlist_tracks_bulk(p_playlist_id uuid, p_track_ids uuid[], p_added_by uuid) OWNER TO postgres;

--
-- Name: aggregate_user_weekly_stats(uuid, date, date); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.aggregate_user_weekly_stats(p_user_id uuid, p_week_start date, p_week_end date) RETURNS jsonb
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_tracks jsonb;
  v_artists jsonb;
  v_albums jsonb;
BEGIN
  -- Top 10 tracks
  SELECT COALESCE(jsonb_agg(t ORDER BY t.play_count DESC), '[]'::jsonb)
  INTO v_tracks
  FROM (
    SELECT 
      track_id as entity_id,
      (array_agg(metadata->>'track_name') FILTER (WHERE metadata->>'track_name' IS NOT NULL))[1] as display_name,
      (array_agg(metadata->>'thumbnail_url') FILTER (WHERE metadata->>'thumbnail_url' IS NOT NULL))[1] as thumbnail_url,
      (array_agg(metadata->'artists') FILTER (WHERE metadata->'artists' IS NOT NULL))[1] as artists,
      (array_agg(metadata->>'album_id') FILTER (WHERE metadata->>'album_id' IS NOT NULL))[1] as album_id,
      (array_agg(metadata->>'album_name') FILTER (WHERE metadata->>'album_name' IS NOT NULL))[1] as album_name,
      (array_agg((metadata->>'duration_seconds')::int) FILTER (WHERE metadata->>'duration_seconds' IS NOT NULL))[1] as duration_seconds,
      COUNT(*)::int as play_count
    FROM play_events
    WHERE user_id = p_user_id 
      AND played_at >= p_week_start 
      AND played_at < p_week_end
    GROUP BY track_id
    ORDER BY play_count DESC
    LIMIT 10
  ) t;

  -- Top 10 artists (desde el array metadata->'artists', SIN fallback)
  SELECT COALESCE(jsonb_agg(a ORDER BY a.play_count DESC), '[]'::jsonb)
  INTO v_artists
  FROM (
    SELECT 
      artist_elem->>'id' as entity_id,
      (array_agg(artist_elem->>'name') FILTER (WHERE artist_elem->>'name' IS NOT NULL))[1] as display_name,
      (array_agg(pe.metadata->>'thumbnail_url') FILTER (WHERE pe.metadata->>'thumbnail_url' IS NOT NULL))[1] as thumbnail_url,
      COUNT(*)::int as play_count
    FROM play_events pe,
      LATERAL jsonb_array_elements(pe.metadata->'artists') as artist_elem
    WHERE pe.user_id = p_user_id 
      AND pe.played_at >= p_week_start 
      AND pe.played_at < p_week_end
      AND artist_elem->>'id' IS NOT NULL
    GROUP BY artist_elem->>'id'
    ORDER BY play_count DESC
    LIMIT 10
  ) a;

  -- Top 10 albums (artist_id/artist_name desde el primer elemento del array)
  SELECT COALESCE(jsonb_agg(al ORDER BY al.play_count DESC), '[]'::jsonb)
  INTO v_albums
  FROM (
    SELECT 
      metadata->>'album_id' as entity_id,
      (array_agg(metadata->>'album_name') FILTER (WHERE metadata->>'album_name' IS NOT NULL))[1] as display_name,
      (array_agg(metadata->>'thumbnail_url') FILTER (WHERE metadata->>'thumbnail_url' IS NOT NULL))[1] as thumbnail_url,
      (array_agg(metadata->'artists'->0->>'id') FILTER (WHERE metadata->'artists'->0->>'id' IS NOT NULL))[1] as artist_id,
      (array_agg(metadata->'artists'->0->>'name') FILTER (WHERE metadata->'artists'->0->>'name' IS NOT NULL))[1] as artist_name,
      COUNT(*)::int as play_count
    FROM play_events
    WHERE user_id = p_user_id 
      AND played_at >= p_week_start 
      AND played_at < p_week_end
      AND metadata->>'album_id' IS NOT NULL
    GROUP BY metadata->>'album_id'
    ORDER BY play_count DESC
    LIMIT 10
  ) al;

  RETURN jsonb_build_object(
    'track', v_tracks,
    'artist', v_artists,
    'album', v_albums
  );
END;
$$;


ALTER FUNCTION public.aggregate_user_weekly_stats(p_user_id uuid, p_week_start date, p_week_end date) OWNER TO postgres;

--
-- Name: bump_playlist_on_track_change(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.bump_playlist_on_track_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
  if pg_trigger_depth() > 1 then
    return null;
  end if;

  update playlists
     set updated_at = now()
   where id = coalesce(new.playlist_id, old.playlist_id);
  return null;
end;
$$;


ALTER FUNCTION public.bump_playlist_on_track_change() OWNER TO postgres;

--
-- Name: bump_playlist_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.bump_playlist_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
  if (new.title, new.description, new.is_public)
     is distinct from (old.title, old.description, old.is_public) then
    new.updated_at = now();
  end if;
  return new;
end;
$$;


ALTER FUNCTION public.bump_playlist_updated_at() OWNER TO postgres;

--
-- Name: cleanup_library_on_playlist_delete(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.cleanup_library_on_playlist_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
  delete from public.library_items
   where kind = 'playlist'
     and source = 'external'
     and external_id = old.id::text;
  return old;
end;
$$;


ALTER FUNCTION public.cleanup_library_on_playlist_delete() OWNER TO postgres;

--
-- Name: get_active_users_in_period(date, date); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.get_active_users_in_period(p_start date, p_end date) RETURNS TABLE(user_id uuid)
    LANGUAGE sql SECURITY DEFINER
    AS $$
  SELECT DISTINCT pe.user_id
  FROM play_events pe
  WHERE pe.played_at >= p_start 
    AND pe.played_at < p_end;
$$;


ALTER FUNCTION public.get_active_users_in_period(p_start date, p_end date) OWNER TO postgres;

--
-- Name: get_featured_new_release(uuid); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.get_featured_new_release(p_user_id uuid) RETURNS TABLE(album_id text, album_name text, artist_id text, artist_name text, release_date date, thumbnail_url text, track_count integer, play_count bigint)
    LANGUAGE sql STABLE
    AS $$
  WITH user_artist_plays AS (
    SELECT
      COALESCE(metadata->'artists'->0->>'id', '')   AS pe_artist_id,
      COALESCE(metadata->'artists'->0->>'name', '') AS pe_artist_name,
      COUNT(*)                                       AS play_count
    FROM play_events
    WHERE user_id = p_user_id
      AND played_at >= NOW() - INTERVAL '90 days'
    GROUP BY pe_artist_id, pe_artist_name
  )
  SELECT
    ur.album_id,
    ur.album_name,
    ur.artist_id,
    ur.artist_name,
    ur.release_date,
    ur.thumbnail_url,
    ur.track_count,
    uap.play_count
  FROM upcoming_releases ur
  INNER JOIN user_artist_plays uap
    ON (
      ur.artist_id IS NOT NULL
      AND uap.pe_artist_id != ''
      AND ur.artist_id = uap.pe_artist_id
    )
    OR (
      (ur.artist_id IS NULL OR uap.pe_artist_id = '')
      AND LOWER(ur.artist_name) = LOWER(uap.pe_artist_name)
    )
  WHERE ur.status = 'released'
    AND ur.release_date >= CURRENT_DATE - 45
  ORDER BY uap.play_count DESC, ur.release_date DESC
  LIMIT 1;
$$;


ALTER FUNCTION public.get_featured_new_release(p_user_id uuid) OWNER TO postgres;

--
-- Name: get_listen_again(uuid, integer, integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.get_listen_again(p_user_id uuid, p_min_plays integer DEFAULT 3, p_not_played_days integer DEFAULT 7, p_limit integer DEFAULT 1) RETURNS TABLE(album_id text, album_name text, artist_id text, artist_name text, thumbnail_url text, total_plays bigint, last_played_at timestamp with time zone)
    LANGUAGE sql STABLE
    AS $$
  SELECT
    metadata->>'album_id'                       AS album_id,
    metadata->>'album_name'                     AS album_name,
    metadata->'artists'->0->>'id'               AS artist_id,
    metadata->'artists'->0->>'name'             AS artist_name,
    metadata->>'thumbnail_url'                  AS thumbnail_url,
    count(*)                                    AS total_plays,
    max(played_at)                              AS last_played_at
  FROM play_events
  WHERE user_id = p_user_id
    AND metadata->>'album_id' IS NOT NULL
  GROUP BY
    metadata->>'album_id',
    metadata->>'album_name',
    metadata->'artists'->0->>'id',
    metadata->'artists'->0->>'name',
    metadata->>'thumbnail_url'
  HAVING
    count(*) >= p_min_plays
    AND max(played_at) < now() - make_interval(days => p_not_played_days)
  ORDER BY count(*) DESC
  LIMIT p_limit;
$$;


ALTER FUNCTION public.get_listen_again(p_user_id uuid, p_min_plays integer, p_not_played_days integer, p_limit integer) OWNER TO postgres;

--
-- Name: get_owned_playlists_with_track(uuid, text); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.get_owned_playlists_with_track(p_user_id uuid, p_track_id text) RETURNS TABLE(playlist_id uuid)
    LANGUAGE sql STABLE
    AS $$
    SELECT pl.id
    FROM playlists pl
    JOIN playlist_tracks pt ON pt.playlist_id = pl.id
    JOIN tracks t ON t.id = pt.track_id
    WHERE pl.owner_id = p_user_id
      AND t.track_id = p_track_id;
$$;


ALTER FUNCTION public.get_owned_playlists_with_track(p_user_id uuid, p_track_id text) OWNER TO postgres;

--
-- Name: get_playlist_thumbnails(uuid[], integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.get_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer DEFAULT 4) RETURNS TABLE(playlist_id uuid, thumbnail_url text)
    LANGUAGE sql STABLE
    AS $$
  SELECT 
    gpt.playlist_id,
    t.thumbnail_url
  FROM (
    SELECT 
      playlist_id,
      track_id,
      ROW_NUMBER() OVER (PARTITION BY playlist_id ORDER BY position) as rn
    FROM genre_playlist_tracks
    WHERE playlist_id = ANY(playlist_ids)
  ) gpt
  JOIN tracks t ON t.track_id = gpt.track_id
  WHERE gpt.rn <= limit_per_playlist
    AND t.thumbnail_url IS NOT NULL
  ORDER BY gpt.playlist_id, gpt.rn;
$$;


ALTER FUNCTION public.get_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer) OWNER TO postgres;

--
-- Name: get_replay_songs(uuid, integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.get_replay_songs(p_user_id uuid, p_min_plays integer DEFAULT 2, p_limit integer DEFAULT 30) RETURNS TABLE(track_id text, track_name text, artists jsonb, album_name text, album_id text, thumbnail_url text, duration_seconds integer, play_count bigint)
    LANGUAGE sql STABLE
    AS $$
  SELECT
    pe.track_id,
    MAX(pe.metadata->>'track_name')              AS track_name,
    (array_agg(pe.metadata->'artists') FILTER (WHERE pe.metadata->'artists' IS NOT NULL))[1] AS artists,
    MAX(pe.metadata->>'album_name')              AS album_name,
    MAX(pe.metadata->>'album_id')                AS album_id,
    MAX(pe.metadata->>'thumbnail_url')           AS thumbnail_url,
    MAX((pe.metadata->>'duration_seconds')::INT) AS duration_seconds,
    COUNT(*)                                     AS play_count
  FROM play_events pe
  WHERE
    pe.user_id = p_user_id
    AND pe.played_at >= NOW() - INTERVAL '60 days'
  GROUP BY pe.track_id
  HAVING COUNT(*) >= p_min_plays
  ORDER BY SUM(EXP(-EXTRACT(EPOCH FROM (NOW() - pe.played_at)) / (86400 * 20))) DESC
  LIMIT p_limit;
$$;


ALTER FUNCTION public.get_replay_songs(p_user_id uuid, p_min_plays integer, p_limit integer) OWNER TO postgres;

--
-- Name: get_user_playlist_thumbnails(uuid[], integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.get_user_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer DEFAULT 4) RETURNS TABLE(playlist_id uuid, thumbnail_url text)
    LANGUAGE sql STABLE
    AS $$
  SELECT
    upt.playlist_id,
    t.thumbnail_url
  FROM (
    SELECT
      playlist_id,
      track_id,
      ROW_NUMBER() OVER (PARTITION BY playlist_id ORDER BY position) AS rn
    FROM playlist_tracks
    WHERE playlist_id = ANY(playlist_ids)
  ) upt
  JOIN tracks t ON t.id = upt.track_id
  WHERE upt.rn <= limit_per_playlist
    AND t.thumbnail_url IS NOT NULL
    AND t.thumbnail_url <> ''
  ORDER BY upt.playlist_id, upt.rn;
$$;


ALTER FUNCTION public.get_user_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer) OWNER TO postgres;

--
-- Name: get_users_with_weekly_stats(date[]); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.get_users_with_weekly_stats(p_weeks date[]) RETURNS TABLE(user_id uuid)
    LANGUAGE sql SECURITY DEFINER
    AS $$
  SELECT DISTINCT uws.user_id
  FROM user_weekly_stats uws
  WHERE uws.week_start = ANY(p_weeks);
$$;


ALTER FUNCTION public.get_users_with_weekly_stats(p_weeks date[]) OWNER TO postgres;

--
-- Name: handle_new_user(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.handle_new_user() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
    insert into public.profiles (id, display_name, avatar_url)
    values (
        new.id,
        new.raw_user_meta_data->>'display_name',
        new.raw_user_meta_data->>'avatar_url'
    );
    return new;
end;
$$;


ALTER FUNCTION public.handle_new_user() OWNER TO postgres;

--
-- Name: handle_profile_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.handle_profile_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
    new.updated_at = now();
    return new;
end;
$$;


ALTER FUNCTION public.handle_profile_updated_at() OWNER TO postgres;

--
-- Name: is_admin(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.is_admin() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
    select exists (
        select 1 from public.profiles
        where id = auth.uid() and role = 'admin'
    );
$$;


ALTER FUNCTION public.is_admin() OWNER TO postgres;

--
-- Name: is_developer_or_higher(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.is_developer_or_higher() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
    select exists (
        select 1 from public.profiles
        where id = auth.uid()
          and role in ('developer', 'admin')
    );
$$;


ALTER FUNCTION public.is_developer_or_higher() OWNER TO postgres;

--
-- Name: is_tester_or_higher(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.is_tester_or_higher() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
    select exists (
        select 1 from public.profiles
        where id = auth.uid()
          and role in ('tester', 'developer', 'admin')
    );
$$;


ALTER FUNCTION public.is_tester_or_higher() OWNER TO postgres;

--
-- Name: move_playlist_track(uuid, integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer) RETURNS json
    LANGUAGE plpgsql SECURITY DEFINER
    AS $$
DECLARE
  v_total INT;
  v_rows JSON;
  v_row RECORD;
  v_idx INT;
BEGIN
  -- serialize writers on this playlist (write protocol, see header)
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;

  -- Defer constraint para permitir posiciones temporales duplicadas
  SET CONSTRAINTS ux_playlist_pos DEFERRED;

  -- Contar tracks
  SELECT COUNT(*) INTO v_total
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  IF v_total = 0 THEN
    RETURN json_build_object('ok', false, 'error', 'empty_playlist');
  END IF;

  -- Clamp indices
  IF p_old_index < 1 THEN p_old_index := 1; END IF;
  IF p_old_index > v_total THEN p_old_index := v_total; END IF;
  IF p_new_index < 1 THEN p_new_index := 1; END IF;
  IF p_new_index > v_total THEN p_new_index := v_total; END IF;

  -- Noop
  IF p_old_index = p_new_index THEN
    SELECT json_agg(row_to_json(t) ORDER BY t.position)
    INTO v_rows
    FROM (
      SELECT id AS "internalId", track_id AS "trackId", position
      FROM playlist_tracks
      WHERE playlist_id = p_playlist_id
      ORDER BY position
    ) t;

    RETURN json_build_object('ok', true, 'noop', true, 'order', v_rows);
  END IF;

  -- Crear tabla temporal con el orden actual
  CREATE TEMP TABLE tmp_reorder AS
  SELECT id, track_id, ROW_NUMBER() OVER (ORDER BY position) AS rn
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  -- Mover a posiciones negativas temporales (basadas en el nuevo orden)
  IF p_new_index < p_old_index THEN
    -- Mover hacia arriba: el movido va a new_index, los demas bajan
    UPDATE playlist_tracks pt
    SET position = CASE
      WHEN t.rn = p_old_index THEN -p_new_index
      WHEN t.rn >= p_new_index AND t.rn < p_old_index THEN -(t.rn + 1)
      ELSE -t.rn
    END
    FROM tmp_reorder t
    WHERE pt.id = t.id;
  ELSE
    -- Mover hacia abajo: el movido va a new_index, los demas suben
    UPDATE playlist_tracks pt
    SET position = CASE
      WHEN t.rn = p_old_index THEN -p_new_index
      WHEN t.rn > p_old_index AND t.rn <= p_new_index THEN -(t.rn - 1)
      ELSE -t.rn
    END
    FROM tmp_reorder t
    WHERE pt.id = t.id;
  END IF;

  -- Convertir posiciones negativas a positivas
  UPDATE playlist_tracks
  SET position = -position
  WHERE playlist_id = p_playlist_id AND position < 0;

  -- Limpiar
  DROP TABLE tmp_reorder;

  -- Bump updated_at del padre: paso dentro del MISMO RPC, asi es atomico
  -- con el reorder. Si el reorder commitea, esto commitea; si falla, ambos.
  UPDATE playlists SET updated_at = now() WHERE id = p_playlist_id;

  -- Devolver orden final
  SELECT json_agg(row_to_json(t) ORDER BY t.position)
  INTO v_rows
  FROM (
    SELECT id AS "internalId", track_id AS "trackId", position
    FROM playlist_tracks
    WHERE playlist_id = p_playlist_id
    ORDER BY position
  ) t;

  RETURN json_build_object('ok', true, 'order', v_rows);

EXCEPTION WHEN OTHERS THEN
  RETURN json_build_object('ok', false, 'error', SQLERRM);
END;
$$;


ALTER FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer) OWNER TO postgres;

--
-- Name: playlist_tracks_reorder(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.playlist_tracks_reorder() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF pg_trigger_depth() > 1 THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
  END IF;

  IF TG_OP = 'INSERT' THEN
    -- posición nula -> al final (seteo directo en NEW, sin UPDATE por id)
    IF NEW.position IS NULL THEN
      SELECT COALESCE(MAX(position),0) + 1
        INTO NEW.position
        FROM public.playlist_tracks
       WHERE playlist_id = NEW.playlist_id;
      RETURN NEW;
    END IF;

    -- insertar en P -> correr a derecha todos los >= P (la fila aún no existe en tabla)
    UPDATE public.playlist_tracks
       SET position = position + 1
     WHERE playlist_id = NEW.playlist_id
       AND position >= NEW.position;

    RETURN NEW;

  ELSIF TG_OP = 'UPDATE' THEN
    IF NEW.playlist_id = OLD.playlist_id AND NEW.position = OLD.position THEN
      RETURN NEW;
    END IF;

    IF NEW.playlist_id = OLD.playlist_id THEN
      -- mover dentro de la misma playlist
      IF NEW.position IS NULL THEN
        SELECT COALESCE(MAX(position),0) + 1
          INTO NEW.position
          FROM public.playlist_tracks
         WHERE playlist_id = NEW.playlist_id;
        RETURN NEW;
      END IF;

      IF NEW.position < OLD.position THEN
        UPDATE public.playlist_tracks
           SET position = position + 1
         WHERE playlist_id = NEW.playlist_id
           AND ctid <> NEW.ctid
           AND position >= NEW.position
           AND position <  OLD.position;
      ELSE
        UPDATE public.playlist_tracks
           SET position = position - 1
         WHERE playlist_id = NEW.playlist_id
           AND ctid <> NEW.ctid
           AND position <= NEW.position
           AND position >  OLD.position;
      END IF;

      RETURN NEW;

    ELSE
      -- cambio de playlist: cerrar hueco en la vieja
      UPDATE public.playlist_tracks
         SET position = position - 1
       WHERE playlist_id = OLD.playlist_id
         AND position > OLD.position;

      -- decidir posición en la nueva
      IF NEW.position IS NULL THEN
        SELECT COALESCE(MAX(position),0) + 1
          INTO NEW.position
          FROM public.playlist_tracks
         WHERE playlist_id = NEW.playlist_id;
      ELSE
        UPDATE public.playlist_tracks
           SET position = position + 1
         WHERE playlist_id = NEW.playlist_id
           AND position >= NEW.position;
      END IF;

      RETURN NEW;
    END IF;

  ELSIF TG_OP = 'DELETE' THEN
    UPDATE public.playlist_tracks
       SET position = position - 1
     WHERE playlist_id = OLD.playlist_id
       AND position > OLD.position;
    RETURN OLD;
  END IF;

  RETURN NEW;
END
$$;


ALTER FUNCTION public.playlist_tracks_reorder() OWNER TO postgres;

--
-- Name: prevent_role_self_update(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.prevent_role_self_update() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare
    caller_uid uuid;
begin
    if new.role = old.role then
        return new;
    end if;

    caller_uid := auth.uid();

    if caller_uid is null then
        return new;
    end if;

    if not public.is_admin() then
        raise exception 'Only admins can change roles';
    end if;

    return new;
end;
$$;


ALTER FUNCTION public.prevent_role_self_update() OWNER TO postgres;

--
-- Name: purge_old_data(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.purge_old_data() RETURNS void
    LANGUAGE plpgsql
    AS $$
declare
  play_count int;
  cutoff_date timestamptz;
begin
  select count(*) into play_count from play_events;

  if play_count > 450000 then
    select played_at into cutoff_date
    from play_events
    order by played_at desc
    offset 360000 limit 1;

    delete from play_events where played_at < cutoff_date;
  end if;
end;
$$;


ALTER FUNCTION public.purge_old_data() OWNER TO postgres;

--
-- Name: recommend_playlists_by_history(uuid, integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.recommend_playlists_by_history(p_user_id uuid, p_days integer DEFAULT 45, p_limit integer DEFAULT 5) RETURNS TABLE(playlist_id uuid, playlist_title text, total_tracks integer, matching_tracks bigint, matching_artists bigint, thumbnails jsonb)
    LANGUAGE sql
    AS $$
  WITH top_artists AS (
    SELECT
      (a.value->>'id') AS artist_id,
      COUNT(*) AS play_count
    FROM play_events pe
    CROSS JOIN LATERAL jsonb_array_elements(pe.metadata->'artists') AS a
    WHERE pe.user_id = p_user_id
      AND pe.played_at >= now() - (p_days || ' days')::interval
      AND a.value->>'id' IS NOT NULL
    GROUP BY (a.value->>'id')
    ORDER BY play_count DESC
    LIMIT 20
  ),
  track_artists AS (
    SELECT
      t.track_id,
      (a.value->>'id') AS artist_id
    FROM tracks t
    CROSS JOIN LATERAL jsonb_array_elements(t.artists) AS a
    WHERE a.value->>'id' IS NOT NULL
  ),
  matched_tracks AS (
    SELECT
      gpt.playlist_id,
      ta_track.track_id,
      ta_track.artist_id
    FROM top_artists ta
    JOIN track_artists ta_track ON ta_track.artist_id = ta.artist_id
    JOIN genre_playlist_tracks gpt ON gpt.track_id = ta_track.track_id
  ),
  ranked AS (
    SELECT
      m.playlist_id,
      gp.title AS playlist_title,
      gp.track_count AS total_tracks,
      COUNT(DISTINCT m.track_id) AS matching_tracks,
      COUNT(DISTINCT m.artist_id) AS matching_artists
    FROM matched_tracks m
    JOIN genre_playlists gp ON gp.id = m.playlist_id
    GROUP BY m.playlist_id, gp.title, gp.track_count
    ORDER BY COUNT(DISTINCT m.artist_id) DESC, COUNT(DISTINCT m.track_id) DESC
    LIMIT p_limit
  ),
  thumbs AS (
    SELECT
      gpt.playlist_id,
      jsonb_agg(t.thumbnail_url ORDER BY gpt.position) AS thumbnails
    FROM genre_playlist_tracks gpt
    JOIN tracks t ON t.track_id = gpt.track_id
    WHERE gpt.playlist_id IN (SELECT playlist_id FROM ranked)
      AND gpt.position <= 4
      AND t.thumbnail_url IS NOT NULL
    GROUP BY gpt.playlist_id
  )
  SELECT
    r.playlist_id,
    r.playlist_title,
    r.total_tracks,
    r.matching_tracks,
    r.matching_artists,
    COALESCE(th.thumbnails, '[]'::jsonb) AS thumbnails
  FROM ranked r
  LEFT JOIN thumbs th ON th.playlist_id = r.playlist_id;
$$;


ALTER FUNCTION public.recommend_playlists_by_history(p_user_id uuid, p_days integer, p_limit integer) OWNER TO postgres;

--
-- Name: remove_playlist_track(uuid, text); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.remove_playlist_track(p_playlist_id uuid, p_track_id text) RETURNS json
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_deleted int;
BEGIN
  -- serialize writers on this playlist (write protocol, see header)
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  DELETE FROM playlist_tracks pt
  USING tracks t
  WHERE pt.playlist_id = p_playlist_id
    AND t.id = pt.track_id
    AND t.track_id = p_track_id;

  GET DIAGNOSTICS v_deleted = ROW_COUNT;

  RETURN json_build_object('ok', true, 'deleted', v_deleted);
END;
$$;


ALTER FUNCTION public.remove_playlist_track(p_playlist_id uuid, p_track_id text) OWNER TO postgres;

--
-- Name: update_genre_playlist_track_count(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.update_genre_playlist_track_count() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    UPDATE genre_playlists 
    SET track_count = track_count + 1,
        updated_at = now()
    WHERE id = NEW.playlist_id;
  ELSIF TG_OP = 'DELETE' THEN
    UPDATE genre_playlists 
    SET track_count = GREATEST(track_count - 1, 0),
        updated_at = now()
    WHERE id = OLD.playlist_id;
  END IF;
  RETURN NULL;
END;
$$;


ALTER FUNCTION public.update_genre_playlist_track_count() OWNER TO postgres;

--
-- Name: update_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.update_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;


ALTER FUNCTION public.update_updated_at() OWNER TO postgres;

--
-- Name: update_updated_at_column(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.update_updated_at_column() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


ALTER FUNCTION public.update_updated_at_column() OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: bug_reports; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.bug_reports (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    reporter_id uuid NOT NULL,
    category text NOT NULL,
    description text NOT NULL,
    entity_type text,
    entity_id text,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT bug_reports_category_check CHECK ((category = ANY (ARRAY['playback'::text, 'loading'::text, 'ui'::text, 'crash'::text, 'other'::text]))),
    CONSTRAINT bug_reports_description_length CHECK (((char_length(description) >= 5) AND (char_length(description) <= 2000))),
    CONSTRAINT bug_reports_entity_consistency CHECK ((((entity_type IS NULL) AND (entity_id IS NULL)) OR ((entity_type IS NOT NULL) AND (entity_id IS NOT NULL)))),
    CONSTRAINT bug_reports_entity_type_check CHECK (((entity_type IS NULL) OR (entity_type = ANY (ARRAY['track'::text, 'album'::text, 'artist'::text, 'playlist'::text])))),
    CONSTRAINT bug_reports_status_check CHECK ((status = ANY (ARRAY['open'::text, 'closed'::text])))
);


ALTER TABLE public.bug_reports OWNER TO postgres;

--
-- Name: TABLE bug_reports; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.bug_reports IS 'Bug reports submitted by tester+ users';


--
-- Name: COLUMN bug_reports.entity_type; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.bug_reports.entity_type IS 'Optional: type of entity the bug is about (track, album, etc.)';


--
-- Name: COLUMN bug_reports.entity_id; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.bug_reports.entity_id IS 'Optional: id of the specific entity';


--
-- Name: error_logs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.error_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    track_id text NOT NULL,
    platform text NOT NULL,
    os_version text,
    stage text NOT NULL,
    error_message text,
    error_code text,
    http_status integer,
    resolved boolean DEFAULT false,
    itag integer,
    mime_type text,
    bitrate integer,
    audio_quality text,
    source text,
    client_name text,
    client_version text,
    visitor_data_used boolean,
    retried boolean,
    playability_status text,
    resolve_duration_ms integer,
    cache_age_ms integer,
    audio_url_host text,
    audio_url_expire bigint,
    audio_url_client text,
    audio_url_mirror text,
    adaptive_format_count integer,
    audio_format_count integer,
    audio_formats_without_url integer,
    urls_withheld boolean,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT error_logs_platform_check CHECK ((platform = ANY (ARRAY['android'::text, 'ios'::text]))),
    CONSTRAINT error_logs_source_check CHECK (((source IS NULL) OR (source = ANY (ARRAY['offline'::text, 'cache'::text, 'fresh'::text, 'fresh-retry'::text])))),
    CONSTRAINT error_logs_stage_check CHECK ((stage = ANY (ARRAY['resolve'::text, 'playback'::text])))
);


ALTER TABLE public.error_logs OWNER TO postgres;

--
-- Name: feed_current; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.feed_current (
    uid uuid DEFAULT gen_random_uuid() NOT NULL,
    kind public.feed_kind NOT NULL,
    type public.media_type NOT NULL,
    id text NOT NULL,
    title text NOT NULL,
    artists jsonb NOT NULL,
    album text,
    album_id text,
    duration_seconds integer,
    year text,
    thumb text,
    release_date text,
    CONSTRAINT feed_current_artists_is_array CHECK ((jsonb_typeof(artists) = 'array'::text)),
    CONSTRAINT feed_current_artists_not_empty CHECK ((jsonb_array_length(artists) > 0))
);


ALTER TABLE public.feed_current OWNER TO postgres;

--
-- Name: genre_playlist_tracks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.genre_playlist_tracks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    playlist_id uuid NOT NULL,
    track_id text NOT NULL,
    "position" integer NOT NULL,
    added_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.genre_playlist_tracks OWNER TO postgres;

--
-- Name: genre_playlists; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.genre_playlists (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    genre_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    thumbnail_url text,
    sort_order integer DEFAULT 0 NOT NULL,
    track_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    category text
);


ALTER TABLE public.genre_playlists OWNER TO postgres;

--
-- Name: genres; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.genres (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    description text,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.genres OWNER TO postgres;

--
-- Name: library_items; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.library_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    kind text NOT NULL,
    external_id text NOT NULL,
    title text NOT NULL,
    thumbnail_url text DEFAULT ''::text NOT NULL,
    artist text DEFAULT ''::text NOT NULL,
    artist_id text DEFAULT ''::text NOT NULL,
    album_id text DEFAULT ''::text NOT NULL,
    album_name text DEFAULT ''::text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source text NOT NULL,
    CONSTRAINT library_items_kind_check CHECK ((kind = ANY (ARRAY['album'::text, 'playlist'::text]))),
    CONSTRAINT library_items_source_check CHECK ((source = ANY (ARRAY['genre'::text, 'replay'::text, 'presenting'::text, 'external'::text])))
);


ALTER TABLE public.library_items OWNER TO postgres;

--
-- Name: play_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.play_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    track_id text NOT NULL,
    played_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE public.play_events OWNER TO postgres;

--
-- Name: playlist_tracks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.playlist_tracks (
    playlist_id uuid NOT NULL,
    track_id uuid NOT NULL,
    "position" integer NOT NULL,
    added_by uuid,
    added_at timestamp with time zone DEFAULT now(),
    id uuid DEFAULT gen_random_uuid() NOT NULL
);


ALTER TABLE public.playlist_tracks OWNER TO postgres;

--
-- Name: playlists; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.playlists (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    is_public boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.playlists OWNER TO postgres;

--
-- Name: profiles; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.profiles (
    id uuid NOT NULL,
    username text,
    display_name text,
    avatar_url text,
    role text DEFAULT 'user'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT profiles_display_name_length CHECK (((display_name IS NULL) OR ((char_length(display_name) >= 1) AND (char_length(display_name) <= 50)))),
    CONSTRAINT profiles_role_check CHECK ((role = ANY (ARRAY['admin'::text, 'developer'::text, 'tester'::text, 'user'::text]))),
    CONSTRAINT profiles_username_format CHECK (((username IS NULL) OR (username ~ '^[a-zA-Z0-9_]+$'::text))),
    CONSTRAINT profiles_username_length CHECK (((username IS NULL) OR ((char_length(username) >= 3) AND (char_length(username) <= 30))))
);


ALTER TABLE public.profiles OWNER TO postgres;

--
-- Name: TABLE profiles; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE public.profiles IS 'User profiles, extends auth.users';


--
-- Name: COLUMN profiles.role; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN public.profiles.role IS 'Hierarchical: admin > developer > tester > user';


--
-- Name: recent_activity; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.recent_activity (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    played_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recent_activity_entity_type_check CHECK ((entity_type = ANY (ARRAY['album'::text, 'artist'::text, 'playlist'::text])))
);


ALTER TABLE public.recent_activity OWNER TO postgres;

--
-- Name: release_sync_errors; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.release_sync_errors (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    artist_name text NOT NULL,
    album_name text NOT NULL,
    error_type text NOT NULL,
    error_detail text,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.release_sync_errors OWNER TO postgres;

--
-- Name: tracks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tracks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    track_id text NOT NULL,
    title text NOT NULL,
    artists jsonb NOT NULL,
    album text NOT NULL,
    album_id text NOT NULL,
    duration_seconds integer NOT NULL,
    thumbnail_url text NOT NULL,
    CONSTRAINT tracks_artists_is_array CHECK ((jsonb_typeof(artists) = 'array'::text)),
    CONSTRAINT tracks_artists_not_empty CHECK ((jsonb_array_length(artists) > 0))
);


ALTER TABLE public.tracks OWNER TO postgres;

--
-- Name: upcoming_releases; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.upcoming_releases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    artist_name text NOT NULL,
    artist_id text,
    album_name text NOT NULL,
    album_id text,
    release_date date,
    is_tba boolean DEFAULT false,
    label text,
    genre text,
    thumbnail_url text,
    track_count integer,
    status text DEFAULT 'pending'::text NOT NULL,
    enrichment_failed boolean DEFAULT false,
    confirmed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT valid_status CHECK ((status = ANY (ARRAY['pending'::text, 'released'::text, 'postponed'::text])))
);


ALTER TABLE public.upcoming_releases OWNER TO postgres;

--
-- Name: user_likes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.user_likes (
    user_id uuid NOT NULL,
    track_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


ALTER TABLE public.user_likes OWNER TO postgres;

--
-- Name: user_monthly_stats; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.user_monthly_stats (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    month date NOT NULL,
    entity_type public.app_entity_type NOT NULL,
    stats jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.user_monthly_stats OWNER TO postgres;

--
-- Name: user_recommendations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.user_recommendations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    bucket text NOT NULL,
    item_type public.app_entity_type NOT NULL,
    item_id text NOT NULL,
    rank smallint NOT NULL,
    seed_type public.app_entity_type,
    seed_id text,
    seed_name text,
    seed_thumb text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    source text,
    generated_at timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone
);


ALTER TABLE public.user_recommendations OWNER TO postgres;

--
-- Name: user_weekly_stats; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.user_weekly_stats (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    week_start date NOT NULL,
    entity_type public.app_entity_type NOT NULL,
    stats jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.user_weekly_stats OWNER TO postgres;

--
-- Name: bug_reports bug_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bug_reports
    ADD CONSTRAINT bug_reports_pkey PRIMARY KEY (id);


--
-- Name: error_logs error_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.error_logs
    ADD CONSTRAINT error_logs_pkey PRIMARY KEY (id);


--
-- Name: feed_current feed_current_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.feed_current
    ADD CONSTRAINT feed_current_pkey PRIMARY KEY (uid);


--
-- Name: genre_playlist_tracks genre_playlist_tracks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genre_playlist_tracks
    ADD CONSTRAINT genre_playlist_tracks_pkey PRIMARY KEY (id);


--
-- Name: genre_playlists genre_playlists_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genre_playlists
    ADD CONSTRAINT genre_playlists_pkey PRIMARY KEY (id);


--
-- Name: genres genres_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genres
    ADD CONSTRAINT genres_name_key UNIQUE (name);


--
-- Name: genres genres_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genres
    ADD CONSTRAINT genres_pkey PRIMARY KEY (id);


--
-- Name: genres genres_slug_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genres
    ADD CONSTRAINT genres_slug_key UNIQUE (slug);


--
-- Name: library_items library_items_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.library_items
    ADD CONSTRAINT library_items_pkey PRIMARY KEY (id);


--
-- Name: library_items library_items_unique_per_user; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.library_items
    ADD CONSTRAINT library_items_unique_per_user UNIQUE (user_id, kind, external_id);


--
-- Name: play_events play_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.play_events
    ADD CONSTRAINT play_events_pkey PRIMARY KEY (id);


--
-- Name: playlist_tracks playlist_tracks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playlist_tracks
    ADD CONSTRAINT playlist_tracks_pkey PRIMARY KEY (id);


--
-- Name: playlists playlists_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playlists
    ADD CONSTRAINT playlists_pkey PRIMARY KEY (id);


--
-- Name: profiles profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_pkey PRIMARY KEY (id);


--
-- Name: profiles profiles_username_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_username_key UNIQUE (username);


--
-- Name: recent_activity recent_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.recent_activity
    ADD CONSTRAINT recent_activity_pkey PRIMARY KEY (id);


--
-- Name: recent_activity recent_activity_unique; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.recent_activity
    ADD CONSTRAINT recent_activity_unique UNIQUE (user_id, entity_type, entity_id);


--
-- Name: release_sync_errors release_sync_errors_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.release_sync_errors
    ADD CONSTRAINT release_sync_errors_pkey PRIMARY KEY (id);


--
-- Name: tracks tracks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tracks
    ADD CONSTRAINT tracks_pkey PRIMARY KEY (id);


--
-- Name: tracks tracks_track_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tracks
    ADD CONSTRAINT tracks_track_id_key UNIQUE (track_id);


--
-- Name: genre_playlist_tracks unique_playlist_position; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genre_playlist_tracks
    ADD CONSTRAINT unique_playlist_position UNIQUE (playlist_id, "position");


--
-- Name: genre_playlist_tracks unique_playlist_track; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genre_playlist_tracks
    ADD CONSTRAINT unique_playlist_track UNIQUE (playlist_id, track_id);


--
-- Name: upcoming_releases upcoming_releases_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.upcoming_releases
    ADD CONSTRAINT upcoming_releases_pkey PRIMARY KEY (id);


--
-- Name: upcoming_releases uq_artist_album; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.upcoming_releases
    ADD CONSTRAINT uq_artist_album UNIQUE (artist_name, album_name);


--
-- Name: user_likes user_likes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_likes
    ADD CONSTRAINT user_likes_pkey PRIMARY KEY (user_id, track_id);


--
-- Name: user_monthly_stats user_monthly_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_monthly_stats
    ADD CONSTRAINT user_monthly_stats_pkey PRIMARY KEY (id);


--
-- Name: user_monthly_stats user_monthly_stats_unique; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_monthly_stats
    ADD CONSTRAINT user_monthly_stats_unique UNIQUE (user_id, month, entity_type);


--
-- Name: user_recommendations user_recommendations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_recommendations
    ADD CONSTRAINT user_recommendations_pkey PRIMARY KEY (id);


--
-- Name: user_weekly_stats user_weekly_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_weekly_stats
    ADD CONSTRAINT user_weekly_stats_pkey PRIMARY KEY (id);


--
-- Name: user_weekly_stats user_weekly_stats_unique; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_weekly_stats
    ADD CONSTRAINT user_weekly_stats_unique UNIQUE (user_id, week_start, entity_type);


--
-- Name: playlist_tracks ux_playlist_pos; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playlist_tracks
    ADD CONSTRAINT ux_playlist_pos UNIQUE (playlist_id, "position") DEFERRABLE INITIALLY DEFERRED;


--
-- Name: playlist_tracks ux_playlist_track; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playlist_tracks
    ADD CONSTRAINT ux_playlist_track UNIQUE (playlist_id, track_id);


--
-- Name: bug_reports_created_at_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX bug_reports_created_at_idx ON public.bug_reports USING btree (created_at DESC);


--
-- Name: bug_reports_reporter_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX bug_reports_reporter_idx ON public.bug_reports USING btree (reporter_id);


--
-- Name: bug_reports_status_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX bug_reports_status_idx ON public.bug_reports USING btree (status);


--
-- Name: feed_current_artists_gin; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX feed_current_artists_gin ON public.feed_current USING gin (artists jsonb_path_ops);


--
-- Name: idx_error_logs_client_version; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_error_logs_client_version ON public.error_logs USING btree (client_version, created_at DESC);


--
-- Name: idx_error_logs_platform; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_error_logs_platform ON public.error_logs USING btree (platform, created_at DESC);


--
-- Name: idx_error_logs_stage_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_error_logs_stage_status ON public.error_logs USING btree (stage, http_status, created_at DESC);


--
-- Name: idx_error_logs_track; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_error_logs_track ON public.error_logs USING btree (track_id);


--
-- Name: idx_error_logs_urls_withheld; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_error_logs_urls_withheld ON public.error_logs USING btree (created_at DESC) WHERE (urls_withheld = true);


--
-- Name: idx_feed_current_kind_type; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_feed_current_kind_type ON public.feed_current USING btree (kind, type);


--
-- Name: idx_feed_current_release; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_feed_current_release ON public.feed_current USING btree (release_date DESC);


--
-- Name: idx_genre_playlist_tracks_playlist_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_genre_playlist_tracks_playlist_id ON public.genre_playlist_tracks USING btree (playlist_id);


--
-- Name: idx_genre_playlist_tracks_position; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_genre_playlist_tracks_position ON public.genre_playlist_tracks USING btree (playlist_id, "position");


--
-- Name: idx_genre_playlist_tracks_track_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_genre_playlist_tracks_track_id ON public.genre_playlist_tracks USING btree (track_id);


--
-- Name: idx_genre_playlists_genre_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_genre_playlists_genre_id ON public.genre_playlists USING btree (genre_id);


--
-- Name: idx_genre_playlists_sort_order; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_genre_playlists_sort_order ON public.genre_playlists USING btree (sort_order);


--
-- Name: idx_library_items_user; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_library_items_user ON public.library_items USING btree (user_id, added_at DESC);


--
-- Name: idx_library_items_user_kind; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_library_items_user_kind ON public.library_items USING btree (user_id, kind, added_at DESC);


--
-- Name: idx_playlisttracks_playlist_pos; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_playlisttracks_playlist_pos ON public.playlist_tracks USING btree (playlist_id, "position");


--
-- Name: idx_releases_artist_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_releases_artist_name ON public.upcoming_releases USING btree (artist_name);


--
-- Name: idx_releases_date; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_releases_date ON public.upcoming_releases USING btree (release_date) WHERE (release_date IS NOT NULL);


--
-- Name: idx_releases_status_pending; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_releases_status_pending ON public.upcoming_releases USING btree (status) WHERE (status = 'pending'::text);


--
-- Name: idx_releases_tba; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_releases_tba ON public.upcoming_releases USING btree (is_tba) WHERE (is_tba = true);


--
-- Name: idx_sync_errors_type; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_sync_errors_type ON public.release_sync_errors USING btree (error_type);


--
-- Name: idx_user_likes_sync; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_user_likes_sync ON public.user_likes USING btree (user_id, updated_at);


--
-- Name: idx_user_likes_user_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_user_likes_user_created ON public.user_likes USING btree (user_id, created_at DESC);


--
-- Name: pe_user_time_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX pe_user_time_idx ON public.play_events USING btree (user_id, played_at DESC);


--
-- Name: pe_user_track_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX pe_user_track_idx ON public.play_events USING btree (user_id, track_id);


--
-- Name: profiles_role_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX profiles_role_idx ON public.profiles USING btree (role);


--
-- Name: ra_user_recent_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ra_user_recent_idx ON public.recent_activity USING btree (user_id, played_at DESC);


--
-- Name: tracks_artists_gin; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX tracks_artists_gin ON public.tracks USING gin (artists jsonb_path_ops);


--
-- Name: ums_user_month_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ums_user_month_idx ON public.user_monthly_stats USING btree (user_id, month DESC);


--
-- Name: ums_user_type_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ums_user_type_idx ON public.user_monthly_stats USING btree (user_id, entity_type, month DESC);


--
-- Name: uniq_feed_current_kind_type_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uniq_feed_current_kind_type_id ON public.feed_current USING btree (kind, type, id);


--
-- Name: user_recommendations_list_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX user_recommendations_list_idx ON public.user_recommendations USING btree (user_id, bucket, rank);


--
-- Name: user_recommendations_type_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX user_recommendations_type_idx ON public.user_recommendations USING btree (user_id, bucket, item_type, rank);


--
-- Name: user_recommendations_unique; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX user_recommendations_unique ON public.user_recommendations USING btree (user_id, bucket, item_type, item_id);


--
-- Name: user_recommendations_valid_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX user_recommendations_valid_idx ON public.user_recommendations USING btree (valid_until);


--
-- Name: uws_user_type_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX uws_user_type_idx ON public.user_weekly_stats USING btree (user_id, entity_type, week_start DESC);


--
-- Name: uws_user_week_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX uws_user_week_idx ON public.user_weekly_stats USING btree (user_id, week_start DESC);


--
-- Name: bug_reports bug_reports_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER bug_reports_updated_at BEFORE UPDATE ON public.bug_reports FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();


--
-- Name: profiles enforce_role_change_permission; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER enforce_role_change_permission BEFORE UPDATE ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.prevent_role_self_update();


--
-- Name: library_items library_items_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER library_items_updated_at BEFORE UPDATE ON public.library_items FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();


--
-- Name: profiles on_profile_updated; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER on_profile_updated BEFORE UPDATE ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.handle_profile_updated_at();


--
-- Name: upcoming_releases releases_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER releases_updated_at BEFORE UPDATE ON public.upcoming_releases FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();


--
-- Name: playlist_tracks trg_bump_playlist_on_track_change; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER trg_bump_playlist_on_track_change AFTER INSERT OR DELETE OR UPDATE ON public.playlist_tracks FOR EACH ROW EXECUTE FUNCTION public.bump_playlist_on_track_change();


--
-- Name: playlists trg_bump_playlist_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER trg_bump_playlist_updated_at BEFORE UPDATE ON public.playlists FOR EACH ROW EXECUTE FUNCTION public.bump_playlist_updated_at();


--
-- Name: playlists trg_cleanup_library_on_playlist_delete; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER trg_cleanup_library_on_playlist_delete AFTER DELETE ON public.playlists FOR EACH ROW EXECUTE FUNCTION public.cleanup_library_on_playlist_delete();


--
-- Name: playlist_tracks trg_playlist_tracks_reorder; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER trg_playlist_tracks_reorder BEFORE INSERT OR DELETE OR UPDATE ON public.playlist_tracks FOR EACH ROW EXECUTE FUNCTION public.playlist_tracks_reorder();

ALTER TABLE public.playlist_tracks DISABLE TRIGGER trg_playlist_tracks_reorder;


--
-- Name: genre_playlist_tracks trigger_update_genre_playlist_track_count; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER trigger_update_genre_playlist_track_count AFTER INSERT OR DELETE ON public.genre_playlist_tracks FOR EACH ROW EXECUTE FUNCTION public.update_genre_playlist_track_count();


--
-- Name: genre_playlists update_genre_playlists_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER update_genre_playlists_updated_at BEFORE UPDATE ON public.genre_playlists FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: user_likes user_likes_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER user_likes_updated_at BEFORE UPDATE ON public.user_likes FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();


--
-- Name: bug_reports bug_reports_reporter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bug_reports
    ADD CONSTRAINT bug_reports_reporter_id_fkey FOREIGN KEY (reporter_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: bug_reports bug_reports_reporter_id_profiles_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bug_reports
    ADD CONSTRAINT bug_reports_reporter_id_profiles_fkey FOREIGN KEY (reporter_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: error_logs error_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.error_logs
    ADD CONSTRAINT error_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id);


--
-- Name: genre_playlist_tracks genre_playlist_tracks_playlist_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genre_playlist_tracks
    ADD CONSTRAINT genre_playlist_tracks_playlist_id_fkey FOREIGN KEY (playlist_id) REFERENCES public.genre_playlists(id) ON DELETE CASCADE;


--
-- Name: genre_playlist_tracks genre_playlist_tracks_track_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genre_playlist_tracks
    ADD CONSTRAINT genre_playlist_tracks_track_id_fkey FOREIGN KEY (track_id) REFERENCES public.tracks(track_id) ON DELETE CASCADE;


--
-- Name: genre_playlists genre_playlists_genre_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.genre_playlists
    ADD CONSTRAINT genre_playlists_genre_id_fkey FOREIGN KEY (genre_id) REFERENCES public.genres(id) ON DELETE CASCADE;


--
-- Name: library_items library_items_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.library_items
    ADD CONSTRAINT library_items_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: play_events play_events_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.play_events
    ADD CONSTRAINT play_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: playlist_tracks playlist_tracks_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playlist_tracks
    ADD CONSTRAINT playlist_tracks_added_by_fkey FOREIGN KEY (added_by) REFERENCES auth.users(id);


--
-- Name: playlist_tracks playlist_tracks_playlist_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playlist_tracks
    ADD CONSTRAINT playlist_tracks_playlist_id_fkey FOREIGN KEY (playlist_id) REFERENCES public.playlists(id) ON DELETE CASCADE;


--
-- Name: playlist_tracks playlist_tracks_track_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playlist_tracks
    ADD CONSTRAINT playlist_tracks_track_id_fkey FOREIGN KEY (track_id) REFERENCES public.tracks(id) ON DELETE CASCADE;


--
-- Name: playlists playlists_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playlists
    ADD CONSTRAINT playlists_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: profiles profiles_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_id_fkey FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: recent_activity recent_activity_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.recent_activity
    ADD CONSTRAINT recent_activity_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: user_likes user_likes_track_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_likes
    ADD CONSTRAINT user_likes_track_id_fkey FOREIGN KEY (track_id) REFERENCES public.tracks(track_id) ON DELETE CASCADE;


--
-- Name: user_likes user_likes_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_likes
    ADD CONSTRAINT user_likes_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: user_monthly_stats user_monthly_stats_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_monthly_stats
    ADD CONSTRAINT user_monthly_stats_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: user_recommendations user_recommendations_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_recommendations
    ADD CONSTRAINT user_recommendations_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: user_weekly_stats user_weekly_stats_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_weekly_stats
    ADD CONSTRAINT user_weekly_stats_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: bug_reports Admins can delete reports; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Admins can delete reports" ON public.bug_reports FOR DELETE USING (public.is_admin());


--
-- Name: profiles Admins can update all profiles; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Admins can update all profiles" ON public.profiles FOR UPDATE USING (public.is_admin());


--
-- Name: bug_reports Developers and admins can update reports; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Developers and admins can update reports" ON public.bug_reports FOR UPDATE USING (public.is_developer_or_higher());


--
-- Name: profiles Developers and admins can view all profiles; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Developers and admins can view all profiles" ON public.profiles FOR SELECT USING (public.is_developer_or_higher());


--
-- Name: bug_reports Developers and admins can view all reports; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Developers and admins can view all reports" ON public.bug_reports FOR SELECT USING (public.is_developer_or_higher());


--
-- Name: bug_reports Testers and above can create reports; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Testers and above can create reports" ON public.bug_reports FOR INSERT WITH CHECK (((auth.uid() = reporter_id) AND public.is_tester_or_higher()));


--
-- Name: profiles Users can update own profile; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Users can update own profile" ON public.profiles FOR UPDATE USING ((auth.uid() = id)) WITH CHECK ((auth.uid() = id));


--
-- Name: profiles Users can view own profile; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Users can view own profile" ON public.profiles FOR SELECT USING ((auth.uid() = id));


--
-- Name: bug_reports Users can view own reports; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "Users can view own reports" ON public.bug_reports FOR SELECT USING ((auth.uid() = reporter_id));


--
-- Name: bug_reports; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.bug_reports ENABLE ROW LEVEL SECURITY;

--
-- Name: error_logs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.error_logs ENABLE ROW LEVEL SECURITY;

--
-- Name: feed_current; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.feed_current ENABLE ROW LEVEL SECURITY;

--
-- Name: genre_playlist_tracks; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.genre_playlist_tracks ENABLE ROW LEVEL SECURITY;

--
-- Name: genre_playlist_tracks genre_playlist_tracks readable by authenticated; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "genre_playlist_tracks readable by authenticated" ON public.genre_playlist_tracks FOR SELECT TO authenticated USING (true);


--
-- Name: genre_playlists; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.genre_playlists ENABLE ROW LEVEL SECURITY;

--
-- Name: genre_playlists genre_playlists readable by authenticated; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "genre_playlists readable by authenticated" ON public.genre_playlists FOR SELECT TO authenticated USING (true);


--
-- Name: genres; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.genres ENABLE ROW LEVEL SECURITY;

--
-- Name: genres genres readable by authenticated; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "genres readable by authenticated" ON public.genres FOR SELECT TO authenticated USING (true);


--
-- Name: library_items; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.library_items ENABLE ROW LEVEL SECURITY;

--
-- Name: library_items library_items deletable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "library_items deletable by owner" ON public.library_items FOR DELETE TO authenticated USING ((auth.uid() = user_id));


--
-- Name: library_items library_items insertable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "library_items insertable by owner" ON public.library_items FOR INSERT TO authenticated WITH CHECK ((auth.uid() = user_id));


--
-- Name: library_items library_items readable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "library_items readable by owner" ON public.library_items FOR SELECT TO authenticated USING ((auth.uid() = user_id));


--
-- Name: library_items library_items updatable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "library_items updatable by owner" ON public.library_items FOR UPDATE TO authenticated USING ((auth.uid() = user_id));


--
-- Name: play_events; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.play_events ENABLE ROW LEVEL SECURITY;

--
-- Name: play_events play_events readable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "play_events readable by owner" ON public.play_events FOR SELECT TO authenticated USING ((user_id = auth.uid()));


--
-- Name: playlist_tracks; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.playlist_tracks ENABLE ROW LEVEL SECURITY;

--
-- Name: playlist_tracks playlist_tracks manageable by playlist owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "playlist_tracks manageable by playlist owner" ON public.playlist_tracks TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.playlists p
  WHERE ((p.id = playlist_tracks.playlist_id) AND (p.owner_id = auth.uid()))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.playlists p
  WHERE ((p.id = playlist_tracks.playlist_id) AND (p.owner_id = auth.uid())))));


--
-- Name: playlist_tracks playlist_tracks readable by playlist visibility; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "playlist_tracks readable by playlist visibility" ON public.playlist_tracks FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.playlists p
  WHERE ((p.id = playlist_tracks.playlist_id) AND (p.is_public OR (p.owner_id = auth.uid()))))));


--
-- Name: playlists; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.playlists ENABLE ROW LEVEL SECURITY;

--
-- Name: playlists playlists deletable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "playlists deletable by owner" ON public.playlists FOR DELETE TO authenticated USING ((owner_id = auth.uid()));


--
-- Name: playlists playlists insertable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "playlists insertable by owner" ON public.playlists FOR INSERT TO authenticated WITH CHECK ((owner_id = auth.uid()));


--
-- Name: playlists playlists readable by owner or public; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "playlists readable by owner or public" ON public.playlists FOR SELECT TO authenticated USING ((is_public OR (owner_id = auth.uid())));


--
-- Name: playlists playlists updatable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "playlists updatable by owner" ON public.playlists FOR UPDATE TO authenticated USING ((owner_id = auth.uid()));


--
-- Name: profiles; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

--
-- Name: recent_activity; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.recent_activity ENABLE ROW LEVEL SECURITY;

--
-- Name: recent_activity recent_activity readable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "recent_activity readable by owner" ON public.recent_activity FOR SELECT TO authenticated USING ((user_id = auth.uid()));


--
-- Name: release_sync_errors; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.release_sync_errors ENABLE ROW LEVEL SECURITY;

--
-- Name: tracks; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.tracks ENABLE ROW LEVEL SECURITY;

--
-- Name: tracks tracks readable by authenticated; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "tracks readable by authenticated" ON public.tracks FOR SELECT TO authenticated USING (true);


--
-- Name: upcoming_releases; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.upcoming_releases ENABLE ROW LEVEL SECURITY;

--
-- Name: upcoming_releases upcoming_releases readable by authenticated; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "upcoming_releases readable by authenticated" ON public.upcoming_releases FOR SELECT TO authenticated USING (true);


--
-- Name: user_likes; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.user_likes ENABLE ROW LEVEL SECURITY;

--
-- Name: user_likes user_likes insertable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "user_likes insertable by owner" ON public.user_likes FOR INSERT TO authenticated WITH CHECK ((auth.uid() = user_id));


--
-- Name: user_likes user_likes readable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "user_likes readable by owner" ON public.user_likes FOR SELECT TO authenticated USING ((auth.uid() = user_id));


--
-- Name: user_likes user_likes updatable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "user_likes updatable by owner" ON public.user_likes FOR UPDATE TO authenticated USING ((auth.uid() = user_id));


--
-- Name: user_monthly_stats; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.user_monthly_stats ENABLE ROW LEVEL SECURITY;

--
-- Name: user_monthly_stats user_monthly_stats readable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "user_monthly_stats readable by owner" ON public.user_monthly_stats FOR SELECT TO authenticated USING ((user_id = auth.uid()));


--
-- Name: user_recommendations; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.user_recommendations ENABLE ROW LEVEL SECURITY;

--
-- Name: user_recommendations user_recommendations readable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "user_recommendations readable by owner" ON public.user_recommendations FOR SELECT TO authenticated USING ((user_id = auth.uid()));


--
-- Name: user_weekly_stats; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE public.user_weekly_stats ENABLE ROW LEVEL SECURITY;

--
-- Name: user_weekly_stats user_weekly_stats readable by owner; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "user_weekly_stats readable by owner" ON public.user_weekly_stats FOR SELECT TO authenticated USING ((user_id = auth.uid()));


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: pg_database_owner
--

GRANT USAGE ON SCHEMA public TO postgres;
GRANT USAGE ON SCHEMA public TO anon;
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA public TO service_role;


--
-- Name: FUNCTION add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid) TO anon;
GRANT ALL ON FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid) TO authenticated;
GRANT ALL ON FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid) TO service_role;


--
-- Name: FUNCTION add_playlist_tracks_bulk(p_playlist_id uuid, p_track_ids uuid[], p_added_by uuid); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.add_playlist_tracks_bulk(p_playlist_id uuid, p_track_ids uuid[], p_added_by uuid) TO anon;
GRANT ALL ON FUNCTION public.add_playlist_tracks_bulk(p_playlist_id uuid, p_track_ids uuid[], p_added_by uuid) TO authenticated;
GRANT ALL ON FUNCTION public.add_playlist_tracks_bulk(p_playlist_id uuid, p_track_ids uuid[], p_added_by uuid) TO service_role;


--
-- Name: FUNCTION aggregate_user_weekly_stats(p_user_id uuid, p_week_start date, p_week_end date); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.aggregate_user_weekly_stats(p_user_id uuid, p_week_start date, p_week_end date) TO anon;
GRANT ALL ON FUNCTION public.aggregate_user_weekly_stats(p_user_id uuid, p_week_start date, p_week_end date) TO authenticated;
GRANT ALL ON FUNCTION public.aggregate_user_weekly_stats(p_user_id uuid, p_week_start date, p_week_end date) TO service_role;


--
-- Name: FUNCTION bump_playlist_on_track_change(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.bump_playlist_on_track_change() TO anon;
GRANT ALL ON FUNCTION public.bump_playlist_on_track_change() TO authenticated;
GRANT ALL ON FUNCTION public.bump_playlist_on_track_change() TO service_role;


--
-- Name: FUNCTION bump_playlist_updated_at(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.bump_playlist_updated_at() TO anon;
GRANT ALL ON FUNCTION public.bump_playlist_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.bump_playlist_updated_at() TO service_role;


--
-- Name: FUNCTION cleanup_library_on_playlist_delete(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.cleanup_library_on_playlist_delete() TO anon;
GRANT ALL ON FUNCTION public.cleanup_library_on_playlist_delete() TO authenticated;
GRANT ALL ON FUNCTION public.cleanup_library_on_playlist_delete() TO service_role;


--
-- Name: FUNCTION get_active_users_in_period(p_start date, p_end date); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION public.get_active_users_in_period(p_start date, p_end date) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_active_users_in_period(p_start date, p_end date) TO authenticated;
GRANT ALL ON FUNCTION public.get_active_users_in_period(p_start date, p_end date) TO service_role;


--
-- Name: FUNCTION get_featured_new_release(p_user_id uuid); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.get_featured_new_release(p_user_id uuid) TO anon;
GRANT ALL ON FUNCTION public.get_featured_new_release(p_user_id uuid) TO authenticated;
GRANT ALL ON FUNCTION public.get_featured_new_release(p_user_id uuid) TO service_role;


--
-- Name: FUNCTION get_listen_again(p_user_id uuid, p_min_plays integer, p_not_played_days integer, p_limit integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.get_listen_again(p_user_id uuid, p_min_plays integer, p_not_played_days integer, p_limit integer) TO anon;
GRANT ALL ON FUNCTION public.get_listen_again(p_user_id uuid, p_min_plays integer, p_not_played_days integer, p_limit integer) TO authenticated;
GRANT ALL ON FUNCTION public.get_listen_again(p_user_id uuid, p_min_plays integer, p_not_played_days integer, p_limit integer) TO service_role;


--
-- Name: FUNCTION get_owned_playlists_with_track(p_user_id uuid, p_track_id text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.get_owned_playlists_with_track(p_user_id uuid, p_track_id text) TO anon;
GRANT ALL ON FUNCTION public.get_owned_playlists_with_track(p_user_id uuid, p_track_id text) TO authenticated;
GRANT ALL ON FUNCTION public.get_owned_playlists_with_track(p_user_id uuid, p_track_id text) TO service_role;


--
-- Name: FUNCTION get_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.get_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer) TO anon;
GRANT ALL ON FUNCTION public.get_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer) TO authenticated;
GRANT ALL ON FUNCTION public.get_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer) TO service_role;


--
-- Name: FUNCTION get_replay_songs(p_user_id uuid, p_min_plays integer, p_limit integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.get_replay_songs(p_user_id uuid, p_min_plays integer, p_limit integer) TO anon;
GRANT ALL ON FUNCTION public.get_replay_songs(p_user_id uuid, p_min_plays integer, p_limit integer) TO authenticated;
GRANT ALL ON FUNCTION public.get_replay_songs(p_user_id uuid, p_min_plays integer, p_limit integer) TO service_role;


--
-- Name: FUNCTION get_user_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.get_user_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer) TO anon;
GRANT ALL ON FUNCTION public.get_user_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer) TO authenticated;
GRANT ALL ON FUNCTION public.get_user_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer) TO service_role;


--
-- Name: FUNCTION get_users_with_weekly_stats(p_weeks date[]); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION public.get_users_with_weekly_stats(p_weeks date[]) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_users_with_weekly_stats(p_weeks date[]) TO authenticated;
GRANT ALL ON FUNCTION public.get_users_with_weekly_stats(p_weeks date[]) TO service_role;


--
-- Name: FUNCTION handle_new_user(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.handle_new_user() TO anon;
GRANT ALL ON FUNCTION public.handle_new_user() TO authenticated;
GRANT ALL ON FUNCTION public.handle_new_user() TO service_role;


--
-- Name: FUNCTION handle_profile_updated_at(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.handle_profile_updated_at() TO anon;
GRANT ALL ON FUNCTION public.handle_profile_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.handle_profile_updated_at() TO service_role;


--
-- Name: FUNCTION is_admin(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.is_admin() TO anon;
GRANT ALL ON FUNCTION public.is_admin() TO authenticated;
GRANT ALL ON FUNCTION public.is_admin() TO service_role;


--
-- Name: FUNCTION is_developer_or_higher(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.is_developer_or_higher() TO anon;
GRANT ALL ON FUNCTION public.is_developer_or_higher() TO authenticated;
GRANT ALL ON FUNCTION public.is_developer_or_higher() TO service_role;


--
-- Name: FUNCTION is_tester_or_higher(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.is_tester_or_higher() TO anon;
GRANT ALL ON FUNCTION public.is_tester_or_higher() TO authenticated;
GRANT ALL ON FUNCTION public.is_tester_or_higher() TO service_role;


--
-- Name: FUNCTION move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer) TO authenticated;
GRANT ALL ON FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer) TO service_role;


--
-- Name: FUNCTION playlist_tracks_reorder(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.playlist_tracks_reorder() TO anon;
GRANT ALL ON FUNCTION public.playlist_tracks_reorder() TO authenticated;
GRANT ALL ON FUNCTION public.playlist_tracks_reorder() TO service_role;


--
-- Name: FUNCTION prevent_role_self_update(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.prevent_role_self_update() TO anon;
GRANT ALL ON FUNCTION public.prevent_role_self_update() TO authenticated;
GRANT ALL ON FUNCTION public.prevent_role_self_update() TO service_role;


--
-- Name: FUNCTION purge_old_data(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.purge_old_data() TO anon;
GRANT ALL ON FUNCTION public.purge_old_data() TO authenticated;
GRANT ALL ON FUNCTION public.purge_old_data() TO service_role;


--
-- Name: FUNCTION recommend_playlists_by_history(p_user_id uuid, p_days integer, p_limit integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.recommend_playlists_by_history(p_user_id uuid, p_days integer, p_limit integer) TO anon;
GRANT ALL ON FUNCTION public.recommend_playlists_by_history(p_user_id uuid, p_days integer, p_limit integer) TO authenticated;
GRANT ALL ON FUNCTION public.recommend_playlists_by_history(p_user_id uuid, p_days integer, p_limit integer) TO service_role;


--
-- Name: FUNCTION remove_playlist_track(p_playlist_id uuid, p_track_id text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.remove_playlist_track(p_playlist_id uuid, p_track_id text) TO anon;
GRANT ALL ON FUNCTION public.remove_playlist_track(p_playlist_id uuid, p_track_id text) TO authenticated;
GRANT ALL ON FUNCTION public.remove_playlist_track(p_playlist_id uuid, p_track_id text) TO service_role;


--
-- Name: FUNCTION update_genre_playlist_track_count(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.update_genre_playlist_track_count() TO anon;
GRANT ALL ON FUNCTION public.update_genre_playlist_track_count() TO authenticated;
GRANT ALL ON FUNCTION public.update_genre_playlist_track_count() TO service_role;


--
-- Name: FUNCTION update_updated_at(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.update_updated_at() TO anon;
GRANT ALL ON FUNCTION public.update_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.update_updated_at() TO service_role;


--
-- Name: FUNCTION update_updated_at_column(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.update_updated_at_column() TO anon;
GRANT ALL ON FUNCTION public.update_updated_at_column() TO authenticated;
GRANT ALL ON FUNCTION public.update_updated_at_column() TO service_role;


--
-- Name: TABLE bug_reports; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.bug_reports TO anon;
GRANT ALL ON TABLE public.bug_reports TO authenticated;
GRANT ALL ON TABLE public.bug_reports TO service_role;


--
-- Name: TABLE error_logs; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.error_logs TO anon;
GRANT ALL ON TABLE public.error_logs TO authenticated;
GRANT ALL ON TABLE public.error_logs TO service_role;


--
-- Name: TABLE feed_current; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.feed_current TO anon;
GRANT ALL ON TABLE public.feed_current TO authenticated;
GRANT ALL ON TABLE public.feed_current TO service_role;


--
-- Name: TABLE genre_playlist_tracks; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.genre_playlist_tracks TO anon;
GRANT ALL ON TABLE public.genre_playlist_tracks TO authenticated;
GRANT ALL ON TABLE public.genre_playlist_tracks TO service_role;


--
-- Name: TABLE genre_playlists; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.genre_playlists TO anon;
GRANT ALL ON TABLE public.genre_playlists TO authenticated;
GRANT ALL ON TABLE public.genre_playlists TO service_role;


--
-- Name: TABLE genres; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.genres TO anon;
GRANT ALL ON TABLE public.genres TO authenticated;
GRANT ALL ON TABLE public.genres TO service_role;


--
-- Name: TABLE library_items; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.library_items TO anon;
GRANT ALL ON TABLE public.library_items TO authenticated;
GRANT ALL ON TABLE public.library_items TO service_role;


--
-- Name: TABLE play_events; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.play_events TO anon;
GRANT ALL ON TABLE public.play_events TO authenticated;
GRANT ALL ON TABLE public.play_events TO service_role;


--
-- Name: TABLE playlist_tracks; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.playlist_tracks TO anon;
GRANT ALL ON TABLE public.playlist_tracks TO authenticated;
GRANT ALL ON TABLE public.playlist_tracks TO service_role;


--
-- Name: TABLE playlists; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.playlists TO anon;
GRANT ALL ON TABLE public.playlists TO authenticated;
GRANT ALL ON TABLE public.playlists TO service_role;


--
-- Name: TABLE profiles; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.profiles TO anon;
GRANT ALL ON TABLE public.profiles TO authenticated;
GRANT ALL ON TABLE public.profiles TO service_role;


--
-- Name: TABLE recent_activity; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.recent_activity TO anon;
GRANT ALL ON TABLE public.recent_activity TO authenticated;
GRANT ALL ON TABLE public.recent_activity TO service_role;


--
-- Name: TABLE release_sync_errors; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.release_sync_errors TO anon;
GRANT ALL ON TABLE public.release_sync_errors TO authenticated;
GRANT ALL ON TABLE public.release_sync_errors TO service_role;


--
-- Name: TABLE tracks; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.tracks TO anon;
GRANT ALL ON TABLE public.tracks TO authenticated;
GRANT ALL ON TABLE public.tracks TO service_role;


--
-- Name: TABLE upcoming_releases; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.upcoming_releases TO anon;
GRANT ALL ON TABLE public.upcoming_releases TO authenticated;
GRANT ALL ON TABLE public.upcoming_releases TO service_role;


--
-- Name: TABLE user_likes; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.user_likes TO anon;
GRANT ALL ON TABLE public.user_likes TO authenticated;
GRANT ALL ON TABLE public.user_likes TO service_role;


--
-- Name: TABLE user_monthly_stats; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.user_monthly_stats TO anon;
GRANT ALL ON TABLE public.user_monthly_stats TO authenticated;
GRANT ALL ON TABLE public.user_monthly_stats TO service_role;


--
-- Name: TABLE user_recommendations; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.user_recommendations TO anon;
GRANT ALL ON TABLE public.user_recommendations TO authenticated;
GRANT ALL ON TABLE public.user_recommendations TO service_role;


--
-- Name: TABLE user_weekly_stats; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.user_weekly_stats TO anon;
GRANT ALL ON TABLE public.user_weekly_stats TO authenticated;
GRANT ALL ON TABLE public.user_weekly_stats TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: supabase_admin
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: supabase_admin
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: supabase_admin
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO service_role;


--
-- PostgreSQL database dump complete
--

\unrestrict 32BQ3YhaPF7W5ztC7YcM84g9HEhlJWImUajJM5ByYeGbmQArklqnDxO4FuvN0aa

