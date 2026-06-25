# SOP: RbacRelationsApiReplicationFailure

## 1. Alert Description

**Alert name:** `RbacRelationsApiReplicationFailure`

**What it means:** The rate of replication events produced by RBAC's application layer exceeds the rate of events successfully consumed and written to Kessel/SpiceDB. In plain terms, the pipeline that keeps the authorization database (Kessel/SpiceDB) in sync with RBAC's PostgreSQL database has fallen behind or stopped processing.

**Alert rule (PromQL):**

```promql
min_over_time((sum(rate(relations_replication_event_total[10m])) - sum(rate(rbac_kafka_consumer_messages_processed_total{message_type="relations",status="success"}[10m])))[1m:15s]) > 0.02
```

The alert fires when the sustained difference between produced and consumed event rates exceeds 0.02 events/second over a 10-minute window. In practical terms, 0.02 events/second is roughly 1.2 events/minute or 72 events/hour -- a small but sustained gap indicating the consumer is consistently falling behind rather than catching up.

**Alert definition location:** app-interface (gitlab.cee.redhat.com MR 166360). The alert is NOT defined in this repository.

### Pipeline overview

The replication pipeline has five stages:

```
Application Write       Outbox Write         Debezium CDC          Kafka Consumer       Kessel gRPC Write
(Service layer)    -->  (PostgreSQL)    -->  (WAL -> Kafka)   -->  (Consumer pod)   -->  (SpiceDB)
                        management_outbox    Kafka topic:           rbac-kafka-consumer   DeleteTuples +
                        insert + delete      outbox.event.          pod (single replica)  CreateTuples
                                             relations-                                   with FencingCheck
                                             replication-event
```

1. **Application Write** -- Service layer builds a `ReplicationEvent` and calls `replicator.replicate(event)`.
2. **Outbox Write** -- `OutboxReplicator` inserts then immediately deletes a row in `management_outbox`. Debezium captures both operations from PostgreSQL WAL; the outbox event router is configured to process the INSERT and ignore the DELETE. Prometheus counter: `relations_replication_event_total`.
3. **Debezium CDC** -- External Debezium connector (configured in app-interface, not this repo) captures WAL changes and publishes to Kafka topic `outbox.event.relations-replication-event`.
4. **Kafka Consumer** -- Single-replica `rbac-kafka-consumer` pod reads messages, validates structure, converts JSON to protobuf. Uses fencing tokens for distributed locking, exponential backoff (10 attempts, 5x multiplier, base delay 300ms, max 30s), and manual offset commits every 10 messages.
5. **Kessel gRPC Write** -- `RelationsApiReplicator` calls `DeleteTuples` then `CreateTuples` with a `FencingCheck` to prevent stale writes after partition reassignment.

### User-facing impact

- **V2 APIs:** Authorization decisions may become stale. Users might retain access they should have lost, or lose access they should have gained. The delay is bounded by how long replication is behind.
- **V1 APIs:** Unaffected. V1 reads directly from PostgreSQL and does not depend on Kessel/SpiceDB.


## 2. Severity and Impact

| Impact area | Effect |
|---|---|
| V2 authorization decisions | Stale -- permissions granted/revoked after the lag point are not yet reflected in Kessel |
| V2 role/group/workspace changes | Changes are persisted in PostgreSQL but not yet visible to the authorization engine |
| V1 API consumers | No impact -- V1 reads from PostgreSQL directly |
| Data consistency | Risk of drift between RBAC DB and SpiceDB increases with lag duration |
| Self-healing | Some failure modes (transient network errors, Kessel restarts) resolve automatically via retries. Others (malformed messages, expired fencing tokens) require manual intervention |

**Severity classification:** High. While V1 APIs continue to function, all V2 authorization decisions rely on Kessel being up-to-date. Extended outages can lead to incorrect access control decisions.


## 3. Diagnostic Steps

Work through these steps in order, from most common to least common causes.

