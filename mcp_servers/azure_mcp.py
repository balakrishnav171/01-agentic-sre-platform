"""
MCP Server: Azure operations.
Provides 5 tools:
  - get_aks_cluster
  - get_azure_metrics
  - get_azure_alerts
  - get_log_analytics_query
  - get_keyvault_secret
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from loguru import logger
import uvicorn

# Azure SDK imports
from azure.identity import DefaultAzureCredential
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.monitor import MonitorManagementClient
from azure.monitor.query import LogsQueryClient, MetricsQueryClient, MetricAggregationType
from azure.keyvault.secrets import SecretClient
from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError

app = FastAPI(title="Azure MCP Server", version="1.0.0")

# ---------------------------------------------------------------------------
# Credential + client helpers
# ---------------------------------------------------------------------------

AZURE_SUBSCRIPTION_ID = os.getenv("AZURE_SUBSCRIPTION_ID", "")


def _credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


def _aks_client(subscription_id: str) -> ContainerServiceClient:
    return ContainerServiceClient(_credential(), subscription_id)


def _monitor_client(subscription_id: str) -> MonitorManagementClient:
    return MonitorManagementClient(_credential(), subscription_id)


def _metrics_query_client() -> MetricsQueryClient:
    return MetricsQueryClient(_credential())


def _logs_query_client() -> LogsQueryClient:
    return LogsQueryClient(_credential())


def _keyvault_client(vault_name: str) -> SecretClient:
    vault_url = f"https://{vault_name}.vault.azure.net"
    return SecretClient(vault_url=vault_url, credential=_credential())


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class AKSClusterRequest(BaseModel):
    resource_group: str
    cluster_name: str
    subscription_id: str = Field(default_factory=lambda: AZURE_SUBSCRIPTION_ID)


class AzureMetricsRequest(BaseModel):
    resource_uri: str
    metric_names: List[str]
    interval: str = "PT5M"
    timespan_hours: int = 1


class AzureAlertsRequest(BaseModel):
    subscription_id: str = Field(default_factory=lambda: AZURE_SUBSCRIPTION_ID)
    resource_group: str


class LogAnalyticsRequest(BaseModel):
    workspace_id: str
    query: str
    timespan_hours: int = 24


class KeyVaultSecretRequest(BaseModel):
    vault_name: str
    secret_name: str


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "azure-mcp"}


# ---------------------------------------------------------------------------
# Tool: get_aks_cluster
# ---------------------------------------------------------------------------

@app.post("/tools/get_aks_cluster")
def get_aks_cluster(req: AKSClusterRequest) -> Dict[str, Any]:
    """Get details of an AKS cluster including provisioning state and node pools."""
    aks = _aks_client(req.subscription_id)
    try:
        cluster = aks.managed_clusters.get(req.resource_group, req.cluster_name)
        agent_pools = [
            {
                "name": pool.name,
                "vm_size": pool.vm_size,
                "count": pool.count,
                "os_type": pool.os_type,
                "mode": pool.mode,
                "provisioning_state": pool.provisioning_state,
                "kubernetes_version": pool.kubernetes_version,
                "min_count": pool.min_count,
                "max_count": pool.max_count,
                "enable_auto_scaling": pool.enable_auto_scaling,
            }
            for pool in (cluster.agent_pool_profiles or [])
        ]
        logger.info(f"Fetched AKS cluster {req.cluster_name} in {req.resource_group}")
        return {
            "name": cluster.name,
            "location": cluster.location,
            "kubernetes_version": cluster.kubernetes_version,
            "provisioning_state": cluster.provisioning_state,
            "fqdn": cluster.fqdn,
            "dns_prefix": cluster.dns_prefix,
            "node_resource_group": cluster.node_resource_group,
            "enable_rbac": cluster.enable_rbac,
            "agent_pools": agent_pools,
            "tags": cluster.tags or {},
        }
    except ResourceNotFoundError as e:
        logger.warning(f"AKS cluster not found: {req.cluster_name}")
        raise HTTPException(status_code=404, detail=str(e))
    except AzureError as e:
        logger.error(f"get_aks_cluster error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_azure_metrics
# ---------------------------------------------------------------------------

@app.post("/tools/get_azure_metrics")
def get_azure_metrics(req: AzureMetricsRequest) -> Dict[str, Any]:
    """Query Azure Monitor metrics for a given resource URI."""
    mq = _metrics_query_client()
    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(hours=req.timespan_hours)
    try:
        response = mq.query_resource(
            resource_uri=req.resource_uri,
            metric_names=req.metric_names,
            timespan=(start_time, end_time),
            granularity=_parse_interval(req.interval),
            aggregations=[
                MetricAggregationType.AVERAGE,
                MetricAggregationType.MAXIMUM,
                MetricAggregationType.MINIMUM,
            ],
        )
        metrics_out = []
        for metric in response.metrics:
            timeseries_out = []
            for ts in metric.timeseries:
                timeseries_out.append(
                    [
                        {
                            "timestamp": dp.timestamp.isoformat(),
                            "average": dp.average,
                            "maximum": dp.maximum,
                            "minimum": dp.minimum,
                        }
                        for dp in ts.data
                    ]
                )
            metrics_out.append(
                {
                    "name": metric.name,
                    "unit": str(metric.unit),
                    "timeseries": timeseries_out,
                }
            )
        logger.info(
            f"Fetched {len(metrics_out)} metrics for resource {req.resource_uri}"
        )
        return {
            "resource_uri": req.resource_uri,
            "interval": req.interval,
            "metrics": metrics_out,
        }
    except AzureError as e:
        logger.error(f"get_azure_metrics error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _parse_interval(interval: str):
    """Convert ISO 8601 duration string to timedelta for the SDK."""
    # Simple mapping for common SRE intervals
    mapping = {
        "PT1M": timedelta(minutes=1),
        "PT5M": timedelta(minutes=5),
        "PT15M": timedelta(minutes=15),
        "PT30M": timedelta(minutes=30),
        "PT1H": timedelta(hours=1),
        "PT6H": timedelta(hours=6),
        "PT12H": timedelta(hours=12),
        "P1D": timedelta(days=1),
    }
    return mapping.get(interval, timedelta(minutes=5))


# ---------------------------------------------------------------------------
# Tool: get_azure_alerts
# ---------------------------------------------------------------------------

@app.post("/tools/get_azure_alerts")
def get_azure_alerts(req: AzureAlertsRequest) -> Dict[str, Any]:
    """Retrieve Azure Monitor alert rules for a resource group."""
    monitor = _monitor_client(req.subscription_id)
    try:
        alert_rules = list(
            monitor.metric_alerts.list_by_resource_group(req.resource_group)
        )
        alerts_out = [
            {
                "name": rule.name,
                "description": rule.description or "",
                "severity": rule.severity,
                "enabled": rule.enabled,
                "scopes": list(rule.scopes or []),
                "evaluation_frequency": str(rule.evaluation_frequency),
                "window_size": str(rule.window_size),
                "auto_mitigate": rule.auto_mitigate,
                "location": rule.location,
                "tags": rule.tags or {},
            }
            for rule in alert_rules
        ]
        logger.info(
            f"Fetched {len(alerts_out)} alert rules in resource group {req.resource_group}"
        )
        return {
            "resource_group": req.resource_group,
            "alerts": alerts_out,
            "total": len(alerts_out),
        }
    except AzureError as e:
        logger.error(f"get_azure_alerts error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_log_analytics_query
# ---------------------------------------------------------------------------

@app.post("/tools/get_log_analytics_query")
def get_log_analytics_query(req: LogAnalyticsRequest) -> Dict[str, Any]:
    """Execute a KQL query against a Log Analytics workspace."""
    lq = _logs_query_client()
    timespan = timedelta(hours=req.timespan_hours)
    try:
        response = lq.query_workspace(
            workspace_id=req.workspace_id,
            query=req.query,
            timespan=timespan,
        )
        tables_out = []
        for table in response.tables:
            columns = [col.name for col in table.columns]
            rows = [dict(zip(columns, row)) for row in table.rows]
            tables_out.append(
                {
                    "name": table.name,
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                }
            )
        total_rows = sum(t["row_count"] for t in tables_out)
        logger.info(
            f"Log Analytics query returned {total_rows} rows from workspace {req.workspace_id}"
        )
        return {
            "workspace_id": req.workspace_id,
            "query": req.query,
            "tables": tables_out,
            "total_rows": total_rows,
        }
    except HttpResponseError as e:
        logger.error(f"get_log_analytics_query HTTP error: {e}")
        raise HTTPException(status_code=e.status_code or 500, detail=str(e))
    except AzureError as e:
        logger.error(f"get_log_analytics_query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_keyvault_secret
# ---------------------------------------------------------------------------

@app.post("/tools/get_keyvault_secret")
def get_keyvault_secret(req: KeyVaultSecretRequest) -> Dict[str, Any]:
    """Retrieve a secret value from Azure Key Vault."""
    kv = _keyvault_client(req.vault_name)
    try:
        secret = kv.get_secret(req.secret_name)
        logger.info(
            f"Retrieved secret '{req.secret_name}' from vault '{req.vault_name}'"
        )
        return {
            "vault_name": req.vault_name,
            "secret_name": req.secret_name,
            "value": secret.value,
            "version": secret.properties.version,
            "enabled": secret.properties.enabled,
            "created_on": secret.properties.created_on.isoformat()
            if secret.properties.created_on
            else None,
            "expires_on": secret.properties.expires_on.isoformat()
            if secret.properties.expires_on
            else None,
        }
    except ResourceNotFoundError as e:
        logger.warning(f"Secret not found: {req.secret_name} in {req.vault_name}")
        raise HTTPException(status_code=404, detail=str(e))
    except AzureError as e:
        logger.error(f"get_keyvault_secret error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
