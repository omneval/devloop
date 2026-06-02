"""ConfigMap-backed store for workflow_id <-> Slack thread_ts mappings.

Both directions are stored in a single ConfigMap under two JSON-encoded keys
so the bot can recover the full mapping after a pod restart.

Mirrors the Discord bot thread_store pattern but uses Slack's ``thread_ts``
value (a string like ``1677536646.000200``) instead of Discord's integer
channel IDs.
"""

import json
import logging
import os

from kubernetes import client, config

log = logging.getLogger(__name__)

_NAMESPACE = os.getenv("K8S_NAMESPACE", "agents")
_CONFIGMAP_NAME = os.getenv("CONFIGMAP_NAME", "slack-thread-map")


def _v1() -> client.CoreV1Api:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


def _load() -> tuple[dict[str, str], dict[str, str]]:
    """Return (workflow_to_thread, thread_to_workflow) dicts."""
    v1 = _v1()
    cm = v1.read_namespaced_config_map(_CONFIGMAP_NAME, _NAMESPACE)
    data = cm.data or {}
    w2t = json.loads(data.get("workflow-to-thread", "{}"))
    t2w = json.loads(data.get("thread-to-workflow", "{}"))
    return w2t, t2w


def _save(w2t: dict[str, str], t2w: dict[str, str]) -> None:
    # The ConfigMap is pre-created by configmap-rbac.yaml; the SA only has
    # get/update/patch (not create). Use patch to avoid replace conflicts.
    v1 = _v1()
    v1.patch_namespaced_config_map(
        _CONFIGMAP_NAME,
        _NAMESPACE,
        {
            "data": {
                "workflow-to-thread": json.dumps(w2t),
                "thread-to-workflow": json.dumps(t2w),
            }
        },
    )


def put(workflow_id: str, thread_ts: str) -> None:
    w2t, t2w = _load()
    w2t[workflow_id] = thread_ts
    t2w[thread_ts] = workflow_id
    _save(w2t, t2w)
    log.info("stored mapping workflow=%s thread=%s", workflow_id, thread_ts)


def get_thread(workflow_id: str) -> str | None:
    w2t, _ = _load()
    return w2t.get(workflow_id)


def get_workflow(thread_ts: str) -> str | None:
    _, t2w = _load()
    return t2w.get(thread_ts)


def delete(workflow_id: str) -> None:
    w2t, t2w = _load()
    thread_ts = w2t.pop(workflow_id, None)
    if thread_ts:
        t2w.pop(thread_ts, None)
    _save(w2t, t2w)
    log.info("removed mapping workflow=%s", workflow_id)
