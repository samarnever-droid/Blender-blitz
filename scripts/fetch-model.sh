#!/usr/bin/env bash
# Export the model from the remote Blender and download it to this machine.
#
#   ./scripts/fetch-model.sh https://blender.you.prod.page "$MCP_AUTH_TOKEN" glb ./downloads
#
# Formats: glb (default), obj, stl, blend, all
# Needs only curl. The AI does not have to be on the same machine as Blender.
set -euo pipefail

BASE="${1:-http://localhost:8080}"
TOKEN="${2:-${MCP_AUTH_TOKEN:-}}"
FORMAT="${3:-glb}"
OUT="${4:-./downloads}"

if [[ -z "$TOKEN" ]]; then
  echo "usage: $0 <base-url> <token> [glb|obj|stl|blend|all] [out-dir]" >&2
  exit 2
fi
BASE="${BASE%/}"
mkdir -p "$OUT"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Build the bpy export script. Selection is limited to meshes so cameras and
# lights don't end up in game/print assets.
read -r -d '' PYCODE <<'PY' || true
import bpy, os
d = os.environ.get('DATA_DIR', '/data')
for o in bpy.data.objects:
    o.select_set(o.type == 'MESH')
fmt = '__FORMAT__'
made = []
if fmt in ('glb', 'all'):
    bpy.ops.export_scene.gltf(filepath=os.path.join(d, 'model.glb'),
                              export_format='GLB', use_selection=True)
    made.append('model.glb')
if fmt in ('obj', 'all'):
    bpy.ops.wm.obj_export(filepath=os.path.join(d, 'model.obj'),
                          export_selected_objects=True)
    made += ['model.obj', 'model.mtl']
if fmt in ('stl', 'all'):
    bpy.ops.wm.stl_export(filepath=os.path.join(d, 'model.stl'),
                          export_selected_objects=True)
    made.append('model.stl')
if fmt in ('blend', 'all'):
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(d, 'scene.blend'), compress=True)
    made.append('scene.blend')
print('EXPORTED:' + ','.join(made))
PY
PYCODE="${PYCODE//__FORMAT__/$FORMAT}"

# JSON-encode the code without needing python/jq.
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
  s="${s//$'\t'/\\t}"; s="${s//$'\r'/}"
  s="${s//$'\n'/\\n}"
  printf '%s' "$s"
}
CODE_JSON="$(json_escape "$PYCODE")"

say "Exporting '$FORMAT' in the remote Blender"
RESP=$(curl -sS -X POST "$BASE/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"execute_blender_code\",\"arguments\":{\"code\":\"$CODE_JSON\"}}}")

if grep -q '"isError":true' <<<"$RESP"; then
  echo "Export failed:" >&2; sed -n 's/^data: //p' <<<"$RESP" | head -c 800 >&2; exit 1
fi
if grep -q 'EXPORTED:' <<<"$RESP"; then
  echo "  $(sed -n 's/.*EXPORTED:\([^\\"]*\).*/  \1/p' <<<"$RESP" | head -1)"
else
  echo "  export call returned; check output below if downloads fail"
fi

say "Files available on the server"
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/files" \
  | tr ',' '\n' | grep '"name"' | sed 's/.*"name":"\([^"]*\)"/  \1/' || true

say "Downloading to $OUT"
case "$FORMAT" in
  glb)   FILES="model.glb" ;;
  obj)   FILES="model.obj model.mtl" ;;
  stl)   FILES="model.stl" ;;
  blend) FILES="scene.blend" ;;
  all)   FILES="model.glb model.obj model.mtl model.stl scene.blend" ;;
  *) echo "unknown format: $FORMAT" >&2; exit 2 ;;
esac

for f in $FILES; do
  code=$(curl -sS -H "Authorization: Bearer $TOKEN" \
          -o "$OUT/$f" -w '%{http_code}' "$BASE/files/$f" || true)
  if [[ "$code" == "200" ]]; then
    printf '  \033[32mOK\033[0m   %-14s %s bytes\n' "$f" "$(wc -c < "$OUT/$f" | tr -d ' ')"
  else
    printf '  \033[33mskip\033[0m %-14s HTTP %s\n' "$f" "$code"
    rm -f "$OUT/$f"
  fi
done

say "Done"
echo "  Files are in: $OUT"
