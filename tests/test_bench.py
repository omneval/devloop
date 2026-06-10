"""Tests for devloop bench (issue #122) — all network seams mocked."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from devloop import bench


class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# Golden fetch
# --------------------------------------------------------------------------- #
def test_fetch_golden_resolves_issue_and_merged_pr():
    def fake_request(method, url, *, accept="", **kwargs):
        if url.endswith("/issues/67"):
            return _Resp({"title": "Traces and Sessions", "body": "## AC\n- x"})
        if "/search/issues" in url:
            assert "is:pr is:merged 67" in kwargs["params"]["q"]
            return _Resp({"items": [{"number": 70}]})
        if url.endswith("/pulls/70"):
            assert accept == "application/vnd.github.diff"
            return _Resp(text="diff --git a/x b/x")
        raise AssertionError(f"unexpected url {url}")

    with patch.object(bench, "_github_request", side_effect=fake_request):
        golden = bench.fetch_golden("omneval/omneval", 67)

    assert golden.title == "Traces and Sessions"
    assert golden.human_pr_number == 70
    assert golden.human_diff.startswith("diff --git")


def test_fetch_golden_tolerates_missing_pr():
    def fake_request(method, url, *, accept="", **kwargs):
        if url.endswith("/issues/9"):
            return _Resp({"title": "t", "body": "b"})
        if "/search/issues" in url:
            return _Resp({"items": []})
        raise AssertionError(url)

    with patch.object(bench, "_github_request", side_effect=fake_request):
        golden = bench.fetch_golden("o/r", 9)

    assert golden.human_pr_number == 0
    assert golden.human_diff == ""


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def test_open_replay_issue_creates_and_labels():
    calls = []

    def fake_request(method, url, *, accept="", **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if url.endswith("/issues"):
            return _Resp({"number": 5})
        return _Resp({})

    golden = bench.GoldenIssue(number=67, title="T", body="B")
    with patch.object(bench, "_github_request", side_effect=fake_request):
        n = bench.open_replay_issue("org/scratch", golden, "agent-ready")

    assert n == 5
    create = calls[0]
    assert create[2]["title"] == "[bench #67] T"
    label = calls[1]
    assert label[1].endswith("/issues/5/labels")
    assert label[2] == {"labels": ["agent-ready"]}


def test_await_agent_pr_polls_until_found():
    results = [0, 0, 31]
    with patch.object(bench, "find_agent_pr", side_effect=lambda *a: results.pop(0)):
        clock = iter([0, 1, 2, 3, 4, 5])
        pr = bench.await_agent_pr(
            "org/scratch",
            5,
            timeout_seconds=100,
            poll_seconds=0,
            _sleep=lambda s: None,
            _clock=lambda: next(clock),
        )
    assert pr == 31


def test_await_agent_pr_times_out():
    with patch.object(bench, "find_agent_pr", return_value=0):
        clock = iter([0, 50, 101, 102])
        pr = bench.await_agent_pr(
            "org/scratch",
            5,
            timeout_seconds=100,
            poll_seconds=0,
            _sleep=lambda s: None,
            _clock=lambda: next(clock),
        )
    assert pr == 0


def test_find_agent_pr_matches_branch_convention():
    pulls = [
        {"number": 30, "head": {"ref": "feature/foo"}},
        {"number": 31, "head": {"ref": "agent/issue-5-some-slug"}},
    ]
    with patch.object(bench, "_github_request", return_value=_Resp(pulls)):
        assert bench.find_agent_pr("org/scratch", 5) == 31
        assert bench.find_agent_pr("org/scratch", 6) == 0


# --------------------------------------------------------------------------- #
# Judge
# --------------------------------------------------------------------------- #
def _fake_judge_completion(payload: dict):
    client = MagicMock()
    msg = MagicMock()
    msg.content = json.dumps(payload)
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=msg)]
    )
    return client


def test_judge_diff_resolves_model_and_parses_verdict(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_JUDGE", raising=False)
    monkeypatch.delenv("AGENT_MODEL_REVIEW", raising=False)
    monkeypatch.setenv("AGENT_MODEL", "base-model")
    client = _fake_judge_completion(
        {"criteria_total": 4, "criteria_met": 3, "score": 7, "rationale": "ok"}
    )
    golden = bench.GoldenIssue(number=1, title="t", body="b", human_diff="hd")

    with patch.object(bench, "_judge_client", return_value=client):
        verdict = bench.judge_diff(golden, "agent diff")

    assert verdict["score"] == 7
    create = client.chat.completions.create.call_args.kwargs
    assert create["model"] == "base-model"
    assert create["response_format"]["type"] == "json_schema"
    user = create["messages"][1]["content"]
    assert "agent diff" in user
    assert "hd" in user  # human baseline included


def test_judge_setting_prefers_judge_then_review_roles(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "base")
    monkeypatch.setenv("AGENT_MODEL_REVIEW", "review")
    assert bench._judge_setting("AGENT_MODEL") == "review"
    monkeypatch.setenv("AGENT_MODEL_JUDGE", "judge")
    assert bench._judge_setting("AGENT_MODEL") == "judge"


def test_judge_diff_requires_model(monkeypatch):
    for var in ("AGENT_MODEL", "AGENT_MODEL_REVIEW", "AGENT_MODEL_JUDGE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="no judge model"):
        bench.judge_diff(bench.GoldenIssue(1, "t", "b"), "d")


# --------------------------------------------------------------------------- #
# Orchestration + report
# --------------------------------------------------------------------------- #
def test_bench_issue_no_replay_judges_human_pr(monkeypatch):
    golden = bench.GoldenIssue(
        number=67, title="T", body="B", human_pr_number=70, human_diff="hd"
    )
    with patch.object(bench, "fetch_golden", return_value=golden):
        with patch.object(
            bench,
            "judge_diff",
            return_value={
                "criteria_total": 5,
                "criteria_met": 5,
                "score": 9,
                "rationale": "solid",
            },
        ) as judged:
            score = bench.bench_issue(
                "o/r",
                "",
                67,
                label="l",
                timeout_seconds=1,
                poll_seconds=0,
                replay=False,
            )

    assert score.agent_pr_number == 70
    assert score.score == 9
    assert judged.call_args.args[1] == "hd"


def test_bench_issue_replay_timeout_reports_error():
    golden = bench.GoldenIssue(number=67, title="T", body="B")
    with patch.object(bench, "fetch_golden", return_value=golden):
        with patch.object(bench, "open_replay_issue", return_value=5):
            with patch.object(bench, "await_agent_pr", return_value=0):
                score = bench.bench_issue(
                    "o/r", "o/s", 67, label="l", timeout_seconds=1, poll_seconds=0
                )
    assert "timed out" in score.error
    assert score.score == 0


def test_bench_issue_survives_judge_failure():
    golden = bench.GoldenIssue(
        number=67, title="T", body="B", human_pr_number=70, human_diff="hd"
    )
    with patch.object(bench, "fetch_golden", return_value=golden):
        with patch.object(bench, "judge_diff", side_effect=RuntimeError("llm down")):
            score = bench.bench_issue(
                "o/r",
                "",
                67,
                label="l",
                timeout_seconds=1,
                poll_seconds=0,
                replay=False,
            )
    assert score.error == "llm down"


def test_format_report_includes_mean():
    scores = [
        bench.IssueScore(67, "a", score=8, criteria_total=4, criteria_met=4),
        bench.IssueScore(68, "b", score=4, criteria_total=2, criteria_met=1),
    ]
    report = bench.format_report(scores)
    assert "mean score: 6.0/10" in report
    assert "#    67" in report


def test_resolve_source_repo_accepts_slug():
    assert bench.resolve_source_repo("owner/repo", "ignored.yaml") == "owner/repo"
