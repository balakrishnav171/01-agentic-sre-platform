"""
kubernetes_agent.py
-------------------
LangChain ReAct agent for Kubernetes diagnosis and remediation.

Provides @tool-decorated functions that make real calls against the
Kubernetes API (via the official ``kubernetes`` Python client).  The
agent uses GPT-4o and the ReAct pattern to reason over pod/node
status, logs, and events before deciding on a corrective action.
"""

from __future__ import annotations

import os
import textwrap
from typing import Any, Optional

from langchain.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from loguru import logger

# ---------------------------------------------------------------------------
# Kubernetes client bootstrap
# ---------------------------------------------------------------------------

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.rest import ApiException

    def _load_k8s_config() -> None:
        """Load in-cluster config, falling back to kubeconfig."""
        try:
            k8s_config.load_incluster_config()
            logger.info("k8s config loaded from in-cluster service account")
        except k8s_config.ConfigException:
            kubeconfig = os.environ.get("KUBECONFIG")
            k8s_config.load_kube_config(config_file=kubeconfig)
            logger.info("k8s config loaded from kubeconfig file")

    _load_k8s_config()
    _K8S_AVAILABLE = True
except Exception as _k8s_boot_err:  # pragma: no cover
    logger.warning("kubernetes client not available: {} — tools will return mock data", _k8s_boot_err)
    _K8S_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helper: truncate long strings so tool outputs stay within token limits
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n...[truncated {len(text) - max_chars} chars]...\n" + text[-half:]


# ---------------------------------------------------------------------------
# LangChain Tools
# ---------------------------------------------------------------------------

@tool
def get_pod_status(namespace_and_pod: str) -> str:
    """
    Retrieve the current status of a Kubernetes pod.

    Args:
        namespace_and_pod: Colon-separated ``namespace:pod_name``.
            Example: ``production:api-server-7d9f4b-xz9kp``.

    Returns:
        JSON-like string with phase, conditions, container statuses,
        restart counts, and node assignment.
    """
    parts = namespace_and_pod.split(":", 1)
    if len(parts) != 2:
        return f"ERROR: expected 'namespace:pod_name', got {namespace_and_pod!r}"

    namespace, pod_name = parts[0].strip(), parts[1].strip()
    logger.info("get_pod_status | ns={} pod={}", namespace, pod_name)

    if not _K8S_AVAILABLE:
        return (
            f"MOCK | pod={pod_name} namespace={namespace} "
            "phase=Running restarts=3 conditions=[Ready=True]"
        )

    try:
        v1 = k8s_client.CoreV1Api()
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        status = pod.status

        containers = []
        for cs in (status.container_statuses or []):
            state_str = "unknown"
            if cs.state.running:
                state_str = f"running since {cs.state.running.started_at}"
            elif cs.state.waiting:
                state_str = f"waiting reason={cs.state.waiting.reason}"
            elif cs.state.terminated:
                state_str = (
                    f"terminated exit_code={cs.state.terminated.exit_code} "
                    f"reason={cs.state.terminated.reason}"
                )
            containers.append(
                f"  {cs.name}: ready={cs.ready} restarts={cs.restart_count} state={state_str}"
            )

        conditions = [
            f"{c.type}={c.status}" for c in (status.conditions or [])
        ]

        return "\n".join([
            f"Pod: {pod_name}",
            f"Namespace: {namespace}",
            f"Phase: {status.phase}",
            f"Node: {pod.spec.node_name}",
            f"Conditions: {', '.join(conditions)}",
            "Container statuses:",
        ] + containers)
    except ApiException as exc:
        logger.error("get_pod_status | ApiException: {}", exc)
        return f"ERROR: Kubernetes API error {exc.status}: {exc.reason}"
    except Exception as exc:
        logger.error("get_pod_status | unexpected error: {}", exc)
        return f"ERROR: {exc}"


