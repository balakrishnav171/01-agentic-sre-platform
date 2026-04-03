"""
metrics_agent.py
----------------
Metrics analysis agent for the Agentic SRE Platform.

Queries both Datadog and AWS CloudWatch for service-level metrics
(CPU, memory, error rate, latency), detects anomalies using statistical
methods, and generates human-readable operational insights.

Dependencies:
    pip install datadog-api-client boto3 pandas numpy loguru
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
from loguru import logger

# ---------------------------------------------------------------------------
# Datadog client (optional import)
# ---------------------------------------------------------------------------
try:
    from datadog_api_client import ApiClient as DDApiClient
    from datadog_api_client import Configuration as DDConfiguration
    from datadog_api_client.v1.api.metrics_api import MetricsApi as DDMetricsApi
    from datadog_api_client.v1.api.metrics_api import MetricsApi

    _DD_AVAILABLE = True
except ImportError:
    logger.warning("datadog_api_client not installed — Datadog metrics will use mock data")
    _DD_AVAILABLE = False

# ---------------------------------------------------------------------------
# AWS CloudWatch client (optional import)
# ---------------------------------------------------------------------------
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    _CW_AVAILABLE = True
except ImportError:
    logger.warning("boto3 not installed — CloudWatch metrics will use mock data")
    _CW_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MetricPoint:
    """A single time-series data point."""

    timestamp: datetime
    value: float
    unit: str = ""


@dataclass
class MetricSeries:
    """A named time series."""

    name: str
    points: list[MetricPoint]
    tags: dict[str, str] = field(default_factory=dict)
    source: str = ""  # 'datadog' | 'cloudwatch'

    def to_series(self) -> pd.Series:
        """Convert to a pandas Series indexed by timestamp."""
        return pd.Series(
            data=[p.value for p in self.points],
            index=[p.timestamp for p in self.points],
            name=self.name,
        )


@dataclass
class Anomaly:
    """A detected metric anomaly."""

    metric_name: str
    timestamp: datetime
    observed_value: float
    expected_value: float
    z_score: float
    severity: str  # 'critical' | 'high' | 'medium' | 'low'
    description: str


@dataclass
class MetricsAnalysis:
    """
    Aggregated analysis result for a single service + time window.
    """

    namespace: str
    service_name: str
    time_window_seconds: int
    analysed_at: datetime
    cpu_p50: float
    cpu_p95: float
    cpu_p99: float
    memory_p50: float
    memory_p95: float
    error_rate_avg: float
    error_rate_max: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    request_rate_avg: float
    anomaly_count: int
    health_score: float  # 0.0 (critical) – 100.0 (healthy)
    series: dict[str, MetricSeries] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MetricsAgent
# ---------------------------------------------------------------------------

class MetricsAgent:
    """
    Metrics analysis agent.

    Fetches CPU, memory, error rate, and latency metrics from Datadog and
    AWS CloudWatch, runs anomaly detection, and produces actionable insights.

    Usage::

        agent = MetricsAgent()
        analysis  = agent.analyze_metrics("production", "api-server", 3600)
        anomalies = agent.detect_anomalies()
        insights  = agent.generate_insights()
    """

    # Anomaly detection thresholds
    _ZSCORE_CRITICAL = 3.5
    _ZSCORE_HIGH = 3.0
    _ZSCORE_MEDIUM = 2.5
    _ZSCORE_LOW = 2.0

    def __init__(
        self,
        dd_api_key: Optional[str] = None,
        dd_app_key: Optional[str] = None,
        aws_region: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ) -> None:
        """
        Initialise the MetricsAgent.

        All credentials fall back to environment variables if not provided.

        Args:
            dd_api_key: Datadog API key (``DD_API_KEY`` env var fallback).
            dd_app_key: Datadog Application key (``DD_APP_KEY`` env var).
            aws_region: AWS region (``AWS_DEFAULT_REGION`` env var).
            aws_access_key_id: AWS access key ID.
            aws_secret_access_key: AWS secret access key.
        """
        # Datadog
        self._dd_api_key = dd_api_key or os.environ.get("DD_API_KEY", "")
        self._dd_app_key = dd_app_key or os.environ.get("DD_APP_KEY", "")

        # AWS
        self._aws_region = aws_region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self._aws_access_key_id = aws_access_key_id or os.environ.get("AWS_ACCESS_KEY_ID")
        self._aws_secret_access_key = (
            aws_secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY")
        )

        # State populated by analyze_metrics
        self._last_analysis: Optional[MetricsAnalysis] = None
        self._last_series: dict[str, MetricSeries] = {}

        logger.info(
            "MetricsAgent initialised | dd_available={} cw_available={}",
            _DD_AVAILABLE and bool(self._dd_api_key),
            _CW_AVAILABLE,
        )

    # ------------------------------------------------------------------
    # Datadog helpers
    # ------------------------------------------------------------------

    def _dd_configuration(self) -> Any:
        """Build a Datadog API configuration object."""
        if not _DD_AVAILABLE:
            raise RuntimeError("datadog_api_client not installed")
        conf = DDConfiguration()
        conf.api_key["apiKeyAuth"] = self._dd_api_key
        conf.api_key["appKeyAuth"] = self._dd_app_key
        return conf

    def _fetch_dd_metric(
        self,
        query: str,
        start: datetime,
        end: datetime,
        metric_name: str,
        tags: Optional[dict[str, str]] = None,
    ) -> MetricSeries:
        """
        Execute a single Datadog metrics query.

        Args:
            query: Datadog metric query string
                (e.g. ``avg:kubernetes.cpu.usage{service:api-server}``).
            start: Query start time (UTC).
            end: Query end time (UTC).
            metric_name: Friendly name for the returned series.
            tags: Optional metadata tags to attach to the series.

        Returns:
            MetricSeries with data points from Datadog.
        """
        if not _DD_AVAILABLE or not self._dd_api_key:
            logger.debug("_fetch_dd_metric | using mock data for {}", metric_name)
            return self._mock_metric_series(metric_name, start, end, tags)

        try:
            with DDApiClient(self._dd_configuration()) as api_client:
                api = DDMetricsApi(api_client)
                response = api.query_metrics(
                    _from=int(start.timestamp()),
                    to=int(end.timestamp()),
                    query=query,
                )
            points: list[MetricPoint] = []
            for series in (response.series or []):
                for pt in (series.pointlist or []):
                    if pt[0] is not None and pt[1] is not None:
                        points.append(
                            MetricPoint(
                                timestamp=datetime.fromtimestamp(pt[0] / 1000, tz=timezone.utc),
                                value=float(pt[1]),
                            )
                        )
            return MetricSeries(
                name=metric_name,
                points=points,
                tags=tags or {},
                source="datadog",
            )
        except Exception as exc:
            logger.warning("_fetch_dd_metric | {} failed: {} — using mock", metric_name, exc)
            return self._mock_metric_series(metric_name, start, end, tags)

    # ------------------------------------------------------------------
    # CloudWatch helpers
    # ------------------------------------------------------------------

    def _fetch_cw_metric(
        self,
        namespace: str,
        metric_name_cw: str,
        dimensions: list[dict[str, str]],
        start: datetime,
        end: datetime,
        stat: str = "Average",
        period: int = 60,
        friendly_name: Optional[str] = None,
    ) -> MetricSeries:
        """
        Fetch a single metric from AWS CloudWatch.

        Args:
            namespace: CloudWatch namespace (e.g. ``AWS/ECS``).
            metric_name_cw: CloudWatch metric name.
            dimensions: List of dimension dicts ``[{'Name': ..., 'Value': ...}]``.
            start: Query start time (UTC).
            end: Query end time (UTC).
            stat: CloudWatch statistic: ``Average``, ``Sum``, ``p99``, etc.
            period: Data point resolution in seconds.
            friendly_name: Override for the returned MetricSeries name.

        Returns:
            MetricSeries with CloudWatch data points.
        """
        series_name = friendly_name or metric_name_cw

        if not _CW_AVAILABLE:
            logger.debug("_fetch_cw_metric | using mock data for {}", series_name)
            return self._mock_metric_series(series_name, start, end, source="cloudwatch")

        try:
            session_kwargs: dict[str, Any] = {"region_name": self._aws_region}
            if self._aws_access_key_id:
                session_kwargs["aws_access_key_id"] = self._aws_access_key_id
                session_kwargs["aws_secret_access_key"] = self._aws_secret_access_key

            session = boto3.Session(**session_kwargs)
            cw = session.client("cloudwatch")

            response = cw.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name_cw,
                Dimensions=dimensions,
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=[stat],
            )
            data_points = sorted(
                response.get("Datapoints", []),
                key=lambda d: d["Timestamp"],
            )
            points = [
                MetricPoint(
                    timestamp=dp["Timestamp"].replace(tzinfo=timezone.utc),
                    value=float(dp[stat]),
                    unit=dp.get("Unit", ""),
                )
                for dp in data_points
            ]
            return MetricSeries(
                name=series_name,
                points=points,
                source="cloudwatch",
            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("_fetch_cw_metric | {} failed: {} — using mock", series_name, exc)
            return self._mock_metric_series(series_name, start, end, source="cloudwatch")
        except Exception as exc:
            logger.warning("_fetch_cw_metric | {} unexpected error: {} — using mock", series_name, exc)
            return self._mock_metric_series(series_name, start, end, source="cloudwatch")

    # ------------------------------------------------------------------
    # Mock / fallback data
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_metric_series(
        name: str,
        start: datetime,
        end: datetime,
        tags: Optional[dict[str, str]] = None,
        source: str = "mock",
    ) -> MetricSeries:
        """
        Generate a plausible mock time series for testing / offline mode.

        Args:
            name: Metric name.
            start: Series start time.
            end: Series end time.
            tags: Optional metadata tags.
            source: Source label for the series.

        Returns:
            MetricSeries with one point per minute of random data.
        """
        import random

        base_values: dict[str, tuple[float, float]] = {
            "cpu": (40.0, 15.0),
            "memory": (60.0, 10.0),
            "error_rate": (0.5, 0.8),
            "latency": (120.0, 40.0),
            "request_rate": (500.0, 100.0),
        }
        key = next((k for k in base_values if k in name.lower()), "cpu")
        base, std = base_values[key]

        points = []
        current = start
        while current <= end:
            # Introduce a simulated spike at ~70% of the window
            progress = (current - start).total_seconds() / max((end - start).total_seconds(), 1)
            spike = base * 0.5 if 0.65 < progress < 0.75 else 0.0
            value = max(0.0, random.gauss(base + spike, std))
            points.append(MetricPoint(timestamp=current, value=round(value, 4)))
            current += timedelta(minutes=1)

        return MetricSeries(name=name, points=points, tags=tags or {}, source=source)

    # ------------------------------------------------------------------
    # Percentile helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _percentiles(values: list[float], *pcts: int) -> list[float]:
        """
        Compute percentiles using pandas.

        Args:
            values: List of numeric values.
            pcts: Percentile integers (e.g., 50, 95, 99).

        Returns:
            List of computed percentile values (NaN-safe).
        """
        if not values:
            return [0.0] * len(pcts)
        s = pd.Series(values)
        return [float(s.quantile(p / 100)) for p in pcts]

    # ------------------------------------------------------------------
    # Public: analyze_metrics
    # ------------------------------------------------------------------

    def analyze_metrics(
        self,
        namespace: str,
        service_name: str,
        time_window: int = 3600,
    ) -> MetricsAnalysis:
        """
        Fetch and analyse metrics for a service over a time window.

        Queries both Datadog and CloudWatch, merges results, and computes
        statistical summaries (p50/p95/p99) for each signal.

        Args:
            namespace: Kubernetes/deployment namespace or environment tag.
            service_name: Logical service name (used in metric tags/dimensions).
            time_window: Look-back window in seconds (default: 3600 = 1 hour).

        Returns:
            MetricsAnalysis dataclass with aggregated statistics.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=time_window)

        logger.info(
            "analyze_metrics | ns={} service={} window={}s",
            namespace, service_name, time_window,
        )

        # ---- Datadog queries -------------------------------------------
        dd_tag = f"service:{service_name},namespace:{namespace}"
        cpu_series = self._fetch_dd_metric(
            query=f"avg:kubernetes.cpu.usage{{kube_namespace:{namespace},kube_deployment:{service_name}}}",
            start=start, end=end,
            metric_name="cpu",
            tags={"service": service_name, "namespace": namespace},
        )
        memory_series = self._fetch_dd_metric(
            query=f"avg:kubernetes.memory.usage{{kube_namespace:{namespace},kube_deployment:{service_name}}}",
            start=start, end=end,
            metric_name="memory",
            tags={"service": service_name},
        )
        error_rate_series = self._fetch_dd_metric(
            query=f"sum:trace.http.request.errors{{service:{service_name}}}.as_rate()",
            start=start, end=end,
            metric_name="error_rate",
        )
        latency_series = self._fetch_dd_metric(
            query=f"avg:trace.http.request.duration{{service:{service_name}}}",
            start=start, end=end,
            metric_name="latency",
        )
        request_rate_series = self._fetch_dd_metric(
            query=f"sum:trace.http.request.hits{{service:{service_name}}}.as_rate()",
            start=start, end=end,
            metric_name="request_rate",
        )

        # ---- CloudWatch queries ----------------------------------------
        cw_cpu_series = self._fetch_cw_metric(
            namespace="AWS/ECS",
            metric_name_cw="CPUUtilization",
            dimensions=[
                {"Name": "ServiceName", "Value": service_name},
                {"Name": "ClusterName", "Value": namespace},
            ],
            start=start, end=end,
            friendly_name="cw_cpu",
        )
        cw_memory_series = self._fetch_cw_metric(
            namespace="AWS/ECS",
            metric_name_cw="MemoryUtilization",
            dimensions=[
                {"Name": "ServiceName", "Value": service_name},
                {"Name": "ClusterName", "Value": namespace},
            ],
            start=start, end=end,
            friendly_name="cw_memory",
        )

        # ---- Merge CPU (Datadog + CloudWatch avg) ----------------------
        def _merge_series(s1: MetricSeries, s2: MetricSeries) -> list[float]:
            """Merge two series by averaging values from both sources."""
            vals = [p.value for p in s1.points] + [p.value for p in s2.points]
            return vals if vals else [0.0]

        cpu_vals = _merge_series(cpu_series, cw_cpu_series)
        mem_vals = _merge_series(memory_series, cw_memory_series)
        err_vals = [p.value for p in error_rate_series.points] or [0.0]
        lat_vals = [p.value for p in latency_series.points] or [0.0]
        req_vals = [p.value for p in request_rate_series.points] or [0.0]

        cpu_p50, cpu_p95, cpu_p99 = self._percentiles(cpu_vals, 50, 95, 99)
        mem_p50, mem_p95 = self._percentiles(mem_vals, 50, 95)
        lat_p50, lat_p95, lat_p99 = self._percentiles(lat_vals, 50, 95, 99)

        error_rate_avg = sum(err_vals) / len(err_vals)
        error_rate_max = max(err_vals)
        request_rate_avg = sum(req_vals) / len(req_vals)

        # ---- Health score (heuristic) ----------------------------------
        health_score = 100.0
        if cpu_p95 > 80:
            health_score -= 20
        if mem_p95 > 85:
            health_score -= 15
        if error_rate_avg > 1.0:
            health_score -= 25
        if lat_p95 > 500:
            health_score -= 20
        health_score = max(0.0, min(100.0, health_score))

        all_series = {
            "cpu": cpu_series,
            "memory": memory_series,
            "error_rate": error_rate_series,
            "latency": latency_series,
            "request_rate": request_rate_series,
            "cw_cpu": cw_cpu_series,
            "cw_memory": cw_memory_series,
        }

        self._last_series = all_series

        analysis = MetricsAnalysis(
            namespace=namespace,
            service_name=service_name,
            time_window_seconds=time_window,
            analysed_at=end,
            cpu_p50=round(cpu_p50, 2),
            cpu_p95=round(cpu_p95, 2),
            cpu_p99=round(cpu_p99, 2),
            memory_p50=round(mem_p50, 2),
            memory_p95=round(mem_p95, 2),
            error_rate_avg=round(error_rate_avg, 4),
            error_rate_max=round(error_rate_max, 4),
            latency_p50_ms=round(lat_p50, 2),
            latency_p95_ms=round(lat_p95, 2),
            latency_p99_ms=round(lat_p99, 2),
            request_rate_avg=round(request_rate_avg, 2),
            anomaly_count=0,  # filled by detect_anomalies
            health_score=round(health_score, 1),
            series=all_series,
        )
        self._last_analysis = analysis
        logger.info(
            "analyze_metrics | health_score={} cpu_p95={} lat_p95={} err_avg={}",
            health_score, cpu_p95, lat_p95, error_rate_avg,
        )
        return analysis

    # ------------------------------------------------------------------
    # Public: detect_anomalies
    # ------------------------------------------------------------------

    def detect_anomalies(self) -> list[Anomaly]:
        """
        Run z-score anomaly detection over the last fetched metric series.

        Must be called after ``analyze_metrics``.  Iterates over each metric
        series and flags points whose z-score exceeds the configured thresholds.

        Returns:
            List of Anomaly instances, sorted by severity then timestamp.

        Raises:
            RuntimeError: If ``analyze_metrics`` has not been called yet.
        """
        if not self._last_series:
            raise RuntimeError("Call analyze_metrics() before detect_anomalies()")

        anomalies: list[Anomaly] = []

        for metric_name, series in self._last_series.items():
            if not series.points:
                continue

            values = [p.value for p in series.points]
            if len(values) < 4:
                continue

            mean = statistics.mean(values)
            try:
                std = statistics.stdev(values)
            except statistics.StatisticsError:
                continue

            if std == 0:
                continue

            for point in series.points:
                z = (point.value - mean) / std
                abs_z = abs(z)

                if abs_z < self._ZSCORE_LOW:
                    continue

                if abs_z >= self._ZSCORE_CRITICAL:
                    severity = "critical"
                elif abs_z >= self._ZSCORE_HIGH:
                    severity = "high"
                elif abs_z >= self._ZSCORE_MEDIUM:
                    severity = "medium"
                else:
                    severity = "low"

                direction = "spike" if z > 0 else "drop"
                anomalies.append(
                    Anomaly(
                        metric_name=metric_name,
                        timestamp=point.timestamp,
                        observed_value=round(point.value, 4),
                        expected_value=round(mean, 4),
                        z_score=round(z, 3),
                        severity=severity,
                        description=(
                            f"{metric_name} {direction} at "
                            f"{point.timestamp.strftime('%H:%M:%S UTC')}: "
                            f"observed={point.value:.3f}, expected≈{mean:.3f}, "
                            f"z={z:.2f}"
                        ),
                    )
                )

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        anomalies.sort(key=lambda a: (severity_order.get(a.severity, 4), a.timestamp))

        if self._last_analysis is not None:
            self._last_analysis.anomaly_count = len(anomalies)

        logger.info("detect_anomalies | found {} anomalies", len(anomalies))
        return anomalies

    # ------------------------------------------------------------------
    # Public: generate_insights
    # ------------------------------------------------------------------

    def generate_insights(self) -> str:
        """
        Generate a human-readable operational insights report.

        Summarises the most recent MetricsAnalysis and detected anomalies
        into actionable findings.

        Returns:
            Multi-line string with performance summary, anomaly highlights,
            and recommended actions.

        Raises:
            RuntimeError: If ``analyze_metrics`` has not been called yet.
        """
        if self._last_analysis is None:
            raise RuntimeError("Call analyze_metrics() before generate_insights()")

        analysis = self._last_analysis
        anomalies = []
        try:
            anomalies = self.detect_anomalies()
        except RuntimeError:
            pass

        lines: list[str] = [
            "=" * 60,
            f"METRICS INSIGHTS REPORT",
            f"Service : {analysis.service_name}",
            f"Namespace: {analysis.namespace}",
            f"Window  : {analysis.time_window_seconds // 60} minutes",
            f"As of   : {analysis.analysed_at.strftime('%Y-%m-%d %H:%M UTC')}",
            f"Health  : {analysis.health_score}/100",
            "=" * 60,
            "",
            "── CPU Utilisation ──────────────────────────────────────",
            f"  p50: {analysis.cpu_p50}%  p95: {analysis.cpu_p95}%  p99: {analysis.cpu_p99}%",
        ]

        if analysis.cpu_p95 > 85:
            lines.append("  [!] HIGH CPU — consider horizontal scaling or right-sizing")
        elif analysis.cpu_p95 > 70:
            lines.append("  [~] Elevated CPU — monitor closely")
        else:
            lines.append("  [OK] CPU within normal range")

        lines += [
            "",
            "── Memory Utilisation ───────────────────────────────────",
            f"  p50: {analysis.memory_p50}%  p95: {analysis.memory_p95}%",
        ]

        if analysis.memory_p95 > 90:
            lines.append("  [!] CRITICAL MEMORY — risk of OOMKill")
        elif analysis.memory_p95 > 75:
            lines.append("  [~] Elevated memory — check for leaks")
        else:
            lines.append("  [OK] Memory within normal range")

        lines += [
            "",
            "── Error Rate ───────────────────────────────────────────",
            f"  Average: {analysis.error_rate_avg:.4f} req/s  Max: {analysis.error_rate_max:.4f} req/s",
        ]

        if analysis.error_rate_avg > 5.0:
            lines.append("  [!] CRITICAL ERROR RATE — investigate immediately")
        elif analysis.error_rate_avg > 1.0:
            lines.append("  [~] Elevated error rate — check application logs")
        else:
            lines.append("  [OK] Error rate acceptable")

        lines += [
            "",
            "── Latency ──────────────────────────────────────────────",
            f"  p50: {analysis.latency_p50_ms}ms  "
            f"p95: {analysis.latency_p95_ms}ms  "
            f"p99: {analysis.latency_p99_ms}ms",
        ]

        if analysis.latency_p99_ms > 1000:
            lines.append("  [!] CRITICAL LATENCY at p99 > 1 second")
        elif analysis.latency_p95_ms > 500:
            lines.append("  [~] Elevated p95 latency — check downstream dependencies")
        else:
            lines.append("  [OK] Latency within SLO")

        lines += [
            "",
            "── Request Rate ─────────────────────────────────────────",
            f"  Average: {analysis.request_rate_avg:.1f} req/s",
            "",
        ]

        # Anomaly summary
        if anomalies:
            critical = [a for a in anomalies if a.severity == "critical"]
            high = [a for a in anomalies if a.severity == "high"]
            lines.append(f"── Anomalies Detected: {len(anomalies)} ──────────────────────────")
            if critical:
                lines.append(f"  CRITICAL ({len(critical)}):")
                for a in critical[:3]:
                    lines.append(f"    • {a.description}")
            if high:
                lines.append(f"  HIGH ({len(high)}):")
                for a in high[:3]:
                    lines.append(f"    • {a.description}")
            if len(anomalies) > 6:
                lines.append(f"  ... and {len(anomalies) - 6} more anomalies")
            lines.append("")

        # Recommendations
        lines.append("── Recommendations ──────────────────────────────────────")
        recs: list[str] = []
        if analysis.cpu_p95 > 85:
            recs.append("Scale deployment horizontally or increase CPU limits.")
        if analysis.memory_p95 > 90:
            recs.append("Investigate memory leak; consider pod restart as immediate mitigation.")
        if analysis.error_rate_avg > 1.0:
            recs.append("Review application error logs and upstream dependencies.")
        if analysis.latency_p95_ms > 500:
            recs.append("Profile slow endpoints; check database query performance.")
        if analysis.health_score < 70:
            recs.append("Consider opening a P1 incident given overall health score.")
        if not recs:
            recs.append("No immediate action required. Continue monitoring.")

        for rec in recs:
            lines.append(f"  → {rec}")

        lines.append("=" * 60)
        report = "\n".join(lines)
        logger.info(
            "generate_insights | health={} anomalies={} recs={}",
            analysis.health_score, len(anomalies), len(recs),
        )
        return report
