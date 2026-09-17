-- 021: pin search_path to 'public', 'pg_temp' on the seven SECURITY
-- DEFINER functions that lacked it (#120)
-- 017 has 8 SECURITY DEFINER functions in public. 018 already pinned
-- search_path on move_playlist_track (SET search_path TO 'public',
-- 'pg_temp') because it creates a temp table. The other seven --
-- get_active_users_in_period, get_users_with_weekly_stats,
-- handle_new_user, is_admin, is_developer_or_higher, is_tester_or_higher,
-- prevent_role_self_update -- are redefined here, each from its last
-- definition in 017 (lines 375, 573, 588, 626, 643, 661, 889), adding or
-- replacing SET search_path so all eight end up pinned the same way.
-- This is a new file, not an edit to 017 or 018: both are already
-- applied, and an applied migration is never edited.
--
-- Two different reasons for the same clause. get_active_users_in_period
-- and get_users_with_weekly_stats carry no SET search_path at all in 017
-- and reference play_events / user_weekly_stats unqualified -- being
-- SECURITY DEFINER, they resolve those names against whatever
-- search_path the caller has, which puts the (unlisted) pg_temp schema
-- first. Pinning search_path here is a real close for these two, the
-- same class of hole 018 closed for move_playlist_track. The other five
-- already carry SET search_path TO 'public' and every relation and
-- function they touch is schema-qualified (public.profiles,
-- public.is_admin(), auth.uid()); for those five, adding pg_temp
-- last is preventive hardening, not a fix for a reachable bug. When
-- pg_temp is not listed explicitly, PostgreSQL searches the temporary
-- schema FIRST for relation names (018's reasoning, not repeated here in
-- full) -- listing it last, as required, pins it behind public.
--
-- Bodies, signatures, RETURNS, LANGUAGE, volatility (STABLE where 017
-- already had it, absent where it did not) and SECURITY DEFINER are
-- unchanged from 017. The bodies below are the 017 bodies with their \r
-- line endings stripped -- 017 is a literal pg_dump and its function
-- bodies carry CRLF line endings today; this file, like 018-020, is LF
-- only. No literal in any of the seven bodies spans more than one line,
-- so this has no semantic effect -- it only means prosrc in the database
-- changes from CRLF to LF for these seven once this file is applied, and
-- a byte-for-byte diff against 017's dumped text needs to normalize \r
-- to see that nothing else moved.
--
-- No GRANT or REVOKE: none of the seven signatures changes, so the
-- privileges 017 already applied to each of them -- including the
-- GRANT ... TO anon still in place on five of them, left untouched on
-- purpose (README finding 7(a)) -- keep covering the function without
-- being reapplied, the same reasoning 018's header gives for
-- move_playlist_track.
--
-- handle_new_user is bound to the on_auth_user_created trigger on
-- auth.users (009, outside public, not in 017's dump) and
-- prevent_role_self_update is bound to enforce_role_change_permission
-- (BEFORE UPDATE ON public.profiles, 017 line 2082). CREATE OR REPLACE
-- does not break either binding: neither name nor signature changes.
--
-- move_playlist_track does not appear below: 018 already gave it
-- 'public', 'pg_temp'.

CREATE OR REPLACE FUNCTION public.get_active_users_in_period(p_start date, p_end date)
 RETURNS TABLE(user_id uuid)
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
  SELECT DISTINCT pe.user_id
  FROM play_events pe
  WHERE pe.played_at >= p_start 
    AND pe.played_at < p_end;
$function$;

CREATE OR REPLACE FUNCTION public.get_users_with_weekly_stats(p_weeks date[])
 RETURNS TABLE(user_id uuid)
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
  SELECT DISTINCT uws.user_id
  FROM user_weekly_stats uws
  WHERE uws.week_start = ANY(p_weeks);
$function$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
begin
    insert into public.profiles (id, display_name, avatar_url)
    values (
        new.id,
        new.raw_user_meta_data->>'display_name',
        new.raw_user_meta_data->>'avatar_url'
    );
    return new;
end;
$function$;

CREATE OR REPLACE FUNCTION public.is_admin()
 RETURNS boolean
 LANGUAGE sql
 STABLE
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
    select exists (
        select 1 from public.profiles
        where id = auth.uid() and role = 'admin'
    );
$function$;

CREATE OR REPLACE FUNCTION public.is_developer_or_higher()
 RETURNS boolean
 LANGUAGE sql
 STABLE
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
    select exists (
        select 1 from public.profiles
        where id = auth.uid()
          and role in ('developer', 'admin')
    );
$function$;

CREATE OR REPLACE FUNCTION public.is_tester_or_higher()
 RETURNS boolean
 LANGUAGE sql
 STABLE
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
    select exists (
        select 1 from public.profiles
        where id = auth.uid()
          and role in ('tester', 'developer', 'admin')
    );
$function$;

CREATE OR REPLACE FUNCTION public.prevent_role_self_update()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
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
$function$;
