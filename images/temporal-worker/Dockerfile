# syntax=docker/dockerfile:1
# devloop-temporal-worker: the reference Temporal Orchestration Worker.
#
# Installs omneval-devloop from PyPI and starts devloop.worker, which registers
# the Dev Loop and Summarization workflows on the orchestration task queue and
# serves /healthz. Consumers who need custom workflows build their own image that
# installs omneval-devloop alongside their workflow code.
#
# SDK_VERSION pins omneval-devloop to the exact release; the release pipeline
# (release.yml) passes the tag version and builds this image only after the PyPI
# publish lands, so the worker and the agent-base image always carry the same
# published SDK. Left empty on continuous (main) builds, where latest is fine.
# (Previously this installed from local source; PyPI is now the single source so
# hatch-vcs versioning — which needs git — never has to run inside a build context.)

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

WORKDIR /app

ARG SDK_VERSION=
RUN UV_HTTP_TIMEOUT=300 uv pip install --system --no-cache \
    "omneval-devloop${SDK_VERSION:+==$SDK_VERSION}"

# /healthz (Kubernetes probes) and the webhook receiver port.
EXPOSE 8080 8088

CMD ["python", "-m", "devloop.worker"]
