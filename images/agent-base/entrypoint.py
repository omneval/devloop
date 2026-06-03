"""Agent Execution Job entrypoint (issues #18 stub, #19 real).

Reads a JSON ``TASK_SPEC`` from the environment, performs the work for its
``phase`` (execute / review / merge / diagnosis), and writes a result payload
to the output ConfigMap (named by ``OUTPUT_CONFIGMAP``) that the Orchestration
Worker's ``dispatch_agent_job`` activity polls.

OTLP spans for clone / install / each agent step / push are exported to omneval
using the ``OTEL_*`` env injected by the Job (X-API-Key auth via
``OTEL_EXPORTER_OTLP_HEADERS=x-api-key=...``; the omneval project is resolved
server-side from the key, tagged by phase via ``OTEL_SERVICE_NAME``).

Designed for testability: ``run_agent`` is the single seam an integration test
mocks, and the output sink writes to a local file when not in a cluster.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# The Agent Job ↔ worker protocol (TaskSpec, AgentJobResult, ConfigMap keys) is
# owned by the installed omneval-devloop package so both images share one
# definition; renaming a field there propagates to both sides.
from devloop.shared import KEY_HUMAN_ANSWER, KEY_RESULT, AgentJobResult, TaskSpec

log = logging.getLogger("agent-entrypoint")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# --------------------------------------------------------------------------- #
# Tracing
# --------------------------------------------------------------------------- #
def _name_openhands_llm_spans(service: str) -> None:
    """Attribute OpenHands' LLM/agent spans to the phase service name.

    OpenHands emits LLM telemetry through Laminar (the ``lmnr`` package), which
    auto-starts whenever ``OTEL_*`` env is present and exports to the same OTLP
    endpoint we use for omneval. Its tracer reports the OTel ``service.name`` as
    ``sys.argv[0]`` — here ``/usr/local/bin/agent-entrypoint.py`` — because
    OpenHands calls ``Laminar.initialize()`` without an ``app_name`` and lmnr's
    ``TracerManager.init`` defaults ``app_name=sys.argv[0]``. The result: every
    LLM call (the bulk of all spans) lands under that bogus service in omneval's
    "User Consumption", dwarfing the real per-phase services we set via
    ``OTEL_SERVICE_NAME`` (plan/execute/review/merge).

    We wrap ``TracerManager.init`` so a missing ``app_name`` falls back to
    ``service`` (the phase) instead of ``sys.argv[0]``. This must run before
    OpenHands imports/initialises lmnr (i.e. before ``run_agent``), which is why
    ``setup_tracing`` calls it up front. Best-effort: if lmnr is absent or its
    layout changed, OpenHands' default behaviour is left untouched rather than
    failing the Job. The lmnr module was renamed from ``traceloop_sdk`` to
    ``opentelemetry_lib`` across versions, so both paths are attempted.
    """
    manager = None
    for module_name in ("lmnr.opentelemetry_lib", "lmnr.traceloop_sdk"):
        try:
            module = __import__(module_name, fromlist=["TracerManager"])
            manager = module.TracerManager
            break
        except Exception:  # noqa: BLE001 - any import/attr failure: skip silently
            continue

    if manager is None or getattr(manager.init, "_omneval_phase_named", False):
        return

    original_init = manager.init

    def init_with_phase(*args, app_name=None, **kwargs):
        # Inject the phase only when the caller (OpenHands) passed no app_name,
        # positionally or by keyword. If it did, respect it untouched.
        if app_name is None and not args:
            app_name = service
        if app_name is None:
            return original_init(*args, **kwargs)
        return original_init(*args, app_name=app_name, **kwargs)

    init_with_phase._omneval_phase_named = True
    manager.init = staticmethod(init_with_phase)
    log.info("named OpenHands LLM telemetry service %r", service)


def setup_tracing():
    """Configure an OTLP tracer from OTEL_* env. Returns a tracer (no-op if the
    OpenTelemetry SDK is unavailable, e.g. in the stub/test path)."""
    # Fix OpenHands' LLM-span service.name before it initialises lmnr (below,
    # any provider setup, and run_agent's openhands import all happen after).
    _name_openhands_llm_spans(os.getenv("OTEL_SERVICE_NAME", "agent"))
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except Exception:  # pragma: no cover - SDK absent in unit tests
        from contextlib import nullcontext

        class _NoopTracer:
            def start_as_current_span(self, *a, **k):
                return nullcontext()

        return _NoopTracer()

    service = os.getenv("OTEL_SERVICE_NAME", "agent")
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    # Endpoint + headers (x-api-key) are read from OTEL_EXPORTER_OTLP_* env.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("agent-entrypoint")


# --------------------------------------------------------------------------- #
# Task spec — the type is the shared protocol (imported above)
# --------------------------------------------------------------------------- #
def load_task_spec() -> TaskSpec:
    return TaskSpec.from_env(os.environ.get("TASK_SPEC", "{}"))


# --------------------------------------------------------------------------- #
# Output sink (ConfigMap in-cluster; local file otherwise)
# --------------------------------------------------------------------------- #
def write_output(payload: dict) -> None:
    name = os.getenv("OUTPUT_CONFIGMAP", "")
    namespace = os.getenv("AGENTS_NAMESPACE", "agents")
    body = {KEY_RESULT: json.dumps(payload)}

    if os.getenv("OUTPUT_FILE"):
        Path(os.environ["OUTPUT_FILE"]).write_text(json.dumps(payload))
        log.info("wrote output to file %s", os.environ["OUTPUT_FILE"])
        return

    from kubernetes import client, config

    config.load_incluster_config()
    core = client.CoreV1Api()
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace), data=body
    )
    try:
        core.create_namespaced_config_map(namespace, cm)
    except client.exceptions.ApiException as exc:
        if getattr(exc, "status", None) == 409:
            core.patch_namespaced_config_map(name, namespace, {"data": body})
        else:
            raise
    log.info("wrote output ConfigMap %s", name)


def read_human_answer() -> str:
    """Read a human's mid-run reply written back by the orchestration worker."""
    if os.getenv("HUMAN_ANSWER_FILE"):
        p = Path(os.environ["HUMAN_ANSWER_FILE"])
        return p.read_text() if p.exists() else ""
    name = os.getenv("OUTPUT_CONFIGMAP", "")
    namespace = os.getenv("AGENTS_NAMESPACE", "agents")
    from kubernetes import client, config

    config.load_incluster_config()
    cm = client.CoreV1Api().read_namespaced_config_map(name, namespace)
    return (cm.data or {}).get(KEY_HUMAN_ANSWER, "")


