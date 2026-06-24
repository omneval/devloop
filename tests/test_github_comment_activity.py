"""Tests for the post_github_comment activity and GithubNotificationInput (issue #72).

TDD: these tests are written before the implementation and should fail first.
"""

from __future__ import annotations

from dataclasses import fields
from unittest.mock import MagicMock

from devloop.github import GithubNotificationInput


# ---------------------------------------------------------------------------
# GithubNotificationInput dataclass
# ---------------------------------------------------------------------------


def test_github_notification_input_has_issue_number_field():
    inp = GithubNotificationInput(issue_number=42, project_id="omneval", body="hello")
    assert inp.issue_number == 42


def test_github_notification_input_has_project_id_field():
    inp = GithubNotificationInput(issue_number=1, project_id="my-project", body="hi")
    assert inp.project_id == "my-project"


def test_github_notification_input_has_body_field():
    inp = GithubNotificationInput(issue_number=3, project_id="p", body="the message")
    assert inp.body == "the message"


def test_github_notification_input_field_names():
    """GithubNotificationInput must have exactly the three expected fields."""
    field_names = {f.name for f in fields(GithubNotificationInput)}
    assert field_names == {"issue_number", "project_id", "body"}


# ---------------------------------------------------------------------------
# post_github_comment activity — unit tests
# ---------------------------------------------------------------------------


def test_post_github_comment_is_importable():
    from devloop.github_ops import post_github_comment  # noqa: F401


def test_post_github_comment_has_activity_defn():
    """The activity must be decorated with @activity.defn so Temporal can register it."""
    from devloop.github_ops import post_github_comment

    assert hasattr(post_github_comment, "__temporal_activity_definition"), (
        "post_github_comment is missing @activity.defn"
    )


def test_post_github_comment_posts_to_issues_api(monkeypatch):
    """Happy path: posts the body to the correct GitHub Issues comment endpoint."""
    from devloop.github_ops import post_github_comment
    from devloop.github import GithubNotificationInput

    # Set up a fake project registry with a token
    fake_cfg = MagicMock()
    fake_cfg.github_url = "https://github.com/omneval/omneval"
    fake_cfg.github_token_secret = "omneval-github-token"

    monkeypatch.setattr("devloop.github_ops.get_project", lambda pid: fake_cfg)

    posted_bodies = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, json=None):
            posted_bodies.append({"url": url, "json": json})
            return FakeResponse()

    async def fake_client(cfg):
        return FakeClient()

    monkeypatch.setattr(
        "devloop.github_ops._client",
        fake_client,
    )

    import asyncio

    inp = GithubNotificationInput(
        issue_number=7,
        project_id="omneval",
        body="⏳ queued — agent is working on this issue",
    )
    asyncio.run(post_github_comment(inp))

    assert len(posted_bodies) == 1
    assert "/issues/7/comments" in posted_bodies[0]["url"]
    assert "queued" in posted_bodies[0]["json"]["body"]


def test_post_github_comment_uses_correct_repo(monkeypatch):
    """The POST goes to the repo derived from the project's github_url."""
    from devloop.github_ops import post_github_comment
    from devloop.github import GithubNotificationInput

    fake_cfg = MagicMock()
    fake_cfg.github_url = "https://github.com/someorg/somerepo"
    fake_cfg.github_token_secret = "tok"

    monkeypatch.setattr("devloop.github_ops.get_project", lambda pid: fake_cfg)

    urls_called = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, json=None):
            urls_called.append(url)
            return FakeResponse()

    async def fake_client(cfg):
        return FakeClient()

    monkeypatch.setattr("devloop.github_ops._client", fake_client)

    import asyncio

    inp = GithubNotificationInput(
        issue_number=99,
        project_id="someorg",
        body="test",
    )
    asyncio.run(post_github_comment(inp))

    assert any(
        "someorg/somerepo" in u and "/issues/99/comments" in u for u in urls_called
    )


# ---------------------------------------------------------------------------
# DevLoopWorkflow no longer imports messaging-bridge-specific names
# ---------------------------------------------------------------------------


def test_dev_loop_does_not_import_discord_constants():
    """dev_loop.py must not import MESSAGING_QUEUE or SendMessageInput
    (those are messaging-bridge-era names; the workflow now uses post_github_comment)."""
    import ast
    import pathlib

    src = pathlib.Path("src/devloop/dev_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in (
                    "MESSAGING_QUEUE",
                    "SendMessageInput",
                    "SendNotificationInput",
                    "CHANNEL_APPROVALS",
                ), f"dev_loop.py still imports messaging-bridge-era name '{alias.name}'"


def test_dev_loop_uses_phaseops_github_comment():
    """The Dev Loop's GitHub-comment path uses PhaseOps (not _WorkflowCommon).

    After consolidating all I/O through PhaseOps, ``dev_loop.py`` inherits from
    ``PhaseOps`` which provides the ``_comment`` method that dispatches the
    ``post_github_comment`` Temporal activity using ``GithubNotificationInput``.
    This test verifies that dev_loop.py imports from PhaseOps.
    """
    import ast
    import pathlib

    src = pathlib.Path("src/devloop/dev_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    from_phase_ops = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "devloop.phases.phase_ops" or (
                node.module and "phase_ops" in node.module
            ):
                for alias in node.names:
                    if alias.name == "PhaseOps":
                        from_phase_ops = True

    assert from_phase_ops, (
        "dev_loop.py must import PhaseOps to use the unified GitHub-comment path"
    )


# ---------------------------------------------------------------------------
# Worker no longer references messaging queue
# ---------------------------------------------------------------------------


def test_worker_does_not_import_messaging_bridge_activities():
    """worker.py must not reference any messaging-bridge activities or queues."""
    import pathlib

    src = pathlib.Path("src/devloop/worker.py").read_text(encoding="utf-8")
    assert "DiscordActivities" not in src, "worker.py still imports DiscordActivities"
    assert "SlackActivities" not in src, "worker.py still imports SlackActivities"
    assert "MESSAGING_QUEUE" not in src, "worker.py still references MESSAGING_QUEUE"


def test_worker_registers_post_github_comment_activity():
    """worker.py must include post_github_comment in its ACTIVITIES list."""
    import pathlib

    src = pathlib.Path("src/devloop/worker.py").read_text(encoding="utf-8")
    assert "post_github_comment" in src, (
        "worker.py must register post_github_comment activity"
    )
