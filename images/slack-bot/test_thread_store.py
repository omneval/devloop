"""Tests for ConfigMap-backed thread_store durability.

Verifies that workflow_id <-> thread_ts mappings survive bot restarts by
mocking the kubernetes CoreV1Api with a fake whose backing dict persists
across "restart" simulations.

Mirrors the Discord bot test_thread_store.py pattern.

Run with:
    cd images/slack-bot
    uv run --with pytest --with kubernetes pytest -q
"""

import types

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

    thread_store.put("wf-001", "C0123:1677536646.000200")

    assert thread_store.get_thread("wf-001") == "C0123:1677536646.000200"


# ---------------------------------------------------------------------------
# get_workflow returns correct workflow_id
# ---------------------------------------------------------------------------


def test_get_workflow_returns_workflow_id_for_stored_thread(monkeypatch):
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    thread_store.put("wf-002", "C0456:1677536646.000300")

    assert thread_store.get_workflow("C0456:1677536646.000300") == "wf-002"


# ---------------------------------------------------------------------------
# Durability across restart: new fake client seeded with same backing dict
# ---------------------------------------------------------------------------


def test_mapping_survives_bot_restart(monkeypatch):
    """Simulate restart: discard the first fake client, seed a second with the
    same backing dict, and assert the mapping is still resolvable.
    """
    backing = {}
    first_client = FakeCoreV1Api(backing)
    _patch_v1(monkeypatch, first_client)

    thread_store.put("wf-restart-1", "C0789:1677536646.000400")

    second_client = FakeCoreV1Api(backing)
    _patch_v1(monkeypatch, second_client)

    assert thread_store.get_thread("wf-restart-1") == "C0789:1677536646.000400"
    assert thread_store.get_workflow("C0789:1677536646.000400") == "wf-restart-1"


# ---------------------------------------------------------------------------
# Pre-restart thread still signals correct workflow after restart
# ---------------------------------------------------------------------------


def test_reply_on_pre_restart_thread_signals_correct_workflow(monkeypatch):
    """A Slack reply arriving after a bot restart resolves to the right
    workflow_id so the Temporal signal is sent to the correct handle.
    """
    backing = {}

    first_client = FakeCoreV1Api(backing)
    _patch_v1(monkeypatch, first_client)
    thread_store.put("wf-phase-gate", "C0ABC:1677536646.000500")

    second_client = FakeCoreV1Api(backing)
    _patch_v1(monkeypatch, second_client)

    resolved = thread_store.get_workflow("C0ABC:1677536646.000500")
    assert resolved == "wf-phase-gate", (
        f"Expected 'wf-phase-gate', got {resolved!r}"
    )


# ---------------------------------------------------------------------------
# Cross-talk: multiple mappings, each independent; deleting one leaves others
# ---------------------------------------------------------------------------


def test_multiple_mappings_no_cross_talk(monkeypatch):
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    thread_store.put("wf-A", "C0AAA:1677536646.001")
    thread_store.put("wf-B", "C0BBB:1677536646.002")
    thread_store.put("wf-C", "C0CCC:1677536646.003")

    assert thread_store.get_thread("wf-A") == "C0AAA:1677536646.001"
    assert thread_store.get_thread("wf-B") == "C0BBB:1677536646.002"
    assert thread_store.get_thread("wf-C") == "C0CCC:1677536646.003"

    assert thread_store.get_workflow("C0AAA:1677536646.001") == "wf-A"
    assert thread_store.get_workflow("C0BBB:1677536646.002") == "wf-B"
    assert thread_store.get_workflow("C0CCC:1677536646.003") == "wf-C"


def test_deleting_one_mapping_does_not_affect_others(monkeypatch):
    fake = FakeCoreV1Api()
    _patch_v1(monkeypatch, fake)

    thread_store.put("wf-del-A", "C0DEL:1677536646.010")
    thread_store.put("wf-del-B", "C0DEL:1677536646.011")
    thread_store.put("wf-del-C", "C0DEL:1677536646.012")

    thread_store.delete("wf-del-A")

    assert thread_store.get_thread("wf-del-A") is None
    assert thread_store.get_thread("wf-del-B") == "C0DEL:1677536646.011"
    assert thread_store.get_thread("wf-del-C") == "C0DEL:1677536646.012"


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
