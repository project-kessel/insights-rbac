# Integration Guidelines

## External Services Overview

RBAC integrates with seven external services: Kessel Relations (gRPC), Kessel Inventory (gRPC), Kafka (producer + consumer), BOP (HTTP), IT Service (HTTP), UMB (STOMP), and Redis (Celery broker + cache). All connections are configured via environment variables with local-dev bypass modes.

## 1. Kessel Relations API (gRPC) -- Relation Replication

### Replicator Selection

Controlled by `get_replicator(write_relationships)` in `rbac/internal/utils.py`:

- `"true"` / `"outbox"` -- `OutboxReplicator` (production: writes to outbox table for Debezium CDC)
- `"logging"` -- `LoggingReplicator` (logs tuples without writing)
- `"false"` -- `NoopReplicator` (no-op)

Always use the replicator abstraction. Never call the Relations API directly from service code -- go through `RelationReplicator.replicate(event)`.

### Building Replication Events

```python
event = ReplicationEvent(
    event_type=ReplicationEventType.CREATE_CUSTOM_ROLE,
    partition_key=PartitionKey.byEnvironment(),
    add=[relation_tuple],       # RelationTuple or common_pb2.Relationship
    remove=[],
    info={"org_id": org_id, "role_id": str(role.uuid)},
)
replicator.replicate(event)
```

Rules:
- Always include `org_id` in `info` dict -- it is required for `resource_context()`.
- For `CREATE_WORKSPACE` events, also include `workspace_id` in `info`.
- `PartitionKey.byEnvironment()` is the only partition key currently used. All events are globally ordered per environment.
- Never produce duplicate tuples in `add` -- `OutboxReplicator` raises `ValueError` on duplicates.
- Empty events (no adds or removes) are silently skipped with a warning log.

### RelationTuple Domain Type

Use `RelationTuple` from `management/relation_replicator/types.py` instead of raw protobuf messages. It validates fields on construction (non-empty strings, valid patterns, no `*` for resource IDs). Convert to protobuf with `.as_message()` or to dict with `.to_dict()`.

### gRPC Channel Creation

Three channel factories in `management/utils.py`:
- `create_client_channel_relation(addr)` -- Relations API (JWT auth via metadata)
- `create_client_channel_inventory(addr)` -- Inventory API (OAuth2 credentials)
- `create_client_channel(addr)` -- Legacy, same as relation

All use insecure channels when `DEVELOPMENT=True` or `CLOWDER_ENABLED=true`, TLS otherwise. Always use as context managers.

### Auth for Relations API

JWT tokens are obtained from Redis via `JWTManager` (not per-request OAuth2). The consumer uses `JWTCacheOptimized`; request-path code uses `JWTCache`. Both are in `management/cache.py`. Token is passed as gRPC metadata: `[("authorization", f"Bearer {token}")]`.

Key env vars: `RELATION_API_SERVER` (default `localhost:9000`), `RELATION_API_CLIENT_ID`, `RELATION_API_CLIENT_SECRET`.

## 2. Debezium CDC / Outbox Pattern

The outbox pattern is the production replication path:

1. Service code calls `OutboxReplicator.replicate(event)`.
2. `OutboxWAL.log()` inserts into `management_outbox` then immediately deletes -- Debezium reads from PostgreSQL WAL, not the table.
3. Debezium publishes the change event to a Kafka topic.
4. `RBACKafkaConsumer` reads the topic and calls `RelationsApiReplicator` to write/delete tuples via gRPC.

The Outbox model (`management/debezium/model.py`) follows the Debezium outbox event router schema: `aggregatetype`, `aggregateid`, `event_type` (column `type`), `payload` (JSON).

### Testing the Outbox

Use `InMemoryLog` instead of `OutboxWAL` in tests:

```python
log = InMemoryLog()
replicator = OutboxReplicator(log=log)
# ... trigger operation ...
assert len(log) == 1
assert log.first().payload["relations_to_add"][0]["resource"]["id"] == expected_id
```

## 3. Kafka

### Producer (`core/kafka.py`)

