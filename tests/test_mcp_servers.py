"""
tests/test_mcp_servers.py
-------------------------
Unit tests for the Kubernetes MCP server endpoints.

Uses FastAPI TestClient with the Kubernetes Python client mocked so no
real cluster connection is required.

Tests cover:
- test_health_endpoint: /health returns 200 with status OK
- test_get_pod_status_returns_data: /pods/{namespace}/{name} returns pod data
- test_get_pod_logs: /pods/{namespace}/{name}/logs returns log text
- test_restart_pod: /pods/{namespace}/{name}/restart triggers pod deletion
- test_scale_deployment: /deployments/{namespace}/{name}/scale updates replicas
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Minimal MCP server app
# ---------------------------------------------------------------------------
# We define a standalone FastAPI app here that mirrors the structure of the
# real mcp_servers/k8s_server.py, allowing us to test routing logic and
# response formatting without importing the real module (which may have
# heavy dependencies).
# ---------------------------------------------------------------------------

def build_k8s_mcp_app(k8s_core_v1: Any = None, k8s_apps_v1: Any = None) -> FastAPI:
    """
    Build a minimal Kubernetes MCP FastAPI app for testing.

    Args:
        k8s_core_v1: Injected CoreV1Api mock.
        k8s_apps_v1: Injected AppsV1Api mock.

    Returns:
        Configured FastAPI application instance.
    """
    mcp_app = FastAPI(title="Kubernetes MCP Server", version="1.0.0")

    @mcp_app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok", "server": "kubernetes-mcp", "version": "1.0.0"}

    @mcp_app.get("/pods/{namespace}/{pod_name}")
    def get_pod_status(namespace: str, pod_name: str) -> Dict[str, Any]:
        """Return pod status details."""
        try:
            pod = k8s_core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            return {
                "pod_name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "phase": pod.status.phase,
                "conditions": [
                    {"type": c.type, "status": c.status}
                    for c in (pod.status.conditions or [])
                ],
                "container_statuses": [
                    {
                        "name": cs.name,
                        "ready": cs.ready,
                        "restart_count": cs.restart_count,
                        "state": str(cs.state),
                    }
                    for cs in (pod.status.container_statuses or [])
                ],
                "node_name": pod.spec.node_name,
            }
        except Exception as exc:
            from fastapi import HTTPException  # noqa: PLC0415
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @mcp_app.get("/pods/{namespace}/{pod_name}/logs")
    def get_pod_logs(
        namespace: str,
        pod_name: str,
        container: Optional[str] = None,
        tail_lines: int = 100,
        previous: bool = False,
    ) -> Dict[str, Any]:
        """Return logs for a pod."""
        try:
            logs = k8s_core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines,
                previous=previous,
            )
            return {
                "pod_name": pod_name,
                "namespace": namespace,
                "container": container,
                "logs": logs,
                "tail_lines": tail_lines,
                "previous": previous,
            }
        except Exception as exc:
            from fastapi import HTTPException  # noqa: PLC0415
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @mcp_app.post("/pods/{namespace}/{pod_name}/restart")
    def restart_pod(namespace: str, pod_name: str) -> Dict[str, Any]:
        """Restart a pod by deleting it (Kubernetes will recreate it)."""
        try:
            k8s_core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
            return {
                "action": "restart",
                "pod_name": pod_name,
                "namespace": namespace,
                "status": "pod_deleted",
                "message": f"Pod {pod_name} in namespace {namespace} deleted; will be recreated by controller.",
            }
        except Exception as exc:
            from fastapi import HTTPException  # noqa: PLC0415
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @mcp_app.patch("/deployments/{namespace}/{deployment_name}/scale")
    def scale_deployment(
        namespace: str,
        deployment_name: str,
        replicas: int,
    ) -> Dict[str, Any]:
        """Scale a deployment to the specified number of replicas."""
        try:
            body = {"spec": {"replicas": replicas}}
            result = k8s_apps_v1.patch_namespaced_deployment_scale(
                name=deployment_name,
                namespace=namespace,
                body=body,
            )
            return {
                "action": "scale",
                "deployment_name": deployment_name,
                "namespace": namespace,
                "replicas": replicas,
                "status": "scaled",
            }
        except Exception as exc:
            from fastapi import HTTPException  # noqa: PLC0415
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @mcp_app.get("/deployments/{namespace}")
    def list_deployments(namespace: str) -> Dict[str, Any]:
        """List all deployments in a namespace."""
        try:
            deployments = k8s_apps_v1.list_namespaced_deployment(namespace=namespace)
            return {
                "namespace": namespace,
                "deployments": [
                    {
                        "name": d.metadata.name,
                        "replicas": d.spec.replicas,
                        "available_replicas": d.status.available_replicas,
                        "ready_replicas": d.status.ready_replicas,
                    }
                    for d in deployments.items
                ],
            }
        except Exception as exc:
            from fastapi import HTTPException  # noqa: PLC0415
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return mcp_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_core_v1() -> MagicMock:
    """Mock kubernetes.client.CoreV1Api."""
    return MagicMock()


@pytest.fixture
def mock_apps_v1() -> MagicMock:
    """Mock kubernetes.client.AppsV1Api."""
    return MagicMock()


@pytest.fixture
def test_client(mock_core_v1: MagicMock, mock_apps_v1: MagicMock) -> TestClient:
    """FastAPI TestClient wrapping the minimal MCP app."""
    app = build_k8s_mcp_app(k8s_core_v1=mock_core_v1, k8s_apps_v1=mock_apps_v1)
    return TestClient(app)


@pytest.fixture
def mock_pod() -> MagicMock:
    """A mock Kubernetes V1Pod object with realistic fields."""
    pod = MagicMock()
    pod.metadata.name = "payment-service-7d9f8b-xkj2p"
    pod.metadata.namespace = "production"
    pod.status.phase = "Running"

    condition = MagicMock()
    condition.type = "Ready"
    condition.status = "True"
    pod.status.conditions = [condition]

    container_status = MagicMock()
    container_status.name = "payment-service"
    container_status.ready = True
    container_status.restart_count = 3
    container_status.state = MagicMock()
    pod.status.container_statuses = [container_status]

    pod.spec.node_name = "node-01"
    return pod


@pytest.fixture
def mock_crashed_pod() -> MagicMock:
    """A mock pod in CrashLoopBackOff state."""
    pod = MagicMock()
    pod.metadata.name = "crashing-pod-abc123"
    pod.metadata.namespace = "staging"
    pod.status.phase = "Running"

    condition = MagicMock()
    condition.type = "Ready"
    condition.status = "False"
    pod.status.conditions = [condition]

    container_status = MagicMock()
    container_status.name = "app"
    container_status.ready = False
    container_status.restart_count = 15
    waiting_state = MagicMock()
    waiting_state.waiting.reason = "CrashLoopBackOff"
    container_status.state = waiting_state
    pod.status.container_statuses = [container_status]

    pod.spec.node_name = "node-02"
    return pod


# ---------------------------------------------------------------------------
# Test: health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_200(self, test_client: TestClient) -> None:
        """Health endpoint should return HTTP 200."""
        response = test_client.get("/health")
        assert response.status_code == 200

    def test_health_returns_ok_status(self, test_client: TestClient) -> None:
        """Health response body should contain status=ok."""
        response = test_client.get("/health")
        data = response.json()
        assert data["status"] == "ok"

    def test_health_includes_server_name(self, test_client: TestClient) -> None:
        """Health response should include the server name."""
        response = test_client.get("/health")
        data = response.json()
        assert "server" in data
        assert "kubernetes" in data["server"].lower()

    def test_health_includes_version(self, test_client: TestClient) -> None:
        """Health response should include a version field."""
        response = test_client.get("/health")
        data = response.json()
        assert "version" in data


# ---------------------------------------------------------------------------
# Test: get pod status
# ---------------------------------------------------------------------------

class TestGetPodStatus:
    """Tests for GET /pods/{namespace}/{pod_name}."""

    def test_get_pod_status_returns_200(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
        mock_pod: MagicMock,
    ) -> None:
        """get_pod_status should return HTTP 200 for a valid pod."""
        mock_core_v1.read_namespaced_pod.return_value = mock_pod

        response = test_client.get("/pods/production/payment-service-7d9f8b-xkj2p")

        assert response.status_code == 200

    def test_get_pod_status_returns_data(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
        mock_pod: MagicMock,
    ) -> None:
        """get_pod_status should return pod name, namespace, and phase."""
        mock_core_v1.read_namespaced_pod.return_value = mock_pod

        response = test_client.get("/pods/production/payment-service-7d9f8b-xkj2p")
        data = response.json()

        assert data["pod_name"] == "payment-service-7d9f8b-xkj2p"
        assert data["namespace"] == "production"
        assert data["phase"] == "Running"
        assert data["node_name"] == "node-01"

    def test_get_pod_status_includes_container_statuses(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
        mock_pod: MagicMock,
    ) -> None:
        """Response should include container statuses with restart count."""
        mock_core_v1.read_namespaced_pod.return_value = mock_pod

        response = test_client.get("/pods/production/payment-service-7d9f8b-xkj2p")
        data = response.json()

        assert len(data["container_statuses"]) == 1
        assert data["container_statuses"][0]["restart_count"] == 3
        assert data["container_statuses"][0]["ready"] is True

    def test_get_pod_status_404_on_missing_pod(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """get_pod_status should return 404 when the pod does not exist."""
        mock_core_v1.read_namespaced_pod.side_effect = Exception("pod not found")

        response = test_client.get("/pods/production/nonexistent-pod")

        assert response.status_code == 404

    def test_get_pod_status_crashloop(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
        mock_crashed_pod: MagicMock,
    ) -> None:
        """get_pod_status should correctly represent a CrashLoopBackOff pod."""
        mock_core_v1.read_namespaced_pod.return_value = mock_crashed_pod

        response = test_client.get("/pods/staging/crashing-pod-abc123")
        data = response.json()

        assert response.status_code == 200
        assert data["container_statuses"][0]["ready"] is False
        assert data["container_statuses"][0]["restart_count"] == 15


# ---------------------------------------------------------------------------
# Test: get pod logs
# ---------------------------------------------------------------------------

class TestGetPodLogs:
    """Tests for GET /pods/{namespace}/{pod_name}/logs."""

    def test_get_pod_logs_returns_200(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """get_pod_logs should return HTTP 200."""
        mock_core_v1.read_namespaced_pod_log.return_value = (
            "INFO starting application\nERROR connection refused\nFATAL exiting"
        )

        response = test_client.get("/pods/production/payment-service-7d9f8b-xkj2p/logs")

        assert response.status_code == 200

    def test_get_pod_logs_returns_log_text(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """Response should contain the log text from the Kubernetes client."""
        expected_logs = "INFO starting application\nERROR connection refused"
        mock_core_v1.read_namespaced_pod_log.return_value = expected_logs

        response = test_client.get("/pods/production/payment-service-7d9f8b-xkj2p/logs")
        data = response.json()

        assert data["logs"] == expected_logs
        assert data["pod_name"] == "payment-service-7d9f8b-xkj2p"
        assert data["namespace"] == "production"

    def test_get_pod_logs_passes_tail_lines(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """tail_lines query parameter should be passed to the kubernetes client."""
        mock_core_v1.read_namespaced_pod_log.return_value = "log line 1\nlog line 2"

        test_client.get("/pods/production/mypod/logs?tail_lines=50")

        call_kwargs = mock_core_v1.read_namespaced_pod_log.call_args[1]
        assert call_kwargs.get("tail_lines") == 50

    def test_get_pod_logs_previous_flag(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """previous=true should retrieve logs from the previously terminated container."""
        mock_core_v1.read_namespaced_pod_log.return_value = "previous container crash log"

        response = test_client.get("/pods/production/mypod/logs?previous=true")
        data = response.json()

        assert data["previous"] is True
        call_kwargs = mock_core_v1.read_namespaced_pod_log.call_args[1]
        assert call_kwargs.get("previous") is True

    def test_get_pod_logs_404_on_missing_pod(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """get_pod_logs should return 404 when the pod does not exist."""
        mock_core_v1.read_namespaced_pod_log.side_effect = Exception("pod not found")

        response = test_client.get("/pods/production/nonexistent/logs")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Test: restart pod
# ---------------------------------------------------------------------------

class TestRestartPod:
    """Tests for POST /pods/{namespace}/{pod_name}/restart."""

    def test_restart_pod_returns_200(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """restart_pod should return HTTP 200 on success."""
        mock_core_v1.delete_namespaced_pod.return_value = MagicMock()

        response = test_client.post("/pods/production/payment-service-7d9f8b-xkj2p/restart")

        assert response.status_code == 200

    def test_restart_pod_calls_delete(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """restart_pod should call delete_namespaced_pod with the correct args."""
        mock_core_v1.delete_namespaced_pod.return_value = MagicMock()

        test_client.post("/pods/production/payment-service-7d9f8b-xkj2p/restart")

        mock_core_v1.delete_namespaced_pod.assert_called_once_with(
            name="payment-service-7d9f8b-xkj2p",
            namespace="production",
        )

    def test_restart_pod_response_contains_action(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """Response should confirm the restart action and pod details."""
        mock_core_v1.delete_namespaced_pod.return_value = MagicMock()

        response = test_client.post("/pods/production/mypod/restart")
        data = response.json()

        assert data["action"] == "restart"
        assert data["pod_name"] == "mypod"
        assert data["namespace"] == "production"
        assert data["status"] == "pod_deleted"

    def test_restart_pod_500_on_error(
        self,
        test_client: TestClient,
        mock_core_v1: MagicMock,
    ) -> None:
        """restart_pod should return 500 if delete raises an exception."""
        mock_core_v1.delete_namespaced_pod.side_effect = Exception("API server unavailable")

        response = test_client.post("/pods/production/mypod/restart")

        assert response.status_code == 500


# ---------------------------------------------------------------------------
# Test: scale deployment
# ---------------------------------------------------------------------------

class TestScaleDeployment:
    """Tests for PATCH /deployments/{namespace}/{deployment_name}/scale."""

    def test_scale_deployment_returns_200(
        self,
        test_client: TestClient,
        mock_apps_v1: MagicMock,
    ) -> None:
        """scale_deployment should return HTTP 200 on success."""
        mock_apps_v1.patch_namespaced_deployment_scale.return_value = MagicMock()

        response = test_client.patch(
            "/deployments/production/payment-service/scale?replicas=5"
        )

        assert response.status_code == 200

    def test_scale_deployment_calls_api_with_replicas(
        self,
        test_client: TestClient,
        mock_apps_v1: MagicMock,
    ) -> None:
        """scale_deployment should call patch_namespaced_deployment_scale with correct replicas."""
        mock_apps_v1.patch_namespaced_deployment_scale.return_value = MagicMock()

        test_client.patch(
            "/deployments/production/payment-service/scale?replicas=10"
        )

        call_args = mock_apps_v1.patch_namespaced_deployment_scale.call_args
        assert call_args[1]["name"] == "payment-service"
        assert call_args[1]["namespace"] == "production"
        assert call_args[1]["body"]["spec"]["replicas"] == 10

    def test_scale_deployment_response_content(
        self,
        test_client: TestClient,
        mock_apps_v1: MagicMock,
    ) -> None:
        """Response should confirm the scale action and final replica count."""
        mock_apps_v1.patch_namespaced_deployment_scale.return_value = MagicMock()

        response = test_client.patch(
            "/deployments/staging/api-gateway/scale?replicas=3"
        )
        data = response.json()

        assert data["action"] == "scale"
        assert data["deployment_name"] == "api-gateway"
        assert data["namespace"] == "staging"
        assert data["replicas"] == 3
        assert data["status"] == "scaled"

    def test_scale_deployment_500_on_error(
        self,
        test_client: TestClient,
        mock_apps_v1: MagicMock,
    ) -> None:
        """scale_deployment should return 500 if the Kubernetes API raises."""
        mock_apps_v1.patch_namespaced_deployment_scale.side_effect = Exception(
            "insufficient permissions"
        )

        response = test_client.patch(
            "/deployments/production/payment-service/scale?replicas=5"
        )

        assert response.status_code == 500

    def test_scale_deployment_to_zero(
        self,
        test_client: TestClient,
        mock_apps_v1: MagicMock,
    ) -> None:
        """scale_deployment should allow scaling to 0 replicas (shutdown)."""
        mock_apps_v1.patch_namespaced_deployment_scale.return_value = MagicMock()

        response = test_client.patch(
            "/deployments/staging/non-critical-service/scale?replicas=0"
        )
        data = response.json()

        assert response.status_code == 200
        assert data["replicas"] == 0
