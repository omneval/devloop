"""GitHub App authentication (issue #160).

Encapsulates the GitHub App token flow behind one interface:
``get_installation_token() -> str``.  Process-wide token cache and lock
live inside this module so callers never touch internal state directly.

Also provides ``github_app_configured() -> bool`` and ``auth_client() ->
httpx.Client`` for callers that need to verify auth configuration or
need an unauthenticated client for pre-authentication HTTP (e.g. minting
tokens).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")

_TOKEN_REFRESH_SKEW_SECONDS = 5 * 60

# Process-wide cache for the current installation access token: (token, expiry).
_installation_token_cache: dict[str, object] = {"token": None, "expires_at": None}

# Serializes the check-mint-store sequence in ``get_installation_token``.
_installation_token_lock = asyncio.Lock()


# ── Public interface ─────────────────────────────────────────────────────


def github_app_configured() -> bool:
    """True when devloop should authenticate as a GitHub App rather than a PAT.

    Both ``GITHUB_APP_ID`` and ``GITHUB_APP_PRIVATE_KEY`` must be set — a
    partially-configured app is treated as "not configured" so devloop falls
    back to PAT rather than failing outright.

    Once those two are present, ``GITHUB_APP_INSTALLATION_ID`` becomes
    required too — without it ``get_installation_token`` would dereference a
    missing env var and raise an opaque ``KeyError``. We raise a clear,
    actionable error here, at configuration-detection time, instead.
    """
    has_id = bool(os.getenv("GITHUB_APP_ID"))
    has_key = bool(os.getenv("GITHUB_APP_PRIVATE_KEY"))
    if not (has_id and has_key):
        return False
    if not os.getenv("GITHUB_APP_INSTALLATION_ID"):
        raise RuntimeError(
            "GitHub App authentication is misconfigured: GITHUB_APP_ID and "
            "GITHUB_APP_PRIVATE_KEY are set but GITHUB_APP_INSTALLATION_ID is "
            "missing"
        )
    return True


async def get_installation_token() -> str:
    """Return a valid installation access token, minting (and caching) a new
    one when the cached token is missing or within 5 minutes of expiring.

    Flow (GitHub App → installation token):
      1. Sign a JWT with the app's RSA private key.
      2. ``POST /app/installations/{installation_id}/access_tokens`` using
         that JWT as the bearer credential — off the event loop, since it's
         a blocking HTTP round trip.
      3. Cache the returned token alongside its ``expires_at`` and reuse it
         until we're within the refresh skew window.

    A process-wide ``asyncio.Lock`` serializes the check-mint-store sequence:
    concurrent callers race the first (lock-free) freshness check, but only
    one proceeds to mint — the rest block on the lock and, after acquiring
    it, find the cache already refreshed by the winner and reuse it (the
    "double-check" requirement).
    """
    cached = _cached_token_if_fresh()
    if cached is not None:
        return cached

    async with _installation_token_lock:
        cached = _cached_token_if_fresh()
        if cached is not None:
            return cached

        installation_id = os.environ["GITHUB_APP_INSTALLATION_ID"]
        app_jwt = _generate_app_jwt()
        token, expires_at = await asyncio.to_thread(
            _mint_installation_token, installation_id, app_jwt
        )

        _installation_token_cache["token"] = token
        _installation_token_cache["expires_at"] = expires_at
        log.info(
            "minted GitHub App installation token (installation %s, expires %s)",
            installation_id,
            expires_at.isoformat(),
        )
        return token


def auth_client() -> object:
    """Build an unauthenticated httpx client for pre-auth HTTP calls.

    Factored out so callers (e.g. mint logic) can substitute a fake transport
    without reaching across the network.
    """
    import httpx

    return httpx.Client(base_url=GITHUB_API, timeout=30.0)


# ── Test seam ────────────────────────────────────────────────────────────


def _reset_installation_token_cache() -> None:
    """Test seam: clear the process-wide installation-token cache."""
    _installation_token_cache["token"] = None  # type: ignore[assignment]
    _installation_token_cache["expires_at"] = None  # type: ignore[assignment]


# ── Private helpers (below this line are implementation details) ──────────


def _cached_token_if_fresh() -> str | None:
    """Return the cached token if it has more than the refresh-skew window
    left, else ``None``."""
    cached_token = _installation_token_cache["token"]
    cached_expiry = _installation_token_cache["expires_at"]
    if cached_token and cached_expiry is not None:
        remaining = (cached_expiry - datetime.now(timezone.utc)).total_seconds()  # type: ignore[operator]
        if remaining > _TOKEN_REFRESH_SKEW_SECONDS:
            return cached_token  # type: ignore[return-value]
    return None


def _generate_app_jwt() -> str:
    """Build the short-lived JWT GitHub Apps use to authenticate as themselves.

    Per GitHub's App authentication docs: RS256-signed, ``iss`` is the App ID,
    ``iat`` is set 60s in the past to tolerate clock drift between devloop and
    GitHub's servers, and ``exp`` is capped at GitHub's 10-minute maximum (we
    use a conservative 9 minutes). This JWT is itself only used to mint
    installation access tokens — it is never sent on regular API calls.
    """
    import jwt as pyjwt

    app_id = os.environ["GITHUB_APP_ID"]
    private_key = os.environ["GITHUB_APP_PRIVATE_KEY"]
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (9 * 60),
        "iss": app_id,
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


def _parse_github_timestamp(value: str) -> datetime:
    """Parse a GitHub API timestamp (``2024-01-01T00:00:00Z``) into an aware
    UTC ``datetime``."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _mint_installation_token(
    installation_id: str, app_jwt: str
) -> tuple[str, datetime]:
    """Blocking HTTP round trip that exchanges an app JWT for an installation
    access token. Run off the event loop via ``asyncio.to_thread`` — this is
    the only network I/O in the mint flow, and it's a regular blocking
    ``httpx.Client`` call."""
    with auth_client() as c:
        resp = c.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return data["token"], _parse_github_timestamp(data["expires_at"])
