# Base images pinned to their multi-arch index digests (not just the moving
# tags) so the FROM layer is stable across builds; moving tags get new digests
# upstream every few days, which busts the entire layer cache and forces a full
# rebuild. Pinning keeps the registry build cache effective and the build
# reproducible. apt-get update inside the stages still pulls security patches;
# bump these digests periodically (docker buildx imagetools inspect <img>).
#   debian:trixie-slim and node:lts-slim digests captured 2026-06-02.
FROM debian:trixie-slim@sha256:4e401d95de7083948053197a9c3913343cd06b706bf15eb6a0c3ccd26f436a0e AS builder

# Install build dependencies
#
# DL3008 (pin apt versions) is intentionally ignored: build-stage uses
# generic packages (git, build-essential, cmake, pkg-config, libzstd-dev) where
# the latest patched versions are preferable to a frozen pin.  Reproducibility
# comes from the snapshot-pinned mame-tools deb in the runtime stage below, not
# from these build deps.
ENV DEBIAN_FRONTEND=noninteractive
# hadolint ignore=DL3008
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
    git \
    build-essential \
    cmake \
    pkg-config \
    libzstd-dev \
    ca-certificates && \
    git clone https://github.com/pacnpal/z3ds_compress.git /tmp/z3ds && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/z3ds

# The fork (pacnpal/z3ds_compress) builds with CMake and adds 3DS decompression
# (.zcci/.zcia/.z3ds/.zcxi/.z3dsx -> raw ROM) plus .cxi/.3dsx support. Its
# CMakeLists statically links libzstd via pkg-config (hence pkg-config above).
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build -j"$(nproc)" && \
    chmod +x build/z3ds_compressor

# ---------------------------------------------------------------------------
# maxcso builder stage: compile maxcso (PSP/PS2 ISO <-> CSO/ZSO) from source.
#
# maxcso is a plain `make` C++ build linking system liblz4 / libuv / libdeflate
# (plus zlib). Compiles per-arch automatically, so multi-arch buildx works.
# Cloning the default branch mirrors the z3ds stage; pin --branch <tag> for
# reproducibility.
# DL3008 ignored for the same reason as the z3ds builder (generic build deps).
# ---------------------------------------------------------------------------
FROM debian:trixie-slim@sha256:4e401d95de7083948053197a9c3913343cd06b706bf15eb6a0c3ccd26f436a0e AS maxcso-builder
ENV DEBIAN_FRONTEND=noninteractive
# Pinned to an immutable maxcso commit so release images are reproducible
# (the build workflow passes only APP_VERSION). This is master @ 2024-01-26,
# which has the ZSO and --crc support this app relies on; the latest *tag*
# (v1.13.0) predates them, so a commit SHA is used rather than a tag. Override
# with --build-arg MAXCSO_REF=<tag|sha> to update maxcso intentionally.
ARG MAXCSO_REF=961f232cf99d546b2b7e704c0ecf3fc5bea52221
# hadolint ignore=DL3008
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
    git \
    build-essential \
    pkgconf \
    zlib1g-dev \
    liblz4-dev \
    libuv1-dev \
    libdeflate-dev \
    ca-certificates && \
    git clone https://github.com/unknownbrackets/maxcso.git /tmp/maxcso && \
    git -C /tmp/maxcso checkout "${MAXCSO_REF}" && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/maxcso

RUN make && \
    if [ ! -f maxcso ] && [ -f bin/maxcso ]; then cp bin/maxcso maxcso; fi && \
    chmod +x maxcso

# ---------------------------------------------------------------------------
# makeps3iso builder stage: compile makeps3iso (decrypted PS3 disc/JB folder ->
# .iso) from source.
#
# bucanero/ps3iso-utils is GPL-3.0, plain portable C (no x86 asm / SIMD — the
# only arch flag is a Darwin-only `-arch x86_64`, never hit here), built with a
# bare `make` per utility directory. Compiles per-arch automatically, so
# linux/amd64 and linux/arm64 buildx both work, same as the maxcso stage. We
# ship the unmodified upstream binary as a separate executable (not linked into
# the app) and pin the source ref for GPL-3.0 source-correspondence, exactly as
# maxcso is pinned. Only makeps3iso is built/copied: verify is a pure-Python
# PARAM.SFO readback, so extractps3iso (round-trip) isn't needed.
# DL3008 ignored for the same reason as the other builders (generic build deps).
# ---------------------------------------------------------------------------
FROM debian:trixie-slim@sha256:4e401d95de7083948053197a9c3913343cd06b706bf15eb6a0c3ccd26f436a0e AS makeps3iso-builder
ENV DEBIAN_FRONTEND=noninteractive
# Pinned to an immutable commit so release images are reproducible. master @
# 2022-03-09 (latest upstream; last tagged release predates it). Override with
# --build-arg MAKEPS3ISO_REF=<tag|sha> to update makeps3iso intentionally.
ARG MAKEPS3ISO_REF=878090980a9042c61901920fed1b034af215e8c7
# hadolint ignore=DL3008
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
    git \
    build-essential \
    ca-certificates && \
    git clone https://github.com/bucanero/ps3iso-utils.git /tmp/ps3iso-utils && \
    git -C /tmp/ps3iso-utils checkout "${MAKEPS3ISO_REF}" && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/ps3iso-utils

