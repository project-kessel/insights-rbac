#
# Copyright 2019 Red Hat, Inc.
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

"""Handler for principal clean up."""

import base64
import json
import logging
from typing import NamedTuple, Optional
from xml.parsers.expat import ExpatError

import xmltodict
from core.kafka import RBACProducer, get_cluster_config
from django.conf import settings
from django.db import connection, transaction
from kafka import KafkaConsumer
from kafka.errors import KafkaError
from management.principal.model import Principal
from management.principal.proxy import PrincipalProxy, external_principal_to_user
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.tenant_service import get_tenant_bootstrap_service
from management.tenant_service.tenant_service import TenantBootstrapService
from prometheus_client import Counter
from rest_framework import status
from sentry_sdk import capture_exception

from api.models import Tenant, User

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

PROXY = PrincipalProxy()  # pylint: disable=invalid-name

LOCK_ID = 42  # For Keith, with Love
KAFKA_CONSUMER_LOCK_ID = 43  # Guards Kafka consumer construction to prevent multi-worker join thrash

# KAFKA Metric Messages
METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL = "kafka_messages_success_total"
METRIC_KAFKA_MESSAGES_FAILURE_TOTAL = "kafka_messages_failure_total"
kafka_messages_success_total = Counter(
    METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL,
    "Number of Kafka messages processed successfully",
)
kafka_messages_failure_total = Counter(
    METRIC_KAFKA_MESSAGES_FAILURE_TOTAL,
    "Number of Kafka messages that failed to be processed",
)


def clean_tenant_principals(tenant):
    """Check if all the principals in the tenant exist, remove non-existent principals."""
    removed_principals = []
    principals = list(Principal.objects.filter(type="user").filter(tenant=tenant))
    tenant_id = tenant.org_id
    logger.info(
        "clean_tenant_principals: Running clean up on %d principals for tenant %s.", len(principals), tenant_id
    )
    for principal in principals:
        if principal.cross_account:
            continue
        logger.debug("clean_tenant_principals: Checking for username %s for tenant %s.", principal.username, tenant_id)
        org_id = tenant.org_id
        resp = PROXY.request_filtered_principals([principal.username], org_id=org_id)
        status_code = resp.get("status_code")
        data = resp.get("data")
        logger.info("clean_tenant_principals: Response code: %s Data: %s", str(status_code), str(data))
        if status_code == status.HTTP_200_OK and data:
            logger.debug(
                "clean_tenant_principals: Username %s found for tenant %s, no change needed.",
                principal.username,
                tenant_id,
            )
        elif status_code == status.HTTP_200_OK and not data:
            removed_principals.append(principal.username)
            logger.info(
                "clean_tenant_principals: Username %s not found for tenant %s, principal eligible for removal.",
                principal.username,
                tenant_id,
            )
            principal.delete()
            logger.info(
                "clean_tenant_principals: Username %s removed.",
                principal.username,
            )
        else:
            logger.warning(
                "clean_tenant_principals: Unknown status %d when checking username %s"
                " for tenant %s, no change needed.",
                status_code,
                principal.username,
                tenant_id,
            )
    removal_message = "clean_tenant_principals: Completed clean up of %d principals for tenant %s, %d removed: %s."
    logger.info(
        removal_message,
        len(principals),
        tenant_id,
        len(removed_principals),
        str(removed_principals),
    )


def clean_tenants_principals():
    """Check which principals are eligible for clean up."""
    logger.info("clean_tenant_principals: Start principal clean up.")

    for tenant in list(Tenant.objects.filter(ready=True).exclude(tenant_name="public")):
        logger.info("clean_tenant_principals: Running principal clean up for tenant %s.", tenant.tenant_name)
        clean_tenant_principals(tenant)
        logger.info("clean_tenant_principals: Completed principal clean up for tenant %s.", tenant.tenant_name)

    logger.info("clean_tenant_principals: Principal cleanup complete for all tenants.")


