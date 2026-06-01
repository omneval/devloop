# syntax=docker/dockerfile:1

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

WORKDIR /app

COPY images/poller/pyproject.toml images/poller/uv.lock .
RUN UV_HTTP_TIMEOUT=300 uv pip install --system --no-cache .

COPY images/poller/poll.py ./

# Volume mount for persistent state across restarts
RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "poll.py"]
