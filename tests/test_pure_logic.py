"""Pure-logic unit tests for planner ordering, gate parsing, alert
classification, and summary dedup (issues #20, #22, #23, #24, #26)."""

import pytest

from devloop import dev_loop_logic as dl
from devloop.github_ops import build_plan
from devloop.summarize_activities import build_prompt, should_summarize


# ---- planner dependency ordering (#20) ----------------------------------- #
def test_build_plan_orders_dependencies_first():
    raw = [
        {"number": 3, "title": "C", "body": "depends on #1 and #2"},
        {"number": 1, "title": "A", "body": "no deps"},
        {"number": 2, "title": "B", "body": "after #1"},
    ]
    plan = build_plan("omneval", raw)
    order = [i.number for i in plan.issues]
    assert order.index(1) < order.index(2) < order.index(3)
    assert plan.issues[order.index(3)].depends_on == [1, 2]


def test_build_plan_excludes_pull_requests():
    raw = [
        {"number": 1, "title": "issue", "body": ""},
        {"number": 2, "title": "pr", "body": "", "pull_request": {"url": "x"}},
    ]
    plan = build_plan("omneval", raw)
    assert [i.number for i in plan.issues] == [1]


def test_build_plan_breaks_cycles():
    raw = [
        {"number": 1, "title": "A", "body": "see #2"},
        {"number": 2, "title": "B", "body": "see #1"},
    ]
    plan = build_plan("omneval", raw)
    assert sorted(i.number for i in plan.issues) == [1, 2]


# ---- approval / merge parsing (#20, #23) --------------------------------- #
@pytest.mark.parametrize(
    "reply,expected",
    [
        ("approve", True),
        ("Approved!", True),
        ("yes please", True),
        ("✅", True),
        ("lgtm", True),
        ("no", False),
        ("redo the plan", False),
        ("", False),
    ],
)
def test_is_approval(reply, expected):
    assert dl.is_approval(reply) is expected


def test_parse_merge_reply_all_passed_excludes_fail():
    reviewed = [(1, "pass"), (2, "warn"), (3, "fail")]
    assert dl.parse_merge_reply("all passed", reviewed) == [1, 2]


def test_parse_merge_reply_explicit_subset():
    reviewed = [(1, "pass"), (2, "warn"), (3, "fail")]
    assert sorted(dl.parse_merge_reply("merge #1 and #3", reviewed)) == [1, 3]


def test_parse_merge_reply_none():
    assert dl.parse_merge_reply("nope", [(1, "pass")]) == []


# ---- summary dedup (#24) ------------------------------------------------- #
def test_should_summarize():
    assert should_summarize("abc", "def", []) is True  # new head
    assert should_summarize("abc", "abc", []) is False  # nothing new
    assert should_summarize("abc", "abc", [1]) is True  # closed issues present
    assert should_summarize("", "", []) is False


def test_build_prompt_mentions_commits_and_issues():
    p = build_prompt(["fix bug", "add feature"], [{"number": 7, "title": "Crash"}])
    assert "fix bug" in p and "#7 Crash" in p
    assert "plain-english" in p.lower()
