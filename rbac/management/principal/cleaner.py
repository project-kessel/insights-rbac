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

import json
import logging
import os
import ssl
from typing import NamedTuple, Optional
from xml.parsers.expat import ExpatError

import xmltodict
from core.kafka import RBACProducer
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
from stompest.config import StompConfig
from stompest.error import StompConnectionError
from stompest.protocol import StompSpec
from stompest.sync import Stomp

from api.models import Tenant, User

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

PROXY = PrincipalProxy()  # pylint: disable=invalid-name

# Location of the CA, certificate and key files as defined in the
# "it-umb-key-pair" secret and the "umb-certificates" volume mount.
CA_LOC = "/opt/rbac/rbac/management/principal/umb_certificates/ca.crt"
CERT_LOC = "/opt/rbac/rbac/management/principal/umb_certificates/tls.crt"
KEY_LOC = "/opt/rbac/rbac/management/principal/umb_certificates/tls.key"


LOCK_ID = 42  # For Keith, with Love

# UMB Metric Messages
METRIC_STOMP_MESSAGES_ACK_TOTAL = "stomp_messages_ack_total"
METRIC_STOMP_MESSAGES_NACK_TOTAL = "stomp_messages_nack_total"
stomp_messages_ack_total = Counter(
    METRIC_STOMP_MESSAGES_ACK_TOTAL,
    "Number of stomp UMB messages processed",
)
stomp_messages_nack_total = Counter(
    METRIC_STOMP_MESSAGES_NACK_TOTAL,
    "Number of stomp UMB messages that failed to be processed",
)

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