RUN make -C makeps3iso && \
    if [ ! -f makeps3iso/makeps3iso ] && [ -f makeps3iso/bin/makeps3iso ]; then \
      cp makeps3iso/bin/makeps3iso makeps3iso/makeps3iso; \
    fi && \
    chmod +x makeps3iso/makeps3iso

# ---------------------------------------------------------------------------
# Frontend builder stage: compile the Svelte 5 SPA with Vite.
#
# Output is emitted to /build/static via vite.config.js (build.outDir),
# which the runtime stage copies into /static.  Multi-arch safe:
# node:lts-slim ships linux/amd64 and linux/arm64 manifests, and the SPA
# has no native dependencies so QEMU emulation under buildx works.
# ---------------------------------------------------------------------------
FROM node:lts-slim@sha256:242549cd46785b480c832479a730f4f2a20865d61ea2e404fdb2a5c3d3b73ecf AS frontend-builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY vite.config.js svelte.config.js index.html ./
COPY src/ ./src/
ARG APP_VERSION=dev
ENV VITE_APP_VERSION=${APP_VERSION}
RUN npm run build

FROM debian:trixie-slim@sha256:4e401d95de7083948053197a9c3913343cd06b706bf15eb6a0c3ccd26f436a0e

# ---------------------------------------------------------------------------
# Immutable pin: mame-tools 0.285+dfsg1-1 from snapshot.debian.org
#
# All runtime deps (libflac14, libsdl2-2.0-0, libutf8proc3, zlib1g, etc.)
# are satisfiable from trixie-slim's own repos; no foreign sources needed.
#
# snapshot.debian.org URLs are content-addressed and timestamp-locked;
# the .deb behind a given URL will never change.
# ---------------------------------------------------------------------------
ARG TARGETARCH

