"""
A2A (Agent-to-Agent) registry for agent discovery and task routing.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger


class TaskStatus(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentCard:
    agent_id: str
    name: str
    description: str
    capabilities: List[str]
    endpoint: str
    version: str
    skills: List[Dict]


@dataclass
class Task:
    task_id: str
    agent_id: str
    input: Dict
    status: TaskStatus = TaskStatus.SUBMITTED
    output: Optional[Dict] = None
    error: Optional[str] = None


class AgentRegistry:
    """Registry for A2A agent discovery and task lifecycle management."""

    def __init__(self, cards_dir: str = "a2a/agent_cards"):
        self.agents: Dict[str, AgentCard] = {}
        self.tasks: Dict[str, Task] = {}
        self._load_cards(cards_dir)

    # ------------------------------------------------------------------
    # Agent card management
    # ------------------------------------------------------------------

    def _load_cards(self, cards_dir: str) -> None:
        """Load all agent cards from JSON files in the given directory."""
        path = Path(cards_dir)
        if not path.exists():
            logger.warning(f"Agent cards dir not found: {cards_dir}")
            return
        for f in path.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                card = AgentCard(**data)
                self.agents[card.agent_id] = card
                logger.info(f"Registered agent: {card.agent_id}")
            except Exception as e:
                logger.error(f"Failed to load agent card {f}: {e}")

    def register_agent(self, card: AgentCard) -> None:
        """Dynamically register a new agent at runtime."""
        self.agents[card.agent_id] = card
        logger.info(f"Registered new agent: {card.agent_id}")

    def deregister_agent(self, agent_id: str) -> bool:
        """Remove an agent from the registry. Returns True if it existed."""
        if agent_id in self.agents:
            del self.agents[agent_id]
            logger.info(f"Deregistered agent: {agent_id}")
            return True
        logger.warning(f"Attempted to deregister unknown agent: {agent_id}")
        return False

    def get_agent(self, agent_id: str) -> Optional[AgentCard]:
        """Look up a single agent by ID."""
        return self.agents.get(agent_id)

    def list_agents(self) -> List[AgentCard]:
        """Return all registered agents."""
        return list(self.agents.values())

    def find_agents_for_capability(self, capability: str) -> List[AgentCard]:
        """Return all agents that declare the given capability."""
        return [a for a in self.agents.values() if capability in a.capabilities]

    # ------------------------------------------------------------------
    # Task lifecycle management
    # ------------------------------------------------------------------

    def submit_task(self, task: Task) -> str:
        """Store a task and return its task_id."""
        self.tasks[task.task_id] = task
        logger.info(f"Task {task.task_id} submitted to agent {task.agent_id}")
        return task.task_id

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        output: Optional[Dict] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update the status (and optionally result/error) of a task."""
        if task_id not in self.tasks:
            logger.warning(f"update_task_status: unknown task_id {task_id}")
            return
        self.tasks[task_id].status = status
        if output is not None:
            self.tasks[task_id].output = output
        if error is not None:
            self.tasks[task_id].error = error
        logger.debug(f"Task {task_id} -> {status.value}")

    def get_task(self, task_id: str) -> Optional[Task]:
        """Retrieve a task by ID."""
        return self.tasks.get(task_id)

    def list_tasks(
        self,
        agent_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> List[Task]:
        """List tasks, optionally filtered by agent and/or status."""
        tasks = list(self.tasks.values())
        if agent_id:
            tasks = [t for t in tasks if t.agent_id == agent_id]
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def cancel_task(self, task_id: str) -> bool:
        """Mark a task as cancelled. Returns True if it existed and was active."""
        task = self.tasks.get(task_id)
        if not task:
            logger.warning(f"cancel_task: unknown task_id {task_id}")
            return False
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            logger.info(f"Task {task_id} already in terminal state {task.status.value}")
            return False
        task.status = TaskStatus.CANCELLED
        logger.info(f"Task {task_id} cancelled")
        return True