### Discovering placeholder values

Commands in this SOP use placeholders like `<namespace>`, `<consumer-pod-name>`, and `<consumer-group-id>`. To discover them:

```bash
# Namespace: check which namespace the RBAC pods are deployed in
oc get pods --all-namespaces | grep rbac

# Consumer pod name: use the pod label to find the exact name
oc get pods -l pod=rbac-kafka-consumer -n <namespace> -o name

# Consumer group ID: read the environment variable from the consumer pod
oc exec <consumer-pod-name> -n <namespace> -- env | grep RBAC_KAFKA_CONSUMER_GROUP_ID

# RBAC host: check the route or ingress
oc get routes -n <namespace> | grep rbac
```

### 3.1 Check consumer pod status

```bash
# Get consumer pod status
oc get pods -l pod=rbac-kafka-consumer -n <namespace>

# Check for crash loops or restarts
oc get pods -l pod=rbac-kafka-consumer -n <namespace> -o wide

# Check pod events for OOMKilled, CrashLoopBackOff, etc.
oc describe pod <consumer-pod-name> -n <namespace>
```

Look for:
- `CrashLoopBackOff` -- consumer is crashing and restarting repeatedly
- `Running` with high restart count -- consumer crashes but recovers
- `0/1 Ready` -- health check is failing (readiness probe at `/tmp/kubernetes-readiness`)
- Pod not present -- deployment may be scaled to 0

### 3.2 Check consumer logs for error patterns

```bash
# Recent consumer logs
oc logs <consumer-pod-name> -n <namespace> --tail=200

# Search for specific error patterns
oc logs <consumer-pod-name> -n <namespace> --tail=1000 | grep -E "ERROR|CRITICAL|Max operation retries|FAILED_PRECONDITION|ValidationError|Lock token not available"
```

Key log patterns to look for:

| Log pattern | Meaning |
|---|---|
| `Max operation retries (10) exceeded` | Consumer exhausted all retries on a message and stopped. Offset NOT committed -- message will be retried on restart. |
| `FAILED_PRECONDITION` | Fencing token was invalidated -- partition was reassigned while processing. Consumer stops to prevent stale writes. |
| `ValidationError is non-retryable` | Malformed message that cannot be fixed by retrying. Consumer stops. |
| `Lock token not available` | Could not acquire fencing token from Kessel Relations API. Consumer cannot process messages safely. |
| `Failed to acquire lock token` | Kessel Relations API is unreachable during partition assignment. |
| `gRPC error processing relations message` | Kessel write failed (check status code for details). |
| `Failed to parse JSON from message` | Raw Kafka message is not valid JSON. Non-retryable. |

### 3.3 Check Grafana dashboard

Dashboard: **insights-rbac-operations** (RBAC Consumer section)

Key panels to check:

| Panel | What to look for |
|---|---|
| **RBAC Consumer -> Relations: Replication event creation rate minus total consumer successful "relations" processed** | Positive values indicate lag. Sustained positive values trigger the alert. |
| **RBAC Consumer-> Relations Excessive lag events** | Shows when the lag threshold (>0.02 rate difference) is breached. |
| **Consumer Status** | `rbac_kafka_consumer_info` should be 1 (running). 0 means consumer is stopped. |
| **Consumer Uptime** | `rbac_kafka_consumer_start_time_seconds` -- check if the consumer recently restarted. |
| **Messages by Status (1h)** | `rbac_kafka_consumer_messages_processed_total` broken down by status. Look for `max_retries_exceeded`, `validation_failed`, `grpc_error`, `fencing_failed`. |
| **Kessel Write Latency P95/P99** | `rbac_kessel_write_duration_seconds` -- high latency indicates Kessel is slow or overloaded. |
| **Retry Rate by Reason** | `rbac_kafka_consumer_retry_attempts_total` -- frequent retries suggest a transient issue. |
| **Validation Errors by Type** | `rbac_kafka_consumer_validation_errors_total` -- non-zero indicates bad messages in the topic. |
| **Lock Acquisition Rate** | `rbac_kafka_consumer_lock_acquisition_total` -- failures here prevent message processing. |
| **RBAC Replication Event Count Over 5 Minutes** | `relations_replication_event_total` -- if this is zero, no events are being produced (application side may be the issue). |
| **Average Replication Latency Over 5 Minutes** | `rbac_replication_event_latency_seconds` -- measures end-to-end time from event creation to successful processing. |

