#!/usr/bin/env bash
# End-to-end check against a running instance.
#
#   ./scripts/smoke-test.sh http://localhost:8080 dev-token-change-me
#   ./scripts/smoke-test.sh https://blender.you.prod.page "$MCP_AUTH_TOKEN"
set -euo pipefail

BASE="${1:-http://localhost:8080}"
TOKEN="${2:-${MCP_AUTH_TOKEN:-}}"

if [[ -z "$TOKEN" ]]; then
  echo "usage: $0 <base-url> <token>" >&2
  exit 2
fi

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

echo "Testing $BASE"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz")
[[ "$code" == "200" ]] && pass "healthz is 200" || fail "healthz returned $code"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/readyz")
[[ "$code" == "200" ]] && pass "readyz is 200 (Blender bridge up)" \
  || fail "readyz returned $code -- Blender may still be starting"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
[[ "$code" == "401" ]] && pass "mcp rejects requests without a token" \
  || fail "expected 401 without token, got $code"

body=$(curl -s -X POST "$BASE/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}')
grep -q 'serverInfo' <<<"$body" && pass "mcp initialize succeeded" \
  || fail "initialize did not return serverInfo: ${body:0:200}"

body=$(curl -s -X POST "$BASE/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
grep -q 'execute_blender_code' <<<"$body" && pass "tools/list includes execute_blender_code" \
  || fail "tools/list missing expected tool: ${body:0:200}"

echo "All checks passed."
