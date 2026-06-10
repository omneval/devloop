"""Workflow-level KPI span emission (issue #122).

The agent entrypoint already traces every phase into omneval; what's missing
from the eval flywheel is the *workflow's* view — loop iterations spent,
verdicts, and the label→PR wall-clock. ``emit_workflow_kpis`` is a small
activity the Dev Loop calls once per issue it carries to reviewer
notification: it opens a single span named ``devloop.workflow.kpi`` carrying
the counters as attributes and exports it to the same OTLP endpoint the Agent
Execution Jobs use.

Everything here is best-effort: a missing OpenTelemetry SDK, an unreachable
collector, or a missing ingest key degrades to a log line — KPI emission must
never fail a workflow.
"""

from __future__ import annotations

import logging
import os

from temporalio import activity

from . import cluster
from .projects import get_project
from .shared import WorkflowKpiInput

log = logging.getLogger(__name__)

# Mirrors k8s_jobs.OMNEVAL_OTLP_ENDPOINT — the worker-side default for the
# omneval trace ingest.
_OTLP_ENDPOINT = os.getenv(
    "OMNEVAL_OTLP_ENDPOINT", "http://omneval-ingest.omneval.svc.cluster.local:8000"
)


def _resolve_ingest_key(project_id: str) -> str:
    """API key for the omneval OTLP ingest: the worker's own env var when set
    (local quickstart), otherwise the project's ingest Secret (in-cluster)."""
    env_key = os.environ.get("OMNEVAL_API_KEY", "")
    if env_key:
        return env_key
    try:
        cfg = get_project(project_id)
        return cluster.read_secret_value(cfg.omneval_ingest_secret, "api-key")
    except Exception:  # noqa: BLE001 — best-effort
        return ""


def _build_exporter(endpoint: str, api_key: str):
    """Seam for tests: construct the OTLP span exporter."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )

    headers = {"x-api-key": api_key} if api_key else None
    # The exporter appends /v1/traces itself only when given OTEL_* env; with
    # an explicit endpoint we must pass the full path.
    return OTLPSpanExporter(
        endpoint=f"{endpoint.rstrip('/')}/v1/traces", headers=headers
    )


def kpi_attributes(inp: WorkflowKpiInput) -> dict:
    """Pure mapping from the input to span attributes (unit-testable)."""
    return {
        "devloop.project": inp.project_id,
        "devloop.issue_number": inp.issue_number,
        "devloop.workflow.ci_fix_iterations": inp.ci_fix_iterations,
        "devloop.workflow.review_fix_passes": inp.review_fix_passes,
        "devloop.workflow.answer_jobs": inp.answer_jobs,
        "devloop.workflow.execute_attempts": inp.execute_attempts,
        "devloop.workflow.review_verdict": inp.review_verdict,
        "devloop.workflow.label_to_pr_seconds": inp.label_to_pr_seconds,
        "devloop.workflow.pr_opened": inp.pr_opened,
        "devloop.workflow.commits": inp.commits,
        "devloop.workflow.ci_exhausted": inp.ci_exhausted,
    }


@activity.defn
async def emit_workflow_kpis(inp: WorkflowKpiInput) -> None:
    """Emit one ``devloop.workflow.kpi`` span carrying the issue's loop KPIs.

    Uses a throwaway TracerProvider per call (one emission per issue per run —
    the setup cost is irrelevant) so the worker process keeps no global OTel
    state and no other instrumentation is affected.
    """
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except Exception:  # noqa: BLE001 — SDK not installed: degrade to a log line
        log.info("workflow KPIs (otel sdk unavailable): %s", kpi_attributes(inp))
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _OTLP_ENDPOINT)
    api_key = _resolve_ingest_key(inp.project_id)

    try:
        provider = TracerProvider(
            resource=Resource.create({"service.name": "devloop-workflow"})
        )
        provider.add_span_processor(
            SimpleSpanProcessor(_build_exporter(endpoint, api_key))
        )
        tracer = provider.get_tracer("devloop-worker")
        with tracer.start_as_current_span("devloop.workflow.kpi") as span:
            for key, value in kpi_attributes(inp).items():
                span.set_attribute(key, value)
        provider.shutdown()
        log.info(
            "emitted workflow KPI span for %s#%d", inp.project_id, inp.issue_number
        )
    except Exception:  # noqa: BLE001 — never fail the workflow over telemetry
        log.warning("workflow KPI emission failed", exc_info=True)