def retrieve_user_info_umb(message) -> User:
    """
    Retrieve user info from the message.

    returns:
        user: User object as of latest known state.
    """
    instance_id: Optional[str] = None

    if (header := message.get("Header")) is not None:
        if (id := header.get("InstanceId")) is not None:
            instance_id = id

    logger.debug("retrieve_user_info_UMB: Processing message with instance_id=%s", instance_id)

    message_user = message["Payload"]["Sync"]["User"]
    identifiers = message_user["Identifiers"]
    user_id: Optional[str] = None

    if isinstance((ids := identifiers["Identifier"]), list):
        for id in ids:  # type: ignore
            if id["@system"] == "WEB" and id["@entity-name"] == "User" and id["@qualifier"] == "id":
                user_id = id["#text"]
                break
    else:
        user_id = identifiers["Identifier"]["#text"]

    if user_id is None:
        raise ValueError("User id not found in message. instance_id=%s", instance_id)

    bop_resp = PROXY.request_filtered_principals([user_id], options={"query_by": "user_id", "return_id": True})

    if not bop_resp["data"]:  # User has been deleted
        # Get data from message instead.
        user = User()
        user.user_id = user_id
        user.is_active = False
        user.username = message_user["Person"]["Credentials"]["Login"]
        # identifiers["Reference"] might be a dict
        if not isinstance((refs := identifiers["Reference"]), list):
            refs = [identifiers["Reference"]]
        # Preserve original UMB behavior: use first matching reference and stop
        # This maintains production behavior where messages typically have one of each reference type
        # If multiple WEB/Customer or EBS/Account references exist, use the first one
        for ref in refs:
            if ref["@system"] == "WEB" and ref["@entity-name"] == "Customer" and ref["@qualifier"] == "id":
                user.org_id = ref["#text"]
                break
            if ref["@system"] == "EBS" and ref["@entity-name"] == "Account" and ref["@qualifier"] == "number":
                user.account = ref["#text"]
                break

        return user

    user_data = bop_resp["data"][0]
    return external_principal_to_user(user_data)


def retrieve_user_info_kafka(message) -> User:
    """
    Retrieve user info from the Kafka message.

    Args:
        message: JSON message from Kafka containing user event data

    returns:
        user: User object as of latest known state.
    """
    instance_id: Optional[str] = None

    # Extract instance ID from header if present
    if (header := message.get("Header")) is not None:
        if (id := header.get("InstanceId")) is not None:
            instance_id = id

    logger.debug("retrieve_user_info_kafka: Processing message with instance_id=%s", instance_id)

    # Navigate through JSON structure (similar to XML but without @ and # prefixes)
    message_user = message["Payload"]["Sync"]["User"]
    identifiers = message_user["Identifiers"]
    user_id: Optional[str] = None

    # Handle both list and single identifier cases
    identifier_list = identifiers.get("Identifier", [])
    if not isinstance(identifier_list, list):
        identifier_list = [identifier_list]

    # Find the user ID from identifiers
    for identifier in identifier_list:
        is_web_user_id = (
            identifier.get("system") == "WEB"
            and identifier.get("entity-name") == "User"
            and identifier.get("qualifier") == "id"
        )
        if is_web_user_id:
            user_id = identifier.get("text") or identifier.get("value")
            break

    if user_id is None:
        raise ValueError(f"User id not found in message. instance_id={instance_id}")

    # Query BOP for user information
    bop_resp = PROXY.request_filtered_principals([user_id], options={"query_by": "user_id", "return_id": True})

    if not bop_resp["data"]:  # User has been deleted
        # Get data from message instead
        user = User()
        user.user_id = user_id
        user.is_active = False
        user.username = message_user["Person"]["Credentials"]["Login"]

        # Handle references (might be a dict or list)
        references = identifiers.get("Reference", [])
        if not isinstance(references, list):
            references = [references]

        # Match UMB behavior: use first matching reference and stop (preserve production semantics)
        # Kafka receives the same messages as UMB (just different transport), so behavior should be identical
        # Messages typically have one WEB/Customer reference (org_id) and one EBS/Account reference (account)
        for ref in references:
            is_web_customer = (
                ref.get("system") == "WEB" and ref.get("entity-name") == "Customer" and ref.get("qualifier") == "id"
            )
            is_ebs_account = (
                ref.get("system") == "EBS" and ref.get("entity-name") == "Account" and ref.get("qualifier") == "number"
            )
            if is_web_customer:
                user.org_id = ref.get("text") or ref.get("value")
                break
            if is_ebs_account:
                user.account = ref.get("text") or ref.get("value")
                break

        return user

    user_data = bop_resp["data"][0]
    return external_principal_to_user(user_data)


