# Blender + MCP for blitz.cloud

Headless Blender with a **Model Context Protocol** endpoint at **`/mcp`**, packaged
to run as a single container on [blitz.cloud](https://blitz.cloud) — **tuned to fit
the free plan's 512 MB ceiling.**

An MCP client (Claude, Cursor, VS Code, …) connects over HTTPS and drives a real
Blender: create objects, run `bpy` code, render, and look at the result.

```
MCP client ──HTTPS──> blitz proxy ──> container :8080
                                        ├── /mcp      MCP Streamable HTTP (token)
                                        ├── /healthz  liveness
                                        ├── /readyz   Blender readiness
                                        ├── /files    JSON index + downloads (token)
                                        └── /         status page
                                              │
                                              ▼ 127.0.0.1:9876 (internal only)
                                        Blender 4.5 LTS, background mode
                                        + MCP addon + main-thread pump
```

One port, because blitz.cloud routes to exactly one. No Xvfb, no GUI, no OpenGL.

---

## Fits in 512 MB — measured, not guessed

Measured on Blender 4.5.9 with a real MCP client, summing peak RSS across all
processes in the container:

| Configuration | Idle | Peak | 512 MB Small? |
|---|---|---|---|
| **Headless (default)** | **296 MB** | **466 MB** | **Yes** |
| GUI on Xvfb (old approach) | ~800 MB | ~900 MB+ | No — OOM-killed |

The default `BLENDER_MODE=headless` runs `blender -b`. That drops the window
manager, OpenGL context and llvmpipe entirely: Blender alone goes from **711 MB
to 243 MB** idle.

### How background mode works here

The MCP addon normally refuses `blender -b`, because it drains its command queue
from `bpy.app.timers` and those timers only fire in a GUI event loop. But the
drainer is just a method — `app/blender_headless.py` calls it directly from a
main-thread loop:

```python
while running:
    server._drain_command_queue()   # what bpy.app.timers would have done
    time.sleep(0.02)
```

All `bpy` work still happens on Blender's main thread, which is the real
requirement. Commands execute normally.

### Render budget at 512 MB

Cycles CPU, 1 thread, denoising off. Add ~58 MB for the gateway + supervisor:

| Resolution | Samples | Time | Blender peak | Total | Verdict |
|---|---|---|---|---|---|
| 640×480 | 32 | 4.9 s | 277 MB | ~335 MB | Comfortable |
| 960×720 | 32 | 10.7 s | 296 MB | ~354 MB | Comfortable |
| 1280×960 | 64 | 37 s | 338 MB | ~396 MB | Fine |
| 1920×1440 | 64 | 87 s | 465 MB | ~523 MB | **Over — will OOM** |

Stay at or below ~1280 px on Small. For 1080p+ or dense scenes, switch to Medium
(1 GB ceiling) — see below.

### Two engine gotchas found by testing

**Workbench is a memory trap.** It renders in 0.38 s but loads the whole
mesa/llvmpipe GPU stack: **491 MB peak vs 267 MB for Cycles** at the same size.
So `look` uses **Cycles CPU at 16 samples**, capped at 640 px. Slightly noisy,
perfectly readable, and it fits.

**EEVEE kills the process.** Selecting `BLENDER_EEVEE_NEXT` in background mode
aborts Blender (verified: exit 137). A model will try it, so the bridge rewrites
EEVEE engine requests to Cycles and logs the substitution instead of dying.

---

## blitz sizing: you have more room than you think

The free plan's **512 MB is a reservation pool, not a per-app hard cap**:

| Size | Reserved from pool | Memory ceiling | Plan |
|---|---|---|---|
| Small | 128 MB | **512 MB** | Free |
| Medium | 256 MB | **1 GB** | Free — fits the 512 MB pool |
| Large | 768 MB | 2 GB | Pro only |

So on the **free plan you can select Medium** (Settings → Advanced): it reserves
only 256 MB of your 512 MB pool but raises the ceiling to 1 GB. Docker apps
*start* at Small, which is why you saw 512 MB.

- **Stay on Small** → the default headless mode fits (466 MB peak).
- **Choose Medium** → 1 GB ceiling; bigger renders, and you can set
  `BLENDER_MODE=gui` for true viewport captures and EEVEE.

If another app already reserves most of your pool, pause or delete it first —
your old stuck `Blender` app is a good candidate.

---

## Deploy to blitz.cloud

### Option A — build from GitHub (recommended)

1. Push this folder to a GitHub repository.
2. In the [blitz dashboard](https://dashboard.blitz.cloud/), click **Host something new → My own code**.
3. Pick the repo (or paste its URL). blitz finds the root `Dockerfile` and builds it.
4. Name the app, e.g. `blender` → `blender.<you>.prod.page`.
5. In the **Environment** tab set:

   | Variable | Value |
   |---|---|
   | `MCP_AUTH_TOKEN` | a long random string — **set this** |
   | `AUTOSAVE_SECONDS` | `300` |

   Generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
6. Leave the size at **Small** for the 512 MB default, or pick **Medium** for headroom.

The first build downloads Blender (~360 MB) and takes several minutes. The app
answers on its port in about a second, and Blender is ready ~10 s after that.

### Option B — publish an image, then deploy it

`.github/workflows/docker.yml` builds `linux/amd64` and pushes to ghcr.io on every
push to `main`.

1. Push to GitHub and wait for the action.
2. Make the package **public** (GitHub → Packages → Package settings → Change visibility).
3. In blitz: **Host something new → An app from Docker Hub**, enter `ghcr.io/<owner>/<repo>:latest`.
4. Set `MCP_AUTH_TOKEN`, choose the size.

> blitz.cloud runs only **public** **amd64** images; the workflow already pins the platform.

### Connect your MCP client

```json
{
  "mcpServers": {
    "blender": {
      "url": "https://blender.<you>.prod.page/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_AUTH_TOKEN" }
    }
  }
}
```

Stdio-only clients can bridge with `mcp-remote`:

```json
{
  "mcpServers": {
    "blender": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://blender.<you>.prod.page/mcp",
               "--header", "Authorization: Bearer YOUR_MCP_AUTH_TOKEN"]
    }
  }
}
```

Verify any deployment:

```bash
./scripts/smoke-test.sh https://blender.<you>.prod.page "$MCP_AUTH_TOKEN"
```

---

## Getting the model out to a local AI

**Yes — a local AI can retrieve the actual 3D model, not just pictures of it.**
Blender runs remotely; the geometry comes back as real files. Verified end to end:
a Suzanne mesh exported from the container and parsed on the local machine with
**1,966 vertices / 968 triangles** intact.

There are two paths, and they serve different purposes:

| What the AI needs | How | Cost |
|---|---|---|
| To *see* the scene | `look` → PNG returned inline over MCP | small, in-context |
| To *reason about* geometry | `execute_blender_code` returning counts, coordinates, bounds | small, text only |
| To *have the model* | export to `/data`, then download over HTTPS | full file, out of context |

### One command

```bash
./scripts/fetch-model.sh https://blender.<you>.prod.page "$MCP_AUTH_TOKEN" glb ./downloads
```

Formats: `glb` (default), `obj`, `stl`, `blend`, `all`. Needs only `curl` — the AI
does not have to run on the same machine as Blender.

### Or do it by hand

Ask the AI to export through MCP:

```python
import bpy
for o in bpy.data.objects:
    o.select_set(o.type == 'MESH')      # skip camera/light in game/print assets
bpy.ops.export_scene.gltf(filepath="/data/model.glb",
                          export_format="GLB", use_selection=True)
```

Then list and fetch:

```bash
# Discover what exists (token required)
curl -H "Authorization: Bearer $TOKEN" https://blender.<you>.prod.page/files

# Download it
curl -H "Authorization: Bearer $TOKEN" \
     https://blender.<you>.prod.page/files/model.glb -o model.glb
```

`GET /files` returns JSON with names, sizes and URLs, so the AI never has to guess
filenames. Blender's internal prefs folder is excluded.

### Verified working headless

| Format | Operator | Result |
|---|---|---|
| GLB / glTF | `export_scene.gltf` | Valid glTF 2.0; Draco compression available |
| OBJ (+MTL) | `wm.obj_export` | 515 verts / 506 faces, materials sidecar included |
| STL | `wm.stl_export` | 980 triangles, valid binary — 3D-print ready |
| PLY / FBX | `wm.ply_export`, `export_scene.fbx` | Available |
| .blend | `wm.save_as_mainfile` | Zstd-compressed; reopens in a fresh Blender with all objects |

The downloaded `.blend` was reopened in a separate Blender process and every
object was present — a full round trip, not just a byte transfer.

> **Note on vertex counts:** GLB reported 1,966 vertices for Suzanne while the
> `.blend` has 507. That's correct — glTF splits vertices at UV and normal seams.
> Nothing is lost.

### Practical limits

- **Don't pipe meshes through MCP text.** A model as base64 in a tool result
  would blow the context window. Export to `/data` and download instead.
- **Download speed** is 50 Mbit/s per app on the free plan (200 on Pro), so a
  100 MB scene takes ~16 s. Fine for assets, slow for huge scenes.
- **Export memory** counts against the 512 MB ceiling. Dense meshes can OOM on
  Small; decimate first or use Medium.
- Files live in `/data`, which persists across restarts and sleep.

---

## The free plan's sleep limit

A free app with an address sleeps after 15 minutes, and **requests from programs
do not wake it** — only a person opening it in a browser does. An MCP client is a
program, so once the app sleeps, tool calls fail until you open the status page.

- Free: open `https://blender.<you>.prod.page/` in a browser before a session.
- Pro: enable **Keep it awake** in Settings.

Work survives sleep: the scene autosaves to `/data/scene.blend` and is restored on
the next start. It also saves on graceful shutdown.

---

## Run locally

```bash
./setup.sh          # generates a token, builds, starts, waits, smoke-tests
```

Or manually:

```bash
cp .env.example .env      # then edit MCP_AUTH_TOKEN
docker compose up --build
open http://localhost:8080
```

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BLENDER_MODE` | `headless` | `headless` = `blender -b`, ~243 MB. `gui` = Xvfb + OpenGL, ~711 MB, needs Medium+. |
| `PORT` | `8080` | Public HTTP port. blitz sets this. |
| `MCP_AUTH_TOKEN` | random per boot | Bearer token for `/mcp` and `/files`. **Set it**, or it changes every restart. |
| `AUTOSAVE_SECONDS` | `300` | Autosave to `/data/scene.blend`. `0` disables. |
| `LOOK_ENGINE` | `CYCLES` | Engine for `look`. Don't use Workbench on Small — it doubles memory. |
| `LOOK_SAMPLES` | `16` | Samples for `look`. |
| `LOOK_MAX_SIZE` | `640` | Hard cap on `look`'s longest edge, whatever the client asks for. |
| `CYCLES_SAMPLES` | `32` | Default scene samples. |
| `CYCLES_CPU_THREADS` | `1` | Render threads. Each extra thread adds memory. |
| `DEFAULT_RES_X` / `DEFAULT_RES_Y` | `640` / `480` | Initial scene resolution. |
| `MEMORY_CACHE_MB` | `128` | Blender cache cap (upstream default 4096). |
| `UNDO_STEPS` / `UNDO_MEMORY_MB` | `4` / `64` | Undo budget. |
| `GL_TEXTURE_LIMIT` | `CLAMP_512` | Max texture size. |
| `MALLOC_ARENA_MAX` | `2` | Stops glibc opening one malloc arena per thread. |
| `MCP_ALLOWED_HOSTS` | empty | Host allowlist. Empty = any host (auth still required). |
| `MCP_STATELESS` | `1` | Stateless sessions; survives restarts and sleep. |
| `LOG_LEVEL` | `WARNING` | `INFO` to debug. |
| `USE_POLYHAVEN` / `USE_SKETCHFAB` / `USE_HYPER3D` | `0` | Asset libraries; the latter two need API keys. |

Telemetry is disabled.

---

## What the MCP client can do

Nine tools, including `execute_blender_code` (arbitrary `bpy`), `get_scene_info`,
`look`, `search_assets` / `import_asset`, `generate_3d`, `get_addon_status`.

Renders written to `/data` are downloadable:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://blender.<you>.prod.page/files/final.png -o render.png
```

In scripts, write into `/data` so files persist:

```python
bpy.context.scene.render.filepath = "/data/final.png"
bpy.ops.render.render(write_still=True)
```

### Headless vs GUI mode

| | headless (default) | gui (`BLENDER_MODE=gui`) |
|---|---|---|
| Idle memory | ~243 MB | ~711 MB |
| Fits Small (512 MB) | Yes | No |
| `look` | Cycles camera render | True viewport capture |
| EEVEE | Rewritten to Cycles | Works (software GL, slow) |
| Viewport click-picking | Not available | Available |
| Cycles CPU rendering | Yes | Yes |

In headless mode `look` renders from the scene camera. If the scene has no camera,
a temporary one is created and removed afterwards. Your render settings are
saved and restored around every `look`.

---

## Security

`execute_blender_code` is **arbitrary code execution**, so `/mcp` requires a
bearer token and generates a random one if unset — closed by default.

- Set your own `MCP_AUTH_TOKEN` and treat it like a password.
- The Blender bridge binds `127.0.0.1:9876` and is never public.
- `/healthz` and `/readyz` are unauthenticated for platform probes; they expose
  only liveness, not scene data.
- Prefer header auth over `?token=` in URLs, which end up in logs.

---

## Troubleshooting

**OOM / container restarts** — you're above the ceiling. Keep renders ≤1280 px,
`CYCLES_CPU_THREADS=1`, denoising off, and don't set `LOOK_ENGINE=BLENDER_WORKBENCH`.
Or switch to Medium for a 1 GB ceiling (free plan allows it).

**`/readyz` returns 503** — Blender is still booting (~10 s), or the addon failed.
Look for `[blender-headless]` lines in Logs.

**App stuck on "Starting"** — the gateway prints `Uvicorn running on
http://0.0.0.0:8080` within a second or two. If that's missing, check
Settings → Port is `8080`.

**"connecting the disk that holds its files" takes forever** — blitz attaching the
persistent volume, before your code runs. Give the first start five minutes. To
rule it out, remove `VOLUME ["/data"]` from the Dockerfile and drop the kept folder
in Settings; you then lose autosave persistence.

**HTTP 421 from `/mcp`** — `MCP_ALLOWED_HOSTS` doesn't match your domain. Clear it
or add the exact host.

**HTTP 401** — wrong/missing token. Without `MCP_AUTH_TOKEN` set, a new random
token is generated each restart.

**Render looks noisy** — that's `look` at 16 samples. Raise `LOOK_SAMPLES`, or do
a full render with more samples and fetch it from `/files`.

**"EEVEE requested but unsupported headless"** — expected; the bridge substituted
Cycles to keep Blender alive. Use GUI mode on Medium+ if you need EEVEE.

### Repository layout

```
Dockerfile                    multi-stage, pinned Blender + checksum, build-time addon assertion
app/supervisor.py             PID 1: gateway first, then Blender (headless or GUI)
app/gateway.py                /mcp, /healthz, /readyz, /files, status page, bearer auth
app/blender_headless.py       low-RAM bridge: queue pump, headless look, EEVEE guard, autosave
app/blender_boot.py           GUI-mode boot script (Xvfb)
scripts/smoke-test.sh         end-to-end HTTP checks
scripts/fetch-model.sh        export in remote Blender + download locally (glb/obj/stl/blend)
setup.sh                      one-command local build + run + verify
.github/workflows/docker.yml  amd64 image → ghcr.io
```

### Credits

Blender is a [Blender Foundation](https://www.blender.org) product. The MCP server
and addon are [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)
(`mcp-for-blender`, MIT). This repo packages them for blitz.cloud and is not
affiliated with either project.
