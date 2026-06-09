# syntax=docker/dockerfile:1
# Universal Agent Image — the batteries-included toolchain image for Agent
# Execution Jobs, published as ghcr.io/omneval/devloop-agent-universal.
#
# Enrolling a project does NOT require building a per-project image: this
# image extends devloop-agent-base (entrypoint, OpenHands SDK, skills, git/gh)
# with the common language toolchains — Go, Node.js, Helm (+ helm-unittest).
# It is the default Agent Execution Job image when a Project Registry entry
# omits agent_image (AGENT_DEFAULT_IMAGE / Helm
# temporalWorker.agentJob.defaultImage).
#
# Per-project customization belongs in the enrolled repo's .devloop/ directory
# (prompts, install/test commands, standards) — not in a derived image. Build
# a derived image only when a project needs a toolchain this image lacks.
#
# BASE_TAG selects the devloop-agent-base to extend: "latest" on continuous
# (main) builds; the release pipeline pins the matching semver so
# devloop-agent-universal:X.Y.Z always wraps devloop-agent-base:X.Y.Z.
ARG BASE_TAG=latest
FROM ghcr.io/omneval/devloop-agent-base:${BASE_TAG}

ARG GO_VERSION=1.25.0
ARG NODE_MAJOR=22
ARG HELM_VERSION=4.2.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Go
RUN curl -fsSL --retry 5 --retry-delay 5 --max-time 300 \
    -o /tmp/go.tar.gz \
    "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
    && tar -C /usr/local -xzf /tmp/go.tar.gz \
    && rm /tmp/go.tar.gz

ENV PATH="/usr/local/go/bin:${PATH}"
ENV GOPATH="/go"
ENV PATH="${GOPATH}/bin:${PATH}"

# Node.js LTS
RUN curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Helm + helm-unittest (chart test gates)
COPY --from=alpine/helm:4.2.0 /usr/bin/helm /usr/local/bin/helm

RUN echo "bf1e2f933afaaab981b4dce6f0caea951635539336f463780ad479de5408f869  /usr/local/bin/helm" \
        | sha256sum -c - \
    && helm version

RUN helm plugin install https://github.com/helm-unittest/helm-unittest --verify=false

WORKDIR /workspace
