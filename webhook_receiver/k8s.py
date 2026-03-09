"""Kubernetes Job creation helper for the webhook receiver."""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from kubernetes import client, config  # type: ignore
from kubernetes.client.rest import ApiException  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

DEFAULT_TTL = 1800
DEFAULT_CPU_LIMIT = "4"
DEFAULT_MEM_LIMIT = "8Gi"
DEFAULT_CPU_REQUEST = "500m"
DEFAULT_MEM_REQUEST = "1Gi"
WORKSPACE_VOLUME = "workspace"


def _load_kube_config() -> None:
    """Load in-cluster config, falling back to local kubeconfig."""
    try:
        config.load_incluster_config()
    except config.config_exception.ConfigException:
        config.load_kube_config()


def make_job_name(project_id: int, note_id: int, task_kind: str) -> str:
    """Derive a deterministic, DNS-safe Job name.

    Args:
        project_id: GitLab project ID.
        note_id: The triggering comment/note ID.
        task_kind: One of review, fix_mr, fix_issue.

    Returns:
        A lowercase string of at most 63 characters suitable for a k8s name.
    """
    raw = f"{project_id}-{note_id}-{task_kind}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"crush-{digest}"


def create_job(
    *,
    namespace: str,
    job_name: str,
    image: str,
    env_vars: dict[str, str],
    ttl_seconds: int = DEFAULT_TTL,
    cpu_limit: Optional[str] = None,
    mem_limit: Optional[str] = None,
) -> client.V1Job:
    """Create a Kubernetes Job and return the created Job object.

    Raises:
        ApiException: if the API call fails for a reason other than conflict.
    """
    _load_kube_config()
    batch_v1 = client.BatchV1Api()

    cpu_lim = cpu_limit or os.environ.get("JOB_CPU_LIMIT", DEFAULT_CPU_LIMIT)
    mem_lim = mem_limit or os.environ.get("JOB_MEM_LIMIT", DEFAULT_MEM_LIMIT)

    # Build the env list, masking sensitive values from logs
    sensitive_keys = {"GITLAB_TOKEN", "CRUSH_API_KEY", "LLM_API_KEY", "WEBHOOK_SECRET"}
    k8s_env = []
    for key, value in env_vars.items():
        k8s_env.append(client.V1EnvVar(name=key, value=value))
        if key not in sensitive_keys:
            logger.debug("  Job env %s=%s", key, value)
        else:
            logger.debug("  Job env %s=<redacted>", key)

    container = client.V1Container(
        name="runner",
        image=image,
        command=["python", "/app/runner.py"],
        env=k8s_env,
        resources=client.V1ResourceRequirements(
            limits={"cpu": cpu_lim, "memory": mem_lim},
            requests={"cpu": DEFAULT_CPU_REQUEST, "memory": DEFAULT_MEM_REQUEST},
        ),
        volume_mounts=[
            client.V1VolumeMount(
                name=WORKSPACE_VOLUME,
                mount_path="/workspace",
            )
        ],
        security_context=client.V1SecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            allow_privilege_escalation=False,
        ),
    )

    workspace_volume = client.V1Volume(
        name=WORKSPACE_VOLUME,
        empty_dir=client.V1EmptyDirVolumeSource(),
    )

    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        containers=[container],
        volumes=[workspace_volume],
        service_account_name="crush-webhook",
    )

    pod_template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": "crush-runner"}),
        spec=pod_spec,
    )

    job_spec = client.V1JobSpec(
        template=pod_template,
        backoff_limit=6,
        ttl_seconds_after_finished=ttl_seconds,
    )

    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            labels={"app": "crush-runner"},
        ),
        spec=job_spec,
    )

    created = batch_v1.create_namespaced_job(namespace=namespace, body=job)
    logger.info("Created Job %s in namespace %s", job_name, namespace)
    return created


def job_exists(namespace: str, job_name: str) -> bool:
    """Return True if a Job with the given name already exists in namespace."""
    _load_kube_config()
    batch_v1 = client.BatchV1Api()
    try:
        batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
        return True
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise
