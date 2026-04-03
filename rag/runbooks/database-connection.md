# Database Connection Pool Exhausted Runbook

**severity:** high
**category:** database
**Last Updated:** 2026-04-01
**Owner:** Platform SRE Team

---

## Overview

Connection pool exhaustion occurs when an application has consumed all available connections in its database connection pool and cannot acquire additional connections. New requests that need a database connection are queued (and eventually time out) or fail immediately with errors like `too many connections`, `connection pool timeout`, or `could not obtain connection from pool`. This is one of the most common database-related incidents in production services.

---

## Symptoms

- Application logs: `connection pool timeout`, `could not obtain connection from the pool`, `HikariPool-1 - Connection is not available, request timed out after 30000ms`
- HTTP 500 or 503 errors on all database-backed endpoints
- Datadog monitor: `postgresql.connections` near or at `max_connections`
- Prometheus: `pg_stat_database_numbackends` approaching `pg_settings_max_connections`
- Increased latency on all database queries (queuing behind connection acquisition)
- Application health endpoint returning unhealthy due to failed DB connection check
- Database server logs: `FATAL: sorry, too many clients already`

---

## Root Causes

### 1. Connection Pool Configured Too Small
The application's connection pool `maxPoolSize` is too small for the current request volume. Each thread/goroutine/request needs a connection concurrently, and the pool is undersized.

### 2. Connection Leak
Application code acquires a connection but never releases it (missing `connection.close()`, exception thrown before close, forgot to use `with` / `using` / try-with-resources pattern). Over time, all pool connections are leaked and never returned.

### 3. Slow Queries Holding Connections
Long-running queries (missing index, full table scan, N+1 query, lock contention) hold connections for extended periods, preventing other requests from acquiring them.

### 4. Deadlocks
Two or more transactions are mutually blocking each other, holding connections indefinitely until the deadlock detection timeout fires and one is rolled back.

### 5. Too Many Application Instances
A horizontal scale-out event (HPA, deployment) created many new application pods, each with its own connection pool, collectively exceeding the database's `max_connections` limit.

### 6. Missing PgBouncer / Connection Pooler
The application connects directly to PostgreSQL without a connection pooler, so each application connection maps to a PostgreSQL backend process (expensive and limited).

### 7. Database Under Maintenance or Recovering
The database is recovering from a crash, performing a long VACUUM, or under heavy I/O load, causing queries to run slowly and hold connections longer.

---

## Diagnosis Steps

### Step 1 — Check current connection count

```sql
-- PostgreSQL
SELECT count(*) AS total_connections,
       max_conn.setting AS max_connections,
       round(count(*)::numeric / max_conn.setting::numeric * 100, 1) AS pct_used
FROM pg_stat_activity
CROSS JOIN (SELECT setting FROM pg_settings WHERE name = 'max_connections') AS max_conn
GROUP BY max_conn.setting;

-- Connections by application and state
SELECT application_name, state, count(*) AS connections
FROM pg_stat_activity
WHERE datname = '<your-database>'
GROUP BY application_name, state
ORDER BY connections DESC;
```

### Step 2 — Find long-running queries

```sql
SELECT pid,
       now() - query_start AS duration,
       state,
       wait_event_type,
       wait_event,
       LEFT(query, 100) AS query_snippet,
       application_name
FROM pg_stat_activity
WHERE state != 'idle'
  AND query_start IS NOT NULL
ORDER BY duration DESC
LIMIT 20;
```

### Step 3 — Find blocking queries and deadlocks

```sql
-- Blocked queries
SELECT blocked.pid AS blocked_pid,
       blocked.query AS blocked_query,
       blocking.pid AS blocking_pid,
       blocking.query AS blocking_query
FROM pg_stat_activity AS blocked
JOIN pg_stat_activity AS blocking
  ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;

-- Lock contention
SELECT relation::regclass AS table_name,
       locktype,
       mode,
       granted,
       count(*) AS count
FROM pg_locks
GROUP BY relation, locktype, mode, granted
ORDER BY count DESC
LIMIT 20;
```

### Step 4 — Check application connection pool metrics

```bash
# HikariCP (Spring Boot) — via Actuator
curl http://<app-host>:8080/actuator/metrics/hikaricp.connections.active
curl http://<app-host>:8080/actuator/metrics/hikaricp.connections.pending
curl http://<app-host>:8080/actuator/metrics/hikaricp.connections.timeout

# Datadog query
avg:postgresql.connections{host:<db-host>} by {db}
```

### Step 5 — Check for connection leaks

```sql
-- Idle connections older than 10 minutes (likely leaked)
SELECT pid, application_name, state, query_start,
       now() - state_change AS idle_duration,
       LEFT(query, 80) AS last_query
FROM pg_stat_activity
WHERE state = 'idle'
  AND state_change < now() - INTERVAL '10 minutes'
ORDER BY idle_duration DESC;
```

### Step 6 — Check PgBouncer stats (if applicable)

