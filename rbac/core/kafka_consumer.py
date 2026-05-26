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

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import grpc
from django.conf import settings
from django.db import connection
from google.protobuf import json_format
from internal.pg_notify_wait import REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_CHANNEL
from kafka import KafkaConsumer, TopicPartition
from kafka.consumer.subscription_state import ConsumerRebalanceListener
from kafka.errors import KafkaError
from kafka.structs import OffsetAndMetadata
from kessel.relations.v1beta1 import common_pb2
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.relation_replicator.relations_api_replicator import (
    RelationsApiReplicator,
)
from prometheus_client import Counter, Gauge, Histogram

from api.models import Tenant

relations_api_replication = RelationsApiReplicator()

logger = logging.getLogger("rbac.core.kafka_consumer")

# Metrics
consumer_start_time = Gauge(
    "rbac_kafka_consumer_start_time_seconds",
    "Unix timestamp when the consumer started",
)

consumer_info = Gauge(
    "rbac_kafka_consumer_info",
    "Consumer running state: 1=running, 0=stopped",
    ["topic", "group_id"],
)

messages_processed_total = Counter(
    "rbac_kafka_consumer_messages_processed_total",
    "Total number of messages processed",
    ["message_type", "status"],
)

message_processing_duration = Histogram(
    "rbac_kafka_consumer_message_processing_duration_seconds",
    "Time spent processing messages",
    ["message_type", "event_type"],
)