@tool
def get_pod_logs(namespace_pod_lines: str) -> str:
    """
    Fetch the most recent log lines from a Kubernetes pod container.

    Args:
        namespace_pod_lines: Format ``namespace:pod_name:tail_lines``.
            ``tail_lines`` defaults to 100 if omitted.
            Example: ``production:api-server-7d9f4b-xz9kp:200``.

    Returns:
        Recent log output (truncated to ~4000 chars).
    """
    parts = namespace_pod_lines.split(":", 2)
    if len(parts) < 2:
        return f"ERROR: expected 'namespace:pod_name[:lines]', got {namespace_pod_lines!r}"

    namespace = parts[0].strip()
    pod_name = parts[1].strip()
    tail_lines = int(parts[2].strip()) if len(parts) == 3 else 100

    logger.info("get_pod_logs | ns={} pod={} lines={}", namespace, pod_name, tail_lines)

    if not _K8S_AVAILABLE:
        return (
            f"MOCK LOGS | pod={pod_name}\n"
            "[ERROR] connection refused to database\n"
            "[WARN]  memory usage at 92%\n"
            "[ERROR] OOMKilled signal received\n"
        )

    try:
        v1 = k8s_client.CoreV1Api()
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            timestamps=True,
            _preload_content=True,
        )
        return _truncate(logs or "(empty log output)")
    except ApiException as exc:
        # Pod may have been restarted; try previous container
        if exc.status == 400:
            try:
                v1 = k8s_client.CoreV1Api()
                logs = v1.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    tail_lines=tail_lines,
                    previous=True,
                    timestamps=True,
                    _preload_content=True,
                )
                return "[PREVIOUS CONTAINER LOGS]\n" + _truncate(logs or "(empty)")
            except Exception:
                pass
        logger.error("get_pod_logs | ApiException: {}", exc)
        return f"ERROR: {exc.status} {exc.reason}"
    except Exception as exc:
        logger.error("get_pod_logs | unexpected error: {}", exc)
        return f"ERROR: {exc}"


@tool
def restart_pod(namespace_and_pod: str) -> str:
    """
    Restart a Kubernetes pod by deleting it (the controller recreates it).

    Args:
        namespace_and_pod: ``namespace:pod_name``.
            Example: ``production:api-server-7d9f4b-xz9kp``.

    Returns:
        Confirmation string or error message.
    """
    parts = namespace_and_pod.split(":", 1)
    if len(parts) != 2:
        return f"ERROR: expected 'namespace:pod_name', got {namespace_and_pod!r}"

    namespace, pod_name = parts[0].strip(), parts[1].strip()
    logger.info("restart_pod | ns={} pod={}", namespace, pod_name)

    if not _K8S_AVAILABLE:
        return f"MOCK | pod {pod_name} in namespace {namespace} deleted (will be recreated by controller)"

    try:
        v1 = k8s_client.CoreV1Api()
        v1.delete_namespaced_pod(
            name=pod_name,
            namespace=namespace,
            body=k8s_client.V1DeleteOptions(grace_period_seconds=0),
        )
        return f"Pod {pod_name} deleted from namespace {namespace}. Controller will recreate it."
    except ApiException as exc:
        logger.error("restart_pod | ApiException: {}", exc)
        return f"ERROR: {exc.status} {exc.reason}"
    except Exception as exc:
        logger.error("restart_pod | unexpected error: {}", exc)
        return f"ERROR: {exc}"


@tool
def scale_deployment(namespace_deployment_replicas: str) -> str:
    """
    Scale a Kubernetes Deployment to a specified number of replicas.

    Args:
        namespace_deployment_replicas: ``namespace:deployment_name:replicas``.
            Example: ``production:api-server:3``.

    Returns:
        Confirmation string or error message.
    """
    parts = namespace_deployment_replicas.split(":", 2)
    if len(parts) != 3:
        return (
            f"ERROR: expected 'namespace:deployment_name:replicas', "
            f"got {namespace_deployment_replicas!r}"
        )

    namespace, deployment, replicas_str = parts[0].strip(), parts[1].strip(), parts[2].strip()
    try:
        replicas = int(replicas_str)
    except ValueError:
        return f"ERROR: replicas must be an integer, got {replicas_str!r}"

    logger.info("scale_deployment | ns={} deployment={} replicas={}", namespace, deployment, replicas)

    if not _K8S_AVAILABLE:
        return f"MOCK | deployment {deployment} in {namespace} scaled to {replicas} replicas"

    try:
        apps_v1 = k8s_client.AppsV1Api()
        body = {"spec": {"replicas": replicas}}
        apps_v1.patch_namespaced_deployment_scale(
            name=deployment,
            namespace=namespace,
            body=body,
        )
        return f"Deployment {deployment} in {namespace} scaled to {replicas} replicas."
    except ApiException as exc:
        logger.error("scale_deployment | ApiException: {}", exc)
        return f"ERROR: {exc.status} {exc.reason}"
    except Exception as exc:
        logger.error("scale_deployment | unexpected error: {}", exc)
        return f"ERROR: {exc}"


