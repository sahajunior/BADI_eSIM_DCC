#!/usr/bin/env bash
# Backup/restore rehearsal (PLAN T22). Non-destructive to the app database: it
# dumps the app DB, restores into a scratch database, compares key row counts and
# a sample ticket, then drops the scratch database. Local/demo only.
set -euo pipefail

# Load the private .env so $POSTGRES_DB / $POSTGRES_USER are available host-side.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ "${CONFIRM:-}" != "yes" ]; then
  echo "This creates and drops the scratch database 'badi_restore_check' in the local" >&2
  echo "Compose database (the app database is untouched). Re-run with CONFIRM=yes." >&2
  exit 1
fi

RESTORE_DB="badi_restore_check"
DUMP="$(mktemp -t badi-restore-XXXXXX.dump)"
trap 'rm -f "$DUMP"' EXIT

psql_db() {
  docker compose exec -T db sh -c \
    "PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -h 127.0.0.1 -U \"\$POSTGRES_USER\" -d \"$1\" -tAc \"$2\""
}

db_size() {
  docker compose exec -T db sh -c \
    "PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -h 127.0.0.1 -U \"\$POSTGRES_USER\" -d postgres -tAc \"SELECT pg_database_size('$1')\""
}

COUNTS="SELECT (SELECT count(*) FROM tickets), (SELECT count(*) FROM ticket_messages), (SELECT count(*) FROM ticket_events)"

echo "1/6 Dumping the application database..."
docker compose exec -T db sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$DUMP"
echo "    dump bytes: $(wc -c < "$DUMP")"

echo "2/6 Capturing source counts and sample ticket..."
source_counts="$(psql_db "$POSTGRES_DB" "$COUNTS")"
sample="$(psql_db "$POSTGRES_DB" "SELECT 'BD-' || ticket_sequence || '|' || subject FROM tickets ORDER BY ticket_sequence LIMIT 1")"
echo "    tickets,messages,events = $source_counts"
echo "    sample = $sample"

echo "3/6 Recreating scratch database $RESTORE_DB..."
psql_db postgres "DROP DATABASE IF EXISTS $RESTORE_DB" > /dev/null
psql_db postgres "CREATE DATABASE $RESTORE_DB" > /dev/null

echo "4/6 Restoring..."
docker compose exec -T db sh -c \
  "PGPASSWORD=\"\$POSTGRES_PASSWORD\" pg_restore -h 127.0.0.1 -U \"\$POSTGRES_USER\" -d $RESTORE_DB --no-owner --no-privileges" < "$DUMP"

echo "5/6 Comparing restored data..."
restored_counts="$(psql_db "$RESTORE_DB" "$COUNTS")"
restored_sample="$(psql_db "$RESTORE_DB" "SELECT 'BD-' || ticket_sequence || '|' || subject FROM tickets ORDER BY ticket_sequence LIMIT 1")"
restored_events="$(psql_db "$RESTORE_DB" "SELECT count(*) FROM ticket_events WHERE event_type = 'sla.breached'")"
echo "    restored tickets,messages,events = $restored_counts"
echo "    restored sample = $restored_sample"

if [ "$source_counts" != "$restored_counts" ]; then
  echo "FAIL: counts differ (source=$source_counts restored=$restored_counts)" >&2
  exit 1
fi
if [ "$sample" != "$restored_sample" ]; then
  echo "FAIL: sample ticket differs" >&2
  exit 1
fi

echo "6/6 Dropping scratch database..."
psql_db postgres "DROP DATABASE $RESTORE_DB" > /dev/null

echo "PASS: backup restored with matching tickets/messages/audits; scratch database removed."
