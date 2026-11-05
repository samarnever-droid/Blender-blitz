#!/usr/bin/env bash
# One-command local setup: generate a token, build the image, start it, verify.
#
#   ./setup.sh              # build + run + smoke test
#   ./setup.sh --no-build   # reuse an existing image
set -euo pipefail
cd "$(dirname "$0")"

BUILD=1
[[ "${1:-}" == "--no-build" ]] && BUILD=0

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Desktop or Docker Engine first." >&2
  exit 1
fi

# 1. Token
if [[ ! -f .env ]]; then
  say "Creating .env with a random token"
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null \
          || openssl rand -base64 32 | tr -d '/+=' | cut -c1-43)
  sed "s|^MCP_AUTH_TOKEN=.*|MCP_AUTH_TOKEN=${TOKEN}|" .env.example > .env
  echo "  wrote .env"
else
  say "Using existing .env"
fi
TOKEN=$(grep -E '^MCP_AUTH_TOKEN=' .env | cut -d= -f2-)

# 2. Build
if [[ "$BUILD" == "1" ]]; then
  say "Building image (first build downloads Blender, ~360 MB)"
  docker compose build
fi

# 3. Run
say "Starting container"
docker compose up -d

# 4. Wait for Blender
say "Waiting for Blender to become ready (up to 3 minutes)"
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/readyz || true)
  if [[ "$code" == "200" ]]; then
    echo "  Blender is ready after ~$((i*3))s"
    break
  fi
  [[ "$i" == "60" ]] && { echo "  Timed out. Check: docker compose logs -f" >&2; exit 1; }
  sleep 3
done

# 5. Verify
say "Running smoke tests"
./scripts/smoke-test.sh http://localhost:8080 "$TOKEN"

say "Done"
cat <<EOF
  Status page : http://localhost:8080
  MCP endpoint: http://localhost:8080/mcp
  Token       : ${TOKEN}

  Logs        : docker compose logs -f
  Stop        : docker compose down
EOF
