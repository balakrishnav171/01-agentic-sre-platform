"""
tests/test_orchestrator.py
--------------------------
Unit tests for the LangGraph-based SREOrchestrator.

Tests cover:
- receive_alert node: parses raw alert payload into state
- classify_alert node: routes kubernetes vs. metrics alerts
- rag_lookup node: retrieves and embeds runbook context
- full graph execution: end-to-end with mocked LLM and agents
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def k8s_alert_payload() -> Dict[str, Any]:
    """Sample CrashLoopBackOff Kubernetes alert payload."""
    return {
        "alert_id": "alert-k8s-001",
        "alert_type": "CrashLoopBackOff",
        "severity": "high",
        "namespace": "production",
        "pod_name": "payment-service-7d9f8b-xkj2p",
        "service_name": "payment-service",
        "description": "Pod payment-service-7d9f8b-xkj2p is in CrashLoopBackOff state",
        "title": "CrashLoopBackOff in production",
        "source": "prometheus",
        "labels": {"team": "payments", "env": "prod"},
    }


@pytest.fixture
def metrics_alert_payload() -> Dict[str, Any]:
    """Sample high CPU usage metrics alert payload."""
    return {
        "alert_id": "alert-metrics-001",
        "alert_type": "HighCPUUsage",
        "severity": "medium",
        "namespace": "staging",
        "pod_name": "api-gateway-6c4d9-zx9nm",
        "service_name": "api-gateway",
        "description": "CPU utilisation at 95% for api-gateway in staging",
        "title": "High CPU in staging",
        "source": "datadog",
        "labels": {"team": "platform", "env": "staging"},
    }


@pytest.fixture
def base_state(k8s_alert_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-populated OrchestratorState as produced by receive_alert node."""
    return {
        "alert_id": "alert-k8s-001",
        "alert_type": "unknown",
        "severity": "high",
        "namespace": "production",
        "pod_name": "payment-service-7d9f8b-xkj2p",
        "service_name": "payment-service",
        "raw_alert": k8s_alert_payload,
        "rag_context": "",
        "remediation_steps": [],
        "assigned_agent": "",
        "result": {},
        "status": "open",
        "escalate": False,
        "messages": [],
        "summary": "",
        "incident_number": None,
        "created_at": "",
    }


@pytest.fixture
def mock_llm() -> MagicMock:
    """Mock ChatOpenAI LLM that returns a valid classification response."""
    mock = MagicMock()
    mock.invoke.return_value = MagicMock(
        content=json.dumps({
            "alert_type": "kubernetes",
            "severity": "high",
            "reasoning": "Pod in CrashLoopBackOff is a Kubernetes workload issue.",
        })
    )
    return mock


@pytest.fixture
def mock_llm_metrics() -> MagicMock:
    """Mock ChatOpenAI LLM that returns a metrics classification response."""
    mock = MagicMock()
    mock.invoke.return_value = MagicMock(
        content=json.dumps({
            "alert_type": "metrics",
            "severity": "medium",
            "reasoning": "High CPU usage is a metrics/performance issue.",
        })
    )
    return mock


# ---------------------------------------------------------------------------
# Test: receive_alert node
# ---------------------------------------------------------------------------

class TestReceiveAlertNode:
    """Tests for the _node_receive_alert graph node."""

    @patch("agents.orchestrator.MemorySaver")
    @patch("langchain_openai.ChatOpenAI")
    def test_receive_alert_populates_alert_id(
        self,
        mock_openai: MagicMock,
        mock_memory: MagicMock,
        k8s_alert_payload: Dict[str, Any],
    ) -> None:
        """receive_alert should preserve the alert_id from the payload."""
        from agents.orchestrator import SREOrchestrator

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = MagicMock()
        orchestrator.memory = MagicMock()

        initial_state: Dict[str, Any] = {
            "raw_alert": k8s_alert_payload,
            "messages": [],
        }

        result = orchestrator._node_receive_alert(initial_state)

        assert result["alert_id"] == "alert-k8s-001"

    @patch("agents.orchestrator.MemorySaver")
    @patch("langchain_openai.ChatOpenAI")
    def test_receive_alert_auto_generates_id(
        self,
        mock_openai: MagicMock,
        mock_memory: MagicMock,
    ) -> None:
        """receive_alert should generate a UUID if alert_id is missing from payload."""
        from agents.orchestrator import SREOrchestrator

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = MagicMock()
        orchestrator.memory = MagicMock()

        initial_state: Dict[str, Any] = {
            "raw_alert": {"severity": "low", "description": "test alert"},
            "messages": [],
        }

        result = orchestrator._node_receive_alert(initial_state)

        assert "alert_id" in result
        assert len(result["alert_id"]) == 36  # UUID format

    @patch("agents.orchestrator.MemorySaver")
    @patch("langchain_openai.ChatOpenAI")
    def test_receive_alert_sets_status_open(
        self,
        mock_openai: MagicMock,
        mock_memory: MagicMock,
        k8s_alert_payload: Dict[str, Any],
    ) -> None:
        """receive_alert should set status='open' and escalate=False."""
        from agents.orchestrator import SREOrchestrator

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = MagicMock()
        orchestrator.memory = MagicMock()

        result = orchestrator._node_receive_alert({"raw_alert": k8s_alert_payload, "messages": []})

        assert result["status"] == "open"
        assert result["escalate"] is False

    @patch("agents.orchestrator.MemorySaver")
    @patch("langchain_openai.ChatOpenAI")
    def test_receive_alert_initialises_messages(
        self,
        mock_openai: MagicMock,
        mock_memory: MagicMock,
        k8s_alert_payload: Dict[str, Any],
    ) -> None:
        """receive_alert should initialise messages with a SystemMessage and HumanMessage."""
        from agents.orchestrator import SREOrchestrator
        from langchain_core.messages import HumanMessage, SystemMessage

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = MagicMock()
        orchestrator.memory = MagicMock()

        result = orchestrator._node_receive_alert({"raw_alert": k8s_alert_payload, "messages": []})

        messages = result["messages"]
        assert len(messages) >= 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)


