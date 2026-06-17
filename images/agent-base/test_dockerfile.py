"""Guard tests for the agent-base Dockerfile.

The entrypoint runs as ``python /usr/local/bin/agent-entrypoint.py`` and does a
bare ``import skills``, which only resolves when ``skills.py`` is baked into the
same directory (sys.path[0]). This regressed once: the COPY was dropped while the
``import skills`` line stayed, so every released Agent Job crashed at import with
"No module named 'skills'". These tests fail loudly if the two ever drift apart
again — they only parse text, so they run anywhere without Docker.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).parent
_DOCKERFILE = (_HERE / "Dockerfile").read_text(encoding="utf-8")
_ENTRYPOINT = (_HERE / "entrypoint.py").read_text(encoding="utf-8")


def _copy_target(src_basename: str) -> str | None:
    """Return the destination path of a ``COPY <src> <dst>`` line, or None."""
    for line in _DOCKERFILE.splitlines():
        m = re.match(rf"\s*COPY\s+{re.escape(src_basename)}\s+(\S+)", line)
        if m:
            return m.group(1)
    return None


def test_skills_module_is_copied_next_to_entrypoint():
    """skills.py must land in the same dir as the entrypoint so `import skills`
    resolves (entrypoint runs as a script, so its dir is sys.path[0])."""
    entrypoint_dst = _copy_target("entrypoint.py")
    skills_dst = _copy_target("skills.py")

    assert entrypoint_dst, "Dockerfile no longer COPYs entrypoint.py"
    assert skills_dst, (
        "Dockerfile does not COPY skills.py — entrypoint's `import skills` will "
        "fail at runtime with \"No module named 'skills'\""
    )
    assert Path(skills_dst).parent == Path(entrypoint_dst).parent, (
        f"skills.py ({skills_dst}) must sit beside the entrypoint "
        f"({entrypoint_dst}) for `import skills` to resolve"
    )


def test_entrypoint_still_imports_skills():
    """Tripwire: if the bare `import skills` is ever removed, the COPY guard above
    is moot — but we want to notice that change here too, not silently."""
    assert re.search(r"^\s*import skills\b", _ENTRYPOINT, re.MULTILINE), (
        "entrypoint.py no longer does `import skills` — if intentional, update or "
        "remove test_skills_module_is_copied_next_to_entrypoint accordingly"
    )


def test_tmux_is_installed():
    """tmux must be present in the agent-base image so devloop workers don't fall
    back to the less-stable subprocess-based terminal.

    See issue #156 for the original warning that prompted this requirement.
    """
    assert "tmux" in _DOCKERFILE, (
        "Dockerfile does not install tmux — devloop workers will log "
        "'tmux is not installed' and fall back to subprocess-based terminals"
    )
