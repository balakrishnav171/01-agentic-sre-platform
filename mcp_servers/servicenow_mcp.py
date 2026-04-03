"""
MCP Server: ServiceNow incident management.
Provides 5 tools:
  - create_incident
  - update_incident
  - get_incident
  - close_incident
  - search_incidents
"""
from __future__ import annotations
import os
from typing import Any, Dict, List, Optional
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from loguru import logger
import uvicorn

app = FastAPI(title="ServiceNow MCP Server", version="1.0.0")

# ---------------------------------------------------------------------------
# ServiceNow client configuration
# ---------------------------------------------------------------------------

SNOW_INSTANCE = os.getenv("SERVICENOW_INSTANCE", "")        # e.g. "dev12345"
SNOW_USER = os.getenv("SERVICENOW_USERNAME", "")
SNOW_PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")
SNOW_API_TIMEOUT = int(os.getenv("SERVICENOW_TIMEOUT", "30"))
SNOW_MAX_RETRIES = int(os.getenv("SERVICENOW_MAX_RETRIES", "3"))

TABLE_API = f"https://{SNOW_INSTANCE}.service-now.com/api/now/table"


def _base_url(table: str) -> str:
    return f"{TABLE_API}/{table}"


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _request_with_retry(
    method: str,
    url: str,
    *,
    json: Optional[Dict] = None,
    params: Optional[Dict] = None,
    max_retries: int = SNOW_MAX_RETRIES,
) -> Dict[str, Any]:
    """Execute an HTTP request with exponential-backoff retry logic."""
    last_exc: Exception = RuntimeError("Unknown error")
    async with httpx.AsyncClient(
        auth=(SNOW_USER, SNOW_PASSWORD),
        headers=_headers(),
        timeout=SNOW_API_TIMEOUT,
    ) as client:
        for attempt in range(1, max_retries + 1):
            try:
                response = await client.request(
                    method, url, json=json, params=params
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    f"ServiceNow HTTP {exc.response.status_code} on attempt {attempt}/{max_retries}: {exc}"
                )
                last_exc = exc
                if exc.response.status_code < 500:
                    # Client errors should not be retried
                    raise HTTPException(
                        status_code=exc.response.status_code,
                        detail=exc.response.text,
                    )
            except httpx.TransportError as exc:
                logger.warning(
                    f"ServiceNow transport error on attempt {attempt}/{max_retries}: {exc}"
                )
                last_exc = exc
    raise HTTPException(status_code=503, detail=f"ServiceNow unreachable after {max_retries} retries: {last_exc}")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateIncidentRequest(BaseModel):
    short_description: str
    description: str
    urgency: int = Field(default=2, ge=1, le=4, description="1=Critical, 2=High, 3=Medium, 4=Low")
    impact: int = Field(default=2, ge=1, le=3, description="1=High, 2=Medium, 3=Low")
    category: str = "software"
    subcategory: Optional[str] = None
    assignment_group: Optional[str] = None
    assigned_to: Optional[str] = None
    caller_id: Optional[str] = None


class UpdateIncidentRequest(BaseModel):
    sys_id: str
    fields: Dict[str, Any]


class GetIncidentRequest(BaseModel):
    sys_id: str


class CloseIncidentRequest(BaseModel):
    sys_id: str
    resolution_notes: str
    close_code: str = "Solved (Permanently)"


class SearchIncidentsRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = 0


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "servicenow-mcp"}


# ---------------------------------------------------------------------------
# Tool: create_incident
# ---------------------------------------------------------------------------

@app.post("/tools/create_incident")
async def create_incident(req: CreateIncidentRequest) -> Dict[str, Any]:
    """Create a new ServiceNow incident."""
    payload: Dict[str, Any] = {
        "short_description": req.short_description,
        "description": req.description,
        "urgency": str(req.urgency),
        "impact": str(req.impact),
        "category": req.category,
    }
    if req.subcategory:
        payload["subcategory"] = req.subcategory
    if req.assignment_group:
        payload["assignment_group"] = req.assignment_group
    if req.assigned_to:
        payload["assigned_to"] = req.assigned_to
    if req.caller_id:
        payload["caller_id"] = req.caller_id

    result = await _request_with_retry(
        "POST", _base_url("incident"), json=payload
    )
    record = result.get("result", {})
    sys_id = record.get("sys_id", "")
    number = record.get("number", "")
    logger.info(f"Created incident {number} (sys_id={sys_id})")
    return {
        "status": "created",
        "sys_id": sys_id,
        "number": number,
        "short_description": req.short_description,
        "urgency": req.urgency,
        "impact": req.impact,
        "state": record.get("state", ""),
    }


# ---------------------------------------------------------------------------
# Tool: update_incident
# ---------------------------------------------------------------------------

