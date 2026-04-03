"""
MCP Server: AWS operations.
Provides 6 tools:
  - get_cloudwatch_metrics
  - get_alarms
  - describe_eks_cluster
  - get_eks_nodegroups
  - put_metric_data
  - get_log_events
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from loguru import logger
import uvicorn

app = FastAPI(title="AWS MCP Server", version="1.0.0")

# ---------------------------------------------------------------------------
# AWS client helpers
# ---------------------------------------------------------------------------

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


def _cloudwatch_client():
    return boto3.client("cloudwatch", region_name=AWS_REGION)


def _logs_client():
    return boto3.client("logs", region_name=AWS_REGION)


def _eks_client():
    return boto3.client("eks", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class MetricDimension(BaseModel):
    Name: str
    Value: str


class CloudWatchMetricsRequest(BaseModel):
    namespace: str
    metric_name: str
    dimensions: List[MetricDimension] = Field(default_factory=list)
    period: int = 300
    start_minutes_ago: int = 60


class AlarmsRequest(BaseModel):
    state_value: str = "ALARM"


class EKSClusterRequest(BaseModel):
    cluster_name: str


class PutMetricRequest(BaseModel):
    namespace: str
    metric_name: str
    value: float
    unit: str = "None"
    dimensions: List[MetricDimension] = Field(default_factory=list)


class LogEventsRequest(BaseModel):
    log_group: str
    log_stream: str
    limit: int = 100


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "aws-mcp"}


# ---------------------------------------------------------------------------
# Tool: get_cloudwatch_metrics
# ---------------------------------------------------------------------------

@app.post("/tools/get_cloudwatch_metrics")
def get_cloudwatch_metrics(req: CloudWatchMetricsRequest) -> Dict[str, Any]:
    """Fetch CloudWatch metric statistics for a given metric."""
    cw = _cloudwatch_client()
    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(minutes=req.start_minutes_ago)
    dims = [{"Name": d.Name, "Value": d.Value} for d in req.dimensions]
    try:
        response = cw.get_metric_statistics(
            Namespace=req.namespace,
            MetricName=req.metric_name,
            Dimensions=dims,
            StartTime=start_time,
            EndTime=end_time,
            Period=req.period,
            Statistics=["Average", "Maximum", "Minimum", "Sum", "SampleCount"],
        )
        datapoints = sorted(
            response.get("Datapoints", []),
            key=lambda x: x["Timestamp"],
        )
        serialized = [
            {
                "timestamp": dp["Timestamp"].isoformat(),
                "average": dp.get("Average"),
                "maximum": dp.get("Maximum"),
                "minimum": dp.get("Minimum"),
                "sum": dp.get("Sum"),
                "sample_count": dp.get("SampleCount"),
                "unit": dp.get("Unit"),
            }
            for dp in datapoints
        ]
        logger.info(
            f"Fetched {len(serialized)} datapoints for {req.namespace}/{req.metric_name}"
        )
        return {
            "namespace": req.namespace,
            "metric_name": req.metric_name,
            "period": req.period,
            "datapoints": serialized,
            "total": len(serialized),
        }
    except (BotoCoreError, ClientError) as e:
        logger.error(f"get_cloudwatch_metrics error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_alarms
# ---------------------------------------------------------------------------

@app.post("/tools/get_alarms")
def get_alarms(req: AlarmsRequest) -> Dict[str, Any]:
    """Retrieve CloudWatch alarms filtered by state."""
    cw = _cloudwatch_client()
    try:
        paginator = cw.get_paginator("describe_alarms")
        alarms = []
        for page in paginator.paginate(StateValue=req.state_value):
            for alarm in page.get("MetricAlarms", []):
                alarms.append(
                    {
                        "alarm_name": alarm["AlarmName"],
                        "alarm_description": alarm.get("AlarmDescription", ""),
                        "state_value": alarm["StateValue"],
                        "state_reason": alarm.get("StateReason", ""),
                        "metric_name": alarm.get("MetricName", ""),
                        "namespace": alarm.get("Namespace", ""),
                        "dimensions": alarm.get("Dimensions", []),
                        "comparison_operator": alarm.get("ComparisonOperator", ""),
                        "threshold": alarm.get("Threshold"),
                        "state_updated_timestamp": alarm.get(
                            "StateUpdatedTimestamp", ""
                        ).isoformat()
                        if alarm.get("StateUpdatedTimestamp")
                        else None,
                    }
                )
        logger.info(f"Fetched {len(alarms)} alarms with state={req.state_value}")
        return {"alarms": alarms, "total": len(alarms), "state_filter": req.state_value}
    except (BotoCoreError, ClientError) as e:
        logger.error(f"get_alarms error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: describe_eks_cluster
# ---------------------------------------------------------------------------

@app.post("/tools/describe_eks_cluster")
def describe_eks_cluster(req: EKSClusterRequest) -> Dict[str, Any]:
    """Describe an EKS cluster including version, status, and endpoint."""
    eks = _eks_client()
    try:
        response = eks.describe_cluster(name=req.cluster_name)
        cluster = response["cluster"]
        logger.info(f"Described EKS cluster: {req.cluster_name}")
        return {
            "name": cluster["name"],
            "status": cluster["status"],
            "kubernetes_version": cluster["version"],
            "endpoint": cluster.get("endpoint", ""),
            "role_arn": cluster.get("roleArn", ""),
            "created_at": cluster["createdAt"].isoformat()
            if cluster.get("createdAt")
            else None,
            "tags": cluster.get("tags", {}),
            "logging": cluster.get("logging", {}),
            "resources_vpc_config": cluster.get("resourcesVpcConfig", {}),
        }
    except (BotoCoreError, ClientError) as e:
        logger.error(f"describe_eks_cluster error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_eks_nodegroups
# ---------------------------------------------------------------------------

@app.post("/tools/get_eks_nodegroups")
def get_eks_nodegroups(req: EKSClusterRequest) -> Dict[str, Any]:
    """List all node groups for an EKS cluster with their status and scaling config."""
    eks = _eks_client()
    try:
        ng_list = eks.list_nodegroups(clusterName=req.cluster_name)
        nodegroups = []
        for ng_name in ng_list.get("nodegroups", []):
            ng = eks.describe_nodegroup(
                clusterName=req.cluster_name, nodegroupName=ng_name
            )["nodegroup"]
            nodegroups.append(
                {
                    "name": ng["nodegroupName"],
                    "status": ng["status"],
                    "instance_types": ng.get("instanceTypes", []),
                    "ami_type": ng.get("amiType", ""),
                    "scaling_config": ng.get("scalingConfig", {}),
                    "disk_size": ng.get("diskSize"),
                    "labels": ng.get("labels", {}),
                    "taints": ng.get("taints", []),
                    "created_at": ng["createdAt"].isoformat()
                    if ng.get("createdAt")
                    else None,
                }
            )
        logger.info(
            f"Fetched {len(nodegroups)} node groups for cluster {req.cluster_name}"
        )
        return {
            "cluster_name": req.cluster_name,
            "nodegroups": nodegroups,
            "total": len(nodegroups),
        }
    except (BotoCoreError, ClientError) as e:
        logger.error(f"get_eks_nodegroups error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: put_metric_data
# ---------------------------------------------------------------------------

@app.post("/tools/put_metric_data")
def put_metric_data(req: PutMetricRequest) -> Dict[str, Any]:
    """Publish a custom metric data point to CloudWatch."""
    cw = _cloudwatch_client()
    dims = [{"Name": d.Name, "Value": d.Value} for d in req.dimensions]
    metric_data = {
        "MetricName": req.metric_name,
        "Value": req.value,
        "Unit": req.unit,
        "Timestamp": datetime.now(tz=timezone.utc),
    }
    if dims:
        metric_data["Dimensions"] = dims
    try:
        cw.put_metric_data(
            Namespace=req.namespace,
            MetricData=[metric_data],
        )
        logger.info(
            f"Published metric {req.namespace}/{req.metric_name}={req.value} {req.unit}"
        )
        return {
            "status": "published",
            "namespace": req.namespace,
            "metric_name": req.metric_name,
            "value": req.value,
            "unit": req.unit,
        }
    except (BotoCoreError, ClientError) as e:
        logger.error(f"put_metric_data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool: get_log_events
# ---------------------------------------------------------------------------

@app.post("/tools/get_log_events")
def get_log_events(req: LogEventsRequest) -> Dict[str, Any]:
    """Retrieve log events from a CloudWatch Logs stream."""
    logs = _logs_client()
    try:
        response = logs.get_log_events(
            logGroupName=req.log_group,
            logStreamName=req.log_stream,
            limit=req.limit,
            startFromHead=False,
        )
        events = [
            {
                "timestamp": datetime.fromtimestamp(
                    e["timestamp"] / 1000, tz=timezone.utc
                ).isoformat(),
                "message": e["message"],
                "ingestion_time": datetime.fromtimestamp(
                    e["ingestionTime"] / 1000, tz=timezone.utc
                ).isoformat()
                if e.get("ingestionTime")
                else None,
            }
            for e in response.get("events", [])
        ]
        logger.info(
            f"Fetched {len(events)} log events from {req.log_group}/{req.log_stream}"
        )
        return {
            "log_group": req.log_group,
            "log_stream": req.log_stream,
            "events": events,
            "total": len(events),
        }
    except (BotoCoreError, ClientError) as e:
        logger.error(f"get_log_events error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
