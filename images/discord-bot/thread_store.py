"""ConfigMap-backed store for workflow_id <-> Discord thread_id mappings.

Both directions are stored in a single ConfigMap under two JSON-encoded keys
so the bot can recover the full mapping after a pod restart.
"""

import json
import logging
import os

from kubernetes import client, config

log = logging.getLogger(__name__)

_NAMESPACE = os.getenv("K8S_NAMESPACE", "agents")
_CONFIGMAP_NAME = os.getenv("CONFIGMAP_NAME", "discord-thread-map")


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


def put(workflow_id: str, thread_id: str) -> None:
    w2t, t2w = _load()
    w2t[workflow_id] = thread_id
    t2w[thread_id] = workflow_id
    _save(w2t, t2w)
    log.info("stored mapping workflow=%s thread=%s", workflow_id, thread_id)


def get_thread(workflow_id: str) -> str | None:
    w2t, _ = _load()
    return w2t.get(workflow_id)


def get_workflow(thread_id: str) -> str | None:
    _, t2w = _load()
    return t2w.get(thread_id)


def delete(workflow_id: str) -> None:
    w2t, t2w = _load()
    thread_id = w2t.pop(workflow_id, None)
    if thread_id:
        t2w.pop(thread_id, None)
    _save(w2t, t2w)
    log.info("removed mapping workflow=%s", workflow_id)
