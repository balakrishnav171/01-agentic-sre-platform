"""
MCP Server: Kubernetes operations.
Provides 6 tools: get_pod_status, get_pod_logs, restart_pod,
scale_deployment, get_node_status, get_events.
"""
from __future__ import annotations
import os
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from kubernetes import client, config as k8s_config
from loguru import logger
import uvicorn

app = FastAPI(title="Kubernetes MCP Server", version="1.0.0")


def get_k8s_client():
    """Load Kubernetes configuration and return API clients."""
    try:
        k8s_config.load_incluster_config()
        logger.debug("Loaded in-cluster Kubernetes config")
    except Exception:
        k8s_config.load_kube_config()
        logger.debug("Loaded kubeconfig from local filesystem")
    return client.CoreV1Api(), client.AppsV1Api()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class PodRequest(BaseModel):
    namespace: str
    pod_name: str


class ScaleRequest(BaseModel):
    namespace: str
    deployment: str
    replicas: int


class LogRequest(BaseModel):
    namespace: str
    pod_name: str
    lines: int = 100


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "kubernetes-mcp"}


# ---------------------------------------------------------------------------
# Tool: get_pod_status
# ---------------------------------------------------------------------------

@app.post("/tools/get_pod_status")
def get_pod_status(req: PodRequest) -> Dict[str, Any]:
    """Get current status and conditions of a pod."""
    v1, _ = get_k8s_client()
    try:
        pod = v1.read_namespaced_pod(name=req.pod_name, namespace=req.namespace)
        containers = [
            {
                "name": c.name,
                "ready": c.ready,
                "restart_count": c.restart_count,
                "state": str(c.state),
            }
            for c in (pod.status.container_statuses or [])
        ]
        conditions = [
            {"type": c.type, "status": c.status}
            for c in (pod.status.conditions or [])
        ]
        logger.info(f"Fetched status for pod {req.pod_name} in {req.namespace}")
        return {
            "pod_name": req.pod_name,
            "namespace": req.namespace,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "containers": containers,
            "conditions": conditions,
        }
    except Exception as e:
        logger.error(f"get_pod_status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_pod_logs
# ---------------------------------------------------------------------------

@app.post("/tools/get_pod_logs")
def get_pod_logs(req: LogRequest) -> Dict[str, Any]:
    """Retrieve recent logs from a pod."""
    v1, _ = get_k8s_client()
    try:
        logs = v1.read_namespaced_pod_log(
            name=req.pod_name,
            namespace=req.namespace,
            tail_lines=req.lines,
        )
        logger.info(f"Fetched {req.lines} log lines for pod {req.pod_name} in {req.namespace}")
        return {"pod_name": req.pod_name, "logs": logs, "lines": req.lines}
    except Exception as e:
        logger.error(f"get_pod_logs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: restart_pod
# ---------------------------------------------------------------------------

@app.post("/tools/restart_pod")
def restart_pod(req: PodRequest) -> Dict[str, Any]:
    """Delete pod to trigger a rolling restart (Deployment recreates it)."""
    v1, _ = get_k8s_client()
    try:
        v1.delete_namespaced_pod(name=req.pod_name, namespace=req.namespace)
        logger.info(f"Restarted pod {req.pod_name} in {req.namespace}")
        return {
            "status": "restarted",
            "pod_name": req.pod_name,
            "namespace": req.namespace,
        }
    except Exception as e:
        logger.error(f"restart_pod error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: scale_deployment
# ---------------------------------------------------------------------------

@app.post("/tools/scale_deployment")
def scale_deployment(req: ScaleRequest) -> Dict[str, Any]:
    """Scale a deployment to the desired number of replicas."""
    _, apps_v1 = get_k8s_client()
    try:
        body = {"spec": {"replicas": req.replicas}}
        apps_v1.patch_namespaced_deployment_scale(
            name=req.deployment,
            namespace=req.namespace,
            body=body,
        )
        logger.info(f"Scaled {req.deployment} in {req.namespace} to {req.replicas} replicas")
        return {
            "status": "scaled",
            "deployment": req.deployment,
            "namespace": req.namespace,
            "replicas": req.replicas,
        }
    except Exception as e:
        logger.error(f"scale_deployment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_node_status
# ---------------------------------------------------------------------------

@app.get("/tools/get_node_status")
def get_node_status() -> Dict[str, Any]:
    """Get status of all cluster nodes."""
    v1, _ = get_k8s_client()
    try:
        nodes = v1.list_node()
        result = []
        for n in nodes.items:
            conditions = {c.type: c.status for c in (n.status.conditions or [])}
            result.append(
                {
                    "name": n.metadata.name,
                    "ready": conditions.get("Ready", "Unknown"),
                    "roles": list(n.metadata.labels.keys()),
                    "capacity": {
                        "cpu": n.status.capacity.get("cpu"),
                        "memory": n.status.capacity.get("memory"),
                    },
                }
            )
        logger.info(f"Fetched status for {len(result)} nodes")
        return {"nodes": result, "total": len(result)}
    except Exception as e:
        logger.error(f"get_node_status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_events
# ---------------------------------------------------------------------------

@app.post("/tools/get_events")
def get_events(req: PodRequest) -> Dict[str, Any]:
    """Get recent cluster events in a namespace (up to 50 events)."""
    v1, _ = get_k8s_client()
    try:
        events = v1.list_namespaced_event(namespace=req.namespace, limit=50)
        result = [
            {
                "name": e.metadata.name,
                "reason": e.reason,
                "message": e.message,
                "type": e.type,
                "count": e.count,
                "last_time": str(e.last_timestamp),
            }
            for e in events.items
        ]
        logger.info(f"Fetched {len(result)} events in namespace {req.namespace}")
        return {"events": result, "namespace": req.namespace}
    except Exception as e:
        logger.error(f"get_events error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
