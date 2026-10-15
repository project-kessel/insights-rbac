# RBAC Kafka Consumer

The RBAC Kafka consumer is a robust, production-ready service that processes relation replication messages to ensure data consistency across the RBAC system. It consumes messages from Kafka topics containing Debezium change data capture events and applies relation changes with guaranteed ordering and eventual consistency.

## What It Does

The consumer processes two main types of messages:

### Debezium Messages
These are change data capture events generated when the RBAC system writes to the outbox table. The consumer validates and processes these messages to replicate relation changes to external systems.

### Replication Messages
These contain specific relation operations with:
- **`relations_to_add`**: New relationships to create (e.g., user becomes member of group)
- **`relations_to_remove`**: Existing relationships to delete (e.g., user removed from group)

The consumer ensures these changes are applied in the correct order to maintain data consistency.

## Key Capabilities

### Reliability & Consistency
- **No Message Loss**: Every message is processed successfully through infinite retry logic
- **Strict Ordering**: Messages within a partition are processed sequentially
- **Exactly-Once Processing**: Each message is processed exactly once using manual offset commits
- **Fault Tolerance**: Automatically recovers from temporary failures

### Production Ready
- **Health Monitoring**: Kubernetes-compatible health checks that work during idle periods
- **Metrics & Observability**: Comprehensive Prometheus metrics for monitoring
- **Graceful Shutdown**: Clean shutdown handling for deployments
- **Resource Efficient**: Optimized for production workloads

## How to Run

### Quick Start

The easiest way to run the consumer is using the provided Makefile targets:

```bash
# Start the consumer with default configuration
make kafka-consumer

# Start with debug logging for troubleshooting
make kafka-consumer-debug
```

### Manual Execution

You can also run the consumer directly:

```bash
# Basic execution
KAFKA_ENABLED=true RBAC_KAFKA_CONSUMER_TOPIC=outbox.event.rbac-consumer-replication-event \
  pipenv run python rbac/manage.py launch-rbac-kafka-consumer

# With custom topic
KAFKA_ENABLED=true RBAC_KAFKA_CONSUMER_TOPIC=my-custom-topic \
  pipenv run python rbac/manage.py launch-rbac-kafka-consumer --topic my-topic

# With debug logging
KAFKA_ENABLED=true RBAC_KAFKA_CONSUMER_TOPIC=outbox.event.rbac-consumer-replication-event \
  DJANGO_LOG_LEVEL=DEBUG pipenv run python rbac/manage.py launch-rbac-kafka-consumer
```

### Command Line Options

The consumer supports several command line options:

```bash
python rbac/manage.py launch-rbac-kafka-consumer --help
```

Available options:
- `--topic TOPIC`: Override the default Kafka topic
- `--verbosity {0,1,2,3}`: Set Django verbosity level
- `--settings SETTINGS`: Specify Django settings module

## Configuration

The consumer is configured through environment variables. Most settings have sensible defaults for production use.

### Essential Settings

These settings must be configured for the consumer to work:

| Variable | Description | Example | Required |
|----------|-------------|---------|----------|
| `KAFKA_ENABLED` | Enable Kafka functionality | `true` | Yes |
| `RBAC_KAFKA_CONSUMER_TOPIC` | Kafka topic to consume from | `platform.rbac.read-after-write` | Yes |
| `RBAC_KAFKA_CONSUMER_GROUP_ID` | Consumer group ID for offset tracking | `rbac-consumer-group` | No |

### Deployment Settings

These control how the consumer runs in Kubernetes:

| Variable | Description | Default | Notes |
|----------|-------------|---------|-------|
| `RBAC_KAFKA_CONSUMER_REPLICAS` | Number of consumer instances | `1` | Set to `0` to disable |
| `DJANGO_LOG_LEVEL` | Logging verbosity | `INFO` | Use `DEBUG` for troubleshooting |

### Integration Settings

These configure connections to external systems:

| Variable | Description | Default | Notes |
|----------|-------------|---------|-------|
| `KAFKA_SERVERS` | Kafka bootstrap servers | Auto-detected | Usually from Clowder |
| `REPLICATION_TO_RELATION_ENABLED` | Enable relation replication | `false` | Production feature |
| `INVENTORY_API_SERVER` | Kessel Inventory API endpoint used by `InventoryApiReplicator` | `localhost:9000` | gRPC server; auth via `INVENTORY_API_CLIENT_ID` / `INVENTORY_API_CLIENT_SECRET` |

## How Message Processing Works

### Message Flow

