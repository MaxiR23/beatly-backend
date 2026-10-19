-- 041: require the first artist of a catalog track to have an id
--
-- Adds the CHECK public.tracks.tracks_artists_primary_has_id, NOT VALID.
-- This is a new file, not an edit to 017 or any other applied migration:
-- 017 (lines 1421-1432) declares only tracks_artists_is_array and
-- tracks_artists_not_empty on tracks, and no file from 018 to 040 adds a
-- CHECK to it.
--
-- It is the database side of the track artists rule of the API
-- (docs/api/conventions.md, "Track artists"): the first artist of a track
-- has a non-empty id. The API validates the rule in models/artist_refs.py;
-- this CHECK covers every other writer, including the ones outside the
-- repo (a script with the service role key, the dashboard). The empty
-- list is already refused by tracks_artists_not_empty.
--
-- The predicate:
--   * COALESCE(..., false): a CHECK that evaluates to NULL passes. With the
--     id key absent, jsonb_typeof(NULL) is NULL, so without the COALESCE a
--     first artist with no id key would pass.
--   * jsonb_typeof(...) = 'string': refuses a JSON null and a number.
--   * <> '': an empty id links to nothing, so it counts as missing.
--
-- NOT VALID: some rows already break the rule (the owner counted 61 rows of
-- genre_playlist_tracks whose track has a single artist with a null id;
-- they can be fewer distinct tracks, since a track can be in several genre
-- playlists; the genre script that loaded them lives outside this repo).
-- They stay until genre playlists are rebuilt. NOT VALID enforces the rule on every INSERT and on every
-- UPDATE (the ON CONFLICT DO UPDATE of an upsert included) without scanning
-- the rows already there. A row that is already broken cannot be rewritten
-- with broken data; it can be rewritten with valid data, which is what an
-- upsert from the API does. An UPDATE that does not touch artists on a
-- broken row also fails, because Postgres checks the CHECK against the
-- whole new row. VALIDATE CONSTRAINT is pending (README, TODO).
--
-- No guards, same reasoning as 024-034: drift against what this file
-- expects must fail loudly. BEGIN/COMMIT. Lock: ACCESS EXCLUSIVE on tracks,
-- brief, because NOT VALID does no scan.
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 040.

BEGIN;

ALTER TABLE public.tracks
  ADD CONSTRAINT tracks_artists_primary_has_id
  CHECK (COALESCE(jsonb_typeof(artists -> 0 -> 'id') = 'string'
                  AND (artists -> 0 ->> 'id') <> '', false))
  NOT VALID;

COMMIT;