### 3.4 Check Kessel Relations API health

```bash
# Check Kessel Relations pods
oc get pods -l app=kessel-relations -n <namespace>

# Check Kessel Relations logs for errors
oc logs -l app=kessel-relations -n <namespace> --tail=100
```

If Kessel is down, the consumer will retry writes with exponential backoff and eventually stop after 10 attempts.

### 3.5 Check Kafka topic lag

```bash
# Check consumer group lag
oc exec <kafka-pod> -- bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --describe \
  --group <consumer-group-id>
```

The consumer group ID is configured via `RBAC_KAFKA_CONSUMER_GROUP_ID` environment variable on the consumer pod.

Look for:
- High `LAG` value on the `outbox.event.relations-replication-event` topic
- `CONSUMER-ID` column empty (consumer is not connected)

### 3.6 Check Debezium connector status

Debezium is managed externally via app-interface. Check the Debezium connector status in the Kafka Connect cluster.

```bash
# Check connector status (exact command depends on your Kafka Connect setup)
oc exec <kafka-connect-pod> -- curl -s localhost:8083/connectors/<connector-name>/status | jq .
```

If the Debezium connector is down:
- The `relations_replication_event_total` counter will increment (application is writing to the outbox)
- But `rbac_kafka_consumer_messages_processed_total` will not change (no messages reaching Kafka)
- The Kafka topic lag may show zero because no new messages are being produced to the topic


## 4. Common Root Causes and Remediation

| Symptom | Root cause | Action |
|---|---|---|
| Consumer pod in `CrashLoopBackOff`, logs show `Max operation retries (10) exceeded` | A message failed processing 10 times (Kessel unavailable, DB error, etc.) | Fix the underlying issue (Kessel, DB), then the pod will restart automatically and replay the message. |
| Consumer pod in `CrashLoopBackOff`, logs show `ValidationError is non-retryable` | Malformed message in Kafka topic. Consumer cannot parse it and stops. | Skip the bad message offset (see Section 5.2), then restart the consumer. |
| Consumer pod in `CrashLoopBackOff`, logs show `Failed to parse JSON from message` | Raw Kafka message is corrupted or not valid JSON. | Skip the bad message offset (see Section 5.2), then restart the consumer. |
| Consumer running but no messages being processed | Debezium connector is down. Events are written to the outbox table but never reach Kafka. | Check Debezium connector status. Restart the connector via app-interface. Escalate to platform team. |
| Consumer logs show `FAILED_PRECONDITION` errors | Fencing token was invalidated, typically due to partition reassignment or Kessel restart. | Consumer will stop automatically. Kubernetes restart will acquire a new lock token. If persistent, check for multiple consumer replicas or Kessel issues. |
| Consumer logs show `Failed to acquire lock token` | Kessel Relations API is unavailable during consumer startup/rebalance. | Check Kessel Relations API health. Consumer will retry lock acquisition up to 5 times with exponential backoff. If Kessel is down, fix Kessel first. |
| High Kessel write latency (P95 > 5s) | Kessel/SpiceDB is overloaded or degraded. | Check Kessel metrics and pod health. Escalate to Kessel team if persistent. |
| Consumer logs show gRPC `UNAVAILABLE` errors | Network connectivity issue between consumer pod and Kessel. | Check network policies, service mesh, and Kessel pod status. |
| `relations_replication_event_total` is zero but changes are being made | Application is not writing to the outbox. Feature flag `REPLICATION_TO_RELATION_ENABLED` may be false. | Check the `REPLICATION_TO_RELATION_ENABLED` environment variable on the RBAC service pods. |
| Consumer running, Kessel healthy, but authorization decisions are stale for specific tenants | Consistency tokens not being saved. Check for `Tenant not found for org_id` warnings in consumer logs. | Re-bootstrap the affected tenant (see Section 5.3). |


