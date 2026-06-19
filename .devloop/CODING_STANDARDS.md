# Coding Standards — devloop

devloop is a Python project (the `omneval-devloop` package) plus a Helm chart and
four container images. Authoritative domain language lives in `CONTEXT.md` at the
repo root — read it before naming anything.

## Python tooling

- Always use [uv](https://github.com/astral-sh/uv). Never add `requirements.txt`.
- Dependencies live in `pyproject.toml`; commit `uv.lock`. Install/sync with
  `uv sync --all-groups`. Run anything project-scoped through `uv run` (e.g.
  `uv run pytest`, `uv run ruff check`).
- Target Python `>=3.12`. Use `from __future__ import annotations` and modern
  typing (`list[str]`, `dict | None`, `X | Y`) — no `typing.List`/`Optional`.
- Dependencies are version **floors** (`>=`), not exact pins — `omneval-devloop`
  is a library co-installed with `openhands-ai` in the agent images, so `==` pins
  cause resolution conflicts. Reproducible builds come from `uv.lock`, not pins.

## Python style

- Format and lint with `ruff` (`uv run ruff check --fix .`). Never submit
  code that fails ruff.
- Modules and functions are `snake_case`; classes are `PascalCase`; constants are
  `UPPER_SNAKE`. Prefer verbose, descriptive names over terse ones.
- Keep functions short. If one exceeds ~50 lines, look for a natural split.
- Use `logging` (module-level `log = logging.getLogger(__name__)`) for diagnostics
  and include context as structured fields. Do not use `print` in library code.
- Public functions touching external input (k8s API, GitHub, Temporal payloads)
  get a docstring stating the contract and at least one test.

## Error handling

- Raise specific exceptions; never swallow errors silently. Either handle or
  re-raise with context.
- The Agent Job output ConfigMap contract (`AgentJobResult.to_payload` /
  `from_payload`) is owned once in `devloop.shared` — both the worker and the
  agent base reference that single definition. Never duplicate the field set or
  key names (`result`, `human_answer`) elsewhere.

## Testing

- Test behaviour through public interfaces, not implementation details. A test
  that breaks on an internal rename was testing the wrong thing.
- Use `pytest` with `pytest-asyncio` for async workflow/activity code.
- Prefer hand-written fakes and injected seams (e.g. the `_loader` parameter on
  `resolve_skills`) over mocking the filesystem or network. Mock only true I/O
  boundaries you don't own.
- Every public function that handles external input must have at least one test.

When mocking your own code in tests, autospec it — never hand-write a stand-in.

If a test needs to replace an internal function, method, or class (via monkeypatch.setattr, unittest.mock.patch, etc.), derive the replacement from the real target with unittest.mock.create_autospec() — don't author a bespoke lambda, def fake_x(...), or ad-hoc class to stand in for it.

Why: a hand-written stand-in encodes whatever signature and sync/async-ness you assumed at the time you wrote the test — not the real one. When the real function's signature later changes (a new required arg, sync becomes async), the hand-written mock keeps "working" against the stale shape and the test keeps passing while production silently breaks. This exact failure mode shipped two broken releases: _client() became async def _client(cfg), but both the call sites and their test mocks (lambda cfg: fake_client) kept assuming the old sync, no-arg shape, so nothing caught the drift until it hit production.

How to apply:

```python
from unittest.mock import create_autospec
mock = create_autospec(real_module.real_function)
mock.return_value = fake_thing # or mock.side_effect = ...
monkeypatch.setattr("real_module.real_function", mock)
```
- create_autospec detects async def automatically and produces an AsyncMock — calling it with the wrong arity, or treating it as sync (forgetting await), fails the test immediately instead of silently passing.
- For a method on a class: create_autospec(RealClass, instance=True) or MagicMock(spec=RealClass).
- This rule is for mocking your own code. Mocking something genuinely external with no good importable spec (a raw HTTP response body, a third-party type without spec support) can still be a hand-written fake — the risk is specifically replacing an internal contract you control and could otherwise assert against directly.

## Helm chart (`charts/devloop`)

- Unit-test template changes with `helm unittest charts/devloop` (the
  helm-unittest plugin is baked into this image). Add or update a test under
  `charts/devloop/tests/` for any template behaviour change.
- Validate rendering and packaging with `helm lint` and `helm template` before
  committing. Keep `values.yaml` documented — every value gets a comment
  explaining its semantics (see the `skillsByPhase` three-way semantics block as
  the reference style).
- Chart `version`/`appVersion` are `0.0.0` placeholders; the release pipeline
  stamps the real semver from the git tag. Do not hand-bump them.

## Kubernetes / GitOps

- Image tags follow `sha-<7-char-hash>-<unix-epoch>` for main builds and semver
  for releases. The epoch lets Flux ImagePolicy select the newest build
  numerically.
- Agent Execution Jobs use Kubernetes Jobs + OpenHands `LocalWorkspace` — no
  Docker-in-Docker (ADR-0004). The pod is the isolation boundary.

## Comments

- Comment the **why**, not the what: a hidden constraint, a subtle invariant, or a
  workaround for a specific bug. Well-named identifiers describe the what.
- No TODO comments without a linked GitHub issue number.

## Architecture decisions

ADRs live in `docs/adr/`. Respect them — especially ADR-0006 (Dev Loop core is a
package, not a plugin), ADR-0007 (hand-rolled `build_agent` for skills injection),
and ADR-0008 (skills convergence directory with stage-and-install). If a change
contradicts an ADR, raise a `QUESTION:` rather than silently diverging.
