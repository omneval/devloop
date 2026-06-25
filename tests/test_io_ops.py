"""Tracer bullet test for the IoOps module extraction.

Verifies that:
- IoOps module exists at devloop.phases.io_ops
- IoOps exposes all 13 I/O helper methods as class attributes
- PhaseOps inherits from IoOps
- PhaseOps instances have every IoOps method available
"""

from __future__ import annotations

import pytest

# ── IoOps module existence and structure ──────────────────────────────────


class TestIoOpsModule:
    """Verify the IoOps module exists and exposes the expected methods."""

    def test_io_ops_module_importable(self) -> None:
        """IoOps is importable from devloop.phases.io_ops."""
        from devloop.phases.io_ops import IoOps

        assert IoOps is not None

    def test_io_ops_has_all_methods(self) -> None:
        """IoOps exposes all 13 I/O helper methods as class attributes."""
        from devloop.phases.io_ops import IoOps

        expected_methods = [
            "_comment",
            "_dispatch",
            "_cleanup",
            "_request_reviewer",
            "_emit_kpis",
            "_kpi_bump",
            "_kpi_take",
            "_phase_comment",
            "_phase_request_reviewer",
            "poll",
            "dispatch_helper",
            "as_int",
            "pr_number_from_url",
        ]
        for method_name in expected_methods:
            assert hasattr(IoOps, method_name), f"IoOps missing method: {method_name}"

    def test_io_ops_has_docstring(self) -> None:
        """IoOps module has a docstring explaining it as the deep I/O adapter."""
        import devloop.phases.io_ops as io_ops_module

        assert io_ops_module.__doc__ is not None
        assert "I/O" in io_ops_module.__doc__ or "io" in io_ops_module.__doc__.lower()


# ── PhaseOps inherits from IoOps ─────────────────────────────────────────


class TestPhaseOpsInheritsIoOps:
    """PhaseOps references IoOps so existing callers see no change."""

    def test_phase_ops_is_subclass_of_io_ops(self) -> None:
        """PhaseOps is a subclass of IoOps."""
        from devloop.phases.io_ops import IoOps
        from devloop.phases.phase_ops import PhaseOps

        assert issubclass(PhaseOps, IoOps)

    @pytest.mark.parametrize(
        "method_name",
        [
            "_comment",
            "_dispatch",
            "_cleanup",
            "_request_reviewer",
            "_emit_kpis",
            "_kpi_bump",
            "_kpi_take",
            "_phase_comment",
            "_phase_request_reviewer",
            "poll",
            "dispatch_helper",
            "as_int",
            "pr_number_from_url",
        ],
    )
    def test_phaseops_instances_have_iops_methods(self, method_name: str) -> None:
        """Every IoOps method is available on PhaseOps instances."""
        from devloop.phases.phase_ops import PhaseOps

        ops = PhaseOps()
        assert hasattr(ops, method_name), (
            f"PhaseOps instance missing method: {method_name}"
        )


# ── Backward compatibility: existing callers still work ──────────────────


class TestBackwardCompatibility:
    """Existing callers see no change after the IoOps extraction."""

    @pytest.mark.asyncio
    async def test_comment_still_works_via_callback(self) -> None:
        """PhaseOps._comment still calls the injectable callback."""
        from devloop.phases.phase_ops import PhaseOps

        ops = PhaseOps()
        log: list = []
        ops.comment = lambda pid, num, body: log.append((pid, num, body)) or (None,)  # type: ignore[assignment]

        # Use a proper async mock
        async def cb(pid: str, num: int, body: str) -> None:
            log.append((pid, num, body))

        ops.comment = cb
        await ops._comment("p", 42, "hello")
        assert log == [("p", 42, "hello")]

    def test_phase_ops_shrinks(self) -> None:
        """phase_ops.py has roughly ~80 lines, not 678."""
        import pathlib

        phase_ops_path = (
            pathlib.Path(__file__).parent.parent
            / "src"
            / "devloop"
            / "phases"
            / "phase_ops.py"
        )
        lines = phase_ops_path.read_text().splitlines()
        # Account for docstring, imports, protocol fields, __init__, sub-protocol refs
        code_lines = [
            ln for ln in lines if ln.strip() and not ln.strip().startswith("#")
        ]
        assert len(code_lines) < 200, (
            f"phase_ops.py still has {len(code_lines)} non-empty lines, "
            "expected it to shrink significantly after extracting IoOps"
        )
