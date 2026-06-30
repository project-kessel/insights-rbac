# Kafka Shadow Mode - Manual Testing Guide

This guide provides manual testing strategies for validating the 3-state principal cleanup feature flag in deployed environments.

## Overview

The `rbac.principal-cleanup.use-kafka.enabled` feature flag now supports 3 modes:
- **umb_only** (flag OFF): Only UMB processes messages and writes to DB
- **kafka_shadow** (flag ON with variant): Both UMB and Kafka run, only UMB writes (Kafka validates)
- **kafka_active** (flag ON, default): Only Kafka processes messages and writes to DB

## Prerequisites

- Access to the deployed environment (stage/prod)
- Access to Unleash admin UI
- Access to Prometheus/Grafana for metrics
- Access to application logs (Kibana/CloudWatch)
- `oc` CLI configured for the namespace

## 1. Unleash Configuration

### Configure UMB Only Mode (Baseline)
```
1. Go to Unleash UI
2. Find flag: rbac.principal-cleanup.use-kafka.enabled
3. Set to: OFF
4. Expected mode: umb_only
```

### Configure Shadow Mode (Validation)
```
1. Go to Unleash UI
2. Find flag: rbac.principal-cleanup.use-kafka.enabled
3. Set to: ON
4. Add variant:
   - Name: kafka_shadow
   - Weight: 100%
5. Expected mode: kafka_shadow
```

### Configure Kafka Active Mode (Cutover)
```
1. Go to Unleash UI
2. Find flag: rbac.principal-cleanup.use-kafka.enabled
3. Set to: ON
4. Remove variant OR set variant to:
   - Name: kafka_active
   - Weight: 100%
5. Expected mode: kafka_active
```

## 2. Verify Mode via Logs

### Check Current Mode
```bash
# Get celery worker pod
POD=$(oc get pods -l app=rbac-worker -o jsonpath='{.items[0].metadata.name}')

# Tail logs for mode detection
oc logs -f $POD | grep "Principal cleanup mode"
```

**Expected output examples:**
```
INFO: Principal cleanup mode: umb_only
INFO: UMB-only mode: processing via UMB
```

```
INFO: Principal cleanup mode: kafka_shadow
INFO: Shadow mode: processing via UMB (active) and Kafka (dry-run)
INFO: Shadow mode: Running UMB consumer (active - writes to DB)
INFO: Shadow mode: Running Kafka consumer (dry-run - no DB writes)
```

```
INFO: Principal cleanup mode: kafka_active
INFO: Kafka-active mode: processing via Kafka
```

## 3. Monitor Shadow Mode Activity

### Check for Dry-Run Log Messages
```bash
oc logs -f $POD | grep "DRY RUN"
```

**Expected output in shadow mode:**
```
WARNING: 🔍 KAFKA SHADOW MODE: Messages will be processed but NO database writes will occur.
INFO: process_principal_events_from_kafka: Processing message from partition 0 at offset 42 (DRY RUN)
INFO: 🔍 DRY RUN: Would process user_id=12345 org_id=67890 is_active=False
INFO: 🔍 DRY RUN: Would call bootstrap_service.update_user() for user jdoe
```

### Verify No Database Writes from Kafka in Shadow Mode
```bash
# Count principals before
BEFORE=$(oc exec $POD -- python manage.py shell -c "from management.principal.model import Principal; print(Principal.objects.count())")

# Wait for shadow mode to process messages (60 seconds)
sleep 60

# Count principals after
AFTER=$(oc exec $POD -- python manage.py shell -c "from management.principal.model import Principal; print(Principal.objects.count())")

# In shadow mode, count should only change from UMB, not Kafka
echo "Before: $BEFORE, After: $AFTER"
```

## 4. Monitor Metrics

### Access Prometheus/Grafana

**Metrics to monitor:**

| Metric | Description | Expected in Shadow Mode |
|--------|-------------|------------------------|
| `stomp_messages_ack_total` | UMB messages processed | Increasing |
| `kafka_messages_success_total` | Kafka messages processed (all modes) | Increasing |
| `kafka_dry_run_messages_total` | Kafka dry-run messages | Increasing (shadow only) |
| `kafka_dry_run_errors_total` | Kafka dry-run errors | Should be 0 or very low |
| `kafka_messages_failure_total` | Kafka production failures | Should be 0 |

### Query Examples (PromQL)

