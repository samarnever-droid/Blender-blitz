"""Low-memory Blender bridge: runs under `blender -b`, no Xvfb, no GUI.

Why this exists
---------------
The MCP addon refuses background mode because it drains its command queue from
`bpy.app.timers`, and those timers only fire in a GUI event loop. But the queue
drainer is just a method -- so this script calls it directly from a main-thread
loop. All `bpy` work still happens on Blender's main thread, which is the actual
requirement.

Measured on Blender 4.5.9: ~244 MB at idle vs ~711 MB for the Xvfb + GUI stack,
because the window manager, OpenGL context and llvmpipe never load.

Trade-offs (documented in README):
  * `look` renders from the scene camera with Workbench instead of grabbing a
    viewport. Workbench works headless and takes ~0.3 s.
  * EEVEE is unavailable: it needs a GPU context and *kills the process* in
    background mode. Engine requests for EEVEE are rewritten to Cycles.
  * Viewport-only tools (pick_viewport_object) have no window to click into.
"""

import os
import re
import signal
import socket
import sys
import threading
import time
import traceback

import addon_utils
import bpy

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SCENE_FILE = os.path.join(DATA_DIR, "scene.blend")
BRIDGE_PORT = int(os.environ.get("BLENDER_PORT", "9876"))
AUTOSAVE_SECONDS = int(os.environ.get("AUTOSAVE_SECONDS", "300"))
# Cycles CPU, not Workbench: Workbench pulls in mesa/llvmpipe and roughly
# doubles peak memory (measured 491 MB vs 267 MB at 480px).
LOOK_ENGINE = os.environ.get("LOOK_ENGINE", "CYCLES")
LOOK_SAMPLES = int(os.environ.get("LOOK_SAMPLES", "16"))
LOOK_MAX_SIZE = int(os.environ.get("LOOK_MAX_SIZE", "640"))
_running = True


def log(msg):
    print(f"[blender-headless] {msg}", flush=True)


def pss_mb():
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return -1


# ---------------------------------------------------------------------------
# Addon
# ---------------------------------------------------------------------------
def enable_addon():
    if hasattr(bpy.types.Scene, "blendermcp_port"):
        return True
    available = {m.__name__ for m in addon_utils.modules()}
    for module in ("addon", "blender_mcp_addon"):
        if module not in available:
            continue
        try:
            addon_utils.enable(module, default_set=True, persistent=True)
        except Exception:
            traceback.print_exc()
        if hasattr(bpy.types.Scene, "blendermcp_port"):
            log(f"addon enabled: {module}")
            return True
    log("ERROR: MCP addon not found on Blender's script paths")
    return False


def apply_memory_guards():
    """Blender's workstation defaults assume far more RAM than blitz gives."""
    try:
        prefs = bpy.context.preferences
        prefs.edit.undo_steps = int(os.environ.get("UNDO_STEPS", "4"))
        prefs.edit.undo_memory_limit = int(os.environ.get("UNDO_MEMORY_MB", "64"))
        prefs.filepaths.save_version = 0
        prefs.system.memory_cache_limit = int(os.environ.get("MEMORY_CACHE_MB", "128"))
        prefs.system.gl_texture_limit = os.environ.get("GL_TEXTURE_LIMIT", "CLAMP_512")
    except Exception:
        log("some memory preferences unavailable")

    scene = bpy.context.scene
    if scene is not None:
        try:
            scene.render.engine = "CYCLES"
            scene.cycles.device = "CPU"
            scene.cycles.samples = int(os.environ.get("CYCLES_SAMPLES", "32"))
            scene.cycles.use_denoising = False  # OIDN costs tens of MB
            scene.render.threads_mode = "FIXED"
            scene.render.threads = int(os.environ.get("CYCLES_CPU_THREADS", "1"))
            scene.render.resolution_x = int(os.environ.get("DEFAULT_RES_X", "640"))
            scene.render.resolution_y = int(os.environ.get("DEFAULT_RES_Y", "480"))
        except Exception:
            log("render defaults skipped")


