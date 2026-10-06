# syntax=docker/dockerfile:1.7
# =============================================================================
# ScaleBotAI - Jenkins controller image
#
# Jenkins LTS (JDK 17) + every tool jenkins/Jenkinsfile needs on the agent:
#   python3.12 (+ venv)       Build / Unit Test / Lint stages (ruff, bandit, pytest come from
#                             the project's .venv built from requirements-dev.txt)
#   docker CLI + buildx       Docker Build / Push (talks to the HOST daemon via the mounted socket)
#   kubectl (pinned)          Kubernetes Deploy / Rollback
#   trivy (pinned)            Security Scan
#   curl, jq, git, openssl    smoke tests, JSON parsing, TLS helpers
#   plugins.txt               installed with jenkins-plugin-cli
#
# Build:  docker compose -f jenkins/docker-compose.jenkins.yml build
# Pinned tool versions are build args - bump them deliberately and rebuild.
# =============================================================================
ARG JENKINS_TAG=lts-jdk17
ARG UV_VERSION=0.8.13

# uv is only used to install a standalone CPython 3.12 (Debian ships a different python3).
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM jenkins/jenkins:${JENKINS_TAG}

# Tool pins are declared right before the layer that uses them, so bumping one version only
# rebuilds that layer and the ones after it (apt layers stay cached).
LABEL org.opencontainers.image.title="scalebotai-jenkins" \
      org.opencontainers.image.description="Jenkins controller with docker, kubectl, trivy and python3.12 for the ScaleBotAI pipeline" \
      org.opencontainers.image.source="https://github.com/scalebotai/scalebotai"

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- OS packages ----------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg jq git openssl python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

# --- Docker CLI + buildx plugin (client only; no daemon inside this container) ----
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/* \
 && docker --version && docker buildx version

# --- kubectl (pinned, SHA-256 verified; keep within +/-1 minor of the cluster version) ---
ARG KUBECTL_VERSION=v1.37.1
RUN ARCH="$(dpkg --print-architecture)" \
 && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
 && echo "$(curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl.sha256")  /usr/local/bin/kubectl" \
      | sha256sum -c - \
 && chmod 0755 /usr/local/bin/kubectl \
 && kubectl version --client

# --- Trivy (pinned, SHA-256 verified against the release checksums file) -----------
ARG TRIVY_VERSION=0.74.0
RUN case "$(dpkg --print-architecture)" in \
      amd64) T_ARCH=64bit ;; \
      arm64) T_ARCH=ARM64 ;; \
      *) echo "unsupported architecture for trivy" >&2; exit 1 ;; \
    esac \
 && TARBALL="trivy_${TRIVY_VERSION}_Linux-${T_ARCH}.tar.gz" \
 && cd /tmp \
 && curl -fsSLO "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${TARBALL}" \
 && curl -fsSLO "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_checksums.txt" \
 && grep " ${TARBALL}\$" "trivy_${TRIVY_VERSION}_checksums.txt" | sha256sum -c - \
 && tar -xzf "${TARBALL}" -C /usr/local/bin trivy \
 && rm -f "${TARBALL}" "trivy_${TRIVY_VERSION}_checksums.txt" \
 && trivy --version

# --- Python 3.12 (standalone CPython managed by uv, exposed as /usr/local/bin/python3.12) ---
ARG PYTHON_VERSION=3.12
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
# A plain file symlink in /usr/local/bin would break `python3.12 -m venv` (the venv's `home` would
# point at /usr/local/bin, where the stdlib is missing). Instead: a stable directory link
# /opt/python3.12 -> the uv-managed install, plus a tiny exec wrapper on the default PATH.
RUN uv python install "${PYTHON_VERSION}" \
 && PY_BIN="$(UV_PYTHON_PREFERENCE=only-managed uv python find "${PYTHON_VERSION}")" \
 && ln -sfn "$(dirname "$(dirname "${PY_BIN}")")" "/opt/python${PYTHON_VERSION}" \
 && printf '#!/bin/sh\nexec /opt/python%s/bin/python%s "$@"\n' "${PYTHON_VERSION}" "${PYTHON_VERSION}" \
      > "/usr/local/bin/python${PYTHON_VERSION}" \
 && chmod 0755 "/usr/local/bin/python${PYTHON_VERSION}" \
 && chmod -R a+rX /opt/uv-python \
 && "python${PYTHON_VERSION}" --version \
 && "python${PYTHON_VERSION}" -m venv /tmp/venv-check \
 && /tmp/venv-check/bin/python -c "import ssl, sqlite3, venv; print('python venv OK')" \
 && rm -rf /tmp/venv-check

# --- Jenkins plugins ------------------------------------------------------------------
USER jenkins
COPY --chown=jenkins:jenkins plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
  CMD curl -fsS http://localhost:8080/login >/dev/null || exit 1