```promql
# Check if shadow mode is active (dry-run messages increasing)
rate(kafka_dry_run_messages_total[5m])

# Verify UMB and Kafka processing similar volumes
rate(stomp_messages_ack_total[5m])
rate(kafka_dry_run_messages_total[5m])

# Check for validation errors in shadow mode
increase(kafka_dry_run_errors_total[1h])

# Compare error rates
rate(stomp_messages_nack_total[5m])
rate(kafka_dry_run_errors_total[5m])
```

### Expected Behavior in Shadow Mode

1. **Message Parity**:
   ```
   stomp_messages_ack_total ≈ kafka_dry_run_messages_total
   ```
   Both should increase at similar rates

2. **Low Error Rate**:
   ```
   kafka_dry_run_errors_total < 1% of kafka_dry_run_messages_total
   ```

3. **No Production Kafka Errors**:
   ```
   kafka_messages_failure_total = 0 (or same as before shadow mode)
   ```

## 5. Test Message Flow

### Send Test Message

```bash
# Create a test principal cleanup event
# This depends on your message bus setup

# For UMB (example):
curl -X POST https://umb.example.com/topic/platform.principal-cleanup \
  -H "Content-Type: application/json" \
  -d '{
    "CanonicalMessage": {
      "Header": {
        "InstanceId": "test-12345"
      },
      "Body": {
        "user_id": "test-user-123",
        "org_id": "11111111",
        "username": "test-user",
        "is_active": false
      }
    }
  }'

# For Kafka (example):
echo '{"CanonicalMessage": {"Header": {"InstanceId": "test-12345"}, "Body": {"user_id": "test-user-123", "org_id": "11111111", "username": "test-user", "is_active": false}}}' | \
  oc exec -i kafka-0 -- kafka-console-producer.sh \
    --broker-list localhost:9092 \
    --topic platform.rbac.principal-cleanup
```

### Verify Processing

**In UMB-only mode:**
```bash
# Check logs
oc logs $POD | grep "test-user-123"

# Verify principal was removed (if inactive)
oc exec $POD -- python manage.py shell -c "from management.principal.model import Principal; print(Principal.objects.filter(username='test-user').exists())"
```

**In Shadow mode:**
```bash
# Check UMB processed it
oc logs $POD | grep "test-user-123" | grep -v "DRY RUN"

# Check Kafka processed it in dry-run
oc logs $POD | grep "test-user-123" | grep "DRY RUN"

# Verify both incremented metrics
# (check Prometheus as shown above)
```

**In Kafka-active mode:**
```bash
# Check only Kafka logs
oc logs $POD | grep "test-user-123"

# Should NOT see UMB processing
oc logs $POD | grep "stomp"
```

## 6. Consumer Offset Validation

### Check Kafka Consumer Group Status

```bash
# Get Kafka pod
KAFKA_POD=$(oc get pods -l app=kafka -o jsonpath='{.items[0].metadata.name}')

# Check consumer group lag
oc exec $KAFKA_POD -- kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group rbac-principal-cleanup \
  --describe
```

**What to check:**
- `LAG`: Should be low (<100) in steady state
- `CURRENT-OFFSET`: Should be advancing
- `LOG-END-OFFSET`: Total messages in topic

**In shadow mode**, consumer group should advance normally even though it's dry-run.

## 7. Database State Validation

### Verify No Unexpected Changes in Shadow Mode

```sql
-- Run before enabling shadow mode
SELECT COUNT(*) FROM principal WHERE type = 'user';

-- Enable shadow mode, wait 5 minutes

-- Run after
SELECT COUNT(*) FROM principal WHERE type = 'user';

-- Count should only change from UMB processing,
-- NOT from Kafka (since it's dry-run)
```

### Check for Duplicate Processing

```sql
-- Check for principals with recent updates
SELECT username, modified
FROM principal
WHERE modified > NOW() - INTERVAL '1 hour'
ORDER BY modified DESC
LIMIT 20;

-- In shadow mode, you should NOT see duplicate rapid updates
-- from both UMB and Kafka processing the same message
```

## 8. Performance Validation

### Monitor Resource Usage

```bash
# Check CPU/Memory usage
oc adm top pod $POD

# In shadow mode, expect ~1.5-2x resource usage
# (running both consumers)
```

### Check Processing Latency

```bash
# Time between message arrival and processing
# Check Kafka message timestamps in logs
oc logs $POD | grep "Processing message" | tail -20
```