@tool
def get_node_status(node_name: str) -> str:
    """
    Retrieve the status and resource capacity of a Kubernetes node.

    Args:
        node_name: Name of the node.  Use ``__all__`` to list all nodes.

    Returns:
        Node conditions, allocatable resources, and taints.
    """
    logger.info("get_node_status | node={}", node_name)

    if not _K8S_AVAILABLE:
        return (
            f"MOCK | node={node_name} Ready=True "
            "cpu=8 memory=32Gi pods=110 unschedulable=False"
        )

    try:
        v1 = k8s_client.CoreV1Api()

        if node_name == "__all__":
            nodes = v1.list_node().items
        else:
            nodes = [v1.read_node(name=node_name)]

        lines = []
        for node in nodes:
            conditions = {c.type: c.status for c in (node.status.conditions or [])}
            allocatable = node.status.allocatable or {}
            taints = [
                f"{t.key}={t.value}:{t.effect}" for t in (node.spec.taints or [])
            ]
            lines.append(
                f"Node: {node.metadata.name}\n"
                f"  Conditions: {conditions}\n"
                f"  Allocatable: cpu={allocatable.get('cpu')} "
                f"memory={allocatable.get('memory')} "
                f"pods={allocatable.get('pods')}\n"
                f"  Taints: {taints or 'none'}\n"
                f"  Unschedulable: {node.spec.unschedulable}"
            )
        return "\n\n".join(lines)
    except ApiException as exc:
        logger.error("get_node_status | ApiException: {}", exc)
        return f"ERROR: {exc.status} {exc.reason}"
    except Exception as exc:
        logger.error("get_node_status | unexpected error: {}", exc)
        return f"ERROR: {exc}"


@tool
def get_events(namespace_and_name: str) -> str:
    """
    Retrieve Kubernetes events related to a pod or namespace.

    Args:
        namespace_and_name: ``namespace:resource_name``.
            Omit resource_name to get all events in namespace.
            Example: ``production:api-server-7d9f4b-xz9kp``.

    Returns:
        Recent warning and normal events, sorted by last timestamp.
    """
    parts = namespace_and_name.split(":", 1)
    namespace = parts[0].strip()
    resource_name = parts[1].strip() if len(parts) == 2 else ""

    logger.info("get_events | ns={} resource={}", namespace, resource_name)

    if not _K8S_AVAILABLE:
        return (
            f"MOCK EVENTS | ns={namespace} resource={resource_name}\n"
            "Warning  OOMKilling  api-server  Container api was OOM-killed\n"
            "Warning  BackOff     api-server  Back-off restarting failed container\n"
            "Normal   Pulled      api-server  Successfully pulled image\n"
        )

    try:
        v1 = k8s_client.CoreV1Api()
        field_selector = f"involvedObject.namespace={namespace}"
        if resource_name:
            field_selector += f",involvedObject.name={resource_name}"

        event_list = v1.list_namespaced_event(
            namespace=namespace,
            field_selector=field_selector,
        )
        events = sorted(
            event_list.items,
            key=lambda e: e.last_timestamp or e.event_time or "",
            reverse=True,
        )[:30]

        lines = []
        for evt in events:
            lines.append(
                f"{evt.type:<8} {evt.reason:<20} {evt.involved_object.name:<30} "
                f"{evt.message}"
            )

        return "\n".join(lines) if lines else "No events found."
    except ApiException as exc:
        logger.error("get_events | ApiException: {}", exc)
        return f"ERROR: {exc.status} {exc.reason}"
    except Exception as exc:
        logger.error("get_events | unexpected error: {}", exc)
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# ReAct prompt template
# ---------------------------------------------------------------------------

_REACT_TEMPLATE = textwrap.dedent("""\
    You are an expert Site Reliability Engineer specialising in Kubernetes.
    Your job is to diagnose and remediate infrastructure issues using the
    available tools.

    TOOLS AVAILABLE:
    {tools}

    TOOL NAMES: {tool_names}

    FORMAT:
    Question: the input question you must answer
    Thought: your reasoning about what to do
    Action: the tool to use (must be one of [{tool_names}])
    Action Input: the input to the tool
    Observation: the result of the action
    ... (repeat Thought/Action/Action Input/Observation as needed)
    Thought: I now know the final answer
    Final Answer: your comprehensive diagnosis and recommended actions

    Begin!

    Question: {input}
    Thought:{agent_scratchpad}
""")

_REACT_PROMPT = PromptTemplate.from_template(_REACT_TEMPLATE)


# ---------------------------------------------------------------------------
# KubernetesAgent
# ---------------------------------------------------------------------------

