"""Unit tests for the cluster ConfigMap/Secret store seam (C1).

The fakes stand in for the kubernetes client so the helpers' parsing, 404
handling, and base64 decode are exercised without a cluster.
"""

import base64
import importlib
import os
import types

import pytest
from kubernetes.client.exceptions import ApiException

from devloop import cluster


def _ns(data):
    return types.SimpleNamespace(data=data)


class FakeCore:
    def __init__(self, *, configmaps=None, secrets=None):
        self._configmaps = configmaps or {}
        self._secrets = secrets or {}
        self.patched = []

    def read_namespaced_config_map(self, name, ns):
        if name not in self._configmaps:
            raise ApiException(status=404, reason="Not Found")
        return _ns(self._configmaps[name])

    def patch_namespaced_config_map(self, name, ns, body):
        self.patched.append((name, body))

    def read_namespaced_secret(self, name, ns):
        if name not in self._secrets:
            raise ApiException(status=404, reason="Not Found")
        return _ns(self._secrets[name])


@pytest.fixture
def fake(monkeypatch):
    core = FakeCore()
    monkeypatch.setattr(cluster, "core", lambda: core)
    return core


def test_read_configmap_data_returns_data(fake):
    fake._configmaps["cm"] = {"k": "v"}
    assert cluster.read_configmap_data("cm") == {"k": "v"}


def test_read_configmap_data_404_returns_none(fake):
    assert cluster.read_configmap_data("missing") is None


def test_read_configmap_data_non_404_propagates(monkeypatch):
    class Boom:
        def read_namespaced_config_map(self, name, ns):
            raise ApiException(status=500, reason="Server Error")

    monkeypatch.setattr(cluster, "core", lambda: Boom())
    with pytest.raises(ApiException):
        cluster.read_configmap_data("cm")


def test_patch_configmap_data_wraps_in_data_key(fake):
    cluster.patch_configmap_data("cm", {"last-sha": "abc"})
    assert fake.patched == [("cm", {"data": {"last-sha": "abc"}})]


def test_read_secret_value_base64_decodes(fake):
    fake._secrets["s"] = {"GITHUB_TOKEN": base64.b64encode(b"ghp_xyz").decode()}
    assert cluster.read_secret_value("s", "GITHUB_TOKEN") == "ghp_xyz"


def test_read_secret_value_missing_key_returns_empty(fake):
    fake._secrets["s"] = {}
    assert cluster.read_secret_value("s", "GITHUB_TOKEN") == ""


def test_data_helper_tolerates_dict_and_object():
    assert cluster._data({"data": {"a": 1}}) == {"a": 1}
    assert cluster._data(_ns({"a": 1})) == {"a": 1}
    assert cluster._data(_ns(None)) == {}


# --------------------------------------------------------------------------- #
# AGENTS_NAMESPACE (issue #33) — env var respected at module import time
# --------------------------------------------------------------------------- #

@pytest.fixture
def _reload_cluster():
    """Reload the cluster module before and after the test to ensure NAMESPACE
    reflects a fresh env-var read each time and later tests are not polluted."""
    importlib.reload(cluster)
    yield
    importlib.reload(cluster)


def test_agents_namespace_defaults_to_agents(_reload_cluster):
    """Without AGENTS_NAMESPACE set, NAMESPACE falls back to the literal 'agents'
    so local/dev runs without a chart still work."""
    os.environ.pop("AGENTS_NAMESPACE", None)
    importlib.reload(cluster)
    assert cluster.NAMESPACE == "agents"


def test_agents_namespace_env_var_is_respected(_reload_cluster):
    """When AGENTS_NAMESPACE is set, cluster.NAMESPACE must reflect that value so
    Jobs land in the operator-chosen namespace, not the hard-coded fallback."""
    os.environ["AGENTS_NAMESPACE"] = "my-agents-ns"
    importlib.reload(cluster)
    assert cluster.NAMESPACE == "my-agents-ns"
    os.environ.pop("AGENTS_NAMESPACE", None)
