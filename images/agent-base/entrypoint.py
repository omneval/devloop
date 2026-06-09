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
from typing import Literal

from openai import OpenAI as _OpenAI
from pydantic import BaseModel as _BaseModel

# The Agent Job ↔ worker protocol (TaskSpec, AgentJobResult, ConfigMap keys) is
# owned by the installed omneval-devloop package so both images share one
# definition; renaming a field there propagates to both sides.
from devloop.shared import KEY_HUMAN_ANSWER, KEY_RESULT, AgentJobResult, TaskSpec

# skills.py is baked beside this entrypoint at /usr/local/bin (the Dockerfile
# COPYs both there), so this bare import resolves via sys.path[0]. Imported at
# module top rather than lazily inside run_agent: a lazy import here once hid a
# missing COPY from every test, shipping a "No module named 'skills'" crash in a
# release. skills.py's own heavy deps (openhands) stay lazy inside it, so this
# top-level import is cheap and side-effect-free.
import skills

log = logging.getLogger("agent-entrypoint")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# --------------------------------------------------------------------------- #
# Structured output models (issue #53)
# --------------------------------------------------------------------------- #


class PlanIssue(_BaseModel):
    id: int
    title: str
    branch: str


class PlanOutput(_BaseModel):
    issues: list[PlanIssue] = []


class InlineComment(_BaseModel):
    file: str
    line: int
    body: str


class ReviewOutput(_BaseModel):
    summary: str
    verdict: Literal["lgtm", "needs_fixes", "needs_human"] = "needs_human"
    inline_comments: list[InlineComment] = []


class RecommendedAction(_BaseModel):
    action: str
    requires_approval: bool = False
    rationale: str = ""


class DiagnosisOutput(_BaseModel):
    severity: str
    affected_resource: str
    root_cause_hypothesis: str
    recommended_actions: list[RecommendedAction] = []


class CodeQualityScanResult(_BaseModel):
    score: int = 0
    report: str = ""
    scan_error: bool = False
    error_message: str = ""


def _get_llm_client() -> _OpenAI:
    return _OpenAI(
        api_key=os.environ.get("AGENT_LLM_API_KEY", "none"),
        base_url=os.environ.get("AGENT_LLM_BASE_URL"),
    )


def _strip_provider_prefix(model: str) -> str:
    """Strip a litellm-style ``<provider>/<model>`` prefix from *model*.

    ``AGENT_MODEL`` is configured using the provider-prefixed form (e.g.
    ``openai/qwen3.6-27b-mtp``) that the OpenHands ``LLM``/litellm stack
    expects for routing. The raw OpenAI SDK client used here talks directly
    to an OpenAI-compatible endpoint and rejects that prefixed name with
    "model not found", so it needs the bare model name instead.
    """
    return model.partition("/")[2] or model


def structured_extractor(text: str, model_cls: type[_BaseModel]) -> _BaseModel:
    """Extract structured output from *text* using a single LLM call with
    ``response_format`` backed by a Pydantic model.

    Raises ``ValueError`` with a clear message if the LLM response is malformed
    or cannot be parsed into *model_cls*.
    """
    client = _get_llm_client()
    model = _strip_provider_prefix(os.environ.get("AGENT_MODEL", "qwen3-27b"))
    schema = model_cls.model_json_schema()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the structured data from the following text. "
                        "Return only valid JSON matching the requested schema."
                    ),
                },
                {"role": "user", "content": text},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": model_cls.__name__, "schema": schema},
            },
        )
    except Exception as exc:
        raise ValueError(
            f"LLM call failed during structured extraction: {exc}"
        ) from exc

    content = response.choices[0].message.content
    if not content:
        raise ValueError(
            f"Empty LLM response during extraction of {model_cls.__name__}"
        )
    try:
        return model_cls.model_validate_json(content)
    except Exception as exc:
        raise ValueError(
            f"Malformed LLM response for {model_cls.__name__}: {exc}"
        ) from exc


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
    """Pause the agent mid-run to ask a human a clarifying question.

    Writes ``{"status": "awaiting_human", "question": question}`` to the output
    ConfigMap/file (which the orchestration worker detects and forwards to
    the human).  Then polls ``read_human_answer()`` every
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


def _extract_answer(text: str) -> str:
    """Pull the agent's final decision out of a ``Phase.ANSWER`` response.

    Convention (mirrors ``QUESTION:``): the agent emits a line starting with
    ``ANSWER:`` carrying its best-informed decision. Falls back to the full
    response text when no such line is present, so the paused agent always
    gets *something* usable back.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ANSWER:"):
            return stripped[len("ANSWER:") :].strip()
    return text.strip()


