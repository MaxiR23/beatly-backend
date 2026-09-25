-- 034: cover the pagination tiebreaker on user_likes (#150)
--
-- Replaces the two non-unique indexes 017 declares on user_likes (lines
-- 1963 and 1970, part of the dump, not edited here): idx_user_likes_sync
-- (user_id, updated_at) and idx_user_likes_user_created (user_id,
-- created_at DESC), neither of which carries track_id. Both keyset-
-- paginated readers in services/likes_service.py order by track_id as a
-- tiebreaker (_LIST_SORT and _SYNC_SORT, both id_column="track_id"),
-- and apply_page() in core/pagination.py applies each SortKey's
-- descending flag to both ORDER BY columns. Without track_id in the
-- index, Postgres has to sort the rows tied on created_at/updated_at
-- itself instead of resolving the ORDER BY straight from the index.
--
-- created_at moves from DESC to ASC: _LIST_SORT has descending=False, so
-- list_likes() (GET /likes) really sorts created_at ASC, track_id ASC --
-- the opposite direction of the index 017 declares. updated_at was
-- already ascending, matching _SYNC_SORT (sync_likes(), GET
-- /likes/sync), so idx_user_likes_sync only gains track_id.
-- list_liked_playlist_tracks(), list_liked_playlist_track_ids() and its
-- _count_through() in services/playlist_service.py share the same
-- (created_at ASC, track_id ASC) shape and benefit from the same index,
-- with no code change. The one DESC reader left,
-- _latest_liked_created_at() (ORDER BY created_at DESC, one column, no
-- track_id), still uses the same btree scanned backwards -- exactly the
-- mirror of how _liked_summary() (ORDER BY created_at ASC) already reads
-- today's DESC index forwards.
--
-- Same names as before (idx_user_likes_user_created, idx_user_likes_sync):
-- this stays a non-unique support index, unlike 028's
-- ux_playlist_order_key, which got a new name because it also changed
-- from non-unique to unique.
--
-- No guards (no IF EXISTS/IF NOT EXISTS, no CREATE INDEX CONCURRENTLY --
-- which cannot run inside a transaction anyway): drift against what this
-- file expects must fail loudly, same reasoning as 024-033. BEGIN/COMMIT:
-- both indexes land together or not at all. Locks: DROP INDEX and CREATE
-- INDEX without CONCURRENTLY each take ACCESS EXCLUSIVE, then SHARE, on
-- user_likes until COMMIT -- reads and writes against likes wait while
-- the two indexes rebuild.
--
-- Does not touch user_likes_pkey (user_id, track_id), the table itself,
-- any function, policy or grant -- an index needs no GRANT.
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 033.

BEGIN;

DROP INDEX public.idx_user_likes_user_created;

CREATE INDEX idx_user_likes_user_created
  ON public.user_likes USING btree (user_id, created_at, track_id);

DROP INDEX public.idx_user_likes_sync;

CREATE INDEX idx_user_likes_sync
  ON public.user_likes USING btree (user_id, updated_at, track_id);

COMMIT;
