"""
orchestrator.py
---------------
LangGraph-based orchestrator agent for the Agentic SRE Platform.

Responsibilities:
  - Receive incoming alerts (from API, queue, or test harness)
  - Perform RAG lookup against ChromaDB runbook store
  - Classify alert type / severity with an LLM
  - Route work to the appropriate specialist agent
  - Persist conversation state via LangGraph MemorySaver
  - Produce a human-readable summary and send a notification
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Sequence

from langchain_core.messages import AnyMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from loguru import logger
from typing_extensions import TypedDict

# ---------------------------------------------------------------------------
# Lazy imports for specialist agents (avoids circular dependencies at module
# load time and keeps cold-start fast when only certain agents are needed).
# ---------------------------------------------------------------------------


def _get_k8s_agent():
    from agents.kubernetes_agent import KubernetesAgent
    return KubernetesAgent()


def _get_metrics_agent():
    from agents.metrics_agent import MetricsAgent
    return MetricsAgent()


def _get_incident_agent():
    from agents.incident_agent import IncidentAgent
    return IncidentAgent()


def _get_rag_agent():
    from agents.rag_agent import RAGAgent
    return RAGAgent()


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class OrchestratorState(TypedDict, total=False):
    """Shared state passed between every node in the LangGraph graph."""

    # ---- alert identity ----------------------------------------------------
    alert_id: str
    """Unique identifier for the incoming alert."""

    alert_type: str
    """Coarse category: 'kubernetes', 'metrics', 'incident', 'unknown'."""

    severity: str
    """One of: 'critical', 'high', 'medium', 'low'."""

    namespace: str
    """Kubernetes namespace associated with the alert (may be empty)."""

    pod_name: str
    """Kubernetes pod name (may be empty)."""

    service_name: str
    """Logical service / deployment name."""

    raw_alert: dict[str, Any]
    """Original alert payload as received."""

    # ---- enrichment --------------------------------------------------------
    rag_context: str
    """Runbook passages retrieved from ChromaDB."""

    remediation_steps: list[str]
    """Ordered list of remediation steps suggested by RAG / LLM."""

    # ---- routing / execution -----------------------------------------------
    assigned_agent: str
    """Which specialist agent was invoked: 'kubernetes', 'metrics', 'incident'."""

    result: dict[str, Any]
    """Structured result returned by the specialist agent."""

    status: str
    """Overall pipeline status: 'open', 'in_progress', 'resolved', 'escalated'."""

    escalate: bool
    """True if the issue could not be resolved automatically."""

    # ---- communication -----------------------------------------------------
    messages: list[AnyMessage]
    """LangChain message history for the LLM conversation."""

    summary: str
    """Human-readable summary of actions taken."""

    incident_number: Optional[str]
    """ServiceNow incident number if one was created."""

    created_at: str
    """ISO-8601 timestamp when the alert was first received."""


# ---------------------------------------------------------------------------
# Orchestrator class
# ---------------------------------------------------------------------------

class SREOrchestrator:
    """
    LangGraph-based orchestrator for the SRE Agent Platform.

    Usage::

        orchestrator = SREOrchestrator()
        final_state = orchestrator.run(alert_payload)
    """

    # LLM used for classification and summarisation
    _DEFAULT_MODEL = "gpt-4o"
    _DEFAULT_TEMPERATURE = 0.0

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        temperature: float = _DEFAULT_TEMPERATURE,
        openai_api_key: Optional[str] = None,
    ) -> None:
        """
        Initialise the orchestrator.

        Args:
            model: OpenAI model name to use for classification/summarisation.
            temperature: LLM temperature (0.0 = deterministic).
            openai_api_key: Optional API key override; falls back to
                ``OPENAI_API_KEY`` environment variable.
        """
        api_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
        self.llm = ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=api_key,
        )
        self.memory = MemorySaver()
        self.graph = self._build_graph()
        logger.info("SREOrchestrator initialised with model={}", model)

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self) -> Any:
        """Assemble and compile the LangGraph StateGraph."""
        builder = StateGraph(OrchestratorState)

        # Register nodes
        builder.add_node("receive_alert", self._node_receive_alert)
        builder.add_node("rag_lookup", self._node_rag_lookup)
        builder.add_node("classify_alert", self._node_classify_alert)
        builder.add_node("route_to_agent", self._node_route_to_agent)
        builder.add_node("k8s_remediate", self._node_k8s_remediate)
        builder.add_node("metrics_analyze", self._node_metrics_analyze)
        builder.add_node("create_incident", self._node_create_incident)
        builder.add_node("summarize", self._node_summarize)
        builder.add_node("notify", self._node_notify)

        # Entry point
        builder.set_entry_point("receive_alert")

        # Linear pipeline up to routing
        builder.add_edge("receive_alert", "rag_lookup")
        builder.add_edge("rag_lookup", "classify_alert")

        # Conditional routing after classification
        builder.add_conditional_edges(
            "classify_alert",
            self._route_condition,
            {
                "kubernetes": "k8s_remediate",
                "metrics": "metrics_analyze",
                "incident": "create_incident",
                "unknown": "create_incident",  # default: raise incident
            },
        )

        # All specialist agents converge on summarize → notify → END
        builder.add_edge("k8s_remediate", "summarize")
        builder.add_edge("metrics_analyze", "summarize")
        builder.add_edge("create_incident", "summarize")
        builder.add_edge("summarize", "notify")
        builder.add_edge("notify", END)

        return builder.compile(checkpointer=self.memory)

    # ------------------------------------------------------------------
    # Routing condition
    # ------------------------------------------------------------------

    @staticmethod
    def _route_condition(state: OrchestratorState) -> str:
        """
        Determine which specialist node to invoke next.

        Args:
            state: Current graph state containing ``alert_type``.

        Returns:
            String key matching a conditional edge: 'kubernetes', 'metrics',
            'incident', or 'unknown'.
        """
        alert_type = state.get("alert_type", "unknown").lower()
        valid = {"kubernetes", "metrics", "incident"}
        return alert_type if alert_type in valid else "unknown"

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def _node_receive_alert(self, state: OrchestratorState) -> OrchestratorState:
        """
        Parse the incoming alert payload and populate base state fields.

        Extracts: alert_id, severity, namespace, pod_name, service_name,
        raw_alert, created_at.  Initialises messages list with a system prompt.

        Args:
            state: Input state (contains ``raw_alert``).

        Returns:
            Updated state with parsed alert fields.
        """
        raw = state.get("raw_alert", {})
        alert_id = raw.get("alert_id") or str(uuid.uuid4())

        logger.info("receive_alert | alert_id={}", alert_id)

        system_msg = SystemMessage(
            content=(
                "You are an expert Site Reliability Engineer. "
                "Analyse alerts, identify root causes, and recommend remediations. "
                "Be concise and actionable."
            )
        )
        human_msg = HumanMessage(
            content=f"New alert received:\n{raw}"
        )

        return {
            **state,
            "alert_id": alert_id,
            "severity": raw.get("severity", "unknown"),
            "namespace": raw.get("namespace", ""),
            "pod_name": raw.get("pod_name", ""),
            "service_name": raw.get("service_name", ""),
            "status": "open",
            "escalate": False,
            "messages": [system_msg, human_msg],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _node_rag_lookup(self, state: OrchestratorState) -> OrchestratorState:
        """
        Query the ChromaDB runbook store for context relevant to this alert.

        Uses ``RAGAgent.query_runbooks`` and ``RAGAgent.get_remediation_steps``.

        Args:
            state: Current state; uses ``raw_alert``, ``service_name``, and
                ``severity`` to build the query.

        Returns:
            State updated with ``rag_context`` and ``remediation_steps``.
        """
        raw = state.get("raw_alert", {})
        query = (
            f"{raw.get('title', '')} "
            f"{raw.get('description', '')} "
            f"service={state.get('service_name', '')} "
            f"namespace={state.get('namespace', '')}"
        ).strip()

        logger.info("rag_lookup | query={!r}", query[:120])

        rag_context = ""
        remediation_steps: list[str] = []

        try:
            rag = _get_rag_agent()
            results = rag.query_runbooks(query)
            if results:
                rag_context = "\n\n".join(
                    f"### {r.title}\n{r.content}" for r in results[:3]
                )
                # Also fetch issue-type-specific remediation
                issue_type = raw.get("alert_type") or raw.get("type", "")
                if issue_type:
                    steps = rag.get_remediation_steps(issue_type)
                    remediation_steps = steps
        except Exception as exc:
            logger.warning("rag_lookup | RAG query failed: {}", exc)
            rag_context = "No runbook context available."

        # Append RAG context to the conversation
        messages: list[AnyMessage] = list(state.get("messages", []))
        messages.append(
            HumanMessage(content=f"Relevant runbook context:\n{rag_context}")
        )

        return {
            **state,
            "rag_context": rag_context,
            "remediation_steps": remediation_steps,
            "messages": messages,
        }

    def _node_classify_alert(self, state: OrchestratorState) -> OrchestratorState:
        """
        Use the LLM to classify alert_type and normalise severity.

        Sends the alert details + RAG context to the LLM and parses
        structured JSON from the response.

        Args:
            state: Current state with messages and raw_alert.

        Returns:
            State updated with ``alert_type`` and ``severity``.
        """
        raw = state.get("raw_alert", {})
        rag_context = state.get("rag_context", "")

        classify_prompt = f"""Classify the following infrastructure alert.

