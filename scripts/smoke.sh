#!/usr/bin/env bash
set -euo pipefail

base_url="${BASE_URL:-http://127.0.0.1:8080}"
# Allow bounded DNS/connection recovery after replacing the API container.
curl --fail --silent --show-error --max-time 3 --retry 10 --retry-delay 1 \
  --retry-all-errors "${base_url}/api/v1/health/ready" > /dev/null
for endpoint in / /api/v1/health/live /api/v1/health/ready /api/v1/docs /api/v1/openapi.json; do
  curl --fail --silent --show-error --max-time 10 "${base_url}${endpoint}" > /dev/null
  printf 'PASS %s\n' "$endpoint"
done
curl --fail --silent --show-error --max-time 10 "${base_url}/api/v1/health/ready" |
  python3 -c 'import json,sys; data=json.load(sys.stdin); assert data == {"status": "ready", "database": "ok"}, data'
printf 'PASS readiness confirms PostgreSQL connectivity\n'
