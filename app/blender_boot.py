"""Runs *inside* Blender's embedded Python at startup.

Responsibilities:
  1. Enable the MCP addon and start its socket bridge on 127.0.0.1:$BLENDER_PORT.
  2. Keep the GUI event loop alive (that loop is what drains MCP commands).
  3. Restore / autosave the scene to /data so work survives blitz restarts
     and the free plan's scale-to-zero sleep.

This file is executed by `blender --python app/blender_boot.py`, NOT by the
system Python, so `bpy` is available here.
"""

import os
import traceback

import addon_utils
import bpy

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SCENE_FILE = os.path.join(DATA_DIR, "scene.blend")
BRIDGE_PORT = int(os.environ.get("BLENDER_PORT", "9876"))
AUTOSAVE_SECONDS = int(os.environ.get("AUTOSAVE_SECONDS", "300"))
CYCLES_THREADS = int(os.environ.get("CYCLES_CPU_THREADS", "2"))


def log(msg):
    print(f"[blender-boot] {msg}", flush=True)


def _addon_ready():
    """True only when the addon's RNA properties actually exist.

    addon_utils.enable() does NOT raise when the module is missing -- it prints
    "Add-on not loaded" and returns None. Checking for the property it registers
    is the only reliable signal.
    """
    return hasattr(bpy.types.Scene, "blendermcp_port")


def enable_addon():
    """Enable the bundled MCP addon (installed as `addon.py`)."""
    if _addon_ready():
        log("addon already active")
        return True

    available = {m.__name__ for m in addon_utils.modules()}
    for module in ("addon", "blender_mcp_addon"):
        if module not in available:
            continue
        try:
            addon_utils.enable(module, default_set=True, persistent=True)
        except Exception:
            traceback.print_exc()
        if _addon_ready():
            log(f"addon enabled: {module}")
            return True

    log("ERROR: MCP addon not found on Blender's script paths")
    log(f"searched modules: {sorted(available)[:20]}")
    log("expected addon.py in Blender's scripts/addons directory")
    return False


def configure_bridge():
    """Point the addon at loopback and make sure the bridge autostarts."""
    scene = bpy.context.scene
    if scene is None or not _addon_ready():
        log("bridge config skipped: addon properties unavailable")
        return
    scene.blendermcp_port = BRIDGE_PORT
    scene.blendermcp_auto_start_server = True

    # Keep the bridge on loopback: only the in-container gateway talks to it.
    # The public surface is the token-protected /mcp endpoint.
    try:
        prefs = bpy.context.preferences.addons.get("addon")
        if prefs is not None:
            prefs.preferences.bind_address = "localhost"
            prefs.preferences.allow_remote_connections = False
    except Exception:
        pass

    # Optional asset providers, off unless keys are supplied.
    for attr, env in (
        ("blendermcp_use_polyhaven", "USE_POLYHAVEN"),
        ("blendermcp_use_sketchfab", "USE_SKETCHFAB"),
        ("blendermcp_use_hyper3d", "USE_HYPER3D"),
    ):
        if hasattr(scene, attr):
            setattr(scene, attr, os.environ.get(env, "0").lower() in ("1", "true", "yes"))
    log(f"bridge configured for 127.0.0.1:{BRIDGE_PORT}")


def configure_render():
    """CPU-only defaults: there is no GPU in the container."""
    scene = bpy.context.scene
    if scene is None:
        return
    try:
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = int(os.environ.get("CYCLES_SAMPLES", "64"))
        scene.render.threads_mode = "FIXED"
        scene.render.threads = CYCLES_THREADS
    except Exception:
        log("render defaults skipped (engine not available yet)")

    # Trim memory: blitz caps container RAM well below a workstation.
    # Defaults here keep a modest scene inside the 1 GB "medium" ceiling.
    try:
        prefs = bpy.context.preferences
        prefs.edit.undo_steps = int(os.environ.get("UNDO_STEPS", "8"))
        prefs.edit.undo_memory_limit = int(os.environ.get("UNDO_MEMORY_MB", "128"))
        prefs.filepaths.save_version = 0
        # Blender defaults to a 4 GB cache, which is larger than the container.
        prefs.system.memory_cache_limit = int(os.environ.get("MEMORY_CACHE_MB", "256"))
        prefs.system.gl_texture_limit = os.environ.get("GL_TEXTURE_LIMIT", "CLAMP_1024")
    except Exception:
        log("some memory preferences unavailable on this Blender build")


def restore_scene():
    """Reopen the last autosaved scene, if any."""
    if os.path.isfile(SCENE_FILE):
        try:
            bpy.ops.wm.open_mainfile(filepath=SCENE_FILE)
            log(f"restored scene from {SCENE_FILE}")
            return True
        except Exception:
            log("could not restore scene; starting fresh")
            traceback.print_exc()
    return False


def start_autosave():
    """Periodically persist the scene so sleep/restart doesn't lose work."""
    if AUTOSAVE_SECONDS <= 0:
        log("autosave disabled")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    state = {"dirty": False}

    def mark_dirty(_scene, _depsgraph=None):
        state["dirty"] = True

    def save():
        if state["dirty"]:
            try:
                bpy.ops.wm.save_as_mainfile(filepath=SCENE_FILE, compress=True)
                state["dirty"] = False
                log(f"autosaved -> {SCENE_FILE}")
            except Exception:
                log("autosave failed")
                traceback.print_exc()
        return float(AUTOSAVE_SECONDS)

    try:
        bpy.app.handlers.depsgraph_update_post.append(mark_dirty)
    except Exception:
        # If we cannot detect edits, save unconditionally instead.
        state["dirty"] = True

    bpy.app.timers.register(save, first_interval=float(AUTOSAVE_SECONDS), persistent=True)
    log(f"autosave every {AUTOSAVE_SECONDS}s -> {SCENE_FILE}")


def report_bridge():
    """Log bridge state once, a few seconds after startup."""
    server = getattr(bpy.types, "blendermcp_server", None)
    running = bool(getattr(server, "running", False))
    log(f"bridge running={running} port={BRIDGE_PORT}")
    if not running:
        log("WARNING: bridge not up yet; the addon retries automatically")
    return None


def main():
    log(f"Blender {bpy.app.version_string} (background={bpy.app.background})")
    if bpy.app.background:
        log("ERROR: started with -b; the MCP addon needs the GUI event loop")
        return

    if not enable_addon():
        # Keep Blender alive so logs stay readable and /readyz reports the
        # failure, rather than crash-looping the whole container.
        log("MCP addon unavailable -- /mcp will report Blender as unreachable")
        return

    restore_scene()
    # Re-apply after a possible file load (load_post can reset scene props).
    configure_bridge()
    configure_render()
    start_autosave()
    bpy.app.timers.register(report_bridge, first_interval=5.0)
    log("startup complete; GUI loop stays alive to serve MCP commands")


main()
