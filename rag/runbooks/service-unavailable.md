# Service Unavailable (503) Runbook

**severity:** high
**category:** networking
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

An HTTP `503 Service Unavailable` response from a Kubernetes service means that the load balancer or ingress controller has no healthy backend endpoints to forward traffic to. This can be caused by pod selector mismatches, all pods failing health checks, endpoint objects having no ready addresses, or the service itself being misconfigured. A 503 is always customer-visible and should be treated with high urgency.

---

## Symptoms

- HTTP 503 responses from the service endpoint or public URL
- `kubectl get endpoints <service-name> -n <namespace>` shows `<none>` in the `ENDPOINTS` column
- Ingress controller access logs show `upstream connect error` or `no healthy upstream`
- Prometheus alert: `KubeEndpointNotReady`, `ServiceUnavailable`, or `TargetDown`
- Datadog synthetics check failing with `503`
- `kubectl describe service <service-name>` shows no matching pods (selector mismatch)
- All pod health checks (`readinessProbe`) are failing

---

## Root Causes

### 1. Pod Selector Mismatch
The `Service`'s `selector` labels do not match the `labels` on any running pod. This is the most common cause — a typo or missed label update after a deployment change.

### 2. All Pods Failing Readiness Probes
Pods are running but not passing the `readinessProbe`, so they are excluded from the endpoint list. Kubernetes only routes traffic to pods that are `Ready`.

### 3. No Running Pods (All Crashed or Evicted)
All pods in the target Deployment have crashed (CrashLoopBackOff, OOMKilled) or been evicted, leaving zero running replicas.

### 4. Health Check Endpoint Not Implemented
The application does not implement the path specified in `readinessProbe.httpGet.path`, causing a 404 or 500 response that fails the probe.

### 5. Readiness Probe Misconfigured
`initialDelaySeconds` too short, `failureThreshold` too low, or wrong port configured — causing valid pods to be marked not-ready.

### 6. Network Policy Blocking Traffic
A `NetworkPolicy` is blocking traffic from the ingress controller / load balancer namespace to the service's pod namespace.

### 7. Service Port Mismatch
The Service `targetPort` does not match the port the container is actually listening on, causing connection refused errors.

### 8. Ingress Misconfiguration
The Ingress resource references the wrong `serviceName` or `servicePort`, or the TLS secret is missing causing the ingress controller to reject the route.

---

## Diagnosis Steps

### Step 1 — Check service endpoints

```bash
kubectl get endpoints <service-name> -n <namespace>
# If ENDPOINTS shows <none> → no pods are matching the selector

kubectl describe endpoints <service-name> -n <namespace>
```

### Step 2 — Verify service selector matches pod labels

```bash
# Get the service selector
kubectl get service <service-name> -n <namespace> \
  -o jsonpath='{.spec.selector}'

# List pods with those labels
kubectl get pods -n <namespace> -l app=<label-value>

# Compare pod labels directly
kubectl get pods -n <namespace> --show-labels | grep <app-name>
```

### Step 3 — Check pod readiness

```bash
kubectl get pods -n <namespace> -l app=<app-label>
# READY column should be "1/1" (or N/N for multi-container pods)

kubectl describe pod <pod-name> -n <namespace> | grep -A 10 "Readiness\|Conditions"
```

### Step 4 — Test the health check endpoint directly

```bash
# Port-forward to the pod and test the readiness endpoint
kubectl port-forward pod/<pod-name> 8080:8080 -n <namespace> &
curl -v http://localhost:8080/healthz
curl -v http://localhost:8080/ready
```

### Step 5 — Check readiness probe configuration

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath=\
'{.spec.containers[*].readinessProbe}' | python3 -m json.tool
```

### Step 6 — Check pod logs for health check failures

```bash
kubectl logs <pod-name> -n <namespace> --tail=100 | \
  grep -i -E "health|ready|probe|error|failed" | tail -30
