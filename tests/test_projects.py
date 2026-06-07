"""Unit tests for the ProjectConfig parser."""

import textwrap
from pathlib import Path

import pytest

from devloop.projects import ProjectConfig, load_projects

_VALID_YAML = textwrap.dedent("""\
    projects:
      - id: omneval
        github_url: https://github.com/omneval/omneval
        default_branch: main
        agent_image: ghcr.io/example/agent:sha-abc1234
        agent_label: agent-ready
        omneval_ingest_secret: omneval-ingest-omneval
        github_token_secret: omneval-agent-github-token
""")


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "projects.yaml"
    p.write_text(content)
    return p


def test_valid_yaml_returns_correct_dataclass(tmp_path):
    path = _write(tmp_path, _VALID_YAML)
    configs = load_projects(path)

    assert len(configs) == 1
    cfg = configs[0]
    assert isinstance(cfg, ProjectConfig)
    assert cfg.id == "omneval"
    assert cfg.github_url == "https://github.com/omneval/omneval"
    assert cfg.default_branch == "main"
    assert cfg.agent_image == "ghcr.io/example/agent:sha-abc1234"
    assert cfg.agent_label == "agent-ready"
    assert cfg.omneval_ingest_secret == "omneval-ingest-omneval"
    assert cfg.github_token_secret == "omneval-agent-github-token"


def test_empty_projects_list(tmp_path):
    path = _write(tmp_path, "projects: []\n")
    assert load_projects(path) == []


def test_multiple_projects(tmp_path):
    yaml = textwrap.dedent("""\
        projects:
          - id: alpha
            github_url: https://github.com/org/alpha
            default_branch: main
            agent_image: org/agent-alpha:latest
            agent_label: agent-ready
            omneval_ingest_secret: omneval-ingest-alpha
            github_token_secret: alpha-github-token
          - id: beta
            github_url: https://github.com/org/beta
            default_branch: trunk
            agent_image: org/agent-beta:latest
            agent_label: agent-ready
            omneval_ingest_secret: omneval-ingest-beta
            github_token_secret: beta-github-token
    """)
    configs = load_projects(_write(tmp_path, yaml))
    assert [c.id for c in configs] == ["alpha", "beta"]


@pytest.mark.parametrize(
    "missing_field",
    [
        "id",
        "github_url",
        "default_branch",
        "agent_image",
        "agent_label",
        "omneval_ingest_secret",
        "github_token_secret",
    ],
)
def test_missing_required_field_raises_value_error(tmp_path, missing_field):
    import yaml

    data = yaml.safe_load(_VALID_YAML)
    del data["projects"][0][missing_field]
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.dump(data))

    with pytest.raises(ValueError, match=missing_field):
        load_projects(path)