`RBACProducer` is a singleton-ish class. In development/test (`DEVELOPMENT=True`, `MOCK_KAFKA=True`, or `KAFKA_ENABLED=False`), it returns `FakeKafkaProducer` (no-op). Production uses `kafka-python`'s `KafkaProducer` with up to 5 retries on initialization.

Only used for notifications. To send: `producer.send_kafka_message(topic, message_dict, headers)`.

### Consumer (`core/kafka_consumer.py`)

`RBACKafkaConsumer` is a standalone process (not part of the Django web server). Key behaviors:

- Manual offset commits (at-least-once delivery), committed every N messages (`CommitConfig.commit_modulo`, default 10).
- Fencing via Kessel lock tokens -- acquired on partition assignment, validated on every write.
- Exponential backoff retry (`RetryConfig`) with jitter. `ValidationError` and `ParseError` are non-retryable.
- Health checks via file-based probes: `/tmp/kubernetes-liveness`, `/tmp/kubernetes-readiness`.
- On max retries exceeded, consumer stops without committing -- Kubernetes restarts the pod.

Env vars: `RBAC_KAFKA_CONSUMER_TOPIC`, `RBAC_KAFKA_CONSUMER_GROUP_ID` (default `rbac-consumer-group`), `RBAC_KAFKA_CUSTOM_CONSUMER_BROKER`.

### Kafka Auth

Configured by Clowder (`KAFKA_AUTH` dict with SASL/SSL settings). Falls back to plain `bootstrap_servers` locally. The consumer filters out producer-only configs (`retries`, `acks`, etc.) from shared `KAFKA_AUTH`.

## 4. Kessel Inventory API (gRPC) -- Access Checks

Two access check patterns in `management/permissions/workspace_inventory_access.py`:

- **CheckForUpdate**: Point check -- "does principal X have relation Y on resource Z?" Used in permission classes (403 decisions). Uses strongly consistent reads.
- **StreamedListObjects**: List check -- "which workspaces does principal X have relation Y on?" Used in filter backends for list operations. Paginated with continuation tokens (page size 1000, max 10,000 pages).

Both methods fall back gracefully on connectivity errors (return `False` / empty set), not exceptions.

Key env vars: `INVENTORY_API_SERVER`, `INVENTORY_API_CLIENT_ID`, `INVENTORY_API_CLIENT_SECRET`.

## 5. BOP (Back Office Proxy) -- Principal Management

`PrincipalProxy` in `management/principal/proxy.py` calls BOP to verify and list user principals.

- Endpoints: `/v3/accounts/{org_id}/users`, `/v3/accounts/{org_id}/usersBy`, `/v1/users`
- Auth: `x-rh-insights-env`, `x-rh-clientid`, `x-rh-apitoken` headers
- TLS: Clowder CA or local cert at `management/principal/certs/client.pem`
- Bypass: `BYPASS_BOP_VERIFICATION=True` returns principals from local DB without calling BOP

Env vars: `PRINCIPAL_PROXY_SERVICE_PROTOCOL`, `_HOST`, `_PORT`, `_PATH`, `PRINCIPAL_PROXY_CLIENT_ID`, `PRINCIPAL_PROXY_API_TOKEN`.

Metrics: `rbac_proxy_request_processing_seconds` (histogram), `bop_request_status_total` (counter by method+status).

## 6. IT Service -- Service Accounts

`ITService` in `management/principal/it_service.py` is a singleton that fetches service accounts from IT SSO.

- Endpoint: `{protocol}://{host}:{port}{base_path}/service_accounts/v1`
- Auth: Bearer token from the requesting user (`user.bearer_token`)
- Pagination: IT returns pages of 100; client loops until response length < limit
- Bypass: `IT_BYPASS_IT_CALLS=True` returns mock service accounts from local DB

Env vars: `IT_SERVICE_HOST`, `IT_SERVICE_PORT`, `IT_SERVICE_BASE_PATH`, `IT_SERVICE_PROTOCOL_SCHEME`, `IT_SERVICE_TIMEOUT_SECONDS`.