class MessageProcessingResult(NamedTuple):
    """
    Result of processing a Kafka message.

    Attributes:
        should_continue: False if another listener is running (lock contention), True otherwise
        success: True if message was processed successfully, False if it failed
    """

    should_continue: bool
    success: bool


def process_kafka_message(
    message, bootstrap_service: TenantBootstrapService, dlq_producer=None
) -> MessageProcessingResult:
    """
    Process each Kafka message.

    Args:
        message: Kafka message containing user event data
        bootstrap_service: Service for updating user/tenant state
        dlq_producer: Optional RBACProducer instance for sending failed messages to DLQ

    Returns:
        MessageProcessingResult with:
        - should_continue: False if another listener is running (lock contention), True otherwise
        - success: True if message was processed successfully, False if it failed
    """
    with transaction.atomic():
        # This is locked per transaction to ensure another listener process does not run concurrently.
        if not _lock_listener():
            # If there is another listener, let it run and abort this one.
            logger.info("process_kafka_message: Another listener is running. Aborting.")
            return MessageProcessingResult(should_continue=False, success=False)

        try:
            # Handle tombstone messages (Kafka deletes with null value)
            if message.value is None:
                logger.warning(
                    "process_kafka_message: Received tombstone message (null value) at offset %d. "
                    "Tombstones are not expected in principal cleanup topic. Skipping.",
                    message.offset,
                )
                kafka_messages_success_total.inc()
                return MessageProcessingResult(should_continue=True, success=True)

            # Parse message - handle both XML (from UMB bridge) and JSON (from native Kafka producer)
            # The messaging bridge copies raw UMB message bodies (XML) to Kafka during migration
            # Decode here (not in consumer config) so UTF-8 errors reach our error handling/DLQ logic
            message_value = message.value.decode("utf-8") if isinstance(message.value, bytes) else message.value

            # Try JSON first, then fallback to XML - more robust than string prefix detection
            # This handles edge cases like leading whitespace (" <CanonicalMessage>")
            parse_error = None
            user = None

            try:
                # Attempt JSON parse first (most common in Kafka-native deployments)
                message_data = json.loads(message_value)
                canonical_message = message_data.get("CanonicalMessage", message_data)
                # Use Kafka retrieval logic for JSON messages (plain keys, no @ or # prefixes)
                user = retrieve_user_info_kafka(canonical_message)
            except json.JSONDecodeError as json_error:
                # Not valid JSON - try XML (from UMB bridge during migration)
                try:
                    data_dict = xmltodict.parse(message_value)
                    canonical_message = data_dict.get("CanonicalMessage")
                    # Use UMB retrieval logic for XML-parsed messages (handles @ and #text attributes)
                    user = retrieve_user_info_umb(canonical_message)
                except ExpatError as xml_error:
                    # Neither JSON nor XML - this is an unprocessable message
                    parse_error = Exception(
                        f"Message is neither valid JSON nor valid XML. "
                        f"JSON error: {json_error}. XML error: {xml_error}"
                    )
                    raise parse_error

            # By default, only process disabled users.
            # If the setting is enabled, process all users.
            if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA:
                # If Tenant is not already ready, don't ready it
                bootstrap_service.update_user(user, ready_tenant=False)

            kafka_messages_success_total.inc()
            return MessageProcessingResult(should_continue=True, success=True)
        except Exception as e:
            logger.error("process_kafka_message: Error processing Kafka message: %s", str(e))
            capture_exception(e)
            kafka_messages_failure_total.inc()

            # Determine if this is a permanent error (unprocessable message) or transient error (retry later)
            # Permanent errors: Parsing failures, missing fields, schema violations, wrong types/structure
            # Transient errors: Network issues, DB connection problems, temporary service unavailability
            is_permanent_error = isinstance(
                e,
                (
                    json.JSONDecodeError,  # Malformed JSON
                    ExpatError,  # Malformed XML (from xmltodict.parse)
                    KeyError,  # Missing required field in message
                    ValueError,  # Invalid data format
                    UnicodeDecodeError,  # Invalid message encoding
                    AttributeError,  # Wrong message structure (e.g., accessing nonexistent attributes)
                    TypeError,  # Wrong type in message (e.g., iterating over non-iterable)
                ),
            )

            # For permanent errors, send to DLQ. For transient errors, retry.
            if not is_permanent_error:
                # Transient errors don't commit offset and will retry
                logger.warning(
                    "process_kafka_message: Transient error at offset %d. "
                    "Will not commit offset - message will be retried on next consumer run.",
                    message.offset,
                )
                return MessageProcessingResult(should_continue=True, success=False)

            # Permanent error - send to DLQ inside transaction
            # This prevents race window where advisory lock is released before DLQ send completes
            if dlq_producer and hasattr(settings, "KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC"):
                dlq_topic = settings.KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC
                if dlq_topic:
                    try:
                        # Build DLQ message with error context
                        # NOTE: original_message may contain PII (usernames, org/account IDs)
                        # Ensure DLQ topic has appropriate access controls and retention policy

                        # Preserve invalid UTF-8 payloads by base64-encoding them
                        # This allows poison messages (e.g., UnicodeDecodeError) to be delivered to DLQ
                        if isinstance(message.value, bytes):
                            try:
                                # Try to decode as UTF-8 first
                                original_message = message.value.decode("utf-8")
                                message_encoding = "utf-8"
                            except UnicodeDecodeError:
                                # If UTF-8 decode fails, base64-encode the raw bytes to preserve them
                                original_message = base64.b64encode(message.value).decode("ascii")
                                message_encoding = "base64"
                        else:
                            # Already a string (shouldn't happen with raw consumer, but handle it)
                            original_message = message.value
                            message_encoding = "string"

                        dlq_message = {
                            "original_message": original_message,
                            "message_encoding": message_encoding,
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "partition": message.partition,
                            "offset": message.offset,
                            "timestamp": message.timestamp,
                        }

                        # Send to DLQ while holding advisory lock (inside transaction)
                        # This prevents race window where another consumer could pick up the same message
                        # before DLQ send completes. Trade-off: holding DB lock during network I/O,
                        # but ensures exactly-once semantics for DLQ delivery.
                        dlq_producer.send_kafka_message(dlq_topic, dlq_message)
                        logger.info(
                            "process_kafka_message: Sent failed message to DLQ topic %s (partition=%d, offset=%d)",
                            dlq_topic,
                            message.partition,
                            message.offset,
                        )

                        # Rollback any partial DB writes from update_user before transaction commits
                        # This prevents inconsistent state where message is in DLQ but partial changes are committed
                        transaction.set_rollback(True)

                        # Return success=True so offset gets committed (message successfully moved to DLQ)
                        return MessageProcessingResult(should_continue=True, success=True)

                    except Exception as dlq_error:
                        logger.error(
                            "process_kafka_message: Failed to send message to DLQ: %s. "
                            "Message will be retried on restart.",
                            str(dlq_error),
                        )
                        capture_exception(dlq_error)
                        # DLQ send failed, so don't commit offset (will retry message)
                        return MessageProcessingResult(should_continue=True, success=False)
                else:
                    logger.warning(
                        "process_kafka_message: No DLQ topic configured. "
                        "Failed message at offset %d will be retried on restart.",
                        message.offset,
                    )
                    return MessageProcessingResult(should_continue=True, success=False)
            else:
                logger.warning(
                    "process_kafka_message: No DLQ producer configured. "
                    "Failed message at offset %d will be retried on restart.",
                    message.offset,
                )
                return MessageProcessingResult(should_continue=True, success=False)


