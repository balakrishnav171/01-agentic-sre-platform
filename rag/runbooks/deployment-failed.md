# Deployment Failed Runbook

**severity:** high
**category:** kubernetes
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

A failed Kubernetes Deployment prevents new pods from reaching `Running` state. This can manifest as `ImagePullBackOff`, `ErrImagePull`, quota exceeded errors, resource constraint violations, or pods stuck in `Pending`. Regardless of cause, the current rollout stalls and the previous ReplicaSet may still be serving traffic (depending on the rollout strategy).

---

## Symptoms

- `kubectl rollout status deployment/<name>` reports `error: deployment ... exceeded its progress deadline`
- `kubectl get pods -n <namespace>` shows pods in `ImagePullBackOff`, `ErrImagePull`, `Pending`, or `Init:Error` states
- CI/CD pipeline deployment step times out or fails
- Prometheus alert: `KubeDeploymentReplicasMismatch` — desired replicas != available replicas
- New pods never transition from `Pending` to `Running`
- `kubectl get events -n <namespace>` shows `FailedScheduling`, `Failed to pull image`, or quota errors

---

## Root Causes

### 1. ImagePullBackOff / ErrImagePull
- Image tag does not exist in the registry
- Registry authentication failure (expired imagePullSecret, rotated credentials)
- Private registry unreachable from the node (network policy, firewall)
- Incorrect image name or tag in the Deployment spec

### 2. ResourceQuota Exceeded
The namespace's `ResourceQuota` for CPU, memory, or object count has been exceeded. New pods cannot be scheduled.

### 3. Insufficient Node Resources
No node in the cluster has sufficient CPU/memory to satisfy the pod's resource requests. Pods remain `Pending` with `FailedScheduling` events.

### 4. PodDisruptionBudget Violation
A PDB prevents Kubernetes from replacing old pods during a rolling update, causing the rollout to stall.

### 5. Invalid Pod Spec
A syntax or semantic error in the Deployment manifest (invalid env var reference, unsupported field, schema validation failure).

### 6. Liveness / Readiness Probe Failure During Rollout
New pods start but immediately fail health checks, preventing them from becoming `Ready`. The rolling update stalls at the `maxUnavailable` threshold.

### 7. Init Container Failure
An init container fails to complete successfully, preventing the main container from starting.

---

## Diagnosis Steps

### Step 1 — Check deployment rollout status

```bash
kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=2m
kubectl get deployment <deployment-name> -n <namespace> -o wide
```

### Step 2 — Inspect pods for error states

```bash
kubectl get pods -n <namespace> -l app=<app-label>
kubectl describe pod <failing-pod-name> -n <namespace>
```

### Step 3 — Check events for scheduling and image errors

```bash
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | tail -30
```

### Step 4 — For ImagePullBackOff — verify image and credentials

```bash
# Confirm the image tag exists in the registry
docker manifest inspect <image>:<tag>

# Check imagePullSecret is present and valid
kubectl get secret <pull-secret-name> -n <namespace>
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.imagePullSecrets}'
```

### Step 5 — For Quota errors — check namespace quota

```bash
kubectl describe resourcequota -n <namespace>
kubectl get resourcequota -n <namespace>
kubectl top pods -n <namespace>
```

### Step 6 — For Pending pods — check node capacity

```bash
kubectl describe node <node-name> | grep -A 10 "Allocated resources"
kubectl get nodes -o custom-columns=\
NAME:.metadata.name,CPU:.status.allocatable.cpu,MEMORY:.status.allocatable.memory
```

### Step 7 — Check PodDisruptionBudget

```bash
kubectl get pdb -n <namespace>
kubectl describe pdb <pdb-name> -n <namespace>
```

### Step 8 — Inspect init containers

```bash
kubectl logs <pod-name> -n <namespace> -c <init-container-name>
kubectl describe pod <pod-name> -n <namespace> | grep -A 10 "Init Containers"
```