# ---------------------------------------------------------------------------
# Test: classify_alert node — Kubernetes
# ---------------------------------------------------------------------------

class TestClassifyAlertK8s:
    """Tests for _node_classify_alert with a Kubernetes-type alert."""

    @patch("agents.orchestrator.MemorySaver")
    def test_classify_alert_kubernetes(
        self,
        mock_memory: MagicMock,
        base_state: Dict[str, Any],
        mock_llm: MagicMock,
    ) -> None:
        """classify_alert should classify CrashLoopBackOff as 'kubernetes'."""
        from agents.orchestrator import SREOrchestrator
        from langchain_core.messages import HumanMessage, SystemMessage

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = mock_llm
        orchestrator.memory = MagicMock()

        # Add messages to state to simulate prior nodes
        state = {
            **base_state,
            "messages": [
                SystemMessage(content="You are an SRE expert."),
                HumanMessage(content="CrashLoopBackOff alert"),
            ],
        }

        result = orchestrator._node_classify_alert(state)

        assert result["alert_type"] == "kubernetes"
        assert result["severity"] == "high"
        assert result["status"] == "in_progress"

    @patch("agents.orchestrator.MemorySaver")
    def test_classify_alert_preserves_severity(
        self,
        mock_memory: MagicMock,
        base_state: Dict[str, Any],
        mock_llm: MagicMock,
    ) -> None:
        """classify_alert should use the LLM-returned severity."""
        from agents.orchestrator import SREOrchestrator
        from langchain_core.messages import HumanMessage, SystemMessage

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = mock_llm
        orchestrator.memory = MagicMock()

        state = {
            **base_state,
            "messages": [HumanMessage(content="test")],
        }

        result = orchestrator._node_classify_alert(state)
        assert result["severity"] in {"critical", "high", "medium", "low"}


# ---------------------------------------------------------------------------
# Test: classify_alert node — Metrics
# ---------------------------------------------------------------------------

class TestClassifyAlertMetrics:
    """Tests for _node_classify_alert with a metrics-type alert."""

    @patch("agents.orchestrator.MemorySaver")
    def test_classify_alert_metrics(
        self,
        mock_memory: MagicMock,
        metrics_alert_payload: Dict[str, Any],
        mock_llm_metrics: MagicMock,
    ) -> None:
        """classify_alert should classify HighCPUUsage as 'metrics'."""
        from agents.orchestrator import SREOrchestrator
        from langchain_core.messages import HumanMessage, SystemMessage

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = mock_llm_metrics
        orchestrator.memory = MagicMock()

        state: Dict[str, Any] = {
            "alert_id": "alert-metrics-001",
            "raw_alert": metrics_alert_payload,
            "severity": "medium",
            "rag_context": "",
            "messages": [
                SystemMessage(content="You are an SRE expert."),
                HumanMessage(content="High CPU usage alert"),
            ],
        }

        result = orchestrator._node_classify_alert(state)

        assert result["alert_type"] == "metrics"
        assert result["severity"] == "medium"

    @patch("agents.orchestrator.MemorySaver")
    def test_classify_alert_falls_back_on_llm_error(
        self,
        mock_memory: MagicMock,
        base_state: Dict[str, Any],
    ) -> None:
        """classify_alert should use raw alert_type if LLM raises an exception."""
        from agents.orchestrator import SREOrchestrator
        from langchain_core.messages import HumanMessage

        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = Exception("LLM unavailable")

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = mock_llm
        orchestrator.memory = MagicMock()

        state = {
            **base_state,
            "alert_type": "kubernetes",
            "messages": [HumanMessage(content="test")],
        }

        # Should not raise; should fall back to raw alert_type
        result = orchestrator._node_classify_alert(state)
        assert result["alert_type"] == "kubernetes"


