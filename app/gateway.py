"""Public HTTP surface for the container.

blitz.cloud gives an app exactly ONE reachable TCP port, so everything is
served from one Starlette app on $PORT:

    POST/GET/DELETE /mcp   MCP Streamable HTTP (bearer token required)
    GET  /healthz          liveness  -- always 200 while the process is up
    GET  /readyz           readiness -- 200 only when Blender's bridge answers
    GET  /                 human-readable status page
    GET  /files/<name>     download renders/autosaves from /data (token required)

Two upstream defaults are corrected here, both of which otherwise fail behind
blitz's HTTPS proxy:

  * FastMCP enables DNS-rebinding protection with a localhost-only allowlist,
    which rejects the public Host header with HTTP 421.
  * Sessions are kept in process memory; blitz can restart or scale the app to
    zero, so stateless mode avoids clients holding a dead session id.
"""

from __future__ import annotations

import html
import os
import secrets
import socket
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BLENDER_HOST = os.environ.get("BLENDER_HOST", "127.0.0.1")
BLENDER_PORT = int(os.environ.get("BLENDER_PORT", "9876"))
STARTED_AT = time.time()

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
# Never expose an unauthenticated Blender: execute_blender_code runs arbitrary
# Python. If no token is configured we generate one and print it to the logs,
# so a fresh deploy is closed by default rather than open.
_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
GENERATED_TOKEN = False
if not _TOKEN:
    _TOKEN = secrets.token_urlsafe(32)
    GENERATED_TOKEN = True


def token() -> str:
    return _TOKEN


def _authorized(request) -> bool:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    else:
        # Some clients can only set a query parameter.
        supplied = request.query_params.get("token", "")
    return bool(supplied) and secrets.compare_digest(supplied, _TOKEN)


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": "Send 'Authorization: Bearer <MCP_AUTH_TOKEN>'."},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


# --------------------------------------------------------------------------
# Blender bridge probe
# --------------------------------------------------------------------------
def bridge_up(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((BLENDER_HOST, BLENDER_PORT), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# MCP app
# --------------------------------------------------------------------------
def _configure_logging():
    """Keep blitz's log viewer readable.

    In stateless mode upstream runs its startup/shutdown lifespan for every
    request, which logs several INFO lines each time. Default to WARNING so
    the Logs tab shows problems rather than a wall of routine chatter.
    """
    import logging

    level = os.environ.get("LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("BlenderMCPServer", "blender-mcp-telemetry", "mcp", "httpx"):
        logging.getLogger(name).setLevel(level)


def build_mcp_app():
    """Import upstream's FastMCP server and adapt it to a proxied deployment."""
    _configure_logging()
    from blender_mcp.server import mcp

    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("PORT", "8080"))
    mcp.settings.streamable_http_path = "/mcp"

    # Stateless: every request stands alone, so a restart/sleep of the app
    # cannot invalidate a client's session id.
    if os.environ.get("MCP_STATELESS", "1").lower() in ("1", "true", "yes"):
        mcp.settings.stateless_http = True

    # blitz terminates TLS and forwards its own Host/Origin. The localhost-only
    # default would answer 421/403 for every real request.
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = os.environ.get("MCP_ALLOWED_HOSTS", "").split(",")
    allowed_hosts = [h.strip() for h in allowed_hosts if h.strip()]
    if allowed_hosts:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts],
        )
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )

    return mcp.streamable_http_app()


