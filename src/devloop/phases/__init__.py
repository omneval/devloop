"""Phase pipeline modules — deep, standalone modules for each Dev Loop phase.

Re-exports for backward compatibility (issue #150 → #153):

    from devloop.phases import Phase, JobStatus

Unified callback protocol (issue #188):

    from devloop.phases import PhaseOps

Per-phase sub-protocols (issue #201):

    from devloop.phases import ExecutePhaseOps, ReviewPhaseOps, CICycleOps, PlanPhaseOps
"""

from devloop.phases.ci_cycle_ops import CICycleOps
from devloop.phases.enums import JobStatus, Phase
from devloop.phases.execute_phase_ops import ExecutePhaseOps
from devloop.phases.phase_ops import PhaseOps
from devloop.phases.plan_phase_ops import PlanPhaseOps
from devloop.phases.review_phase_ops import ReviewPhaseOps

__all__ = [
    "CICycleOps",
    "ExecutePhaseOps",
    "JobStatus",
    "Phase",
    "PhaseOps",
    "PlanPhaseOps",
    "ReviewPhaseOps",
]
