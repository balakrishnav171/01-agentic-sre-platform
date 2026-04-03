# High CPU Usage Runbook

**severity:** medium
**category:** performance
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

High CPU usage in a Kubernetes workload can cause service degradation, request timeouts, throttling, and in extreme cases pod eviction. This runbook covers diagnosis and remediation for both sustained and spike-pattern CPU issues across pods, nodes, and namespaces.

---

## Symptoms

- `kubectl top pods -n <namespace>` shows one or more pods near or at CPU limit.
- Prometheus alert: `CPUThrottlingHigh` — container CPU throttling ratio > 25%.
- Prometheus alert: `KubePodCPUNearLimit` — pod CPU usage > 80% of limit for 15 minutes.
- Datadog monitor: `kubernetes.cpu.usage.total` above threshold.
- Increased HTTP latency, request queuing, or timeouts in application APM.
- HPA scaling events showing `ScalingActive: True` with `DesiredReplicas` continuously increasing.
- Node CPU saturation visible in Grafana node exporter dashboards.

---

## Root Causes

### 1. Insufficient CPU Limits / Requests
CPU requests are set too low, causing the pod to be throttled by the Linux CFS scheduler when other pods compete for CPU on the same node.

### 2. Application-Level Inefficiency
- Inefficient algorithms (O(n²) loops on large datasets)
- Memory leaks causing excessive GC pressure and GC CPU spikes
- Busy-wait loops or polling without backoff
- Connection pool contention causing thread spinning

### 3. Traffic Spike / Load Surge
Unexpected traffic increase (traffic spike, batch job, crawler) saturating CPU before HPA can respond.

### 4. Runaway Background Job or Cron
A background task (reindexing, report generation, data migration) consuming CPU without rate limiting.

### 5. JVM / Runtime Performance Regression
New code deployment with a performance regression, increased GC pause frequency, or JIT de-optimisation.

### 6. Noisy Neighbour on the Node
Other pods co-located on the same node are consuming CPU, reducing headroom (check node-level utilisation).

### 7. HPA Not Configured or Under-configured
The Deployment lacks a HorizontalPodAutoscaler, or the HPA `minReplicas`/`maxReplicas` window is too narrow to absorb traffic spikes.

---

## Diagnosis Steps

### Step 1 — Check pod CPU usage

```bash
kubectl top pods -n <namespace> --sort-by=cpu
kubectl top pods -n <namespace> -l app=<app-label> --sort-by=cpu
```

### Step 2 — Check node CPU usage

```bash
kubectl top nodes
kubectl describe node <node-name> | grep -A 20 "Allocated resources"
```

### Step 3 — Check CPU limits and throttling

```bash
kubectl describe pod <pod-name> -n <namespace> | grep -A 6 "Limits\|Requests"
```

Prometheus query for CPU throttling ratio:
```promql
rate(container_cpu_cfs_throttled_seconds_total{namespace="<namespace>",container="<container>"}[5m])
/ rate(container_cpu_cfs_periods_total{namespace="<namespace>",container="<container>"}[5m])
```

### Step 4 — Datadog queries

CPU usage over time:
```
avg:kubernetes.cpu.usage.total{kube_namespace:<namespace>,kube_deployment:<deployment>} by {pod_name}
```

CPU throttling:
```
avg:kubernetes.cpu.throttled.time{kube_namespace:<namespace>} by {pod_name}
```

### Step 5 — Check HPA status

```bash
kubectl get hpa -n <namespace>
kubectl describe hpa <hpa-name> -n <namespace>
```

### Step 6 — Inspect application metrics

```bash
# JVM heap and GC (if applicable)
kubectl exec -it <pod-name> -n <namespace> -- jstat -gc <pid> 1000 10

# Check process CPU breakdown inside container
kubectl exec -it <pod-name> -n <namespace> -- top -b -n 3 -H
```

### Step 7 — Review recent deployments

```bash
kubectl rollout history deployment/<deployment-name> -n <namespace>
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | grep -i deploy | tail -20
```

