"""Tests for documentation files: existence and YAML syntax validation."""

import re
from pathlib import Path

import pytest
import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT_DIR / "docs"
README_PATH = ROOT_DIR / "README.md"

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
    assert links, (
        "expected at least one anchor link from github-app.md into getting-started.md"
    )
    for anchor in links:
        assert anchor in anchors, (
            f"github-app.md links to getting-started.md#{anchor}, "
            f"but no heading in getting-started.md slugifies to that anchor"
        )


# ---------------------------------------------------------------------------
# README.md — issue #42 (complete and standardize) and #57
# ---------------------------------------------------------------------------


def test_readme_has_project_header_with_title():
    """README must have a clear project name heading (issue #42)."""
    text = README_PATH.read_text()
    assert "# devloop" in text, "README should have '# devloop' as the main heading"


def test_readme_has_status_badges():
    """README must include at least one status badge (issue #42)."""
    text = README_PATH.read_text()
    assert "!" in text and "[" in text and "![" in text, (
        "README should contain at least one badge image"
    )


def test_readme_has_multi_sentence_description():
    """README must have a 2-3 sentence project description (issue #42)."""
    text = README_PATH.read_text()
    # Look for description paragraphs (lines that are not headings, badges, or links)
    lines = text.splitlines()
    desc_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("#")
            or stripped.startswith("![")
            or stripped.startswith("[")
        ):
            continue
        if stripped.startswith("- ") or stripped.startswith("|"):
            break
        desc_lines.append(stripped)
    # The description should have at least 2 sentences worth of content
    desc_text = " ".join(desc_lines[:3])
    sentence_count = desc_text.count(". ") + desc_text.count(".\n")
    assert sentence_count >= 2 or len(desc_text) > 100, (
        "README should have a 2-3 sentence description of what devloop is"
    )


def test_readme_has_key_features_section():
    """README must include a Key Features section (issue #42)."""
    text = README_PATH.read_text()
    assert re.search(r"##\s+Key Features", text, re.IGNORECASE), (
        "README should have a 'Key Features' section"
    )
    assert "- " in text or "* " in text, (
        "Key Features section should contain a bulleted list"
    )


def test_readme_has_prerequisites_section():
    """README must include a Prerequisites section listing system requirements (issue #42)."""
    text = README_PATH.read_text()
    assert re.search(r"##\s+Prerequisites", text, re.IGNORECASE), (
        "README should have a 'Prerequisites' section"
    )


def test_readme_has_installation_section():
    """README must include an Installation & Setup section (issue #42)."""
    text = README_PATH.read_text()
    assert re.search(r"##\s+(Installation|Getting Started)", text, re.IGNORECASE), (
        "README should have an 'Installation' or 'Getting Started' section"
    )


def test_readme_has_quick_start_section():
    """README must include a Quick Start or Usage Guide section (issue #42)."""
    text = README_PATH.read_text()
    assert re.search(
        r"##\s+(Quick Start|Usage|Running Dev Loop)", text, re.IGNORECASE
    ), "README should have a 'Quick Start' or usage guide section"


def test_readme_has_configuration_section():
    """README must include a Configuration section (issue #42)."""
    text = README_PATH.read_text()
    assert re.search(r"##\s+Configuration", text, re.IGNORECASE), (
        "README should have a 'Configuration' section"
    )


def test_readme_has_running_tests_section():
    """README must include a Running Tests section (issue #42)."""
    text = README_PATH.read_text()
    assert re.search(r"##\s+(Running Tests|Testing|Tests)", text, re.IGNORECASE), (
        "README should have a 'Running Tests' section"
    )


def test_readme_has_contributing_section():
    """README must include a Contributing section (issue #42)."""
    text = README_PATH.read_text()
    assert re.search(r"##\s+Contributing", text, re.IGNORECASE), (
        "README should have a 'Contributing' section"
    )


def test_readme_has_license_section():
    """README must include a License section (issue #42)."""
    text = README_PATH.read_text()
    assert re.search(r"##\s+License", text, re.IGNORECASE), (
        "README should have a 'License' section"
    )
    assert "Apache" in text or "MIT" in text or "license" in text.lower(), (
        "README License section should mention the license type"
    )


# ---------------------------------------------------------------------------
# README.md — issue #57 (preserved)
# ---------------------------------------------------------------------------


def test_readme_documents_response_format_requirement():
    """README.md must document the response_format consumer constraint (issue #57)."""
    text = README_PATH.read_text()
    assert "response_format" in text, (
        "README.md should document the response_format requirement for model endpoints"
    )


def test_readme_documents_skills_by_phase_remediation_and_fix_pass():
    """README.md must document skillsByPhase.remediation and skillsByPhase.fix_pass (issue #57)."""
    text = README_PATH.read_text()
    assert "remediation" in text, "README.md should document skillsByPhase.remediation"
    assert "fix_pass" in text, "README.md should document skillsByPhase.fix_pass"
