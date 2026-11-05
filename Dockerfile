# syntax=docker/dockerfile:1
# Blender + MCP (Model Context Protocol) for blitz.cloud
#
# One container, one public port ($PORT, default 8080):
#   /mcp      -> MCP Streamable HTTP endpoint (bearer-token protected)
#   /healthz  -> liveness
#   /readyz   -> readiness (Blender bridge reachable)
#   /         -> status page
#
# Blender's MCP addon refuses to run under `blender -b` because its command
# queue is drained by bpy.app.timers, which only fire in a GUI event loop.
# So we run the real GUI on a virtual X display (Xvfb) with software OpenGL.

# ---------------------------------------------------------------------------
# Stage 1: download, verify and slim down Blender
# ---------------------------------------------------------------------------
FROM debian:trixie-slim AS blender

ARG BLENDER_SERIES=4.5
ARG BLENDER_VERSION=4.5.9
ARG BLENDER_SHA256=dcdc3eca6c9825bb35a8033b689c053f3cb5a9b0cd2a61b2eac2a49436b4ad3d

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN set -eux; \
    curl -fsSL -o blender.tar.xz \
      "https://download.blender.org/release/Blender${BLENDER_SERIES}/blender-${BLENDER_VERSION}-linux-x64.tar.xz"; \
    echo "${BLENDER_SHA256}  blender.tar.xz" | sha256sum -c -; \
    mkdir -p /opt/blender; \
    tar -xJf blender.tar.xz --strip-components=1 -C /opt/blender; \
    rm blender.tar.xz

# Remove only things that are safe in a CPU-only container:
#  - Cycles GPU kernels (CUDA/HIP blobs, loaded via dlopen at runtime)
#  - GPU denoiser backends and the HIP runtime (also dlopened)
#  - translations (UI is English here) and CPython's test/IDE suites
# NOTE: do NOT delete lib/*.so such as libcycles_kernel_oneapi_aot.so or
# lib/mesa -- those are hard DT_NEEDED entries and Blender won't start.
RUN set -eux; \
    find /opt/blender/${BLENDER_SERIES}/scripts/addons_core/cycles/lib -type f \
      \( -name '*.cubin.zst' -o -name '*.hipfb.zst' -o -name '*.fatbin.zst' \) -delete; \
    rm -f /opt/blender/lib/libhiprt64.so* \
          /opt/blender/lib/libOpenImageDenoise_device_hip.so* \
          /opt/blender/lib/libOpenImageDenoise_device_cuda.so*; \
    rm -rf /opt/blender/${BLENDER_SERIES}/datafiles/locale \
           /opt/blender/${BLENDER_SERIES}/python/lib/python3.11/test \
           /opt/blender/${BLENDER_SERIES}/python/lib/python3.11/idlelib \
           /opt/blender/${BLENDER_SERIES}/python/lib/python3.11/tkinter \
           /opt/blender/${BLENDER_SERIES}/python/lib/python3.11/lib2to3; \
    find /opt/blender -name '__pycache__' -type d -prune -exec rm -rf {} +; \
    ldd /opt/blender/blender | grep -i 'not found' && exit 1 || true

# ---------------------------------------------------------------------------
# Stage 2: Python dependencies for the MCP server / gateway
# ---------------------------------------------------------------------------
FROM debian:trixie-slim AS pydeps

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM debian:trixie-slim

ARG BLENDER_SERIES=4.5
ARG MCP_ADDON_VERSION=2.2.7

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:/opt/blender:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
      # X server + GL, so Blender's GUI loop and offscreen GL work headless
      xvfb xauth libgl1 libglx-mesa0 libgl1-mesa-dri libegl1 libegl-mesa0 \
      libxi6 libxxf86vm1 libxfixes3 libxrender1 libxext6 libx11-6 libxcb1 \
      libxkbcommon0 libsm6 libice6 libdbus-1-3 libvulkan1 libgomp1 libuuid1 \
      libfontconfig1 libfreetype6 \
      python3 ca-certificates tini curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=blender /opt/blender /opt/blender
COPY --from=pydeps  /opt/venv   /opt/venv

# Install the MCP addon where Blender 4.5 actually looks for it.
#
# Blender's addon_utils.paths() uses `addons_core` for the FIRST script path
# (the bundled install) and `addons` only for later/user paths. Dropping the
# file in scripts/addons here is silently ignored, so it must go in
# addons_core. This also makes it work regardless of $HOME or the runtime uid.
RUN set -eux; \
    ADDON_SRC="$(/opt/venv/bin/python -c 'import blender_mcp.addon_manager as m; print(m.get_bundled_addon_path())')"; \
    mkdir -p "/opt/blender/${BLENDER_SERIES}/scripts/addons_core"; \
    cp "$ADDON_SRC" "/opt/blender/${BLENDER_SERIES}/scripts/addons_core/addon.py"; \
    /opt/blender/blender --version | head -1; \
    BLENDER_USER_RESOURCES=/tmp/verify /opt/blender/blender -b --factory-startup \
      --python-expr "import addon_utils, bpy; addon_utils.enable('addon', default_set=True); assert hasattr(bpy.types.Scene, 'blendermcp_port'), 'MCP addon did not register'; print('ADDON VERIFY OK')" \
      | tee /tmp/verify.log; \
    grep -q 'ADDON VERIFY OK' /tmp/verify.log

WORKDIR /app
COPY app/ /app/app/

# Persistent work area (declare it so blitz keeps it across restarts).
RUN mkdir -p /data && chmod 777 /data
VOLUME ["/data"]

# Defaults are tuned for blitz.cloud's Small size (512 MB ceiling).
# BLENDER_MODE=headless avoids Xvfb + OpenGL entirely: ~243 MB idle instead of
# ~711 MB. Set BLENDER_MODE=gui (and a Medium/Large size) for true viewport
# captures and EEVEE.
ENV PORT=8080 \
    BLENDER_MODE=headless \
    BLENDER_HOST=127.0.0.1 \
    BLENDER_PORT=9876 \
    BLENDER_BIN=/opt/blender/blender \
    BLENDER_SERIES=${BLENDER_SERIES} \
    DATA_DIR=/data \
    DISPLAY_NUM=99 \
    SCREEN_GEOMETRY=1024x768x24 \
    AUTOSAVE_SECONDS=300 \
    DISABLE_TELEMETRY=1 \
    BLENDER_MCP_DISABLE_TELEMETRY=1 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe \
    LP_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2 \
    MEMORY_CACHE_MB=128 \
    UNDO_STEPS=4 \
    UNDO_MEMORY_MB=64 \
    GL_TEXTURE_LIMIT=CLAMP_512 \
    CYCLES_SAMPLES=32 \
    CYCLES_CPU_THREADS=1 \
    LOOK_ENGINE=CYCLES \
    LOOK_SAMPLES=16 \
    LOOK_MAX_SIZE=640 \
    DEFAULT_RES_X=640 \
    DEFAULT_RES_Y=480

# blitz.cloud routes HTTPS traffic to this single port.
EXPOSE 8080

# tini reaps zombies from Xvfb/Blender and forwards SIGTERM.
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "app.supervisor"]
