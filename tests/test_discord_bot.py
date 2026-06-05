"""Tests for discord_bot env-var-driven thread store initialization.

discord.py is an optional dependency and requires a real gateway to do anything
useful.  These tests validate only the environment-variable-driven factory
function, mocking the discord module so the test suite runs without the extra
install.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _import_discord_bot():
    """Import devloop.messaging.discord_bot with a stubbed discord module."""
    # Stub discord and its sub-namespaces used at module level
    stub = types.ModuleType("discord")
    stub.Client = MagicMock
    stub.Intents = MagicMock()
    stub.Intents.default = MagicMock(return_value=MagicMock())
    stub.Thread = object
    stub.Message = object
    stub.TextChannel = object
    stub.ChannelType = MagicMock()

    sys.modules.setdefault("discord", stub)

    import importlib
    import devloop.messaging.discord_bot as mod
    importlib.reload(mod)
    return mod


_db = _import_discord_bot()


# --------------------------------------------------------------------------- #
# Tracer bullet: CONFIGMAP_NAME env var is respected
# --------------------------------------------------------------------------- #


def test_discord_thread_store_reads_configmap_name_from_env(monkeypatch):
    monkeypatch.setenv("CONFIGMAP_NAME", "my-custom-map")
    store = _db._make_thread_store()
    assert store._name == "my-custom-map"


# --------------------------------------------------------------------------- #
# K8S_NAMESPACE env var is respected
# --------------------------------------------------------------------------- #


def test_discord_thread_store_reads_namespace_from_env(monkeypatch):
    monkeypatch.setenv("K8S_NAMESPACE", "my-namespace")
    store = _db._make_thread_store()
    assert store._namespace == "my-namespace"


# --------------------------------------------------------------------------- #
# Defaults when env vars are absent
# --------------------------------------------------------------------------- #


def test_discord_thread_store_defaults_namespace_to_agents(monkeypatch):
    monkeypatch.delenv("K8S_NAMESPACE", raising=False)
    store = _db._make_thread_store()
    assert store._namespace == "agents"
