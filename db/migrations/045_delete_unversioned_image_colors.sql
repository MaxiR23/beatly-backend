-- 045: delete the image colors stored without an algorithm version (#215)
--
-- Since #215 the service stores every color under a key that carries the
-- version of the color algorithm (v2:<image key>, ADR 023). The rows
-- written before it, with the HLS adjustment, are keyed by the bare image
-- key: nothing reads them any more. This file deletes them once.
--
-- The predicate is the negation of the current version prefix and assumes
-- nothing about how an old key starts (an eligible URL can carry a leading
-- space or an upper case scheme, urlsplit tolerates both): it deletes every
-- row whose key does not start with v2:.
--
-- Idempotent through the WHERE: a second run deletes 0 rows. No table
-- references image_colors and it has no triggers (042), so the DELETE
-- cascades to nothing. No DDL, no policies, no guards.
--
-- Order: AFTER deploying the code of #215, once no instance of the
-- previous code is left. Applied before, it deletes the colors the running
-- code still reads, and that code computes them again unversioned; run it
-- again after the deploy. On a new database built from 017 it runs after
-- 044 and deletes 0 rows.

BEGIN;

DELETE FROM public.image_colors
WHERE image_key NOT LIKE 'v2:%';

COMMIT;
