# syntax=docker/dockerfile:1.7
# =============================================================================
# ScaleBotAI API - production multi-stage Dockerfile
# -----------------------------------------------------------------------------
# Build context is the repository ROOT (so requirements.txt and src/ are visible):
#
#   docker build -f docker/api/Dockerfile -t scalebotai/scalebotai-api:local .
#
# Stages (pick one with --target):
#   builder       compilers + pip; installs every dependency into /opt/venv
#   test          builder + dev dependencies + source + tests; RUNS pytest at build time
#                   docker build -f docker/api/Dockerfile --target test .
#   test-reports  exports the JUnit XML produced by the test stage to the host
#                   docker build -f docker/api/Dockerfile --target test-reports --output type=local,dest=reports .
#   runtime       DEFAULT (last stage): slim, non-root, only the venv + app code
#
# BuildKit only builds the stages the chosen target depends on, so a normal
# `docker build` (target = runtime) never executes the test stage.
# =============================================================================

# Both stages MUST share the same base image: the venv built in "builder" stores
# absolute paths to /usr/local/bin/python3.12, which must also exist in "runtime".
ARG PYTHON_IMAGE=python:3.12-slim


# =============================================================================
# Stage 1: builder - everything needed to INSTALL dependencies (never shipped)
# =============================================================================
FROM ${PYTHON_IMAGE} AS builder

# PYTHONDONTWRITEBYTECODE: no stray .pyc files written by build-time tools.
# PIP_DISABLE_PIP_VERSION_CHECK: quieter, faster pip.
# NOTE: we deliberately do NOT set PIP_NO_CACHE_DIR, because the BuildKit cache
# mount below *is* the pip cache - and it never ends up inside an image layer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Compilers/headers for any dependency that has no pre-built wheel for the
# target platform (e.g. a new Python release or an unusual CPU architecture).
# They live ONLY in this stage, so they add zero bytes to the final image.
# The apt cache mounts keep downloaded .deb files between builds (not in layers).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get update \
 && apt-get install -y --no-install-recommends build-essential

# An isolated virtual environment is trivial to copy between stages: one
# directory holds every installed package and console script (uvicorn, ...).
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

# LAYER-CACHING RULE: copy the dependency manifest BEFORE the source code.
# Editing a .py file does not change requirements.txt, so Docker reuses the
# (slow) "pip install" layer below and rebuilds in seconds instead of minutes.
COPY requirements.txt .

# --mount=type=cache persists ~/.cache/pip across builds on the build host,
# so even when requirements.txt changes only new/changed wheels are downloaded.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt


# =============================================================================
# Stage 2: test - used by CI ("docker build --target test"). The build FAILS
# if any test fails, so a broken commit can never produce an image.
# Unit tests use SQLite (aiosqlite) + fakeredis: no external services needed.
# =============================================================================
FROM builder AS test

WORKDIR /workspace

# Dev/test tooling (pytest, ruff, fakeredis, aiosqlite, ...). requirements-dev.txt
# may include "-r requirements.txt", so both files are copied side by side.
COPY requirements.txt requirements-dev.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-dev.txt

# pyproject.toml carries the pytest config (pythonpath = ["src"]) and ruff config.
COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests

# Unit + integration suites (pyproject testpaths); both use SQLite + fakeredis.
# Override at build time, e.g. --build-arg PYTEST_ARGS="tests/unit -q"
ARG PYTEST_ARGS="-q"
RUN mkdir -p /workspace/reports \
 && pytest ${PYTEST_ARGS} -p no:cacheprovider --junitxml=/workspace/reports/junit.xml


# =============================================================================
# Stage 3: test-reports - an empty (scratch) stage holding only the JUnit file,
# so CI can export it with:  --target test-reports --output type=local,dest=reports
# =============================================================================
FROM scratch AS test-reports
COPY --from=test /workspace/reports/ /


# =============================================================================
# Stage 4: runtime - the image that is pushed to Docker Hub and runs in
# Compose / Kubernetes. Contains: slim Python + /opt/venv + app code. Nothing else.
# =============================================================================
FROM ${PYTHON_IMAGE} AS runtime

# PYTHONDONTWRITEBYTECODE=1  the code directory is root-owned/read-only for the app
#                            user (and the container root FS is read-only in Compose),
#                            so never try to write .pyc files.
# PYTHONUNBUFFERED=1         stdout/stderr are unbuffered -> JSON log lines reach
#                            `docker logs` / Filebeat immediately.
# PATH                       makes /opt/venv/bin/uvicorn the default "uvicorn".
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:${PATH}"

# Dedicated, unprivileged system user with FIXED numeric ids (10001). Fixed ids
# matter because Kubernetes "runAsUser: 10001 / runAsNonRoot: true" and volume
# ownership checks work on numbers, not names. No home dir, no login shell.
# /var/log/scalebotai is created here, owned by appuser: when Compose mounts the
# empty named volume "app-logs" on it, Docker copies this ownership into the volume.
RUN groupadd --system --gid 10001 appuser \
 && useradd --system --uid 10001 --gid appuser \
        --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin appuser \
 && mkdir -p /var/log/scalebotai \
 && chown appuser:appuser /var/log/scalebotai \
 && chmod 0755 /var/log/scalebotai

# Only the ready-made virtual environment crosses the stage boundary: no
# compilers, no pip cache, no apt lists, no build directory.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Application code goes in LAST: it changes on almost every commit, so keeping it
# in the final layer means every layer above stays cached. The files stay owned
# by root (read-only for appuser): a compromised process cannot rewrite its own code.
COPY src/app /app/app

# Documentation only; publishing is decided by `docker run -p` / Compose / k8s.
EXPOSE 8000

# Docker-level health probe (used by `docker ps` and Compose depends_on).
# Liveness only: /health/live does no DB/Redis checks, so a database outage does
# not cause restarts. Uses the Python stdlib -> no curl/wget needed in the image.
# (Kubernetes ignores HEALTHCHECK and uses its own startup/liveness/readiness probes.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"]

# Build metadata (OCI image-spec annotations). Declared near the end on purpose:
# VCS_REF/BUILD_DATE change on every build, and placing them late keeps all the
# expensive layers above cacheable. Inspect with:
#   docker image inspect --format '{{json .Config.Labels}}' <image>
ARG VERSION=1.0.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG VCS_URL=https://github.com/your-org/scalebotai
LABEL org.opencontainers.image.title="scalebotai-api" \
      org.opencontainers.image.description="ScaleBotAI - cloud-native DevOps learning-assistant chatbot API (FastAPI)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${VCS_URL}" \
      org.opencontainers.image.vendor="ScaleBotAI Capstone" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim"

# Drop root. Numeric form so Kubernetes can verify runAsNonRoot without
# resolving /etc/passwd inside the image.
USER 10001:10001

# Exec form (JSON array): uvicorn runs as PID 1 and receives SIGTERM directly,
# giving graceful shutdown on `docker stop` and during rolling updates. ONE worker
# per container: scale by adding containers/pods (Compose --scale, k8s HPA), which
# keeps Prometheus metrics single-process and lets the orchestrator do the scaling.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--workers", "1"]
