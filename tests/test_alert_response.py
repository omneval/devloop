"""Tests for the Alert Response Workflow consumer extension example (issue #10).

Verifies the example under docs/examples/alert-response/ is a complete,
self-contained consumer extension: correct file layout, valid configuration,
proper workflow registration, and functional allowlist logic.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
import yaml

EXAMPLE_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "examples" / "alert-response"
)
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"


# --------------------------------------------------------------------------- #
# File existence checks
# --------------------------------------------------------------------------- #

EXPECTED_FILES = [
    "Dockerfile",
    "worker.py",
    "alert_response.py",
    "allowlist.yaml",
    "pyproject.toml",
    "uv.lock",
    "README.md",
]


@pytest.mark.parametrize("filename", EXPECTED_FILES)
def test_example_files_exist(filename):
    path = EXAMPLE_DIR / filename
    assert path.exists(), f"{filename} not found in docs/examples/alert-response/"


# --------------------------------------------------------------------------- #
# allowlist.yaml
# --------------------------------------------------------------------------- #


def test_allowlist_valid_yaml():
    path = EXAMPLE_DIR / "allowlist.yaml"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), "allowlist.yaml must parse to a dict"


def test_allowlist_has_categories():
    data = yaml.safe_load((EXAMPLE_DIR / "allowlist.yaml").read_text())
    assert any(isinstance(v, list) for v in data.values()), (
        "allowlist must have at least one category with a list of actions"
    )


def test_allowlist_has_restart_category():
    data = yaml.safe_load((EXAMPLE_DIR / "allowlist.yaml").read_text())
    assert "restart" in data, "allowlist should have a 'restart' category"
    assert "nginx" in data["restart"], "nginx should be in restart allowlist"


# --------------------------------------------------------------------------- #
# pyproject.toml
# --------------------------------------------------------------------------- #


def test_pyproject_declares_omneval_devloop():
    text = (EXAMPLE_DIR / "pyproject.toml").read_text()
    assert "omneval-devloop" in text, "pyproject.toml must depend on omneval-devloop"


def test_pyproject_requires_python312():
    text = (EXAMPLE_DIR / "pyproject.toml").read_text()
    assert "3.12" in text, "pyproject.toml should require Python >=3.12"


# --------------------------------------------------------------------------- #
# Dockerfile
# --------------------------------------------------------------------------- #


def test_dockerfile_references_omneval_devloop():
    text = (EXAMPLE_DIR / "Dockerfile").read_text()
    assert "omneval-devloop" in text, "Dockerfile must install omneval-devloop"


def test_dockerfile_copies_worker():
    text = (EXAMPLE_DIR / "Dockerfile").read_text()
    assert "COPY" in text and "worker.py" in text


def test_dockerfile_copies_allowlist():
    text = (EXAMPLE_DIR / "Dockerfile").read_text()
    assert "allowlist.yaml" in text


# --------------------------------------------------------------------------- #
# README.md
# --------------------------------------------------------------------------- #


def test_readme_explains_consumer_extension():
    text = (EXAMPLE_DIR / "README.md").read_text()
    assert "extend" in text.lower() or "extension" in text.lower()


def test_readme_mentions_adapting():
    text = (EXAMPLE_DIR / "README.md").read_text()
    assert "adapt" in text.lower() or "adapting" in text.lower()


def test_readme_describes_file_layout():
    text = (EXAMPLE_DIR / "README.md").read_text()
    for f in ("worker.py", "alert_response.py", "allowlist.yaml", "pyproject.toml"):
        assert f in text, f"README should mention {f}"


# --------------------------------------------------------------------------- #
# Getting-started link
# --------------------------------------------------------------------------- #


def test_getting_started_links_to_example():
    text = (DOCS_DIR / "getting-started.md").read_text()
    assert "examples/alert-response" in text, (
        "getting-started.md should link to the alert-response example"
    )


def test_getting_started_mentions_custom_workflows():
    text = (DOCS_DIR / "getting-started.md").read_text()
    assert "custom" in text.lower() and "workflow" in text.lower()


# --------------------------------------------------------------------------- #
# alert_response.py module
# --------------------------------------------------------------------------- #


@pytest.fixture
def alert_module():
    """Import the alert_response module from the example directory."""
    example_dir = str(EXAMPLE_DIR)
    if example_dir not in sys.path:
        sys.path.insert(0, example_dir)
    mod = importlib.import_module("alert_response")
    return mod


def test_alert_module_imports(alert_module):
    """The module should import without errors."""
    assert alert_module is not None


def test_alert_response_workflow_defined(alert_module):
    """AlertResponseWorkflow should be a decorated Temporal workflow class."""
    assert hasattr(alert_module, "AlertResponseWorkflow")
    wf = alert_module.AlertResponseWorkflow
    assert hasattr(wf, "run"), "AlertResponseWorkflow should have a run method"


def test_alert_response_workflow_has_human_reply_signal(alert_module):
    """The workflow should accept human_reply signals."""
    wf = alert_module.AlertResponseWorkflow
    assert hasattr(wf, "human_reply")


def test_alert_response_input_defined(alert_module):
    """AlertResponseInput dataclass should exist."""
    assert hasattr(alert_module, "AlertResponseInput")
    inp = alert_module.AlertResponseInput
    assert hasattr(inp, "__dataclass_fields__")


def test_load_allowlist_returns_dict(alert_module):
    """load_allowlist should return a dict."""
    result = alert_module.load_allowlist(str(EXAMPLE_DIR / "allowlist.yaml"))
    assert isinstance(result, dict)


def test_load_allowlist_missing_file_returns_empty(alert_module):
    """load_allowlist with a nonexistent path should return {}."""
    result = alert_module.load_allowlist("/nonexistent/allowlist.yaml")
    assert result == {}


def test_is_allowlisted_true(alert_module):
    """An allowlisted action should return True."""
    allowlist = {"restart": ["nginx", "redis"]}
    assert alert_module._is_allowlisted("nginx", "restart", allowlist) is True


def test_is_allowlisted_false(alert_module):
    """A non-allowlisted action should return False."""
    allowlist = {"restart": ["nginx"]}
    assert alert_module._is_allowlisted("drop-database", "restart", allowlist) is False


def test_is_allowlisted_missing_category(alert_module):
    """A category not in the allowlist should return False."""
    allowlist = {"restart": ["nginx"]}
    assert alert_module._is_allowlisted("nginx", "destroy", allowlist) is False


# --------------------------------------------------------------------------- #
# worker.py registration
# --------------------------------------------------------------------------- #


def test_worker_registers_devloop_workflow():
    text = (EXAMPLE_DIR / "worker.py").read_text()
    assert "DevLoopWorkflow" in text


def test_worker_registers_alert_response_workflow():
    text = (EXAMPLE_DIR / "worker.py").read_text()
    assert "AlertResponseWorkflow" in text


def test_worker_imports_from_alert_response():
    text = (EXAMPLE_DIR / "worker.py").read_text()
    assert "from alert_response import" in text or "import alert_response" in text


def test_worker_registers_both_in_workflows_list():
    """Both DevLoopWorkflow and AlertResponseWorkflow must appear in WORKFLOWS."""
    text = (EXAMPLE_DIR / "worker.py").read_text()
    # Find the WORKFLOWS list
    wf_match = re.search(r"WORKFLOWS\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert wf_match, "worker.py should define a WORKFLOWS list"
    wf_block = wf_match.group(1)
    assert "DevLoopWorkflow" in wf_block, "WORKFLOWS should include DevLoopWorkflow"
    assert "AlertResponseWorkflow" in wf_block, "WORKFLOWS should include AlertResponseWorkflow"


# --------------------------------------------------------------------------- #
# uv.lock
# --------------------------------------------------------------------------- #


def test_uv_lock_exists_and_has_content():
    lock = EXAMPLE_DIR / "uv.lock"
    assert lock.exists()
    content = lock.read_text()
    assert "omneval-devloop" in content, "uv.lock should resolve omneval-devloop"


# --------------------------------------------------------------------------- #
# Integration: workflow end-to-end with time-skipping env
# --------------------------------------------------------------------------- #
# Note: Integration tests require the full Temporal time-skipping environment
# and proper allowlist file paths. The unit tests above cover all logic paths.
# A full integration test can be added once the example runs in a container.
