"""Tests for GitHub App authentication (issue #81).

devloop-bot can authenticate to GitHub either via a fine-grained PAT
(``GITHUB_TOKEN`` / ``github_token_secret`` — the existing path) or via a
GitHub App installation token (new). GitHub Apps mint short-lived (1h)
installation tokens from a JWT signed with the app's RSA private key; the
token is cached and refreshed 5 minutes before it expires.

TDD: written before the implementation; should fail first.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from devloop import github_ops
from devloop.projects import ProjectConfig, _REGISTRY


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _generate_private_key_pem() -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key


_PRIVATE_KEY_PEM, _PRIVATE_KEY = _generate_private_key_pem()

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


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """The installation-token cache is process-global; reset it around tests."""
    github_ops.auth._reset_installation_token_cache()
    yield
    github_ops.auth._reset_installation_token_cache()


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _PRIVATE_KEY_PEM)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "987654")
    yield


class FakePostResp:
    def __init__(self, data, status=201):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeAppHTTPClient:
    """Captures the request made to mint an installation token."""

    instances: list["FakeAppHTTPClient"] = []

    def __init__(self, *args, token_response=None, **kwargs):
        self.requests: list[tuple[str, str, dict | None]] = []
        self._token_response = token_response or {
            "token": "ghs_fake_installation_token",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        FakeAppHTTPClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self.requests.append((url, (headers or {}).get("Authorization", ""), json))
        return FakePostResp(self._token_response)


@pytest.fixture(autouse=True)
def _clear_fake_client_instances():
    FakeAppHTTPClient.instances.clear()
    yield
    FakeAppHTTPClient.instances.clear()


# --------------------------------------------------------------------------- #
# Auth-mode selection
# --------------------------------------------------------------------------- #
def test_uses_github_app_when_app_id_and_key_set(app_env):
    assert github_ops.auth.github_app_configured() is True


def test_falls_back_to_pat_when_app_env_not_set(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    assert github_ops.auth.github_app_configured() is False


def test_falls_back_to_pat_when_only_app_id_set(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    assert github_ops.auth.github_app_configured() is False


def test_falls_back_to_pat_when_only_private_key_set(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _PRIVATE_KEY_PEM)
    assert github_ops.auth.github_app_configured() is False


def test_raises_clear_error_when_installation_id_missing(monkeypatch):
    """issue #89: ID + key without an installation ID must raise a clear,
    actionable error rather than a raw KeyError surfacing later."""
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _PRIVATE_KEY_PEM)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        github_ops.auth.github_app_configured()

    message = str(exc_info.value)
    assert "GITHUB_APP_INSTALLATION_ID" in message
    assert "GITHUB_APP_ID" in message
    assert "GITHUB_APP_PRIVATE_KEY" in message
    assert not isinstance(exc_info.value, KeyError)


# --------------------------------------------------------------------------- #
# JWT generation
# --------------------------------------------------------------------------- #
def test_app_jwt_is_signed_with_private_key_and_has_expected_claims(app_env):
    token = github_ops.auth._generate_app_jwt()

    # Decoding with the *public* key proves it was signed with the matching
    # private key (RS256, per GitHub App auth requirements).
    public_key = _PRIVATE_KEY.public_key()
    decoded = jwt.decode(token, key=public_key, algorithms=["RS256"])

    assert decoded["iss"] == "123456"
    # iat should be slightly in the past (GitHub recommends -60s for clock drift)
    assert decoded["iat"] <= int(time.time())
    # exp must be within GitHub's allowed max of 10 minutes
    assert decoded["exp"] - decoded["iat"] <= 600
    assert decoded["exp"] > decoded["iat"]


# --------------------------------------------------------------------------- #
# Installation token generation + caching
# --------------------------------------------------------------------------- #
async def test_installation_token_is_fetched_via_post(app_env, monkeypatch):
    monkeypatch.setattr(
        github_ops.auth,
        "auth_client",
        lambda: FakeAppHTTPClient(),
    )

    token = await github_ops.auth.get_installation_token()

    assert token == "ghs_fake_installation_token"
    assert len(FakeAppHTTPClient.instances) == 1
    client = FakeAppHTTPClient.instances[0]
    assert len(client.requests) == 1
    url, auth_header, body = client.requests[0]
    assert url == "/app/installations/987654/access_tokens"
    assert auth_header.startswith("Bearer ")

    # The bearer token used to mint the installation token is the signed app JWT
    app_jwt = auth_header.removeprefix("Bearer ")
    public_key = _PRIVATE_KEY.public_key()
    decoded = jwt.decode(app_jwt, key=public_key, algorithms=["RS256"])
    assert decoded["iss"] == "123456"


async def test_installation_token_is_cached_between_calls(app_env, monkeypatch):
    monkeypatch.setattr(
        github_ops.auth,
        "auth_client",
        lambda: FakeAppHTTPClient(),
    )

    first = await github_ops.auth.get_installation_token()
    second = await github_ops.auth.get_installation_token()

    assert first == second == "ghs_fake_installation_token"
    # Only one POST should have been made — the second call reused the cache.
    assert len(FakeAppHTTPClient.instances) == 1
    assert len(FakeAppHTTPClient.instances[0].requests) == 1


async def test_installation_token_is_refreshed_five_minutes_before_expiry(
    app_env, monkeypatch
):
    # First token expires in 4 minutes — within the 5-minute refresh window —
    # so the very next call must mint a fresh one rather than reuse the cache.
    soon_expiring = {
        "token": "ghs_almost_expired",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=4)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    fresh = {
        "token": "ghs_fresh_token",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    responses = iter([soon_expiring, fresh])
    monkeypatch.setattr(
        github_ops.auth,
        "auth_client",
        lambda: FakeAppHTTPClient(token_response=next(responses)),
    )

    first = await github_ops.auth.get_installation_token()
    second = await github_ops.auth.get_installation_token()

    assert first == "ghs_almost_expired"
    assert second == "ghs_fresh_token"
    assert len(FakeAppHTTPClient.instances) == 2


async def test_installation_token_reused_when_comfortably_within_expiry(
    app_env, monkeypatch
):
    # Token still has 30 minutes left — well outside the 5-minute refresh
    # window — so it must be reused, not regenerated.
    long_lived = {
        "token": "ghs_long_lived",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    monkeypatch.setattr(
        github_ops.auth,
        "auth_client",
        lambda: FakeAppHTTPClient(token_response=long_lived),
    )

    for _ in range(5):
        assert await github_ops.auth.get_installation_token() == "ghs_long_lived"

    assert len(FakeAppHTTPClient.instances) == 1
    assert len(FakeAppHTTPClient.instances[0].requests) == 1


async def test_concurrent_calls_mint_token_only_once(app_env, monkeypatch):
    """issue #86: two concurrent get_installation_token calls against an
    empty cache must not both mint a fresh token. The lock serializes the
    check-mint-store sequence — the loser blocks on the lock (acquired by
    the winner *before* the blocking mint call) and, after a double-check,
    reuses the winner's freshly-minted token instead of minting again."""
    monkeypatch.setattr(github_ops.auth, "auth_client", lambda: FakeAppHTTPClient())

    first, second = await asyncio.gather(
        github_ops.auth.get_installation_token(),
        github_ops.auth.get_installation_token(),
    )

    assert first == second == "ghs_fake_installation_token"
    assert len(FakeAppHTTPClient.instances) == 1
    assert len(FakeAppHTTPClient.instances[0].requests) == 1
    assert (
        github_ops.auth._installation_token_cache["token"]
        == "ghs_fake_installation_token"
    )
    assert github_ops.auth._installation_token_cache["expires_at"] is not None