1. **Consumption**: Consumer reads messages from Kafka topic in order
2. **Validation**: Each message is validated for correct structure and content
3. **Processing**: Valid messages are processed to apply relation changes
4. **Retry Logic**: Failed messages are retried infinitely with exponential backoff
5. **Commit**: Successfully processed messages have their Kafka offset committed

### Message Types & Structure

The consumer processes structured JSON messages with specific formats:

#### Debezium Messages
The consumer only accepts standard Debezium message format:

```json
{
  "schema": {
    "type": "string",
    "optional": false,
    "name": "io.debezium.data.Json",
    "version": 1
  },
  "payload": "{\"relations_to_add\": [...], \"relations_to_remove\": [...]}"
}
```

**Required Fields:**
- `schema`: Debezium schema definition (object)
- `payload`: JSON string or object containing the actual data

**Important Notes:**
- Messages not in this format will be rejected
- The `payload` field can be either a JSON string (standard) or pre-parsed object
- Only relation change messages are currently supported in the payload

#### Relations Payload
For relation changes (`aggregatetype: "relations"`):

```json
{
  "relations_to_add": [
    {
      "resource": {"type": "rbac", "id": "group-123"},
      "subject": {"type": "rbac", "id": "user-456"},
      "relation": "member"
    }
  ],
  "relations_to_remove": [
    {
      "resource": {"type": "rbac", "id": "group-123"},
      "subject": {"type": "rbac", "id": "user-789"},
      "relation": "member"
    }
  ]
}
```

#### Workspace Payload
For workspace events (`aggregatetype: "workspace"`):

```json
{
  "org_id": "12345",
  "account_number": "67890",
  "workspace": {
    "id": "workspace-uuid-789",
    "name": "Development Environment"
  },
  "operation": "create"
}
```

## Health Monitoring

The consumer includes comprehensive health monitoring designed for production Kubernetes deployments.

### Health Check System

The consumer maintains two types of health indicators:

- **Liveness**: Indicates the consumer process is running
- **Readiness**: Indicates the consumer can successfully process messages

These work by creating/removing files that Kubernetes can check, and they continue working even when no messages are being processed.

### Background Health Monitoring

A background thread runs every 30 seconds to:
- Verify Kafka connectivity
- Update health status files
- Monitor consumer state

This ensures health checks work during idle periods when no messages are in the queue.

### Kubernetes Integration

Configure your deployment with these health check settings:

```yaml
# Check if process is alive (restart if fails)
livenessProbe:
  exec:
    command: ["test", "-f", "/tmp/kubernetes-liveness"]
  initialDelaySeconds: 30
  periodSeconds: 10
  failureThreshold: 3

# Check if ready to process (stop traffic if fails)
readinessProbe:
  exec:
    command: ["test", "-f", "/tmp/kubernetes-readiness"]
  initialDelaySeconds: 10
  periodSeconds: 5
  failureThreshold: 3
```

### Health States

| Scenario | Liveness | Readiness | Kubernetes Action |
|----------|----------|-----------|-------------------|
| Normal operation | ✅ | ✅ | Routes traffic |
| Idle (no messages) | ✅ | ✅ | Routes traffic |
| Kafka disconnected | ✅ | ❌ | Stops traffic |
| Consumer crashed | ❌ | ❌ | Restarts pod |

## Reliability & Error Handling

The consumer is designed for maximum reliability and data consistency in production environments.

### Infinite Retry Logic

The consumer never skips messages. If processing fails, it will retry infinitely until successful:

- **Exponential Backoff**: Delays start at 1 second and increase up to 5 minutes
- **Jitter**: Random delays prevent thundering herd problems
- **Graceful Shutdown**: Retries can be interrupted for deployments

### Retry Behavior

When a message fails to process, the consumer follows this pattern:

| Attempt | Delay | Total Time | Status |
|---------|-------|------------|--------|
| 1       | 1s    | 1s         | First retry |
| 2       | 2s    | 3s         | Exponential backoff |
| 3       | 4s    | 7s         | Continues |
| 5       | 16s   | 31s        | Still trying |
| 10      | 5m    | ~17m       | Max delay reached |
| ∞       | 5m    | Forever    | Until success |

### Message Ordering Guarantees

- **Sequential Processing**: Messages within a partition are processed one at a time
- **No Skipping**: Failed messages block subsequent messages until resolved
- **Exactly-Once**: Each message is processed exactly once using manual offset commits
- **Resumable**: Consumer restarts resume from the last successfully processed message

### Error Classification

The consumer handles different types of errors appropriately:

- **Transient Errors** (network timeouts, database locks): Retry infinitely
- **Logic Errors** (validation failures, processing bugs): Retry infinitely
- **Permanent Errors** (malformed JSON): Skip and log (rare)
- **Shutdown Signals**: Stop gracefully and resume on restart

### Data Consistency Benefits

This approach ensures:
- **No Lost Messages**: Every valid message is eventually processed
- **Correct Order**: Relation changes are applied in the exact sequence they occurred
- **Fault Tolerance**: Temporary issues don't cause data loss
- **Idempotent Restarts**: Safe to restart the consumer at any time

## Async Rebalance Listener Threading Model

The consumer uses `AsyncConsumerRebalanceListener` (kafka-python 3.0.11) to handle partition rebalance events without blocking the IO thread. Both `on_partitions_assigned` and `on_partitions_revoked` are async coroutines that dispatch blocking work to a background thread via `_run_in_thread`, which uses `kafka.future.Future` + `threading.Thread`.

### Why a worker thread is needed

Rebalance callbacks run as coroutines on the IO thread (NetworkSelector). Calling blocking operations directly (e.g., `consumer.commit()`, gRPC lock acquisition) from the IO thread would deadlock: `consumer.commit()` internally calls `net.run()`, which raises `RuntimeError("Cannot block on net.run() from the IO thread itself")`.

### Thread-safety of consumer calls from the worker thread

The worker thread calls `OffsetManager.commit()`, which uses `consumer.assignment()` and `consumer.commit(offsets=...)`. Both are safe to call from a non-IO thread:

- **`assignment()`** delegates to `SubscriptionState.assigned_partitions()`, which is `@synchronized` (acquires an `RLock`) and returns a copy of local state. No network IO involved. (subscription_state.py L349)

- **`commit()`** calls `ConsumerCoordinator.commit_offsets_sync()`, which uses `net.run(coroutine)`. When the caller is not the IO thread, `NetworkSelector.run()` enqueues the coroutine via `call_soon()`, wakes the selector, and blocks on a `threading.Event` until the IO thread processes the request. The docstring states: *"Safe to call concurrently from multiple caller threads."* (selector.py L357-362)

### No deadlock risk

The async rebalance callback `await`s a `KafkaFuture` returned by `_run_in_thread`. This yields control back to the NetworkSelector, which continues its poll loop (heartbeats, IO). When the worker thread's `commit()` call enqueues work via `call_soon()`, the selector picks it up and runs it on the IO thread. The sequence completes without circular blocking:

1. Async callback yields `KafkaFuture` → selector continues polling
2. Worker thread calls `commit()` → `net.run()` → enqueues on selector → blocks on `Event`
3. Selector processes enqueued commit coroutine → sets `Event`
4. Worker thread unblocks → resolves `KafkaFuture` → calls `net.wakeup()`
5. Selector wakes → resumes the rebalance callback coroutine

## Monitoring & Observability

The consumer provides comprehensive metrics for production monitoring and alerting.

### Prometheus Metrics

The consumer exposes these metrics for monitoring:

#### Message Processing
- **`rbac_kafka_consumer_messages_processed_total`**: Count of processed messages
  - Labels: `message_type` (debezium, relations, workspace), `status` (success, error, etc.)
- **`rbac_kafka_consumer_message_processing_duration_seconds`**: Processing time histogram
  - Labels: `message_type`

#### Error Tracking
- **`rbac_kafka_consumer_validation_errors_total`**: Validation error count
  - Labels: `error_type` (missing_field, invalid_type, etc.)
- **`rbac_kafka_consumer_retry_attempts_total`**: Retry attempt count
  - Labels: `retry_reason`, `attempt_number`
- **`rbac_kafka_consumer_message_retry_duration_seconds`**: Time spent retrying
  - Labels: `retry_reason`

### Key Metrics to Monitor

For production alerting, monitor these metrics:

1. **Message Processing Rate**: `rate(rbac_kafka_consumer_messages_processed_total[5m])`
2. **Error Rate**: `rate(rbac_kafka_consumer_validation_errors_total[5m])`
3. **Retry Rate**: `rate(rbac_kafka_consumer_retry_attempts_total[5m])`
4. **Processing Latency**: `histogram_quantile(0.95, rbac_kafka_consumer_message_processing_duration_seconds)`

### Recommended Alerts