def request_human_input(
    question: str,
    *,
    tracer=None,
) -> tuple[str, bool]:
    """Pause the agent mid-run to ask a human a clarifying question via Discord.

    Writes ``{"status": "awaiting_human", "question": question}`` to the output
    ConfigMap/file (which the orchestration worker detects and forwards to
    Discord).  Then polls ``read_human_answer()`` every
    ``HUMAN_ANSWER_POLL_SECONDS`` (default 15) until an answer arrives or
    ``HUMAN_ANSWER_TIMEOUT_SECONDS`` (default 14400 = 4 hours) elapses.

    Returns:
        (answer, False) — the human replied in time.
        ("", True)     — the timeout elapsed; caller should proceed with a
                         best-guess assumption and document it in the summary.
    """
    timeout = float(os.getenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "14400"))
    poll = float(os.getenv("HUMAN_ANSWER_POLL_SECONDS", "15"))

    write_output(
        AgentJobResult(status="awaiting_human", question=question).to_payload()
    )
    log.info("awaiting human answer to: %s", question)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = read_human_answer()
        if answer:
            log.info("received human answer")
            return answer, False
        if poll > 0:
            time.sleep(poll)
        else:
            # poll=0 means instant-check once then exit (test mode)
            break

    log.warning("human answer timeout after %.0fs; proceeding with best guess", timeout)
    return "", True


