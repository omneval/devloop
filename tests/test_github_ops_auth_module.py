"""Test the new auth module (issue #160 extraction).

These tests exercise the auth module's public interface independently of any
REST operations.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from devloop.github_ops.auth import (
    GITHUB_API,
    get_installation_token,
    github_app_configured,
)


def _mock_auth_client(token: str = "ghs_mock_token") -> httpx.Client:
    """Create an httpx.Client that mocks the GitHub App install token minting."""

    def _send(request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        if "access_tokens" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "token": token,
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(_send))


class TestGitHubAppConfigured:
    """Tests for the github_app_configured() interface."""

    def setup_method(self) -> None:
        """Ensure auth env vars are cleared between tests."""
        for key in (
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_INSTALLATION_ID",
        ):
            os.environ.pop(key, None)

    def test_returns_false_when_nothing_configured(self) -> None:
        assert github_app_configured() is False

    def test_returns_false_when_only_id_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        assert github_app_configured() is False

    def test_raises_when_id_and_key_set_no_installation_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY are set but
        GITHUB_APP_INSTALLATION_ID is missing, github_app_configured()
        raises a clear RuntimeError (issue #89)."""
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key-pem")

        with pytest.raises(RuntimeError, match="GITHUB_APP_INSTALLATION_ID"):
            github_app_configured()

    def test_returns_true_when_all_three_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key-pem")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
        assert github_app_configured() is True


class TestGetInstallationToken:
    """Tests for the get_installation_token() -> str interface."""

    def setup_method(self) -> None:
        """Ensure auth env vars are cleared and cache is reset between tests."""
        from devloop.github_ops.auth import _reset_installation_token_cache

        for key in (
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_INSTALLATION_ID",
        ):
            os.environ.pop(key, None)
        _reset_installation_token_cache()

    async def test_returns_token_string_when_app_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_installation_token() returns a non-empty string when
        the GitHub App is configured."""

        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key-pem")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")

        fake_token = "ghs_fake_installation_token_123"

        captured: list[httpx.Request] = []

        def mock_send(
            request: httpx.Request, *args: Any, **kwargs: Any
        ) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"token": fake_token, "expires_at": "2099-01-01T00:00:00Z"},
            )

        client = httpx.Client(
            base_url=GITHUB_API,
            transport=httpx.MockTransport(mock_send),
        )

        # Patch both JWT generation (fake key can't be parsed) and the HTTP client
        monkeypatch.setattr(
            "devloop.github_ops.auth._generate_app_jwt",
            lambda: "fake-jwt-token",
        )
        monkeypatch.setattr(
            "devloop.github_ops.auth.auth_client",
            lambda: client,
        )

        token = await get_installation_token()
        assert isinstance(token, str)
        assert len(token) > 0
        assert token == fake_token
        assert len(captured) == 1
        assert captured[0].method == "POST"
        assert "access_tokens" in captured[0].url.path

    async def test_returns_cached_token_on_subsequent_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_installation_token() reuses the cached token on subsequent calls
        without making another HTTP request."""

        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key-pem")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")

        fake_token = "ghs_reused_token"

        call_count = 0

        def mock_send(
            request: httpx.Request, *args: Any, **kwargs: Any
        ) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={"token": fake_token, "expires_at": "2099-01-01T00:00:00Z"},
            )

        client = httpx.Client(
            base_url=GITHUB_API,
            transport=httpx.MockTransport(mock_send),
        )

        monkeypatch.setattr(
            "devloop.github_ops.auth._generate_app_jwt",
            lambda: "fake-jwt-token",
        )
        monkeypatch.setattr(
            "devloop.github_ops.auth.auth_client",
            lambda: client,
        )

        first = await get_installation_token()
        second = await get_installation_token()

        assert first == second == fake_token
        assert call_count == 1  # Only one HTTP call

    async def test_raises_key_error_when_installation_id_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY are set but
        GITHUB_APP_INSTALLATION_ID is missing, get_installation_token()
        raises KeyError (the env var lookup fails directly)."""
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key-pem")
        monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

        with pytest.raises(KeyError):
            await get_installation_token()

    async def test_raises_key_error_when_no_auth_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no GitHub App is configured, get_installation_token() raises
        a clear error."""
        monkeypatch.delenv("GITHUB_APP_ID", raising=False)

        with pytest.raises(KeyError):
            await get_installation_token()
