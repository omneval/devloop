"""ConfigMap-backed thread ↔ workflow mapping (issue #29).

Provides durable storage for ``workflow_id ↔ thread_id`` mappings so that
messaging activity can resolve a thread after a bot pod restart.

Usage (per-platform):
    from devloop.messaging.thread_store import ConfigMapThreadStore

    store = ConfigMapThreadStore(configmap_name="my-bot-threads")
    store.put("wf-001", "thread-abc")
    tid = store.get_thread("wf-001")
"""

from __future__ import annotations

import json
import logging

from kubernetes import client as k8s_client

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_W2T_KEY = "workflow-to-thread"
_T2W_KEY = "thread-to-workflow"


def _v1() -> k8s_client.CoreV1Api:
    """Lazily create a Kubernetes CoreV1Api client.

    Uses in-cluster config by default (service account token mounted into the
    pod).  Falls back to kubeconfig file when running locally.
    """
    try:
        k8s_client.config.load_incluster_config()
    except k8s_client.ConfigException:
        k8s_client.config.load_kube_config()
    return k8s_client.CoreV1Api()


# --------------------------------------------------------------------------- #
# ConfigMapThreadStore
# --------------------------------------------------------------------------- #


class ConfigMapThreadStore:
    """Kubernetes ConfigMap-backed bidirectional workflow↔thread mapping.

    The ConfigMap stores two JSON keys::

        {
            "workflow-to-thread": {"wf-001": "thread-abc"},
            "thread-to-workflow": {"thread-abc": "wf-001"},
        }

    Both directions are stored explicitly for O(1) lookups.
    """

    def __init__(
        self,
        configmap_name: str = "bot-threads",
        namespace: str = "default",
    ) -> None:
        self._name = configmap_name
        self._namespace = namespace

    # -- public API ---------------------------------------------------------

    def put(self, workflow_id: str, thread_id: str, channel: str = "") -> None:
        """Store or update the mapping *workflow_id → thread_id*.

        Optionally stores *channel* for platforms that need it (Slack).
        """
        api = _v1()
        w2t, t2w = self._read_configmap(api)
        w2t[workflow_id] = thread_id
        t2w[thread_id] = workflow_id
        if channel:
            self._store_channel(thread_id, channel)
        self._patch_configmap(api, w2t, t2w)
        log.info("stored %s → %s", workflow_id, thread_id)

    def get_thread(self, workflow_id: str) -> str | None:
        """Return the thread_id for *workflow_id*, or ``None``."""
        w2t, _ = self._read_configmap(_v1())
        return w2t.get(workflow_id)

    def get_workflow(self, thread_id: str) -> str | None:
        """Return the workflow_id that owns *thread_id*, or ``None``."""
        _, t2w = self._read_configmap(_v1())
        return t2w.get(thread_id)

    def get_channel(self, thread_id: str) -> str | None:
        """Return the channel for *thread_id*, or ``None``."""
        try:
            cm = _v1().read_namespaced_config_map(self._name, self._namespace)
            channels_raw = cm.data.get("channels", "{}")
            channels: dict[str, str] = json.loads(channels_raw)
            return channels.get(thread_id)
        except k8s_client.ApiException:
            return None

    def delete(self, workflow_id: str) -> None:
        """Remove the mapping for *workflow_id*."""
        api = _v1()
        w2t, t2w = self._read_configmap(api)
        thread_id = w2t.pop(workflow_id, None)
        if thread_id:
            t2w.pop(thread_id, None)
        self._patch_configmap(api, w2t, t2w)
        log.info("deleted %s", workflow_id)

    # -- internal helpers ---------------------------------------------------

    def _read_configmap(
        self, api: k8s_client.CoreV1Api
    ) -> tuple[dict, dict]:
        """Read the ConfigMap and return (w2t, t2w) dicts."""
        try:
            cm = api.read_namespaced_config_map(self._name, self._namespace)
            data = cm.data or {}
            w2t = json.loads(data.get(_W2T_KEY, "{}"))
            t2w = json.loads(data.get(_T2W_KEY, "{}"))
            return w2t, t2w
        except k8s_client.ApiException as exc:
            if exc.status == 404:
                return {}, {}
            raise

    def _patch_configmap(
        self, api: k8s_client.CoreV1Api, w2t: dict, t2w: dict
    ) -> None:
        """Atomically patch the ConfigMap with *w2t* and *t2w*."""
        body = {
            "data": {
                _W2T_KEY: json.dumps(w2t),
                _T2W_KEY: json.dumps(t2w),
            }
        }
        api.patch_namespaced_config_map(self._name, self._namespace, body)

    def _store_channel(self, thread_id: str, channel: str) -> None:
        """Store the channel for *thread_id* in a separate key."""
        api = _v1()
        try:
            cm = api.read_namespaced_config_map(self._name, self._namespace)
            channels_raw = cm.data.get("channels", "{}")
            channels: dict[str, str] = json.loads(channels_raw)
        except k8s_client.ApiException:
            channels = {}
        channels[thread_id] = channel
        body = {"data": {"channels": json.dumps(channels)}}
        api.patch_namespaced_config_map(self._name, self._namespace, body)