# KAFKA Shadow Mode Metrics
METRIC_KAFKA_DRY_RUN_MESSAGES_TOTAL = "kafka_dry_run_messages_total"
METRIC_KAFKA_DRY_RUN_ERRORS_TOTAL = "kafka_dry_run_errors_total"
kafka_dry_run_messages_total = Counter(
    METRIC_KAFKA_DRY_RUN_MESSAGES_TOTAL,
    "Number of Kafka messages processed in dry-run/shadow mode",
)
kafka_dry_run_errors_total = Counter(
    METRIC_KAFKA_DRY_RUN_ERRORS_TOTAL,
    "Number of Kafka messages that would have failed if not in dry-run mode",
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


ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
# Cert verification of IT host is failing complains about self-signed cert
# Since hot umb host it is within Red Hat network, we can trust the host
ssl_context.verify_mode = ssl.CERT_NONE
if os.path.isfile(CERT_LOC):
    ssl_context.load_cert_chain(certfile=CERT_LOC, keyfile=KEY_LOC)

# Load the CA's certificate in the context.
if os.path.isfile(CA_LOC):
    ssl_context.load_verify_locations(cafile=CA_LOC)

CONFIG = StompConfig(
    f"ssl://{settings.UMB_HOST}:{settings.UMB_PORT}", sslContext=ssl_context, version=StompSpec.VERSION_1_2
)
QUEUE = f"/queue/Consumer.{settings.SA_NAME}.users-subscription.VirtualTopic.canonical.user"
UMB_CLIENT = Stomp(CONFIG)


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
        # BUG FIX: Original UMB code had break statements that would only extract org_id OR account,
        # not both. Removing breaks to extract both values when present in the message.
        # Users typically have both org_id and account, and messages often contain both references.
        for ref in refs:
            if ref["@system"] == "WEB" and ref["@entity-name"] == "Customer" and ref["@qualifier"] == "id":
                user.org_id = ref["#text"]
            if ref["@system"] == "EBS" and ref["@entity-name"] == "Account" and ref["@qualifier"] == "number":
                user.account = ref["#text"]

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

        # BUG FIX: Original UMB code had break statements that would only extract org_id OR account,
        # not both. Removing breaks to extract both values when present in the message.
        # Users typically have both org_id and account, and messages often contain both references.
        for ref in references:
            is_web_customer = (
                ref.get("system") == "WEB" and ref.get("entity-name") == "Customer" and ref.get("qualifier") == "id"
            )
            is_ebs_account = (
                ref.get("system") == "EBS" and ref.get("entity-name") == "Account" and ref.get("qualifier") == "number"
            )
            if is_web_customer:
                user.org_id = ref.get("text") or ref.get("value")
            if is_ebs_account:
                user.account = ref.get("text") or ref.get("value")

        return user

    user_data = bop_resp["data"][0]
    return external_principal_to_user(user_data)


def process_umb_event(frame, umb_client: Stomp, bootstrap_service: TenantBootstrapService) -> bool:
    """
    Process each umb frame.

    If the process should continue to listen for more frames, return True. Otherwise, return False.
    """
    with transaction.atomic():
        # This is locked per transaction to ensure another listener process does not run concurrently.
        if not _lock_listener():
            # If there is another listener, let it run and abort this one.
            logger.info("process_umb_event: Another listener is running. Aborting.")
            return False

        try:
            body = frame.body.decode("utf-8", errors="ignore")
            data_dict = xmltodict.parse(body)
            canonical_message = data_dict.get("CanonicalMessage")

            user = retrieve_user_info_umb(canonical_message)
            # By default, only process disabled users.
            # If the setting is enabled, process all users.
            if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_UMB:
                # If Tenant is not already ready, don't ready it
                bootstrap_service.update_user(user, ready_tenant=False)
            umb_client.ack(frame)
            stomp_messages_ack_total.inc()
        except Exception as e:
            logger.error("process_umb_event: Error processing umb message : %s", str(e))
            capture_exception(e)
            # Nack sends back to the broker that we failed to process this message.
            # The broker may redeliver the message up to a certain number of retries.
            # Eventually, the message is discarded, usually logged and sent to a DLQ.
            # In other words, nacking is appropriate for messages which *may* be processable
            # if retried.
            # Either way, this lets us eventually proceed further in the queue,
            # and should mark the message so it can be debugged later if needed.
            umb_client.nack(frame)
            stomp_messages_nack_total.inc()

    return True


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
    message, bootstrap_service: TenantBootstrapService, dlq_producer=None, dry_run: bool = False
) -> MessageProcessingResult:
    """
    Process each Kafka message.

    Args:
        message: Kafka message containing user event data
        bootstrap_service: Service for updating user/tenant state
        dlq_producer: Optional RBACProducer instance for sending failed messages to DLQ
        dry_run: If True, validate message but don't write to database (shadow mode)

    Returns:
        MessageProcessingResult with:
        - should_continue: False if another listener is running (lock contention), True otherwise
        - success: True if message was processed successfully, False if it failed
    """
    # Track error for DLQ send outside transaction (to avoid holding DB lock during network I/O)
    send_to_dlq = False
    error_for_dlq = None

    with transaction.atomic():
        # This is locked per transaction to ensure another listener process does not run concurrently.
        if not _lock_listener():
            # If there is another listener, let it run and abort this one.
            logger.info("process_kafka_message: Another listener is running. Aborting.")
            return MessageProcessingResult(should_continue=False, success=False)

        try:
            # Parse message - handle both XML (from UMB bridge) and JSON (from native Kafka producer)
            # The messaging bridge copies raw UMB message bodies (XML) to Kafka during migration
            message_value = message.value.decode("utf-8") if isinstance(message.value, bytes) else message.value

            # Detect format: XML starts with '<' or '<?xml', JSON starts with '{' or '['
            message_value_stripped = message_value.strip()
            if message_value_stripped.startswith("<"):
                # XML format (from UMB bridge) - parse same as UMB consumer
                data_dict = xmltodict.parse(message_value)
                canonical_message = data_dict.get("CanonicalMessage")
                # Use UMB retrieval logic for XML-parsed messages (handles @ and #text attributes)
                user = retrieve_user_info_umb(canonical_message)
            else:
                # JSON format (native Kafka producer) - parse as JSON
                message_data = json.loads(message_value)
                canonical_message = message_data.get("CanonicalMessage", message_data)
                # Use Kafka retrieval logic for JSON messages (plain keys, no @ or # prefixes)
                user = retrieve_user_info_kafka(canonical_message)

            if dry_run:
                # DRY RUN MODE: Validate message structure and log what would happen
                logger.info(
                    "DRY RUN: Would process user_id=%s org_id=%s is_active=%s",
                    user.user_id,
                    user.org_id,
                    user.is_active,
                )

                # Validate message structure is correct but DON'T call update_user (no DB writes)
                if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA:
                    logger.info(
                        "DRY RUN: Would call bootstrap_service.update_user() for user %s",
                        user.username,
                    )

                kafka_messages_success_total.inc()
                kafka_dry_run_messages_total.inc()
                return MessageProcessingResult(should_continue=True, success=True)

            else:
                # NORMAL MODE: Actually update the database
                # By default, only process disabled users.
                # If the setting is enabled, process all users.
                if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA:
                    # If Tenant is not already ready, don't ready it
                    bootstrap_service.update_user(user, ready_tenant=False)

                kafka_messages_success_total.inc()
                return MessageProcessingResult(should_continue=True, success=True)
        except Exception as e:
            mode_msg = " (DRY RUN)" if dry_run else ""
            logger.error(f"process_kafka_message: Error processing Kafka message{mode_msg}: %s", str(e))
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

            if dry_run:
                # In dry-run, track errors but ALWAYS continue processing (never block)
                kafka_dry_run_errors_total.inc()
                if is_permanent_error:
                    logger.warning("DRY RUN: Permanent error detected - message would be sent to DLQ in production.")
                else:
                    logger.warning("DRY RUN: Transient error detected - message would be retried in production.")
                # In dry-run mode, always commit offset to continue validation (never block on any errors)
                return MessageProcessingResult(should_continue=True, success=True)

            # For production mode, only send permanent errors to DLQ
            # Transient errors will not commit offset and will retry
            if not is_permanent_error:
                logger.warning(
                    "process_kafka_message: Transient error at offset %d. "
                    "Will not commit offset - message will be retried on next consumer run.",
                    message.offset,
                )
                return MessageProcessingResult(should_continue=True, success=False)

            # Permanent error - mark for DLQ send outside transaction (to avoid holding DB lock)
            if dlq_producer and hasattr(settings, "KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC"):
                dlq_topic = settings.KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC
                if dlq_topic:
                    send_to_dlq = True
                    error_for_dlq = e
                    # Transaction will commit, then DLQ send happens outside
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

    # Transaction and advisory lock are now released - safe to do network I/O
    if send_to_dlq and error_for_dlq:
        dlq_topic = settings.KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC
        try:
            # Build DLQ message with error context
            # NOTE: original_message may contain PII (usernames, org/account IDs)
            # Ensure DLQ topic has appropriate access controls and retention policy
            dlq_message = {
                "original_message": (
                    message.value.decode("utf-8") if isinstance(message.value, bytes) else message.value
                ),
                "error": str(error_for_dlq),
                "error_type": type(error_for_dlq).__name__,
                "partition": message.partition,
                "offset": message.offset,
                "timestamp": message.timestamp,
                "dry_run": dry_run,
            }
            dlq_producer.send_kafka_message(dlq_topic, dlq_message)
            logger.info(
                "process_kafka_message: Sent failed message to DLQ topic %s (partition=%d, offset=%d)",
                dlq_topic,
                message.partition,
                message.offset,
            )
            # Return success=True so offset gets committed (message moved to DLQ)
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