# --------------------------------------------------------------------------- #
# Token resolution dispatcher (GitHub App vs PAT)
# --------------------------------------------------------------------------- #
async def test_resolve_token_uses_github_app_when_configured(app_env, monkeypatch):
    monkeypatch.setattr(
        github_ops.auth,
        "auth_client",
        lambda: FakeAppHTTPClient(),
    )
    # Even though the project carries a github_token_secret, GitHub App auth
    # takes priority when configured.
    monkeypatch.setattr(
        github_ops.operations.cluster,
        "read_secret_value",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("PAT path should not run")
        ),
    )

    token = await github_ops._resolve_token(_PROJECT)

    assert token == "ghs_fake_installation_token"


async def test_resolve_token_falls_back_to_pat_when_app_not_configured(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(
        github_ops.operations.cluster,
        "read_secret_value",
        lambda name, key, **kw: "pat-token-value",
    )

    token = await github_ops._resolve_token(_PROJECT)

    assert token == "pat-token-value"


async def test_resolve_token_uses_env_when_secret_name_empty(monkeypatch):
    """Local quickstart (issue #116): a registry entry with an empty
    github_token_secret resolves the worker's own GITHUB_TOKEN env var instead
    of reaching for the Kubernetes API (which doesn't exist locally)."""
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "gho_local_cli_token")
    monkeypatch.setattr(
        github_ops.operations.cluster,
        "read_secret_value",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("K8s Secret path should not run for an empty name")
        ),
    )
    local_project = ProjectConfig(
        id="local",
        github_url="https://github.com/omneval/omneval",
        default_branch="main",
        agent_image="img",
        agent_label="agent-ready",
        omneval_ingest_secret="s",
        github_token_secret="",
    )

    token = await github_ops._resolve_token(local_project)

    assert token == "gho_local_cli_token"


# --------------------------------------------------------------------------- #
# Backward compatibility: _client() still builds a working PAT-authed client
# --------------------------------------------------------------------------- #
async def test_client_uses_pat_when_github_app_not_configured(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    captured = {}

    def fake_read_secret_value(name, key, **kw):
        captured["name"] = name
        captured["key"] = key
        return "pat-token-value"

    monkeypatch.setattr(
        github_ops.operations.cluster, "read_secret_value", fake_read_secret_value
    )

    client = await github_ops._client(_PROJECT)
    try:
        assert client.headers["authorization"] == "Bearer pat-token-value"
    finally:
        client.close()

    assert captured == {"name": "omneval-agent-github-token", "key": "GITHUB_TOKEN"}


async def test_client_uses_installation_token_when_github_app_configured(
    app_env, monkeypatch
):
    monkeypatch.setattr(
        github_ops.auth,
        "auth_client",
        lambda: FakeAppHTTPClient(),
    )
    monkeypatch.setattr(
        github_ops.operations.cluster,
        "read_secret_value",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("PAT path should not run")
        ),
    )

    client = await github_ops._client(_PROJECT)
    try:
        assert client.headers["authorization"] == "Bearer ghs_fake_installation_token"
    finally:
        client.close()
