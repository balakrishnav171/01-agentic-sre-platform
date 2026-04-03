# etcd Backup and Recovery Runbook

**severity:** critical
**category:** infrastructure
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

etcd is the consistent distributed key-value store that Kubernetes uses to persist all cluster state (Deployments, Services, ConfigMaps, Secrets, RBAC policies, etc.). Loss of etcd data is catastrophic — the entire cluster state is lost. This runbook covers:

1. **Scheduled backup procedure** — how to take a consistent etcd snapshot.
2. **Restore procedure** — how to recover from a backup after data loss or corruption.

Backups must be taken regularly (at minimum daily) and stored in a durable location (S3, GCS, Azure Blob) separate from the cluster.

---

## Symptoms Indicating etcd Issues

- `kubectl get <any-resource>` returns `etcdserver: request timed out`
- API server is responding slowly or returning 503 errors
- Prometheus alert: `EtcdNoLeader`, `EtcdInsufficientMembers`, `EtcdHighFsyncDuration`
- Control plane components (scheduler, controller-manager) report connection errors to etcd
- etcd pod logs show `FAILED to send out heartbeat on time`, `rafthttp: failed to read`, or `mvcc: database space exceeded`

---

## Prerequisites

- `etcdctl` v3 installed (same version as the cluster's etcd)
- Access to the etcd endpoint (usually `https://127.0.0.1:2379` on control plane nodes)
- etcd TLS certificates accessible (typically under `/etc/kubernetes/pki/etcd/`)
- Destination storage accessible (S3 bucket, NFS mount, or local disk with off-site replication)

---

## Part 1: Backup Procedure

### Step 1 — Set etcdctl environment variables

```bash
# On the control plane node
export ETCDCTL_API=3
export ETCDCTL_ENDPOINTS=https://127.0.0.1:2379
export ETCDCTL_CACERT=/etc/kubernetes/pki/etcd/ca.crt
export ETCDCTL_CERT=/etc/kubernetes/pki/etcd/healthcheck-client.crt
export ETCDCTL_KEY=/etc/kubernetes/pki/etcd/healthcheck-client.key
```

### Step 2 — Verify etcd cluster health

```bash
etcdctl endpoint health
etcdctl endpoint status --write-out=table
etcdctl member list --write-out=table
```

Expected output: all members show `isLeader`, `isLearner`, and a consistent `raftTerm`.

### Step 3 — Take a snapshot backup

```bash
BACKUP_DIR="/opt/etcd-backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SNAPSHOT_FILE="${BACKUP_DIR}/etcd-snapshot-${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

etcdctl snapshot save "${SNAPSHOT_FILE}"

echo "Snapshot saved to: ${SNAPSHOT_FILE}"
ls -lh "${SNAPSHOT_FILE}"
```

### Step 4 — Verify the snapshot

```bash
etcdctl snapshot status "${SNAPSHOT_FILE}" --write-out=table
# Output should show: hash, revision, totalKey, totalSize
```

### Step 5 — Upload to durable storage

```bash
# AWS S3
aws s3 cp "${SNAPSHOT_FILE}" \
  "s3://<your-backup-bucket>/etcd-backups/$(basename ${SNAPSHOT_FILE})" \
  --sse aws:kms

# GCS
gsutil cp "${SNAPSHOT_FILE}" \
  "gs://<your-backup-bucket>/etcd-backups/$(basename ${SNAPSHOT_FILE})"

# Azure Blob Storage
az storage blob upload \
  --account-name <storage-account> \
  --container-name etcd-backups \
  --name "$(basename ${SNAPSHOT_FILE})" \
  --file "${SNAPSHOT_FILE}"
```

### Step 6 — Clean up old local snapshots (keep last 7)

```bash
ls -t "${BACKUP_DIR}"/etcd-snapshot-*.db | tail -n +8 | xargs -r rm -v
```

### Automated Backup CronJob

Deploy this as a Kubernetes CronJob on the control plane node:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: etcd-backup
  namespace: kube-system
spec:
  schedule: "0 2 * * *"  # Daily at 02:00 UTC
  jobTemplate:
    spec:
      template:
        spec:
          hostNetwork: true
          hostPID: true
          tolerations:
          - key: "node-role.kubernetes.io/control-plane"
            operator: "Exists"
            effect: "NoSchedule"
          nodeSelector:
            node-role.kubernetes.io/control-plane: ""
          containers:
          - name: etcd-backup
            image: bitnami/etcd:3.5
            command:
            - /bin/sh
            - -c
            - |
              export ETCDCTL_API=3
              TIMESTAMP=$(date +%Y%m%d_%H%M%S)
              etcdctl --endpoints=https://127.0.0.1:2379 \
                --cacert=/etc/kubernetes/pki/etcd/ca.crt \
                --cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt \
                --key=/etc/kubernetes/pki/etcd/healthcheck-client.key \
                snapshot save /backup/etcd-snapshot-${TIMESTAMP}.db &&
              aws s3 cp /backup/etcd-snapshot-${TIMESTAMP}.db \
                s3://<backup-bucket>/etcd-backups/etcd-snapshot-${TIMESTAMP}.db
            volumeMounts:
            - name: etcd-certs
              mountPath: /etc/kubernetes/pki/etcd
              readOnly: true
            - name: backup-storage
              mountPath: /backup
          volumes:
          - name: etcd-certs
            hostPath:
              path: /etc/kubernetes/pki/etcd
          - name: backup-storage
            emptyDir: {}
          restartPolicy: OnFailure
```

---

## Part 2: Restore Procedure

**WARNING:** etcd restore is a destructive operation. All cluster state after the backup timestamp will be lost. Coordinate with all stakeholders before proceeding. This procedure should only be executed after a complete etcd data loss or irreparable corruption.

### Step 1 — Stop the API server and etcd

On **all** control plane nodes:
```bash
# Move static pod manifests to disable control plane components
sudo mv /etc/kubernetes/manifests/kube-apiserver.yaml /tmp/
sudo mv /etc/kubernetes/manifests/etcd.yaml /tmp/

# Verify pods are gone
sudo crictl ps | grep -E "etcd|apiserver"
# Should show no running containers after ~30 seconds
```

### Step 2 — Download the backup from durable storage

```bash
RESTORE_SNAPSHOT="/opt/etcd-restore/etcd-snapshot-latest.db"
mkdir -p /opt/etcd-restore

# AWS S3
aws s3 cp "s3://<backup-bucket>/etcd-backups/etcd-snapshot-<TIMESTAMP>.db" \
  "${RESTORE_SNAPSHOT}"

# Verify snapshot integrity
ETCDCTL_API=3 etcdctl snapshot status "${RESTORE_SNAPSHOT}" --write-out=table
```

### Step 3 — Restore the snapshot on the first control plane node

```bash
export ETCDCTL_API=3

# Back up current etcd data directory
sudo mv /var/lib/etcd /var/lib/etcd.bak.$(date +%Y%m%d_%H%M%S)

# Restore from snapshot
sudo etcdctl snapshot restore "${RESTORE_SNAPSHOT}" \
  --name <node-name> \
  --initial-cluster "<node-name>=https://<node-ip>:2380" \
  --initial-cluster-token etcd-cluster-restore \
  --initial-advertise-peer-urls "https://<node-ip>:2380" \
  --data-dir /var/lib/etcd

sudo chown -R etcd:etcd /var/lib/etcd
```

### Step 4 — (Multi-node HA) Restore on all other control plane nodes

For each additional control plane node, repeat Step 3 with the respective node's name and IP in `--initial-cluster`.

```bash
# Example for 3-node cluster
sudo etcdctl snapshot restore "${RESTORE_SNAPSHOT}" \
  --name <node-2-name> \
  --initial-cluster "<node-1>=https://<ip-1>:2380,<node-2>=https://<ip-2>:2380,<node-3>=https://<ip-3>:2380" \
  --initial-cluster-token etcd-cluster-restore \
  --initial-advertise-peer-urls "https://<node-2-ip>:2380" \
  --data-dir /var/lib/etcd
```

### Step 5 — Restore static pod manifests

On **all** control plane nodes:
```bash
sudo mv /tmp/etcd.yaml /etc/kubernetes/manifests/
sudo mv /tmp/kube-apiserver.yaml /etc/kubernetes/manifests/

# Watch etcd and API server come up
sudo crictl ps -w | grep -E "etcd|apiserver"
```

### Step 6 — Verify cluster recovery

```bash
# Wait for API server to be healthy
kubectl get nodes
kubectl get pods --all-namespaces | head -20

# Verify etcd cluster health
export ETCDCTL_API=3
export ETCDCTL_ENDPOINTS=https://127.0.0.1:2379
export ETCDCTL_CACERT=/etc/kubernetes/pki/etcd/ca.crt
export ETCDCTL_CERT=/etc/kubernetes/pki/etcd/healthcheck-client.crt
export ETCDCTL_KEY=/etc/kubernetes/pki/etcd/healthcheck-client.key

etcdctl endpoint health
etcdctl endpoint status --write-out=table
etcdctl member list --write-out=table
```

---

## Prevention Measures

- **Schedule daily automated backups** and verify they upload successfully to off-site storage.
- **Test restores quarterly** in a non-production cluster to validate the procedure works.
- **Monitor etcd disk usage** — alert at 70% of the etcd data directory filesystem (default quota 8 GB).
- **Compact and defrag etcd regularly** to prevent database space exceeded:
  ```bash
  ETCDCTL_API=3 etcdctl compact $(etcdctl endpoint status --write-out=json | \
    python3 -c "import sys,json; print(max(m['Status']['header']['revision'] for m in json.load(sys.stdin)))")
  ETCDCTL_API=3 etcdctl defrag --cluster
  ```
- **Use managed etcd** (AWS EKS, GKE, AKS) to offload backup and recovery management.
- **Enable Prometheus alerts**: `EtcdNoLeader`, `EtcdInsufficientMembers`, `EtcdHighFsyncDuration > 0.5s`.

---

## Escalation Criteria

- etcd cluster has lost quorum (majority of members unavailable in a 3-node cluster).
- Restore procedure fails due to snapshot corruption.
- API server cannot reconnect to etcd after restore.
- Data loss window exceeds the acceptable RPO (Recovery Point Objective).
- The incident requires vendor (cloud provider) support escalation.

**On-call contact:** `#sre-oncall` and `#platform-engineering` Slack channels
**Incident severity:** P0 — etcd failure is a complete cluster outage
