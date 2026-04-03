# Node NotReady Runbook

**severity:** critical
**category:** kubernetes
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

A Kubernetes node in `NotReady` state means the kubelet on that node is not communicating heartbeats to the control plane. After the node status update grace period (default 40 seconds), the node controller marks the node `NotReady`. After a further `pod-eviction-timeout` (default 5 minutes), pods on the node are evicted and rescheduled elsewhere — if cluster capacity exists.

A `NotReady` node is a critical infrastructure event that can cause widespread pod eviction and service disruption.

---

## Symptoms

- `kubectl get nodes` shows node status `NotReady`
- Prometheus alert: `KubeNodeNotReady` — node has been NotReady for > 1 minute
- Pods on the node enter `Unknown` phase (kubelet not reporting status)
- After eviction timeout, pods on the node are rescheduled to other nodes (if capacity allows)
- `kubectl describe node <node-name>` shows `KubeletNotReady`, `KubeletHasSufficientMemory: False`, or `KubeletHasDiskPressure: True`
- Node conditions: `MemoryPressure`, `DiskPressure`, `PIDPressure`, `NetworkUnavailable`
- SSH to the node may be unreachable

---

## Root Causes

### 1. kubelet Process Crashed or Stopped
The kubelet daemon on the node has exited or been killed. The node can no longer report its status to the API server.

### 2. Disk Pressure
The node's filesystem is full or critically low. Kubelet evicts pods to free disk space and may stop functioning if `/var/lib/kubelet` is on the full filesystem.

### 3. Memory Pressure
Node is under severe memory pressure. The kernel OOM killer may have killed the kubelet process, or the kubelet itself evicted all pods and entered a degraded state.

### 4. Network Issues
- Network interface down or misconfigured
- CNI plugin failure (Calico, Flannel, Cilium) — pods cannot communicate
- Route tables corrupted or missing
- Node cannot reach the API server endpoint

### 5. Node Kernel Panic or Hardware Failure
The operating system has panicked, or underlying hardware (disk, NIC, DIMM) has failed.

### 6. Container Runtime Failure
Docker, containerd, or CRI-O has crashed or become unresponsive. Kubelet cannot manage containers.

### 7. Certificate Expiry
Node kubelet TLS certificate has expired. Kubelet cannot authenticate to the API server.

---

## Diagnosis Steps

### Step 1 — Confirm node status and conditions

```bash
kubectl get nodes -o wide
kubectl describe node <node-name>
# Look at Conditions: MemoryPressure, DiskPressure, PIDPressure, Ready
```

### Step 2 — List pods on the affected node

```bash
kubectl get pods --all-namespaces \
  --field-selector=spec.nodeName=<node-name> -o wide
```

### Step 3 — SSH to the node (if accessible)

```bash
ssh <node-user>@<node-ip>

# Check kubelet status
sudo systemctl status kubelet
sudo journalctl -u kubelet --since "30 minutes ago" | tail -100

# Check container runtime
sudo systemctl status containerd
sudo systemctl status docker

# Check disk usage
df -h
du -sh /var/lib/kubelet/* 2>/dev/null | sort -rh | head -20

# Check memory
free -h
vmstat 1 5

# Check network
ip a
ip route
ping <api-server-ip>

# Check for kernel errors
sudo dmesg | tail -50
sudo journalctl -k --since "1 hour ago" | grep -i -E "error|panic|oom" | tail -50
```

### Step 4 — Check for CNI issues

```bash
# On the node
ls /etc/cni/net.d/
/opt/cni/bin/calico-node --version 2>/dev/null || echo "Calico not found"

# From the control plane
kubectl get pods -n kube-system | grep -E "calico|flannel|cilium|weave"
kubectl logs -n kube-system <cni-pod-on-affected-node> --tail=50
```

### Step 5 — Check certificate validity

```bash
# On the node
sudo openssl x509 -in /var/lib/kubelet/pki/kubelet.crt -noout -dates
sudo openssl x509 -in /etc/kubernetes/pki/apiserver.crt -noout -dates 2>/dev/null
```

### Step 6 — Check cloud provider node status

```bash
# AWS EC2 instance health
aws ec2 describe-instance-status --instance-ids <instance-id>

# GCP Compute Engine
gcloud compute instances describe <instance-name> --zone=<zone>

# Azure VM
az vm show -g <resource-group> -n <vm-name> --query "instanceView.statuses"
```

---

## Drain and Cordon Procedures

### Cordon the node (prevent new pod scheduling)

```bash
# Cordon immediately to prevent new pods from being scheduled on the node
kubectl cordon <node-name>
kubectl get node <node-name>  # Should show SchedulingDisabled
```

### Drain the node (evict all pods)

```bash
# Drain with grace period — evicts all pods (except DaemonSets)
kubectl drain <node-name> \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --grace-period=60 \
  --timeout=120s

# If drain is stuck on a PodDisruptionBudget, force after waiting
kubectl drain <node-name> \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --disable-eviction=true \
  --force
```

### Restart kubelet on the node

```bash
sudo systemctl restart kubelet
sudo systemctl status kubelet

# Watch node come back
kubectl get nodes -w
```

### Restart container runtime

```bash
sudo systemctl restart containerd
# or
sudo systemctl restart docker
sudo systemctl restart kubelet
```

### Uncordon after the node is healthy

```bash
kubectl uncordon <node-name>
kubectl get nodes
# Verify node is Ready and schedulable
```

---

## Node Replacement Procedure (Cloud)

If the node cannot be recovered and must be replaced:

```bash
# 1. Cordon and drain (see above)
kubectl cordon <node-name>
kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --force

# 2. Delete the node object from Kubernetes
kubectl delete node <node-name>

# 3. Terminate the underlying VM (cloud provider)
# AWS:
aws ec2 terminate-instances --instance-ids <instance-id>
# The ASG / node group will replace it automatically

# 4. Monitor the new node joining
kubectl get nodes -w
```

---

## Prevention Measures

- **Enable cluster autoscaler** to automatically remove NotReady nodes and replace them from the node group.
- **Set up node problem detector** (`node-problem-detector` DaemonSet) to report kernel and system issues as node conditions.
- **Monitor disk usage** on all nodes; alert at 70% and 85% full.
- **Monitor kubelet certificate expiry** and automate rotation using kubeadm or cert-manager.
- **Use managed node groups** (EKS managed groups, GKE node pools) that automatically replace unhealthy nodes.
- **Set `podDisruptionBudget`** for all critical services to ensure graceful redistribution during node drains.
- **Configure node taints** for under-resourced nodes to prevent over-scheduling.

---

## Escalation Criteria

- Multiple nodes are simultaneously NotReady (possible network partition or control plane issue).
- The node cannot be SSH'd to (physical/hardware failure may require datacenter intervention).
- Evicted pods cannot be rescheduled due to insufficient cluster capacity.
- Data-bearing pods (databases, stateful sets with local PVs) were on the node and may have lost data.
- The control plane itself is affected (API server unreachable from multiple nodes).

**On-call contact:** `#sre-oncall` Slack channel
**Incident severity:** P1 — node NotReady is a critical infrastructure failure
