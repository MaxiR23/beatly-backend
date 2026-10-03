#!/usr/bin/env bash
# Builds the local integration-test database from db/migrations (#176).
#
# Applies 017 (the schema baseline) and every later numbered migration, in
# order, to the database of the local Supabase stack started with
# `supabase --workdir db/local start`. 001-016 are history subsumed by 017
# and are never applied. No migration file is edited: the one statement of
# 017 that cannot run on a Supabase database, CREATE SCHEMA public, is
# dropped from the stream on its way to psql, and the file stays intact.
#
# psql runs inside the stack's own database container, so the host needs
# no psql of its own (017 opens with \restrict, which needs psql 17.6 or
# later), and as supabase_admin: 017 alters the default privileges of
# supabase_admin, which the postgres role is not allowed to do.
#
# Never point this at anything but the local stack: it only knows how to
# reach a local container.

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
migrations="$here/../migrations"
project_id="$(sed -n 's/^project_id = "\(.*\)"$/\1/p' "$here/supabase/config.toml")"
container="supabase_db_${project_id}"

baseline="$migrations/017_schema_baseline.sql"
if [ "$(grep -c '^CREATE SCHEMA public;$' "$baseline")" != "1" ]; then
  echo "expected exactly one 'CREATE SCHEMA public;' line in 017" >&2
  exit 1
fi

for file in "$migrations"/[0-9][0-9][0-9]_*.sql; do
  number="$(basename "$file" | cut -c1-3)"
  if [ "$((10#$number))" -lt 17 ]; then
    continue
  fi
  echo "applying $(basename "$file")"
  if [ "$number" = "017" ]; then
    grep -v '^CREATE SCHEMA public;$' "$file"
  else
    cat "$file"
  fi | docker exec -i "$container" \
    psql -v ON_ERROR_STOP=1 --quiet -U supabase_admin -d postgres -f - >/dev/null
done

# PostgREST reloads its schema cache on DDL already; this makes it explicit.
echo "NOTIFY pgrst, 'reload schema';" | docker exec -i "$container" \
  psql -v ON_ERROR_STOP=1 --quiet -U supabase_admin -d postgres -f - >/dev/null
