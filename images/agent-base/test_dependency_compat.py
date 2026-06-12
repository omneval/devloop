"""Guard tests for dependency compatibility in the agent-base image.

These tests prevent regressions where dependency resolution picks versions
that are incompatible at runtime. They only parse text, so they run
anywhere without Docker or the full dependency tree.

Issue #144: lmnr >= 0.7.53 removed the ``rollout_entrypoint`` parameter that
openhands-sdk's laminar observability wrapper passes to ``laminar_observe()``.
The SDK pins ``lmnr>=0.7.47,<0.7.53`` from 1.28.0 onward; earlier SDKs had no
upper bound and could resolve to an incompatible lmnr.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).parent
_PYPROJECT = _HERE / "pyproject.toml"
_UV_LOCK = _HERE / "uv.lock"


def _toml_field(name: str) -> str | None:
    """Extract ``name = "value"`` or ``"name==version"`` from pyproject.toml.

    Handles both top-level key=value and array items like
    ``dependencies = ["openhands-sdk==1.28.1", ...]``.
    """
    text = _PYPROJECT.read_text(encoding="utf-8")

    # Direct key = "value"
    for line in text.splitlines():
        m = re.match(rf'\s*{re.escape(name)}\s*=\s*"([^"]+)"', line)
        if m:
            return m.group(1)

    # Array item: "name==version" or "name>=version"
    for line in text.splitlines():
        m = re.search(rf'["\']({re.escape(name)}[><=!~]+\d[^"\']*)["\']', line)
        if m:
            return m.group(1)

    return None


def _lock_version(package: str) -> str | None:
    """Return the version string for *package* from uv.lock.

    uv.lock uses TOML, so we scan for the ``name = "package"`` line in a
    ``[[package]]`` entry and then read the next ``version = "..."`` line.
    """
    text = _UV_LOCK.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf'^\s*name\s*=\s*"{re.escape(package)}"\s*$', line):
            for j in range(i + 1, min(i + 4, len(lines))):
                m = re.match(r'^\s*version\s*=\s*"([^"]+)"', lines[j])
                if m:
                    return m.group(1)
            break
    return None


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse ``major.minor.patch`` (ignore pre-release suffixes)."""
    parts = re.split(r"[.\-a-zA-Z_]", v)
    return tuple(int(p) for p in parts if p.isdigit())


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_sdk_version_is_compatible():
    """openhands-sdk must be >= 1.28.0, which added lmnr<0.7.53 upper bound."""
    sdk = _toml_field("openhands-sdk")
    assert sdk is not None, "pyproject.toml does not pin openhands-sdk"
    version = re.search(r"(\d+\.\d+\.\d+)", sdk)
    assert version, f"cannot parse openhands-sdk version from {sdk}"
    assert _version_tuple(version.group(1)) >= (1, 28, 0), (
        f"openhands-sdk {version.group(1)} < 1.28.0 — missing lmnr upper bound "
        f"that prevents rollout_entrypoint TypeError (issue #144)"
    )


def test_lock_resolves_compatible_lmnr():
    """uv.lock must resolve lmnr to a version < 0.7.53 (which dropped
    rollout_entrypoint)."""
    lmnr_ver = _lock_version("lmnr")
    assert lmnr_ver is not None, "uv.lock does not contain lmnr"
    assert _version_tuple(lmnr_ver) < (0, 7, 53), (
        f"uv.lock resolved lmnr {lmnr_ver} >= 0.7.53 — missing rollout_entrypoint, "
        f"will cause TypeError in openhands-sdk laminar wrapper (issue #144)"
    )


def test_lock_and_pyproject_sdk_agree():
    """The SDK version in uv.lock must match pyproject.toml — if they drift,
    the lock is stale and runtime deps may be wrong."""
    pyproject_sdk = _toml_field("openhands-sdk")
    assert pyproject_sdk is not None
    lock_sdk = _lock_version("openhands-sdk")
    assert lock_sdk is not None, "uv.lock does not contain openhands-sdk"

    pyproject_ver = re.search(r"(\d+\.\d+\.\d+)", pyproject_sdk)
    assert pyproject_ver
    assert lock_sdk == pyproject_ver.group(1), (
        f"openhands-sdk version mismatch: pyproject.toml has {pyproject_sdk} "
        f"but uv.lock has {lock_sdk} — regenerate with `uv lock`"
    )
