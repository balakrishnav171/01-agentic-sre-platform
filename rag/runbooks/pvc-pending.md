# PVC Pending Runbook

**severity:** medium
**category:** kubernetes
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

A `PersistentVolumeClaim` (PVC) stuck in `Pending` state prevents any pod that mounts it from starting (pods remain `Pending` indefinitely). PVCs are pending either because no suitable `PersistentVolume` is available for static provisioning, or because the dynamic provisioner has failed to create a new volume.

---

## Symptoms

- `kubectl get pvc -n <namespace>` shows `STATUS: Pending`
- Pods that reference the PVC remain in `Pending` state with event: `persistentvolumeclaim "<pvc-name>" not found` or `waiting for a volume to be created`
- `kubectl describe pvc <pvc-name>` shows events like:
  - `no persistent volumes available for this claim and no storage class is set`
  - `failed to provision volume with StorageClass "<class>": ...`
  - `exceeded quota: <quota-name>`
  - `ProvisioningFailed`
- Prometheus alert: `KubePersistentVolumeFillingUp` or custom PVC Pending alert

---

## Root Causes

### 1. StorageClass Not Found or Misconfigured
The PVC references a `storageClassName` that does not exist, is misspelled, or has been deleted. Without a valid StorageClass, the dynamic provisioner cannot create the backing volume.

### 2. Dynamic Provisioner Not Running
The storage provisioner pod (e.g., `ebs-csi-controller`, `gke-pd-csi-driver`, `azuredisk-csi`) is crashed or not deployed. Without the provisioner, PVCs cannot be dynamically fulfilled.

### 3. ResourceQuota Exceeded for PVC Storage
The namespace has a `ResourceQuota` with a `requests.storage` limit that has been reached. New PVCs cannot be created until existing ones are deleted.

### 4. Access Mode Mismatch
The PVC requests `ReadWriteMany` (RWX) but the StorageClass only supports `ReadWriteOnce` (RWO) volumes (e.g., AWS EBS). No volume can satisfy the claim.

### 5. Zone / Topology Mismatch
The requested StorageClass has `volumeBindingMode: WaitForFirstConsumer` and no pod has been scheduled yet, or the pod and available zone do not match.

### 6. No Available PersistentVolumes (Static Provisioning)
For statically provisioned PVs, no available PV matches the PVC's capacity, access mode, and selector.

### 7. Provisioner Quota Limits in the Cloud
The cloud account has hit a volume quota (AWS EBS volume limit, Azure disk quota per region). New volumes cannot be created.

---

## Diagnosis Steps

### Step 1 — Check PVC status and events

```bash
kubectl get pvc -n <namespace>
kubectl describe pvc <pvc-name> -n <namespace>
# Pay attention to Events at the bottom
```

### Step 2 — Verify StorageClass exists

```bash
kubectl get storageclass
kubectl describe storageclass <storage-class-name>
# Check: Provisioner, ReclaimPolicy, VolumeBindingMode, AllowVolumeExpansion
```

### Step 3 — Check provisioner pods

```bash
# AWS EBS CSI Driver
kubectl get pods -n kube-system | grep ebs-csi
kubectl logs -n kube-system <ebs-csi-controller-pod> -c csi-provisioner | tail -50

# GCP PD CSI
kubectl get pods -n kube-system | grep pd-csi
kubectl logs -n kube-system <gce-pd-csi-driver-pod> | tail -50

# Azure Disk CSI
kubectl get pods -n kube-system | grep disk-csi
kubectl logs -n kube-system <azuredisk-csi-controller-pod> -c csi-provisioner | tail -50
```

### Step 4 — Check ResourceQuota

```bash
kubectl describe resourcequota -n <namespace>
# Check requests.storage used vs hard limit
```

### Step 5 — List available PersistentVolumes

```bash
kubectl get pv
kubectl get pv -o custom-columns=\
NAME:.metadata.name,CAPACITY:.spec.capacity.storage,\
ACCESS:.spec.accessModes,STATUS:.status.phase,CLAIM:.spec.claimRef.name
```