## 5. Remediation Actions

### 5.1 Restart consumer pod

The consumer pod is designed to be safely restarted. On restart, it replays from the last committed offset (commits every 10 messages). If it stopped due to max retries exceeded, the offset was NOT committed, so the failing message will be retried.

```bash
# Delete the pod to trigger a restart
oc delete pod <consumer-pod-name> -n <namespace>

# Verify it comes back up
oc get pods -l pod=rbac-kafka-consumer -n <namespace> -w
```

### 5.2 Skip a bad message offset

If the consumer is stuck on a malformed message (ValidationError, JSON parse error), you need to manually advance the consumer group's committed offset past the bad message.

**WARNING:** Skipping a message means the relations in that message will NOT be replicated to Kessel. You may need to run parity checks or DR reconciliation afterward.

1. Identify the stuck offset from consumer logs:
   ```
   ValidationError is non-retryable for message at partition 0, offset 12345
   ```

2. Scale down the consumer to 0 replicas:
   ```bash
   # Scale down via ClowdApp or deployment
   oc scale deployment rbac-kafka-consumer -n <namespace> --replicas=0
   ```

3. Reset the consumer group offset to skip past the bad message. If the stuck offset is 12345, use 12346 (offset + 1):
   ```bash
   oc exec <kafka-pod> -- bin/kafka-consumer-groups.sh \
     --bootstrap-server localhost:9092 \
     --group <consumer-group-id> \
     --topic outbox.event.relations-replication-event \
     --reset-offsets \
     --to-offset 12346 \
     --execute
   ```

4. Scale the consumer back up:
   ```bash
   oc scale deployment rbac-kafka-consumer -n <namespace> --replicas=1
   ```

5. Run parity checks to detect and fix any drift (see Section 5.4).

### 5.3 Re-bootstrap affected tenants

If a specific tenant's authorization data is out of sync, re-bootstrap it:

```bash
curl -X POST "https://<rbac-host>/_private/api/utils/bootstrap_tenant/" \
  -H "Content-Type: application/json" \
  -d '{"org_ids": ["<org_id>"]}'
```

To force re-bootstrap even if the tenant is already bootstrapped:

**WARNING:** The `force=true` parameter is only allowed when `REPLICATION_TO_RELATION_ENABLED` is `false`. Using it while replication is enabled will return an error. If you need to re-bootstrap during normal operation, use `force_admin_only=true` instead (see below).

```bash
curl -X POST "https://<rbac-host>/_private/api/utils/bootstrap_tenant/?force=true" \
  -H "Content-Type: application/json" \
  -d '{"org_ids": ["<org_id>"]}'
```

To re-replicate only admin default bindings (safe even when replication is on):

```bash
curl -X POST "https://<rbac-host>/_private/api/utils/bootstrap_tenant/?force_admin_only=true" \
  -H "Content-Type: application/json" \
  -d '{"org_ids": ["<org_id>"]}'
```

### 5.4 Run parity checks

Parity checks compare RBAC PostgreSQL state against Kessel/SpiceDB to detect drift. This is a Celery task gated by `PARITY_CHECK_ENABLED` and configured with `PARITY_CHECK_ORG_IDS`.

The parity check verifies:
- Workspace parent relations
- Custom role permissions
- Seeded role hierarchy
- Bootstrap completeness (root workspace, default workspace, tenant mapping)
- Group-principal relations

To trigger parity checks for specific tenants:

1. Set the required environment variables on the RBAC worker pods:
   ```
   PARITY_CHECK_ENABLED=True
   PARITY_CHECK_ORG_IDS=org_id_1,org_id_2,org_id_3
   ```