def process_principal_events_from_umb(bootstrap_service: Optional[TenantBootstrapService] = None):
    """Process principals events from UMB."""
    logger.info("process_tenant_principal_events: Start processing principal events from umb.")
    bootstrap_service = bootstrap_service or get_tenant_bootstrap_service(OutboxReplicator())
    try:
        # 1.1 or greater is required to support NACK, used when messages fail.
        UMB_CLIENT.connect(versions=[StompSpec.VERSION_1_1, StompSpec.VERSION_1_2])
        # We only have one subscription for this connection, so using a static ID header.
        UMB_CLIENT.subscribe(QUEUE, {StompSpec.ACK_HEADER: StompSpec.ACK_CLIENT_INDIVIDUAL, StompSpec.ID_HEADER: "0"})
    except StompConnectionError as e:
        # Skip if already connected/subscribed
        if not str(e).startswith(("Already connected", "Already subscribed")):
            raise e

    try:
        while UMB_CLIENT.canRead(15):  # Check if queue is empty, 15 sec timeout
            frame = UMB_CLIENT.receiveFrame()
            logger.info("process_tenant_principal_events: Processing frame. info=%s", frame.info())
            if not process_umb_event(frame, UMB_CLIENT, bootstrap_service):
                break
    finally:
        UMB_CLIENT.disconnect()
        logger.info("process_tenant_principal_events: Principal event processing finished.")