def _extract_question(text: str) -> str | None:
    """Detect a clarifying question in the agent's response text.

    Convention: the agent emits a line starting with ``QUESTION:`` (case-
    sensitive) when it needs human input before it can proceed.  Everything
    after the prefix on that line is the question text.

    Returns the question string, or None if no QUESTION: line is present.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("QUESTION:"):
            return stripped[len("QUESTION:") :].strip()
    return None


# --------------------------------------------------------------------------- #
# git / gh helpers
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], cwd: str | None = None) -> str:
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(
        cmd, cwd=cwd, check=True, text=True, capture_output=True
    ).stdout


# --------------------------------------------------------------------------- #
# Test-suite discovery and execution
# --------------------------------------------------------------------------- #
_NPM_DEFAULT_PLACEHOLDER = 'echo "Error: no test specified" && exit 1'
_MAX_TEST_OUTPUT = (
    4096  # bytes kept in result payload (truncated for Discord / Temporal UI)
)


def run_project_tests(workdir: str, timeout: int = 300) -> tuple[bool, str]:
    """Discover and run the project's test suite(s).

    Discovery mirrors ``install_deps`` — checks for go.mod, pyproject.toml /
    setup.py, and package.json.  For each detected ecosystem a separate test
    command is run; ALL must pass for the overall result to be True.

    Node/npm special case: if the ``test`` script in package.json is the npm
    default placeholder (``echo "Error: no test specified" && exit 1``) or is
    absent, that ecosystem is skipped rather than false-failed.

    Policy — no tests detected: returns ``(True, "no tests detected — skipped")``
    so that a bare project (e.g. docs-only repo) is not blocked from merging.
    This is documented here: treat absence of a test harness as a pass, not a
    failure, because blocking every project that hasn't set up tests yet would
    cause more pain than it prevents.  A future issue can add a required-tests
    policy flag.

    A failed subprocess (non-zero exit) does NOT raise — the exit code is
    captured and returned as ``passed=False`` so the caller can report it.
    """
    p = Path(workdir)
    commands: list[tuple[str, list[str]]] = []  # (label, cmd)

    if (p / "go.mod").exists():
        commands.append(("go", ["go", "test", "./..."]))

    if (p / "pyproject.toml").exists() or (p / "setup.py").exists():
        commands.append(("python", ["python", "-m", "pytest"]))

    pkg_json = p / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            test_script = pkg.get("scripts", {}).get("test", "")
            if test_script and test_script.strip() != _NPM_DEFAULT_PLACEHOLDER.strip():
                commands.append(("node", ["npm", "test"]))
        except (json.JSONDecodeError, OSError):
            pass  # malformed package.json — skip

    if not commands:
        return True, "no tests detected — skipped"

    all_passed = True
    combined_output: list[str] = []

    for label, cmd in commands:
        log.info("running %s tests: %s", label, " ".join(cmd))
        result = subprocess.run(
            cmd, cwd=workdir, text=True, capture_output=True, timeout=timeout
        )
        out = (result.stdout or "") + (result.stderr or "")
        combined_output.append(f"[{label}]\n{out}")
        if result.returncode != 0:
            all_passed = False
            log.warning("%s test suite FAILED (exit %d)", label, result.returncode)
        else:
            log.info("%s test suite passed", label)

    return all_passed, "\n".join(combined_output)


def clone_repo(github_url: str, branch: str, workdir: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    url = github_url
    if token and url.startswith("https://"):
        url = url.replace("https://", f"https://x-access-token:{token}@")
    _run(["git", "clone", "--branch", branch, url, workdir])
    _run(["git", "config", "user.name", "homelab-agent"], cwd=workdir)
    _run(["git", "config", "user.email", "agent@blosshomelab.com"], cwd=workdir)


def install_deps(workdir: str) -> None:
    p = Path(workdir)
    if (p / "go.mod").exists():
        _run(["go", "mod", "download"], cwd=workdir)
    if (p / "package-lock.json").exists():
        _run(["npm", "ci"], cwd=workdir)
    elif (p / "package.json").exists():
        _run(["npm", "install"], cwd=workdir)
    if (p / "pyproject.toml").exists() or (p / "setup.py").exists():
        _run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=workdir)


def push_branch(workdir: str, branch: str, force: bool = False) -> None:
    """Push ``branch`` to origin.

    ``force=True`` is used for agent-owned issue branches so a re-run (a Temporal
    activity retry, or a fresh Dev Loop round on the same issue) overwrites any
    stale remote head instead of being rejected non-fast-forward. The default
    (no force) is used when pushing the protected default branch in the merge
    phase — that push must stay fast-forward."""
    cmd = ["git", "push", "--set-upstream", "origin", branch]
    if force:
        cmd.insert(2, "--force")
    _run(cmd, cwd=workdir)


def open_draft_pr(workdir: str, branch: str, base: str, title: str, body: str) -> str:
    """Open a draft PR for the pushed branch (best-effort).

    A failure here must NOT abort an otherwise-successful implementation: the
    branch is already pushed, and the merge phase operates on the branch
    directly (git fetch + merge), not the PR. The PR is purely informational.
    Common failure: the GitHub token lacks ``pull_requests: write`` (gh exits
    non-zero / 403), or a PR for the branch already exists. Returns the PR URL,
    or "" when creation failed (logged, not raised)."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=workdir,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        log.warning(
            "draft PR not created (continuing without one): %s",
            (result.stderr or result.stdout or "").strip(),
        )
        return ""
    out = (result.stdout or "").strip()
    return out.splitlines()[-1] if out else ""


def repo_slug(github_url: str) -> str:
    """Return ``owner/repo`` from a GitHub URL (for ``gh -R``)."""
    slug = github_url.rstrip("/").removesuffix(".git")
    parts = slug.split("/")
    return f"{parts[-2]}/{parts[-1]}"


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    """Run a ``gh`` subcommand, capturing output (never raises)."""
    log.info("$ gh %s", " ".join(args))
    return subprocess.run(["gh", *args], text=True, capture_output=True)


def _existing_pr(repo: str, branch: str) -> tuple[str, bool]:
    """Return (url, is_draft) for an open PR whose head is ``branch``, or ("", False)."""
    r = _gh(["pr", "view", branch, "-R", repo, "--json", "url,isDraft"])
    if r.returncode != 0:
        return "", False
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return "", False
    return data.get("url", ""), bool(data.get("isDraft"))