### Step 6 — Check VolumeBinding mode and pod scheduling

```bash
kubectl get storageclass <storage-class-name> -o jsonpath='{.volumeBindingMode}'
# If WaitForFirstConsumer, ensure the pod requesting the PVC is also scheduled

kubectl get pod -n <namespace> | grep Pending
kubectl describe pod <pending-pod-name> -n <namespace>
```

### Step 7 — Check cloud provider volume quotas

```bash
# AWS
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-D18FCD1D  # General Purpose SSD (gp2) volume storage

# Azure
az vm list-usage --location <region> | grep "Disk"

# GCP
gcloud compute regions describe <region> --format="table(quotas.metric, quotas.usage, quotas.limit)"
```

---

## Remediation Steps

1. **For missing StorageClass** — create or apply the correct StorageClass manifest:
   ```bash
   # Example for AWS EBS gp3
   cat <<EOF | kubectl apply -f -
   apiVersion: storage.k8s.io/v1
   kind: StorageClass
   metadata:
     name: gp3
   provisioner: ebs.csi.aws.com
   parameters:
     type: gp3
   volumeBindingMode: WaitForFirstConsumer
   allowVolumeExpansion: true
   EOF
   ```

2. **For provisioner not running** — restart the CSI controller:
   ```bash
   kubectl rollout restart deployment/ebs-csi-controller -n kube-system
   kubectl get pods -n kube-system -l app=ebs-csi-controller -w
   ```

3. **For ResourceQuota exceeded** — delete unused PVCs or request a quota increase:
   ```bash
   kubectl get pvc -n <namespace>
   kubectl delete pvc <unused-pvc-name> -n <namespace>
   # Request quota increase:
   kubectl edit resourcequota <quota-name> -n <namespace>
   ```

4. **For access mode mismatch** — update the PVC to use the supported access mode, or use a StorageClass that supports RWX (e.g., NFS provisioner, AWS EFS CSI):
   ```bash
   kubectl delete pvc <pvc-name> -n <namespace>
   # Recreate with corrected accessModes
   ```

5. **For WaitForFirstConsumer with no scheduled pod** — ensure the pod that references the PVC is deployed and check its scheduling constraints (node selector, affinity).

6. **For static PV mismatch** — create a PV that matches the PVC's requirements:
   ```bash
   cat <<EOF | kubectl apply -f -
   apiVersion: v1
   kind: PersistentVolume
   metadata:
     name: pv-manual-01
   spec:
     capacity:
       storage: 10Gi
     accessModes:
       - ReadWriteOnce
     persistentVolumeReclaimPolicy: Retain
     storageClassName: manual
     hostPath:
       path: /mnt/data
   EOF
   ```

7. **For cloud quota limits** — request a quota increase through the cloud provider console or support ticket, then retry PVC provisioning.

---

## Prevention Measures

- **Define a default StorageClass** so PVCs without an explicit `storageClassName` always have a provisioner.
- **Monitor PVC Pending duration** — alert if any PVC is Pending for more than 5 minutes.
- **Set namespace-level storage quotas** proactively to prevent runaway storage consumption.
- **Test CSI driver health** as part of the cluster readiness checks.
- **Use VolumeClaimTemplates in StatefulSets** rather than manual PVC creation to ensure consistent provisioning.
- **Document and enforce access mode requirements** per application type in the internal developer portal.

---

## Escalation Criteria

- Provisioner is healthy but volumes are still not being created (possible cloud API issue or account-level suspension).
- Multiple namespaces have PVCs stuck Pending simultaneously.
- A StatefulSet with data-bearing pods cannot start due to PVC provisioning failures, risking data availability.
- Cloud quota increase request exceeds normal approval thresholds.

**On-call contact:** `#sre-oncall` Slack channel
**Incident severity:** P2 if blocking production data services, P3 otherwise
