"""Tests verifying _as_int has been consolidated into PhaseOps.as_int.

This test enforces locality: the conversion logic lives in one place
(phase_ops.py via PhaseOps.as_int), not scattered across multiple
modules.  When a caller needs the behaviour, they reach PhaseOps —
they do not re-define it locally.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Files that previously defined local _as_int functions (all now
# expected to be deleted after consolidation).
_FILES_CHECKED: list[str] = [
    "src/devloop/dev_loop.py",
    "src/devloop/phases/execute.py",
    "src/devloop/phases/notifier.py",
    "src/devloop/phases/pipeline.py",
    "src/devloop/phases/plan.py",
    "src/devloop/phases/review.py",
    "src/devloop/phases/review_fix_pass.py",
]


class TestAsIntConsolidation:
    """The _as_int helper lives ONLY in PhaseOps, not duplicated elsewhere."""

    def _has_local_as_int(self, source_path: str) -> bool:
        """Return True if the file defines a module-level _as_int function."""
        filepath = Path(source_path)
        if not filepath.exists():
            return False
        source = filepath.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "_as_int" and isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    # Check it's module-level (not inside a class/function)
                    for parent_node in ast.walk(tree):
                        if (
                            isinstance(parent_node, (ast.Module,))
                            and parent_node is not node
                        ):
                            pass
                    # Simpler check: it's at module top level if its lineno
                    # is before the first class or function def.
                    # Just check it exists.
                    return True
        return False

    def test_no_local_as_int_in_source_files(self) -> None:
        """No file in the list above should define a module-level _as_int."""
        for path in _FILES_CHECKED:
            found = False
            filepath = Path(path)
            if not filepath.exists():
                continue
            source = filepath.read_text()
            tree = ast.parse(source)
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == "_as_int":
                        found = True
                        break
            assert not found, (
                f"{path} still defines a local _as_int function — "
                "use PhaseOps.as_int instead"
            )

    def test_phase_ops_has_as_int(self) -> None:
        """PhaseOps provides the as_int method."""
        from devloop.phases.phase_ops import PhaseOps

        assert hasattr(PhaseOps, "as_int")
        assert callable(getattr(PhaseOps, "as_int"))

    def test_as_int_converts_valid_int(self) -> None:
        """PhaseOps.as_int returns int as-is."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps().as_int(42) == 42

    def test_as_int_converts_string_number(self) -> None:
        """PhaseOps.as_int parses numeric strings."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps().as_int("123") == 123

    def test_as_int_returns_zero_for_invalid(self) -> None:
        """PhaseOps.as_int returns 0 for non-convertible values."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps().as_int("abc") == 0
        assert PhaseOps().as_int(None) == 0
