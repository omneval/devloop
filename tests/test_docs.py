"""Tests for documentation files: existence and YAML syntax validation."""

import re
from pathlib import Path

import pytest
import yaml


DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

EXPECTED_DOCS = [
    "getting-started.md",
    "temporal-prerequisites.md",
]

YAML_BLOCK_RE = re.compile(r"^```yaml\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _extract_yaml_blocks(markdown_text: str) -> list[str]:
    """Return all YAML code blocks from a markdown string."""
    return [m.group(1).strip() for m in YAML_BLOCK_RE.finditer(markdown_text)]


@pytest.mark.parametrize("filename", EXPECTED_DOCS)
def test_doc_file_exists(filename):
    path = DOCS_DIR / filename
    assert path.exists(), f"{filename} not found in docs/"


@pytest.mark.parametrize("filename", EXPECTED_DOCS)
def test_yaml_blocks_parse_without_error(filename):
    """Every fenced ```yaml block must be valid YAML."""
    path = DOCS_DIR / filename
    text = path.read_text()
    blocks = _extract_yaml_blocks(text)

    for i, block in enumerate(blocks):
        if not block:
            continue
        try:
            yaml.safe_load(block)
        except yaml.YAMLError as exc:
            pytest.fail(
                f"{filename} YAML block {i + 1} is not valid: {exc}\n"
                f"Block content:\n---\n{block}\n---"
            )


def test_getting_started_covers_temporal_install():
    text = (DOCS_DIR / "getting-started.md").read_text()
    assert "helm install temporal" in text


def test_getting_started_covers_devloop_deploy():
    text = (DOCS_DIR / "getting-started.md").read_text()
    assert "helm install devloop" in text


def test_getting_started_covers_project_enrollment():
    text = (DOCS_DIR / "getting-started.md").read_text()
    assert "projects.yaml" in text


def test_getting_started_covers_agent_image_build():
    text = (DOCS_DIR / "getting-started.md").read_text()
    assert "devloop-agent-base" in text
    assert "docker build" in text


def test_getting_started_covers_verification():
    text = (DOCS_DIR / "getting-started.md").read_text()
    assert "kubectl get pods" in text


def test_temporal_prerequisites_has_reference_values():
    text = (DOCS_DIR / "temporal-prerequisites.md").read_text()
    assert "sqlite" in text.lower()


def test_getting_started_documents_required_fields():
    """Project Registry schema must mention all required fields."""
    text = (DOCS_DIR / "getting-started.md").read_text()
    required = [
        "id",
        "github_url",
        "default_branch",
        "agent_image",
        "agent_label",
        "omneval_ingest_secret",
        "github_token_secret",
    ]
    for field in required:
        assert field in text, f"Required field '{field}' not documented"


def test_getting_started_documents_optional_fields():
    text = (DOCS_DIR / "getting-started.md").read_text()
    assert "pr_reviewer" in text


def test_getting_started_documents_config_settings():
    """Guide must cover GITHUB_TOKEN and temporalHost."""
    text = (DOCS_DIR / "getting-started.md").read_text()
    text_lower = text.lower()
    assert "github_token" in text_lower
    assert "temporalHost" in text


def _heading_to_anchor(heading: str) -> str:
    """Approximate GitHub's Markdown heading-to-anchor slugification."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s+", "-", slug)


def test_github_app_doc_anchor_links_resolve_to_real_headings():
    """Anchor links from github-app.md into getting-started.md must match a heading there (issue #91)."""
    github_app_text = (DOCS_DIR / "github-app.md").read_text()
    getting_started_text = (DOCS_DIR / "getting-started.md").read_text()

    headings = re.findall(r"^#+\s+(.+)$", getting_started_text, re.MULTILINE)
    anchors = {_heading_to_anchor(h) for h in headings}

    links = re.findall(r"\(getting-started\.md#([\w-]+)\)", github_app_text)
    assert links, "expected at least one anchor link from github-app.md into getting-started.md"
    for anchor in links:
        assert anchor in anchors, (
            f"github-app.md links to getting-started.md#{anchor}, "
            f"but no heading in getting-started.md slugifies to that anchor"
        )
