# CrashLoopBackOff Runbook

**severity:** high
**category:** kubernetes
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

A `CrashLoopBackOff` status indicates that a Kubernetes pod is starting, crashing, and then Kubernetes is waiting progressively longer before restarting it. The back-off timer starts at 10 seconds and doubles with each failure, up to a maximum of 5 minutes. This is one of the most common and critical Kubernetes failure modes.

---

## Symptoms

- Pod status shows `CrashLoopBackOff` in `kubectl get pods`
- Pod restart count is continuously incrementing
- `kubectl describe pod <pod-name>` shows `Back-off restarting failed container`
- Application logs show repeated startup failures
- Alerts firing: `KubePodCrashLooping`, `PodRestartingTooOften`
- Service endpoints become unavailable
- Health checks failing in Datadog / Prometheus

---

## Root Causes

### 1. Application Configuration Error
The application fails to start due to missing or invalid environment variables, configuration files, or secrets. Common in newly deployed versions or after config changes.

### 2. Missing or Inaccessible Secrets / ConfigMaps
The pod references a `Secret` or `ConfigMap` that does not exist in the namespace, causing the container to exit with a non-zero code before the main process starts.

### 3. Resource Constraints (OOMKilled)
The container exceeds its memory limit and is killed by the kernel OOM killer. The pod then enters `CrashLoopBackOff` after repeated OOM kills. Check `kubectl describe pod` for `OOMKilled` as the last state reason.

### 4. Liveness Probe Misconfiguration
An overly aggressive liveness probe (short `initialDelaySeconds`, too low `failureThreshold`) kills the container before the application finishes initialising, causing a restart loop.

### 5. Dependency Unavailability
The application requires an external service (database, message broker, cache) that is unreachable at startup and exits with a non-zero code instead of waiting/retrying.

### 6. Image Pull or Startup Script Errors
Entrypoint script fails (permission error, syntax error in shell script, missing binary inside the container image).

### 7. Port Conflict
The container attempts to bind a port that is already in use on the node (only possible with `hostPort` or `hostNetwork: true`).

### 8. Persistent Volume Mount Failure
A required PVC is not bound or has incompatible access mode, causing the container to fail to start.

---

## Diagnosis Steps

### Step 1 — Identify affected pods

```bash
kubectl get pods -n <namespace> --field-selector=status.phase=Running | grep -v Running
kubectl get pods -n <namespace> | grep CrashLoopBackOff
```

### Step 2 — Describe the pod for events and last state

```bash
kubectl describe pod <pod-name> -n <namespace>
```

Look for:
- `Last State: Terminated` — check `Reason` (OOMKilled, Error, Completed)
- `Exit Code` — non-zero means the process crashed
- `Events` section at the bottom for recent Kubernetes events

### Step 3 — Fetch current and previous container logs

```bash
# Current logs
kubectl logs <pod-name> -n <namespace> --tail=100

# Previous container instance logs (the one that crashed)
kubectl logs <pod-name> -n <namespace> --previous --tail=200
```

### Step 4 — Check environment variables and secrets

```bash
kubectl exec -it <pod-name> -n <namespace> -- env | sort
kubectl get secret <secret-name> -n <namespace> -o jsonpath='{.data}' | base64 -d
```

### Step 5 — Check resource usage

```bash
kubectl top pod <pod-name> -n <namespace>
kubectl describe pod <pod-name> -n <namespace> | grep -A 6 "Limits\|Requests"
```

### Step 6 — Check liveness/readiness probe configuration

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].livenessProbe}'
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].readinessProbe}'
```

### Step 7 — Inspect events across the namespace

```bash
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | tail -30
```

### Step 8 — Check if ConfigMaps and Secrets exist

```bash
kubectl get cm -n <namespace>
kubectl get secret -n <namespace>
```

---

## Remediation Steps

1. **Identify the root cause first** — read `kubectl logs <pod> --previous` before making any changes. Never restart blindly.

2. **For missing secrets or ConfigMaps** — create the missing resource or verify the reference name matches exactly (case-sensitive):
   ```bash
   kubectl create secret generic <secret-name> --from-literal=key=value -n <namespace>
   kubectl apply -f configmap.yaml -n <namespace>
   ```

3. **For OOMKilled** — increase the memory limit in the Deployment manifest:
   ```bash
   kubectl set resources deployment <deployment-name> -n <namespace> \
     --limits=memory=512Mi --requests=memory=256Mi
   ```

4. **For liveness probe failures** — patch the probe to give the app more startup time:
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":60}]'
   ```

5. **For application config errors** — update the ConfigMap or environment variables and trigger a rolling restart:
   ```bash
   kubectl edit configmap <configmap-name> -n <namespace>
   kubectl rollout restart deployment/<deployment-name> -n <namespace>
   ```

6. **For dependency failures** — verify the dependent service is running and reachable:
   ```bash
   kubectl get svc -n <namespace>
   kubectl exec -it <pod-name> -n <namespace> -- nc -zv <service-host> <port>
   ```

7. **For image entrypoint errors** — shell into the image using a debug override:
   ```bash
   kubectl run debug-pod --image=<image> -n <namespace> --rm -it \
     --command -- /bin/sh
   ```

8. **Rollback to the last working version** if the crash was introduced by a recent deployment:
   ```bash
   kubectl rollout history deployment/<deployment-name> -n <namespace>
   kubectl rollout undo deployment/<deployment-name> -n <namespace>
   kubectl rollout status deployment/<deployment-name> -n <namespace>
   ```

9. **For PVC mount failures** — verify PVC is bound and accessible:
   ```bash
   kubectl get pvc -n <namespace>
   kubectl describe pvc <pvc-name> -n <namespace>
   ```

10. **Verify recovery** — confirm pods are Running and restart count has stopped increasing:
    ```bash
    kubectl get pods -n <namespace> -w
    kubectl rollout status deployment/<deployment-name> -n <namespace>
    ```

---

## Prevention Measures

- **Set appropriate resource requests and limits** for all containers to prevent OOMKilled.
- **Use `startupProbe`** for applications with slow initialisation to avoid premature liveness probe failures.
- **Validate secrets and ConfigMaps exist before deployment** using admission webhooks or CI/CD pre-flight checks.
- **Implement init containers** to wait for dependencies (databases, queues) before the main container starts.
- **Enable pod disruption budgets (PDBs)** to prevent accidental disruption during maintenance.
- **Use Helm chart schema validation** or `kustomize` to catch misconfigurations before they reach the cluster.
- **Set `imagePullPolicy: IfNotPresent`** in production to avoid intermittent image pull failures.
- **Monitor pod restart counts** with a Prometheus alert: `kube_pod_container_status_restarts_total > 5 over 10m`.

---

## Escalation Criteria

Escalate to the on-call SRE lead or engineering team if any of the following are true:

- The CrashLoopBackOff persists after exhausting all standard remediation steps.
- More than 3 pods in the same Deployment are simultaneously CrashLoopBackOff.
- The pod is part of a critical data-path service (payments, auth, order processing).
- `kubectl logs --previous` shows a panic, segfault, or unrecoverable database corruption.
- The issue recurs within 30 minutes of a successful remediation.
- Data loss or data corruption is suspected.
- The incident is impacting SLA-bound services with active customer escalations.

**On-call contact:** `#sre-oncall` Slack channel
**Incident severity:** P1 if customer-impacting, P2 otherwise
**Runbook owner:** Platform SRE — review quarterly