# ---------------------------------------------------------------------------
# Test: rag_lookup node
# ---------------------------------------------------------------------------

class TestRagLookupNode:
    """Tests for _node_rag_lookup — verifies RAG context is populated."""

    @patch("agents.orchestrator.MemorySaver")
    @patch("agents.orchestrator._get_rag_agent")
    def test_rag_lookup_returns_context(
        self,
        mock_get_rag: MagicMock,
        mock_memory: MagicMock,
        base_state: Dict[str, Any],
    ) -> None:
        """rag_lookup should populate rag_context when RAGAgent returns results."""
        from agents.orchestrator import SREOrchestrator
        from langchain_core.messages import HumanMessage

        # Mock RAGAgent
        mock_rag = MagicMock()
        mock_result = MagicMock()
        mock_result.title = "CrashLoopBackOff Runbook"
        mock_result.content = "Check kubectl logs --previous for crash details."
        mock_rag.query_runbooks.return_value = [mock_result]
        mock_rag.get_remediation_steps.return_value = [
            "1. Check pod logs",
            "2. Describe pod",
            "3. Rollback if needed",
        ]
        mock_get_rag.return_value = mock_rag

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = MagicMock()
        orchestrator.memory = MagicMock()

        state = {
            **base_state,
            "messages": [HumanMessage(content="alert")],
        }

        result = orchestrator._node_rag_lookup(state)

        assert "CrashLoopBackOff Runbook" in result["rag_context"]
        assert len(result["remediation_steps"]) == 3
        assert result["remediation_steps"][0] == "1. Check pod logs"

    @patch("agents.orchestrator.MemorySaver")
    @patch("agents.orchestrator._get_rag_agent")
    def test_rag_lookup_handles_empty_results(
        self,
        mock_get_rag: MagicMock,
        mock_memory: MagicMock,
        base_state: Dict[str, Any],
    ) -> None:
        """rag_lookup should set a default rag_context when no results are returned."""
        from agents.orchestrator import SREOrchestrator
        from langchain_core.messages import HumanMessage

        mock_rag = MagicMock()
        mock_rag.query_runbooks.return_value = []
        mock_get_rag.return_value = mock_rag

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = MagicMock()
        orchestrator.memory = MagicMock()

        state = {
            **base_state,
            "messages": [HumanMessage(content="alert")],
        }

        result = orchestrator._node_rag_lookup(state)

        # rag_context should be set (even if empty) and not raise
        assert "rag_context" in result

    @patch("agents.orchestrator.MemorySaver")
    @patch("agents.orchestrator._get_rag_agent")
    def test_rag_lookup_handles_rag_exception(
        self,
        mock_get_rag: MagicMock,
        mock_memory: MagicMock,
        base_state: Dict[str, Any],
    ) -> None:
        """rag_lookup should gracefully handle RAGAgent exceptions."""
        from agents.orchestrator import SREOrchestrator
        from langchain_core.messages import HumanMessage

        mock_rag = MagicMock()
        mock_rag.query_runbooks.side_effect = ConnectionError("ChromaDB unreachable")
        mock_get_rag.return_value = mock_rag

        orchestrator = SREOrchestrator.__new__(SREOrchestrator)
        orchestrator.llm = MagicMock()
        orchestrator.memory = MagicMock()

        state = {
            **base_state,
            "messages": [HumanMessage(content="alert")],
        }

        # Should not raise
        result = orchestrator._node_rag_lookup(state)
        assert result["rag_context"] == "No runbook context available."


# ---------------------------------------------------------------------------
# Test: routing condition
# ---------------------------------------------------------------------------

class TestRouteCondition:
    """Tests for the _route_condition static method."""

    def test_route_condition_kubernetes(self) -> None:
        """Route condition should return 'kubernetes' for kubernetes alert type."""
        from agents.orchestrator import SREOrchestrator

        state = {"alert_type": "kubernetes"}
        assert SREOrchestrator._route_condition(state) == "kubernetes"

    def test_route_condition_metrics(self) -> None:
        """Route condition should return 'metrics' for metrics alert type."""
        from agents.orchestrator import SREOrchestrator

        state = {"alert_type": "metrics"}
        assert SREOrchestrator._route_condition(state) == "metrics"

    def test_route_condition_incident(self) -> None:
        """Route condition should return 'incident' for incident alert type."""
        from agents.orchestrator import SREOrchestrator

        state = {"alert_type": "incident"}
        assert SREOrchestrator._route_condition(state) == "incident"

    def test_route_condition_unknown(self) -> None:
        """Route condition should return 'unknown' for unrecognised alert types."""
        from agents.orchestrator import SREOrchestrator

        state = {"alert_type": "foobar"}
        assert SREOrchestrator._route_condition(state) == "unknown"

    def test_route_condition_case_insensitive(self) -> None:
        """Route condition should normalise to lowercase."""
        from agents.orchestrator import SREOrchestrator

        state = {"alert_type": "KUBERNETES"}
        assert SREOrchestrator._route_condition(state) == "kubernetes"


