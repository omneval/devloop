"""Tests for consolidated webhook/constants module.

This verifies that shared constants like _AGENT_BRANCH and AGENT_GITHUB_LOGIN
are defined once in _constants.py and imported by all callers (no duplicates).
"""

from __future__ import annotations


class TestAgentBranchConsolidated:
    """_AGENT_BRANCH regex is defined once in _constants.py."""

    def test_agent_branch_in_constants(self) -> None:
        """The _AGENT_BRANCH regex lives in _constants module."""
        import re

        from devloop._constants import _AGENT_BRANCH

        assert isinstance(_AGENT_BRANCH, re.Pattern)

    def test_agent_branch_matches_agent_issue_branch(self) -> None:
        """The consolidated _AGENT_BRANCH regex matches agent issue branches."""
        from devloop._constants import _AGENT_BRANCH

        assert _AGENT_BRANCH.match("agent/issue-42")
        assert _AGENT_BRANCH.match("agent/issue-9999")
        assert _AGENT_BRANCH.match("agent/issue-123-fix")
        assert _AGENT_BRANCH.match("agent/issue-123") is None or _AGENT_BRANCH.match(
            "agent/issue-123"
        )

    def test_agent_branch_does_not_match_human_branch(self) -> None:
        """The consolidated _AGENT_BRANCH regex does not match human branches."""
        from devloop._constants import _AGENT_BRANCH

        assert _AGENT_BRANCH.match("main") is None
        assert _AGENT_BRANCH.match("feature/my-feature") is None
        assert _AGENT_BRANCH.match("agent/pr-42") is None

    def test_agent_branch_unique_definition(self) -> None:
        """Only _constants.py defines _AGENT_BRANCH (no duplicates elsewhere)."""
        import subprocess

        result = subprocess.run(
            [
                "grep",
                "-rn",
                "_AGENT_BRANCH = re.compile",
                "src/",
            ],
            capture_output=True,
            text=True,
        )
        # Only one definition should exist — in _constants.py
        lines = [line for line in result.stdout.strip().splitlines() if line]
        assert len(lines) == 1, (
            f"_AGENT_BRANCH should be defined in exactly one file, found: {lines}"
        )
        assert "_constants.py" in lines[0]


class TestAgentGithubLoginConsolidated:
    """AGENT_GITHUB_LOGIN is defined once in _constants.py."""

    def test_agent_github_login_in_constants(self) -> None:
        """The AGENT_GITHUB_LOGIN constant lives in _constants module."""
        from devloop._constants import AGENT_GITHUB_LOGIN

        assert isinstance(AGENT_GITHUB_LOGIN, str)

    def test_agent_github_login_default(self) -> None:
        """The default value of AGENT_GITHUB_LOGIN is 'devloop-bot'."""
        import importlib
        import os

        import devloop._constants as constants_module

        # Save original, temporarily clear env
        original_value = constants_module.AGENT_GITHUB_LOGIN
        os.environ.pop("AGENT_GITHUB_LOGIN", None)

        # Reimport to get default
        importlib.reload(constants_module)

        default_value = constants_module.AGENT_GITHUB_LOGIN
        assert default_value == "devloop-bot"

        # Restore
        if original_value:
            os.environ["AGENT_GITHUB_LOGIN"] = original_value
        importlib.reload(constants_module)

    def test_agent_github_login_unique_definition(self) -> None:
        """Only _constants.py defines AGENT_GITHUB_LOGIN (no duplicates)."""
        import subprocess

        result = subprocess.run(
            [
                "grep",
                "-rn",
                "AGENT_GITHUB_LOGIN.*=.*os.environ",
                "src/",
            ],
            capture_output=True,
            text=True,
        )
        # Only one definition should exist — in _constants.py
        lines = [line for line in result.stdout.strip().splitlines() if line]
        assert len(lines) == 1, (
            f"AGENT_GITHUB_LOGIN should be defined in exactly one file, found: {lines}"
        )
        assert "_constants.py" in lines[0]
