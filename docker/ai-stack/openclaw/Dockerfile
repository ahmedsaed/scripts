# Extended OpenClaw image with development tools, Homebrew, and safe sudo
#
# Build:  docker build -t openclaw-extended:latest .
# Run:    docker compose up -d
#
# The image pre-bakes system packages so the container never needs root at runtime.
# Homebrew is installed in the image but packages live on a volume for persistence.
# Docker socket access is via GID matching, not root.

ARG OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:latest
FROM ${OPENCLAW_IMAGE}

# ── System dependencies ──────────────────────────────────────────
# Everything a reasonable development workflow might need at runtime.
# Pre-baking these avoids sudo/apt usage during normal operation.
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    \
    # Build essentials
    build-essential \
    cmake \
    pkg-config \
    \
    # Python ecosystem
    python3-pip \
    python3-venv \
    python3-dev \
    \
    # Common native lib deps (cryptography, image, ML packages)
    libssl-dev \
    libffi-dev \
    libjpeg-dev \
    libpng-dev \
    libblas-dev \
    liblapack-dev \
    libsqlite3-dev \
    \
    # Media / document tooling
    ffmpeg \
    imagemagick \
    \
    # Utilities
    curl \
    wget \
    jq \
    unzip \
    xz-utils \
    ca-certificates \
    gnupg \
    openssh-client \
    \
    # Controlled sudo — see sudoers below
    sudo \
    \
    # Vulkan GPU runtime (whisper.cpp AMD acceleration)
    mesa-vulkan-drivers \
    \
    && rm -rf /var/lib/apt/lists/*

# ── GPU access: render group for Vulkan ─────────────────────────
# Build with: docker build --build-arg RENDER_GID=$(getent group render | cut -d: -f3) .
ARG RENDER_GID=107
RUN groupadd -g ${RENDER_GID} render 2>/dev/null || true && \
    usermod -aG render node

# ── Sudo: scoped to apt-get only ─────────────────────────────────
# The node user can run `sudo apt install -y <pkg>` for one-off system deps.
# No root shell, no arbitrary commands.
RUN echo "node ALL=(root) NOPASSWD: /usr/bin/apt" \
    > /etc/sudoers.d/node && \
    chmod 0440 /etc/sudoers.d/node

# ── npm: user-owned global prefix ────────────────────────────────
# Prevents global prefix collisions with working directories.
# Also avoids EACCES when installing global packages.
# The directory itself lives on the persisted /home/node volume.
ENV NPM_CONFIG_PREFIX=/home/node/.npm-global
RUN mkdir -p /home/node/.npm-global && chown node:node /home/node/.npm-global

# ── pip: default to user installs ───────────────────────────────
# `pip install` behaves as `pip install --user` by default.
# Virtualenv activation overrides this automatically.
ENV PIP_USER=1
ENV PIP_NO_WARN_SCRIPT_LOCATION=0

# ── Homebrew ─────────────────────────────────────────────────────
# Installed as the node user (brew is designed for user-space operation).
# The Cellar lives in /home/linuxbrew/.linuxbrew which is mounted as
# a bind volume from the host for package persistence.
RUN mkdir -p /home/linuxbrew/.linuxbrew && chown node:node /home/linuxbrew/.linuxbrew
USER node
RUN NONINTERACTIVE=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    && /home/linuxbrew/.linuxbrew/bin/brew update --force --quiet

USER root

# ── PATH ─────────────────────────────────────────────────────────
# workspace bin, npm global bin first (user tools win), then brew, then system.
ENV PATH=/home/node/.openclaw/workspace/bin:/home/node/.npm-global/bin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ── Runtime user ─────────────────────────────────────────────────
# Container runs as node (non-root). The compose file can override
# the GID for docker socket access via: user: "1000:${DOCKER_GID}"
USER node
WORKDIR /app

# Keep the original entrypoint — it handles the node prefix logic.
