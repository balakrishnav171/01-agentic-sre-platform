"""
incident_agent.py
-----------------
ServiceNow incident management agent for the Agentic SRE Platform.

Provides CRUD operations against the ServiceNow Table API:
  - create_incident   → returns incident number (e.g. INC0012345)
  - update_incident   → patch work notes / short description
  - get_incident_status → current state + assignment info
  - close_incident    → resolve with resolution notes

Uses ``httpx`` for async-compatible HTTP calls with retry logic backed
by ``tenacity`` (exponential backoff with jitter).

Dependencies:
    pip install httpx tenacity loguru
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    RetryError,
)
import logging

# ---------------------------------------------------------------------------
# ServiceNow state codes
# ---------------------------------------------------------------------------

# https://developer.servicenow.com/dev.do#!/reference/api/latest/rest/c_TableAPI
_STATE_MAP: dict[str, int] = {
    "new": 1,
    "in_progress": 2,
    "on_hold": 3,
    "resolved": 6,
    "closed": 7,
    "cancelled": 8,
}

_STATE_REVERSE: dict[int, str] = {v: k for k, v in _STATE_MAP.items()}

# Map SRE severity → ServiceNow impact / urgency
_SEVERITY_IMPACT: dict[str, tuple[int, int]] = {
    "critical": (1, 1),  # (impact, urgency)
    "high": (2, 2),
    "medium": (2, 3),
    "low": (3, 3),
    "unknown": (3, 3),
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ServiceNowError(Exception):
    """Raised when the ServiceNow API returns an error response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"ServiceNow API error {status_code}: {message}")


class IncidentNotFoundError(ServiceNowError):
    """Raised when an incident number cannot be found."""


# ---------------------------------------------------------------------------
# Retry predicate
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient errors that should be retried."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.NetworkError):
        return True
    if isinstance(exc, ServiceNowError) and exc.status_code in {429, 500, 502, 503, 504}:
        return True
    return False


# ---------------------------------------------------------------------------
# IncidentAgent
# ---------------------------------------------------------------------------