def open_review_pr(
    repo: str, branch: str, base: str, title: str, body: str, reviewer: str
) -> str:
    """Ensure a *ready-for-review* PR exists for ``branch`` → ``base`` and tag the
    reviewer. This is the merge phase's terminal action under the PR-review model:
    instead of merging the approved branch into the default branch directly, the
    human reviews and merges the PR on GitHub (its ``Closes #N`` then closes the
    issue).

    The execute phase opens a *draft* PR while work is in flight; here we surface
    it, mark it ready, and tag the reviewer. If none exists yet (draft creation
    earlier was skipped) we create one.

    Tagging strategy: the agent's GitHub token usually authenticates as the human
    reviewer's own account, and GitHub forbids requesting a review from the PR
    author — so a formal review *request* (``--add-reviewer``) is best-effort
    only. The reliable signals are an assignee (self-assignment is allowed) and
    an ``@``-mention comment, both of which notify the reviewer. All ``gh`` calls
    scope to ``repo`` via ``-R`` (the branch is already pushed; no checkout
    needed). Returns the PR URL, or "" if the PR could not be created.
    """
    url, is_draft = _existing_pr(repo, branch)
    if url:
        if is_draft:
            _gh(["pr", "ready", branch, "-R", repo])
    else:
        r = _gh(
            [
                "pr",
                "create",
                "-R",
                repo,
                "-H",
                branch,
                "-B",
                base,
                "-t",
                title,
                "-b",
                body,
            ]
        )
        if r.returncode != 0:
            log.warning(
                "review PR not created: %s", (r.stderr or r.stdout or "").strip()
            )
            return ""
        out = (r.stdout or "").strip()
        url = out.splitlines()[-1] if out else ""

    if reviewer:
        # Self-assignment always works and notifies; review request is best-effort.
        _gh(["pr", "edit", branch, "-R", repo, "--add-assignee", reviewer])
        _gh(["pr", "edit", branch, "-R", repo, "--add-reviewer", reviewer])
        _gh(
            [
                "pr",
                "comment",
                branch,
                "-R",
                repo,
                "--body",
                f"cc @{reviewer} — ready for review.",
            ]
        )
    return url