# --------------------------------------------------------------------------- #
# git / gh helpers
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], cwd: str | None = None) -> str:
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(
        cmd, cwd=cwd, check=True, text=True, capture_output=True
    ).stdout


def _describe_exception(exc: BaseException) -> str:
    """Render an exception for ``AgentJobResult.error``.

    ``CalledProcessError.__str__`` reports only the command and exit code, not
    the captured output — which hid the real cause behind failures like
    GitHub's "refusing to allow a Personal Access Token to create or update
    workflow `.github/workflows/...` without `workflow` scope" push rejection
    (it surfaced only as "git push ... returned non-zero exit status 1", with
    the actual rejection message stuck in ``exc.stderr``). Append the captured
    stderr/stdout, when present, so the real reason ends up in the job result
    instead of requiring a log dive."""
    msg = str(exc)
    if isinstance(exc, subprocess.CalledProcessError):
        output = (exc.stderr or exc.stdout or "").strip()
        if output:
            msg = f"{msg}\n{output}"
    return msg


# --------------------------------------------------------------------------- #
# Test-suite discovery and execution
# --------------------------------------------------------------------------- #
_NPM_DEFAULT_PLACEHOLDER = 'echo "Error: no test specified" && exit 1'
_MAX_TEST_OUTPUT = 4096  # bytes kept in result payload (truncated for Temporal UI)


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
    git_name = os.environ.get("GIT_AUTHOR_NAME", "homelab-agent")
    git_email = os.environ.get("GIT_AUTHOR_EMAIL", "agent@blosshomelab.com")
    _run(["git", "config", "user.name", git_name], cwd=workdir)
    _run(["git", "config", "user.email", git_email], cwd=workdir)


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


# Cap on the PR diff handed to the agent's prompt — keeps the LLM context (and
# the truncated copy logged for diagnostics) bounded regardless of PR size.
_MAX_PR_DIFF_CHARS = 60_000


def _fetch_pr_diff(repo: str, pr_number: int) -> str:
    """Fetch a PR's unified diff via ``gh`` (best-effort — never raises).

    The diff used to be fetched by the workflow and threaded through
    ``TaskSpec.extra["pr_diff"]`` into the ``TASK_SPEC`` env var — which broke
    for large PRs: a big enough diff pushes a single env var past Linux's
    ``MAX_ARG_STRLEN`` (128 KiB), and the container fails to even exec Python
    with "argument list too long". The agent already has ``GH_TOKEN``/``gh``
    and clones the repo, so it fetches the diff itself, off the env-var path
    entirely. Returns ``""`` on any failure — the agent still has the
    comment/review body and branch access to work from."""
    if pr_number <= 0:
        return ""
    result = _gh(["pr", "diff", str(pr_number), "-R", repo])
    if result.returncode != 0:
        log.warning(
            "could not fetch PR #%d diff: %s",
            pr_number,
            (result.stderr or result.stdout or "").strip(),
        )
        return ""
    diff = result.stdout or ""
    if len(diff) > _MAX_PR_DIFF_CHARS:
        diff = diff[:_MAX_PR_DIFF_CHARS] + "\n... (diff truncated)"
    return diff


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
# Skill allowlist parsing (issue #36)
# --------------------------------------------------------------------------- #
def _load_skills_allowlist(phase: str) -> dict | None:
    """Build the per-phase allowlist from ``AGENT_SKILLS_ENABLED``.

    ``AGENT_SKILLS_ENABLED`` is set by the Temporal Orchestration Worker's
    ``render_job`` for the active phase only — extracted from the
    ``AGENT_SKILLS_BY_PHASE`` JSON map delivered by the Helm chart.

    Three-way semantics (mirrors the Helm → worker → Job chain):
    - Env var absent   → returns ``None``           → all skills (default)
    - Env var = ``""`` → returns ``{phase: []}``    → no skills
    - Env var = names  → returns ``{phase: [names]}``

    The ``None``-sentinel is the backward-compat guarantee for existing
    deployments that were deployed before ``skillsByPhase`` was introduced:
    ``os.environ.get`` returns ``None`` (not ``""``) when the var is absent.
    """
    raw = os.environ.get("AGENT_SKILLS_ENABLED")  # None when absent (not "")
    if raw is None:
        # Env var not set → phase absent from the by-phase map → all skills
        return None
    if not raw.strip():
        # Env var set but empty → phase had [] → no skills
        return {phase: []}
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return {phase: names}


