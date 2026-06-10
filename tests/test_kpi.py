"""Tests for workflow KPI span emission (issue #122)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from devloop import kpi
from devloop.projects import ProjectConfig, _REGISTRY
from devloop.shared import WorkflowKpiInput

_PROJECT = ProjectConfig(
    id="proj",
    github_url="https://github.com/org/proj",
    default_branch="main",
    agent_image="img",
    agent_label="agent-ready",
    omneval_ingest_secret="omneval-ingest-proj",
    github_token_secret="proj-token",
)


@pytest.fixture(autouse=True)
def _registry():
    _REGISTRY.clear()
    _REGISTRY["proj"] = _PROJECT
    yield
    _REGISTRY.clear()


def _input(**kw) -> WorkflowKpiInput:
    return WorkflowKpiInput(project_id="proj", issue_number=42, **kw)


def test_kpi_attributes_maps_every_counter():
    inp = _input(
        ci_fix_iterations=3,
        review_fix_passes=1,
        answer_jobs=2,
        execute_attempts=1,
        review_verdict="lgtm",
        label_to_pr_seconds=123.5,
        pr_opened=True,
        commits=4,
        ci_exhausted=False,
    )
    attrs = kpi.kpi_attributes(inp)
    assert attrs["devloop.project"] == "proj"
    assert attrs["devloop.issue_number"] == 42
    assert attrs["devloop.workflow.ci_fix_iterations"] == 3
    assert attrs["devloop.workflow.review_fix_passes"] == 1
    assert attrs["devloop.workflow.answer_jobs"] == 2
    assert attrs["devloop.workflow.execute_attempts"] == 1
    assert attrs["devloop.workflow.review_verdict"] == "lgtm"
    assert attrs["devloop.workflow.label_to_pr_seconds"] == 123.5
    assert attrs["devloop.workflow.pr_opened"] is True
    assert attrs["devloop.workflow.commits"] == 4
    assert attrs["devloop.workflow.ci_exhausted"] is False


def test_resolve_ingest_key_prefers_env(monkeypatch):
    monkeypatch.setenv("OMNEVAL_API_KEY", "env-key")
    assert kpi._resolve_ingest_key("proj") == "env-key"


def test_resolve_ingest_key_reads_project_secret(monkeypatch):
    monkeypatch.delenv("OMNEVAL_API_KEY", raising=False)
    monkeypatch.setattr(
        kpi.cluster, "read_secret_value", lambda name, key: f"{name}/{key}"
    )
    assert kpi._resolve_ingest_key("proj") == "omneval-ingest-proj/api-key"


def test_resolve_ingest_key_degrades_to_empty(monkeypatch):
    monkeypatch.delenv("OMNEVAL_API_KEY", raising=False)

    def boom(*a, **k):
        raise RuntimeError("no cluster")

    monkeypatch.setattr(kpi.cluster, "read_secret_value", boom)
    assert kpi._resolve_ingest_key("proj") == ""


async def test_emit_workflow_kpis_sets_attributes_on_span(monkeypatch):
    """The activity exports one span whose attributes carry the counters."""
    monkeypatch.setenv("OMNEVAL_API_KEY", "k")

    exporter = MagicMock()
    with patch.object(kpi, "_build_exporter", return_value=exporter) as build:
        # Use the real OTel SDK with the mocked exporter.
        await kpi.emit_workflow_kpis(_input(ci_fix_iterations=2, commits=3))

    build.assert_called_once()
    # SimpleSpanProcessor exports synchronously on span end.
    assert exporter.export.called
    spans = exporter.export.call_args.args[0]
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "devloop.workflow.kpi"
    assert span.attributes["devloop.workflow.ci_fix_iterations"] == 2
    assert span.attributes["devloop.workflow.commits"] == 3
    assert span.resource.attributes["service.name"] == "devloop-workflow"


async def test_emit_workflow_kpis_never_raises(monkeypatch):
    monkeypatch.setenv("OMNEVAL_API_KEY", "k")

    def boom(*a, **k):
        raise RuntimeError("collector down")

    with patch.object(kpi, "_build_exporter", side_effect=boom):
        # Must not raise — telemetry failures degrade to a warning.
        await kpi.emit_workflow_kpis(_input())