2. The Celery task name is `management.tasks.run_kessel_parity_checks_in_worker`. It runs on the Celery beat schedule if configured, or can be invoked directly:
   ```bash
   # From the worker pod
   oc exec <worker-pod> -- python /opt/rbac/rbac/manage.py shell -c \
     "from management.tasks import run_kessel_parity_checks_in_worker; run_kessel_parity_checks_in_worker.delay()"
   ```

3. Monitor the worker logs for results:
   ```bash
   oc logs -l pod=rbac-worker-service -n <namespace> --tail=200 | grep -i "parity"
   ```

### 5.5 Rebuild workspace relations

If workspace parent relations are missing in Kessel for a specific tenant:

```bash
curl -X POST "https://<rbac-host>/_private/api/utils/rebuild_tenant_workspace_relations/<org_id>/"
```

Use `?dry_run=true` first to see what would be changed.

### 5.6 Recompute role bindings

If role bindings for a tenant are in an inconsistent state:

```bash
curl -X POST "https://<rbac-host>/_private/api/utils/recompute_tenant_role_bindings/<org_id>/"
```

### 5.7 Read tuples for debugging

To inspect what tuples currently exist in Kessel for debugging:

```bash
curl -X POST "https://<rbac-host>/_private/api/relations/read_tuples/" \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {
      "resource_namespace": "rbac",
      "resource_type": "workspace",
      "resource_id": "<workspace-uuid>",
      "relation": "",
      "subject_filter": {
        "subject_namespace": "rbac",
        "subject_type": "workspace",
        "subject_id": "",
        "relation": null
      }
    }
  }'
```


## 6. DR Reconciliation Procedure

**This feature is not yet implemented.** The DR reconciliation endpoint, feature flag, and Celery task described below are planned but do not exist in the codebase yet. Do not attempt to use these commands during an incident -- they will return 404. This section will be updated when the feature is implemented.

In the meantime, for database restore scenarios, escalate to the **RBAC engineering team** and use the existing remediation tools: re-bootstrap affected tenants (Section 5.3), run parity checks (Section 5.4), and rebuild workspace relations (Section 5.5) as needed.


## 7. Escalation Path

### When to escalate

| Situation | Escalate to | Why |
|---|---|---|
| Kessel Relations API is down or returning persistent errors | **Kessel team** | They own the Relations API and SpiceDB infrastructure |
| Kessel write latency is consistently high (P95 > 5s) | **Kessel team** | May indicate SpiceDB performance degradation |
| Debezium connector is down or not publishing to Kafka | **Platform team (app-interface)** | Debezium connectors are managed via app-interface |
| Kafka broker is unavailable or topic is misconfigured | **Platform team** | Kafka infrastructure is managed by the platform team |
| Database restored from backup and data consistency is needed | **DBA team + RBAC team** | DBA provides the restore timestamp; RBAC team runs re-bootstrap, parity checks, and workspace relation rebuilds |
| Consumer keeps crashing on the same message after skipping | **RBAC team (engineering)** | Likely a bug in the consumer or replication logic |
| Alert persists after all remediation steps | **RBAC team lead** | May require code changes or architectural investigation |

### Useful links

- **Grafana dashboard:** insights-rbac-operations (RBAC Consumer section)
- **Consumer source code:** `rbac/core/kafka_consumer.py`
- **Outbox replicator:** `rbac/management/relation_replicator/outbox_replicator.py`
- **gRPC replicator:** `rbac/management/relation_replicator/relations_api_replicator.py`
- **Celery tasks:** `rbac/management/tasks.py`
- **Internal API endpoints:** `rbac/internal/views.py`
- **Consumer pod config:** `deploy/rbac-clowdapp.yml` (deployment `rbac-kafka-consumer`)
- **Alert definition:** app-interface (gitlab.cee.redhat.com MR 166360)