class TokenGate:
    """ASGI middleware: require the bearer token on /mcp and /files only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith("/mcp") or path.startswith("/files"):
                from starlette.requests import Request

                if not _authorized(Request(scope)):
                    await _unauthorized()(scope, receive, send)
                    return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------
# Plain routes
# --------------------------------------------------------------------------
async def healthz(_request):
    return JSONResponse({"status": "ok", "uptime_seconds": round(time.time() - STARTED_AT, 1)})


async def readyz(_request):
    up = bridge_up()
    return JSONResponse(
        {"status": "ready" if up else "starting", "blender_bridge": up},
        status_code=200 if up else 503,
    )


async def list_files(request):
    """Token-protected index of /data so a client can discover exports.

    StaticFiles deliberately has no directory listing, which means a remote AI
    would have to guess filenames. This returns the names, sizes and URLs.
    """
    entries = []
    base = DATA_DIR.resolve()
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        # Blender's own prefs/cache live here; they are not user artifacts.
        if rel.split("/")[0] == "blender-resources":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": rel,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
                "url": f"/files/{rel}",
            }
        )
    return JSONResponse({"count": len(entries), "files": entries})


async def index(request):
    up = bridge_up()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost:8080")
    scheme = request.headers.get("x-forwarded-proto", "http")
    endpoint = f"{scheme}://{host}/mcp"
    dot = "#22c55e" if up else "#f59e0b"
    state = "Blender is ready" if up else "Blender is still starting"
    rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
        for k, v in (
            ("MCP endpoint", endpoint),
            ("Transport", "Streamable HTTP (stateless)"),
            ("Auth", "Bearer token required"),
            ("Blender bridge", f"{BLENDER_HOST}:{BLENDER_PORT}"),
            ("Uptime", f"{int(time.time() - STARTED_AT)}s"),
        )
    )
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blender MCP</title>
<style>
 :root{{color-scheme:dark}}
 body{{margin:0;background:#0b0f14;color:#e6edf3;
   font:16px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
   display:flex;justify-content:center;padding:40px 20px}}
 main{{max-width:720px;width:100%}}
 h1{{font-size:1.6rem;margin:0 0 4px}}
 .sub{{color:#8b98a5;margin:0 0 24px}}
 .pill{{display:inline-flex;align-items:center;gap:8px;background:#131a22;
   border:1px solid #232c36;border-radius:999px;padding:6px 14px;font-size:.9rem}}
 .dot{{width:9px;height:9px;border-radius:50%;background:{dot}}}
 table{{width:100%;border-collapse:collapse;margin:24px 0;font-size:.92rem}}
 td{{padding:9px 0;border-bottom:1px solid #1c242e;vertical-align:top}}
 td:first-child{{color:#8b98a5;width:170px}}
 pre{{background:#131a22;border:1px solid #232c36;border-radius:10px;
   padding:14px;overflow-x:auto;font-size:.82rem}}
 code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
 a{{color:#58a6ff}}
</style></head><body><main>
<h1>Blender MCP</h1>
<p class="sub">Headless Blender with a Model Context Protocol endpoint.</p>
<span class="pill"><span class="dot"></span>{state}</span>
<table>{rows}</table>
<p>Connect an MCP client to <code>{html.escape(endpoint)}</code> with your
<code>MCP_AUTH_TOKEN</code> as a bearer token:</p>
<pre><code>{{
  "mcpServers": {{
    "blender": {{
      "url": "{html.escape(endpoint)}",
      "headers": {{ "Authorization": "Bearer YOUR_TOKEN" }}
    }}
  }}
}}</code></pre>
<p class="sub">Health: <a href="/healthz">/healthz</a> &middot;
Readiness: <a href="/readyz">/readyz</a></p>
</main></body></html>"""
    return HTMLResponse(body)


def build_app() -> Starlette:
    mcp_app = build_mcp_app()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Mount at "/" so the inner path stays exactly /mcp.
    routes = [
        Route("/", index, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        # Exact /files -> JSON index; /files/<name> -> the file itself.
        Route("/files", list_files, methods=["GET"]),
        Route("/files/", list_files, methods=["GET"]),
        Mount("/files", app=StaticFiles(directory=str(DATA_DIR)), name="files"),
        Mount("/", app=mcp_app),
    ]

    # The MCP app owns a lifespan (session manager); reuse it for the parent.
    app = Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context)
    app.add_middleware(TokenGate)
    return app


app = build_app()
