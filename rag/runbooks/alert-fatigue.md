# Alert Fatigue Remediation Runbook

**severity:** low
**category:** observability
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

Alert fatigue occurs when on-call engineers are overwhelmed by a high volume of alerts — many of which are noisy, transient, redundant, or low-value. The result is desensitisation: engineers start ignoring or silencing alerts wholesale, meaning real incidents are missed. This runbook provides a structured approach to auditing, deduplicating, suppressing, and tuning alert rules to restore signal-to-noise ratio.

Alert fatigue remediation is a continuous improvement process, not a one-time fix. Treat your alerting system the same way you treat code: review, refactor, and iterate.

---

## Symptoms of Alert Fatigue

- On-call engineers acknowledge alerts without investigating them
- Alert volume > 50 pages per week per engineer
- More than 30% of alerts are resolved as "noise" or "not actionable"
- Alerts are being auto-acknowledged or silenced without human review
- Engineers report waking up multiple times per night to non-critical alerts
- Slack `#alerts` channel has a high message volume with low engagement
- Incident retrospectives repeatedly cite "too many alerts" as a contributing factor

---

## Root Causes

### 1. Threshold Set Too Aggressively
Alert thresholds are set at levels that produce false positives under normal operating conditions (e.g., alerting at 80% CPU for a service that routinely runs at 75%).

### 2. No Alerting Tiers / Severity Levels
All alerts page the on-call engineer regardless of actual business impact. Low-severity issues should go to a ticket, not a page.

### 3. Missing Alert Deduplication
The same underlying issue produces multiple alerts from different monitoring systems (Prometheus, Datadog, PagerDuty rules) that are not correlated or deduplicated.

### 4. No Maintenance Window Suppression
Alerts fire during planned maintenance, deployments, or known transient spikes, adding noise with no value.

### 5. Stale Alert Rules
Alert rules created years ago for services that no longer exist, or for conditions that are no longer relevant, continue to fire.

### 6. Symptom-Based vs. Cause-Based Alerting
Too many alerts on low-level technical indicators (CPU %, memory %) when what matters is user-visible symptoms (latency, error rate, availability).

---

## Deduplication Strategies

### Prometheus Alertmanager — Group and Deduplicate

Configure `group_by`, `group_wait`, `group_interval`, and `repeat_interval` to batch related alerts:

```yaml
# alertmanager.yml
route:
  group_by: ['alertname', 'namespace', 'severity']
  group_wait: 30s          # Wait 30s to collect related alerts before firing
  group_interval: 5m       # Wait 5m before resending a group with new alerts
  repeat_interval: 4h      # Re-notify after 4h if still firing (was: 1h)
  receiver: 'ops-pagerduty'

  routes:
  # Route low-severity to Slack only (no page)
  - match:
      severity: low
    receiver: 'slack-low-severity'
    repeat_interval: 24h

  # Route medium to Slack + ticket only
  - match:
      severity: medium
    receiver: 'slack-medium-jira'
    repeat_interval: 8h

  # Route critical directly to PagerDuty
  - match:
      severity: critical
    receiver: 'pagerduty-critical'
    group_wait: 10s
    repeat_interval: 30m
```

### Alertmanager Inhibition Rules

Suppress child alerts when a parent alert is already firing:

```yaml
inhibit_rules:
# If a node is NotReady, suppress all pod-level alerts on that node
- source_match:
    alertname: 'KubeNodeNotReady'
  target_match_re:
    alertname: 'KubePod.*'
  equal: ['node']

# If namespace has quota exceeded, suppress resource alerts from that namespace
- source_match:
    alertname: 'KubeQuotaExceeded'
  target_match_re:
    alertname: 'Kube.*'
  equal: ['namespace']

# If the service is down, suppress latency alerts for the same service
- source_match:
    alertname: 'ServiceDown'
  target_match_re:
    alertname: 'HighLatency|ErrorRateHigh'
  equal: ['service', 'namespace']
```

---

## Suppression Windows

### Deploying a Silence in Alertmanager

Silence alerts during planned deployments:

```bash
# Using amtool CLI
amtool silence add \
  --alertmanager.url=http://alertmanager:9093 \
  --comment="Planned deployment of payment-service v2.3.0" \
  --duration=45m \
  'namespace="payments"' 'alertname=~"Deployment.*|Pod.*"'

# List active silences
amtool silence query --alertmanager.url=http://alertmanager:9093

# Expire a silence
amtool silence expire --alertmanager.url=http://alertmanager:9093 <silence-id>
```

