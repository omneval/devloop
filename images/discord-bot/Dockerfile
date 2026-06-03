# syntax=docker/dockerfile:1
# devloop-discord-bot: Discord <-> Temporal messaging bridge.
#
# Installs omneval-devloop[discord] from PyPI and starts main.py, which
# connects the Discord gateway client and the Temporal activity worker.
#
# SDK_VERSION pins omneval-devloop to the exact release; the release pipeline
# (release.yml) passes the tag version and builds this image only after the
# PyPI publish lands. Left empty on continuous (main) builds, where latest
# is fine.

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

WORKDIR /app

ARG SDK_VERSION=
COPY pyproject.toml ./
RUN if [ -n "$SDK_VERSION" ]; then \
        echo "omneval-devloop==$SDK_VERSION" > /tmp/sdk-constraint.txt; \
    fi; \
    UV_HTTP_TIMEOUT=300 uv pip install --system --no-cache \
        ${SDK_VERSION:+--constraint /tmp/sdk-constraint.txt} . \
    && rm -f /tmp/sdk-constraint.txt

COPY main.py ./

EXPOSE 8080

CMD ["python", "main.py"]