```bash
# Connect to PgBouncer admin console
psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer

SHOW POOLS;
SHOW STATS;
SHOW CLIENTS;
SHOW SERVERS;
```

---

## Remediation Steps

### Immediate Relief

1. **Kill idle/leaked connections** to free pool capacity:
   ```sql
   -- Kill idle connections idle for more than 10 minutes
   SELECT pg_terminate_backend(pid)
   FROM pg_stat_activity
   WHERE datname = '<your-database>'
     AND state = 'idle'
     AND state_change < now() - INTERVAL '10 minutes'
     AND pid <> pg_backend_pid();
   ```

2. **Kill long-running blocking queries** after verifying they are safe to cancel:
   ```sql
   -- Cancel a specific query gracefully (sends SIGINT)
   SELECT pg_cancel_backend(<pid>);

   -- Terminate if cancel doesn't work (sends SIGTERM)
   SELECT pg_terminate_backend(<pid>);
   ```

3. **Temporarily increase `max_connections`** on the database (requires restart for PostgreSQL):
   ```sql
   -- This change requires a PostgreSQL restart
   ALTER SYSTEM SET max_connections = 300;
   -- Then restart PostgreSQL (or signal reload for parameters that allow it)
   ```
   *Note:* Increasing `max_connections` alone is a temporary measure. Each PostgreSQL backend consumes ~5-10MB RAM.

4. **Reduce application connection pool size** if too many app instances are connecting:
   ```bash
   # Set environment variable for HikariCP
   kubectl set env deployment/<deployment-name> -n <namespace> \
     SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE=5 \
     SPRING_DATASOURCE_HIKARI_MINIMUM_IDLE=2
   kubectl rollout restart deployment/<deployment-name> -n <namespace>
   ```

5. **Deploy or scale PgBouncer** to multiplex application connections:
   ```bash
   # Scale PgBouncer if already deployed
   kubectl scale deployment pgbouncer -n <namespace> --replicas=3

   # PgBouncer key settings (pgbouncer.ini)
   # pool_mode = transaction   (best connection utilisation)
   # max_client_conn = 1000    (clients can connect freely)
   # default_pool_size = 20    (only 20 backend connections to PostgreSQL)
   ```

6. **Add missing database indexes** if slow queries are the root cause:
   ```sql
   -- Find missing indexes via pg_stat_user_tables
   SELECT relname AS table_name,
          seq_scan,
          idx_scan,
          n_live_tup
   FROM pg_stat_user_tables
   WHERE seq_scan > idx_scan
     AND n_live_tup > 10000
   ORDER BY seq_scan DESC
   LIMIT 10;

   -- Create index concurrently (does not lock table)
   CREATE INDEX CONCURRENTLY idx_<table>_<column> ON <table>(<column>);
   ```

7. **Fix connection leaks in application code** — review and ensure all database connections are closed in `finally` blocks or use resource management patterns (`WITH` in Python, `try-with-resources` in Java, `defer` in Go).

8. **Configure idle connection timeout** in PgBouncer or application pool to automatically reclaim stale connections:
   ```ini
   # PgBouncer — pgbouncer.ini
   server_idle_timeout = 300    ; Close server connections idle >5min
   client_idle_timeout = 60     ; Close client connections idle >1min
   ```

---

## Prevention Measures

- **Deploy PgBouncer in transaction mode** for all services that use PostgreSQL — this is the single most impactful change.
- **Set connection pool `connectionTimeout`** to 3-5 seconds so requests fail fast instead of queuing indefinitely.
- **Set `idleTimeout`** and `maxLifetime` in HikariCP to reclaim stale connections:
  ```properties
  spring.datasource.hikari.connection-timeout=5000
  spring.datasource.hikari.idle-timeout=300000
  spring.datasource.hikari.max-lifetime=1800000
  spring.datasource.hikari.maximum-pool-size=10
  ```
- **Alert on connection pool pending > 0** (requests waiting for a connection) — this is an early warning signal before exhaustion occurs.
- **Add database index analysis** to the CI pipeline to catch missing indexes before production deployment.
- **Enable `log_min_duration_statement = 1000`** in PostgreSQL to log slow queries automatically.
- **Use read replicas** for read-heavy workloads to spread connection load.
- **Set `statement_timeout`** in PostgreSQL to prevent runaway queries from holding connections indefinitely:
  ```sql
  ALTER ROLE <app-user> SET statement_timeout = '30s';
  ```

---

## Escalation Criteria

- Connection pool exhaustion persists despite immediate relief measures.
- Database server is OOMKilling or crashing due to too many backends.
- Deadlocks are causing widespread transaction rollbacks affecting business operations.
- The issue is caused by a corrupt index or table requiring a database DBA to intervene.
- Data loss or data corruption is suspected.
- An emergency `max_connections` increase requires database restart and coordinated downtime.

**On-call contact:** `#sre-oncall` and `#db-oncall` Slack channels
**Incident severity:** P1 if all database-backed services are failing, P2 for partial impact