### Automated Silence During CI/CD Deployments

Add to your deployment pipeline:

```bash
#!/bin/bash
# pre-deploy.sh — silence alerts for the service being deployed
NAMESPACE="${1}"
DEPLOYMENT="${2}"
DURATION="${3:-30m}"

SILENCE_ID=$(curl -s -X POST http://alertmanager:9093/api/v2/silences \
  -H "Content-Type: application/json" \
  -d "{
    \"matchers\": [
      {\"name\": \"namespace\", \"value\": \"${NAMESPACE}\", \"isRegex\": false},
      {\"name\": \"deployment\", \"value\": \"${DEPLOYMENT}\", \"isRegex\": false}
    ],
    \"startsAt\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
    \"endsAt\": \"$(date -u -d \"+${DURATION}\" +%Y-%m-%dT%H:%M:%SZ)\",
    \"comment\": \"Auto-silence during deployment of ${DEPLOYMENT}\"
  }" | python3 -c "import sys,json; print(json.load(sys.stdin)['silenceID'])")

echo "Created silence: ${SILENCE_ID}"
echo "${SILENCE_ID}" > /tmp/silence-id.txt
```

---

## Tuning Alert Thresholds

### SRE Golden Signals Approach

Alert only on what users care about — the Four Golden Signals:

1. **Latency** — Are requests slow?
2. **Traffic** — Has demand changed unexpectedly?
3. **Errors** — Are requests failing?
4. **Saturation** — Is the system near capacity?

Example — prefer symptom-based alerts over cause-based:

```yaml
# GOOD: Alert on user-visible error rate
- alert: HighErrorRate
  expr: |
    sum(rate(http_requests_total{status=~"5.."}[5m])) by (service)
    /
    sum(rate(http_requests_total[5m])) by (service)
    > 0.01
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "{{ $labels.service }} error rate above 1%"

# BAD: Alert on CPU (usually not user-visible)
# - alert: HighCPU
#   expr: container_cpu_usage_seconds_total > 0.8
#   for: 2m
```

### Raising Thresholds and Adding Durations

```yaml
# Before (noisy): fires immediately on any 90% memory
- alert: PodMemoryHigh
  expr: container_memory_working_set_bytes / container_spec_memory_limit_bytes > 0.9
  for: 0m
  labels:
    severity: warning

# After (tuned): sustained 90% for 15 minutes
- alert: PodMemoryHigh
  expr: container_memory_working_set_bytes / container_spec_memory_limit_bytes > 0.90
  for: 15m
  labels:
    severity: warning
```

---

## Alert Audit Process

Run a monthly alert audit:

1. **Export alert firing history** from Alertmanager/PagerDuty for the past 30 days.
2. **Categorise each alert rule**: actionable, informational, noise, stale.
3. **Remove or disable stale rules** for services no longer running.
4. **Increase `for` duration** on rules that fire transiently but self-resolve.
5. **Demote severity** of alerts that are consistently resolved without action.
6. **Merge related alerts** using a single multi-condition rule instead of separate rules.
7. **Document the owner** of each alert rule — unowned rules are candidates for deletion.

---

## Prevention Measures

- **Adopt the "alert on symptoms, not causes" philosophy** for all new alert rules.
- **Require a runbook link** (`annotations.runbook_url`) on every alert created.
- **Review alert rules in pull requests** — treat alert definitions as code.
- **Set a policy**: no new `severity: critical` alerts without a corresponding runbook.
- **Track alert noise metrics** in a Grafana dashboard: alerts per week, pages per on-call shift, false positive rate.
- **Hold monthly alert review meetings** to audit and retire stale rules.
- **Implement error budget burn rate alerts** (SLO-based) as a replacement for raw metric threshold alerts.

---

## Escalation Criteria

Alert fatigue is not normally an escalation scenario, but escalate to engineering leadership if:

- On-call attrition is occurring due to alert volume.
- Engineers have started disabling or muting entire alert categories.
- A real P1 incident was missed because the alert was buried in noise.
- The alerting infrastructure itself (Alertmanager, PagerDuty) is overloaded.

**Owner:** SRE Platform Team
**Review cadence:** Monthly
**Incident severity:** Not applicable — this is a process improvement runbook
