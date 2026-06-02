# syntax=docker/dockerfile:1
# Alert Response Workflow — consumer extension example.
#
# This Dockerfile shows how to build a custom consumer image that installs
# omneval-devloop from PyPI alongside your own workflow code.  The resulting
# image runs a single Temporal worker that handles both the SDK workflows
# (DevLoop, Summarization) and the custom AlertResponseWorkflow.
#
# To adapt this for your own workflow:
#   1. Replace alert_response.py with your workflow code
#   2. Replace worker.py with your custom worker (or keep it to run both)
#   3. Adjust pyproject.toml dependencies
#   4. Remove allowlist.yaml if not needed
#
# Build:
#   docker build -t ghcr.io/your-org/alert-response-worker:latest .

FROM python:3.12-slim

# Install uv for fast, reproducible dependency resolution
COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

WORKDIR /app

# Install omneval-devloop and example dependencies
COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

# Copy custom workflow code and configuration
COPY alert_response.py ./
COPY worker.py ./
COPY allowlist.yaml /etc/alert-response/allowlist.yaml

# /healthz (Kubernetes probes) and the webhook receiver port
EXPOSE 8080 8088

# Mount projects.yaml at runtime (via ConfigMap)
VOLUME ["/app/projects.yaml"]

CMD ["python", "worker.py"]
