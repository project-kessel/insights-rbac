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
import os
import ssl
import time
from typing import NamedTuple, Optional
from xml.parsers.expat import ExpatError

import xmltodict
from core.kafka import RBACProducer, get_cluster_config
from django.conf import settings
from django.db import connection
from kafka import KafkaConsumer
from kafka.errors import KafkaError
from management.atomic_transactions import run_atomic_with_retry
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
KAFKA_CONSUMER_LOCK_ID = 43  # Guards Kafka consumer construction to prevent multi-worker join thrash

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

# KAFKA Validation Mode Metrics
METRIC_KAFKA_VALIDATION_SUCCESS_TOTAL = "kafka_validation_success_total"
METRIC_KAFKA_VALIDATION_FALLBACK_TOTAL = "kafka_validation_fallback_total"
kafka_validation_success_total = Counter(
    METRIC_KAFKA_VALIDATION_SUCCESS_TOTAL,
    "Number of times Kafka succeeded in validation mode (no UMB fallback needed)",
)
kafka_validation_fallback_total = Counter(
    METRIC_KAFKA_VALIDATION_FALLBACK_TOTAL,
    "Number of times UMB fallback was triggered in validation mode",
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


class _LockContention(Exception):
    """Raised when the advisory listener lock cannot be acquired."""

    pass


def process_umb_event(frame, umb_client: Stomp, bootstrap_service: TenantBootstrapService) -> bool:
    """
    Process each umb frame.

    If the process should continue to listen for more frames, return True. Otherwise, return False.

    Message parsing happens outside the transaction (pure computation).  The DB
    work (advisory lock + update_user) runs inside ``run_atomic_with_retry`` so
    that serialization conflicts with concurrent API traffic are retried at the
    correct (outermost) transaction boundary.
    """
    # --- Parse outside the transaction (no DB needed) ---
    try:
        body = frame.body.decode("utf-8", errors="ignore")
        data_dict = xmltodict.parse(body)
        canonical_message = data_dict.get("CanonicalMessage")
        user = retrieve_user_info_umb(canonical_message)
    except Exception as e:
        logger.error("process_umb_event: Error parsing umb message: %s", str(e))
        capture_exception(e)
        umb_client.nack(frame)
        stomp_messages_nack_total.inc()
        return True

    # --- DB work with proper outer retry ---
    try:

        def _db_work():
            if not _lock_listener():
                raise _LockContention()
            if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_UMB:
                bootstrap_service.update_user(user, ready_tenant=False)

        run_atomic_with_retry(5, _db_work)
        umb_client.ack(frame)
        stomp_messages_ack_total.inc()
    except _LockContention:
        logger.info("process_umb_event: Another listener is running. Aborting.")
        return False
    except Exception as e:
        logger.error("process_umb_event: Error processing umb message: %s", str(e))
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


def _parse_kafka_message_to_user(message):
    """Parse a Kafka message into a User object.

    Handles tombstones, JSON and XML formats.
    Returns (user, None) on success or (None, 'tombstone') for tombstones.
    Raises on parse failure.
    """
    if message.value is None:
        return None, "tombstone"

    message_value = message.value.decode("utf-8") if isinstance(message.value, bytes) else message.value

    try:
        message_data = json.loads(message_value)
        canonical_message = message_data.get("CanonicalMessage", message_data)
        return retrieve_user_info_kafka(canonical_message), None
    except json.JSONDecodeError as json_error:
        try:
            data_dict = xmltodict.parse(message_value)
            canonical_message = data_dict.get("CanonicalMessage")
            return retrieve_user_info_umb(canonical_message), None
        except ExpatError as xml_error:
            raise Exception(
                f"Message is neither valid JSON nor valid XML. " f"JSON error: {json_error}. XML error: {xml_error}"
            ) from xml_error


def _send_to_dlq(message, error, dlq_producer, dry_run) -> MessageProcessingResult:
    """Attempt to send a failed message to DLQ. Returns a MessageProcessingResult."""
    if not dlq_producer or not hasattr(settings, "KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC"):
        logger.warning(
            "process_kafka_message: No DLQ producer configured. "
            "Failed message at offset %d will be retried on restart.",
            message.offset,
        )
        return MessageProcessingResult(should_continue=True, success=dry_run)

    dlq_topic = settings.KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC
    if not dlq_topic:
        logger.warning(
            "process_kafka_message: No DLQ topic configured. "
            "Failed message at offset %d will be retried on restart.",
            message.offset,
        )
        return MessageProcessingResult(should_continue=True, success=dry_run)

    try:
        # NOTE: original_message may contain PII (usernames, org/account IDs)
        # Ensure DLQ topic has appropriate access controls and retention policy
        if isinstance(message.value, bytes):
            try:
                original_message = message.value.decode("utf-8")
                message_encoding = "utf-8"
            except UnicodeDecodeError:
                original_message = base64.b64encode(message.value).decode("ascii")
                message_encoding = "base64"
        else:
            original_message = message.value
            message_encoding = "string"

        dlq_message = {
            "original_message": original_message,
            "message_encoding": message_encoding,
            "error": str(error),
            "error_type": type(error).__name__,
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
        return MessageProcessingResult(should_continue=True, success=True)

    except Exception as dlq_error:
        logger.error(
            "process_kafka_message: Failed to send message to DLQ: %s. " "Message will be retried on restart.",
            str(dlq_error),
        )
        capture_exception(dlq_error)
        if dry_run:
            logger.warning("process_kafka_message: DLQ failure in dry-run mode, committing offset anyway")
            return MessageProcessingResult(should_continue=True, success=True)
        return MessageProcessingResult(should_continue=True, success=False)


def process_kafka_message(
    message, bootstrap_service: TenantBootstrapService, dlq_producer=None, dry_run: bool = False
) -> MessageProcessingResult:
    """
    Process each Kafka message.

    Message parsing runs outside the transaction (pure computation).  The DB
    work (advisory lock + update_user) runs inside ``run_atomic_with_retry`` so
    that serialization conflicts with concurrent API traffic are properly
    retried at the outermost transaction boundary.

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
    # --- 1. Parse message + resolve user outside the transaction ---
    # This calls BOP (network I/O) so can raise transient errors too.
    try:
        user, marker = _parse_kafka_message_to_user(message)
    except Exception as e:
        mode_msg = " (DRY RUN)" if dry_run else ""
        logger.error("process_kafka_message: Error parsing Kafka message%s: %s", mode_msg, str(e))
        capture_exception(e)
        kafka_messages_failure_total.inc()

        is_permanent_error = isinstance(
            e,
            (
                json.JSONDecodeError,  # Malformed JSON
                ExpatError,  # Malformed XML
                KeyError,  # Missing required field in message
                ValueError,  # Invalid data format
                UnicodeDecodeError,  # Invalid message encoding
                AttributeError,  # Wrong message structure
                TypeError,  # Wrong type in message
            ),
        )

        if dry_run:
            kafka_dry_run_errors_total.inc()
            if is_permanent_error:
                return _send_to_dlq(message, e, dlq_producer, dry_run=True)
            else:
                # Transient error in dry-run: commit offset and continue
                logger.warning("DRY RUN: Transient error detected - message would be retried in production.")
                return MessageProcessingResult(should_continue=True, success=True)

        if is_permanent_error:
            return _send_to_dlq(message, e, dlq_producer, dry_run=False)
        else:
            logger.warning(
                "process_kafka_message: Transient error at offset %d. "
                "Will not commit offset - message will be retried on next consumer run.",
                message.offset,
            )
            return MessageProcessingResult(should_continue=True, success=False)

    # --- 2. Tombstone handling (no DB needed) ---
    if marker == "tombstone":
        logger.warning(
            "process_kafka_message: Received tombstone message (null value) at offset %d. "
            "Tombstones are not expected in principal cleanup topic. Skipping.",
            message.offset,
        )
        kafka_messages_success_total.inc()
        return MessageProcessingResult(should_continue=True, success=True)

    # --- 3. Dry-run mode: validate only, no DB writes ---
    if dry_run:
        logger.debug("DRY RUN: Would process user")
        if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA:
            logger.debug("DRY RUN: Would call bootstrap_service.update_user()")
        kafka_messages_success_total.inc()
        kafka_dry_run_messages_total.inc()
        return MessageProcessingResult(should_continue=True, success=True)

    # --- 4. Normal mode: DB work with proper outer retry ---
    try:

        def _db_work():
            if not _lock_listener():
                raise _LockContention()
            if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA:
                bootstrap_service.update_user(user, ready_tenant=False)

        run_atomic_with_retry(5, _db_work)
        kafka_messages_success_total.inc()
        return MessageProcessingResult(should_continue=True, success=True)

    except _LockContention:
        logger.info("process_kafka_message: Another listener is running. Aborting.")
        return MessageProcessingResult(should_continue=False, success=False)

    except Exception as e:
        logger.error("process_kafka_message: Error processing Kafka message: %s", str(e))
        capture_exception(e)
        kafka_messages_failure_total.inc()

        # Determine if this is a permanent error or transient error
        is_permanent_error = isinstance(
            e,
            (
                KeyError,  # Missing required field in message
                ValueError,  # Invalid data format
                AttributeError,  # Wrong message structure
                TypeError,  # Wrong type in message
            ),
        )

        if not is_permanent_error:
            logger.warning(
                "process_kafka_message: Transient error at offset %d. "
                "Will not commit offset - message will be retried on next consumer run.",
                message.offset,
            )
            return MessageProcessingResult(should_continue=True, success=False)

        return _send_to_dlq(message, e, dlq_producer, dry_run=False)


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
            logger.info(
                "process_tenant_principal_events: Processing frame for %s",
                frame.headers.get("esbWebUserId", "unknown"),
            )
            logger.debug("process_tenant_principal_events: Processing frame. info=%s", frame.info())
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
        # idle-poll stop; decoupled from wall-clock drain so a quiet topic releases its
        # worker slot and group membership quickly (default 15 s) without shortening
        # the busy-topic drain budget (default 50 s).
        "consumer_timeout_ms": settings.KAFKA_PRINCIPAL_CLEANUP_CONSUMER_TIMEOUT_MS,
        # Timeout tuning: beat interval + drain must fit in session/max_poll without LeaveGroup
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

        # Wall-clock budget so a busy topic cannot keep this Celery task alive past the drain window.
        # consumer_timeout_ms alone only ends the iterator after idle polls.
        drain_timeout_ms = settings.KAFKA_PRINCIPAL_CLEANUP_DRAIN_TIMEOUT_MS
        drain_deadline = time.monotonic() + (drain_timeout_ms / 1000.0)

        # Process messages
        for message in consumer:
            if time.monotonic() >= drain_deadline:
                logger.info(
                    "process_principal_events_from_kafka: Drain window of %dms elapsed, stopping cycle.",
                    drain_timeout_ms,
                )
                break

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