# --------------------------------------------------------------------------- #
# Agent runner (the single seam mocked by the integration test)
# --------------------------------------------------------------------------- #
@dataclass
class AgentOutcome:
    summary: str = ""
    files_changed: bool = True
    structured: dict | None = None  # review/diagnosis JSON


def build_agent(llm, cli_mode: bool = True, agent_context=None):
    """Construct an Agent using the default preset (issue #32, ADR-0007).

    Replicates ``get_default_agent(llm, cli_mode)`` — wiring terminal,
    file_editor, task_tracker tools and the LLM-summarising condenser — and
    adds ``agent_context`` to the ``Agent(...)`` constructor so installed
    skills are injected when provided.

    ``agent_context=None`` is the no-op path: the agent is built exactly as
    before issue #32, so existing behaviour is preserved when no skills are
    loaded.

    WHY hand-rolled instead of ``get_default_agent``?
    ``get_default_agent`` does not accept ``agent_context`` — there is no
    supported path to skills injection through it.  See ADR-0007.

    OVERRIDE SEAM: derived images can replace this function (``_base.build_agent
    = my_build_agent``) to inject custom tools, a different condenser, or a
    modified ``AgentContext`` without touching the rest of the entrypoint.
    See ``images/agent-base/SKILLS.md`` for an example.

    DO NOT replace this with a ``get_default_agent`` call — that would silently
    drop skills support.
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

    Real mode uses the openhands-sdk API:
        LLM(model, base_url, api_key)
        → _load_skills_allowlist(phase)                ← reads AGENT_SKILLS_ENABLED
        → resolve_skills(phase, allowlist)             ← skills.py seam (#32, #36)
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

    # ------------------------------------------------------------------ #
    # Skill resolution (issues #32, #35, #36)
    # Build the per-phase allowlist from AGENT_SKILLS_ENABLED (set by
    # render_job for the active phase) and pass it to resolve_skills.
    # Wrapped in a span so skill-loading health is observable in omneval.
    # Best-effort: if the loader raises the phase is never blocked.
    # ------------------------------------------------------------------ #
    _selection_mode = os.environ.get("AGENT_SKILLS_SELECTION_MODE", "triggers")
    _allowlist = _load_skills_allowlist(spec.phase)

    resolved: list = []
    skipped: list[dict] = []
    with tracer.start_as_current_span("skills.load") as _skills_span:
        try:
            resolved, skipped = skills.resolve_skills(spec.phase, _allowlist)
        except Exception as _exc:  # noqa: BLE001 — skill errors must not block the phase
            log.warning(
                "skills resolution failed (continuing without skills): %s", _exc
            )
            skipped = [{"name": "", "reason": f"loader error: {_exc}"}]

        if skipped:
            log.warning("run_agent: skipped skills: %s", skipped)

        # Emit OTLP attributes on the skills.load span.  OTel only accepts
        # primitive attribute values; the details list is JSON-encoded.
        if _skills_span is not None:
            _skills_span.set_attribute("skills.loaded", len(resolved))
            _skills_span.set_attribute("skills.skipped", len(skipped))
            _skills_span.set_attribute("skills.selection_mode", _selection_mode)
            if skipped:
                _skills_span.set_attribute(
                    "skills.skipped_details", json.dumps(skipped)
                )

    # Format a one-line notice for any skipped skills (issue #35).  Empty
    # string when no skills were skipped — no change to the phase summary.
    _skip_notice = skills.format_skipped_notice(skipped)

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
        summary = f"agent error: {exc}"
        if _skip_notice:
            summary = f"{summary}\n\n{_skip_notice}"
        return AgentOutcome(summary=summary, files_changed=False)

    if not text:
        log.warning("run_agent: empty final response — agent produced no output")
        summary = "agent produced no output"
        if _skip_notice:
            summary = f"{summary}\n\n{_skip_notice}"
        return AgentOutcome(summary=summary, files_changed=False)

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

    if spec.phase == "diagnosis":
        diag = structured_extractor(text, DiagnosisOutput)
        structured = diag.model_dump()
    else:
        structured = None
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
    "ci_fix": "ci_fix.md",
    "answer": "answer.md",
    "pr_comment": "pr_comment.md",
    "remediation": "remediation.md",
    "code_quality_scan": "code_quality_scan.md",
    "code_quality_improve": "code_quality_improve.md",
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
            "TRIGGERING_ISSUE": str(spec.issue_number),
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
    if spec.phase == "ci_fix":
        failures = spec.extra.get("ci_check_failures", []) or []
        lines = []
        for f in failures:
            name = f.get("name", "unknown check")
            conclusion = f.get("conclusion", "")
            summary = f.get("summary", "")
            details_url = f.get("details_url", "")
            line = f"- **{name}** ({conclusion or 'failing'})"
            if summary:
                line += f": {summary}"
            if details_url:
                line += f" — {details_url}"
            lines.append(line)
        return {
            "BRANCH": spec.branch,
            "SOURCE_BRANCH": base,
            "CI_CHECK_FAILURES": "\n".join(lines) or "- (no failure details provided)",
        }
    if spec.phase == "answer":
        return {
            "BRANCH": spec.branch,
            "SOURCE_BRANCH": base,
            "QUESTION": spec.extra.get("question", ""),
        }
    if spec.phase == "pr_comment":
        source = spec.extra.get("source", "comment")
        return {
            "BRANCH": spec.branch,
            "SOURCE_BRANCH": base,
            "PR_DIFF": spec.extra.get("pr_diff", "") or "(no diff available)",
            "COMMENT_BODY": spec.extra.get("comment_body", ""),
            "FEEDBACK_SOURCE": "a PR review" if source == "review" else "a PR comment",
            "FEEDBACK_AUTHOR": spec.extra.get("author", "the reviewer"),
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
    if spec.phase == "remediation":
        return {
            "BRANCH": spec.branch,
            "CI_CHECK_FAILURES": spec.extra.get("ci_check_failures", "none"),
        }
    if spec.phase == "code_quality_scan":
        return {
            "THRESHOLD": str(spec.extra.get("threshold", 7000)),
            "DEFAULT_BRANCH": os.environ.get("DEFAULT_BRANCH", "main"),
        }
    if spec.phase == "code_quality_improve":
        return {
            "SENTRUX_REPORT": spec.extra.get("sentrux_report", ""),
            "PARENT_ISSUE_NUMBER": str(spec.extra.get("parent_issue_number", 0)),
            "AGENT_LABEL": spec.extra.get("agent_label", "agent-ready"),
        }
    return {}


def build_agent_message(spec: TaskSpec) -> str:
    """Build the prompt sent to the agent for this phase.

    plan / execute / review / merge / diagnosis / ci_fix / answer / pr_comment
    render the bundled prompt templates (the diagnosis template asks for a
    structured ``<diagnosis>`` JSON block so the Alert Response remediation
    phase gets executable actions; the ci_fix template targets minimal changes
    that turn failing CI checks green — see ``Phase.CI_FIX`` / ``_ci_fix_loop``;
    the answer template asks a fresh agent to investigate a paused agent's
    mid-run question with branch access and return its best-informed
    decision — see ``Phase.ANSWER`` / ``_answer_questions``; the pr_comment
    template asks the agent to make targeted changes responding to reviewer
    feedback on an open PR and summarize them with the commit SHA — see
    ``Phase.PR_COMMENT`` / ``PRCommentWorkflow``).
    """
    if spec.phase in _PROMPT_FILES:
        return load_prompt(_PROMPT_FILES[spec.phase], _prompt_variables(spec))
    return spec.instructions


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
    plan_model = structured_extractor(outcome.summary, PlanOutput)
    plan = plan_model.model_dump()
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
    """Review phase: comment-only analysis with structured verdict.

    The agent analyses the diff and posts findings. No commits are made — the
    branch history after Review contains zero new commits. Returns a verdict
    (lgtm / needs_fixes / needs_human) via structured_extractor so the
    workflow can act on it.
    """
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    with tracer.start_as_current_span("clone"):
        clone_repo(os.environ["GITHUB_URL"], spec.branch, workdir)
    with tracer.start_as_current_span("install_deps"):
        install_deps(workdir)

    outcome = run_agent(spec, workdir, tracer)
    review_model = structured_extractor(outcome.summary, ReviewOutput)
    review = review_model.model_dump()
    return AgentJobResult(
        status="complete",
        issue_number=spec.issue_number,
        branch=spec.branch,
        commits=0,
        review=review,
        summary=outcome.summary,
    ).to_payload()


def handle_answer(spec: TaskSpec, tracer) -> dict:
    """Phase.ANSWER (#77): a fresh agent investigates a paused agent's mid-run
    clarifying question with read access to the working branch and returns the
    best-informed answer — no human interaction required.

    The agent makes no commits; it only investigates and decides. Its decision
    (the ``ANSWER:`` line, or the full response as a fallback) is returned in
    ``AgentJobResult.summary`` and patched back into the paused job by the
    workflow's ``_answer_questions``."""
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    base = os.environ.get("DEFAULT_BRANCH", "main")
    branch = spec.branch or base

    with tracer.start_as_current_span("clone"):
        clone_repo(os.environ["GITHUB_URL"], branch, workdir)
    with tracer.start_as_current_span("install_deps"):
        install_deps(workdir)

    outcome = run_agent(spec, workdir, tracer)
    answer = _extract_answer(outcome.summary)
    return AgentJobResult(
        status="complete",
        issue_number=spec.issue_number,
        branch=branch,
        summary=answer,
    ).to_payload()


def handle_ci_fix(spec: TaskSpec, tracer) -> dict:
    """Phase.CI_FIX (#76): make minimal targeted changes to turn failing CI
    checks green. Runs on the existing branch; any pushed commits are picked
    up by the workflow's `_ci_fix_loop`, which re-polls CI and re-dispatches
    up to `ci_fix_max_iterations` until checks pass or attempts are exhausted."""
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
            [
                "git",
                "commit",
                "-m",
                f"ci_fix: address failing checks on #{spec.issue_number}",
            ],
            cwd=workdir,
        )

    fixes = _commit_count(workdir, base_sha)
    if fixes:
        with tracer.start_as_current_span("push"):
            push_branch(workdir, spec.branch, force=True)
    return AgentJobResult(
        status="complete",
        issue_number=spec.issue_number,
        branch=spec.branch,
        commits=fixes,
        summary=outcome.summary,
    ).to_payload()


