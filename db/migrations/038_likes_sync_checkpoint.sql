-- 038: a server-side sync checkpoint for the likes reads (#180)
--
-- GET /likes and GET /likes/sync now return a checkpoint: the database
-- clock at the start of a read, minus a 60 second overlap. A client sends
-- it back as `since` on its next sync, so a row whose updated_at was
-- stamped before the read began but committed after it is still picked up.
-- The clock has to be the database's, because updated_at is stamped by
-- update_updated_at() with the database's now(); a clock read in Python
-- would compare two machines. PostgREST cannot expose now() through
-- .table().select(), so this file adds a function and the backend calls it
-- with db.rpc("likes_sync_checkpoint", {}).
--
-- STABLE: it reads the transaction clock and writes nothing. now() is the
-- start of the transaction of the RPC call, which is a transaction of its
-- own in PostgREST and earlier than the one of the data query that follows
-- it. That is the safe side: the checkpoint can only be older than the
-- data it covers, never newer.
--
-- SECURITY INVOKER (the default): it touches no table, so it needs no
-- privileges beyond EXECUTE. SET search_path TO 'public' in the bare form,
-- as 019 and 020: the body creates no temporary table and touches no
-- relation, so nothing can be shadowed.
--
-- Grants: the client of this function is the user client (anon key plus
-- the user's JWT), which PostgREST runs as authenticated. PUBLIC and anon
-- are revoked, as 025, 028 and 031 do; authenticated gets an explicit
-- GRANT EXECUTE, redundant with the default privileges of 017 (lines 3112
-- and 3122) but not dependent on them, as the view of 035 does for a new
-- object read by the user client. service_role is not touched, as in 032
-- and 035. No policy, view or function calls this one (it is new), so the
-- REVOKE breaks no policy, the lesson of 025. Same warning as 025, 031 and
-- 032: a future DROP + CREATE FUNCTION resets the ACL and the default
-- privileges hand EXECUTE back to anon, so the REVOKEs must be repeated.
--
-- No guards, no CREATE OR REPLACE, no IF NOT EXISTS: if the name exists
-- already that is drift and the file must fail loudly. BEGIN/COMMIT, like
-- every file that has changed privileges since 025. Normal migration: it
-- applies to the live database and also runs when building a new
-- database, after 037. Apply it BEFORE deploying the code that calls it.

BEGIN;

CREATE FUNCTION public.likes_sync_checkpoint()
 RETURNS timestamp with time zone
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT now() - interval '60 seconds';
$function$;

REVOKE ALL ON FUNCTION public.likes_sync_checkpoint() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.likes_sync_checkpoint() FROM anon;
GRANT EXECUTE ON FUNCTION public.likes_sync_checkpoint() TO authenticated;

COMMIT;
