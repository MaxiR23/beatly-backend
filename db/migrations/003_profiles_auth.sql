-- 003: profiles, auth and roles
-- Exported from Supabase on 2026-07-28 via pg_get_functiondef (verbatim).
-- handle_new_user: trigger on auth.users, creates the profile on signup.
-- handle_profile_updated_at: profiles-specific updated_at trigger.
-- is_admin / is_developer_or_higher / is_tester_or_higher: role helpers
--   (SECURITY DEFINER, read profiles.role via auth.uid()).
-- prevent_role_self_update: trigger blocking role changes by non-admins.

CREATE OR REPLACE FUNCTION public.handle_new_user()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
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

CREATE OR REPLACE FUNCTION public.handle_profile_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    new.updated_at = now();
    return new;
end;
$function$;

CREATE OR REPLACE FUNCTION public.is_admin()
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
    select exists (
        select 1 from public.profiles
        where id = auth.uid() and role = 'admin'
    );
$function$;

CREATE OR REPLACE FUNCTION public.is_developer_or_higher()
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
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
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
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
 SET search_path TO 'public'
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