def process_principal_events_from_kafka(
    bootstrap_service: Optional[TenantBootstrapService] = None, dry_run: bool = False
):
    """
    Process principal events from Kafka.

    Args:
        bootstrap_service: Service for tenant/user operations
        dry_run: If True, process messages but don't write to database (shadow mode)
    """
    mode_msg = " (DRY RUN - SHADOW MODE)" if dry_run else ""
    logger.info(f"process_principal_events_from_kafka: Start processing principal events from Kafka{mode_msg}.")

    if dry_run:
        logger.warning(
            "KAFKA SHADOW MODE: Messages will be processed but NO database writes will occur. "
            "This is for validation only."
        )
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
    kafka_config = {
        "bootstrap_servers": settings.KAFKA_SERVERS,
        "group_id": f"{settings.SA_NAME}-principal-cleanup",
        "auto_offset_reset": "earliest",
        "enable_auto_commit": False,  # Manual commit for at-least-once semantics
        "value_deserializer": lambda m: m.decode("utf-8"),
        "consumer_timeout_ms": 15000,  # 15 second timeout per run, matches UMB behavior
    }

    # Add authentication if configured
    kafka_auth = getattr(settings, "KAFKA_AUTH", None)
    if kafka_auth:
        kafka_config.update(kafka_auth)

    # Initialize consumer to None to avoid UnboundLocalError in finally block
    consumer = None

    # Initialize DLQ producer if DLQ topic is configured
    dlq_topic = getattr(settings, "KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC", None)
    dlq_producer = None
    if dlq_topic:
        try:
            dlq_producer = RBACProducer()
            logger.info("process_principal_events_from_kafka: DLQ producer initialized for topic: %s", dlq_topic)
        except Exception as e:
            logger.warning(
                "process_principal_events_from_kafka: Failed to initialize DLQ producer: %s. "
                "Failed messages will be retried instead of sent to DLQ.",
                str(e),
            )

    try:
        consumer = KafkaConsumer(topic, **kafka_config)
        logger.info("process_principal_events_from_kafka: Connected to Kafka, subscribed to topic: %s", topic)

        # Process messages
        for message in consumer:
            mode_suffix = " (DRY RUN)" if dry_run else ""
            logger.info(
                "process_principal_events_from_kafka: Processing message from partition %d at offset %d%s",
                message.partition,
                message.offset,
                mode_suffix,
            )
            result = process_kafka_message(message, bootstrap_service, dlq_producer, dry_run=dry_run)
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
        logger.info("process_principal_events_from_kafka: Principal event processing finished.")


def _lock_listener() -> bool:
    """Attempt to acquire a lock for the listener and if acquired return True, else False."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s);", [LOCK_ID])
        result = cursor.fetchone()
    if result is None:
        raise Exception("Advisory lock returned none, expected bool.")
    return result[0]  # Returns True if lock acquired, False otherwise