def handle_pr_comment(spec: TaskSpec, tracer) -> dict:
    """Phase.PR_COMMENT (#78): respond to reviewer feedback on an open agent PR.

    Given the PR diff and the comment/review body, the agent makes targeted
    changes on the existing branch, commits, and pushes — mirroring
    ``handle_ci_fix``'s clone → run_agent → commit → push shape. Any pushed
    commits are picked up by ``PRCommentWorkflow``'s CI fix loop.

    The diff is fetched here (via ``gh pr diff``), not threaded through
    ``TASK_SPEC`` — see ``_fetch_pr_diff`` for why a large diff in an env var
    crashes the container outright.

    Note: the agent does **not** post the GitHub reply itself — that happens
    via the workflow's ``post_github_comment`` activity once this Job
    completes, using ``AgentJobResult.summary`` (which the prompt asks the
    agent to end with, referencing the commit SHA it pushed)."""
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    with tracer.start_as_current_span("clone"):
        clone_repo(os.environ["GITHUB_URL"], spec.branch, workdir)
    with tracer.start_as_current_span("install_deps"):
        install_deps(workdir)

    pr_number = int(spec.extra.get("pr_number", 0) or 0)
    spec.extra["pr_diff"] = _fetch_pr_diff(
        repo_slug(os.environ["GITHUB_URL"]), pr_number
    )

    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=workdir).strip()
    outcome = run_agent(spec, workdir, tracer)

    _run(["git", "add", "-A"], cwd=workdir)
    if _run(["git", "status", "--porcelain"], cwd=workdir).strip():
        _run(
            [
                "git",
                "commit",
                "-m",
                f"pr_comment: address feedback on #{spec.issue_number}",
            ],
            cwd=workdir,
        )

    fixes = _commit_count(workdir, base_sha)
    if fixes:
        with tracer.start_as_current_span("push"):
            push_branch(workdir, spec.branch, force=True)
    return AgentJobResult(
        status="complete",
        issue_number=spec.issue_number,
        branch=spec.branch,
        commits=fixes,
        summary=outcome.summary,
    ).to_payload()


