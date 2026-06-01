"""Tests for ConfigMap-backed thread_store durability (issue #33).

Verifies that workflow_id <-> thread_id mappings survive bot restarts by
mocking the kubernetes CoreV1Api with a fake whose backing dict persists
across "restart" simulations (a new fake client instance seeded with the
same data dict).

Run with:
    cd images/discord-bot
    uv run --with pytest --with kubernetes pytest -q
"""

import json
import types

import pytest

import thread_store


# ---------------------------------------------------------------------------
# Fake kubernetes infrastructure
# ---------------------------------------------------------------------------

def _make_configmap(data: dict):
    """Return a SimpleNamespace that looks like a V1ConfigMap with .data."""
    return types.SimpleNamespace(data=data)


class FakeCoreV1Api:
    """Fake kubernetes CoreV1Api backed by a shared mutable dict.

    The same ``backing`` dict can be shared between multiple FakeCoreV1Api
    instances to simulate a ConfigMap that outlives a bot pod restart.
    """

    def __init__(self, backing: dict | None = None):
        # backing["cm_data"] holds the ConfigMap's .data dict
        self._backing = backing if backing is not None else {}
        if "cm_data" not in self._backing:
            self._backing["cm_data"] = {}

    def read_namespaced_config_map(self, name: str, namespace: str):
        return _make_configmap(dict(self._backing["cm_data"]))

    def patch_namespaced_config_map(self, name: str, namespace: str, body: dict):
        self._backing["cm_data"].update(body.get("data", {}))


def _patch_v1(monkeypatch, fake_api: FakeCoreV1Api):
    """Patch thread_store._v1 to return *fake_api* instead of hitting a cluster."""
    monkeypatch.setattr(thread_store, "_v1", lambda: fake_api)


# ---------------------------------------------------------------------------
# Tracer bullet: put + get_thread
# ---------------------------------------------------------------------------

def test_put_stores_mapping_and_get_thread_returns_it(monkeypatch):
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    thread_store.put("wf-001", "thread-aaa")

    assert thread_store.get_thread("wf-001") == "thread-aaa"


# ---------------------------------------------------------------------------
# get_workflow returns correct workflow_id
# ---------------------------------------------------------------------------

def test_get_workflow_returns_workflow_id_for_stored_thread(monkeypatch):
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    thread_store.put("wf-002", "thread-bbb")

    assert thread_store.get_workflow("thread-bbb") == "wf-002"


# ---------------------------------------------------------------------------
# Durability across restart: new fake client seeded with same backing dict
# ---------------------------------------------------------------------------

def test_mapping_survives_bot_restart(monkeypatch):
    """Simulate restart: discard the first fake client, seed a second with the
    same backing dict (the "ConfigMap data that survived in the cluster"), and
    assert the mapping is still resolvable.
    """
    backing = {}
    first_client = FakeCoreV1Api(backing)
    _patch_v1(monkeypatch, first_client)

    # Bot lifetime 1: store the mapping
    thread_store.put("wf-restart-1", "thread-restart-x")

    # --- Simulate pod restart: new FakeCoreV1Api over the SAME backing dict ---
    second_client = FakeCoreV1Api(backing)
    _patch_v1(monkeypatch, second_client)

    # Bot lifetime 2: mapping must still be resolvable
    assert thread_store.get_thread("wf-restart-1") == "thread-restart-x"
    assert thread_store.get_workflow("thread-restart-x") == "wf-restart-1"


# ---------------------------------------------------------------------------
# Pre-restart thread still signals correct workflow after restart
# ---------------------------------------------------------------------------

def test_reply_on_pre_restart_thread_signals_correct_workflow(monkeypatch):
    """A Discord reply arriving after a bot restart resolves to the right
    workflow_id so the Temporal signal is sent to the correct handle.
    """
    backing = {}

    first_client = FakeCoreV1Api(backing)
    _patch_v1(monkeypatch, first_client)
    thread_store.put("wf-phase-gate", "thread-phase-gate-99")

    # Restart
    second_client = FakeCoreV1Api(backing)
    _patch_v1(monkeypatch, second_client)

    # discord_client.on_message calls get_workflow(thread_id) to find the workflow
    resolved = thread_store.get_workflow("thread-phase-gate-99")
    assert resolved == "wf-phase-gate", (
        f"Expected 'wf-phase-gate', got {resolved!r} — reply would signal the wrong workflow"
    )


# ---------------------------------------------------------------------------
# Cross-talk: multiple mappings, each independent; deleting one leaves others
# ---------------------------------------------------------------------------

def test_multiple_mappings_no_cross_talk(monkeypatch):
    """Each thread_id maps only to its own workflow_id and vice-versa."""
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    thread_store.put("wf-A", "thread-A")
    thread_store.put("wf-B", "thread-B")
    thread_store.put("wf-C", "thread-C")

    assert thread_store.get_thread("wf-A") == "thread-A"
    assert thread_store.get_thread("wf-B") == "thread-B"
    assert thread_store.get_thread("wf-C") == "thread-C"

    assert thread_store.get_workflow("thread-A") == "wf-A"
    assert thread_store.get_workflow("thread-B") == "wf-B"
    assert thread_store.get_workflow("thread-C") == "wf-C"

    # No cross-mapping
    assert thread_store.get_workflow("thread-A") != "wf-B"
    assert thread_store.get_workflow("thread-B") != "wf-C"


def test_deleting_one_mapping_does_not_affect_others(monkeypatch):
    """Deleting workflow A's mapping leaves B and C intact."""
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    thread_store.put("wf-del-A", "thread-del-A")
    thread_store.put("wf-del-B", "thread-del-B")
    thread_store.put("wf-del-C", "thread-del-C")

    thread_store.delete("wf-del-A")

    # Deleted mapping is gone
    assert thread_store.get_thread("wf-del-A") is None
    assert thread_store.get_workflow("thread-del-A") is None

    # Other mappings are untouched
    assert thread_store.get_thread("wf-del-B") == "thread-del-B"
    assert thread_store.get_thread("wf-del-C") == "thread-del-C"
    assert thread_store.get_workflow("thread-del-B") == "wf-del-B"
    assert thread_store.get_workflow("thread-del-C") == "wf-del-C"


# ---------------------------------------------------------------------------
# Missing key returns None (not KeyError)
# ---------------------------------------------------------------------------

def test_get_thread_returns_none_for_unknown_workflow(monkeypatch):
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    assert thread_store.get_thread("no-such-workflow") is None


def test_get_workflow_returns_none_for_unknown_thread(monkeypatch):
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    assert thread_store.get_workflow("no-such-thread") is None