class KubernetesAgent:
    """
    LangChain ReAct agent for Kubernetes diagnosis and remediation.

    Wraps the six k8s tools (get_pod_status, get_pod_logs, restart_pod,
    scale_deployment, get_node_status, get_events) in a GPT-4o ReAct loop.

    Usage::

        agent = KubernetesAgent()
        diagnosis = agent.diagnose_issue(state)
        result    = agent.remediate(state)
    """

    _TOOLS = [
        get_pod_status,
        get_pod_logs,
        restart_pod,
        scale_deployment,
        get_node_status,
        get_events,
    ]

    def __init__(
        self,
        model: str = "gpt-4o",
        temperature: float = 0.0,
        max_iterations: int = 10,
        openai_api_key: Optional[str] = None,
    ) -> None:
        """
        Initialise the KubernetesAgent.

        Args:
            model: OpenAI chat model to use.
            temperature: LLM temperature.
            max_iterations: Maximum ReAct loop iterations.
            openai_api_key: Optional API key override.
        """
        api_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
        self.llm = ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=api_key,
        )
        self.max_iterations = max_iterations

        react_agent = create_react_agent(
            llm=self.llm,
            tools=self._TOOLS,
            prompt=_REACT_PROMPT,
        )
        self.executor = AgentExecutor(
            agent=react_agent,
            tools=self._TOOLS,
            verbose=True,
            max_iterations=max_iterations,
            handle_parsing_errors=True,
            return_intermediate_steps=True,
        )
        logger.info("KubernetesAgent initialised | model={}", model)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def diagnose_issue(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        Run the ReAct agent to diagnose a Kubernetes issue.

        The agent will call tools iteratively until it reaches a conclusion.

        Args:
            state: Orchestrator state dict containing at minimum
                ``namespace``, ``pod_name``, ``service_name``, and
                ``raw_alert``.

        Returns:
            Dict with keys: ``output`` (str), ``intermediate_steps`` (list),
            ``pod_name``, ``namespace``.
        """
        namespace = state.get("namespace", "default")
        pod_name = state.get("pod_name", "")
        service_name = state.get("service_name", "")
        alert_desc = state.get("raw_alert", {}).get("description", "No description provided.")
        rag_context = state.get("rag_context", "")

        question = (
            f"Diagnose the following Kubernetes issue and provide a root cause analysis.\n\n"
            f"Namespace: {namespace}\n"
            f"Pod: {pod_name}\n"
            f"Service: {service_name}\n"
            f"Alert description: {alert_desc}\n"
            f"Runbook context:\n{rag_context[:1000]}\n\n"
            f"Please check pod status, logs, node health, and recent events. "
            f"Identify the root cause."
        )

        logger.info(
            "diagnose_issue | ns={} pod={} service={}",
            namespace, pod_name, service_name,
        )

        try:
            result = self.executor.invoke({"input": question})
            return {
                "output": result.get("output", ""),
                "intermediate_steps": result.get("intermediate_steps", []),
                "pod_name": pod_name,
                "namespace": namespace,
            }
        except Exception as exc:
            logger.error("diagnose_issue | error: {}", exc)
            return {
                "output": f"Diagnosis failed: {exc}",
                "intermediate_steps": [],
                "pod_name": pod_name,
                "namespace": namespace,
                "error": str(exc),
            }

    def remediate(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        Run the ReAct agent to apply a corrective action.

        Based on the alert context the agent will decide whether to restart
        the pod, scale the deployment, or take another action.

        Args:
            state: Orchestrator state dict. Uses ``namespace``, ``pod_name``,
                ``service_name``, ``severity``, ``raw_alert``, and
                ``remediation_steps``.

        Returns:
            Dict with keys: ``output`` (str), ``actions_taken`` (list),
            ``success`` (bool).
        """
        namespace = state.get("namespace", "default")
        pod_name = state.get("pod_name", "")
        service_name = state.get("service_name", "")
        severity = state.get("severity", "medium")
        alert_desc = state.get("raw_alert", {}).get("description", "")
        remediation_hints = state.get("remediation_steps", [])

        question = (
            f"Remediate the following Kubernetes issue.\n\n"
            f"Namespace: {namespace}\n"
            f"Pod: {pod_name}\n"
            f"Service: {service_name}\n"
            f"Severity: {severity}\n"
            f"Description: {alert_desc}\n"
            f"Suggested remediation steps from runbook:\n"
            + "\n".join(f"  - {s}" for s in remediation_hints[:5])
            + "\n\nApply the most appropriate remediation. "
            "If the pod is crash-looping, restart it. "
            "If traffic is too high, scale the deployment. "
            "Use get_events and get_pod_status to confirm your actions took effect."
        )

        logger.info(
            "remediate | ns={} pod={} severity={}",
            namespace, pod_name, severity,
        )

        try:
            result = self.executor.invoke({"input": question})
            steps = result.get("intermediate_steps", [])
            actions = [
                {"tool": action.tool, "input": action.tool_input, "output": obs}
                for action, obs in steps
            ]
            return {
                "output": result.get("output", ""),
                "actions_taken": actions,
                "success": True,
            }
        except Exception as exc:
            logger.error("remediate | error: {}", exc)
            return {
                "output": f"Remediation failed: {exc}",
                "actions_taken": [],
                "success": False,
                "error": str(exc),
            }
