"""Test that operations can accept a fake token parameter (issue #160).

These tests verify that ``_client()``, ``_resolve_token()``, and
``_async_resolve()`` accept an optional ``token`` parameter so that
the operations module can be tested with a fake token string
without needing to monkeypatch auth internals.
"""

from __future__ import annotations

import httpx
import pytest

from devloop import github_ops
from devloop.github_ops import create_github_issue
from devloop.github import CreateGithubIssueInput
from devloop.projects import ProjectConfig, _REGISTRY

_PROJECT = ProjectConfig(
    id="omneval",
    github_url="https://github.com/omneval/omneval",
    default_branch="main",
    agent_image="img",
    agent_label="agent-ready",
    omneval_ingest_secret="s",
    github_token_secret="omneval-agent-github-token",
)


@pytest.fixture(autouse=True)
def _registry():
    _REGISTRY.clear()
    _REGISTRY["omneval"] = _PROJECT
    yield
    _REGISTRY.clear()


class FakeResp:
    def __init__(self, data: dict = {}, status: int = 200) -> None:
        self._data = data
        self.status_code = status

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeClient:
    """A fake httpx.Client that returns configurable responses."""

    def __init__(self, post_return: dict | None = None) -> None:
        self._post_return = post_return or {"number": 42}
        self.posts: list[tuple[str, dict | None]] = []
        self.headers: dict = {}
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *a: object) -> None:
        return False

    def post(self, url: str, json: dict | None = None) -> FakeResp:
        self.posts.append((url, json))
        return FakeResp(self._post_return)


def _async_client_factory(make_client):  # noqa: ANN201
    """Wrap a synchronous fake-client factory for the async ``_client`` shim."""

    async def _fake_client(cfg, extra_headers=None, token=None):  # noqa: ANN201, ARG001
        return make_client()

    return _fake_client


class TestResolveTokenTokenParam:
    """Tests for _resolve_token() accepting an optional token parameter."""

    async def test_resolve_token_returns_passed_token_directly(self) -> None:
        """When ``token`` is passed to ``_resolve_token()``, it is returned
        without calling auth or cluster."""
        result = await github_ops._resolve_token(_PROJECT, token="super-secret")
        assert result == "super-secret"

    async def test_resolve_token_skips_auth_when_token_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When token is provided, auth is never called even if GitHub App
        is configured."""
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")

        result = await github_ops._resolve_token(_PROJECT, token="bypassed")
        assert result == "bypassed"


class TestAsyncResolveTokenParam:
    """Tests for _async_resolve() accepting an optional token parameter."""

    async def test_async_resolve_returns_passed_token_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``token`` is passed to ``_async_resolve()``, it is returned
        without calling auth or cluster."""
        from devloop.github_ops import operations

        result = await operations._async_resolve(_PROJECT, token="super-secret")
        assert result == "super-secret"

    async def test_async_resolve_skips_auth_when_token_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with GitHub App configured, a passed token bypasses auth."""
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")

        from devloop.github_ops import operations

        result = await operations._async_resolve(_PROJECT, token="bypassed")
        assert result == "bypassed"


class TestClientTokenParam:
    """Tests for _client() accepting an optional token parameter."""

    async def test_client_accepts_token_param_uses_it_for_auth_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``token`` is passed to ``_client()``, it is used in the
        Authorization header instead of resolving auth."""
        captured: dict[str, str | None] = {}

        class ClientMaker:
            def __call__(self) -> httpx.Client:  # noqa: ANN204
                token = captured.get("token") or ""
                return httpx.Client(
                    base_url="https://api.github.com",
                    headers={"Authorization": f"Bearer {token}"},
                )

        maker = ClientMaker()

        def fake_client_factory(
            cfg,  # noqa: ANN201
            extra_headers=None,  # noqa: ANN001, ARG001
            token=None,  # noqa: ANN001
        ):
            captured["token"] = str(token) if token else None
            return maker()

        monkeypatch.setattr(
            github_ops,
            "_client",
            _async_client_factory(
                lambda: fake_client_factory(_PROJECT, token="my-fake-token")
            ),
        )

        result = await github_ops._client(_PROJECT, token="my-fake-token")
        assert isinstance(result, httpx.Client)

    async def test_client_factory_passes_token_to_inner_func(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The _client shim forwards the token keyword to _resolve_token."""
        token_log: list[str | None] = []

        async def record_client(cfg, extra_headers=None, token=None):  # noqa: ANN201, ANN001
            token_log.append(token)
            import httpx

            return httpx.Client(
                base_url="https://api.github.com",
                headers={"Authorization": "Bearer test"},
            )

        monkeypatch.setattr(github_ops, "_client", record_client)

        await github_ops._client(_PROJECT, token="explicit")
        assert token_log == ["explicit"]


class TestCreateGithubIssueWithToken:
    """Tests verifying create_github_issue works when token is threaded through."""

    async def test_create_github_issue_works_with_token_param(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_github_issue should work end-to-end when the _client shim
        is replaced with a fake."""

        def make_fake() -> FakeClient:
            return FakeClient(post_return={"number": 99})

        monkeypatch.setattr(
            github_ops,
            "_client",
            _async_client_factory(make_fake),
        )

        from temporalio.testing import ActivityEnvironment

        result = await ActivityEnvironment().run(
            create_github_issue,
            CreateGithubIssueInput(
                project_id="omneval",
                title="Test issue",
                body="Body",
                labels=["bug"],
            ),
        )
        assert result == 99


class TestOperationsNoAuthInternalAccess:
    """Tests verifying the operations module makes no direct calls to auth."""

    async def test_operations_uses_client_not_auth_internal_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operations module should route through _client/_resolve_token,
        never touching auth._installation_token_cache or auth._installation_token_lock."""
        from devloop.github_ops import operations

        assert not hasattr(operations, "_installation_token_cache")
        assert not hasattr(operations, "_installation_token_lock")

        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")

        result = await operations._async_resolve(_PROJECT, token="test")
        assert result == "test"
