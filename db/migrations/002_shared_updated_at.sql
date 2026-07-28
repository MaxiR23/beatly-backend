-- 002: generic updated_at helpers (shared trigger functions)
-- Exported from Supabase on 2026-07-28 via pg_get_functiondef (verbatim).
-- NOTE: these are TWO functions doing exactly the same thing (duplication
-- inherited from the legacy DB). Different tables point at one or the
-- other through their CREATE TRIGGER. Candidates for unification when the
-- new database is built; versioned as-is for now.

CREATE OR REPLACE FUNCTION public.update_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.update_updated_at_column()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$function$;