kessel_write_duration = Histogram(
    "rbac_kessel_write_duration_seconds",
    "Time spent on Kessel Relations API write and delete calls",
    ["operation", "event_type"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

validation_errors_total = Counter(
    "rbac_kafka_consumer_validation_errors_total",
    "Total number of validation errors",
    ["error_type"],
)

retry_attempts_total = Counter(
    "rbac_kafka_consumer_retry_attempts_total",
    "Total number of message retry attempts",
    ["retry_reason", "attempt_number"],
)

message_retry_duration = Histogram(
    "rbac_kafka_consumer_message_retry_duration_seconds",
    "Time spent retrying message processing",
    ["retry_reason"],
)

rebalance_events_total = Counter(
    "rbac_kafka_consumer_rebalance_events_total",
    "Total number of rebalance events",
    ["event_type"],  # partitions_assigned, partitions_revoked
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


@dataclass
class RetryConfig:
    """Configuration for retry logic.

    Implements exponential backoff with the formula:
    backoff = min(backoff_factor * attempts * base_delay, max_backoff_seconds)
    """

    operation_max_retries: int = 10  # Max operation retry attempts (-1 = infinite)
    backoff_factor: int = 5  # Exponential backoff multiplier
    max_backoff_seconds: int = 30  # Maximum wait time between retries
    base_delay: float = 0.3  # Base delay in seconds (300ms)
    jitter_factor: float = 0.1  # Random jitter to avoid thundering herd

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for retry attempt with exponential backoff and jitter.

        Formula: backoff = min(backoff_factor * attempts * base_delay, max_backoff_seconds)

        Example with defaults (backoff_factor=5, base_delay=0.3s, max=30s):
        - Attempt 1: 1.5s (5 * 1 * 0.3)
        - Attempt 2: 3.0s (5 * 2 * 0.3)
        - Attempt 3: 4.5s (5 * 3 * 0.3)
        - Attempt 10+: 30s (capped)
        """
        # Calculate exponential backoff: backoff_factor * (attempt+1) * base_delay
        # Note: attempt starts at 0, so we add 1 to get proper delay on first retry
        delay = self.backoff_factor * (attempt + 1) * self.base_delay
        delay = min(delay, self.max_backoff_seconds)

        # Add jitter to avoid thundering herd problem
        jitter = delay * self.jitter_factor * random.random()
        return delay + jitter


@dataclass
class CommitConfig:
    """Configuration for offset commit policy."""

    commit_modulo: int = 10  # Commit every N messages (batch commits)
    commit_on_rebalance: bool = True  # Commit offsets on rebalance
    commit_on_shutdown: bool = True  # Commit offsets on shutdown


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


class ValidationError(Exception):
    """Raised when message validation fails permanently (non-retryable)."""

    pass


class RetryHelper:
    """Handles retry logic with exponential backoff for message processing.

    This class encapsulates all retry policy and logic, making it easy to test
    and maintain separately from the business logic.
    """

    def __init__(
        self,
        retry_config: RetryConfig,
        shutdown_event: threading.Event,
        error_handler=None,
    ):
        """Initialize the retry helper.

        Args:
            retry_config: Configuration for retry behavior
            shutdown_event: Event to signal shutdown (interrupts retries)
            error_handler: Optional callable(Exception) -> bool to short-circuit retries
        """
        self.retry_config = retry_config
        self.shutdown_event = shutdown_event
        self.error_handler = error_handler

    def run(self, fn, *args, **kwargs):
        """Execute a function with retry logic.

        Args:
            fn: The function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            The return value from the successful function execution

        Raises:
            The final exception if all retries are exhausted or shutdown occurs
        """
        attempt = 0
        start_time = time.time()

        while True:
            try:
                result = fn(*args, **kwargs)
                if attempt > 0:
                    total_duration = time.time() - start_time
                    logger.info(
                        f"Operation successful after {attempt + 1} attempts "
                        f"(total retry time: {total_duration:.2f}s)"
                    )
                    message_retry_duration.labels(retry_reason="processing_error").observe(total_duration)
                return result

            except Exception as e:
                # Check if error handler wants to short-circuit retry
                if not self._should_retry(e):
                    logger.warning(f"Error handler short-circuited retry: {e}. " f"Operation will be skipped.")
                    raise

                # Check if we've hit max retries (if configured)
                if self._exceeded_max_retries(attempt):
                    error_msg = f"Max operation retries ({self.retry_config.operation_max_retries}) " f"exceeded: {e}"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg) from e

                # Determine error type for logging
                error_type = type(e).__name__
                retry_reason = self._classify_error(e)

                logger.warning(f"Error on attempt {attempt + 1}: {error_type}: {e}")

                # Record retry attempt
                retry_attempts_total.labels(
                    retry_reason=retry_reason,
                    attempt_number=min(attempt + 1, 10),  # Cap at 10 for cardinality
                ).inc()

                # Calculate delay and wait before retry
                delay = self.retry_config.calculate_delay(attempt)
                logger.info(f"Retrying in {delay:.2f}s (attempt {attempt + 1})")

                # Sleep with ability to interrupt for shutdown
                if self.shutdown_event.wait(delay):
                    logger.info("Retry interrupted by shutdown signal")
                    raise InterruptedError("Shutdown signal received during retry")

                attempt += 1

                # Log periodic status for long-running retries
                if attempt % 10 == 0:
                    elapsed = time.time() - start_time
                    logger.warning(f"Operation still retrying after {attempt} attempts " f"(elapsed: {elapsed:.2f}s)")

    def _should_retry(self, exception: Exception) -> bool:
        """Determine if an exception should trigger a retry.

        Args:
            exception: The exception to evaluate

        Returns:
            bool: True if should retry, False if should skip retry
        """
        # Allow custom error handler to short-circuit retry logic
        if self.error_handler and callable(self.error_handler):
            if self.error_handler(exception):
                return False

        # Retry on ALL exceptions - we want to ensure at-least-once delivery
        return True

    def _exceeded_max_retries(self, attempt: int) -> bool:
        """Check if max retries have been exceeded.

        Args:
            attempt: The current attempt number (0-indexed)

        Returns:
            bool: True if max retries exceeded
        """
        max_retries = self.retry_config.operation_max_retries
        return 0 <= max_retries <= attempt

    def _classify_error(self, exception: Exception) -> str:
        """Classify error type for metrics.

        Args:
            exception: The exception to classify

        Returns:
            str: Error classification for metrics
        """
        if isinstance(exception, (json.JSONDecodeError, UnicodeDecodeError)):
            return "json_error"
        elif isinstance(exception, (ConnectionError, TimeoutError, OSError)):
            return "network_error"
        elif isinstance(exception, ValueError):
            return "validation_error"
        else:
            return "error"


class OffsetManager:
    """Manages Kafka offset storage and batch commits.

    This class encapsulates all offset management logic, making the main
    consumer loop cleaner and easier to understand.
    """

    def __init__(self, consumer: KafkaConsumer, commit_config: CommitConfig):
        """Initialize the offset manager.

        Args:
            consumer: The Kafka consumer instance
            commit_config: Configuration for commit behavior
        """
        self.consumer = consumer
        self.commit_config = commit_config
        # Store tuples of (offset, leader_epoch) for each partition
        self.stored_offsets: Dict[TopicPartition, tuple] = {}
        self.offset_mutex = threading.Lock()

    def store(
        self,
        topic_partition: TopicPartition,
        offset: int,
        leader_epoch: Optional[int] = None,
    ):
        """Store offset and leader_epoch for later batch commit (thread-safe).

        Args:
            topic_partition: The topic partition
            offset: The message offset
            leader_epoch: The leader epoch for the partition (optional)
        """
        with self.offset_mutex:
            self.stored_offsets[topic_partition] = (offset, leader_epoch)
            logger.debug(
                f"Stored offset {offset} (leader_epoch={leader_epoch}) " f"for partition {topic_partition.partition}"
            )

    def should_commit(self, offset: int) -> bool:
        """Check if offset should trigger a batch commit.

        Args:
            offset: The current message offset

        Returns:
            bool: True if offset should be committed now
        """
        if self.commit_config.commit_modulo <= 0:
            return False  # Disabled

        return (offset + 1) % self.commit_config.commit_modulo == 0

    def commit(self) -> tuple[bool, int]:
        """Commit all stored offsets to Kafka (thread-safe).

        Returns:
            tuple[bool, int]: (success, count) where success is True if commit succeeded,
                              and count is the number of offsets committed (0 if none)
        """
        if not self.consumer:
            logger.warning("Cannot commit offsets: consumer not initialized")
            return False, 0

        # Create a copy of offsets to avoid holding the lock during commit
        with self.offset_mutex:
            if not self.stored_offsets:
                logger.debug("No stored offsets to commit")
                return True, 0  # Success, but nothing to commit

            offsets_to_commit = self.stored_offsets.copy()

        # Initialize offset_dict before try block to avoid scoping issues
        offset_dict = None

        try:
            # Commit the copied offsets
            # Note: Kafka expects offset+1 for the next message to consume
            # kafka-python requires OffsetAndMetadata objects with leader_epoch
            offset_dict = {
                tp: OffsetAndMetadata(offset + 1, None, leader_epoch)
                for tp, (offset, leader_epoch) in offsets_to_commit.items()
            }

            # Verify all partitions are currently assigned before committing
            assigned_partitions = self.consumer.assignment()
            unassigned_partitions = set(offset_dict.keys()) - assigned_partitions
            if unassigned_partitions:
                logger.warning(
                    f"Attempting to commit offsets for unassigned partitions: {unassigned_partitions}. "
                    f"Currently assigned: {assigned_partitions}. Skipping unassigned partitions."
                )
                # Filter out unassigned partitions
                offset_dict = {tp: om for tp, om in offset_dict.items() if tp in assigned_partitions}

                if not offset_dict:
                    logger.warning("No assigned partitions to commit after filtering")
                    return False, 0

            count = len(offset_dict)
            logger.info(f"Committing {count} offset(s) to Kafka: {offset_dict}")
            self.consumer.commit(offsets=offset_dict)
            logger.info(f"Successfully committed {count} offset(s)")

            # Clear stored offsets after successful commit
            with self.offset_mutex:
                self.stored_offsets.clear()

            return True, count

        except Exception as e:
            # Build error message with safe access to offset_dict
            error_details = f"Failed to commit offsets: {type(e).__name__}: {e}."
            if offset_dict is not None:
                error_details += f" Attempted to commit {len(offset_dict)} offset(s): {offset_dict}."
            else:
                error_details += f" Failed before creating offset_dict. Offsets to commit: {offsets_to_commit}."

            error_details += (
                f" Consumer state: group_id={self.consumer.config.get('group_id')}, "
                f"bootstrap_servers={self.consumer.config.get('bootstrap_servers')}, "
                f"assigned_partitions={self.consumer.assignment() if self.consumer else 'N/A'}"
            )
            logger.error(error_details)

            # On commit failure, restore offsets back to storage for next attempt
            with self.offset_mutex:
                for tp, (offset, leader_epoch) in offsets_to_commit.items():
                    # Only restore if not updated by another thread
                    # Compare by offset only (first element of tuple)
                    if tp not in self.stored_offsets or self.stored_offsets[tp][0] <= offset:
                        self.stored_offsets[tp] = (offset, leader_epoch)

            return False, 0

    def clear(self):
        """Clear all stored offsets (thread-safe)."""
        with self.offset_mutex:
            self.stored_offsets.clear()


class RebalanceListener(ConsumerRebalanceListener):
    """Listen for Kafka consumer rebalance events.

    Inherits from ConsumerRebalanceListener to properly integrate with kafka-python.
    """

    def __init__(self, consumer_instance):
        """Initialize the rebalance listener.

        Args:
            consumer_instance: The RBACKafkaConsumer instance
        """
        self.consumer_instance = consumer_instance

    def on_partitions_revoked(self, revoked):
        """Handle partition revocation during rebalance.

        Args:
            revoked: List of TopicPartition objects being revoked
        """
        self.consumer_instance._on_partitions_revoked(revoked)

    def on_partitions_assigned(self, assigned):
        """Handle partition assignment during rebalance.

        Args:
            assigned: List of TopicPartition objects being assigned
        """
        import time

        # Track rebalance event
        rebalance_events_total.labels(event_type="partitions_assigned").inc()

        logger.info(f"Partitions assigned: {assigned}")

        # Acquire lock token for assigned partitions
        if len(assigned) > 0:
            # Typically only one partition per consumer
            # Note: assigned is a set, so we need to convert to list to access by index
            partition = list(assigned)[0]

            # Get consumer group ID from the consumer instance
            try:
                consumer_group_id = self.consumer_instance.consumer.config.get("group_id")
                if not consumer_group_id:
                    logger.error("Consumer group_id is not set in consumer config!")
                    consumer_group_id = "unknown"
            except AttributeError as e:
                logger.error(f"Failed to get consumer group_id - consumer may not be initialized: {e}")
                self.consumer_instance.lock_acquisition_failed = True
                return

            # Generate lock ID: {consumer_group_id}/{partition_number}
            lock_id = f"{consumer_group_id}/{partition.partition}"

            logger.info(f"Attempting to acquire lock token for {lock_id}")

            # Track lock acquisition timing
            start_time = time.time()

            try:
                # Acquire lock token from Relations API
                lock_token = self.consumer_instance._acquire_lock_with_retry(lock_id)

                # Record successful acquisition
                duration = time.time() - start_time
                lock_acquisition_duration.labels(status="success").observe(duration)
                lock_acquisition_total.labels(status="success", reason="rebalance").inc()

                logger.info(f"Successfully acquired lock token: {lock_token} (took {duration:.2f}s)")

                # Store lock token in consumer instance (thread-safe)
                with self.consumer_instance._lock_mutex:
                    self.consumer_instance.lock_id = lock_id
                    self.consumer_instance.lock_token = lock_token
                    # Reset failure flag on successful acquisition
                    self.consumer_instance.lock_acquisition_failed = False

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
                with self.consumer_instance._lock_mutex:
                    self.consumer_instance.lock_token = None
                    self.consumer_instance.lock_id = None

                # Mark consumer as unhealthy to trigger pod restart
                self.consumer_instance._update_health_status(False)

                # DON'T re-raise - kafka-python may suppress the exception
                # Instead, set a flag that will be checked before processing messages
                self.consumer_instance.lock_acquisition_failed = True
        else:
            logger.warning("on_partitions_assigned called with empty partition list")


def _send_pg_notify_best_effort(channel: str, payload: str, description: str) -> None:
    """Send PostgreSQL NOTIFY after successful side effects. Errors are logged only; they do not fail replication."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [channel, payload])
        logger.info("Sent NOTIFY on channel '%s' (%s)", channel, description)
    except Exception as e:
        logger.error("Failed NOTIFY on channel '%s' (%s): %s", channel, description, e)


class RBACKafkaConsumer:
    """RBAC Kafka consumer for processing Debezium and replication messages."""

    def __init__(
        self,
        topic: Optional[str] = None,
        health_check_interval: int = 30,
        retry_config: Optional[RetryConfig] = None,
        commit_config: Optional[CommitConfig] = None,
    ):
        """Initialize the consumer."""
        self.topic = topic or settings.RBAC_KAFKA_CONSUMER_TOPIC
        self.consumer: Optional[KafkaConsumer] = None
        self.validator = MessageValidator()
        self.retry_config = retry_config or RetryConfig()
        self.commit_config = commit_config or CommitConfig()
        self.liveness_file = Path("/tmp/kubernetes-liveness")
        self.readiness_file = Path("/tmp/kubernetes-readiness")
        self.is_healthy = False
        self.is_consuming = False
        self.is_paused_for_retry = False  # Track if consumer is paused due to max retries
        self.health_check_interval = health_check_interval
        self.health_check_thread: Optional[threading.Thread] = None
        self._stop_health_check = threading.Event()
        self.last_activity = time.time()
        self._shutdown_in_progress = False

        # Offset manager (will be initialized when consumer is created)
        self.offset_manager: Optional[OffsetManager] = None

        # Fencing token state (thread-safe access required)
        self.lock_id: Optional[str] = None
        self.lock_token: Optional[str] = None
        self._lock_mutex = threading.Lock()
        self.lock_acquisition_failed: bool = False  # Track if lock acquisition failed during rebalance

    def _create_consumer(self) -> KafkaConsumer:
        """Create and configure Kafka consumer."""
        if not settings.KAFKA_ENABLED:
            raise RuntimeError("Kafka must be enabled to run the consumer")

        if not self.topic:
            raise RuntimeError("Consumer topic must be configured")

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

        try:
            if kafka_auth:
                # Filter out producer-specific configurations that are not valid for consumers
                # Producer-only configs: retries, max_in_flight_requests_per_connection, acks, etc.
                producer_only_configs = {
                    "retries",
                    "max_in_flight_requests_per_connection",
                    "acks",
                    "enable_idempotence",
                    "transactional_id",
                    "transaction_timeout_ms",
                    "compression_type",
                    "batch_size",
                    "linger_ms",
                    "buffer_memory",
                    "max_block_ms",
                    "delivery_timeout_ms",
                }
                consumer_auth = {k: v for k, v in kafka_auth.items() if k not in producer_only_configs}
                # Log if any producer-specific configs were filtered out
                filtered_configs = set(kafka_auth.keys()) & producer_only_configs
                if filtered_configs:
                    logger.info(f"Filtered out producer-specific configs for consumer: {filtered_configs}")
                consumer = KafkaConsumer(
                    self.topic,
                    auto_offset_reset="earliest",  # Process all messages from beginning if no offset exists
                    enable_auto_commit=False,  # Manual commit for at-least-once processing
                    group_id=settings.RBAC_KAFKA_CONSUMER_GROUP_ID,
                    **consumer_auth,
                )
                logger.info(f"Kafka consumer created with auth for topic: {self.topic}")
            else:
                consumer = KafkaConsumer(
                    self.topic,
                    bootstrap_servers=kafka_servers,
                    auto_offset_reset="earliest",  # Process all messages from beginning if no offset exists
                    enable_auto_commit=False,  # Manual commit for at-least-once processing
                    group_id=settings.RBAC_KAFKA_CONSUMER_GROUP_ID,
                )
                logger.info(f"Kafka consumer created with servers {kafka_servers} for topic: {self.topic}")

            return consumer

        except Exception as e:
            logger.error(f"Failed to create Kafka consumer: {e}")
            raise

    def _acquire_lock(self, lock_id: str) -> str:
        """Acquire a lock token from the Relations API.

        Args:
            lock_id: Unique identifier for the lock (format: "consumer-group/partition")

        Returns:
            str: The lock token

        Raises:
            grpc.RpcError: If the lock acquisition fails
        """
        return relations_api_replication.acquire_lock(lock_id)

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

    def _on_partitions_revoked(self, revoked_partitions):
        """Handle partition rebalance - commit offsets before partitions are revoked.

        This is called by Kafka when partitions are being reassigned to other consumers.
        We commit our current offsets to ensure we don't lose progress.
        """
        # Track rebalance event
        rebalance_events_total.labels(event_type="partitions_revoked").inc()

        if not self.commit_config.commit_on_rebalance:
            logger.debug("Rebalance offset commit disabled by config")
            return

        if self._shutdown_in_progress:
            logger.debug("Skipping rebalance commit during shutdown")
            return

        logger.info(f"Partitions being revoked: {revoked_partitions}")

        # Commit any stored offsets before losing the partitions
        if self.offset_manager:
            success, count = self.offset_manager.commit()
            if success and count > 0:
                logger.info(f"Successfully committed {count} offset(s) during rebalance")
            elif success and count == 0:
                logger.debug("No stored offsets to commit during rebalance")
            else:
                logger.warning("Failed to commit offsets during rebalance")

        # Clear lock token since we no longer own the partition
        with self._lock_mutex:
            if self.lock_token:
                logger.info(f"Clearing lock token for {self.lock_id} due to partition revocation")
                self.lock_token = None
                self.lock_id = None

    def _update_health_status(self, healthy: bool):
        """Update health status files."""
        self.is_healthy = healthy
        self.last_activity = time.time()

        if healthy:
            # Create/update liveness file
            self.liveness_file.touch()
            # Create/update readiness file
            self.readiness_file.touch()
            logger.debug("Health status updated: healthy")
        else:
            # Remove readiness file but keep liveness file
            if self.readiness_file.exists():
                self.readiness_file.unlink()
            logger.warning("Health status updated: unhealthy")

    def _start_health_check_thread(self):
        """Start the background health check thread."""
        if self.health_check_thread is not None:
            return

        self._stop_health_check.clear()
        self.health_check_thread = threading.Thread(
            target=self._health_check_loop,
            name="kafka-consumer-health-check",
            daemon=True,
        )
        self.health_check_thread.start()
        logger.info(f"Started health check thread with {self.health_check_interval}s interval")

    def _stop_health_check_thread(self):
        """Stop the background health check thread."""
        if self.health_check_thread is None:
            return

        self._stop_health_check.set()
        self.health_check_thread.join(timeout=5)
        self.health_check_thread = None
        logger.info("Stopped health check thread")

    def _health_check_loop(self):
        """Background thread that periodically updates health status."""
        while not self._stop_health_check.wait(self.health_check_interval):
            try:
                # Check if consumer is still alive and connected
                if self.is_consuming and self.consumer is not None:
                    # Try to check Kafka connection by getting partition metadata
                    # This is a lightweight operation that verifies connectivity
                    try:
                        # This will raise an exception if Kafka is unreachable
                        # Use partitions_for_topic which is more universally available
                        self.consumer.partitions_for_topic(self.topic)

                        # Consumer is alive and connected
                        self._update_health_status(True)
                        logger.debug("Health check passed: consumer is connected and ready")

                    except Exception as e:
                        logger.warning(f"Health check failed: Kafka connectivity issue: {e}")
                        # Don't immediately mark as unhealthy, give it a few tries
                        # Only update liveness (consumer process is alive) but not readiness
                        self.liveness_file.touch()
                        if self.readiness_file.exists():
                            self.readiness_file.unlink()

                elif self.is_consuming:
                    # Consumer should be running but isn't - this is a problem
                    logger.error("Health check failed: consumer should be running but isn't")
                    self._update_health_status(False)
                else:
                    # Consumer is not supposed to be running (stopped/stopping)
                    logger.debug("Health check: consumer is not running (expected)")

            except Exception as e:
                logger.error(f"Health check thread error: {e}")
                # Keep liveness but remove readiness on thread errors
                self.liveness_file.touch()
                if self.readiness_file.exists():
                    self.readiness_file.unlink()

    def _process_single(self, message_value: Dict[str, Any], message_partition: int, message_offset: int) -> bool:
        """Process a single message (parse and handle).

        This is the core message processing logic without retry concerns.

        Args:
            message_value: The Kafka message value to process
            message_partition: The partition number (for logging)
            message_offset: The message offset (for logging)

        Returns:
            bool: True if message processed successfully

        Raises:
            ValidationError: If message validation fails
            Other exceptions: If processing fails
        """
        # Parse Debezium message (may raise ValidationError or JSONDecodeError)
        parsed_message = self._parse_debezium_message(message_value)

        # Process the message (may raise ValidationError or other exceptions)
        return self._process_debezium_message(parsed_message, message_partition, message_offset)

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

    def _process_message_with_retry(
        self,
        message_value: Dict[str, Any],
        message_offset: int,
        message_partition: int,
        topic_partition: TopicPartition,
        leader_epoch: Optional[int] = None,
        error_handler=None,
    ) -> bool:
        """Process a message with comprehensive retry logic.

        Retries transient errors (network, DB, etc.) but NOT ValidationError.
        ValidationError indicates permanently malformed messages that won't become valid.

        Args:
            message_value: The Kafka message value to process
            message_offset: The message offset
            message_partition: The partition number
            topic_partition: TopicPartition object for offset tracking
            leader_epoch: The leader epoch for the partition (optional)
            error_handler: Optional callable(Exception) -> bool to short-circuit retries

        Returns:
            bool: True if message processed successfully, False only on shutdown (InterruptedError)

        Raises:
            Exception: Re-raises any exception that should stop the consumer
        """  # noqa: D202

        # Define error handler to skip retries for non-retryable errors
        def should_skip_retry(exception: Exception) -> bool:
            """Return True if retry should be skipped (non-retryable error)."""
            # Import here to avoid circular dependency
            from google.protobuf.json_format import ParseError

            # ValidationError and ParseError mean bad message format - retrying won't help
            if isinstance(exception, (ValidationError, ParseError)):
                logger.error(
                    f"{type(exception).__name__} is non-retryable for message at partition {message_partition}, "
                    f"offset {message_offset}. Consumer will stop."
                )
                return True
            # Use custom error handler if provided
            if error_handler and callable(error_handler):
                return error_handler(exception)
            return False

        # Create a retry helper with custom error handling
        retry_helper = RetryHelper(
            retry_config=self.retry_config,
            shutdown_event=self._stop_health_check,
            error_handler=should_skip_retry,
        )

        # Process message with retry logic
        def process_wrapper():
            """Wrap message processing for retry logic."""
            # Process the message
            success = self._process_single(message_value, message_partition, message_offset)

            # If processing returned False, treat as an error and retry
            if not success:
                raise RuntimeError("Message processing failed with False return value")

            return True

        try:
            # Run with retry logic
            # IMPORTANT: We do NOT commit offsets during retries
            # Offsets are only committed after successful processing
            retry_helper.run(process_wrapper)
            return True

        except InterruptedError:
            # Shutdown signal received - this is the ONLY case where we return False
            logger.info("Message processing interrupted by shutdown signal")
            return False

        except RuntimeError as e:
            # Max retries exceeded - stop consumer to allow Kubernetes restart
            error_msg = (
                f"Max operation retries exceeded for message "
                f"(partition: {message_partition}, offset: {message_offset}): {e}. "
                f"Consumer will STOP to allow Kubernetes restart.\n"
                f"Offset NOT committed - message will be retried on restart.\n"
                f"To resolve: Fix the issue. Kubernetes will restart the pod automatically.\n"
                f"Message content: {message_value}"
            )
            logger.error(error_msg)
            messages_processed_total.labels(message_type="unknown", status="max_retries_exceeded").inc()

            # Mark consumer as paused to prevent offset commit on shutdown
            self.is_paused_for_retry = True

            # Stop consumer and let Kubernetes handle restart
            logger.critical(
                "CONSUMER STOPPING: Max retries exceeded. Manual intervention required. "
                "Kubernetes will restart the pod. Offset NOT committed - message will be retried."
            )

            # Raise exception to stop consumer - Kubernetes will restart the pod
            raise

        except Exception as e:
            # Error handler short-circuited retry or other unexpected error
            # This should NOT be silently ignored - raise to stop the consumer
            error_msg = (
                f"Error handler short-circuited retry for message "
                f"(partition: {message_partition}, offset: {message_offset}): {e}. "
                f"Consumer will stop to prevent silent message loss."
            )
            logger.error(error_msg)
            messages_processed_total.labels(message_type="unknown", status="error_handler_skip").inc()
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
            messages_processed_total.labels(message_type="debezium", status="error").inc()
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
                messages_processed_total.labels(message_type="relations", status="validation_failed").inc()
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
                relation_pb = json_format.ParseDict(relation_dict, common_pb2.Relationship())
                relations_to_add_pb.append(relation_pb)

            relations_to_remove_pb = []
            for relation_dict in replication_msg.relations_to_remove:
                relation_pb = json_format.ParseDict(relation_dict, common_pb2.Relationship())
                relations_to_remove_pb.append(relation_pb)

            # Build fencing check with lock token (thread-safe read)
            # Note: Lock token should be available because _run_message_loop calls
            # _ensure_lock_token_on_assignment before processing the first message.
            # However, if that acquisition failed or token was cleared, we fail fast here.
            fencing_check = None
            with self._lock_mutex:
                if self.lock_id and self.lock_token:
                    from kessel.relations.v1beta1 import relation_tuples_pb2

                    fencing_check = relation_tuples_pb2.FencingCheck(
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
            replication_delete_response = relations_api_replication.delete_relationships(
                relationships=relations_to_remove_pb, fencing_check=fencing_check
            )
            delete_duration = time.time() - delete_start

            # Do tuple writes for relationships with fencing check
            add_start = time.time()
            replication_add_response = relations_api_replication.write_relationships(
                relationships=relations_to_add_pb, fencing_check=fencing_check
            )
            add_duration = time.time() - add_start

            # Extract consistency token from responses
            token = getattr(replication_add_response.consistency_token, "token", None) or getattr(
                replication_delete_response.consistency_token, "token", None
            )

            if token and org_id:
                try:
                    tenant = Tenant.objects.get(org_id=org_id)
                    tenant.relations_consistency_token = token
                    tenant.save()
                except Tenant.DoesNotExist:
                    logger.warning(
                        f"Tenant not found for org_id: {org_id}. " f"Unable to save consistency token: {token}"
                    )
            else:
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

            # remove_legacy_root_workspace_tenant_parent_relations job LISTENs for batch completion
            batch_notify_token = resource_context.get("notify_token") if resource_context else None
            if event_type == ReplicationEventType.REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS.value and batch_notify_token:
                payload = str(batch_notify_token).strip()
                _send_pg_notify_best_effort(
                    REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_CHANNEL,
                    payload,
                    f"remove_root_parent_tenant_relationships batch (token={payload})",
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

            messages_processed_total.labels(message_type="relations", status="success").inc()
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
                messages_processed_total.labels(message_type="relations", status="fencing_failed").inc()
                raise RuntimeError(error_msg) from e
            else:
                logger.error(
                    f"gRPC error processing relations message: {e.code()}: {e.details()} "
                    f"(partition: {message_partition}, offset: {message_offset})"
                )
                messages_processed_total.labels(message_type="relations", status="grpc_error").inc()
                raise
        except Exception as e:
            logger.error(
                f"Error processing relations message: {e} "
                f"(partition: {message_partition}, offset: {message_offset})"
            )
            messages_processed_total.labels(message_type="relations", status="error").inc()
            # Re-raise to trigger retry logic
            raise

    def _initialize_consumer_setup(self):
        """Initialize consumer, subscribe to topic, and prepare for consumption.

        Returns:
            RebalanceListener: The rebalance listener instance
        """
        self.consumer = self._create_consumer()
        self.offset_manager = OffsetManager(self.consumer, self.commit_config)

        # Subscribe to topic with rebalance listener
        rebalance_listener = RebalanceListener(self)
        self.consumer.subscribe([self.topic], listener=rebalance_listener)

        self.is_consuming = True
        self._start_health_check_thread()
        self._update_health_status(True)

        logger.info(f'RBAC Kafka consumer started, listening on topic "{self.topic}"')
        logger.info(f"Batch commit enabled: every {self.commit_config.commit_modulo} messages")
        logger.info("Waiting for messages from Kafka...")

        return rebalance_listener

    def _initialize_partition_offset_tracking(self, topic_partition, last_committed_offsets):
        """Initialize offset tracking for a new partition.

        Args:
            topic_partition: TopicPartition object
            last_committed_offsets: Dict tracking last committed offsets per partition
        """
        try:
            committed = self.consumer.committed(topic_partition)
            last_committed_offsets[topic_partition] = committed if committed is not None else -1
            logger.info(
                f"Partition {topic_partition.partition}: last committed offset: "
                f"{last_committed_offsets[topic_partition]}"
            )
        except Exception as e:
            logger.warning(f"Could not get committed offset for partition {topic_partition.partition}: {e}")
            last_committed_offsets[topic_partition] = -1

    def _handle_tombstone_message(self, message, topic_partition):
        """Handle tombstone (None value) messages.

        Args:
            message: Kafka message with None value
            topic_partition: TopicPartition object

        Returns:
            bool: True (tombstone handled successfully)
        """
        logger.warning(
            f"Received message with None value, skipping "
            f"(partition: {message.partition}, offset: {message.offset})"
        )
        # Treat None messages as successfully processed
        self.offset_manager.store(topic_partition, message.offset, message.leader_epoch)
        if self.offset_manager.should_commit(message.offset):
            self.offset_manager.commit()  # Don't need to check return value here
        return True

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
            messages_processed_total.labels(message_type="unknown", status="json_error").inc()
            raise

    def _process_and_commit_message(self, message, message_value, topic_partition, last_committed_offsets):
        """Process a message and handle offset commits.

        Args:
            message: Kafka message
            message_value: Parsed message value (dict)
            topic_partition: TopicPartition object
            last_committed_offsets: Dict tracking last committed offsets

        Returns:
            bool: True if should continue processing, False if should break loop
        """
        # Log processing
        last_committed = last_committed_offsets.get(topic_partition, -1)
        logger.info(
            f"Processing message (partition: {message.partition}, offset: {message.offset}, "
            f"last_committed: {last_committed})"
        )
        logger.debug(f"Message content (partition: {message.partition}, offset: {message.offset}): {message_value}")

        # Process with retry logic
        success = self._process_message_with_retry(
            message_value,
            message.offset,
            message.partition,
            topic_partition,
            message.leader_epoch,
        )

        if success:
            # Store offset after successful processing
            self.offset_manager.store(topic_partition, message.offset, message.leader_epoch)

            # Check if we should commit based on batch size
            if self.offset_manager.should_commit(message.offset):
                logger.info(
                    f"Batch commit triggered at offset {message.offset} "
                    f"(CommitModulo: {self.commit_config.commit_modulo})"
                )
                success, count = self.offset_manager.commit()
                if success:
                    last_committed_offsets[topic_partition] = message.offset + 1
            else:
                logger.debug(f"Offset {message.offset} stored, waiting for batch commit")

            # Update activity timestamp
            self.last_activity = time.time()
            return True  # Continue processing
        else:
            # Shutdown interrupted - InterruptedError
            logger.info(
                f"Message processing interrupted by shutdown "
                f"(partition: {message.partition}, offset: {message.offset}). "
                f"Offset NOT committed - message will be retried on restart."
            )
            return False  # Break loop

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
        consumer_group_id = self.consumer.config.get("group_id")
        expected_lock_id = f"{consumer_group_id}/{partition.partition}"

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

    def _run_message_loop(self):
        """Run the main message consumption loop."""
        last_committed_offsets = {}

        for message in self.consumer:
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
            if not last_committed_offsets:
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

            try:
                topic_partition = TopicPartition(message.topic, message.partition)

                # Initialize offset tracking for new partitions
                if topic_partition not in last_committed_offsets:
                    self._initialize_partition_offset_tracking(topic_partition, last_committed_offsets)

                # Handle tombstone messages
                if message.value is None:
                    self._handle_tombstone_message(message, topic_partition)
                    continue

                # Parse JSON message
                message_value = self._parse_message_value(message)

                # Process and commit
                should_continue = self._process_and_commit_message(
                    message, message_value, topic_partition, last_committed_offsets
                )

                if not should_continue:
                    break  # Shutdown requested

            except Exception as e:
                # Fail fast on unexpected exceptions
                logger.error(
                    f"Unexpected error in message loop "
                    f"(partition: {getattr(message, 'partition', 'unknown')}, "
                    f"offset: {getattr(message, 'offset', 'unknown')}): {e}. "
                    f"Consumer will stop to prevent data loss. "
                    f"Message will be retried on restart."
                )
                messages_processed_total.labels(message_type="unknown", status="unexpected_error").inc()
                raise

    def start_consuming(self):
        """Start consuming messages from Kafka.

        The consumer will stop on fatal errors (e.g., max retries exceeded).
        Use Kubernetes/orchestration layer to restart the consumer pod on failure.
        """
        try:
            # Record consumer start time
            consumer_start_time.set_to_current_time()
            logger.info("Consumer start time recorded in metrics")

            # Initialize consumer and subscribe to topic
            self._initialize_consumer_setup()

            # Record consumer info after successful initialization
            consumer_info.labels(
                topic=self.topic,
                group_id=settings.RBAC_KAFKA_CONSUMER_GROUP_ID,
            ).set(1)
            logger.info(
                f"Consumer info metric set: topic={self.topic}, group_id={settings.RBAC_KAFKA_CONSUMER_GROUP_ID}"
            )

            # Start main message processing loop
            # Note: Partition assignment and lock token acquisition happen automatically
            # via the on_partitions_assigned callback during the first poll
            self._run_message_loop()

        except KafkaError as e:
            logger.error(f"Kafka error: {e}")
            self._update_health_status(False)
            raise
        except Exception as e:
            logger.error(f"Consumer error: {e}")
            self._update_health_status(False)
            raise
        finally:
            self._shutdown_in_progress = True
            self.is_consuming = False
            self._stop_health_check_thread()

            # Commit any remaining offsets on shutdown ONLY if not paused for retry
            if self.commit_config.commit_on_shutdown and self.offset_manager:
                if self.is_paused_for_retry:
                    logger.warning(
                        "Consumer was paused due to max retries - NOT committing offsets on shutdown. "
                        "Message will be retried on next restart."
                    )
                else:
                    logger.info("Committing remaining offsets on shutdown")
                    success, count = self.offset_manager.commit()
                    if success and count > 0:
                        logger.info(f"Successfully committed {count} offset(s) on shutdown")
                    elif success and count == 0:
                        logger.debug("No stored offsets to commit on shutdown")
                    else:
                        logger.warning("Failed to commit offsets on shutdown")

            if self.consumer:
                self.consumer.close()
                logger.info("Kafka consumer closed")

    def stop_consuming(self):
        """Stop consuming messages."""
        self._shutdown_in_progress = True
        self.is_consuming = False
        self._stop_health_check_thread()

        # Update consumer status metric
        if hasattr(self, "topic"):
            consumer_info.labels(
                topic=self.topic,
                group_id=settings.RBAC_KAFKA_CONSUMER_GROUP_ID,
            ).set(0)
            logger.info("Consumer status metric updated to stopped")

        # Commit any remaining offsets on stop ONLY if not paused for retry
        if self.commit_config.commit_on_shutdown and self.offset_manager:
            if self.is_paused_for_retry:
                logger.warning(
                    "Consumer was paused due to max retries - NOT committing offsets on stop. "
                    "Message will be retried on next restart."
                )
            else:
                logger.info("Committing remaining offsets on stop")
                success, count = self.offset_manager.commit()
                if success and count > 0:
                    logger.info(f"Successfully committed {count} offset(s) on stop")
                elif success and count == 0:
                    logger.debug("No stored offsets to commit on stop")
                else:
                    logger.warning("Failed to commit offsets on stop")

        if self.consumer:
            self.consumer.close()
            self.consumer = None

        self._update_health_status(False)
        logger.info("Kafka consumer stopped")

    def is_ready(self) -> bool:
        """Check if consumer is ready."""
        return self.readiness_file.exists()

    def is_alive(self) -> bool:
        """Check if consumer is alive."""
        return self.liveness_file.exists()
