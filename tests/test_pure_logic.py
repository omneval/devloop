"""Pure-logic unit tests for planner ordering, gate parsing, alert
classification, and summary dedup (issues #20, #22, #23, #24, #26)."""

import pytest
from unittest.mock import MagicMock, patch

from devloop import dev_loop_logic as dl
from devloop import Phase
from devloop.summarize_activities import build_prompt, should_summarize


# ---- Phase enum (#53) ---------------------------------------------------- #


def test_phase_has_no_fix_pass_member():
    """The Review Fix Pass is dispatched as a Phase.PR_COMMENT job (the proven
    re-engagement path); the never-dispatched FIX_PASS member was removed."""
    assert not hasattr(Phase, "FIX_PASS")
    assert Phase.PR_COMMENT.value == "pr_comment"


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


# ---- PR number extraction (#22) ------------------------------------------ #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/omneval/omneval/pull/42", 42),
        ("https://github.com/o/r/pull/7/files", 7),
        ("branch pushed (no PR link)", 0),
        ("", 0),
    ],
)
def test_pr_number_from_url(url, expected):
    assert dl.pr_number_from_url(url) == expected


# ---- review findings fallback rendering (#213) --------------------------- #
def test_render_review_findings_comment_summary_only():
    body = dl.render_review_findings_comment("looks good overall", [])
    assert "looks good overall" in body
    assert "Agent review" in body


def test_render_review_findings_comment_includes_inline():
    from devloop.github import InlineComment

    body = dl.render_review_findings_comment(
        "needs a fix", [InlineComment("a.py", 12, "missing null check")]
    )
    assert "needs a fix" in body
    assert "a.py:12" in body
    assert "missing null check" in body


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


# ---- summarize_activities LLM endpoint (#consolidate-llm-base-url) --------- #


def test_llm_summary_uses_agent_llm_base_url(monkeypatch):
    """_llm_summary must POST to AGENT_LLM_BASE_URL, not AGENT_OPENAI_BASE_URL."""
    import devloop.summarize_activities as sa

    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://custom-llm.local/v1")
    # Reload the module so the module-level variable picks up the new env value.
    import importlib

    importlib.reload(sa)

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": '{"summary": "summary text"}'}}]
    }

    with patch("httpx.post", return_value=fake_response) as mock_post:
        result = sa._llm_summary("test prompt")

    called_url = mock_post.call_args[0][0]
    assert called_url.startswith("http://custom-llm.local/v1"), (
        f"Expected AGENT_LLM_BASE_URL to control the endpoint, got {called_url!r}"
    )
    assert result == "summary text"
