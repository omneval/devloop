"""Agent runner seam (issue #121, ADR-0011).

``entrypoint.run_agent`` is the single seam every test mocks, but the harness
that actually drives the model used to be hard-wired inside it (OpenHands
SDK). This module formalizes the runner contract so the harness is selectable
purely via config:

* A **runner** is selected by ``resolve_runner()`` from the ``AGENT_RUNNER``
  env var (default ``openhands``; per-project override via the Project
  Registry's ``agent_runner`` field, which the worker forwards as this env).
* ``runner.start(spec, workdir, skills=..., build_agent=..., llm_setting=...)``
  returns a **session**; ``session.send(message) -> str`` runs one agent turn
  over the cloned workspace and returns the final assistant text. Subsequent
  ``send`` calls continue the *same* conversation — that is what the
  ``QUESTION:`` → park → resume round-trip in ``run_agent`` relies on.

Everything else — phase handlers, prompt rendering, the ``QUESTION:`` /
``ANSWER:`` / ``<promise>COMPLETE</promise>`` sentinels, commit/push logic,
structured extraction — is harness-portable and stays in ``entrypoint.py``.

``build_agent`` and ``llm_setting`` are injected by the caller rather than
imported: the entrypoint runs as ``__main__`` in the image (so this module
cannot import it back), and ``entrypoint.build_agent`` is a documented
override seam (ADR-0007) that derived images monkeypatch — injection at call
time picks the override up.

Capability matrix (see docs/adr/0011-pluggable-agent-runner.md):

| Runner | Skills injection | Condenser | Mid-run resume | Model endpoint |
|---|---|---|---|---|
| ``openhands`` (default) | yes (AgentContext) | yes | in-process conversation | any OpenAI-compatible |
| ``claude-agent-sdk`` | not yet (ignored, logged) | n/a (SDK-managed context) | session-id resume | Anthropic (API key / Bedrock / Vertex) |
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

DEFAULT_RUNNER = "openhands"


# --------------------------------------------------------------------------- #
# OpenHands (default — existing behavior, moved verbatim from run_agent)
# --------------------------------------------------------------------------- #
class _OpenHandsSession:
    def __init__(self, conversation):
        self._conversation = conversation

    def send(self, message: str) -> str:
        from openhands.sdk.conversation import get_agent_final_response

        self._conversation.send_message(message)
        self._conversation.run()
        return get_agent_final_response(self._conversation.state.events)


class OpenHandsRunner:
    name = "openhands"
    supports_skills = True

    def start(self, spec, workdir: str, *, skills=None, build_agent, llm_setting):
        """Construct LLM → Agent → LocalConversation exactly as run_agent did.

        ``build_agent`` is the (possibly image-overridden, ADR-0007) factory
        from the entrypoint; ``llm_setting`` is the per-role env resolver.
        """
        from openhands.sdk import LLM, AgentContext, LocalConversation

        # Construct AgentContext only when skills are available. Empty → None
        # → agent behaves as before issue #32 (no-op path).
        # load_public_skills=False prevents fetching public skills off GitHub
        # at Job runtime.
        agent_context = (
            AgentContext(skills=skills, load_public_skills=False) if skills else None
        )
        # The Review phase resolves the "review" role so a different model can
        # check the implementer's work (cross-model review); all other phases
        # use the base execute model.
        llm_role = "review" if spec.phase == "review" else ""
        llm = LLM(
            model=llm_setting("AGENT_MODEL", llm_role, "qwen3-27b"),
            base_url=llm_setting(
                "AGENT_LLM_BASE_URL", llm_role, "http://192.168.68.104/v1"
            ),
            api_key=llm_setting("AGENT_LLM_API_KEY", llm_role, "local"),
        )
        agent = build_agent(llm=llm, cli_mode=True, agent_context=agent_context)
        conversation = LocalConversation(agent=agent, workspace=workdir)
        return _OpenHandsSession(conversation)


# --------------------------------------------------------------------------- #
# Claude Agent SDK (issue #121) — Anthropic models driven by their native
# harness. Requires the `claude-agent-sdk` package and the Claude Code CLI in
# the agent image (a derived image; agent-base does not bundle them).
# --------------------------------------------------------------------------- #
class _ClaudeSession:
    def __init__(self, workdir: str, model: str, api_key: str):
        self._workdir = workdir
        self._model = model
        self._api_key = api_key
        # The SDK has no long-lived in-process conversation object the way
        # OpenHands does; multi-turn continuity uses the session id from the
        # previous turn's ResultMessage via options.resume.
        self._session_id: str | None = None

    def send(self, message: str) -> str:
        import asyncio

        return asyncio.run(self._turn(message))

    async def _turn(self, message: str) -> str:
        from claude_agent_sdk import ClaudeAgentOptions, query

        kwargs: dict = {
            "cwd": self._workdir,
            # The pod is the isolation boundary (ADR-0004) — same trust model
            # as the OpenHands LocalWorkspace path.
            "permission_mode": "bypassPermissions",
        }
        if self._model:
            kwargs["model"] = self._model
        if self._api_key:
            kwargs["env"] = {"ANTHROPIC_API_KEY": self._api_key}
        if self._session_id:
            kwargs["resume"] = self._session_id

        result_text = ""
        async for msg in query(prompt=message, options=ClaudeAgentOptions(**kwargs)):
            session_id = getattr(msg, "session_id", None)
            if session_id:
                self._session_id = session_id
            if type(msg).__name__ == "ResultMessage":
                result_text = getattr(msg, "result", "") or ""
        return result_text


class ClaudeAgentSdkRunner:
    name = "claude-agent-sdk"
    supports_skills = False

    def start(self, spec, workdir: str, *, skills=None, build_agent, llm_setting):
        if skills:
            log.warning(
                "claude-agent-sdk runner does not inject Agent Skills yet — "
                "%d resolved skill(s) ignored",
                len(skills),
            )
        llm_role = "review" if spec.phase == "review" else ""
        return _ClaudeSession(
            workdir=workdir,
            model=llm_setting("AGENT_MODEL", llm_role, "") or "",
            api_key=llm_setting("AGENT_LLM_API_KEY", llm_role, "") or "",
        )


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
_RUNNERS = {
    OpenHandsRunner.name: OpenHandsRunner,
    ClaudeAgentSdkRunner.name: ClaudeAgentSdkRunner,
}


def resolve_runner(name: str = ""):
    """Instantiate the configured runner.

    Explicit *name* wins (tests); otherwise ``AGENT_RUNNER`` env (set by the
    worker from the project's ``agent_runner`` registry field or the
    deployment-wide Helm value); otherwise the OpenHands default. An unknown
    name fails loudly — silently falling back would make an A/B run lie.
    """
    selected = (name or os.getenv("AGENT_RUNNER", "") or DEFAULT_RUNNER).strip()
    cls = _RUNNERS.get(selected)
    if cls is None:
        raise RuntimeError(
            f"unknown AGENT_RUNNER {selected!r} — known runners: "
            f"{', '.join(sorted(_RUNNERS))}"
        )
    return cls()