# ---------------------------------------------------------------------------
# Headless `look`
# ---------------------------------------------------------------------------
def headless_screenshot(self, max_size=800, filepath=None, format="png"):
    """Replace the addon's viewport grab with a fast camera render.

    Returns the same shape the MCP server expects, and restores every render
    setting it touched so a `look` never changes the user's scene setup.
    """
    scene = bpy.context.scene
    if scene is None:
        return {"error": "No active scene"}

    filepath = filepath or os.path.join(DATA_DIR, "look.png")
    render = scene.render
    saved = (
        render.engine, render.resolution_x, render.resolution_y,
        render.resolution_percentage, render.filepath, render.image_settings.file_format,
    )
    saved_cycles = None
    if hasattr(scene, "cycles"):
        saved_cycles = (
            scene.cycles.samples, scene.cycles.use_denoising, scene.cycles.device,
        )
    temp_cam = None
    try:
        if scene.camera is None:
            # Frame the scene from a default angle so `look` still shows something.
            import mathutils

            cam_data = bpy.data.cameras.new("MCP_LookCam")
            temp_cam = bpy.data.objects.new("MCP_LookCam", cam_data)
            scene.collection.objects.link(temp_cam)
            temp_cam.location = (7.0, -7.0, 5.0)
            direction = mathutils.Vector((0, 0, 0)) - temp_cam.location
            temp_cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            scene.camera = temp_cam

        # Cap the longest edge: render memory scales with pixel count, and a
        # client asking for 2000px would otherwise exceed the container ceiling.
        max_size = min(int(max_size or LOOK_MAX_SIZE), LOOK_MAX_SIZE)

        # Keep the longest edge at max_size, preserving the scene's aspect.
        aspect = render.resolution_x / max(render.resolution_y, 1)
        if aspect >= 1:
            width, height = int(max_size), max(1, int(max_size / aspect))
        else:
            width, height = max(1, int(max_size * aspect)), int(max_size)

        render.engine = LOOK_ENGINE
        render.resolution_x, render.resolution_y = width, height
        render.resolution_percentage = 100
        render.filepath = filepath
        render.image_settings.file_format = format.upper()

        # Cycles CPU is the cheap engine here. Workbench looks faster but loads
        # the whole mesa/llvmpipe GPU stack: measured peak RSS 491 MB vs 267 MB
        # for Cycles at the same size, which blows a 512 MB ceiling.
        if LOOK_ENGINE == "CYCLES" and hasattr(scene, "cycles"):
            scene.cycles.device = "CPU"
            scene.cycles.samples = LOOK_SAMPLES
            scene.cycles.use_denoising = False

        bpy.ops.render.render(write_still=True)

        return {
            "success": True,
            "width": width,
            "height": height,
            "filepath": filepath,
            "method": "headless-camera-render",
            "file": bpy.data.filepath,
            "scene": scene.name,
            "scene_count": len(bpy.data.scenes),
        }
    except Exception as exc:
        traceback.print_exc()
        return {"error": f"headless render failed: {exc}"}
    finally:
        (render.engine, render.resolution_x, render.resolution_y,
         render.resolution_percentage, render.filepath,
         render.image_settings.file_format) = saved
        if saved_cycles is not None:
            (scene.cycles.samples, scene.cycles.use_denoising,
             scene.cycles.device) = saved_cycles
        if temp_cam is not None:
            cam_data = temp_cam.data
            bpy.data.objects.remove(temp_cam, do_unlink=True)
            bpy.data.cameras.remove(cam_data)
            scene.camera = None


# ---------------------------------------------------------------------------
# EEVEE guard
# ---------------------------------------------------------------------------
# Selecting EEVEE in background mode aborts the process (verified: exit 137).
# A model will happily try it, so rewrite the request instead of dying.
_EEVEE = re.compile(r"BLENDER_EEVEE(?:_NEXT)?")


def guard_engine(code):
    if not _EEVEE.search(code):
        return code, False
    return _EEVEE.sub("CYCLES", code), True


def wrap_execute(server):
    original = server.execute_command

    def execute(command):
        try:
            if command.get("type") == "execute_code":
                params = command.get("params") or {}
                code = params.get("code", "")
                patched, changed = guard_engine(code)
                if changed:
                    params["code"] = patched
                    command["params"] = params
                    log("EEVEE requested but unsupported headless; using Cycles instead")
        except Exception:
            pass
        return original(command)

    server.execute_command = execute


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------
def start_bridge(server_cls):
    """Do what BlenderMCPServer.start() does, minus the background-mode guard."""
    server = server_cls(host="127.0.0.1", port=BRIDGE_PORT)
    server.get_viewport_screenshot = headless_screenshot.__get__(server, server_cls)
    wrap_execute(server)

    server.running = True
    server.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.socket.bind((server.host, server.port))
    server.socket.listen(5)
    threading.Thread(target=server._server_loop, daemon=True).start()
    bpy.types.blendermcp_server = server
    log(f"bridge listening on 127.0.0.1:{BRIDGE_PORT}")
    return server


def restore_scene():
    if os.path.isfile(SCENE_FILE):
        try:
            bpy.ops.wm.open_mainfile(filepath=SCENE_FILE)
            log(f"restored scene from {SCENE_FILE}")
        except Exception:
            log("could not restore scene; starting fresh")


def main():
    global _running

    def on_signal(signum, _frame):
        global _running
        log(f"signal {signum}: saving and exiting")
        _running = False

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log(f"Blender {bpy.app.version_string} background={bpy.app.background}")
    if not enable_addon():
        sys.exit(1)

    import addon as addon_mod

    restore_scene()
    apply_memory_guards()
    os.makedirs(DATA_DIR, exist_ok=True)
    server = start_bridge(addon_mod.BlenderMCPServer)

    log(f"idle memory ~{pss_mb():.0f} MB; ready for MCP commands")

    last_save = time.time()
    while _running:
        # This is the job bpy.app.timers does in GUI mode.
        server._drain_command_queue()
        if AUTOSAVE_SECONDS > 0 and time.time() - last_save >= AUTOSAVE_SECONDS:
            try:
                bpy.ops.wm.save_as_mainfile(filepath=SCENE_FILE, compress=True)
                log(f"autosaved -> {SCENE_FILE}")
            except Exception:
                log("autosave failed")
            last_save = time.time()
        time.sleep(0.02)

    if AUTOSAVE_SECONDS > 0:
        try:
            bpy.ops.wm.save_as_mainfile(filepath=SCENE_FILE, compress=True)
            log("saved scene on shutdown")
        except Exception:
            pass
    server.running = False
    log("stopped")


main()
