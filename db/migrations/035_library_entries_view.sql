-- 035: unify the library into one view, library_entries (#153)
--
-- GET /library currently reads only library_items -- saved albums and
-- saved playlists, both external to the user. The issue also wants the
-- user's own playlists and a fixed "liked songs" entry in the same list,
-- ordered together by recency. This file adds the source that makes that
-- possible: a view, not a function, because every keyset-paginated
-- reader in services/ (apply_page()/build_page() from
-- core/pagination.py) runs its query over db.table(...), never
-- db.rpc(...) -- 10 of 10 apply_page() calls and 14 of 14 build_page()
-- calls in the repo today are on a .table() query. A function would not
-- fit that shape without a second, bespoke code path. The "liked songs"
-- entry itself is not a row here: it is synthesized in Python by
-- services/library_service.py, the same way get_liked_playlist() already
-- synthesizes it for GET /playlists/liked -- this view only has to cover
-- own playlists and saved items.
--
-- WITH (security_invoker = true): this view is created by postgres, the
-- owner of playlists, library_items, playlist_tracks and tracks, and
-- without security_invoker a view runs with the privileges and, more
-- importantly, the RLS exemption of its owner -- every row of every
-- user's playlists and library_items would be visible to any caller.
-- security_invoker makes the view re-check RLS as the calling role
-- (authenticated, via the user's own JWT under PostgREST), the same way
-- every other read in this backend already works.
--
-- The playlists branch selects owner_id as user_id and created_at as
-- added_at, both as-is, with no COALESCE: created_at is nullable in 017
-- (line 1342, no NOT NULL), and GET /playlists (_LIST_SORT in
-- services/playlist_service.py) already assumes it is populated without
-- a fallback. This view repeats that assumption, it does not add a new
-- one; a NULL created_at is a pre-existing risk, not one this file
-- creates.
--
-- RLS scoping, read carefully: "library_items readable by owner" (017
-- line 2453, USING (auth.uid() = user_id)) does limit the library_items
-- branch to the caller's own rows -- a second barrier behind the
-- .eq("user_id", ...) the service adds. "playlists readable by owner or
-- public" (017 line 2526, USING (is_public OR owner_id = auth.uid()))
-- does not: as an invoker of that policy, the caller can also see every
-- OTHER user's public playlist through this view. The only thing that
-- narrows the playlists branch to "my own playlists" is the
-- .eq("user_id", user_id) that services/library_service.py adds on top
-- of the view -- RLS here is not a second barrier for that branch, only
-- a barrier against other users' PRIVATE playlists. If that .eq is ever
-- dropped, this view starts leaking other users' public playlists into
-- GET /library; see test_list_scopes_query_to_authenticated_user in
-- test/routes/test_library.py.
--
-- The thumbnail subquery (a correlated LIMIT 1 over playlist_tracks
-- joined to tracks, ordered by order_key, filtering out a null or empty
-- tracks.thumbnail_url) uses the same thumbnail predicate
-- (t.thumbnail_url IS NOT NULL AND t.thumbnail_url <> '') and the same
-- order_key order as get_user_playlist_thumbnails today -- the version
-- CREATE OR REPLACEd by 028_use_playlist_tracks_order_key.sql, not the
-- original 004 version -- but not its window: that function numbers the
-- first limit_per_playlist (4) tracks BEFORE filtering by thumbnail,
-- while this subquery filters first and takes the first track with an
-- image across the whole playlist. The two agree (this cover is the
-- mosaic's first tile) whenever one of the first 4 tracks has a
-- thumbnail; when none of them does, this view still returns a cover
-- and the mosaic comes out empty. This view does not call that function
-- -- it needs one thumbnail per playlist, not four. The subquery reads
-- playlist_tracks and tracks as the invoker, which is why it needs their
-- own SELECT policies: "playlist_tracks readable by playlist visibility"
-- (017 line 2497) and "tracks readable by authenticated" (017 line
-- 2571), both already granted to authenticated.
--
-- REVOKE/GRANT: 017's default privileges (lines 3130-3133 and
-- 3140-3143, ALTER DEFAULT PRIVILEGES ... GRANT ALL ON TABLES TO anon,
-- authenticated) apply to every new relation created afterwards,
-- including a view, so library_entries is born with ALL granted to both
-- anon and authenticated. This file revokes both and grants back only
-- SELECT to authenticated: nobody should read this view as anon (it has
-- no RLS-independent filter of its own), and INSERT/UPDATE/DELETE on it
-- make no sense -- it is a UNION ALL of two tables, which Postgres does
-- not treat as an updatable view. service_role is not touched: it keeps
-- the ALL the same default privileges already grant it, which is moot in
-- practice since the view has nothing to update, insert into or delete
-- from either way.
--
-- An own playlist is never a saved item -- there is nothing to save
-- about your own -- so this view does not deduplicate the two branches
-- of the UNION ALL against each other: POST /library is assumed to
-- never receive one. A playlist saved that way would surface twice,
-- once from each branch. Decided in #153, not an oversight.
--
-- No guards (no CREATE OR REPLACE VIEW, no IF NOT EXISTS): drift against
-- what this file expects -- an already-existing relation named
-- library_entries, or the columns/policies it depends on looking
-- different from 017/028 -- must fail loudly, same reasoning as
-- 024-034. BEGIN/COMMIT: the view and its grants land together or not at
-- all.
--
-- Touches no table, function, trigger or existing policy. No index is
-- added by this file (see the issue's plan, Risks: performance is left
-- for later measurement against a live database).
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 034.

BEGIN;

CREATE VIEW public.library_entries
WITH (security_invoker = true)
AS
SELECT
  p.owner_id AS user_id,
  p.id AS row_id,
  'playlist'::text AS kind,
  'user'::text AS source,
  p.id::text AS id,
  p.title,
  (
    SELECT t.thumbnail_url
    FROM public.playlist_tracks pt
    JOIN public.tracks t ON t.id = pt.track_id
    WHERE pt.playlist_id = p.id
      AND t.thumbnail_url IS NOT NULL
      AND t.thumbnail_url <> ''
    ORDER BY pt.order_key
    LIMIT 1
  ) AS thumbnail_url,
  NULL::text AS subtitle,
  p.created_at AS added_at
FROM public.playlists p
UNION ALL
SELECT
  li.user_id,
  li.id AS row_id,
  li.kind,
  li.source,
  li.external_id AS id,
  li.title,
  nullif(li.thumbnail_url, '') AS thumbnail_url,
  nullif(li.artist, '') AS subtitle,
  li.added_at
FROM public.library_items li;

REVOKE ALL ON public.library_entries FROM anon;
REVOKE ALL ON public.library_entries FROM authenticated;
GRANT SELECT ON public.library_entries TO authenticated;

COMMIT;