class IncidentAgent:
    """
    ServiceNow incident management agent.

    All HTTP calls use ``httpx.Client`` (sync) with per-call retry logic.
    Set ``SNOW_INSTANCE``, ``SNOW_USER``, and ``SNOW_PASSWORD`` (or
    ``SNOW_TOKEN``) in the environment, or pass them to the constructor.

    Usage::

        agent = IncidentAgent()
        number = agent.create_incident(alert_data)
        agent.update_incident(number, {"work_notes": "Diagnosis complete."})
        status = agent.get_incident_status(number)
        agent.close_incident(number, "Root cause: OOMKill. Restarted pod.")
    """

    _TABLE_URL = "https://{instance}.service-now.com/api/now/table/incident"
    _TIMEOUT = 30.0  # seconds
    _MAX_RETRIES = 4
    _RETRY_MIN_WAIT = 1   # seconds
    _RETRY_MAX_WAIT = 30  # seconds

    def __init__(
        self,
        instance: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        bearer_token: Optional[str] = None,
        caller_id: Optional[str] = None,
        assignment_group: Optional[str] = None,
    ) -> None:
        """
        Initialise the IncidentAgent.

        Credentials are resolved from arguments first, then environment
        variables (``SNOW_INSTANCE``, ``SNOW_USER``, ``SNOW_PASSWORD``,
        ``SNOW_TOKEN``).

        Args:
            instance: ServiceNow instance subdomain (e.g. ``acmecorp``).
            username: Basic-auth username.
            password: Basic-auth password.
            bearer_token: OAuth2 bearer token (preferred over basic-auth).
            caller_id: Default caller sys_id for new incidents.
            assignment_group: Default assignment group for new incidents.
        """
        self.instance = instance or os.environ.get("SNOW_INSTANCE", "dev")
        self.username = username or os.environ.get("SNOW_USER", "")
        self.password = password or os.environ.get("SNOW_PASSWORD", "")
        self.bearer_token = bearer_token or os.environ.get("SNOW_TOKEN", "")
        self.caller_id = caller_id or os.environ.get("SNOW_CALLER_ID", "")
        self.assignment_group = (
            assignment_group
            or os.environ.get("SNOW_ASSIGNMENT_GROUP", "SRE Team")
        )

        self._base_url = self._TABLE_URL.format(instance=self.instance)

        logger.info(
            "IncidentAgent initialised | instance={} auth={}",
            self.instance,
            "bearer" if self.bearer_token else "basic",
        )

    # ------------------------------------------------------------------
    # HTTP client
    # ------------------------------------------------------------------

    def _build_client(self) -> httpx.Client:
        """Build a configured httpx client with auth headers."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        auth: Optional[tuple[str, str]] = None

        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.username and self.password:
            auth = (self.username, self.password)

        return httpx.Client(
            headers=headers,
            auth=auth,
            timeout=self._TIMEOUT,
        )

    def _handle_response(self, response: httpx.Response, context: str) -> dict[str, Any]:
        """
        Parse and validate an httpx Response.

        Args:
            response: The HTTP response object.
            context: Human-readable label for error messages.

        Returns:
            Parsed JSON body as a dict.

        Raises:
            IncidentNotFoundError: For 404 responses.
            ServiceNowError: For other non-2xx responses.
        """
        if response.status_code == 404:
            raise IncidentNotFoundError(404, f"{context}: resource not found")
        if not response.is_success:
            try:
                body = response.json()
                msg = body.get("error", {}).get("message", response.text)
            except Exception:
                msg = response.text
            raise ServiceNowError(response.status_code, f"{context}: {msg}")

        return response.json()

    # ------------------------------------------------------------------
    # Retry-wrapped internal helpers
    # ------------------------------------------------------------------

    def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to the incident table with retry/backoff."""

        @retry(
            retry=retry_if_exception_type(_is_retryable),  # type: ignore[arg-type]
            stop=stop_after_attempt(self._MAX_RETRIES),
            wait=wait_exponential(
                multiplier=1,
                min=self._RETRY_MIN_WAIT,
                max=self._RETRY_MAX_WAIT,
            ),
            before_sleep=before_sleep_log(logging.getLogger("incident_agent"), logging.WARNING),
            reraise=True,
        )
        def _inner() -> dict[str, Any]:
            with self._build_client() as client:
                resp = client.post(self._base_url, json=payload)
            return self._handle_response(resp, "create_incident")

        return _inner()

    def _patch_with_retry(self, sys_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """PATCH an existing incident record."""

        @retry(
            retry=retry_if_exception_type(_is_retryable),  # type: ignore[arg-type]
            stop=stop_after_attempt(self._MAX_RETRIES),
            wait=wait_exponential(
                multiplier=1,
                min=self._RETRY_MIN_WAIT,
                max=self._RETRY_MAX_WAIT,
            ),
            reraise=True,
        )
        def _inner() -> dict[str, Any]:
            url = f"{self._base_url}/{sys_id}"
            with self._build_client() as client:
                resp = client.patch(url, json=payload)
            return self._handle_response(resp, "update_incident")

        return _inner()

    def _get_with_retry(self, params: dict[str, Any]) -> dict[str, Any]:
        """GET from the incident table."""

        @retry(
            retry=retry_if_exception_type(_is_retryable),  # type: ignore[arg-type]
            stop=stop_after_attempt(self._MAX_RETRIES),
            wait=wait_exponential(
                multiplier=1,
                min=self._RETRY_MIN_WAIT,
                max=self._RETRY_MAX_WAIT,
            ),
            reraise=True,
        )
        def _inner() -> dict[str, Any]:
            with self._build_client() as client:
                resp = client.get(self._base_url, params=params)
            return self._handle_response(resp, "get_incident")

        return _inner()

    # ------------------------------------------------------------------
    # Incident number ↔ sys_id resolution
    # ------------------------------------------------------------------

    def _resolve_sys_id(self, number: str) -> str:
        """
        Look up the ServiceNow sys_id for an incident number.

        Args:
            number: Incident number (e.g. ``INC0012345``).

        Returns:
            sys_id string.

        Raises:
            IncidentNotFoundError: If no incident with that number exists.
        """
        params = {
            "sysparm_query": f"number={number}",
            "sysparm_fields": "sys_id,number",
            "sysparm_limit": 1,
        }
        body = self._get_with_retry(params)
        records = body.get("result", [])
        if not records:
            raise IncidentNotFoundError(404, f"Incident {number} not found")
        return records[0]["sys_id"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_incident(self, alert_data: dict[str, Any]) -> str:
        """
        Create a new ServiceNow incident from SRE alert data.

        Constructs an incident with impact, urgency, short_description,
        description, assignment group, and optional caller from the
        alert payload.

        Args:
            alert_data: Dict with keys such as:
                - ``alert_id``        (str)
                - ``alert_type``      (str)
                - ``severity``        (str: critical|high|medium|low)
                - ``service_name``    (str)
                - ``namespace``       (str)
                - ``pod_name``        (str)
                - ``description``     (str, optional)
                - ``rag_context``     (str, optional)
                - ``remediation_steps`` (list[str], optional)

        Returns:
            ServiceNow incident number (e.g. ``INC0012345``).

        Raises:
            ServiceNowError: On unrecoverable API failure.
        """
        severity = alert_data.get("severity", "unknown").lower()
        impact, urgency = _SEVERITY_IMPACT.get(severity, (3, 3))

        service = alert_data.get("service_name", "unknown-service")
        namespace = alert_data.get("namespace", "")
        pod = alert_data.get("pod_name", "")
        alert_id = alert_data.get("alert_id", "")
        alert_type = alert_data.get("alert_type", "unknown")

        short_desc = (
            f"[{severity.upper()}] {alert_type.title()} alert: "
            f"{service}{f'/{pod}' if pod else ''}"
            f"{f' (ns: {namespace})' if namespace else ''}"
        )[:160]

        steps_text = ""
        steps = alert_data.get("remediation_steps", [])
        if steps:
            steps_text = "\n\nSuggested Remediation Steps:\n" + "\n".join(
                f"  {i+1}. {s}" for i, s in enumerate(steps[:10])
            )

        rag_text = ""
        rag = alert_data.get("rag_context", "")
        if rag:
            rag_text = f"\n\nRunbook Context:\n{rag[:2000]}"

        description = (
            f"SRE Platform Automated Incident\n"
            f"Alert ID: {alert_id}\n"
            f"Alert Type: {alert_type}\n"
            f"Severity: {severity}\n"
            f"Service: {service}\n"
            f"Namespace: {namespace}\n"
            f"Pod: {pod}\n"
            f"Detected: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"\nDescription:\n{alert_data.get('description', 'No description provided.')}"
            f"{steps_text}"
            f"{rag_text}"
        )

        payload: dict[str, Any] = {
            "short_description": short_desc,
            "description": description,
            "impact": impact,
            "urgency": urgency,
            "category": "Software",
            "subcategory": alert_type,
            "assignment_group": {"name": self.assignment_group},
            "u_alert_id": alert_id,
        }

        if self.caller_id:
            payload["caller_id"] = {"sys_id": self.caller_id}

        logger.info(
            "create_incident | service={} severity={} impact={} urgency={}",
            service, severity, impact, urgency,
        )

        try:
            body = self._post_with_retry(payload)
            result = body.get("result", {})
            number = result.get("number", "")
            sys_id = result.get("sys_id", "")
            logger.info("create_incident | created {} (sys_id={})", number, sys_id)
            return number
        except (ServiceNowError, RetryError) as exc:
            logger.error("create_incident | failed: {}", exc)
            # In offline/mock mode, return a synthetic number
            if not self.bearer_token and not (self.username and self.password):
                mock_number = f"INC{abs(hash(alert_id)) % 10_000_000:07d}"
                logger.warning("create_incident | offline mode — returning mock number {}", mock_number)
                return mock_number
            raise

    def update_incident(
        self,
        number: str,
        update: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Update an existing ServiceNow incident.

        Accepts any valid ServiceNow incident field in ``update``.
        Common fields: ``work_notes``, ``short_description``,
        ``description``, ``state`` (use string from ``_STATE_MAP``),
        ``assigned_to``, ``assignment_group``.

        Args:
            number: Incident number (e.g. ``INC0012345``).
            update: Dict of field name → new value.

        Returns:
            Updated incident record as a dict.

        Raises:
            IncidentNotFoundError: If the incident does not exist.
            ServiceNowError: On API failure.
        """
        logger.info("update_incident | number={} fields={}", number, list(update.keys()))

        # Convert friendly state strings to numeric codes
        if "state" in update and isinstance(update["state"], str):
            state_code = _STATE_MAP.get(update["state"].lower())
            if state_code is not None:
                update = {**update, "state": state_code}

        # Timestamp work notes automatically
        if "work_notes" in update:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            update = {
                **update,
                "work_notes": f"[{ts}] {update['work_notes']}",
            }

        try:
            sys_id = self._resolve_sys_id(number)
            body = self._patch_with_retry(sys_id, update)
            result = body.get("result", {})
            logger.info("update_incident | {} updated successfully", number)
            return result
        except (ServiceNowError, RetryError) as exc:
            logger.error("update_incident | failed for {}: {}", number, exc)
            raise

    def get_incident_status(self, number: str) -> dict[str, Any]:
        """
        Retrieve the current status and key metadata for an incident.

        Args:
            number: Incident number (e.g. ``INC0012345``).

        Returns:
            Dict with fields: ``number``, ``state``, ``state_label``,
            ``short_description``, ``priority``, ``assigned_to``,
            ``assignment_group``, ``opened_at``, ``updated_at``,
            ``resolved_at``, ``close_notes``, ``sys_id``.

        Raises:
            IncidentNotFoundError: If the incident does not exist.
            ServiceNowError: On API failure.
        """
        logger.info("get_incident_status | number={}", number)

        params = {
            "sysparm_query": f"number={number}",
            "sysparm_fields": (
                "sys_id,number,state,short_description,priority,"
                "assigned_to,assignment_group,opened_at,sys_updated_on,"
                "resolved_at,close_notes,work_notes"
            ),
            "sysparm_limit": 1,
        }

        try:
            body = self._get_with_retry(params)
            records = body.get("result", [])
            if not records:
                raise IncidentNotFoundError(404, f"Incident {number} not found")

            rec = records[0]
            state_code = int(rec.get("state", 1))

            return {
                "number": rec.get("number", number),
                "state": state_code,
                "state_label": _STATE_REVERSE.get(state_code, "unknown"),
                "short_description": rec.get("short_description", ""),
                "priority": rec.get("priority", ""),
                "assigned_to": rec.get("assigned_to", {}).get("display_value", ""),
                "assignment_group": rec.get("assignment_group", {}).get("display_value", ""),
                "opened_at": rec.get("opened_at", ""),
                "updated_at": rec.get("sys_updated_on", ""),
                "resolved_at": rec.get("resolved_at", ""),
                "close_notes": rec.get("close_notes", ""),
                "sys_id": rec.get("sys_id", ""),
            }
        except (ServiceNowError, RetryError) as exc:
            logger.error("get_incident_status | failed for {}: {}", number, exc)
            raise

    def close_incident(self, number: str, resolution: str) -> dict[str, Any]:
        """
        Resolve and close a ServiceNow incident with resolution notes.

        Sets ``state=resolved``, ``close_code=Solved (Permanently)``,
        and ``close_notes`` to the provided resolution text.

        Args:
            number: Incident number (e.g. ``INC0012345``).
            resolution: Human-readable resolution notes explaining root cause
                and actions taken.

        Returns:
            Updated incident record dict (same shape as
            ``get_incident_status``).

        Raises:
            IncidentNotFoundError: If the incident does not exist.
            ServiceNowError: On API failure.
        """
        logger.info("close_incident | number={}", number)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        close_update: dict[str, Any] = {
            "state": _STATE_MAP["resolved"],
            "close_code": "Solved (Permanently)",
            "close_notes": f"[{ts}] Resolved by SRE Agent Platform.\n\n{resolution}",
            "work_notes": (
                f"[{ts}] Incident auto-resolved by SRE Agent Platform.\n"
                f"Resolution: {resolution[:500]}"
            ),
        }

        try:
            sys_id = self._resolve_sys_id(number)
            self._patch_with_retry(sys_id, close_update)
            logger.info("close_incident | {} closed successfully", number)
            return self.get_incident_status(number)
        except (ServiceNowError, RetryError) as exc:
            logger.error("close_incident | failed for {}: {}", number, exc)
            raise