def process_principal_events_from_kafka(bootstrap_service: Optional[TenantBootstrapService] = None):
    """
    Process principal events from Kafka.

    Args:
        bootstrap_service: Service for tenant/user operations
    """
    logger.info("process_principal_events_from_kafka: Start processing principal events from Kafka.")
    bootstrap_service = bootstrap_service or get_tenant_bootstrap_service(OutboxReplicator())

    # Validate required configuration
    topic = settings.KAFKA_PRINCIPAL_CLEANUP_TOPIC
    if not topic:
        logger.error(
            "process_principal_events_from_kafka: KAFKA_PRINCIPAL_CLEANUP_TOPIC is not configured. "
            "Cannot process principal events from Kafka."
        )
        return

    # Build Kafka consumer configuration
    # NOTE: This consumer runs periodically via Celery beat (every 60s) and consumes for 15s,
    # creating a 45-second gap between consumption periods. This matches the UMB behavior
    # where the consumer also ran periodically. For continuous consumption, a persistent
    # consumer (like launch-rbac-kafka-consumer) would be more appropriate, but this
    # approach maintains compatibility with the existing UMB-based architecture.

    # Include ENV_NAME in group_id to prevent offset interference across environments
    # In multi-env setups (staging, ephemeral, CI) that share a Kafka cluster, environments
    # must use distinct consumer groups to avoid message loss and offset conflicts
    env_name = getattr(settings, "ENV_NAME", "stage")

    it_kafka_servers, consumer_auth = get_cluster_config("it_managed", for_consumer=True)
    if not it_kafka_servers or not consumer_auth:
        # No IT-managed credentials wired yet (or missing) -> safely no-op instead of falling back to
        # the Clowder cluster, which does not host the principal-cleanup topics.
        logger.warning(
            "process_principal_events_from_kafka: IT-managed Kafka cluster is not configured "
            "(missing bootstrap servers or credentials). Skipping Kafka consume for topic '%s'.",
            topic,
        )
        return

    kafka_config = {
        "bootstrap_servers": it_kafka_servers,
        "group_id": f"{settings.SA_NAME}-{env_name}-principal-cleanup",
        "auto_offset_reset": "earliest",
        "enable_auto_commit": False,  # Manual commit for at-least-once semantics
        # No value_deserializer - leave as bytes to handle tombstones and UTF-8 errors in process_kafka_message
        "consumer_timeout_ms": 15000,  # 15 second timeout per run, matches UMB behavior
        # Timeout tuning: 60s beat cycle + 15s drain must fit in session_timeout_ms without causing LeaveGroup
        "session_timeout_ms": settings.KAFKA_PRINCIPAL_CLEANUP_SESSION_TIMEOUT_MS,
        "heartbeat_interval_ms": settings.KAFKA_PRINCIPAL_CLEANUP_HEARTBEAT_INTERVAL_MS,
        "max_poll_interval_ms": settings.KAFKA_PRINCIPAL_CLEANUP_MAX_POLL_INTERVAL_MS,
    }
    kafka_config.update(consumer_auth)

    # Static membership: reuse same group.instance.id across periodic cycles to avoid full rebalance
    if settings.KAFKA_PRINCIPAL_CLEANUP_STATIC_MEMBERSHIP_ENABLED:
        kafka_config["group_instance_id"] = f"{settings.SA_NAME}-{env_name}-principal-cleanup-static"

    # Initialize consumer to None to avoid UnboundLocalError in finally block
    consumer = None

    consumer_lock_held = False

    # Initialize DLQ producer if DLQ topic is configured. The DLQ topic is also on the IT-managed
    # cluster, so the producer must target that profile (not the default Clowder one).
    dlq_topic = getattr(settings, "KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC", None)
    dlq_producer = None
    if dlq_topic:
        try:
            dlq_producer = RBACProducer(cluster="it_managed")
            logger.info("process_principal_events_from_kafka: DLQ producer initialized for topic: %s", dlq_topic)
        except Exception as e:
            logger.warning(
                "process_principal_events_from_kafka: Failed to initialize DLQ producer: %s. "
                "Failed messages will be retried instead of sent to DLQ.",
                str(e),
            )

    try:
        consumer_lock_held = _try_acquire_kafka_consumer_lock()
        if not consumer_lock_held:
            logger.info(
                "process_principal_events_from_kafka: Another worker is already running the Kafka consumer. "
                "Skipping this cycle to avoid consumer group rebalance thrash."
            )
            return

        consumer = KafkaConsumer(topic, **kafka_config)
        logger.info("process_principal_events_from_kafka: Connected to Kafka, subscribed to topic: %s", topic)

        # Process messages
        for message in consumer:
            logger.info(
                "process_principal_events_from_kafka: Processing message from partition %d at offset %d",
                message.partition,
                message.offset,
            )
            result = process_kafka_message(message, bootstrap_service, dlq_producer)
            if not result.should_continue:
                # Lock contention - another listener is running, abort this consumer
                logger.info("process_principal_events_from_kafka: Lock contention detected, aborting consumer.")
                break

            if not result.success:
                logger.warning(
                    "process_principal_events_from_kafka: Message processing failed at offset %d. "
                    "Stopping consumer to preserve at-least-once semantics. "
                    "Consumer will retry from this offset on restart.",
                    message.offset,
                )
                break

            try:
                consumer.commit()
                logger.debug(
                    "process_principal_events_from_kafka: Committed offset %d for partition %d",
                    message.offset,
                    message.partition,
                )
            except Exception as commit_error:
                logger.error(
                    "process_principal_events_from_kafka: Failed to commit offset %d: %s",
                    message.offset,
                    commit_error,
                )
                break

    except KafkaError as e:
        logger.error("process_principal_events_from_kafka: Kafka error: %s", str(e))
        capture_exception(e)
    finally:
        if consumer is not None:
            try:
                consumer.close()
                logger.info("process_principal_events_from_kafka: Kafka consumer closed.")
            except Exception as e:
                logger.error("process_principal_events_from_kafka: Error closing consumer: %s", str(e))
        if consumer_lock_held:
            _release_kafka_consumer_lock()
        logger.info("process_principal_events_from_kafka: Principal event processing finished.")


