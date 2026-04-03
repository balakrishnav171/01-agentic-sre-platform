"""
A2A task router — delegates tasks between agents based on capability.
"""
from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional, Tuple
import httpx
from loguru import logger

from a2a.agent_registry import AgentCard, AgentRegistry, Task, TaskStatus


# ---------------------------------------------------------------------------
# Alert → capability mapping
# ---------------------------------------------------------------------------

# Maps alert category keywords to the capability that should handle them.
ALERT_CAPABILITY_MAP: List[Tuple[str, str]] = [
    # (keyword_in_alert_type, capability)
    ("pod", "pod_diagnosis"),
    ("deployment", "deployment_scaling"),
    ("node", "node_inspection"),
    ("container", "pod_diagnosis"),
    ("oom", "pod_diagnosis"),
    ("cpu", "metrics_analysis"),
    ("memory", "metrics_analysis"),
    ("latency", "metrics_analysis"),
    ("error_rate", "metrics_analysis"),
    ("anomaly", "anomaly_detection"),
    ("cost", "cost_optimization"),
    ("incident", "incident_creation"),
    ("alert", "incident_creation"),
    ("escalat", "escalation"),
]

DEFAULT_CAPABILITY = "incident_creation"
HTTP_TIMEOUT = 30  # seconds


class TaskRouter:
    """Routes alert payloads and generic task inputs to the best-matching agent."""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def route_alert(self, alert_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Determine which agent should handle an alert and delegate to it.

        The alert_data dict should contain at minimum:
          - alert_type (str)  — e.g. "pod_crash", "high_cpu"
          - description (str) — human-readable alert description
        Additional keys are passed through to the agent as task input.
        """
        alert_type: str = alert_data.get("alert_type", "")
        capability = self._resolve_capability(alert_type)
        logger.info(
            f"Routing alert '{alert_type}' → capability '{capability}'"
        )
        return await self.delegate_task(capability, alert_data)

    async def delegate_task(
        self,
        capability: str,
        input_data: Dict[str, Any],
        preferred_agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Submit input_data to the best agent that supports the given capability.

        If preferred_agent_id is provided and that agent has the capability,
        it is selected first.  Otherwise the first registered match is used.

        Returns a dict with task_id, agent_id, status, and the agent's response.
        """
        agents = self.registry.find_agents_for_capability(capability)
        if not agents:
            logger.error(f"No agents found for capability '{capability}'")
            return {
                "status": "error",
                "error": f"No agent registered for capability: {capability}",
                "capability": capability,
            }

        agent = self._select_agent(agents, preferred_agent_id)
        task_id = str(uuid.uuid4())
        task = Task(
            task_id=task_id,
            agent_id=agent.agent_id,
            input=input_data,
            status=TaskStatus.SUBMITTED,
        )
        self.registry.submit_task(task)

        logger.info(
            f"Delegating task {task_id} to agent '{agent.agent_id}' "
            f"at {agent.endpoint}"
        )
        self.registry.update_task_status(task_id, TaskStatus.WORKING)

        try:
            response = await self._call_agent(agent, input_data)
            self.registry.update_task_status(
                task_id, TaskStatus.COMPLETED, output=response
            )
            logger.info(f"Task {task_id} completed successfully")
            return {
                "status": "completed",
                "task_id": task_id,
                "agent_id": agent.agent_id,
                "agent_name": agent.name,
                "response": response,
            }
        except Exception as exc:
            error_msg = str(exc)
            self.registry.update_task_status(
                task_id, TaskStatus.FAILED, error=error_msg
            )
            logger.error(f"Task {task_id} failed: {error_msg}")
            return {
                "status": "failed",
                "task_id": task_id,
                "agent_id": agent.agent_id,
                "error": error_msg,
            }

    async def broadcast_task(
        self,
        capability: str,
        input_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Delegate the same task to ALL agents that have the capability and
        return a list of results (one per agent).
        """
        agents = self.registry.find_agents_for_capability(capability)
        if not agents:
            return [
                {
                    "status": "error",
                    "error": f"No agent registered for capability: {capability}",
                }
            ]
        results = []
        for agent in agents:
            result = await self.delegate_task(
                capability, input_data, preferred_agent_id=agent.agent_id
            )
            results.append(result)
        return results

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return current status and output/error for a task."""
        task = self.registry.get_task(task_id)
        if not task:
            return None
        return {
            "task_id": task.task_id,
            "agent_id": task.agent_id,
            "status": task.status.value,
            "output": task.output,
            "error": task.error,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_capability(self, alert_type: str) -> str:
        """Map an alert_type string to the most appropriate capability."""
        lower = alert_type.lower()
        for keyword, cap in ALERT_CAPABILITY_MAP:
            if keyword in lower:
                return cap
        logger.debug(
            f"No capability mapping for '{alert_type}', defaulting to '{DEFAULT_CAPABILITY}'"
        )
        return DEFAULT_CAPABILITY

    def _select_agent(
        self,
        agents: List[AgentCard],
        preferred_agent_id: Optional[str],
    ) -> AgentCard:
        """
        Select an agent from the list.  If preferred_agent_id is in the list,
        use it; otherwise fall back to the first registered agent.
        """
        if preferred_agent_id:
            for agent in agents:
                if agent.agent_id == preferred_agent_id:
                    return agent
        return agents[0]

    async def _call_agent(
        self,
        agent: AgentCard,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Send a POST request to the agent's /tasks/execute endpoint
        and return the parsed JSON response.
        """
        url = f"{agent.endpoint}/tasks/execute"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(url, json=input_data)
            response.raise_for_status()
            return response.json()