Alert payload:
{raw}

Relevant runbook context:
{rag_context}

Respond with ONLY valid JSON in this exact format (no markdown fences):
{{
  "alert_type": "<kubernetes|metrics|incident>",
  "severity": "<critical|high|medium|low>",
  "reasoning": "<one sentence>"
}}"""

        messages: list[AnyMessage] = list(state.get("messages", []))
        messages.append(HumanMessage(content=classify_prompt))

        logger.info("classify_alert | invoking LLM for alert_id={}", state.get("alert_id"))

        try:
            response = self.llm.invoke(messages)
            content = response.content.strip()
            # Strip markdown code fences if present
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            import json
            parsed = json.loads(content)
            alert_type = parsed.get("alert_type", "unknown").lower()
            severity = parsed.get("severity", state.get("severity", "unknown")).lower()
            reasoning = parsed.get("reasoning", "")

            logger.info(
                "classify_alert | type={} severity={} reasoning={!r}",
                alert_type,
                severity,
                reasoning,
            )
        except Exception as exc:
            logger.warning("classify_alert | LLM parsing failed: {} — using raw values", exc)
            alert_type = raw.get("alert_type", "unknown").lower()
            severity = state.get("severity", "unknown").lower()

        messages.append(response if "response" in dir() else HumanMessage(content="Classification failed."))

        return {
            **state,
            "alert_type": alert_type,
            "severity": severity,
            "messages": messages,
            "status": "in_progress",
        }

    def _node_route_to_agent(self, state: OrchestratorState) -> OrchestratorState:
        """
        Populate ``assigned_agent`` based on ``alert_type``.

        This node is present for explicitness; the actual branching is handled
        by the conditional edges from ``classify_alert``.

        Args:
            state: Current state with ``alert_type``.

        Returns:
            State updated with ``assigned_agent``.
        """
        alert_type = state.get("alert_type", "unknown")
        mapping = {
            "kubernetes": "kubernetes",
            "metrics": "metrics",
            "incident": "incident",
        }
        assigned = mapping.get(alert_type, "incident")
        logger.info("route_to_agent | assigned={}", assigned)
        return {**state, "assigned_agent": assigned}

    def _node_k8s_remediate(self, state: OrchestratorState) -> OrchestratorState:
        """
        Invoke the KubernetesAgent to diagnose and remediate pod/node issues.

        Args:
            state: Current state (namespace, pod_name, severity, etc.).

        Returns:
            State updated with ``result``, ``status``, and ``escalate``.
        """
        logger.info(
            "k8s_remediate | pod={} ns={}",
            state.get("pod_name"),
            state.get("namespace"),
        )
        result: dict[str, Any] = {}
        escalate = False

        try:
            k8s = _get_k8s_agent()
            diagnosis = k8s.diagnose_issue(state)
            remediation = k8s.remediate(state)
            result = {
                "agent": "kubernetes",
                "diagnosis": diagnosis,
                "remediation": remediation,
            }
        except Exception as exc:
            logger.error("k8s_remediate | error: {}", exc)
            result = {"agent": "kubernetes", "error": str(exc)}
            escalate = True

        return {
            **state,
            "assigned_agent": "kubernetes",
            "result": result,
            "status": "resolved" if not escalate else "escalated",
            "escalate": escalate,
        }

    def _node_metrics_analyze(self, state: OrchestratorState) -> OrchestratorState:
        """
        Invoke the MetricsAgent to analyse time-series metrics for the service.

        Args:
            state: Current state (namespace, service_name).

        Returns:
            State updated with ``result`` and anomaly insights.
        """
        logger.info("metrics_analyze | service={}", state.get("service_name"))
        result: dict[str, Any] = {}
        escalate = False

        try:
            metrics = _get_metrics_agent()
            analysis = metrics.analyze_metrics(
                namespace=state.get("namespace", "default"),
                service_name=state.get("service_name", ""),
                time_window=3600,  # last hour
            )
            anomalies = metrics.detect_anomalies()
            insights = metrics.generate_insights()
            result = {
                "agent": "metrics",
                "analysis": analysis.__dict__ if hasattr(analysis, "__dict__") else str(analysis),
                "anomalies": [a.__dict__ if hasattr(a, "__dict__") else str(a) for a in anomalies],
                "insights": insights,
            }
        except Exception as exc:
            logger.error("metrics_analyze | error: {}", exc)
            result = {"agent": "metrics", "error": str(exc)}
            escalate = True

        return {
            **state,
            "assigned_agent": "metrics",
            "result": result,
            "status": "resolved" if not escalate else "escalated",
            "escalate": escalate,
        }

    def _node_create_incident(self, state: OrchestratorState) -> OrchestratorState:
        """
        Create a ServiceNow incident for the alert.

        Args:
            state: Current state; passes ``raw_alert`` + enrichments to the
                IncidentAgent.

        Returns:
            State updated with ``incident_number`` and ``result``.
        """
        logger.info("create_incident | alert_id={}", state.get("alert_id"))
        result: dict[str, Any] = {}
        escalate = False

        try:
            incident = _get_incident_agent()
            alert_data = {
                **state.get("raw_alert", {}),
                "alert_id": state.get("alert_id"),
                "alert_type": state.get("alert_type"),
                "severity": state.get("severity"),
                "namespace": state.get("namespace"),
                "pod_name": state.get("pod_name"),
                "service_name": state.get("service_name"),
                "rag_context": state.get("rag_context", ""),
                "remediation_steps": state.get("remediation_steps", []),
            }
            incident_number = incident.create_incident(alert_data)
            result = {
                "agent": "incident",
                "incident_number": incident_number,
            }
        except Exception as exc:
            logger.error("create_incident | error: {}", exc)
            result = {"agent": "incident", "error": str(exc)}
            escalate = True

        return {
            **state,
            "assigned_agent": "incident",
            "incident_number": result.get("incident_number"),
            "result": result,
            "status": "in_progress" if result.get("incident_number") else "escalated",
            "escalate": escalate,
        }

    def _node_summarize(self, state: OrchestratorState) -> OrchestratorState:
        """
        Generate a human-readable summary of all actions taken.

        Sends the accumulated conversation history to the LLM and requests
        a concise executive summary.

        Args:
            state: Full state after specialist agent execution.

        Returns:
            State updated with ``summary``.
        """
        logger.info("summarize | alert_id={}", state.get("alert_id"))

        result = state.get("result", {})
        summary_prompt = f"""Provide a concise executive summary of the SRE incident response.