def _review_pr_body(issue_number: int, summary: str, reviewer: str) -> str:
    parts = []
    if issue_number:
        parts.append(f"Implements #{issue_number}.")
    if summary:
        parts.append(summary)
    if issue_number:
        parts.append(f"Closes #{issue_number}")
    if reviewer:
        parts.append(f"cc @{reviewer} — ready for review.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Agent runner (the single seam mocked by the integration test)
# --------------------------------------------------------------------------- #
@dataclass
class AgentOutcome:
    summary: str = ""
    files_changed: bool = True
    structured: dict | None = None  # review/diagnosis JSON


def build_agent(llm, cli_mode: bool = True, agent_context=None):
    """Construct an Agent using the default preset (issue #32).

    Replicates ``get_default_agent(llm, cli_mode)`` — wiring terminal,
    file_editor, task_tracker tools and the LLM-summarising condenser — and
    adds ``agent_context`` to the ``Agent(...)`` constructor so installed
    skills are injected when provided.

    ``agent_context=None`` is the no-op path: the agent is built exactly as
    before issue #32, so existing behaviour is preserved when no skills are
    loaded.
    """
    from openhands.sdk import Agent
    from openhands.tools.preset.default import get_default_condenser, get_default_tools

    tools = get_default_tools(enable_browser=not cli_mode)
    condenser = get_default_condenser(
        llm=llm.model_copy(update={"usage_id": "condenser"})
    )
    return Agent(
        llm=llm,
        tools=tools,
        system_prompt_kwargs={"cli_mode": cli_mode},
        condenser=condenser,
        agent_context=agent_context,
    )


def run_agent(spec: TaskSpec, workdir: str, tracer) -> AgentOutcome:
    """Drive an OpenHands LocalConversation over the cloned workspace.

    Stub mode (AGENT_STUB=1) returns a fixed success without invoking the model
    — used to prove the dispatch→poll→ConfigMap round-trip (issue #18).

    Real mode uses the openhands-sdk 1.24.0 API:
        LLM(model, base_url, api_key)
        → resolve_skills(phase, allowlist=None)   ← skills.py seam (#32)
        → AgentContext(skills=..., load_public_skills=False) when skills present
        → build_agent(llm=llm, cli_mode=True, agent_context=ctx)
          (hand-rolled preset: terminal + file_editor + task_tracker tools;
          cli_mode drops the Chromium-only browser tool; agent_context carries
          installed skills — None when none are installed, no-op path)
        → LocalConversation(agent=agent, workspace=workdir)
        → send_message → run → get_agent_final_response(state.events)

    Failure modes:
    - Model/LLM error (run() raises): caught, returns a failed AgentOutcome.
    - Empty final response (no diff / agent produced no output): files_changed=False.
    Neither case leaves the Job hung; main() always writes a terminal ConfigMap.
    """
    if os.getenv("AGENT_STUB") == "1":
        return AgentOutcome(summary="stub run", files_changed=False)

    # Lazy import so the module stays importable without the SDK installed
    # (existing integration tests mock run_agent directly).
    from openhands.sdk import AgentContext, LLM, LocalConversation
    from openhands.sdk.conversation import get_agent_final_response

    # Resolve installed skills for this phase.  allowlist=None means all
    # installed skills are allowed; per-phase filtering is the skills.py seam.
    import skills as _skills_mod

    resolved, skipped = _skills_mod.resolve_skills(spec.phase, allowlist=None)
    if skipped:
        log.info("run_agent: skipped skills %s", skipped)

    # Construct AgentContext only when skills are available.  Empty → None →
    # agent behaves as before issue #32 (no-op path).  load_public_skills=False
    # prevents the agent from fetching public skills off GitHub at Job runtime.
    agent_context = (
        AgentContext(skills=resolved, load_public_skills=False) if resolved else None
    )

    message = build_agent_message(spec)
    try:
        with tracer.start_as_current_span("agent.run"):
            llm = LLM(
                model=os.getenv("AGENT_MODEL", "qwen3-27b"),
                base_url=os.getenv("AGENT_LLM_BASE_URL", "http://192.168.68.104/v1"),
                api_key=os.getenv("AGENT_LLM_API_KEY", "local"),
            )
            agent = build_agent(llm=llm, cli_mode=True, agent_context=agent_context)
            conversation = LocalConversation(agent=agent, workspace=workdir)
            conversation.send_message(message)
            conversation.run()
            text = get_agent_final_response(conversation.state.events)
    except Exception as exc:  # noqa: BLE001
        log.exception("run_agent failed: %s", exc)
        return AgentOutcome(
            summary=f"agent error: {exc}",
            files_changed=False,
        )

    if not text:
        log.warning("run_agent: empty final response — agent produced no output")
        return AgentOutcome(summary="agent produced no output", files_changed=False)

    # ------------------------------------------------------------------ #
    # Mid-run human-question round-trip (issue #36)
    # If the agent emits a QUESTION: line it needs human clarification
    # before it can continue.  We park the Job (awaiting_human), wait for
    # the orchestration worker to write back the answer, then resume.
    # ------------------------------------------------------------------ #
    question = _extract_question(text)
    if question:
        answer, timed_out = request_human_input(question, tracer=tracer)

        if timed_out:
            # 4-hour timeout exceeded — instruct the agent to proceed on its own.
            # Document the assumption in the summary so it surfaces in the PR body.
            best_guess_note = (
                f"[best-guess assumption: no human answer received within the timeout "
                f"for question '{question}'; agent proceeded autonomously]"
            )
            log.warning("proceeding with best guess — %s", best_guess_note)
            resume_prompt = (
                "No human answer was received within the allowed timeout. "
                "Please proceed with your best assumption and complete the task."
            )
        else:
            best_guess_note = ""
            resume_prompt = f"Human answer: {answer}"

        # Feed the answer (or best-guess instruction) back and re-run.
        try:
            conversation.send_message(resume_prompt)
            conversation.run()
            text = get_agent_final_response(conversation.state.events)
        except Exception as exc:  # noqa: BLE001
            log.exception("run_agent resume failed: %s", exc)
            return AgentOutcome(
                summary=f"agent error during resume: {exc}",
                files_changed=False,
            )

        if best_guess_note:
            text = f"{text}\n\n{best_guess_note}" if text else best_guess_note

    structured = _extract_diagnosis(text) if spec.phase == "diagnosis" else None
    return AgentOutcome(summary=text, structured=structured)


# --------------------------------------------------------------------------- #
# Prompt templates (bundled in the agent-base image; one per phase)
# --------------------------------------------------------------------------- #
# entrypoint phase -> bundled prompt template filename
_PROMPT_FILES = {
    "plan": "plan.md",
    "execute": "implement.md",
    "review": "review.md",
    "merge": "merge.md",
    "diagnosis": "diagnosis.md",
}

_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")
_INSTALLED_PROMPTS = "/usr/local/share/agent-prompts"


def _prompts_dir() -> str:
    """Resolve the prompt-template directory.

    ``AGENT_PROMPTS_DIR`` wins; otherwise the image install path is used, with a
    fallback to ``prompts/`` next to this file so the suite runs from the repo.
    """
    env = os.getenv("AGENT_PROMPTS_DIR")
    if env:
        return env
    if Path(_INSTALLED_PROMPTS).is_dir():
        return _INSTALLED_PROMPTS
    return str(Path(__file__).parent / "prompts")


def load_prompt(name: str, variables: dict[str, str]) -> str:
    """Read a bundled prompt template and substitute ``{{VAR}}`` placeholders.

    Any placeholder the caller does not supply is stripped so it never leaks
    into the agent prompt as a literal ``{{FOO}}``.
    """
    text = Path(_prompts_dir(), name).read_text()
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return _PLACEHOLDER_RE.sub("", text)


def _prompt_variables(spec: TaskSpec) -> dict[str, str]:
    base = os.environ.get("DEFAULT_BRANCH", "main")
    if spec.phase == "plan":
        feedback = (spec.extra.get("feedback") or "").strip()
        return {
            "AGENT_LABEL": spec.extra.get("agent_label", "agent-ready"),
            "FEEDBACK": (
                "# REVISION\n\nThe previous plan was rejected. Address this "
                f"feedback before re-planning:\n\n{feedback}"
                if feedback
                else ""
            ),
        }
    if spec.phase == "execute":
        return {
            "TASK_ID": str(spec.issue_number),
            "ISSUE_TITLE": spec.title,
            "BRANCH": spec.branch,
        }
    if spec.phase == "review":
        return {"BRANCH": spec.branch, "SOURCE_BRANCH": base}
    if spec.phase == "merge":
        branches = spec.extra.get("branches", [])
        issues = spec.extra.get("issues", [])
        return {
            "BRANCHES": "\n".join(f"- {b}" for b in branches),
            "ISSUES": "\n".join(
                f"- {i.get('id')}: {i.get('title', '')}" for i in issues
            ),
        }
    if spec.phase == "diagnosis":
        alert = spec.extra.get("alert", {}) or {}
        details = {
            "labels": alert.get("labels", {}),
            "annotations": alert.get("annotations", {}),
        }
        return {
            "ALERT_NAME": str(alert.get("name", "") or "unknown-alert"),
            "ALERT_SEVERITY": str(alert.get("severity", "") or "warning"),
            "ALERT_NAMESPACE": str(alert.get("namespace", "") or "(unknown)"),
            "ALERT_DETAILS": json.dumps(details),
        }
    return {}


def build_agent_message(spec: TaskSpec) -> str:
    """Build the prompt sent to the agent for this phase.

    plan / execute / review / merge / diagnosis render the bundled prompt
    templates (the diagnosis template asks for a structured ``<diagnosis>`` JSON
    block so the Alert Response remediation phase gets executable actions).
    """
    if spec.phase in _PROMPT_FILES:
        return load_prompt(_PROMPT_FILES[spec.phase], _prompt_variables(spec))
    return spec.instructions


_PLAN_RE = re.compile(r"<plan>([\s\S]*?)</plan>", re.IGNORECASE)


def _extract_plan(text: str) -> dict | None:
    """Parse the ``<plan>{...}</plan>`` block emitted by the planner prompt."""
    m = _PLAN_RE.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return None


def _extract_json(text: str) -> dict | None:
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


_DIAG_RE = re.compile(r"<diagnosis>([\s\S]*?)</diagnosis>", re.IGNORECASE)


def _extract_diagnosis(text: str) -> dict | None:
    """Parse the ``<diagnosis>{...}</diagnosis>`` block the diagnosis prompt emits.

    Tolerant of a ```` ```json ```` fence inside the tags and of the model
    omitting the tags entirely (falls back to the first/last-brace JSON scan).
    Returns None if no JSON is recoverable."""
    m = _DIAG_RE.search(text or "")
    inner = m.group(1) if m else (text or "")
    inner = inner.strip()
    if inner.startswith("```"):
        # drop a leading ```json / ``` fence and any trailing fence
        inner = re.sub(r"^```[a-zA-Z]*\n?", "", inner)
        inner = re.sub(r"\n?```$", "", inner.strip())
    try:
        return json.loads(inner.strip())
    except json.JSONDecodeError:
        return _extract_json(inner)


_REVIEW_RE = re.compile(r"<review>([\s\S]*?)</review>", re.IGNORECASE)


def _extract_review(text: str) -> dict | None:
    """Parse the ``<review>{...}</review>`` block the reviewer prompt emits:
    ``{"summary": str, "inline_comments": [{"file","line","body"}]}``.

    Only matches an explicit ``<review>`` block (unlike diagnosis, which falls
    back to a brace scan) — the reviewer's free-text narration would otherwise be
    misparsed as findings. Tolerant of a ```` ```json ```` fence inside the tags.
    Returns None when no block is present or no JSON is recoverable."""
    m = _REVIEW_RE.search(text or "")
    if not m:
        return None
    inner = m.group(1).strip()
    if inner.startswith("```"):
        inner = re.sub(r"^```[a-zA-Z]*\n?", "", inner)
        inner = re.sub(r"\n?```$", "", inner.strip())
    try:
        return json.loads(inner.strip())
    except json.JSONDecodeError:
        return _extract_json(inner)


def _normalize_actions(actions) -> list[dict]:
    """Coerce the model's recommended_actions into ``[{action, requires_approval,
    rationale}]`` with a string command and a bool gate.

    ``requires_approval`` defaults to **False** so an allowlisted command runs
    autonomously by default (the allowlist is the real safety boundary — a
    non-allowlisted command is gated regardless of this flag). The agent sets it
    True to force a human gate on an otherwise-autonomous command. Entries with
    no command string are dropped."""
    out: list[dict] = []
    for a in actions or []:
        if isinstance(a, dict):
            cmd = str(a.get("action", "") or "").strip()
            if not cmd:
                continue
            out.append(
                {
                    "action": cmd,
                    "requires_approval": bool(a.get("requires_approval", False)),
                    "rationale": str(a.get("rationale", "") or ""),
                }
            )
        elif isinstance(a, str) and a.strip():
            out.append(
                {"action": a.strip(), "requires_approval": False, "rationale": ""}
            )
    return out


# --------------------------------------------------------------------------- #
# Phase handlers
# --------------------------------------------------------------------------- #
def _issue_ids(spec: TaskSpec) -> list[int]:
    """Issue numbers from spec.extra['issues'], accepting either ``[{id,...}]``
    (workflow) or a bare ``[id, ...]`` list."""
    ids: list[int] = []
    for item in spec.extra.get("issues", []):
        value = item.get("id") if isinstance(item, dict) else item
        if str(value).isdigit():
            ids.append(int(value))
    return ids


def _commit_count(workdir: str, since_sha: str) -> int:
    out = _run(["git", "rev-list", "--count", f"{since_sha}..HEAD"], cwd=workdir)
    return int(out.strip() or "0")


def handle_plan(spec: TaskSpec, tracer) -> dict:
    """Plan phase: clone the repo, run the planner prompt, and return the
    ``<plan>`` it emits ({"issues": [{id, title, branch}]}).

    The planner reads the open issues (via ``gh`` in the prompt) and the real
    codebase, builds a dependency graph, and lists the unblocked issues.
    """
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    base = os.environ.get("DEFAULT_BRANCH", "main")
    with tracer.start_as_current_span("clone"):
        clone_repo(os.environ["GITHUB_URL"], base, workdir)
    outcome = run_agent(spec, workdir, tracer)
    plan = _extract_plan(outcome.summary) or {"issues": []}
    plan.setdefault("issues", [])
    return AgentJobResult(
        status="complete", plan=plan, summary=outcome.summary
    ).to_payload()


def handle_execute(spec: TaskSpec, tracer) -> dict:
    """Execute phase: implement one issue on its branch using the implement
    prompt (Ralph/TDD loop). Pushes the branch and opens a draft PR only when
    the agent actually produced commits."""
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    github_url = os.environ["GITHUB_URL"]
    base = os.environ.get("DEFAULT_BRANCH", "main")
    branch = spec.branch or f"agent/issue-{spec.issue_number}"

    with tracer.start_as_current_span("clone"):
        clone_repo(github_url, base, workdir)
    with tracer.start_as_current_span("install_deps"):
        install_deps(workdir)

    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=workdir).strip()
    _run(["git", "checkout", "-b", branch], cwd=workdir)
    outcome = run_agent(spec, workdir, tracer)

    # The implement prompt commits its own work; sweep up anything left
    # uncommitted so a half-finished change still surfaces as commits.
    _run(["git", "add", "-A"], cwd=workdir)
    if _run(["git", "status", "--porcelain"], cwd=workdir).strip():
        _run(
            [
                "git",
                "commit",
                "-m",
                f"agent: implement #{spec.issue_number} {spec.title}",
            ],
            cwd=workdir,
        )

    commits = _commit_count(workdir, base_sha)
    if commits == 0:
        # No work produced — the workflow skips this issue (no branch/PR).
        return AgentJobResult(
            status="complete",
            issue_number=spec.issue_number,
            summary=outcome.summary or "agent produced no commits",
        ).to_payload()

    with tracer.start_as_current_span("tests"):
        tests_passed, test_output = run_project_tests(workdir)
    with tracer.start_as_current_span("push"):
        push_branch(workdir, branch, force=True)

    test_snippet = test_output[:_MAX_TEST_OUTPUT]
    summary_parts = [outcome.summary]
    if test_snippet:
        summary_parts.append(f"\n--- test output ---\n{test_snippet}")

    pr_url = open_draft_pr(
        workdir,
        branch,
        base,
        title=f"agent: #{spec.issue_number} {spec.title}",
        body=f"Implements #{spec.issue_number}.\n\n{outcome.summary}\n\nCloses #{spec.issue_number}",
    )
    return AgentJobResult(
        status="complete",
        issue_number=spec.issue_number,
        branch=branch,
        pr_url=pr_url,
        commits=commits,
        tests_passed=tests_passed,
        summary="\n".join(summary_parts),
    ).to_payload()