---

## Remediation Steps

1. **For ImagePullBackOff — fix the image reference** in the Deployment and apply:
   ```bash
   kubectl set image deployment/<deployment-name> \
     <container-name>=<correct-image>:<correct-tag> -n <namespace>
   ```

2. **For ImagePullBackOff — refresh the imagePullSecret**:
   ```bash
   kubectl delete secret <pull-secret-name> -n <namespace>
   kubectl create secret docker-registry <pull-secret-name> \
     --docker-server=<registry> \
     --docker-username=<user> \
     --docker-password=<password> \
     -n <namespace>
   kubectl rollout restart deployment/<deployment-name> -n <namespace>
   ```

3. **For ResourceQuota exceeded — identify and clean up unused resources**:
   ```bash
   kubectl delete pods --field-selector=status.phase=Succeeded -n <namespace>
   kubectl delete pods --field-selector=status.phase=Failed -n <namespace>
   # Request quota increase from cluster admin if needed
   kubectl edit resourcequota <quota-name> -n <namespace>
   ```

4. **For insufficient node resources — trigger cluster autoscaler** (if enabled) or manually add nodes, or reduce resource requests temporarily:
   ```bash
   kubectl set resources deployment <deployment-name> -n <namespace> \
     --requests=cpu=100m,memory=128Mi
   ```

5. **For PodDisruptionBudget violation — temporarily relax the PDB**:
   ```bash
   kubectl patch pdb <pdb-name> -n <namespace> \
     --type='json' -p='[{"op":"replace","path":"/spec/minAvailable","value":0}]'
   kubectl rollout restart deployment/<deployment-name> -n <namespace>
   # Restore PDB after rollout completes
   kubectl patch pdb <pdb-name> -n <namespace> \
     --type='json' -p='[{"op":"replace","path":"/spec/minAvailable","value":1}]'
   ```

6. **For invalid pod spec — validate and fix the manifest**:
   ```bash
   kubectl apply --dry-run=client -f deployment.yaml
   kubectl apply --dry-run=server -f deployment.yaml
   ```

---

## Rollback Procedure

When a deployment failure occurs and the previous version must be restored:

```bash
# View rollout history to identify the last stable revision
kubectl rollout history deployment/<deployment-name> -n <namespace>

# Rollback to the immediately preceding revision
kubectl rollout undo deployment/<deployment-name> -n <namespace>

# Or rollback to a specific revision
kubectl rollout undo deployment/<deployment-name> -n <namespace> --to-revision=<N>

# Monitor rollback progress
kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=5m

# Confirm pods are running on the rolled-back version
kubectl get pods -n <namespace> -l app=<app-label> -o wide
kubectl describe deployment <deployment-name> -n <namespace> | grep Image
```

After rollback, create a post-incident action item to fix the root cause before re-deploying.

---

## Prevention Measures

- **Enforce image tag immutability** — never use `:latest` in production; use digest-pinned or semantic versioned tags.
- **Add `progressDeadlineSeconds`** to all Deployments to ensure failed rollouts are detected early (default is 600s; set to 300s for fast feedback).
- **Validate manifests in CI** using `kubeval`, `kubeconform`, or `kubectl apply --dry-run=server`.
- **Use Helm with `--atomic` flag** so deployments automatically rollback on failure.
- **Monitor namespace quotas** and alert when utilisation exceeds 80%.
- **Test imagePullSecrets** as part of the deployment pipeline before production.
- **Set `maxSurge` and `maxUnavailable`** appropriately for the service's traffic tolerance.

---

## Escalation Criteria

- Rollback also fails (both current and previous images are broken).
- The failure is blocking a critical release with customer-facing SLA implications.
- Quota increase requires approval from the cluster admin team.
- Node resource exhaustion is cluster-wide (not namespace-specific).

**On-call contact:** `#sre-oncall` Slack channel
**Incident severity:** P2 if deployment is blocking production, P3 if staging only