Alert ID: {state.get('alert_id')}
Alert Type: {state.get('alert_type')}
Severity: {state.get('severity')}
Namespace: {state.get('namespace')}
Pod: {state.get('pod_name')}
Service: {state.get('service_name')}
Assigned Agent: {state.get('assigned_agent')}
Status: {state.get('status')}
Incident Number: {state.get('incident_number', 'N/A')}
Escalated: {state.get('escalate', False)}

Agent Result:
{result}

Remediation Steps Applied:
{state.get('remediation_steps', [])}

RAG Context Used:
{state.get('rag_context', 'None')[:500]}

Write a 3-5 sentence summary covering: what the issue was, what actions were taken, current status, and any follow-up required.
"""
        try:
            response = self.llm.invoke([HumanMessage(content=summary_prompt)])
            summary = response.content.strip()
        except Exception as exc:
            logger.warning("summarize | LLM call failed: {}", exc)
            summary = (
                f"Alert {state.get('alert_id')} ({state.get('alert_type')}, "
                f"severity={state.get('severity')}) processed by "
                f"{state.get('assigned_agent')} agent. "
                f"Status: {state.get('status')}."
            )

        logger.info("summarize | summary={!r}", summary[:120])
        return {**state, "summary": summary}

    def _node_notify(self, state: OrchestratorState) -> OrchestratorState:
        """
        Send a notification with the incident summary.

        Currently logs to stdout and writes to a mock notification store.
        Replace with real PagerDuty / Slack / email integration as needed.

        Args:
            state: Full final state including ``summary``.

        Returns:
            State unchanged (side-effect only node).
        """
        alert_id = state.get("alert_id", "unknown")
        severity = state.get("severity", "unknown")
        status = state.get("status", "unknown")
        summary = state.get("summary", "")
        incident_number = state.get("incident_number", "N/A")
        escalate = state.get("escalate", False)

        notification = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "alert_id": alert_id,
            "severity": severity,
            "status": status,
            "incident_number": incident_number,
            "escalate": escalate,
            "summary": summary,
            "channel": "ops-alerts",
        }

        # In production: send to Slack, PagerDuty, email, etc.
        logger.info(
            "notify | alert_id={} severity={} status={} incident={} escalate={}",
            alert_id,
            severity,
            status,
            incident_number,
            escalate,
        )
        logger.info("notify | notification payload: {}", notification)

        return state

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(
        self,
        alert_payload: dict[str, Any],
        thread_id: Optional[str] = None,
    ) -> OrchestratorState:
        """
        Execute the full orchestration pipeline for a single alert.

        Args:
            alert_payload: Raw alert dict.  Must contain at minimum a
                ``description`` or ``title`` key.
            thread_id: Optional LangGraph thread ID for conversation
                persistence.  A new UUID is generated if not provided.

        Returns:
            The final ``OrchestratorState`` after all nodes have executed.
        """
        thread_id = thread_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        initial_state: OrchestratorState = {
            "raw_alert": alert_payload,
            "alert_id": alert_payload.get("alert_id", ""),
            "alert_type": alert_payload.get("alert_type", "unknown"),
            "severity": alert_payload.get("severity", "unknown"),
            "namespace": alert_payload.get("namespace", ""),
            "pod_name": alert_payload.get("pod_name", ""),
            "service_name": alert_payload.get("service_name", ""),
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

        logger.info(
            "run | starting pipeline thread_id={} alert_payload_keys={}",
            thread_id,
            list(alert_payload.keys()),
        )

        final_state = self.graph.invoke(initial_state, config=config)
        logger.info(
            "run | pipeline complete status={} escalate={}",
            final_state.get("status"),
            final_state.get("escalate"),
        )
        return final_state

    def run_async_stream(
        self,
        alert_payload: dict[str, Any],
        thread_id: Optional[str] = None,
    ):
        """
        Stream node-by-node state updates for an alert (generator).

        Yields tuples of (node_name, partial_state) as each node completes.

        Args:
            alert_payload: Raw alert dict.
            thread_id: Optional LangGraph thread ID.

        Yields:
            Tuples of (str, OrchestratorState).
        """
        thread_id = thread_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        initial_state: OrchestratorState = {
            "raw_alert": alert_payload,
            "alert_id": alert_payload.get("alert_id", ""),
            "alert_type": alert_payload.get("alert_type", "unknown"),
            "severity": alert_payload.get("severity", "unknown"),
            "namespace": alert_payload.get("namespace", ""),
            "pod_name": alert_payload.get("pod_name", ""),
            "service_name": alert_payload.get("service_name", ""),
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

        for event in self.graph.stream(initial_state, config=config):
            for node_name, partial_state in event.items():
                logger.debug("stream | node={}", node_name)
                yield node_name, partial_state