---

## Remediation Steps

### Immediate Relief

1. **Scale out horizontally** if HPA is not auto-scaling fast enough:
   ```bash
   kubectl scale deployment <deployment-name> -n <namespace> --replicas=<current+2>
   ```

2. **Increase CPU limits** to stop throttling (temporary measure — address root cause in parallel):
   ```bash
   kubectl set resources deployment <deployment-name> -n <namespace> \
     --limits=cpu=2000m --requests=cpu=500m
   kubectl rollout restart deployment/<deployment-name> -n <namespace>
   ```

3. **Configure or tune the HPA** to react faster to CPU spikes:
   ```yaml
   apiVersion: autoscaling/v2
   kind: HorizontalPodAutoscaler
   metadata:
     name: <deployment-name>
     namespace: <namespace>
   spec:
     scaleTargetRef:
       apiVersion: apps/v1
       kind: Deployment
       name: <deployment-name>
     minReplicas: 2
     maxReplicas: 20
     metrics:
     - type: Resource
       resource:
         name: cpu
         target:
           type: Utilization
           averageUtilization: 60
     behavior:
       scaleUp:
         stabilizationWindowSeconds: 30
         policies:
         - type: Percent
           value: 100
           periodSeconds: 60
   ```
   Apply: `kubectl apply -f hpa.yaml`

4. **Kill or pause a runaway background job** if identified:
   ```bash
   kubectl exec -it <pod-name> -n <namespace> -- kill -15 <pid>
   # or delete the CronJob to stop new triggers
   kubectl delete cronjob <job-name> -n <namespace>
   ```

5. **Profile the application** to identify the CPU hotspot:
   - For Go: enable pprof endpoint and capture CPU profile: `go tool pprof http://localhost:6060/debug/pprof/profile?seconds=30`
   - For JVM: `kubectl exec -it <pod-name> -- jstack <pid> > thread-dump.txt`
   - For Python: use `py-spy top --pid <pid>` inside the container

6. **Rollback the deployment** if a recent release introduced a regression:
   ```bash
   kubectl rollout undo deployment/<deployment-name> -n <namespace>
   kubectl rollout status deployment/<deployment-name> -n <namespace>
   ```

7. **Enable GOMAXPROCS awareness** (Go services) by setting `GOMAXPROCS` based on the container's CPU quota using `uber-go/automaxprocs`.

8. **Tune JVM heap** if GC is the CPU driver:
   ```bash
   kubectl set env deployment/<deployment-name> -n <namespace> \
     JAVA_OPTS="-Xms256m -Xmx512m -XX:+UseG1GC -XX:MaxGCPauseMillis=200"
   ```

---

## Prevention Measures

- **Right-size CPU requests and limits** using VPA (VerticalPodAutoscaler) recommendations after 7 days of production data.
- **Configure HPA on all stateless Deployments** with `averageUtilization: 60` to leave headroom.
- **Set Prometheus alert** for CPU throttling above 25% sustained for 10 minutes.
- **Run load tests** in staging before every release to catch CPU regressions early.
- **Use resource quotas** per namespace to prevent any single team from starving cluster CPU.
- **Profile applications** in CI using benchmark tests for performance-critical code paths.
- **Set `PodDisruptionBudget`** so HPA scale-down does not reduce replicas below minimum safe count.

---

## Escalation Criteria

- CPU throttling > 50% sustained for more than 30 minutes with no reduction after scaling.
- HPA has reached `maxReplicas` and CPU is still saturated.
- Service error rate (5xx) is increasing in correlation with CPU saturation.
- The issue affects multiple namespaces or multiple unrelated services simultaneously (suspect node-level issue or misconfigured shared component).
- Profiling identifies a bug (infinite loop, mutex deadlock) requiring an emergency hotfix deployment.

**On-call contact:** `#sre-oncall` Slack channel
**Incident severity:** P2 if latency SLA is breached, P3 otherwise
