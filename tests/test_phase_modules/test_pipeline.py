"""Unit tests for PhasePipeline — the orchestration loop."""

from __future__ import annotations

from devloop.dev_loop import DevLoopInput, DevLoopResult
from devloop.phases.pipeline import PhasePipeline


class _MockPhases:
    """Collect calls for assertions."""

    def __init__(self) -> None:
        self.plan_calls: list[tuple] = []
        self.execute_calls: list[tuple] = []
        self.review_calls: list[tuple] = []
        self.fix_calls: list[tuple] = []
        self.notify_calls: list[tuple] = []


async def _make_phases(state: _MockPhases) -> dict:
    """Build phase callables for the pipeline."""

    async def plan_phase(inp, rnd):
        state.plan_calls.append((inp, rnd))
        return {"issues": []}

    async def execute_phase(inp, issue):
        state.execute_calls.append((inp, issue))
        return {"commits": 0, "branch": "", "pr_url": "", "issue_id": issue["id"]}

    async def review_phase(inp, issue, exec_result):
        state.review_calls.append((inp, issue, exec_result))
        return {"verdict": "lgtm"}

    async def fix_pass(inp, issue, exec_result, review):
        state.fix_calls.append((inp, issue, exec_result, review))
        return False  # no fix needed

    async def notifier(inp, issue, exec_result):
        state.notify_calls.append((inp, issue, exec_result))

    return {
        "plan": plan_phase,
        "execute": execute_phase,
        "review": review_phase,
        "fix_pass": fix_pass,
        "notify": notifier,
    }


class TestPhasePipelineEmpty:
    """Pipeline completes when plan returns no issues."""

    async def test_no_issues(self) -> None:
        """When plan returns no issues, the pipeline completes the round."""
        state = _MockPhases()

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            return {"issues": []}

        pipeline = PhasePipeline()
        inp = DevLoopInput(project_id="test", max_iterations=3)
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=lambda i, e: {"commits": 0, "branch": "", "pr_url": ""},
            review_phase=lambda i, e, r: {"verdict": "lgtm"},
            fix_pass=lambda i, e, r, v: False,
            notifier=lambda i, e, r: None,
        )
        assert isinstance(result, DevLoopResult)
        assert result.status == "completed"
        assert result.detail == ""
        assert len(state.plan_calls) == 1


class TestPhasePipelineNextIssue:
    """Pipeline #184: when plan exhausts the current issue, a queued issue
    (from ``next_issue``) is picked up instead of ending the run."""

    async def test_queued_issue_continues_run(self) -> None:
        """Plan empties for issue #1, ``next_issue`` hands back #2, and the
        pipeline plans/executes/notifies #2 in the same run."""
        state = _MockPhases()
        plans = {
            1: {"issues": [{"id": 1, "title": "first"}]},
            2: {"issues": [{"id": 2, "title": "second"}]},
        }
        call_log: list[int] = []

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            call_log.append(inp.triggering_issue)
            # First call for an issue returns it; the second call for that
            # same issue (the "no more rounds" check) returns empty.
            if call_log.count(inp.triggering_issue) > 1:
                return {"issues": []}
            return plans[inp.triggering_issue]

        async def execute_phase(inp, issue):
            state.execute_calls.append((inp, issue))
            return {
                "commits": 1,
                "branch": "main",
                "pr_url": f"https://pr/{issue['id']}",
            }

        async def review_phase(inp, issue, exec_result):
            state.review_calls.append((inp, issue, exec_result))
            return {"verdict": "lgtm", "summary": ""}

        async def notifier(inp, issue, exec_result):
            state.notify_calls.append((inp, issue, exec_result))

        queue = [2]

        def next_issue():
            return queue.pop(0) if queue else 0

        pipeline = PhasePipeline()
        inp = DevLoopInput(project_id="test", triggering_issue=1, max_iterations=5)
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=execute_phase,
            review_phase=review_phase,
            fix_pass=lambda i, e, r, v: False,
            notifier=notifier,
            next_issue=next_issue,
        )

        assert result.status == "completed"
        assert result.queued_for_review == [1, 2]
        assert len(state.execute_calls) == 2
        assert queue == []  # drained

    async def test_empty_queue_completes_normally(self) -> None:
        """When ``next_issue`` has nothing queued, the run completes as before."""

        async def plan_phase(inp, rnd):
            return {"issues": []}

        result = await PhasePipeline().run(
            DevLoopInput(project_id="test", triggering_issue=1, max_iterations=3),
            plan_phase=plan_phase,
            execute_phase=lambda i, e: {"commits": 0},
            review_phase=lambda i, e, r: {"verdict": "lgtm"},
            fix_pass=lambda i, e, r, v: False,
            notifier=lambda i, e, r: None,
            next_issue=lambda: 0,
        )
        assert result.status == "completed"
        assert result.queued_for_review == []