def _lock_listener() -> bool:
    """Attempt to acquire a lock for the listener and if acquired return True, else False."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s);", [LOCK_ID])
        result = cursor.fetchone()
    if result is None:
        raise Exception("Advisory lock returned none, expected bool.")
    return result[0]  # Returns True if lock acquired, False otherwise


def _try_acquire_kafka_consumer_lock() -> bool:
    """
    Attempt to acquire session-level advisory lock for Kafka consumer construction.

    Uses a session-level lock (not transaction-level) so it can span the entire consume loop.
    Returns True if lock acquired, False if another worker already holds it.
    Must call _release_kafka_consumer_lock() in finally block when done.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s);", [KAFKA_CONSUMER_LOCK_ID])
        result = cursor.fetchone()
    if result is None:
        raise Exception("Advisory lock returned none, expected bool.")
    return result[0]  # Returns True if lock acquired, False otherwise


def _release_kafka_consumer_lock():
    """Release the session-level advisory lock for Kafka consumer."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s);", [KAFKA_CONSUMER_LOCK_ID])
        result = cursor.fetchone()
    if result is None:
        raise Exception("Advisory unlock returned none, expected bool.")
    if not result[0]:
        logger.warning("_release_kafka_consumer_lock: Failed to release lock (was it held?)")
