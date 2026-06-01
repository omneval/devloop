# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock .
RUN UV_HTTP_TIMEOUT=300 uv pip install --system --no-cache .

COPY thread_store.py activities.py discord_client.py text_utils.py main.py ./

EXPOSE 8080

CMD ["python", "main.py"]
