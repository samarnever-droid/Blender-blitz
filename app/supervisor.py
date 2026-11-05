"""PID 1 supervisor: start Xvfb, Blender and the HTTP gateway, in that order.

Order matters on blitz.cloud: the platform marks an app Online as soon as
something answers on $PORT. Blender needs ~20-40s to boot, so the gateway is
started FIRST and /readyz reports progress until Blender's bridge is up.
That avoids the app sitting on "Starting" while Blender loads.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DISPLAY_NUM = os.environ.get("DISPLAY_NUM", "99")
GEOMETRY = os.environ.get("SCREEN_GEOMETRY", "1024x768x24")
BLENDER_BIN = os.environ.get("BLENDER_BIN", "/opt/blender/blender")
BLENDER_PORT = int(os.environ.get("BLENDER_PORT", "9876"))
PORT = int(os.environ.get("PORT", "8080"))
BOOT_SCRIPT = str(Path(__file__).parent / "blender_boot.py")
HEADLESS_SCRIPT = str(Path(__file__).parent / "blender_headless.py")

_children: list[subprocess.Popen] = []
_shutting_down = False


def log(msg: str) -> None:
    print(f"[supervisor] {msg}", flush=True)


def _spawn(cmd: list[str], env: dict | None = None) -> subprocess.Popen:
    log("spawn: " + " ".join(cmd))
    proc = subprocess.Popen(cmd, env=env, stdout=sys.stdout, stderr=sys.stderr)
    _children.append(proc)
    return proc


def wait_for_x(display: str, timeout: float = 30.0) -> bool:
    """Xvfb is ready once its unix socket exists."""
    sock_path = Path(f"/tmp/.X11-unix/X{display.lstrip(':')}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sock_path.exists():
            return True
        time.sleep(0.2)
    return False


def wait_for_bridge(timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", BLENDER_PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def start_gateway() -> subprocess.Popen:
    """Serve HTTP immediately so blitz's readiness probe succeeds early."""
    env = os.environ.copy()
    env.setdefault("MCP_AUTH_TOKEN", os.environ.get("MCP_AUTH_TOKEN", ""))
    return _spawn(
        [
            sys.executable, "-m", "uvicorn", "app.gateway:app",
            "--host", "0.0.0.0", "--port", str(PORT),
            "--no-access-log", "--timeout-graceful-shutdown", "10",
        ],
        env=env,
    )


def start_blender() -> subprocess.Popen:
    """Start Blender in the configured mode.

    headless (default): `blender -b` plus a main-thread pump for the addon's
        command queue. ~243 MB idle, fits blitz's 512 MB Small ceiling.
    gui: real GUI on Xvfb. ~711 MB idle, needs Medium/Large, but gives true
        viewport captures and EEVEE.
    """
    mode = os.environ.get("BLENDER_MODE", "headless").strip().lower()
    resources = DATA_DIR / "blender-resources"
    resources.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            # Blender writes prefs/caches here; /data survives restarts.
            "BLENDER_USER_RESOURCES": str(resources),
            # Lets upstream's addon-status check find the installed addon.
            "BLENDERMCP_ADDONS_DIR": str(
                Path(BLENDER_BIN).parent
                / os.environ.get("BLENDER_SERIES", "4.5")
                / "scripts" / "addons_core"
            ),
            # glibc otherwise opens one malloc arena per thread, which inflates
            # RSS significantly in a small container.
            "MALLOC_ARENA_MAX": os.environ.get("MALLOC_ARENA_MAX", "2"),
        }
    )

    cmd = [BLENDER_BIN, "--factory-startup", "--enable-autoexec"]

    if mode == "gui":
        display = f":{DISPLAY_NUM}"
        _spawn(
            [
                "Xvfb", display,
                "-screen", "0", GEOMETRY,
                "+extension", "GLX", "+extension", "RANDR", "+render",
                "-noreset", "-nolisten", "tcp",
            ]
        )
        if not wait_for_x(display):
            log("WARNING: Xvfb socket did not appear; Blender may fail to start")
        env.update(
            {
                "DISPLAY": display,
                "LIBGL_ALWAYS_SOFTWARE": "1",
                "GALLIUM_DRIVER": os.environ.get("GALLIUM_DRIVER", "llvmpipe"),
            }
        )
        log("mode=gui (Xvfb + OpenGL; needs Medium size or larger)")
        cmd += ["--python", BOOT_SCRIPT]
    else:
        log("mode=headless (no Xvfb/GUI; tuned for a 512 MB ceiling)")
        cmd += ["-b", "--python", HEADLESS_SCRIPT]

    return _spawn(cmd, env=env)


def shutdown(signum, _frame):
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    log(f"signal {signum}: stopping children")
    for proc in reversed(_children):
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    deadline = time.time() + 12
    for proc in reversed(_children):
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    log("shutdown complete")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log(f"public port={PORT} mcp=/mcp blender_bridge=127.0.0.1:{BLENDER_PORT}")

    # 1. HTTP first, so the platform sees an open port right away.
    gateway = start_gateway()

    # 2. Blender on a virtual display.
    blender = start_blender()

    # 3. Report readiness once, without blocking the gateway.
    if wait_for_bridge(timeout=180):
        log("Blender bridge is up; /mcp is fully operational")
    else:
        log("WARNING: Blender bridge not reachable yet; /readyz stays 503")

    if os.environ.get("MCP_AUTH_TOKEN", "").strip():
        log("auth: using MCP_AUTH_TOKEN from the environment")
    else:
        log("auth: MCP_AUTH_TOKEN not set -- a random token was generated")
        log("auth: set MCP_AUTH_TOKEN in the Environment tab to use your own")

    # 4. Supervise: if either critical process dies, exit so the platform
    #    restarts the container instead of serving a half-broken app.
    while not _shutting_down:
        time.sleep(2)
        if gateway.poll() is not None:
            log(f"gateway exited ({gateway.returncode}); shutting down")
            shutdown(signal.SIGTERM, None)
        if blender.poll() is not None:
            log(f"Blender exited ({blender.returncode}); shutting down")
            shutdown(signal.SIGTERM, None)


if __name__ == "__main__":
    main()