class TestPhasePipelinePlanFail:
    """Pipeline returns failed_plan when plan returns None."""

    async def test_plan_returns_none(self) -> None:
        """When plan returns None, the pipeline returns failed_plan."""

        result = await PhasePipeline().run(
            DevLoopInput(project_id="test", max_iterations=3),
            plan_phase=lambda i, r: None,
            execute_phase=lambda i, e: {"commits": 0},
            review_phase=lambda i, e, r: {"verdict": "lgtm"},
            fix_pass=lambda i, e, r, v: False,
            notifier=lambda i, e, r: None,
        )
        assert result.status == "failed_plan"
        assert result.detail == "plan rejected"


class TestPhasePipelineSingleIssue:
    """Pipeline processes a single issue through plan→execute→review→notify."""

    async def test_full_round(self) -> None:
        """One issue goes through all phases, queued_for_review includes it."""
        state = _MockPhases()
        call_count = {"plan": 0, "exec": 0, "review": 0, "notify": 0}

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            call_count["plan"] += 1
            if call_count["plan"] > 1:
                return {"issues": []}  # second round: no more issues
            return {"issues": [{"id": 42, "title": "test"}]}

        async def execute_phase(inp, issue):
            state.execute_calls.append((inp, issue))
            call_count["exec"] += 1
            return {"commits": 3, "branch": "main", "pr_url": "https://pr/42"}

        async def review_phase(inp, issue, exec_result):
            state.review_calls.append((inp, issue, exec_result))
            call_count["review"] += 1
            return {"verdict": "lgtm", "summary": ""}

        async def notifier(inp, issue, exec_result):
            state.notify_calls.append((inp, issue, exec_result))
            call_count["notify"] += 1

        pipeline = PhasePipeline()
        inp = DevLoopInput(project_id="test", max_iterations=3)
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=execute_phase,
            review_phase=review_phase,
            fix_pass=lambda i, e, r, v: False,
            notifier=notifier,
        )

        assert result.status == "completed"
        assert 42 in result.queued_for_review
        assert len(state.plan_calls) == 2  # round 1 (issue) + round 2 (empty)
        assert len(state.execute_calls) == 1
        assert len(state.review_calls) == 1
        assert len(state.notify_calls) == 1


class TestPhasePipelineReviewNeedsFixes:
    """Pipeline loops review + fix_pass until lgtm or max iterations."""

    async def test_needs_fixes_then_lgtm(self) -> None:
        """Review needs_fixes → fix → review lgtm → notify."""
        state = _MockPhases()
        review_verdicts: list[str] = []

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            if rnd > 1:  # only issue on first round
                return {"issues": []}
            return {"issues": [{"id": 42, "title": "test"}]}

        async def execute_phase(inp, issue):
            state.execute_calls.append((inp, issue))
            return {"commits": 1, "branch": "main", "pr_url": "https://pr/42"}

        async def review_phase(inp, issue, exec_result):
            state.review_calls.append((inp, issue, exec_result))
            verdict = review_verdicts.pop(0) if review_verdicts else "lgtm"
            return {"verdict": verdict, "summary": ""}

        async def fix_pass(inp, issue, exec_result, review):
            state.fix_calls.append((inp, issue, exec_result, review))
            return True  # fix produced commits

        async def notifier(inp, issue, exec_result):
            state.notify_calls.append((inp, issue, exec_result))

        review_verdicts.append("needs_fixes")  # first review needs_fixes
        review_verdicts.append("lgtm")  # second review passes

        pipeline = PhasePipeline()
        inp = DevLoopInput(
            project_id="test",
            max_iterations=3,
            review_fix_max_iterations=2,
        )
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=execute_phase,
            review_phase=review_phase,
            fix_pass=fix_pass,
            notifier=notifier,
        )

        assert result.status == "completed"
        assert 42 in result.queued_for_review
        assert len(state.fix_calls) == 1
        assert len(state.review_calls) == 2  # original + re-review


