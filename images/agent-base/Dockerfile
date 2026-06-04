# syntax=docker/dockerfile:1
# Agent Base Image — shared toolchain for all Agent Execution Jobs.
# Project repos are cloned at Job runtime; do NOT add project source here.

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

ARG GH_VERSION=2.93.0
ARG KUBECTL_VERSION=1.34.1
ARG FLUX_VERSION=2.8.8
ARG ARGOCD_VERSION=2.13.3

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# gh CLI
RUN curl -fsSL --retry 5 --retry-delay 5 --max-time 300 \
    -o /tmp/gh.tar.gz \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    && tar -xz -C /tmp -f /tmp/gh.tar.gz \
    && mv "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_amd64"

# kubectl
RUN curl -fsSL --retry 5 --retry-delay 5 --max-time 300 \
    -o /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# flux CLI
RUN curl -fsSL --retry 5 --retry-delay 5 --max-time 300 \
    -o /tmp/flux.tar.gz \
    "https://github.com/fluxcd/flux2/releases/download/v${FLUX_VERSION}/flux_${FLUX_VERSION}_linux_amd64.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/flux.tar.gz flux \
    && rm /tmp/flux.tar.gz \
    && chmod +x /usr/local/bin/flux

# argocd CLI
RUN curl -fsSL --retry 5 --retry-delay 5 --max-time 300 \
    -o /usr/local/bin/argocd \
    "https://github.com/argoproj/argo-cd/releases/download/v${ARGOCD_VERSION}/argocd-linux-amd64" \
    && chmod +x /usr/local/bin/argocd

# Runtime Python dependencies are declared in pyproject.toml (OpenHands agent
# runtime, OTLP tracing exporter, and omneval-devloop — the shared Agent Job
# protocol, devloop.shared). Installed --system; no first-party package is built.
#
# SDK_VERSION pins omneval-devloop to the exact release on PyPI. The release
# pipeline (release.yml) passes the tag version and only builds this image after
# the PyPI publish has landed, so the baked SDK always matches the image's semver
# tag. Left empty on continuous (main) builds, where the latest release is used.
ARG SDK_VERSION=
COPY pyproject.toml /tmp/agent-base/pyproject.toml
RUN if [ -n "$SDK_VERSION" ]; then \
        echo "omneval-devloop==$SDK_VERSION" > /tmp/sdk-constraint.txt; \
    fi; \
    UV_HTTP_TIMEOUT=300 uv pip install --system --no-cache \
        ${SDK_VERSION:+--constraint /tmp/sdk-constraint.txt} \
        /tmp/agent-base \
    && rm -rf /tmp/agent-base /tmp/sdk-constraint.txt

# Agent Execution Job entrypoint (issues #18/#19). Project repos are cloned at
# runtime; this is the only application code baked into the image.
COPY entrypoint.py /usr/local/bin/agent-entrypoint.py

# Skills resolution module (issues #32/#34/#35/#36). The entrypoint runs as
# `python /usr/local/bin/agent-entrypoint.py`, so /usr/local/bin is sys.path[0]
# and entrypoint.py's bare `import skills` resolves to this file. It MUST sit in
# the same dir as the entrypoint — without it every Agent Job crashes at import
# with "No module named 'skills'".
COPY skills.py /usr/local/bin/skills.py

# Phase prompt templates (plan / implement / review / merge), rendered per phase
# by the entrypoint. Bundled here so every project shares the same agent prompts.
COPY prompts/ /usr/local/share/agent-prompts/

WORKDIR /workspace
