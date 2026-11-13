#
# Copyright 2024 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""RBAC Kafka consumer for processing Debezium and replication messages."""

import enum
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import grpc
from core.base_consumer import (
    BaseRBACKafkaConsumer,
    CommitConfig,
    RetryConfig,
    ValidationError,
    make_consumer_metrics,
)
from core.kafka import PRODUCER_ONLY_CONFIGS
from django.conf import settings
from django.db import OperationalError, connection, transaction
from google.protobuf import json_format
from internal.migration_coordination import notify_migration_batch_completion
from kafka import TopicPartition
from kafka.consumer.fetcher import ConsumerRecord
from kessel.inventory.v1beta2 import relationship_pb2
from management.inventory_replicator.inventory_api_replicator import InventoryApiReplicator
from prometheus_client import Counter, Histogram

from api.models import Tenant

inventory_api_replication = InventoryApiReplicator()

logger = logging.getLogger("rbac.core.kafka_consumer")

# Metrics
base_metrics = make_consumer_metrics("rbac_kafka_consumer")

message_processing_duration = Histogram(
    "rbac_kafka_consumer_message_processing_duration_seconds",
    "Time spent processing messages",
    ["message_type", "event_type"],
)

kessel_write_duration = Histogram(
    "rbac_kessel_write_duration_seconds",
    "Time spent on Kessel Inventory API write and delete calls",
    ["operation", "event_type"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

validation_errors_total = Counter(
    "rbac_kafka_consumer_validation_errors_total",
    "Total number of validation errors",
    ["error_type"],
)

lock_acquisition_total = Counter(
    "rbac_kafka_consumer_lock_acquisition_total",
    "Total number of lock acquisition attempts",
    ["status", "reason"],  # status: success/failure, reason: rebalance/startup/error_type
)

lock_acquisition_duration = Histogram(
    "rbac_kafka_consumer_lock_acquisition_duration_seconds",
    "Time spent acquiring lock tokens",
    ["status"],  # success, failure
)

# Replication event end-to-end latency metric
# Measures time from event creation (in producer) to successful processing (in consumer)
# Labels: event_type - the type of replication event (e.g., create_custom_role, assign_role)
#
# Example Prometheus queries:
#
# 1. Average replication latency over 5 minutes:
#    rate(rbac_replication_event_latency_seconds_sum[5m]) / rate(rbac_replication_event_latency_seconds_count[5m])
#
# 2. 95th percentile latency:
#    histogram_quantile(0.95, rate(rbac_replication_event_latency_seconds_bucket[5m]))
#
# 3. 99th percentile latency by event type:
#    histogram_quantile(0.99, sum(rate(rbac_replication_event_latency_seconds_bucket[5m])) by (le, event_type))
#
# 4. Median (50th percentile) latency:
#    histogram_quantile(0.50, rate(rbac_replication_event_latency_seconds_bucket[5m]))
#
# 5. Events processed per second by event type:
#    sum(rate(rbac_replication_event_latency_seconds_count[1m])) by (event_type)
#
# 6. Alert: High replication latency (> 30 seconds):
#    histogram_quantile(0.95, rate(rbac_replication_event_latency_seconds_bucket[5m])) > 30
#
replication_event_latency = Histogram(
    "rbac_replication_event_latency_seconds",
    "End-to-end latency from event creation to successful processing in seconds",
    ["event_type"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

consistency_token_save_total = Counter(
    "rbac_kafka_consumer_consistency_token_save_total",
    "Consistency token save attempts and outcomes",
    ["status"],  # success, lock_timeout, error
)


@dataclass
class DebeziumMessage:
    """Represents a validated Debezium message."""

    aggregatetype: str
    aggregateid: str
    event_type: str
    payload: Dict[str, Any]

    @classmethod
    def from_kafka_message(cls, message_value: Dict[str, Any]) -> "DebeziumMessage":
        """Create DebeziumMessage from Kafka message value."""
        return cls(
            aggregatetype=message_value.get("aggregatetype", ""),
            aggregateid=message_value.get("aggregateid", ""),
            event_type=message_value.get("type", ""),
            payload=message_value.get("payload", {}),
        )


@dataclass
class ReplicationMessage:
    """Represents a validated replication message."""

    relations_to_add: List[Dict[str, Any]]
    relations_to_remove: List[Dict[str, Any]]

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "ReplicationMessage":
        """Create ReplicationMessage from payload."""
        return cls(
            relations_to_add=payload.get("relations_to_add", []),
            relations_to_remove=payload.get("relations_to_remove", []),
        )


class MessageValidator:
    """Validates Kafka messages."""

    REQUIRED_PARSED_MESSAGE_FIELDS = ["aggregatetype", "aggregateid", "type", "payload"]
    VALID_AGGREGATE_TYPES = ["relations"]

    @staticmethod
    def validate_parsed_message(message_value: Dict[str, Any]) -> bool:
        """Validate parsed message structure (after Debezium parsing)."""
        try:
            # Check required fields in the parsed message
            for field in MessageValidator.REQUIRED_PARSED_MESSAGE_FIELDS:
                if field not in message_value:
                    logger.error(f"Missing required field '{field}' in parsed message")
                    validation_errors_total.labels(error_type="missing_field").inc()
                    return False

            # Validate payload is a dict
            payload = message_value.get("payload")
            if not isinstance(payload, dict):
                logger.error("payload must be a dictionary")
                validation_errors_total.labels(error_type="invalid_payload_type").inc()
                return False

            return True

        except Exception as e:
            logger.error(f"Error validating parsed message: {e}")
            validation_errors_total.labels(error_type="validation_exception").inc()
            return False

    @staticmethod
    def validate_replication_message(payload: Dict[str, Any]) -> bool:
        """Validate replication message payload."""
        try:
            # Check that at least one of relations_to_add or relations_to_remove is present
            has_relations_to_add = "relations_to_add" in payload
            has_relations_to_remove = "relations_to_remove" in payload

            if not has_relations_to_add and not has_relations_to_remove:
                logger.error(
                    "Missing required field: at least one of 'relations_to_add' "
                    "or 'relations_to_remove' must be present"
                )
                validation_errors_total.labels(error_type="missing_relations_fields").inc()
                return False

            # Validate relations_to_add is a list if present
            relations_to_add = payload.get("relations_to_add", [])
            if has_relations_to_add and not isinstance(relations_to_add, list):
                logger.error("relations_to_add must be a list")
                validation_errors_total.labels(error_type="invalid_relations_to_add_type").inc()
                return False

            # Validate relations_to_remove is a list if present
            relations_to_remove = payload.get("relations_to_remove", [])
            if has_relations_to_remove and not isinstance(relations_to_remove, list):
                logger.error("relations_to_remove must be a list")
                validation_errors_total.labels(error_type="invalid_relations_to_remove_type").inc()
                return False

            # Validate that at least one list is not empty (meaningful message)
            if not relations_to_add and not relations_to_remove:
                logger.warning("Both relations_to_add and relations_to_remove are empty - this may indicate a bug")
                validation_errors_total.labels(error_type="empty_relations").inc()
                return False

            # Validate structure of relations
            all_relations = list(relations_to_add) + list(relations_to_remove)
            for relation in all_relations:
                if not isinstance(relation, dict):
                    logger.error("Each relation must be a dictionary")
                    validation_errors_total.labels(error_type="invalid_relation_type").inc()
                    return False

                # Basic relation structure validation
                if "resource" not in relation or "subject" not in relation:
                    logger.error("Each relation must have 'resource' and 'subject' fields")
                    validation_errors_total.labels(error_type="invalid_relation_structure").inc()
                    return False

            return True

        except Exception as e:
            logger.error(f"Error validating replication message: {e}")
            validation_errors_total.labels(error_type="replication_validation_exception").inc()
            return False


def _send_pg_notify_best_effort(channel: str, payload: str, description: str) -> None:
    """Send PostgreSQL NOTIFY after successful side effects. Errors are logged only; they do not fail replication."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [channel, payload])
        logger.info("Sent NOTIFY on channel '%s' (%s)", channel, description)
    except Exception as e:
        logger.error("Failed NOTIFY on channel '%s' (%s): %s", channel, description, e)


_CONSISTENCY_TOKEN_LOCK_TIMEOUT_MS = 2_000


class _TokenSaveStatus(enum.Enum):
    SUCCESS = "success"
    LOCK_TIMEOUT = "lock_timeout"
    ERROR = "error"


def _save_consistency_token_best_effort(org_id: str, token: str, aggregateid: str) -> None:
    """Persist the latest relations consistency token on the tenant row.

    This is a best-effort operation: if the row is locked by a long-running
    transaction (e.g. migrate_binding_scope, bootstrap_tenants), we fail fast
    with a short lock_timeout instead of blocking for the session-wide 60s.
    A future message for the same org will overwrite the token anyway, so
    skipping one save is harmless.
    """
    token_save_status = _TokenSaveStatus.SUCCESS
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = %s", [f"{_CONSISTENCY_TOKEN_LOCK_TIMEOUT_MS}ms"])
            rows = Tenant.objects.filter(org_id=org_id).update(relations_consistency_token=token)
        if rows == 0:
            logger.warning("Tenant not found for org_id: %s. Unable to save consistency token.", org_id)
    except OperationalError as e:
        error_str = str(e).lower()
        is_lock_timeout = (
            "lock timeout" in error_str
            or "lock_timeout" in error_str
            or "canceling statement due to lock" in error_str
        )
        if is_lock_timeout:
            token_save_status = _TokenSaveStatus.LOCK_TIMEOUT
            logger.warning(
                "Consistency token save skipped (lock contention) for org_id=%s, "
                "aggregateid=%s. A later message will update the token.",
                org_id,
                aggregateid,
            )
        else:
            token_save_status = _TokenSaveStatus.ERROR
            logger.error("Consistency token save failed for org_id=%s: %s", org_id, e)
    except Exception as e:
        token_save_status = _TokenSaveStatus.ERROR
        logger.error("Consistency token save failed for org_id=%s: %s", org_id, e)
    finally:
        consistency_token_save_total.labels(status=token_save_status.value).inc()


def _consumer_config(topic: str):
    # Check if custom broker is configured for the consumer
    custom_broker = settings.RBAC_KAFKA_CUSTOM_CONSUMER_BROKER

    if custom_broker:
        # Use custom broker configuration
        logger.info(f"Using custom Kafka broker: {custom_broker}")
        kafka_servers = [custom_broker]
        kafka_auth = None  # Don't use auth with custom broker
    else:
        # Use default Clowder/localhost configuration
        kafka_auth = settings.KAFKA_AUTH
        kafka_servers = settings.KAFKA_SERVERS

    common_config = {
        "session_timeout_ms": 45000,
    }

    if kafka_auth:
        # Filter out producer-specific configurations that are not valid for consumers
        consumer_auth = {k: v for k, v in kafka_auth.items() if k not in PRODUCER_ONLY_CONFIGS}
        # Log if any producer-specific configs were filtered out
        filtered_configs = set(kafka_auth.keys()) & PRODUCER_ONLY_CONFIGS
        if filtered_configs:
            logger.info(f"Filtered out producer-specific configs for consumer: {filtered_configs}")

        logger.info(f"Will use auth for Kafka topic: {topic}")
        return {**common_config, **consumer_auth}
    else:
        logger.info(f"Will use servers {kafka_servers} for Kafka topic: {topic}")
        return {**common_config, "bootstrap_servers": kafka_servers}


class RBACKafkaConsumer(BaseRBACKafkaConsumer[None]):
    """RBAC Kafka consumer for processing Debezium and replication messages."""

    def __init__(
        self,
        topic: Optional[str] = None,
        group_id: Optional[str] = None,
        health_check_interval: int = 30,
        retry_config: Optional[RetryConfig] = None,
        commit_config: Optional[CommitConfig] = None,
    ):
        """Initialize the consumer."""
        if not topic:
            topic = settings.RBAC_KAFKA_CONSUMER_TOPIC

        if not group_id:
            group_id = settings.RBAC_KAFKA_CONSUMER_GROUP_ID

        super().__init__(
            topic=topic,
            group_id=group_id,
            health_check_interval=health_check_interval,
            retry_config=retry_config,
            commit_config=commit_config,
            extra_consumer_config=_consumer_config(topic=topic),
            metrics=base_metrics,
        )

        self.validator = MessageValidator()

        # Fencing token state (thread-safe access required)
        self.lock_id: Optional[str] = None
        self.lock_token: Optional[str] = None
        self._lock_mutex = threading.Lock()
        self.lock_acquisition_failed: bool = False  # Track if lock acquisition failed during rebalance

    def _acquire_lock(self, lock_id: str) -> str:
        """Acquire a lock token from the Relations API.

        Args:
            lock_id: Unique identifier for the lock (format: "consumer-group/partition")

        Returns:
            str: The lock token

        Raises:
            grpc.RpcError: If the lock acquisition fails
        """
        return inventory_api_replication.acquire_lock(lock_id)

    def _acquire_lock_with_retry(
        self,
        lock_id: str,
        max_retries: Optional[int] = None,
        backoff_base: Optional[float] = None,
        max_backoff: Optional[float] = None,
    ) -> str:
        """Acquire lock with retry logic.

        Args:
            lock_id: Unique identifier for the lock
            max_retries: Maximum number of retry attempts (default: from env or 5)
            backoff_base: Base for exponential backoff in seconds (default: 2)
            max_backoff: Maximum backoff delay in seconds (default: 30)

        Returns:
            str: The acquired lock token

        Raises:
            RuntimeError: If max retries exceeded
            grpc.RpcError: If lock acquisition fails permanently
        """
        # Use configurable defaults from environment or fallback to safe values
        if max_retries is None:
            max_retries = getattr(settings, "RBAC_KAFKA_LOCK_ACQUISITION_MAX_RETRIES", 5)
        if backoff_base is None:
            backoff_base = getattr(settings, "RBAC_KAFKA_LOCK_ACQUISITION_BACKOFF_BASE", 2.0)
        if max_backoff is None:
            max_backoff = getattr(settings, "RBAC_KAFKA_LOCK_ACQUISITION_MAX_BACKOFF", 30.0)

        for attempt in range(max_retries):
            try:
                return self._acquire_lock(lock_id)
            except grpc.RpcError as e:
                logger.warning(f"Lock acquisition attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"Max retries ({max_retries}) exceeded for lock acquisition")
                    raise RuntimeError(f"Failed to acquire lock after {max_retries} attempts") from e

                # Exponential backoff with max cap
                delay = min(backoff_base**attempt, max_backoff)
                logger.info(f"Retrying lock acquisition in {delay:.1f}s...")
                time.sleep(delay)

        raise RuntimeError(f"Failed to acquire lock after {max_retries} attempts")

    def _do_handle_partitions_revoked(self, revoked_partitions: list[TopicPartition]):
        # Clear lock token since we no longer own the partition
        with self._lock_mutex:
            if self.lock_token:
                logger.info(f"Clearing lock token for {self.lock_id} due to partition revocation")
                self.lock_token = None
                self.lock_id = None

    def _do_handle_partitions_assigned(self, assigned_partitions: list[TopicPartition]):
        # Acquire lock token for assigned partitions
        if len(assigned_partitions) > 0:
            # Typically only one partition per consumer
            partition = assigned_partitions[0]

            # Generate lock ID: {consumer_group_id}/{partition_number}
            lock_id = f"{self.group_id}/{partition.partition}"

            logger.info(f"Attempting to acquire lock token for {lock_id}")

            # Track lock acquisition timing
            start_time = time.time()

            try:
                # This runs in a worker thread, so it won't block the consumer IO / heartbeat loop.
                lock_token = self._acquire_lock_with_retry(lock_id)

                # Record successful acquisition
                duration = time.time() - start_time
                lock_acquisition_duration.labels(status="success").observe(duration)
                lock_acquisition_total.labels(status="success", reason="rebalance").inc()

                logger.info(f"Successfully acquired lock token: {lock_token} (took {duration:.2f}s)")

                # Store lock token in consumer instance (thread-safe)
                with self._lock_mutex:
                    self.lock_id = lock_id
                    self.lock_token = lock_token
                    # Reset failure flag on successful acquisition
                    self.lock_acquisition_failed = False

                logger.info(f"Acquired and stored lock token for partition {partition.partition}: {lock_token}")

            except Exception as e:
                # Record failed acquisition
                duration = time.time() - start_time
                lock_acquisition_duration.labels(status="failure").observe(duration)

                # Classify error type for metrics
                error_type = type(e).__name__
                lock_acquisition_total.labels(status="failure", reason=error_type).inc()

                # Log with full stack trace for debugging
                logger.error(f"Failed to acquire lock token for {lock_id}: {e} (took {duration:.2f}s)", exc_info=True)
                logger.critical(
                    f"CRITICAL: Cannot proceed without lock token for partition {partition.partition}. "
                    f"Consumer must stop to prevent processing without fencing protection."
                )

                # Clear any partial state
                with self._lock_mutex:
                    self.lock_token = None
                    self.lock_id = None

                # Mark consumer as unhealthy to trigger pod restart
                self._update_health_status(False)

                # DON'T re-raise - kafka-python may suppress the exception
                # Instead, set a flag that will be checked before processing messages
                self.lock_acquisition_failed = True
        else:
            logger.warning("_do_handle_partitions_assigned called with empty partition list")

    def _prepare_for_message(self, message, first_message: bool):
        # Check if lock acquisition failed during rebalance
        if self.lock_acquisition_failed:
            error_msg = (
                "Lock acquisition failed during rebalance. Cannot process messages without fencing token. "
                "Stopping consumer to prevent data corruption."
            )
            logger.critical(error_msg)
            raise RuntimeError(error_msg)

        # On first message, ensure we have a lock token
        # This handles cases where on_partitions_assigned doesn't fire
        if first_message:
            token_acquired = self._ensure_lock_token_on_assignment()
            if not token_acquired:
                # Partitions not assigned yet - this should not happen since we have a message
                # This is a fatal error - we cannot process without partition assignment
                error_msg = (
                    f"Received message but no partitions assigned. "
                    f"Message partition: {message.partition}, offset: {message.offset}. "
                    f"This indicates a Kafka consumer state issue. "
                    f"Cannot proceed without partition assignment - stopping consumer."
                )
                logger.error(error_msg)
                # Raise error to stop consumer - at-least-once delivery will be preserved
                # because offset was not committed. Message will be retried on restart.
                raise RuntimeError(error_msg)

    def _process_single(self, message: ConsumerRecord, batch_data: None) -> bool:
        """Process a single message (parse and handle).

        This is the core message processing logic without retry concerns.

        Args:
            message: The Kafka message to process

        Returns:
            bool: True if message processed successfully

        Raises:
            ValidationError: If message validation fails
            Other exceptions: If processing fails
        """
        message_value = self._parse_message_value(message)
        logger.debug(
            f"Parsed message content (partition: {message.partition}, offset: {message.offset}): {message_value}"
        )

        # Parse Debezium message (may raise ValidationError or JSONDecodeError)
        parsed_message = self._parse_debezium_message(message_value)

        # Process the message (may raise ValidationError or other exceptions)
        return self._process_debezium_message(parsed_message, message.partition, message.offset)

    def _parse_debezium_message(self, message_value: Dict[str, Any]) -> Dict[str, Any]:
        """Parse standard Debezium message format with schema/payload wrapper.

        Standard Debezium messages come in this format:
        {
            "schema": {...},
            "payload": "JSON_STRING_CONTAINING_ACTUAL_DATA"
        }

        This method extracts and parses the payload to get the actual business data.

        Raises:
            ValidationError: If message format is invalid
        """
        try:
            # Only accept standard Debezium message format with schema and payload
            if "schema" not in message_value or "payload" not in message_value:
                error_msg = (
                    f"Message is not in standard Debezium format. "
                    f"Expected 'schema' and 'payload' fields. Got: {list(message_value.keys())}"
                )
                logger.error(error_msg)
                raise ValidationError(error_msg)

            payload_str = message_value.get("payload")

            # Parse payload to dict if it's a string
            if isinstance(payload_str, str):
                try:
                    payload_data = json.loads(payload_str)
                    logger.debug(f"Parsed Debezium payload: {payload_data}")
                except json.JSONDecodeError as e:
                    error_msg = f"Failed to parse Debezium payload JSON: {e}, payload: {payload_str}"
                    logger.error(error_msg)
                    # JSONDecodeError will be caught by default error handler
                    raise
            elif isinstance(payload_str, dict):
                # Payload is already parsed as dict
                payload_data = payload_str
                logger.debug(f"Debezium payload already parsed: {payload_data}")
            else:
                error_msg = f"Debezium payload must be a string or dict, got: {type(payload_str)}"
                logger.error(error_msg)
                raise ValidationError(error_msg)

            # Validate payload structure - common logic for both string and dict payloads
            if "relations_to_add" in payload_data or "relations_to_remove" in payload_data:
                # Extract aggregatetype and aggregateid from the event if available
                return {
                    "aggregatetype": payload_data.get("aggregatetype", ""),
                    "aggregateid": payload_data.get("aggregateid", ""),
                    "type": payload_data.get("type", ""),
                    "payload": payload_data,
                }
            else:
                error_msg = (
                    f"Unknown payload structure in Debezium message. "
                    f"Expected 'relations_to_add' or 'relations_to_remove'. "
                    f"Got: {list(payload_data.keys())}"
                )
                logger.error(error_msg)
                raise ValidationError(error_msg)

        except ValidationError:
            # Re-raise ValidationError - will NOT be retried (non-retryable)
            raise
        except json.JSONDecodeError:
            # Re-raise JSONDecodeError to be handled by error handler
            raise
        except Exception as e:
            logger.error(f"Error parsing Debezium message: {e}")
            # Re-raise other exceptions to be handled by retry logic
            raise

    def _process_debezium_message(
        self, message_value: Dict[str, Any], message_partition: int, message_offset: int
    ) -> bool:
        """Process a Debezium message."""
        try:
            # Create structured message
            # Note: message structure is already validated by _parse_debezium_message
            debezium_msg = DebeziumMessage.from_kafka_message(message_value)

            # Process all messages with relations - no strict aggregate type checking
            return self._process_relations_message(debezium_msg, message_partition, message_offset)

        except ValidationError:
            # Re-raise ValidationError - will NOT be retried (non-retryable)
            raise
        except Exception as e:
            logger.error(
                f"Error processing Debezium message (partition: {message_partition}, offset: {message_offset}): {e}"
            )
            self.metrics.messages_processed_total.labels(message_type="debezium", status="error").inc()
            # Re-raise to allow retry logic to handle
            raise

    def _process_relations_message(
        self, debezium_msg: DebeziumMessage, message_partition: int, message_offset: int
    ) -> bool:
        """Process a relations Debezium message."""
        processing_start = time.time()
        try:
            # Validate replication payload
            if not self.validator.validate_replication_message(debezium_msg.payload):
                logger.error(
                    f"Replication message validation failed "
                    f"(partition: {message_partition}, offset: {message_offset}). "
                    f"Payload content: {debezium_msg.payload}"
                )
                self.metrics.messages_processed_total.labels(
                    message_type="relations", status="validation_failed"
                ).inc()
                raise ValidationError(
                    f"Replication message validation failed for aggregateid: {debezium_msg.aggregateid}"
                )

            resource_context = debezium_msg.payload.get("resource_context")
            org_id = None
            event_type = None
            resource_id = None
            created_at = None

            # Extract org_id, event_type, resource_id, and created_at from resource_context if present
            if resource_context and isinstance(resource_context, dict):
                org_id = resource_context.get("org_id")
                event_type = resource_context.get("event_type")
                resource_id = resource_context.get("resource_id")
                created_at = resource_context.get("created_at")
            else:
                logger.debug(
                    f"No resource_context found, skipping org_id and event_type extraction. "
                    f"aggregateid: {debezium_msg.aggregateid}, "
                    f"partition: {message_partition}, offset: {message_offset}"
                )

            # Create structured replication message
            replication_msg = ReplicationMessage.from_payload(debezium_msg.payload)

            logger.info(
                f"Processing relations message - org_id: {org_id}, "
                f"event_type: {event_type}, "
                f"relations_to_add: {len(replication_msg.relations_to_add)}, "
                f"relations_to_remove: {len(replication_msg.relations_to_remove)}, "
                f"partition: {message_partition}, offset: {message_offset}"
            )

            # Convert JSON dictionaries to protobuf objects
            relations_to_add_pb = []
            for relation_dict in replication_msg.relations_to_add:
                relation_pb = json_format.ParseDict(relation_dict, relationship_pb2.Relationship())
                relations_to_add_pb.append(relation_pb)

            relations_to_remove_pb = []
            for relation_dict in replication_msg.relations_to_remove:
                relation_pb = json_format.ParseDict(relation_dict, relationship_pb2.Relationship())
                relations_to_remove_pb.append(relation_pb)

            # Build fencing check with lock token (thread-safe read)
            # Note: Lock token should be available because _run_message_loop calls
            # _ensure_lock_token_on_assignment before processing the first message.
            # However, if that acquisition failed or token was cleared, we fail fast here.
            fencing_check = None
            with self._lock_mutex:
                if self.lock_id and self.lock_token:
                    from kessel.inventory.v1beta2 import relation_fencing_check_pb2

                    fencing_check = relation_fencing_check_pb2.RelationFencingCheck(
                        lock_id=self.lock_id,
                        lock_token=self.lock_token,
                    )
                    logger.debug(
                        f"Using fencing check - lock_id: {self.lock_id}, " f"lock_token: {self.lock_token[:8]}..."
                    )
                else:
                    # Lock token not available - fail fast to prevent writes without fencing
                    error_msg = (
                        "Lock token not available during message processing. "
                        "This indicates partition assignment failed or token was cleared. "
                        "Cannot process message without fencing token."
                    )
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

            # Do tuple deletes for relationships with fencing check
            delete_start = time.time()
            replication_delete_response = inventory_api_replication.delete_relationships(
                relationships=relations_to_remove_pb, fencing_check=fencing_check
            )
            delete_duration = time.time() - delete_start

            # Do tuple writes for relationships with fencing check
            add_start = time.time()
            replication_add_response = inventory_api_replication.write_relationships(
                relationships=relations_to_add_pb, fencing_check=fencing_check
            )
            add_duration = time.time() - add_start

            # Extract consistency token from responses
            token = getattr(replication_add_response.consistency_token, "token", None) or getattr(
                replication_delete_response.consistency_token, "token", None
            )

            normalized_org_id = org_id.strip() if isinstance(org_id, str) else org_id
            is_global_system_role_event = event_type in {
                "create_system_role",
                "update_system_role",
                "delete_system_role",
            } and normalized_org_id in (None, "", "None")

            if token and normalized_org_id and not is_global_system_role_event:
                _save_consistency_token_best_effort(normalized_org_id, token, debezium_msg.aggregateid)
            elif not is_global_system_role_event:
                logger.warning(
                    f"No consistency token in either write or delete response - "
                    f"org_id: {org_id}, "
                    f"aggregateid: {debezium_msg.aggregateid}"
                )

            # Send NOTIFY for workspace creation events (Read-Your-Writes support)
            if event_type == "create_workspace" and resource_id:
                logger.info(
                    "Workspace create event processed - org_id=%s, workspace_id=%s, consistency_token=%s",
                    org_id,
                    resource_id,
                    token,
                )
                _send_pg_notify_best_effort(
                    settings.READ_YOUR_WRITES_CHANNEL,
                    resource_id,
                    f"read-your-writes after workspace create (workspace_id={resource_id})",
                )

            notify_migration_batch_completion(
                event_type,
                resource_context,
                lambda channel, payload, context: _send_pg_notify_best_effort(channel, payload, context),
            )

            # Calculate processing duration and replication latency
            processing_duration_seconds = time.time() - processing_start
            latency_event_type = event_type or "unknown"

            # Record Kessel write duration metrics
            kessel_write_duration.labels(operation="delete", event_type=latency_event_type).observe(delete_duration)
            kessel_write_duration.labels(operation="add", event_type=latency_event_type).observe(add_duration)

            # Record processing duration metric with event_type label
            message_processing_duration.labels(message_type="debezium", event_type=latency_event_type).observe(
                processing_duration_seconds
            )

            # Calculate and emit replication latency metric
            latency_seconds = None
            if created_at is not None:
                try:
                    latency_seconds = time.time() - float(created_at)
                    replication_event_latency.labels(event_type=latency_event_type).observe(latency_seconds)
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"Could not calculate replication latency: invalid created_at value "
                        f"'{created_at}': {e} "
                        f"(partition: {message_partition}, offset: {message_offset})"
                    )

            logger.info(
                f"Message processed successfully "
                f"(partition: {message_partition}, offset: {message_offset}, "
                f"event_type: {latency_event_type}, "
                f"kessel_delete_duration: {delete_duration:.3f}s, "
                f"kessel_add_duration: {add_duration:.3f}s, "
                f"processing_duration: {processing_duration_seconds:.3f}s, "
                f"replication_event_latency: {f'{latency_seconds:.3f}s' if latency_seconds is not None else 'N/A'})"
            )

            self.metrics.messages_processed_total.labels(message_type="relations", status="success").inc()
            return True

        except ValidationError:
            # Re-raise ValidationError - will NOT be retried (non-retryable)
            raise
        except grpc.RpcError as e:
            # Handle gRPC errors specially to check for invalid fencing tokens
            if e.code() == grpc.StatusCode.FAILED_PRECONDITION:
                error_msg = (
                    f"Fencing token validation failed - partition reassigned. "
                    f"Lock ID: {self.lock_id}, Token: {self.lock_token}. "
                    f"Consumer will stop processing to prevent stale updates. "
                    f"(partition: {message_partition}, offset: {message_offset})"
                )
                logger.error(error_msg)
                self.metrics.messages_processed_total.labels(message_type="relations", status="fencing_failed").inc()
                raise RuntimeError(error_msg) from e
            else:
                logger.error(
                    f"gRPC error processing relations message: {e.code()}: {e.details()} "
                    f"(partition: {message_partition}, offset: {message_offset})"
                )
                self.metrics.messages_processed_total.labels(message_type="relations", status="grpc_error").inc()
                raise
        except Exception as e:
            logger.error(
                f"Error processing relations message: {e} "
                f"(partition: {message_partition}, offset: {message_offset})"
            )
            self.metrics.messages_processed_total.labels(message_type="relations", status="error").inc()
            # Re-raise to trigger retry logic
            raise

    def _parse_message_value(self, message):
        """Parse JSON from raw Kafka message bytes.

        Args:
            message: Kafka message

        Returns:
            dict: Parsed JSON message

        Raises:
            json.JSONDecodeError, UnicodeDecodeError: If parsing fails
        """
        try:
            return json.loads(message.value.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # Fail fast on JSON parse errors
            raw_content = message.value[:100] if len(message.value) > 100 else message.value
            error_msg = (
                f"Failed to parse JSON from message at partition {message.partition}, "
                f"offset {message.offset}: {e}. "
                f"Sample content: {raw_content}. "
                f"This indicates a malformed message in the Kafka topic. "
                f"Fix the producer or manually skip the offset, then restart the consumer."
            )
            logger.error(error_msg)
            self.metrics.messages_processed_total.labels(message_type="unknown", status="json_error").inc()
            raise

    def _ensure_lock_token_on_assignment(self):
        """Ensure lock token is acquired when partitions are assigned.

        This handles the case where on_partitions_assigned callback might not fire
        on consumer restart if partition assignment hasn't changed.
        """
        assigned = self.consumer.assignment()
        if not assigned:
            return False

        # Get the expected lock_id for the currently assigned partition
        # Note: We assume single partition per consumer - assert this invariant
        if len(assigned) > 1:
            logger.warning(
                f"Consumer has multiple partitions assigned: {assigned}. "
                f"RBAC consumer is designed for single partition per consumer. "
                f"Using first partition only."
            )

        partition = list(assigned)[0]
        expected_lock_id = f"{self.group_id}/{partition.partition}"

        # Check if we already have a lock token for the correct partition
        with self._lock_mutex:
            if self.lock_token and self.lock_id == expected_lock_id:
                # Token exists and matches current assignment
                logger.debug(f"Lock token already valid for {self.lock_id}")
                return True
            elif self.lock_token:
                # Token exists but for wrong partition - clear stale token
                logger.warning(
                    f"Stale lock token detected. Current: {self.lock_id}, "
                    f"Expected: {expected_lock_id}. Clearing and reacquiring."
                )
                self.lock_token = None
                self.lock_id = None

        # Acquire lock token for assigned partition
        lock_id = expected_lock_id

        logger.info(f"Acquiring lock token for assigned partition: {lock_id}")

        # Track lock acquisition timing
        start_time = time.time()

        try:
            lock_token = self._acquire_lock_with_retry(lock_id)

            # Record successful acquisition
            duration = time.time() - start_time
            lock_acquisition_duration.labels(status="success").observe(duration)
            lock_acquisition_total.labels(status="success", reason="startup").inc()

            with self._lock_mutex:
                self.lock_id = lock_id
                self.lock_token = lock_token
                # Reset failure flag on successful acquisition
                self.lock_acquisition_failed = False

            logger.info(
                f"Acquired lock token for partition {partition.partition}: {lock_token} (took {duration:.2f}s)"
            )
            return True
        except Exception as e:
            # Record failed acquisition
            duration = time.time() - start_time
            lock_acquisition_duration.labels(status="failure").observe(duration)

            # Classify error type for metrics
            error_type = type(e).__name__
            lock_acquisition_total.labels(status="failure", reason=error_type).inc()

            logger.error(f"Failed to acquire lock token for {lock_id}: {e} (took {duration:.2f}s)")
            raise RuntimeError(f"Failed to acquire lock token for partition {partition.partition}") from e
