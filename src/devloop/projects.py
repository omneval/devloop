"""Project registry loader for the Orchestration Worker.

Reads agents/projects.yaml and surfaces typed ProjectConfig objects.
No dynamic reload — restart the worker to pick up registry changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = (
    "id",
    "github_url",
    "default_branch",
    "agent_label",
    "omneval_ingest_secret",
    "github_token_secret",
)


@dataclass(frozen=True)
class ProjectConfig:
    id: str
    github_url: str
    default_branch: str
    agent_label: str
    omneval_ingest_secret: str
    # Secret (agents ns, key "GITHUB_TOKEN") holding this project's scoped GitHub
    # token. Per-project so each org/owner gets its own credential — the worker
    # resolves it per project and the Agent Execution Job mounts it.
    github_token_secret: str
    # GitHub login tagged for review on the PR the merge phase opens (assignee +
    # @-mention). Optional; empty means the merge phase opens the PR untagged.
    pr_reviewer: str = ""
    # Agent Execution Job image. Optional since the universal image: empty means
    # the worker falls back to AGENT_DEFAULT_IMAGE (the published
    # devloop-agent-universal, via Helm temporalWorker.agentJob.defaultImage).
    # Set it only when the project needs a derived image with extra toolchains.
    agent_image: str = ""


def load_projects(path: str | Path) -> list[ProjectConfig]:
    """Parse projects.yaml and return a list of ProjectConfig.

    Raises ValueError if any project entry is missing required fields.
    """
    raw = Path(path).read_text()
    data: dict[str, Any] = yaml.safe_load(raw)
    entries = data.get("projects", [])
    configs: list[ProjectConfig] = []
    for entry in entries:
        missing = [f for f in _REQUIRED_FIELDS if f not in entry]
        if missing:
            raise ValueError(
                f"Project entry missing required fields: {missing!r} in {entry!r}"
            )
        configs.append(
            ProjectConfig(
                id=entry["id"],
                github_url=entry["github_url"],
                default_branch=entry["default_branch"],
                agent_image=entry.get("agent_image", ""),
                agent_label=entry["agent_label"],
                omneval_ingest_secret=entry["omneval_ingest_secret"],
                github_token_secret=entry["github_token_secret"],
                pr_reviewer=entry.get("pr_reviewer", ""),
            )
        )
    logger.info(
        "loaded %d project%s: %s",
        len(configs),
        "" if len(configs) == 1 else "s",
        ", ".join(c.id for c in configs),
    )
    return configs


# ---------------------------------------------------------------------------
# Process-wide registry.
#
# The worker calls install_registry() once at startup; activities then resolve
# project configs by id without re-reading the file (no dynamic reload — a
# worker restart is required to pick up registry changes).
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, ProjectConfig] = {}


def install_registry(path: str | Path) -> list[ProjectConfig]:
    """Load projects.yaml and make the configs available process-wide."""
    configs = load_projects(path)
    _REGISTRY.clear()
    _REGISTRY.update({c.id: c for c in configs})
    return configs


def get_project(project_id: str) -> ProjectConfig:
    """Return the registered ProjectConfig for ``project_id``.

    Raises KeyError if the project is not in the registry.
    """
    try:
        return _REGISTRY[project_id]
    except KeyError:
        raise KeyError(
            f"project {project_id!r} not in registry (known: {sorted(_REGISTRY)})"
        ) from None


def parse_github_repo(github_url: str) -> str:
    """Return ``owner/repo`` from a GitHub URL (used for gh CLI / REST calls)."""
    slug = github_url.rstrip("/").removesuffix(".git")
    parts = slug.split("/")
    return f"{parts[-2]}/{parts[-1]}"
