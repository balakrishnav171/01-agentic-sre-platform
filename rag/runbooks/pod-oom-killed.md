# Pod OOMKilled Runbook

**severity:** high
**category:** kubernetes
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

`OOMKilled` (Out of Memory Killed) occurs when a container exceeds its memory limit. The Linux kernel's OOM killer terminates the container process, and Kubernetes records the exit reason as `OOMKilled`. The pod then enters `CrashLoopBackOff` if the restart policy is `Always` (the default). This is distinct from node-level memory pressure, which causes pod eviction.

---

## Symptoms

- `kubectl describe pod <pod-name>` shows `Last State: Terminated — Reason: OOMKilled`
- Exit code `137` (128 + SIGKILL signal 9)
- Pod enters `CrashLoopBackOff` after repeated OOM kills
- Prometheus alert: `KubeContainerOOMKilled`
- Datadog monitor: `kubernetes.memory.working_set` approaching or exceeding the configured limit
- Grafana: container memory working set graph shows a sawtooth pattern (fill → kill → reset → fill)
- Application logs abruptly terminate with no error message (process receives SIGKILL, no shutdown hook)

---

## Memory Pressure Causes

### 1. Memory Limit Set Too Low
The container's `resources.limits.memory` was estimated conservatively and does not account for peak memory usage, GC overhead, or off-heap native memory.

### 2. Memory Leak in Application Code
The application allocates memory and fails to release it (missing `close()` calls, leaked goroutines accumulating state, growing caches without eviction policies).

### 3. Excessive Request Concurrency
Too many concurrent requests arriving simultaneously, each allocating memory, causing aggregate usage to spike above the limit.

### 4. Large In-Memory Data Structures
Loading large datasets, files, or query results entirely into memory (e.g. reading a 2 GB CSV file into RAM, materialising a full database table).

### 5. Native / Off-Heap Memory Growth (JVM, Go)
JVM's off-heap memory (direct byte buffers, native libraries), or Go's runtime using more memory than reflected in heap metrics alone.

### 6. Sidecar Containers Consuming Shared Memory
A logging or metrics sidecar (Fluent Bit, Prometheus exporter) in the same pod consuming memory that competes with the main container (note: limits are per-container in Kubernetes).

---

## Diagnosis Steps

### Step 1 — Confirm OOMKill and last exit code

```bash
kubectl describe pod <pod-name> -n <namespace>
# Look for:
# Last State:     Terminated
#   Reason:       OOMKilled
#   Exit Code:    137
```

### Step 2 — Check current memory requests and limits

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath=\
'{.spec.containers[*].resources}' | python3 -m json.tool
```

### Step 3 — Check current memory usage (if pod is running)

```bash
kubectl top pod <pod-name> -n <namespace>
kubectl top pod -n <namespace> --sort-by=memory | head -10
```

### Step 4 — Query Prometheus for memory trend

```promql
# Working set memory over time
container_memory_working_set_bytes{namespace="<namespace>", pod=~"<pod-prefix>.*"}

# Memory limit
kube_pod_container_resource_limits{resource="memory", namespace="<namespace>"}

# OOMKill events
kube_pod_container_status_last_terminated_reason{reason="OOMKilled", namespace="<namespace>"}
```

### Step 5 — Inspect application heap (JVM)

```bash
# Force heap dump before next OOM (if pod is momentarily running)
kubectl exec -it <pod-name> -n <namespace> -- \
  jmap -dump:format=b,file=/tmp/heap.hprof <pid>