def handle_review(spec: TaskSpec, tracer) -> dict:
    """Review phase: the reviewer prompt refines the branch in place (clarity,
    consistency, standards) and commits. Any refinements are pushed back to the
    branch; functionality is preserved."""
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    with tracer.start_as_current_span("clone"):
        clone_repo(os.environ["GITHUB_URL"], spec.branch, workdir)
    with tracer.start_as_current_span("install_deps"):
        install_deps(workdir)

    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=workdir).strip()
    outcome = run_agent(spec, workdir, tracer)

    _run(["git", "add", "-A"], cwd=workdir)
    if _run(["git", "status", "--porcelain"], cwd=workdir).strip():
        _run(
            ["git", "commit", "-m", f"review: refine #{spec.issue_number}"], cwd=workdir
        )

    refinements = _commit_count(workdir, base_sha)
    if refinements:
        with tracer.start_as_current_span("push"):
            push_branch(workdir, spec.branch, force=True)
    return AgentJobResult(
        status="complete",
        issue_number=spec.issue_number,
        branch=spec.branch,
        commits=refinements,
        review=_extract_review(outcome.summary),
        summary=outcome.summary,
    ).to_payload()


def handle_merge(spec: TaskSpec, tracer) -> dict:
    """Merge phase (PR-review model): open a *review* PR for each approved branch
    instead of merging it into the default branch directly.

    The Discord Merge gate already approved that this work should go up for
    review; this phase turns the pushed branch into a ready-for-review PR (the
    execute phase opened it as a draft) and tags the reviewer (``PR_REVIEWER``,
    e.g. ``zbloss``). A human then does the final code review and merges the PR
    on GitHub — at which point its ``Closes #N`` closes the issue. Nothing is
    pushed to the default branch here, and no test re-run/merge happens locally:
    the GitHub PR (and any CI on it) is the gate.

    Failure to open *any* PR is a phase failure (the work would otherwise be
    stranded on a branch with no review surface)."""
    base = os.environ.get("DEFAULT_BRANCH", "main")
    repo = repo_slug(os.environ["GITHUB_URL"])
    reviewer = os.getenv("PR_REVIEWER", "").strip()
    branches = spec.extra.get("branches", [])
    issues = spec.extra.get("issues", [])

    pr_urls: list[str] = []
    for i, branch in enumerate(branches):
        issue = issues[i] if i < len(issues) else {}
        num = int(issue.get("id")) if str(issue.get("id", "")).isdigit() else 0
        title = f"agent: #{num} {issue.get('title', '')}".strip()
        body = _review_pr_body(num, "", reviewer)
        with tracer.start_as_current_span("open_pr"):
            url = open_review_pr(repo, branch, base, title, body, reviewer)
        if url:
            log.info("opened review PR for %s: %s", branch, url)
            pr_urls.append(url)
        else:
            log.error("could not open a review PR for branch %s", branch)

    if not pr_urls:
        return AgentJobResult(
            status="failed",
            merged_issues=_issue_ids(spec),
            summary="Merge phase opened no review PR (gh pr create failed).",
            error="no review PR opened",
        ).to_payload()

    return AgentJobResult(
        status="complete",
        merged_issues=_issue_ids(spec),
        pr_url=pr_urls[0],
        tests_passed=True,
        summary="Opened review PR(s): "
        + ", ".join(pr_urls)
        + (f"\n\nTagged @{reviewer} for review." if reviewer else ""),
    ).to_payload()


