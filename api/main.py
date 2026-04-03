"""
SRE Agent Platform API — main FastAPI application.

Provides REST endpoints to:
- Receive and process SRE alerts through the LangGraph orchestrator
- Converse with the SRE AI via a chat interface
- Introspect registered A2A agents
- Search the runbook knowledge base via RAG
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# App initialisation
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SRE Agent Platform",
    version="1.0.0",
    description="Agentic SRE with LangGraph + MCP + A2A",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AlertRequest(BaseModel):
    """Incoming SRE alert payload."""

    alert_id: Optional[str] = Field(
        default=None,
        description="Unique alert identifier; auto-generated if not provided.",
    )
    alert_type: str = Field(
        ...,
        description="Coarse alert category, e.g. 'CrashLoopBackOff', 'HighCPU', 'OOMKilled'.",
    )
    severity: str = Field(
        ...,
        description="Alert severity: critical | high | medium | low",
    )
    namespace: str = Field(
        ...,
        description="Kubernetes namespace where the alert originated.",
    )
    pod_name: Optional[str] = Field(
        default=None,
        description="Name of the affected pod (if applicable).",
    )
    service_name: Optional[str] = Field(
        default=None,
        description="Logical service or deployment name.",
    )
    message: str = Field(
        ...,
        description="Human-readable alert message or description.",
    )
    source: str = Field(
        default="bigpanda",
        description="Alert source system: bigpanda | datadog | pagerduty | prometheus",
    )
    labels: Optional[Dict[str, str]] = Field(
        default=None,
        description="Additional labels/tags from the alert source.",
    )


class ChatRequest(BaseModel):
    """Conversational chat request."""

    session_id: str = Field(
        ...,
        description="Session/thread ID for conversation continuity.",
    )
    message: str = Field(
        ...,
        description="User message to the SRE assistant.",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional additional context (namespace, pod, etc.).",
    )


class AlertResponse(BaseModel):
    """Structured response after processing an SRE alert."""

    alert_id: str
    status: str
    assigned_agent: str
    remediation_steps: List[str]
    incident_number: Optional[str]
    summary: str
    rag_context_used: Optional[str] = None
    escalated: bool = False


class ChatResponse(BaseModel):
    """Response to a chat message."""

    session_id: str
    reply: str
    sources: List[str] = Field(default_factory=list)


class AgentInfo(BaseModel):
    """Metadata about a registered A2A agent."""

    agent_id: str
    name: str
    description: str
    capabilities: List[str]
    endpoint: Optional[str]
    status: str


class RunbookResult(BaseModel):
    """A single runbook search result."""

    runbook_name: str
    content: str
    score: float
    severity: str
    category: str


# ---------------------------------------------------------------------------
# Dependency helpers — lazy imports to keep startup fast
# ---------------------------------------------------------------------------


def _get_orchestrator():
    """Return a cached SREOrchestrator instance."""
    from agents.orchestrator import SREOrchestrator  # noqa: PLC0415
    return SREOrchestrator()


def _get_rag_retriever():
    """Return a SemanticRetriever instance pointed at the runbook collection."""
    from rag.retriever import SemanticRetriever  # noqa: PLC0415
    return SemanticRetriever()


# In-memory agent registry (replace with a real registry / service discovery)
_AGENT_REGISTRY: List[Dict[str, Any]] = [
    {
        "agent_id": "k8s-agent-01",
        "name": "Kubernetes Agent",
        "description": "Diagnoses and remediates Kubernetes workload issues via kubectl and MCP.",
        "capabilities": [
            "pod_diagnosis",
            "pod_restart",
            "deployment_rollback",
            "log_retrieval",
            "resource_scaling",
        ],
        "endpoint": "http://k8s-mcp:8001",
        "status": "active",
    },
    {
        "agent_id": "metrics-agent-01",
        "name": "Metrics Agent",
        "description": "Analyses time-series metrics from Datadog and Prometheus, detects anomalies.",
        "capabilities": [
            "metric_analysis",
            "anomaly_detection",
            "datadog_query",
            "prometheus_query",
            "trend_analysis",
        ],
        "endpoint": "http://metrics-service:8005",
        "status": "active",
    },
    {
        "agent_id": "incident-agent-01",
        "name": "Incident Agent",
        "description": "Creates and manages ServiceNow incidents for SRE alerts.",
        "capabilities": [
            "incident_creation",
            "incident_update",
            "escalation",
            "notification",
        ],
        "endpoint": "http://snow-mcp:8004",
        "status": "active",
    },
    {
        "agent_id": "rag-agent-01",
        "name": "RAG Agent",
        "description": "Retrieves relevant SRE runbooks from ChromaDB knowledge base.",
        "capabilities": [
            "runbook_retrieval",
            "semantic_search",
            "hybrid_search",
            "remediation_lookup",
        ],
        "endpoint": "http://chromadb:8000",
        "status": "active",
    },
]

# In-memory chat session store (replace with Redis or persistent store in production)
_CHAT_SESSIONS: Dict[str, List[Dict[str, str]]] = {}


# ---------------------------------------------------------------------------
# Background task helpers
# ---------------------------------------------------------------------------


def _background_process_alert(alert_id: str, alert_payload: Dict[str, Any]) -> None:
    """
    Fire-and-forget background task to run the full orchestration pipeline.

    This is called for non-blocking alert processing. Results are logged but
    not returned to the caller (use the /alert endpoint for synchronous responses).

    Args:
        alert_id: The alert identifier.
        alert_payload: Full alert dict to pass to the orchestrator.
    """
    try:
        orchestrator = _get_orchestrator()
        final_state = orchestrator.run(alert_payload, thread_id=alert_id)
        logger.info(
            "background_process_alert | alert_id={} status={} escalated={}",
            alert_id,
            final_state.get("status"),
            final_state.get("escalate"),
        )
    except Exception as exc:
        logger.error("background_process_alert | alert_id={} error={}", alert_id, exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post(
    "/alert",
    response_model=AlertResponse,
    summary="Process an incoming SRE alert",
    tags=["Alerts"],
)
async def process_alert(
    alert: AlertRequest,
    background_tasks: BackgroundTasks,
) -> AlertResponse:
    """
    Receive an incoming SRE alert and run it through the full LangGraph
    orchestration pipeline synchronously.

    The pipeline performs:
    1. Alert reception and normalisation
    2. RAG runbook lookup
    3. LLM-based classification and routing
    4. Specialist agent invocation (Kubernetes / Metrics / Incident)
    5. Summarisation and notification

    Args:
        alert: Structured alert payload.
        background_tasks: FastAPI background task runner (used for fire-and-
            forget side effects such as updating external ticketing systems).

    Returns:
        AlertResponse with status, assigned agent, remediation steps, and summary.

    Raises:
        HTTPException 422: If alert payload fails validation.
        HTTPException 500: If the orchestration pipeline encounters an unrecoverable error.
    """
    alert_id = alert.alert_id or str(uuid.uuid4())
    logger.info(
        "POST /alert | alert_id={} type={} severity={} ns={}",
        alert_id,
        alert.alert_type,
        alert.severity,
        alert.namespace,
    )

    # Build the raw alert payload for the orchestrator
    alert_payload: Dict[str, Any] = {
        "alert_id": alert_id,
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "namespace": alert.namespace,
        "pod_name": alert.pod_name or "",
        "service_name": alert.service_name or "",
        "message": alert.message,
        "description": alert.message,
        "title": f"{alert.alert_type} in {alert.namespace}",
        "source": alert.source,
        "labels": alert.labels or {},
    }

    try:
        orchestrator = _get_orchestrator()
        final_state = orchestrator.run(alert_payload, thread_id=alert_id)
    except Exception as exc:
        logger.error("process_alert | orchestration failed: {}", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Orchestration pipeline failed: {str(exc)}",
        ) from exc

    remediation_steps: List[str] = final_state.get("remediation_steps", [])
    if not remediation_steps:
        # Try to pull steps from the agent result
        result = final_state.get("result", {})
        remediation = result.get("remediation", {})
        if isinstance(remediation, dict):
            remediation_steps = remediation.get("steps", [])
        elif isinstance(remediation, list):
            remediation_steps = remediation

    # Enqueue any async side effects
    background_tasks.add_task(
        _background_process_alert,
        alert_id,
        alert_payload,
    )

    return AlertResponse(
        alert_id=alert_id,
        status=final_state.get("status", "unknown"),
        assigned_agent=final_state.get("assigned_agent", "unknown"),
        remediation_steps=remediation_steps,
        incident_number=final_state.get("incident_number"),
        summary=final_state.get("summary", "No summary available."),
        rag_context_used=final_state.get("rag_context", "")[:500] if final_state.get("rag_context") else None,
        escalated=final_state.get("escalate", False),
    )


@app.post(
    "/chat",
    response_model=ChatResponse,
    summary="Conversational SRE assistant",
    tags=["Chat"],
)
async def chat(req: ChatRequest) -> ChatResponse:
    """
    Send a message to the SRE conversational assistant.

    Uses the RAG knowledge base and LLM to answer SRE questions, explain
    runbook steps, or discuss ongoing incidents. Maintains conversation history
    per session_id.

    Args:
        req: Chat request with session_id and message.

    Returns:
        ChatResponse with the assistant's reply and any relevant runbook sources.

    Raises:
        HTTPException 500: If the LLM or RAG query fails.
    """
    logger.info("POST /chat | session_id={} message={!r}", req.session_id, req.message[:80])

    # Retrieve conversation history for this session
    history = _CHAT_SESSIONS.get(req.session_id, [])

    # RAG: retrieve relevant runbook context
    sources: List[str] = []
    rag_context = ""
    try:
        retriever = _get_rag_retriever()
        results = retriever.hybrid_search(req.message, n_results=3)
        if results:
            rag_context = "\n\n".join(
                f"**{r['runbook_name']}** (severity: {r['severity']}):\n{r['content'][:300]}"
                for r in results
            )
            sources = [r["runbook_name"] for r in results]
    except Exception as exc:
        logger.warning("chat | RAG retrieval failed: {}", exc)

    # Build prompt with history and context
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
    from langchain_openai import ChatOpenAI  # noqa: PLC0415
    import os  # noqa: PLC0415

    system_prompt = (
        "You are an expert SRE (Site Reliability Engineer) assistant. "
        "You help engineers diagnose and resolve infrastructure incidents. "
        "Be concise, actionable, and reference Kubernetes best practices. "
        "When you have relevant runbook context, use it in your answer.\n\n"
        f"Relevant runbook context:\n{rag_context}" if rag_context else
        "You are an expert SRE (Site Reliability Engineer) assistant. "
        "You help engineers diagnose and resolve infrastructure incidents. "
        "Be concise and actionable."
    )

    messages = [SystemMessage(content=system_prompt)]

    # Add conversation history
    for turn in history[-10:]:  # Keep last 10 turns
        if turn["role"] == "user":
            messages.append(HumanMessage(content=turn["content"]))
        else:
            from langchain_core.messages import AIMessage  # noqa: PLC0415
            messages.append(AIMessage(content=turn["content"]))

    messages.append(HumanMessage(content=req.message))

    try:
        llm = ChatOpenAI(
            model="gpt-4o",
            temperature=0.1,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        response = llm.invoke(messages)
        reply = response.content.strip()
    except Exception as exc:
        logger.error("chat | LLM invocation failed: {}", exc)
        raise HTTPException(
            status_code=500,
            detail=f"LLM invocation failed: {str(exc)}",
        ) from exc

    # Update session history
    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": reply})
    _CHAT_SESSIONS[req.session_id] = history[-20:]  # Keep last 20 turns max

    logger.info("chat | session_id={} reply_length={}", req.session_id, len(reply))

    return ChatResponse(
        session_id=req.session_id,
        reply=reply,
        sources=sources,
    )


@app.get(
    "/agents",
    response_model=List[AgentInfo],
    summary="List all registered A2A agents",
    tags=["Agents"],
)
async def list_agents() -> List[AgentInfo]:
    """
    Return metadata about all registered Agent-to-Agent (A2A) specialist agents.

    The registry includes the Kubernetes agent, metrics agent, incident agent,
    and RAG agent. In production this would be backed by a service discovery
    mechanism (e.g., Consul, Kubernetes Service labels, or an A2A directory).

    Returns:
        List of AgentInfo objects describing each registered agent.
    """
    logger.info("GET /agents | returning {} agents", len(_AGENT_REGISTRY))
    return [AgentInfo(**agent) for agent in _AGENT_REGISTRY]


@app.get(
    "/agents/{agent_id}",
    response_model=AgentInfo,
    summary="Get a specific agent by ID",
    tags=["Agents"],
)
async def get_agent(agent_id: str) -> AgentInfo:
    """
    Return metadata for a single agent by its ID.

    Args:
        agent_id: The unique agent identifier (e.g. ``"k8s-agent-01"``).

    Returns:
        AgentInfo for the requested agent.

    Raises:
        HTTPException 404: If no agent with the given ID is found.
    """
    for agent in _AGENT_REGISTRY:
        if agent["agent_id"] == agent_id:
            return AgentInfo(**agent)
    raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")


@app.get(
    "/health",
    summary="Health check",
    tags=["System"],
)
async def health() -> Dict[str, str]:
    """
    Return the service health status.

    This endpoint is used by Kubernetes readiness/liveness probes and load
    balancers to verify the API is running.

    Returns:
        Dict with ``status`` and ``version`` keys.
    """
    return {"status": "ok", "version": "1.0.0"}


@app.get(
    "/runbooks",
    response_model=List[RunbookResult],
    summary="Search runbooks via RAG",
    tags=["Runbooks"],
)
async def search_runbooks(
    query: str = Query(..., description="Natural-language search query for runbooks."),
    limit: int = Query(default=5, ge=1, le=20, description="Max number of results to return."),
    keyword: Optional[str] = Query(
        default=None,
        description="Optional keyword filter — only return runbooks containing this string.",
    ),
    severity: Optional[str] = Query(
        default=None,
        description="Filter results by severity level (critical|high|medium|low).",
    ),
) -> List[RunbookResult]:
    """
    Search the SRE runbook knowledge base using semantic (vector) search.

    Optionally filter by keyword (hybrid search) and severity level. Results
    are sorted by relevance score (descending).

    Args:
        query: Natural-language query string, e.g. ``"pod keeps restarting"``
        limit: Maximum number of results to return (1–20).
        keyword: Optional keyword that must appear in the returned runbook chunks.
        severity: Optional severity level filter (applied post-retrieval).

    Returns:
        List of RunbookResult objects ordered by similarity score.

    Raises:
        HTTPException 400: If the query is empty.
        HTTPException 500: If the RAG retrieval fails.
    """
    if not query.strip():
        raise HTTPException(status_code=400, detail="Query parameter must not be empty.")

    logger.info(
        "GET /runbooks | query={!r} limit={} keyword={!r} severity={!r}",
        query[:80],
        limit,
        keyword,
        severity,
    )

    try:
        retriever = _get_rag_retriever()
        raw_results = retriever.hybrid_search(
            text=query,
            keyword=keyword,
            n_results=limit * 2,  # Fetch extra to allow for severity filtering
        )
    except Exception as exc:
        logger.error("search_runbooks | RAG retrieval failed: {}", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Runbook search failed: {str(exc)}",
        ) from exc

    # Apply severity filter if requested
    if severity:
        raw_results = [r for r in raw_results if r.get("severity", "").lower() == severity.lower()]

    # Sort by score descending and cap at limit
    raw_results.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    raw_results = raw_results[:limit]

    logger.info("search_runbooks | returning {} results", len(raw_results))

    return [
        RunbookResult(
            runbook_name=r.get("runbook_name", "unknown"),
            content=r.get("content", ""),
            score=r.get("score", 0.0),
            severity=r.get("severity", "medium"),
            category=r.get("category", "general"),
        )
        for r in raw_results
    ]


@app.get(
    "/runbooks/{runbook_name}",
    summary="Get a specific runbook by name",
    tags=["Runbooks"],
)
async def get_runbook(runbook_name: str) -> Dict[str, Any]:
    """
    Retrieve all chunks for a specific runbook by its name (filename stem).

    Args:
        runbook_name: Runbook filename stem, e.g. ``"crashloopbackoff"`` or
            ``"high-cpu-usage"``.

    Returns:
        Dict with ``runbook_name``, ``chunks`` (list), and ``metadata``.

    Raises:
        HTTPException 404: If no runbook chunks with the given name are found.
        HTTPException 500: If the retrieval fails.
    """
    logger.info("GET /runbooks/{} | fetching chunks", runbook_name)

    try:
        retriever = _get_rag_retriever()
        # Use keyword filter to fetch all chunks for this runbook
        results = retriever.keyword_filter(
            text=runbook_name.replace("-", " "),
            keyword=runbook_name,
        )
    except Exception as exc:
        logger.error("get_runbook | retrieval failed: {}", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Runbook retrieval failed: {str(exc)}",
        ) from exc

    # Filter to only exact runbook name matches
    matched = [r for r in results if r.get("runbook_name") == runbook_name]

    if not matched:
        raise HTTPException(
            status_code=404,
            detail=f"Runbook '{runbook_name}' not found in the knowledge base.",
        )

    return {
        "runbook_name": runbook_name,
        "chunk_count": len(matched),
        "severity": matched[0].get("severity", "unknown"),
        "category": matched[0].get("category", "general"),
        "chunks": [
            {
                "chunk_index": r.get("chunk_index", 0),
                "content": r.get("content", ""),
            }
            for r in sorted(matched, key=lambda x: x.get("chunk_index", 0))
        ],
    }


@app.delete(
    "/chat/{session_id}",
    summary="Clear a chat session",
    tags=["Chat"],
)
async def clear_chat_session(session_id: str) -> Dict[str, str]:
    """
    Delete the conversation history for a given session.

    Args:
        session_id: The session ID to clear.

    Returns:
        Confirmation message.
    """
    if session_id in _CHAT_SESSIONS:
        del _CHAT_SESSIONS[session_id]
        logger.info("clear_chat_session | session_id={} cleared", session_id)
        return {"message": f"Session '{session_id}' cleared."}
    return {"message": f"Session '{session_id}' not found (already empty)."}


# ---------------------------------------------------------------------------
# Application startup / shutdown events
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    """Log startup confirmation."""
    logger.info("SRE Agent Platform API starting up — version=1.0.0")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Log shutdown."""
    logger.info("SRE Agent Platform API shutting down.")