def handle_merge(spec: TaskSpec, tracer) -> dict:
    """Merge phase (PR-review model): open a *review* PR for each approved branch
    instead of merging it into the default branch directly.

    The Merge gate already approved that this work should go up for
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
    human-approval gate) run them. Falls back to a label-only diagnosis with no actions
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


def handle_remediation(spec: TaskSpec, tracer) -> dict:
    """Remediation phase: diagnose and fix failing CI checks on the PR branch.

    Clones the branch, runs the agent with the remediation prompt (which includes
    the CI check failure output), commits any fixes, and pushes back.  Returns
    the number of commits produced so the workflow can decide whether the
    remediation succeeded.
    """
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    github_url = os.environ["GITHUB_URL"]
    branch = spec.branch

    with tracer.start_as_current_span("clone"):
        clone_repo(github_url, branch, workdir)
    with tracer.start_as_current_span("install_deps"):
        install_deps(workdir)

    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=workdir).strip()
    outcome = run_agent(spec, workdir, tracer)

    # Sweep up any uncommitted changes the agent left behind.
    _run(["git", "add", "-A"], cwd=workdir)
    if _run(["git", "status", "--porcelain"], cwd=workdir).strip():
        _run(
            [
                "git",
                "commit",
                "-m",
                f"fix: remediate CI checks for #{spec.issue_number}",
            ],
            cwd=workdir,
        )

    commits = _commit_count(workdir, base_sha)
    if commits > 0:
        with tracer.start_as_current_span("push"):
            push_branch(workdir, branch, force=True)

    return AgentJobResult(
        status="complete",
        issue_number=spec.issue_number,
        branch=branch,
        commits=commits,
        summary=outcome.summary,
    ).to_payload()


def handle_code_quality_scan(spec: TaskSpec, tracer) -> dict:
    """Code quality scan phase: run sentrux and return structured result."""
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    base = os.environ.get("DEFAULT_BRANCH", "main")
    with tracer.start_as_current_span("clone"):
        clone_repo(os.environ["GITHUB_URL"], base, workdir)
    outcome = run_agent(spec, workdir, tracer)
    result = structured_extractor(outcome.summary, CodeQualityScanResult)
    # Detect abort: no rules.toml found
    if "rules.toml" in outcome.summary.lower() and (
        "not found" in outcome.summary.lower() or "missing" in outcome.summary.lower()
    ):
        result.scan_error = True
        result.error_message = outcome.summary
    return AgentJobResult(
        status="complete",
        summary=outcome.summary,
        plan=result.model_dump(),
    ).to_payload()


def handle_code_quality_improve(spec: TaskSpec, tracer) -> dict:
    """Code quality improve phase: file improvement issues using improve-codebase-architecture + to-issues skills."""
    workdir = os.getenv("WORKDIR", "/workspace/repo")
    base = os.environ.get("DEFAULT_BRANCH", "main")
    with tracer.start_as_current_span("clone"):
        clone_repo(os.environ["GITHUB_URL"], base, workdir)
    outcome = run_agent(spec, workdir, tracer)
    return AgentJobResult(
        status="complete",
        summary=outcome.summary,
    ).to_payload()


_HANDLERS = {
    "plan": handle_plan,
    "execute": handle_execute,
    "review": handle_review,
    "ci_fix": handle_ci_fix,
    "merge": handle_merge,
    "diagnosis": handle_diagnosis,
    "answer": handle_answer,
    "pr_comment": handle_pr_comment,
    "remediation": handle_remediation,
    "code_quality_scan": handle_code_quality_scan,
    "code_quality_improve": handle_code_quality_improve,
}


# --------------------------------------------------------------------------- #
# ConfigMap skill installation (issue #34)
# --------------------------------------------------------------------------- #

SKILLS_STAGING_DIR_DEFAULT = "/etc/agent-skills/staging"


def _install_configmap_skills() -> None:
    """Install ConfigMap-delivered skills into the convergence directory.

    Reads ``AGENT_SKILLS_CONFIGMAP`` to determine whether skills are available.
    When set, copies staged files from ``AGENT_SKILLS_STAGING_DIR`` (or the
    default path) into the skills convergence directory. Best-effort: a
    failure for one skill is logged and skipped, never failing the phase
    (ADR-0008).
    """
    configmap_name = os.environ.get("AGENT_SKILLS_CONFIGMAP")
    if not configmap_name:
        return

    staging_dir = os.environ.get("AGENT_SKILLS_STAGING_DIR", SKILLS_STAGING_DIR_DEFAULT)
    installed = skills.install_configmap_skills(staging_dir)
    if installed:
        log.info(
            "installed %d ConfigMap skill(s) from %s: %s",
            len(installed),
            staging_dir,
            ", ".join(installed),
        )


def main() -> int:
    tracer = setup_tracing()

    # Install ConfigMap-delivered skills before the agent phase runs
    # (ADR-0008: ConfigMap-wins precedence on name collision).
    _install_configmap_skills()

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
        write_output(
            AgentJobResult(status="failed", error=_describe_exception(exc)).to_payload()
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