def handle_diagnosis(spec: TaskSpec, tracer) -> dict:
    """Diagnosis phase (Alert Response): the agent investigates read-only and
    emits a ``<diagnosis>`` JSON block. We normalize ``recommended_actions`` into
    executable ``{action, requires_approval, rationale}`` entries so the
    workflow's remediation phase can allowlist-check and (autonomously or after a
    Discord gate) run them. Falls back to a label-only diagnosis with no actions
    if the model produced nothing parseable."""
    alert = spec.extra.get("alert", {}) or {}
    outcome = run_agent(spec, os.getenv("WORKDIR", "/tmp"), tracer)
    structured = outcome.structured or {}
    diagnosis = {
        "severity": structured.get("severity") or alert.get("severity", "warning"),
        "affected_resource": structured.get("affected_resource")
        or alert.get("namespace", "unknown"),
        "root_cause_hypothesis": structured.get("root_cause_hypothesis")
        or outcome.summary,
        "recommended_actions": _normalize_actions(
            structured.get("recommended_actions")
        ),
    }
    return AgentJobResult(status="complete", diagnosis=diagnosis).to_payload()


_HANDLERS = {
    "plan": handle_plan,
    "execute": handle_execute,
    "review": handle_review,
    "merge": handle_merge,
    "diagnosis": handle_diagnosis,
}


def main() -> int:
    tracer = setup_tracing()
    spec = load_task_spec()
    log.info(
        "phase=%s project=%s issue=%s", spec.phase, spec.project_id, spec.issue_number
    )
    handler = _HANDLERS.get(spec.phase)
    if handler is None:
        write_output(
            AgentJobResult(
                status="failed", error=f"unknown phase {spec.phase!r}"
            ).to_payload()
        )
        return 1
    try:
        payload = handler(spec, tracer)
        write_output(payload)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("agent job failed")
        write_output(AgentJobResult(status="failed", error=str(exc)).to_payload())
        return 1


if __name__ == "__main__":
    sys.exit(main())