# ---------------------------------------------------------------------------
# Test: full graph execution (mocked)
# ---------------------------------------------------------------------------

class TestFullGraphExecution:
    """End-to-end test with all external dependencies mocked."""

    @patch("agents.orchestrator._get_k8s_agent")
    @patch("agents.orchestrator._get_rag_agent")
    @patch("langchain_openai.ChatOpenAI")
    def test_full_graph_execution_k8s_alert(
        self,
        mock_chat_openai: MagicMock,
        mock_get_rag: MagicMock,
        mock_get_k8s: MagicMock,
        k8s_alert_payload: Dict[str, Any],
    ) -> None:
        """
        Full graph should process a Kubernetes alert end-to-end and return
        a final state with status, assigned_agent, and summary populated.
        """
        # Mock LLM responses
        mock_llm_instance = MagicMock()
        mock_chat_openai.return_value = mock_llm_instance

        def llm_side_effect(messages: Any) -> MagicMock:
            content = str(messages)
            if "Classify" in content or "alert_type" in content.lower():
                return MagicMock(
                    content=json.dumps({
                        "alert_type": "kubernetes",
                        "severity": "high",
                        "reasoning": "CrashLoopBackOff is Kubernetes.",
                    })
                )
            return MagicMock(
                content="The pod is in CrashLoopBackOff. "
                        "The Kubernetes agent restarted it. Status: resolved."
            )

        mock_llm_instance.invoke.side_effect = llm_side_effect

        # Mock RAGAgent
        mock_rag = MagicMock()
        mock_rag_result = MagicMock()
        mock_rag_result.title = "CrashLoopBackOff"
        mock_rag_result.content = "Check pod logs, describe pod, rollback if needed."
        mock_rag.query_runbooks.return_value = [mock_rag_result]
        mock_rag.get_remediation_steps.return_value = [
            "1. Check logs",
            "2. Describe pod",
            "3. Rollback",
        ]
        mock_get_rag.return_value = mock_rag

        # Mock KubernetesAgent
        mock_k8s = MagicMock()
        mock_k8s.diagnose_issue.return_value = {"status": "CrashLoopBackOff", "restart_count": 5}
        mock_k8s.remediate.return_value = {"action": "restarted", "steps": ["Restarted pod"]}
        mock_get_k8s.return_value = mock_k8s

        from agents.orchestrator import SREOrchestrator

        orchestrator = SREOrchestrator()
        final_state = orchestrator.run(k8s_alert_payload, thread_id="test-thread-001")

        assert final_state["alert_id"] == "alert-k8s-001"
        assert final_state["alert_type"] == "kubernetes"
        assert final_state["assigned_agent"] == "kubernetes"
        assert final_state["status"] in {"resolved", "escalated", "in_progress"}
        assert isinstance(final_state["summary"], str)
        assert len(final_state["summary"]) > 0

    @patch("agents.orchestrator._get_incident_agent")
    @patch("agents.orchestrator._get_rag_agent")
    @patch("langchain_openai.ChatOpenAI")
    def test_full_graph_execution_unknown_alert_creates_incident(
        self,
        mock_chat_openai: MagicMock,
        mock_get_rag: MagicMock,
        mock_get_incident: MagicMock,
    ) -> None:
        """An unknown alert type should route to the incident agent."""
        mock_llm_instance = MagicMock()
        mock_chat_openai.return_value = mock_llm_instance
        mock_llm_instance.invoke.return_value = MagicMock(
            content=json.dumps({
                "alert_type": "unknown",
                "severity": "low",
                "reasoning": "Cannot classify.",
            })
        )

        mock_rag = MagicMock()
        mock_rag.query_runbooks.return_value = []
        mock_get_rag.return_value = mock_rag

        mock_incident = MagicMock()
        mock_incident.create_incident.return_value = "INC0001234"
        mock_get_incident.return_value = mock_incident

        unknown_payload = {
            "alert_id": "alert-unknown-001",
            "alert_type": "StrangeError",
            "severity": "low",
            "namespace": "default",
            "description": "Something unexpected happened.",
            "title": "Unknown alert",
        }

        from agents.orchestrator import SREOrchestrator

        orchestrator = SREOrchestrator()
        final_state = orchestrator.run(unknown_payload)

        assert final_state["assigned_agent"] == "incident"
        assert final_state["incident_number"] == "INC0001234"