```

### Step 7 — Check NetworkPolicies

```bash
kubectl get networkpolicy -n <namespace>
kubectl describe networkpolicy <policy-name> -n <namespace>
```

### Step 8 — Check service and ingress configuration

```bash
kubectl describe service <service-name> -n <namespace>
kubectl get ingress -n <namespace>
kubectl describe ingress <ingress-name> -n <namespace>

# Check ingress controller pods
kubectl get pods -n ingress-nginx
kubectl logs -n ingress-nginx <ingress-controller-pod> --tail=50
```

### Step 9 — Test connectivity from within the cluster

```bash
kubectl run debug-pod --image=curlimages/curl -n <namespace> --rm -it \
  --restart=Never -- curl -v http://<service-name>.<namespace>.svc.cluster.local/healthz
```

---

## Remediation Steps

1. **For pod selector mismatch** — fix the Service selector to match the pod labels:
   ```bash
   kubectl edit service <service-name> -n <namespace>
   # Update .spec.selector to match pod labels exactly
   # Verify immediately:
   kubectl get endpoints <service-name> -n <namespace>
   ```

2. **For readiness probe path mismatch** — implement the health endpoint or fix the probe path:
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> \
     --type='json' -p='[{
       "op":"replace",
       "path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path",
       "value":"/health"
     }]'
   kubectl rollout restart deployment/<deployment-name> -n <namespace>
   ```

3. **For overly strict readiness probe** — increase `initialDelaySeconds` and `failureThreshold`:
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> \
     --type='json' -p='[
       {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/initialDelaySeconds","value":30},
       {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/failureThreshold","value":5}
     ]'
   ```

4. **For no running pods** — follow CrashLoopBackOff or OOMKilled runbook to restore pods, then verify endpoints recover automatically once pods are Ready.

5. **For port mismatch** — correct the `targetPort` in the Service spec:
   ```bash
   kubectl edit service <service-name> -n <namespace>
   # Update .spec.ports[*].targetPort to match container's actual listening port
   ```

6. **For NetworkPolicy blocking** — add an ingress rule to allow traffic from the ingress controller:
   ```yaml
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: allow-ingress-controller
     namespace: <namespace>
   spec:
     podSelector:
       matchLabels:
         app: <app-label>
     ingress:
     - from:
       - namespaceSelector:
           matchLabels:
             kubernetes.io/metadata.name: ingress-nginx
   ```

7. **For ingress misconfiguration** — verify and correct the Ingress resource:
   ```bash
   kubectl describe ingress <ingress-name> -n <namespace>
   kubectl edit ingress <ingress-name> -n <namespace>
   # Ensure serviceName and servicePort match the Service object
   ```

8. **Force-delete and recreate the Endpoint slice** if endpoints are stale:
   ```bash
   kubectl delete endpointslice -n <namespace> \
     -l kubernetes.io/service-name=<service-name>
   # Kubernetes will recreate automatically
   ```

---

## Prevention Measures

- **Use `kubectl apply --dry-run=server`** before applying Service changes to catch selector typos.
- **Implement standardised label conventions** (`app`, `version`, `component`) and enforce them with OPA/Gatekeeper policies.
- **Add Prometheus alert** for `kube_endpoint_address_not_ready > 0` sustained for 2 minutes.
- **Use readiness probes consistently** — every production container must have a readiness probe.
- **Test health endpoints** in CI pipeline integration tests before deploying to production.
- **Set `minReadySeconds`** on Deployments to ensure pods are fully ready before being added to the endpoint pool.

---

## Escalation Criteria

- 503s are occurring on a payment, authentication, or other business-critical service.
- All remediation steps have been applied and endpoints are still empty.
- Multiple services in the same namespace or cluster are simultaneously unavailable.
- The ingress controller itself is crashing or not responding.
- A data layer dependency (database, cache) is unavailable and causing the readiness probe to fail.

**On-call contact:** `#sre-oncall` Slack channel
**Incident severity:** P1 if revenue-impacting, P2 for internal services