@app.post("/tools/update_incident")
async def update_incident(req: UpdateIncidentRequest) -> Dict[str, Any]:
    """Update fields on an existing ServiceNow incident."""
    url = f"{_base_url('incident')}/{req.sys_id}"
    result = await _request_with_retry("PATCH", url, json=req.fields)
    record = result.get("result", {})
    logger.info(f"Updated incident {req.sys_id}, fields: {list(req.fields.keys())}")
    return {
        "status": "updated",
        "sys_id": req.sys_id,
        "number": record.get("number", ""),
        "updated_fields": list(req.fields.keys()),
    }


# ---------------------------------------------------------------------------
# Tool: get_incident
# ---------------------------------------------------------------------------

@app.post("/tools/get_incident")
async def get_incident(req: GetIncidentRequest) -> Dict[str, Any]:
    """Retrieve a ServiceNow incident by sys_id."""
    url = f"{_base_url('incident')}/{req.sys_id}"
    params = {
        "sysparm_fields": (
            "sys_id,number,short_description,description,state,urgency,"
            "impact,priority,category,subcategory,assignment_group,"
            "assigned_to,caller_id,opened_at,resolved_at,closed_at,"
            "resolution_notes,close_code,comments_and_work_notes"
        )
    }
    result = await _request_with_retry("GET", url, params=params)
    record = result.get("result", {})
    logger.info(f"Fetched incident {req.sys_id}")

    def _display(field: Any) -> Any:
        """ServiceNow reference fields return {'value': ..., 'display_value': ...}."""
        if isinstance(field, dict):
            return field.get("display_value") or field.get("value")
        return field

    return {
        "sys_id": _display(record.get("sys_id")),
        "number": _display(record.get("number")),
        "short_description": _display(record.get("short_description")),
        "description": _display(record.get("description")),
        "state": _display(record.get("state")),
        "urgency": _display(record.get("urgency")),
        "impact": _display(record.get("impact")),
        "priority": _display(record.get("priority")),
        "category": _display(record.get("category")),
        "subcategory": _display(record.get("subcategory")),
        "assignment_group": _display(record.get("assignment_group")),
        "assigned_to": _display(record.get("assigned_to")),
        "caller_id": _display(record.get("caller_id")),
        "opened_at": _display(record.get("opened_at")),
        "resolved_at": _display(record.get("resolved_at")),
        "closed_at": _display(record.get("closed_at")),
        "resolution_notes": _display(record.get("resolution_notes")),
        "close_code": _display(record.get("close_code")),
    }


# ---------------------------------------------------------------------------
# Tool: close_incident
# ---------------------------------------------------------------------------

@app.post("/tools/close_incident")
async def close_incident(req: CloseIncidentRequest) -> Dict[str, Any]:
    """Close (resolve) a ServiceNow incident with resolution notes."""
    url = f"{_base_url('incident')}/{req.sys_id}"
    payload = {
        "state": "6",                           # Resolved
        "close_code": req.close_code,
        "close_notes": req.resolution_notes,
        "resolution_code": req.close_code,
        "resolution_notes": req.resolution_notes,
    }
    result = await _request_with_retry("PATCH", url, json=payload)
    record = result.get("result", {})
    logger.info(f"Closed incident {req.sys_id} with code '{req.close_code}'")
    return {
        "status": "closed",
        "sys_id": req.sys_id,
        "number": record.get("number", {}).get("display_value", ""),
        "close_code": req.close_code,
        "resolution_notes": req.resolution_notes,
    }


# ---------------------------------------------------------------------------
# Tool: search_incidents
# ---------------------------------------------------------------------------

@app.post("/tools/search_incidents")
async def search_incidents(req: SearchIncidentsRequest) -> Dict[str, Any]:
    """Search ServiceNow incidents using an encoded query string."""
    params = {
        "sysparm_query": req.query,
        "sysparm_limit": req.limit,
        "sysparm_offset": req.offset,
        "sysparm_fields": (
            "sys_id,number,short_description,state,urgency,impact,"
            "priority,category,assignment_group,assigned_to,opened_at"
        ),
        "sysparm_display_value": "true",
    }
    result = await _request_with_retry(
        "GET", _base_url("incident"), params=params
    )
    records = result.get("result", [])
    incidents = [
        {
            "sys_id": r.get("sys_id"),
            "number": r.get("number"),
            "short_description": r.get("short_description"),
            "state": r.get("state"),
            "urgency": r.get("urgency"),
            "impact": r.get("impact"),
            "priority": r.get("priority"),
            "category": r.get("category"),
            "assignment_group": r.get("assignment_group"),
            "assigned_to": r.get("assigned_to"),
            "opened_at": r.get("opened_at"),
        }
        for r in records
    ]
    logger.info(f"Search '{req.query}' returned {len(incidents)} incidents")
    return {
        "query": req.query,
        "incidents": incidents,
        "total": len(incidents),
        "offset": req.offset,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8004)
