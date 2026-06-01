# syntax=docker/dockerfile:1
# Agent Base Image — shared toolchain for all Agent Execution Jobs.
# Project repos are cloned at Job runtime; do NOT add project source here.

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ARG GH_VERSION=2.93.0
ARG KUBECTL_VERSION=1.34.1
ARG FLUX_VERSION=2.8.8

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

# Python SDKs: OpenHands agent runtime + Temporal orchestration client +
# OTLP exporter (omneval tracing) + kubernetes (output ConfigMap writes).
RUN UV_HTTP_TIMEOUT=300 uv pip install --system --no-cache \
    "openhands-ai==1.7.0" \
    "temporalio==1.27.2" \
    "opentelemetry-sdk>=1.33.1" \
    "opentelemetry-exporter-otlp-proto-http>=1.33.1" \
    "kubernetes>=33.1,<34"

# Agent Execution Job entrypoint (issues #18/#19). Project repos are cloned at
# runtime; this is the only application code baked into the image.
COPY entrypoint.py /usr/local/bin/agent-entrypoint.py

# Phase prompt templates (plan / implement / review / merge), rendered per phase
# by the entrypoint. Bundled here so every project shares the same agent prompts.
COPY prompts/ /usr/local/share/agent-prompts/

WORKDIR /workspace