## 7. UMB (Unified Message Bus) -- Principal Lifecycle Events

STOMP-based consumer in `management/principal/cleaner.py`. Processes principal create/update/disable events.

- Controlled by `PRINCIPAL_CLEANUP_DELETION_ENABLED_UMB` and `UMB_JOB_ENABLED` feature flags
- Runs as a Celery beat task every 60 seconds when enabled
- Uses `StompSpec.ACK_CLIENT_INDIVIDUAL` for per-message acknowledgment
- Falls back to BOP-based cleanup (`clean_tenants_principals`) when UMB is disabled (runs every 7 days)

## 8. Notifications Service

Notifications are sent via Kafka to the `NOTIFICATIONS_TOPIC`. The producer (`core/kafka.py`) sends JSON messages matching the template in `management/notifications/message_template.json` (bundle: `console`, application: `rbac`).

Event types: `custom-role-created`, `custom-role-deleted`, `custom-role-updated`, `group-created`, `group-deleted`, `group-updated`, `rh-new-role-available`, `rh-platform-default-role-updated`, `rh-new-tam-request-created`, etc.

Guards:
- `NOTIFICATIONS_ENABLED` controls custom resource notifications
- `NOTIFICATIONS_RH_ENABLED` controls Red Hat system role/group notifications
- `skip_rh_notifications` context var suppresses during seeding
- `KAFKA_ENABLED=False` disables all notifications

## 9. Celery Tasks and Beat Schedule

Broker: Redis (`CELERY_BROKER_URL`). Config namespace: `CELERY_`.

Scheduled tasks in `rbac/rbac/celery.py`:
- `cross_account_cleanup` -- daily at midnight
- `run_redis_cache_health` -- every 30 seconds
- `principal_cleanup_via_umb` -- every 60 seconds (when UMB enabled)
- `principal_cleanup` -- every 7 days (when UMB disabled)

Worker starts a Prometheus metrics server on Clowder's `metricsPort` (default 9000). Failure to start metrics server exits the process.

## 10. Read-Your-Writes (Workspace Creation)

After the Kafka consumer successfully replicates a `create_workspace` event, it sends a PostgreSQL `NOTIFY` on the `READ_YOUR_WRITES_CHANNEL`. The Django request handler `LISTEN`s on this channel to block until the workspace is confirmed replicated. Best-effort -- NOTIFY failure does not fail the replication.

Env vars: `READ_YOUR_WRITES_WORKSPACE_ENABLED`, `READ_YOUR_WRITES_CHANNEL`, `READ_YOUR_WRITES_TIMEOUT_SECONDS` (default 10).

## 11. Feature Flags Summary

| Flag | Default | Effect |
|------|---------|--------|
| `V2_APIS_ENABLED` | `False` | Registers v2 URL routes |
| `KAFKA_ENABLED` | `False` | Enables Kafka producer/consumer |
| `NOTIFICATIONS_ENABLED` | `False` | Enables custom resource notifications |
| `NOTIFICATIONS_RH_ENABLED` | `False` | Enables RH system notifications |
| `BYPASS_BOP_VERIFICATION` | `False` | Skips BOP calls, uses local DB |
| `IT_BYPASS_IT_CALLS` | `False` | Mocks IT service responses |
| `MOCK_KAFKA` | `False` | Uses FakeKafkaProducer |
| `PRINCIPAL_CLEANUP_DELETION_ENABLED_UMB` | `False` | UMB-based principal cleanup |
| `READ_YOUR_WRITES_WORKSPACE_ENABLED` | `False` | Enables workspace create blocking |

## 12. Prometheus Metrics Conventions

All external service calls are instrumented. Follow these patterns:
- Histograms for request duration (suffix `_seconds` or `_processing_seconds`)
- Counters for request status (labels: `method`, `status`)
- Counters for errors (label: `error` or `error_type`)
- Kafka consumer: `rbac_kafka_consumer_*` prefix for all consumer metrics
- Replication latency: `rbac_replication_event_latency_seconds` (histogram with event_type label)