## 9. Error Scenario Testing

### Test Malformed Message Handling

**Create a malformed message:**
```bash
echo '{"invalid": "message without required fields"}' | \
  oc exec -i kafka-0 -- kafka-console-producer.sh \
    --broker-list localhost:9092 \
    --topic platform.rbac.principal-cleanup
```

**In shadow mode, expect:**
```
ERROR: process_kafka_message: Error processing Kafka message (DRY RUN): ...
WARNING: 🔍 DRY RUN: Message would have failed in production. Committing offset to continue validation.
```

**Verify:**
- Metric `kafka_dry_run_errors_total` increments
- Consumer continues processing (doesn't stop)
- Offset is committed (message not reprocessed)

**In kafka-active mode:**
- Message goes to DLQ (if configured)
- OR consumer stops and retries

## 10. Rollback Testing

### Test Mode Switching

1. **Start in umb_only**
   - Verify UMB processing

2. **Switch to kafka_shadow**
   - Verify both run
   - Verify only UMB writes

3. **Switch back to umb_only**
   - Verify clean shutdown of Kafka consumer
   - Verify UMB continues normally

4. **Switch to kafka_active**
   - Verify Kafka takes over
   - Verify UMB stops (or continues if dual-write)

**No restart should be required** - Celery beat will pick up the new mode on the next run (within 60 seconds).

## 11. Alerts to Configure

### Recommended Alerts

```yaml
# Shadow mode validation error rate too high
- alert: KafkaShadowModeHighErrorRate
  expr: rate(kafka_dry_run_errors_total[5m]) > 0.01
  annotations:
    summary: "Kafka shadow mode error rate is {{ $value }}"
    description: "More than 1% of dry-run messages are failing validation"

# Shadow mode message mismatch
- alert: KafkaShadowModeMessageMismatch
  expr: abs(rate(stomp_messages_ack_total[5m]) - rate(kafka_dry_run_messages_total[5m])) > 10
  annotations:
    summary: "UMB and Kafka processing different message volumes"
    description: "UMB rate: {{ rate(stomp_messages_ack_total[5m]) }}, Kafka rate: {{ rate(kafka_dry_run_messages_total[5m]) }}"

# Consumer lag too high
- alert: KafkaConsumerLagHigh
  expr: kafka_consumer_lag > 1000
  annotations:
    summary: "Kafka consumer lag is {{ $value }} messages"
    description: "Consumer is falling behind message production"
```

## 12. Success Criteria for Shadow Mode

Before switching to `kafka_active`, verify:

- ✅ Shadow mode runs for at least 1 week without issues
- ✅ `kafka_dry_run_errors_total` < 0.1% of `kafka_dry_run_messages_total`
- ✅ Message processing rates match: `stomp_messages_ack_total` ≈ `kafka_dry_run_messages_total`
- ✅ No database inconsistencies detected
- ✅ Consumer lag stays below 100 messages
- ✅ Resource usage is acceptable (<2x baseline)
- ✅ All error scenarios tested and handled correctly

## 13. Troubleshooting

### Shadow Mode Not Running

**Symptom:** No `kafka_dry_run_messages_total` metric increasing

**Check:**
```bash
# Verify flag configuration
curl https://unleash.example.com/api/admin/features/rbac.principal-cleanup.use-kafka.enabled

# Check worker logs
oc logs $POD | grep "kafka_shadow"

# Verify both job settings are enabled
oc exec $POD -- env | grep -E "UMB_JOB_ENABLED|KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED"
```

### Only One Consumer Running in Shadow Mode

**Symptom:** Only seeing UMB or Kafka logs, not both

**Check:**
```bash
# Verify task dispatcher logic
oc logs $POD | grep "Shadow mode: Running"

# Should see both:
# "Shadow mode: Running UMB consumer"
# "Shadow mode: Running Kafka consumer"
```

### Database Writes from Kafka in Shadow Mode

**Symptom:** Database changing from Kafka in dry-run mode

**Check:**
```bash
# Verify dry_run parameter is being passed
oc logs $POD | grep "process_principal_events_from_kafka.*dry_run"

# Verify update_user is NOT called
oc logs $POD | grep "update_user" | grep -c "DRY RUN"
```

## Summary

This guide provides comprehensive manual testing strategies for the 3-state principal cleanup feature. Use these tests to validate the shadow mode in deployed environments before cutting over to Kafka as the primary message consumer.