ARG MAME_TOOLS_SNAPSHOT="https://snapshot.debian.org/archive/debian/20260213T023117Z/pool/main/m/mame"
ARG MAME_TOOLS_VERSION="0.285+dfsg1-1"
ARG MAME_TOOLS_SHA256_AMD64="d99e82887aab57d9a66b2f1ffd80210aabeb064808a6d05f69af1584049fd195"
ARG MAME_TOOLS_SHA256_ARM64="6388bff0f6242dfd3a09c63c6e25ab94e0a64fe7cf2b3b0170f89ff7c13340a8"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system dependencies, pinned mame-tools, create wrapper script, and prepare venv
#
# DL3008 (pin apt versions) is intentionally ignored: mame-tools is pinned via
# the snapshot.debian.org .deb downloaded inside the RUN below; the remaining
# packages (python3, util-linux, gosu, etc.) are stable trixie security-tracked
# dependencies where pinning would block routine CVE patches.
ENV DEBIAN_FRONTEND=noninteractive
# hadolint ignore=DL3008
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      python3-venv \
      util-linux \
      unrar-free \
      p7zip-full \
      wget \
      unzip \
      zstd \
      bash \
      gosu \
      liblz4-1 \
      libuv1t64 \
      libdeflate0 \
      zlib1g \
      ca-certificates && \
    # Install dolphin-emu only where available/practical (non-fatal)
    if [ "$TARGETARCH" = "amd64" ]; then \
      apt-get install -y --no-install-recommends dolphin-emu || \
        echo "WARNING: dolphin-emu install failed on amd64; continuing without it"; \
    else \
      echo "Skipping dolphin-emu on ${TARGETARCH}"; \
    fi && \
    # --- Install pinned mame-tools from snapshot ---
    MAME_DEB="mame-tools_${MAME_TOOLS_VERSION}_${TARGETARCH}.deb" && \
    if [ "$TARGETARCH" = "amd64" ]; then \
      EXPECTED_SHA256="${MAME_TOOLS_SHA256_AMD64}"; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
      EXPECTED_SHA256="${MAME_TOOLS_SHA256_ARM64}"; \
    else \
      echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1; \
    fi && \
    wget -q "${MAME_TOOLS_SNAPSHOT}/${MAME_DEB}" -O /tmp/mame-tools.deb && \
    echo "${EXPECTED_SHA256}  /tmp/mame-tools.deb" | sha256sum -c - && \
    dpkg -i /tmp/mame-tools.deb || apt-get install -y -f --no-install-recommends && \
    rm /tmp/mame-tools.deb && \
    # --- Verify chdman version (capture fully to avoid SIGPIPE under pipefail) ---
    CHDMAN_VER="$(chdman 2>&1 || true)" && echo "$CHDMAN_VER" | grep -q "0\.285" && \
    # --- Clean up ---
    rm -rf /var/lib/apt/lists/* && \
    # Only create dolphin-tool wrapper if the binary exists
    if command -v /usr/games/dolphin-tool >/dev/null 2>&1; then \
      printf '#!/bin/bash\nexec /usr/games/dolphin-tool "$@"\n' > /usr/local/bin/dolphin-tool && \
      chmod +x /usr/local/bin/dolphin-tool; \
    fi && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir "pip>=25.3"

ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /app/
# This also installs `nsz` (Nintendo Switch NSP/XCI <-> NSZ/XCZ), which lands on
# PATH at /opt/venv/bin/nsz. nsz needs the operator's own prod.keys mounted at
# runtime (SWITCH_KEYS); no keys are baked into this image.
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install z3ds_compressor from builder stage (CMake emits it under build/)
COPY --from=builder /tmp/z3ds/build/z3ds_compressor /usr/local/bin/z3ds_compressor

# Install maxcso from its builder stage (PSP/PS2 ISO <-> CSO/ZSO)
COPY --from=maxcso-builder /tmp/maxcso/maxcso /usr/local/bin/maxcso

# Install makeps3iso from its builder stage (decrypted PS3 folder -> .iso).
# GPL-3.0, shipped unmodified as a standalone binary (see the builder stage).
COPY --from=makeps3iso-builder /tmp/ps3iso-utils/makeps3iso/makeps3iso /usr/local/bin/makeps3iso

# Copy application
COPY app/ /app/
# Bring in tracked static assets (images, etc.) and then overlay the
# Vite-built SPA (index.html + hashed assets/) from the frontend-builder
# stage. emptyOutDir=false in vite.config.js, plus this ordering, keeps
# /static/images alongside the generated /static/index.html + /static/assets.
COPY static/ /static/
COPY --from=frontend-builder /build/static/ /static/
COPY migrations/ /migrations/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app

# Version injected from GitHub release tag at build time
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Configuration
ENV COMPRESSATORIUM_MOUNT_ROOT="/data"
ENV CHD_MODE="webui"
ENV CHDMAN_MODE="createcd"
ENV MAX_CONCURRENT_JOBS=1
# Process-priority defaults (nice 10, ioprio best-effort/6) come from the
# application config, NOT baked image ENV. Baking COMPRESSATORIUM_TOOL_* here
# would shadow the legacy CHD_CHDMAN_* aliases (which have lower precedence),
# so `docker run -e CHD_CHDMAN_NICE=5` would be silently ignored. Leaving them
# unset lets either the new or legacy env override take effect.
ENV PYTHONUNBUFFERED=1

# Default volume mount point
VOLUME ["/data/games"]

# Expose web port
EXPOSE 8080

# Health check (only applies in webui mode)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD if [ "${CHD_MODE:-webui}" = "cli" ]; then \
            exit 0; \
        fi; \
        if [ "$(id -u)" = "0" ]; then \
            gosu converter python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"; \
        else \
            python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"; \
        fi

# Create runtime user/group (pinned to 999:999) and prepare ownership for entrypoint privilege drop
RUN groupadd -r -g 999 converter && useradd -r -u 999 -g converter -s /sbin/nologin converter \
    && chown -R converter:converter /app /static /opt/venv \
    && mkdir -p /data/games /config \
    && chown converter:converter /data/games /config

# nosemgrep: dockerfile.security.missing-user-entrypoint.missing-user-entrypoint
# The container deliberately starts as root so entrypoint.sh can honour the
# PUID/PGID env vars (`usermod`/`groupmod`/`chown` require root) before
# `exec gosu converter "$0" "$@"` drops privileges to the unprivileged
# converter user (uid 999) for the actual application.  Adding `USER` here
# would prevent the runtime UID/GID remap that lets the container write
# host-correct ownership on bind-mounted volumes.
ENTRYPOINT ["/entrypoint.sh"]