kubectl cp <namespace>/<pod-name>:/tmp/heap.hprof ./heap.hprof
# Analyse with Eclipse MAT or JVisualVM
```

### Step 6 — Profile Go memory

```bash
# If pprof is enabled
kubectl port-forward pod/<pod-name> 6060:6060 -n <namespace> &
go tool pprof http://localhost:6060/debug/pprof/heap
```

### Step 7 — Check for memory leaks using pod logs before kill

```bash
kubectl logs <pod-name> -n <namespace> --previous | grep -i -E "memory|oom|heap|gc|leak" | tail -50
```

### Step 8 — Check node memory pressure

```bash
kubectl describe node <node-name> | grep -A 5 "MemoryPressure\|Conditions"
kubectl top nodes --sort-by=memory
```

---

## Remediation Steps

1. **Immediate: Increase the memory limit** to give the container headroom while investigating the root cause:
   ```bash
   kubectl set resources deployment <deployment-name> -n <namespace> \
     --limits=memory=1Gi --requests=memory=512Mi
   kubectl rollout restart deployment/<deployment-name> -n <namespace>
   ```

2. **Monitor memory trajectory** after the increase to confirm it stabilises:
   ```bash
   watch -n 5 kubectl top pod -n <namespace> -l app=<app-label>
   ```

3. **For JVM memory leaks** — enable GC logging and heap dump on OOM:
   ```bash
   kubectl set env deployment/<deployment-name> -n <namespace> \
     JAVA_OPTS="-Xmx768m -XX:+HeapDumpOnOutOfMemoryError \
     -XX:HeapDumpPath=/tmp/heapdump.hprof \
     -Xlog:gc*:stdout:time,uptime,level,tags"
   ```

4. **For Go memory leaks** — enable pprof and capture heap profile on the next occurrence.

5. **For large in-memory datasets** — refactor the code to use streaming or pagination:
   - Replace `SELECT *` with paginated queries using `LIMIT`/`OFFSET` or cursor-based pagination.
   - Use `io.Reader` / generator patterns instead of loading entire files into byte slices.

6. **Tune JVM heap and GC settings** for container awareness:
   ```bash
   kubectl set env deployment/<deployment-name> -n <namespace> \
     JAVA_TOOL_OPTIONS="-XX:+UseContainerSupport \
     -XX:MaxRAMPercentage=75.0 \
     -XX:+UseG1GC \
     -XX:MaxGCPauseMillis=200"
   ```

7. **Review and fix cache eviction policies** — ensure in-memory caches have:
   - Maximum size caps (`maximumSize` in Guava Cache, `maxsize` in cachetools)
   - TTL-based expiry to prevent unbounded growth

8. **Configure Vertical Pod Autoscaler (VPA) in recommendation mode** to track actual memory usage and suggest appropriate limits:
   ```yaml
   apiVersion: autoscaling.k8s.io/v1
   kind: VerticalPodAutoscaler
   metadata:
     name: <deployment-name>-vpa
     namespace: <namespace>
   spec:
     targetRef:
       apiVersion: apps/v1
       kind: Deployment
       name: <deployment-name>
     updatePolicy:
       updateMode: "Off"  # Recommendation only
   ```

9. **Check for goroutine leaks** (Go services):
   ```bash
   kubectl port-forward pod/<pod-name> 6060:6060 -n <namespace> &
   curl http://localhost:6060/debug/pprof/goroutine?debug=1 | head -50
   ```

10. **If a code bug is confirmed** — rollback the deployment to the previous working version:
    ```bash
    kubectl rollout undo deployment/<deployment-name> -n <namespace>
    kubectl rollout status deployment/<deployment-name> -n <namespace>
    ```

---

## Prevention Measures

- **Use VPA in recommendation mode** for 2 weeks after a new service launch to calibrate memory limits based on real traffic.
- **Set memory limits = 2x memory requests** as a starting baseline; tune downward based on VPA data.
- **Add OOMKill Prometheus alert**: `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} > 0` with severity `high`.
- **Enable heap dumps on OOM** in all JVM services via `JVM_OPTS`.
- **Conduct memory profiling** as part of the load testing pipeline before each major release.
- **Review GC logs** in staging to ensure pause times are acceptable and heap sizing is correct.
- **Set resource quotas per namespace** to prevent a memory leak in one service from consuming cluster-wide memory.

---

## Escalation Criteria

- OOMKill is occurring every few minutes despite limit increases (possible catastrophic memory leak).
- Multiple pods in the same Deployment are OOMKilling simultaneously.
- Memory limit would need to exceed the node's allocatable memory, requiring a node type change.
- Heap dump analysis reveals data corruption or an exploited vulnerability.
- The service is customer-facing and SLA breach is imminent.

**On-call contact:** `#sre-oncall` Slack channel
**Incident severity:** P1 if customer-impacting, P2 otherwise