class TestPhasePipelineMaxIterations:
    """Pipeline stops after max_iterations rounds."""

    async def test_max_iterations(self) -> None:
        """After max_iterations, pipeline stops with completed status."""
        state = _MockPhases()
        call_count = {"plan": 0}

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            call_count["plan"] += 1
            if call_count["plan"] >= inp.max_iterations:
                return {"issues": []}  # last round: no more issues
            return {"issues": [{"id": 42, "title": "test"}]}

        pipeline = PhasePipeline()
        inp = DevLoopInput(
            project_id="test",
            max_iterations=3,
            execute_max_iterations=1,
        )
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=lambda i, e: {"commits": 1, "branch": "", "pr_url": ""},
            review_phase=lambda i, e, r: {"verdict": "lgtm", "summary": ""},
            fix_pass=lambda i, e, r, v: False,
            notifier=lambda i, e, r: None,
        )
        assert result.status == "completed"
        assert len(state.plan_calls) == 3  # 3 issue rounds (round 3 returns empty)


class TestPhasePipelineExecuteNoCommits:
    """Pipeline skips review when execute produces zero commits."""

    async def test_execute_zero_commits_skips_review(self) -> None:
        """Zero commits from execute → no review, no notify, next round."""
        state = _MockPhases()
        call_count = {"plan": 0}

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            call_count["plan"] += 1
            if call_count["plan"] >= inp.max_iterations:
                return {"issues": []}
            return {"issues": [{"id": 42, "title": "test"}]}

        async def execute_phase(inp, issue):
            state.execute_calls.append((inp, issue))
            return {"commits": 0, "branch": "", "pr_url": ""}

        async def review_phase(inp, issue, exec_result):
            state.review_calls.append((inp, issue, exec_result))
            return {"verdict": "lgtm"}

        async def notifier(inp, issue, exec_result):
            state.notify_calls.append((inp, issue, exec_result))

        pipeline = PhasePipeline()
        inp = DevLoopInput(project_id="test", max_iterations=2)
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=execute_phase,
            review_phase=review_phase,
            fix_pass=lambda i, e, r, v: False,
            notifier=notifier,
        )
        assert result.status == "completed"
        assert len(state.review_calls) == 0
        assert len(state.notify_calls) == 0


class TestPhasePipelineFixPassNoCommits:
    """Pipeline stops fix loop when fix_pass produces no commits."""

    async def test_fix_pass_returns_false_stops(self) -> None:
        """When fix_pass returns False, the fix loop stops and proceeds."""
        state = _MockPhases()
        call_count = {"plan": 0}

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            call_count["plan"] += 1
            if call_count["plan"] >= inp.max_iterations:
                return {"issues": []}
            return {"issues": [{"id": 42, "title": "test"}]}

        async def execute_phase(inp, issue):
            state.execute_calls.append((inp, issue))
            return {"commits": 1, "branch": "main", "pr_url": "https://pr/42"}

        async def review_phase(inp, issue, exec_result):
            state.review_calls.append((inp, issue, exec_result))
            return {"verdict": "needs_fixes", "summary": "fix me"}

        async def fix_pass(inp, issue, exec_result, review):
            state.fix_calls.append((inp, issue, exec_result, review))
            return False  # fix produced no commits

        async def notifier(inp, issue, exec_result):
            state.notify_calls.append((inp, issue, exec_result))

        pipeline = PhasePipeline()
        inp = DevLoopInput(
            project_id="test",
            max_iterations=2,
            review_fix_max_iterations=3,
        )
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=execute_phase,
            review_phase=review_phase,
            fix_pass=fix_pass,
            notifier=notifier,
        )
        assert result.status == "completed"
        assert len(state.fix_calls) == 1  # only one, fix returned False
        assert len(state.review_calls) == 1  # no re-review