```yaml
# High error rate
- alert: RBACConsumerHighErrorRate
  expr: rate(rbac_kafka_consumer_validation_errors_total[5m]) > 0.1

# Consumer not processing messages
- alert: RBACConsumerStalled
  expr: rate(rbac_kafka_consumer_messages_processed_total[5m]) == 0

# High retry rate indicates issues
- alert: RBACConsumerHighRetryRate
  expr: rate(rbac_kafka_consumer_retry_attempts_total[5m]) > 1
```

## Deployment

The consumer runs as a Kubernetes deployment with configurable scaling and resource management.

### Scaling

Control the number of consumer instances:

```bash
# Single instance (default)
RBAC_KAFKA_CONSUMER_REPLICAS=1

# Multiple instances for higher throughput
RBAC_KAFKA_CONSUMER_REPLICAS=3

# Disable consumer (maintenance mode)
RBAC_KAFKA_CONSUMER_REPLICAS=0
```

**Important**: Multiple replicas will process messages from different partitions in parallel, but messages within each partition are still processed sequentially to maintain ordering.

### Resource Configuration

The consumer deployment includes:
- CPU and memory limits/requests
- Health check configuration
- Environment variable injection
- Automatic restart on failure

See `deploy/rbac-clowdapp.yml` for complete deployment configuration.

## Troubleshooting

### Common Issues & Solutions

| Problem | Symptoms | Solution |
|---------|----------|----------|
| Consumer won't start | Pod crashes on startup | Check `KAFKA_ENABLED=true` and topic configuration |
| No messages processed | Zero processing rate | Verify Kafka connectivity and topic exists |
| High retry rate | Many retry attempts | Check downstream services (database, APIs) |
| Validation failures | Validation error metrics | Review message structure and format |

### Debug Mode

For detailed troubleshooting, enable debug logging:

```bash
# Debug mode with detailed logging
make kafka-consumer-debug

# Or manually with debug level
DJANGO_LOG_LEVEL=DEBUG make kafka-consumer
```

### Log Analysis

Monitor these log patterns:

```
# Normal operation
[INFO] RBAC Kafka consumer started, listening on topic "platform.rbac.read-after-write"
[INFO] Processing message (partition: 0, offset: 1234)
[DEBUG] Message content (partition: 0, offset: 1234): {"aggregatetype": "relations", "aggregateid": "group-123", ...}
[INFO] Processing relations message - aggregateid: group-123, event_type: create_group
[INFO] Message processed successfully (partition: 0, offset: 1234)
[INFO] Committed offset (partition: 0, offset: 1234)

# Non-JSON messages (reduced noise)
[INFO] Skipped 10 non-JSON messages (latest: partition 0, offset 1245) - Sample content: b'  "event_type": "role_binding_created",'

# Retry scenarios
[WARN] Processing error for message (partition: 0, offset: 1235) on attempt 1: Database timeout
Message content: {'aggregatetype': 'relations', 'aggregateid': 'group-123', ...}
[INFO] Retrying message processing in 1.05s (attempt 1, partition: 0, offset: 1235)
[INFO] Message successfully processed after 3 attempts (total retry time: 4.23s)

# Health monitoring
[DEBUG] Health check passed: consumer is connected and ready
```

### Performance Tuning

For high-throughput scenarios:
- Increase `RBAC_KAFKA_CONSUMER_REPLICAS` to process multiple partitions
- Monitor processing latency metrics
- Adjust resource limits based on actual usage
- Consider topic partitioning strategy

## Development

### Running Tests

```bash
# All consumer tests
python rbac/manage.py test tests.core.test_kafka_consumer tests.management.test_launch_rbac_kafka_consumer

# Specific test classes
python rbac/manage.py test tests.core.test_kafka_consumer.RetryConfigTests
```

### Adding Message Types

To support new message types:

1. Add aggregate type to `MessageValidator.VALID_AGGREGATE_TYPES`
2. Implement validation in `MessageValidator.validate_*_message`
3. Add processing logic in `RBACKafkaConsumer._process_*_message`
4. Add comprehensive tests
5. Update documentation with examples

### Files Modified

The consumer implementation spans these key files:

- `rbac/core/kafka_consumer.py` - Core consumer logic and validation
- `rbac/management/management/commands/launch-rbac-kafka-consumer.py` - Django management command
- `deploy/rbac-clowdapp.yml` - Kubernetes deployment configuration
- `rbac/rbac/settings.py` - Django settings for consumer configuration
- `tests/core/test_kafka_consumer.py` - Consumer unit tests
- `tests/management/test_launch_rbac_kafka_consumer.py` - Command tests
