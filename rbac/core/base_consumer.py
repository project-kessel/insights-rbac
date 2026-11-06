#
# Copyright 2026 Red Hat, Inc.
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

"""Common base for RBAC Kafka consumers."""

import abc
import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from django.conf import settings
from kafka import AsyncConsumerRebalanceListener, KafkaConsumer, OffsetAndMetadata, TopicPartition
from kafka.errors import KafkaError
from kafka.future import Future as KafkaFuture
from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised when message validation fails permanently (non-retryable)."""

    pass


@dataclass(frozen=True)
class BaseConsumerMetrics:
    """Metrics to be used by BaseRBACKafkaConsumer."""

    consumer_start_time: Gauge
    consumer_info: Gauge
    messages_processed_total: Counter
    retry_attempts_total: Counter
    message_retry_duration: Histogram
    rebalance_events_total: Counter
    last_message_processed_time: Gauge
    last_poll_time: Gauge


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


class RetryHelper:
    """Handles retry logic with exponential backoff for message processing.

    This class encapsulates all retry policy and logic, making it easy to test
    and maintain separately from the business logic.
    """

    def __init__(
        self,
        *,
        retry_config: RetryConfig,
        shutdown_event: threading.Event,
        metrics: BaseConsumerMetrics,
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
        self.metrics = metrics
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
                    self.metrics.message_retry_duration.labels(retry_reason="processing_error").observe(total_duration)
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
                self.metrics.retry_attempts_total.labels(
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
        # Allow custom error handler to short-circuit retry logiclast_message_processed_time
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

        Safe to call from any thread: ``consumer.commit()`` internally calls
        ``net.run()`` which marshals work onto the IO thread when the caller
        is a different thread (kafka-python selector.py L357-362).
        ``consumer.assignment()`` is ``@synchronized`` and reads local state
        only (subscription_state.py L349).

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


class RebalanceListener(AsyncConsumerRebalanceListener):
    """Listen for Kafka consumer rebalance events.

    Uses AsyncConsumerRebalanceListener so that blocking IO (offset commits,
    gRPC lock acquisition) runs in a worker thread, keeping the consumer's
    IO / heartbeat loop responsive.

    Note: kafka-python 3.x uses its own event loop (NetworkSelector), not
    asyncio. Standard asyncio primitives like asyncio.to_thread() will raise
    ``RuntimeError: no running event loop``. Instead, we bridge to worker
    threads using kafka's own ``Future`` which the NetworkSelector can
    ``await`` natively.
    """

    def __init__(
        self,
        *,
        on_assigned: Callable[[list[TopicPartition]], None],
        on_revoked: Callable[[list[TopicPartition]], None],
    ):
        """Initialize the rebalance listener with the provided assignment/revocation callbacks."""
        super().__init__()
        self._on_assigned = on_assigned
        self._on_revoked = on_revoked

    def _run_in_thread(self, fn, *args):
        """Run a blocking function in a daemon thread, returning a KafkaFuture.

        The returned KafkaFuture is compatible with kafka-python's internal
        event loop, so ``await future`` properly yields control back to the
        NetworkSelector (keeping heartbeats alive) until the thread completes.

        After the worker thread resolves the future, it wakes the
        NetworkSelector so the suspended coroutine resumes immediately
        rather than waiting for the next heartbeat / select timeout.
        """
        future = KafkaFuture()

        def _target():
            try:
                result = fn(*args)
                future.success(result)
            except Exception as exc:
                future.failure(exc)

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        return future

    async def on_partitions_revoked(self, revoked):
        """Handle partition revocation during rebalance.

        Runs the provided revocation callback in a worker thread. Returns a
        promise that completes when the callback finishes.

        Thread-safety of calling consumer.commit()/assignment() from the
        worker thread (verified against kafka-python 3.0.11 source):

        * ``assignment()`` delegates to ``SubscriptionState.assigned_partitions()``
          which is ``@synchronized`` (acquires an RLock) and returns a copy of
          local state -- no network IO involved.  (subscription_state.py L349)

        * ``commit()`` calls ``ConsumerCoordinator.commit_offsets_sync()`` which
          uses ``self._net.run(coroutine)`` to marshal the actual network work
          onto the IO thread.  When the caller is **not** the IO thread,
          ``NetworkSelector.run()`` enqueues the coroutine via ``call_soon()``,
          wakes the selector, and blocks on a ``threading.Event`` until the IO
          thread completes it.  Its docstring states: "Safe to call concurrently
          from multiple caller threads."  (selector.py L357-362)

        * No deadlock: this async callback awaits a KafkaFuture (yielded to
          the selector), so the IO thread's poll loop keeps running and can pick
          up the enqueued commit task from the worker thread.

        Args:
            revoked: List of TopicPartition objects being revoked
        """
        await self._run_in_thread(self._on_revoked, list(revoked))

    async def on_partitions_assigned(self, assigned):
        """Handle partition assignment during rebalance.

        Runs the provided assignment callback in a worker thread. Returns a
        promise that completes when the callback finishes.

        Args:
            assigned: List of TopicPartition objects being assigned
        """
        await self._run_in_thread(self._on_assigned, list(assigned))


class BaseRBACKafkaConsumer(abc.ABC):
    """Common base class for standalone Kafka consumers."""

    def __init__(
        self,
        *,
        topic: str,
        group_id: str,
        metrics: BaseConsumerMetrics,
        health_check_interval: int = 30,
        retry_config: Optional[RetryConfig] = None,
        commit_config: Optional[CommitConfig] = None,
        extra_consumer_config: Optional[dict] = None,
    ):
        """Initialize the consumer."""
        if not topic:
            raise RuntimeError("Consumer topic must be configured")

        self.topic = topic
        self.group_id = group_id
        self.consumer: Optional[KafkaConsumer] = None
        self.retry_config = retry_config or RetryConfig()
        self.commit_config = commit_config or CommitConfig()
        self.extra_consumer_config = dict(extra_consumer_config or {})
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
        self._metrics = metrics

        # Offset manager (will be initialized when consumer is created)
        self.offset_manager: Optional[OffsetManager] = None

    def _create_consumer(self) -> KafkaConsumer:
        """Create and configure Kafka consumer."""
        if not settings.KAFKA_ENABLED:
            raise RuntimeError("Kafka must be enabled to run the consumer")

        try:
            base_config = {
                "auto_offset_reset": "earliest",  # Process all messages from beginning if no offset exists
                "enable_auto_commit": False,  # Manual commit for at-least-once processing
                "group_id": self.group_id,
            }

            for key in base_config.keys():
                if key in self.extra_consumer_config:
                    raise ValueError(f"Unexpected key in extra_consumer_config: {key}")

            consumer = KafkaConsumer(self.topic, **base_config, **self.extra_consumer_config)
            logger.info(f"Kafka consumer created for topic: {self.topic}")

            return consumer

        except Exception as e:
            logger.error(f"Failed to create Kafka consumer: {e}")
            raise

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
            self.readiness_file.unlink(missing_ok=True)
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
            self._health_check_loop_iteration()

    def _health_check_loop_iteration(self):
        """Run a single health check iteration."""
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
                    self._metrics.last_poll_time.set(time.time())
                    logger.debug("Health check passed: consumer is connected and ready")

                except Exception as e:
                    logger.warning(f"Health check failed: Kafka connectivity issue: {e}")
                    # Don't immediately mark as unhealthy, give it a few tries
                    # Only update liveness (consumer process is alive) but not readiness
                    self.liveness_file.touch()
                    self.readiness_file.unlink(missing_ok=True)

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
            self.readiness_file.unlink(missing_ok=True)

    @abc.abstractmethod
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
        ...

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
            metrics=self._metrics,
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
            self._metrics.messages_processed_total.labels(message_type="unknown", status="max_retries_exceeded").inc()

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
            self._metrics.messages_processed_total.labels(message_type="unknown", status="error_handler_skip").inc()
            raise

    def _initialize_consumer_setup(self):
        """Initialize consumer, subscribe to topic, and prepare for consumption.

        Returns:
            RebalanceListener: The rebalance listener instance
        """
        self.consumer = self._create_consumer()
        self.offset_manager = OffsetManager(self.consumer, self.commit_config)

        # Subscribe to topic with rebalance listener
        rebalance_listener = RebalanceListener(
            on_assigned=self._on_partitions_assigned, on_revoked=self._on_partitions_revoked
        )
        self.consumer.subscribe([self.topic], listener=rebalance_listener)

        self.is_consuming = True
        self._start_health_check_thread()
        self._update_health_status(True)

        logger.info(f"Kafka consumer started, listening on topic {self.topic!r} in group {self.group_id!r}")
        logger.info(f"Batch commit enabled: every {self.commit_config.commit_modulo} messages")
        logger.info("Waiting for messages from Kafka...")

        return rebalance_listener

    @abc.abstractmethod
    def _do_handle_partitions_revoked(self, revoked_partitions: list[TopicPartition]):
        """Handle any actions required after permission revocation."""
        ...

    def _on_partitions_revoked(self, revoked_partitions: list[TopicPartition]):
        """Handle partition rebalance - commit offsets before partitions are revoked.

        This is called by Kafka when partitions are being reassigned to other consumers.
        We commit our current offsets to ensure we don't lose progress.
        """
        # Track rebalance event
        self._metrics.rebalance_events_total.labels(event_type="partitions_revoked").inc()

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

        self._do_handle_partitions_revoked(revoked_partitions)

    @abc.abstractmethod
    def _do_handle_partitions_assigned(self, assigned_partitions: list[TopicPartition]):
        """Handle any actions required after permission assignment."""
        ...

    def _on_partitions_assigned(self, assigned_partitions: list[TopicPartition]):
        """Handle partition assignment during rebalance.

        Runs the blocking lock acquisition in a worker thread to avoid
        blocking the consumer IO thread.

        Args:
            assigned_partitions: List of TopicPartition objects being assigned
        """
        # Track rebalance event
        self._metrics.rebalance_events_total.labels(event_type="partitions_assigned").inc()
        logger.info(f"Partitions assigned: {assigned_partitions}")

        self._do_handle_partitions_assigned(assigned_partitions)

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

    def _handle_tombstone_message(self, message, topic_partition: TopicPartition):
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

        # Update activity timestamp and heartbeat metric for tombstones too,
        # so the consumer doesn't appear inactive when processing tombstones.
        self.last_activity = time.time()
        self._metrics.last_message_processed_time.set(self.last_activity)
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
            self._metrics.messages_processed_total.labels(message_type="unknown", status="json_error").inc()
            raise

    def _process_and_commit_message(self, message, topic_partition, last_committed_offsets):
        """Process a message and handle offset commits.

        Args:
            message: Kafka message
            message_value: Parsed message value (dict)
            topic_partition: TopicPartition object
            last_committed_offsets: Dict tracking last committed offsets

        Returns:
            bool: True if should continue processing, False if should break loop
        """
        # Parse JSON message
        message_value = self._parse_message_value(message)

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

            # Update activity timestamp and heartbeat metric
            self.last_activity = time.time()
            self._metrics.last_message_processed_time.set(self.last_activity)
            return True  # Continue processing
        else:
            # Shutdown interrupted - InterruptedError
            logger.info(
                f"Message processing interrupted by shutdown "
                f"(partition: {message.partition}, offset: {message.offset}). "
                f"Offset NOT committed - message will be retried on restart."
            )
            return False  # Break loop

    @abc.abstractmethod
    def _prepare_for_message(self, message, first_message: bool):
        """Perform any setup required before handling a message."""
        ...

    def _run_message_loop(self):
        """Run the main message consumption loop."""
        last_committed_offsets = {}

        for message in self.consumer:
            # Record poll activity — each iteration processes one message from a poll batch.
            # During idle periods (no messages), the health check thread keeps this metric fresh.
            self._metrics.last_poll_time.set(time.time())

            self._prepare_for_message(message=message, first_message=(len(last_committed_offsets) == 0))

            try:
                topic_partition = TopicPartition(message.topic, message.partition)

                # Initialize offset tracking for new partitions
                if topic_partition not in last_committed_offsets:
                    self._initialize_partition_offset_tracking(topic_partition, last_committed_offsets)

                # Handle tombstone messages
                if message.value is None:
                    self._handle_tombstone_message(message, topic_partition)
                    continue

                # Process and commit
                should_continue = self._process_and_commit_message(message, topic_partition, last_committed_offsets)

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
                self._metrics.messages_processed_total.labels(message_type="unknown", status="unexpected_error").inc()
                raise

    def start_consuming(self):
        """Start consuming messages from Kafka.

        The consumer will stop on fatal errors (e.g., max retries exceeded).
        Use Kubernetes/orchestration layer to restart the consumer pod on failure.
        """
        try:
            # Record consumer start time
            self._metrics.consumer_start_time.set_to_current_time()
            logger.info("Consumer start time recorded in metrics")

            # Initialize consumer and subscribe to topic
            self._initialize_consumer_setup()

            # Record consumer info after successful initialization
            self._metrics.consumer_info.labels(
                topic=self.topic,
                group_id=self.group_id,
            ).set(1)
            logger.info(f"Consumer info metric set: topic={self.topic}, group_id={self.group_id}")

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
            self._metrics.consumer_info.labels(topic=self.topic, group_id=self.group_id).set(0)
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