class TestPhasePipelineConsecutiveZeroCommits:
    """Pipeline stops re-planning an issue after 2 consecutive zero-commit rounds."""

    async def test_breaks_on_two_consecutive_zero_commits(self) -> None:
        """When the same issue produces zero commits twice, the pipeline
        breaks out of the round loop instead of looping for max_iterations."""
        state = _MockPhases()
        plan_rounds: list[int] = []

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            # Issue #42 is available in rounds 1-10 (but should be abandoned
            # after 2 zero-commit rounds)
            if len(plan_rounds) < 10:
                plan_rounds.append(rnd)
                return {"issues": [{"id": 42, "title": "test"}]}
            # After the pipeline breaks, the plan is still called for the
            # next round — this verifies the break actually happened.
            return {"issues": []}

        async def execute_phase(inp, issue):
            state.execute_calls.append((inp, issue))
            return {"commits": 0, "branch": "", "pr_url": ""}

        pipeline = PhasePipeline()
        # max_iterations=10 would normally loop 10 rounds, but our guard
        # should break after round 2 (2 consecutive zero commits).
        inp = DevLoopInput(project_id="test", max_iterations=10)
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=execute_phase,
            review_phase=lambda i, e, r: {"verdict": "lgtm"},
            fix_pass=lambda i, e, r, v: False,
            notifier=lambda i, e, r: None,
        )
        assert result.status == "completed"
        # Only 2 plan calls: round 1 + round 2, then break.
        assert len(state.plan_calls) == 2
        # Only 2 execute calls: same two rounds.
        assert len(state.execute_calls) == 2
        # No review/notify because we broke before executing the full round.
        assert len(state.review_calls) == 0
        assert len(state.notify_calls) == 0

    async def test_resets_on_success_between_fails(self) -> None:
        """When a different issue succeeds between zero-commit attempts,
        the counter resets — back-to-back zero commits on a single issue
        is what triggers the break, not zero commits with interleaved
        successes on other issues."""
        state = _MockPhases()
        plan_rnds: list[int] = []

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            plan_rnds.append(rnd)
            if len(plan_rnds) >= 6:
                return {"issues": []}
            # Rounds 1,3,5: #42 (fails); rounds 2,4: #99 (succeeds)
            if rnd % 2 == 0:
                return {"issues": [{"id": 99, "title": "99"}]}
            return {"issues": [{"id": 42, "title": "42"}]}

        async def execute_phase(inp, issue):
            state.execute_calls.append((inp, issue))
            if issue["id"] == 99:
                return {"commits": 1, "branch": "main", "pr_url": "https://pr/99"}
            return {"commits": 0, "branch": "", "pr_url": ""}

        async def review_phase(inp, issue, exec_result):
            state.review_calls.append((inp, issue, exec_result))
            return {"verdict": "lgtm"}

        async def notifier(inp, issue, exec_result):
            state.notify_calls.append((inp, issue, exec_result))

        pipeline = PhasePipeline()
        inp = DevLoopInput(project_id="test", max_iterations=10)
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=execute_phase,
            review_phase=review_phase,
            fix_pass=lambda i, e, r, v: False,
            notifier=notifier,
        )
        # Issue #99 succeeds on every even round, resetting the counter
        # before #42 can accumulate 2 consecutive zeros.
        assert result.status == "completed"
        assert 99 in result.queued_for_review
        # Should have completed all rounds (up to the empty plan cap).
        assert len(state.plan_calls) >= 4
    """Pipeline stops after review_fix_max_iterations fix passes."""

    async def test_max_fix_passes(self) -> None:
        """After max fix passes, review proceeds even with needs_fixes."""
        state = _MockPhases()
        call_count = {"plan": 0}

        async def plan_phase(inp, rnd):
            state.plan_calls.append((inp, rnd))
            call_count["plan"] += 1
            if call_count["plan"] >= inp.max_iterations:
                return {"issues": []}
            return {"issues": [{"id": 42, "title": "test"}]}

        async def execute_phase(inp, issue):
            state.execute_calls.append((inp, issue))
            return {"commits": 1, "branch": "main", "pr_url": "https://pr/42"}

        async def review_phase(inp, issue, exec_result):
            state.review_calls.append((inp, issue, exec_result))
            return {"verdict": "needs_fixes", "summary": "fix me"}

        async def fix_pass(inp, issue, exec_result, review):
            state.fix_calls.append((inp, issue, exec_result, review))
            return True  # always produces commits

        async def notifier(inp, issue, exec_result):
            state.notify_calls.append((inp, issue, exec_result))

        pipeline = PhasePipeline()
        inp = DevLoopInput(
            project_id="test",
            max_iterations=2,
            review_fix_max_iterations=2,
        )
        result = await pipeline.run(
            inp,
            plan_phase=plan_phase,
            execute_phase=execute_phase,
            review_phase=review_phase,
            fix_pass=fix_pass,
            notifier=notifier,
        )
        assert result.status == "completed"
        assert len(state.fix_calls) == 2  # max reached
        assert len(state.review_calls) == 3  # original + 2 re-reviews
