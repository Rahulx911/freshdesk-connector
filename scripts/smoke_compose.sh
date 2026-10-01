#!/usr/bin/env bash
# Production-like smoke test: builds the image, starts 2 replicas + Redis + mock
# Freshdesk with docker compose, then checks health, auth, a real MCP call, the
# shared rate budget under a burst, metrics, and that logs leak no secrets.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p deploy/secrets
TOKEN=$(freshdesk-connector token create --tenant kettle-and-leaf --name ci-smoke 2>/dev/null)
HASH=$(python -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$TOKEN")
sed "s/REPLACE_WITH_OUTPUT_OF_token_create/$HASH/" deploy/tenants.example.json > deploy/tenants.json
printf 'mock-api-key-123' > deploy/secrets/fd_key_kettle_and_leaf
python -c "import secrets;print(secrets.token_urlsafe(24),end='')" > deploy/secrets/metrics_token
chmod 644 deploy/secrets/* deploy/tenants.json
trap 'docker compose logs --no-color > /tmp/compose.log 2>&1 || true; docker compose down -v >/dev/null 2>&1 || true' EXIT

# BUILD_NETWORK=host / PYTHON_IMAGE=... let restricted sandboxes build; CI uses the defaults.
BUILD_NET=${BUILD_NETWORK:-default}
BASE=${PYTHON_IMAGE:-python:3.12-slim}
docker build -q --network "$BUILD_NET" --build-arg PYTHON_IMAGE="$BASE" -t freshdesk-connector:local . >/dev/null
docker build -q --network "$BUILD_NET" --build-arg PYTHON_IMAGE="$BASE" -f deploy/Dockerfile.mock -t freshdesk-mock:local . >/dev/null
docker compose up -d --no-build --wait
P0=$(docker compose port --index 1 connector 8000 | cut -d: -f2)
P1=$(docker compose port --index 2 connector 8000 | cut -d: -f2)
echo "replicas on :$P0 and :$P1"

for P in $P0 $P1; do
  test "$(curl -fsS localhost:$P/healthz)" = ok
  R=$(curl -sS localhost:$P/readyz); echo "readyz :$P -> $R"; echo "$R" | grep -q '"ready":true'
done
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST localhost:$P0/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
test "$code" = 401 && echo "unauthenticated request rejected (401)"

python scripts/loadtest.py --url http://127.0.0.1:$P0 --url http://127.0.0.1:$P1 --token "$TOKEN" \
  --concurrency 10 --duration 60 --calls-per-worker 6 --json /tmp/burst.json
python - <<'PY'
import json
r = json.load(open("/tmp/burst.json"))
o = r["outcomes"]
assert set(o) <= {"ok", "rate_limited"}, o
assert o.get("ok", 0) > 0 and o.get("rate_limited", 0) > 0, o
print("burst OK:", o)
PY

MT=$(cat deploy/secrets/metrics_token)
curl -fsS -H "Authorization: Bearer $MT" localhost:$P0/metrics | grep -q 'fdconn_tool_calls_total{outcome="ok"'
docker compose logs --no-color connector > /tmp/connector.log 2>&1
if grep -qE "$TOKEN|mock-api-key-123" /tmp/connector.log; then echo "SECRET LEAKED IN LOGS"; exit 1; fi
grep -q '"msg": "tool_call"' /tmp/connector.log
echo "compose smoke test passed"
