-- 022: translate the Spanish comments inside four function bodies to
-- English (#56)
--
-- Four functions carry comments in Spanish inside their bodies:
-- add_playlist_track, aggregate_user_weekly_stats and
-- playlist_tracks_reorder, all last defined in 017, and
-- move_playlist_track, last defined in 018 (019-021 do not touch it --
-- 021's header says so explicitly). This file redefines all four with
-- CREATE OR REPLACE, copying each one's current body verbatim and
-- changing only the text of 18 comments (19 lines, one of them wraps
-- two lines), one comment at a time, from Spanish to English. Nothing
-- else moves: signature, RETURNS, LANGUAGE, volatility, SECURITY
-- DEFINER, SET search_path (present only on move_playlist_track, as
-- 'public', 'pg_temp', unchanged since 018) and every line of logic are
-- byte-for-byte the same as the source file.
--
-- New file, not an edit to 017 or 018: both are already applied, and an
-- applied migration is never edited.
--
-- aggregate_user_weekly_stats is the only one of the four whose 017
-- body is CRLF; its body below is that same text with \r stripped, the
-- same normalization 021 already applies to bodies copied out of 017
-- (018 copies its body from 013, already LF; 019 and 020 copy nothing
-- from 017). No literal in that function spans more than one line, so
-- this has no semantic effect -- prosrc moves from CRLF to LF once this
-- is applied, same reasoning as 021's header.
--
-- No GRANT or REVOKE: none of the four signatures changes, so the
-- privileges already applied by 017 (and, for move_playlist_track, 018)
-- keep covering the function without being reapplied.
--
-- playlist_tracks_reorder is bound to the trg_playlist_tracks_reorder
-- trigger (017 line 2131). CREATE OR REPLACE does not break that
-- binding -- neither name nor signature changes -- and does not touch
-- the trigger's enabled/disabled state (017 line 2133 disables it; that
-- state, and whether it should be re-enabled, is out of scope here).
--
-- move_playlist_track's EXCEPTION handler still returns SQLERRM in the
-- JSON error field -- README finding 4, untouched by this file.

CREATE OR REPLACE FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid)
 RETURNS json
 LANGUAGE plpgsql
AS $function$
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
    -- the track is already in the playlist (constraint ux_playlist_track)
    RETURN json_build_object('ok', false, 'error', 'track_already_in_playlist');
END;
$function$;

CREATE OR REPLACE FUNCTION public.aggregate_user_weekly_stats(p_user_id uuid, p_week_start date, p_week_end date)
 RETURNS jsonb
 LANGUAGE plpgsql
AS $function$
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

  -- Top 10 artists (from the metadata->'artists' array, no fallback)
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

  -- Top 10 albums (artist_id/artist_name from the first element of the array)
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
$function$;

CREATE OR REPLACE FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
  v_total INT;
  v_rows JSON;
  v_row RECORD;
  v_idx INT;
  v_owner_id uuid;
BEGIN
  -- serialize writers on this playlist (write protocol, see 013)
  SELECT owner_id INTO v_owner_id
  FROM playlists WHERE id = p_playlist_id FOR UPDATE;

  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  IF auth.uid() IS NOT NULL AND auth.uid() <> v_owner_id THEN
    RETURN json_build_object('ok', false, 'error', 'forbidden');
  END IF;

  -- Defer constraint to allow duplicate temporary positions
  SET CONSTRAINTS ux_playlist_pos DEFERRED;

  -- Count tracks
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

  -- Create a temp table with the current order
  CREATE TEMP TABLE tmp_reorder AS
  SELECT id, track_id, ROW_NUMBER() OVER (ORDER BY position) AS rn
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  -- Move to temporary negative positions (based on the new order)
  IF p_new_index < p_old_index THEN
    -- Move up: the moved row goes to new_index, the rest move down
    UPDATE playlist_tracks pt
    SET position = CASE
      WHEN t.rn = p_old_index THEN -p_new_index
      WHEN t.rn >= p_new_index AND t.rn < p_old_index THEN -(t.rn + 1)
      ELSE -t.rn
    END
    FROM tmp_reorder t
    WHERE pt.id = t.id;
  ELSE
    -- Move down: the moved row goes to new_index, the rest move up
    UPDATE playlist_tracks pt
    SET position = CASE
      WHEN t.rn = p_old_index THEN -p_new_index
      WHEN t.rn > p_old_index AND t.rn <= p_new_index THEN -(t.rn - 1)
      ELSE -t.rn
    END
    FROM tmp_reorder t
    WHERE pt.id = t.id;
  END IF;

  -- Convert negative positions back to positive
  UPDATE playlist_tracks
  SET position = -position
  WHERE playlist_id = p_playlist_id AND position < 0;

  -- Clean up
  DROP TABLE tmp_reorder;

  -- Bump the parent's updated_at: this happens inside the SAME RPC, so it is
  -- atomic with the reorder. If the reorder commits, this commits; if it fails, so does this.
  UPDATE playlists SET updated_at = now() WHERE id = p_playlist_id;

  -- Return the final order
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
$function$;

CREATE OR REPLACE FUNCTION public.playlist_tracks_reorder()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  IF pg_trigger_depth() > 1 THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
  END IF;

  IF TG_OP = 'INSERT' THEN
    -- null position -> goes to the end (set directly on NEW, no UPDATE by id)
    IF NEW.position IS NULL THEN
      SELECT COALESCE(MAX(position),0) + 1
        INTO NEW.position
        FROM public.playlist_tracks
       WHERE playlist_id = NEW.playlist_id;
      RETURN NEW;
    END IF;

    -- insert at P -> shift right everything >= P (the row does not exist in the table yet)
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
      -- move within the same playlist
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
      -- playlist change: close the gap in the old one
      UPDATE public.playlist_tracks
         SET position = position - 1
       WHERE playlist_id = OLD.playlist_id
         AND position > OLD.position;

      -- decide the position in the new one
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
$function$;
