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
import time
from typing import NamedTuple, Optional
from xml.parsers.expat import ExpatError

import xmltodict
from core.kafka import RBACProducer, get_cluster_config
from django.conf import settings
from django.db import connection
from kafka import KafkaConsumer, TopicPartition
from kafka.errors import KafkaError
from kafka.structs import OffsetAndMetadata
from management.atomic_transactions import run_atomic_with_retry
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


def _extract_user_id_from_xml_canonical(message) -> str:
    """Extract WEB User id from a parsed XML CanonicalMessage."""
    instance_id: Optional[str] = None
    if (header := message.get("Header")) is not None:
        if (id := header.get("InstanceId")) is not None:
            instance_id = id

    identifiers = message["Payload"]["Sync"]["User"]["Identifiers"]
    user_id: Optional[str] = None

    if isinstance((ids := identifiers["Identifier"]), list):
        for id in ids:  # type: ignore
            if id["@system"] == "WEB" and id["@entity-name"] == "User" and id["@qualifier"] == "id":
                user_id = id["#text"]
                break
    else:
        user_id = identifiers["Identifier"]["#text"]

    if user_id is None:
        raise ValueError(f"User id not found in message. instance_id={instance_id}")
    return str(user_id)


def _extract_user_id_from_kafka_canonical(message) -> str:
    """Extract WEB User id from a parsed Kafka JSON CanonicalMessage."""
    instance_id: Optional[str] = None
    if (header := message.get("Header")) is not None:
        if (id := header.get("InstanceId")) is not None:
            instance_id = id

    identifiers = message["Payload"]["Sync"]["User"]["Identifiers"]
    user_id: Optional[str] = None

    identifier_list = identifiers.get("Identifier", [])
    if not isinstance(identifier_list, list):
        identifier_list = [identifier_list]

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
    return str(user_id)


def retrieve_user_info_xml(message, *, skip_bop: bool = False, bop_user_by_id: Optional[dict] = None) -> User:
    """
    Retrieve user info from an XML message.

    Args:
        message: Parsed XML CanonicalMessage dict
        skip_bop: If True, build User from the message payload only (no BOP call).
            Used by Kafka dry-run/shadow mode for fast consume validation.
        bop_user_by_id: Optional prefetched BOP results keyed by user_id (str).
            When provided, skips the per-message BOP call and uses this map
            (missing key = user not found / deleted).

    returns:
        user: User object as of latest known state.
    """
    instance_id: Optional[str] = None

    if (header := message.get("Header")) is not None:
        if (id := header.get("InstanceId")) is not None:
            instance_id = id

    logger.debug("retrieve_user_info_xml: Processing message with instance_id=%s", instance_id)

    message_user = message["Payload"]["Sync"]["User"]
    identifiers = message_user["Identifiers"]
    user_id = _extract_user_id_from_xml_canonical(message)

    if skip_bop:
        return _user_from_xml_message_payload(user_id, message_user, identifiers)

    if bop_user_by_id is not None:
        user_data = bop_user_by_id.get(str(user_id))
        if not user_data:
            return _user_from_xml_message_payload(user_id, message_user, identifiers)
        return external_principal_to_user(user_data)

    bop_resp = PROXY.request_filtered_principals([user_id], options={"query_by": "user_id", "return_id": True})

    if not bop_resp["data"]:  # User has been deleted
        return _user_from_xml_message_payload(user_id, message_user, identifiers)

    user_data = bop_resp["data"][0]
    return external_principal_to_user(user_data)


def _user_from_xml_message_payload(user_id: str, message_user: dict, identifiers: dict) -> User:
    """Build a User from XML message fields when BOP has no data (or is skipped)."""
    user = User()
    user.user_id = user_id
    user.is_active = False
    user.username = message_user["Person"]["Credentials"]["Login"]
    if not isinstance((refs := identifiers["Reference"]), list):
        refs = [identifiers["Reference"]]
    for ref in refs:
        if ref["@system"] == "WEB" and ref["@entity-name"] == "Customer" and ref["@qualifier"] == "id":
            user.org_id = ref["#text"]
            break
        if ref["@system"] == "EBS" and ref["@entity-name"] == "Account" and ref["@qualifier"] == "number":
            user.account = ref["#text"]
            break
    return user


def retrieve_user_info_kafka(message, *, skip_bop: bool = False, bop_user_by_id: Optional[dict] = None) -> User:
    """
    Retrieve user info from the Kafka message.

    Args:
        message: JSON message from Kafka containing user event data
        skip_bop: If True, build User from the message payload only (no BOP call).
            Used by Kafka dry-run/shadow mode for fast consume validation.
        bop_user_by_id: Optional prefetched BOP results keyed by user_id (str).
            When provided, skips the per-message BOP call and uses this map
            (missing key = user not found / deleted).

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
    user_id = _extract_user_id_from_kafka_canonical(message)

    if skip_bop:
        return _user_from_kafka_message_payload(user_id, message_user, identifiers)

    if bop_user_by_id is not None:
        user_data = bop_user_by_id.get(str(user_id))
        if not user_data:
            return _user_from_kafka_message_payload(user_id, message_user, identifiers)
        return external_principal_to_user(user_data)

    # Query BOP for user information
    bop_resp = PROXY.request_filtered_principals([user_id], options={"query_by": "user_id", "return_id": True})

    if not bop_resp["data"]:  # User has been deleted
        return _user_from_kafka_message_payload(user_id, message_user, identifiers)

    user_data = bop_resp["data"][0]
    return external_principal_to_user(user_data)


def _user_from_kafka_message_payload(user_id: str, message_user: dict, identifiers: dict) -> User:
    """Build a User from Kafka message fields when BOP has no data (or is skipped)."""
    user = User()
    user.user_id = user_id
    user.is_active = False
    user.username = message_user["Person"]["Credentials"]["Login"]

    references = identifiers.get("Reference", [])
    if not isinstance(references, list):
        references = [references]

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


class _LockContention(Exception):
    """Raised when the advisory listener lock cannot be acquired."""

    pass


class MessageProcessingResult(NamedTuple):
    """
    Result of processing a Kafka message.

    Attributes:
        should_continue: False if another listener is running (lock contention), True otherwise
        success: True if message was processed successfully, False if it failed
    """

    should_continue: bool
    success: bool


def _parse_kafka_message_to_user(message, *, skip_bop: bool = False, bop_user_by_id: Optional[dict] = None):
    """Parse a Kafka message into a User object.

    Handles tombstones, JSON and XML formats.
    Returns (user, None) on success or (None, 'tombstone') for tombstones.
    Raises on parse failure.

    Args:
        message: Kafka message
        skip_bop: If True, do not call BOP (message-payload User only). Used for dry-run.
        bop_user_by_id: Optional prefetched BOP results keyed by user_id (str).
    """
    if message.value is None:
        return None, "tombstone"

    message_value = message.value.decode("utf-8") if isinstance(message.value, bytes) else message.value

    try:
        message_data = json.loads(message_value)
        canonical_message = message_data.get("CanonicalMessage", message_data)
        return (
            retrieve_user_info_kafka(canonical_message, skip_bop=skip_bop, bop_user_by_id=bop_user_by_id),
            None,
        )
    except json.JSONDecodeError as json_error:
        try:
            data_dict = xmltodict.parse(message_value)
            canonical_message = data_dict.get("CanonicalMessage")
            return (
                retrieve_user_info_xml(canonical_message, skip_bop=skip_bop, bop_user_by_id=bop_user_by_id),
                None,
            )
        except ExpatError as xml_error:
            raise Exception(
                f"Message is neither valid JSON nor valid XML. " f"JSON error: {json_error}. XML error: {xml_error}"
            ) from xml_error


def _try_extract_user_id_from_kafka_message(message) -> Optional[str]:
    """Best-effort user_id extraction for BOP batching. Returns None for tombstones/unparseable."""
    if message.value is None:
        return None

    message_value = message.value.decode("utf-8") if isinstance(message.value, bytes) else message.value

    try:
        message_data = json.loads(message_value)
        canonical_message = message_data.get("CanonicalMessage", message_data)
        return _extract_user_id_from_kafka_canonical(canonical_message)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        try:
            data_dict = xmltodict.parse(message_value)
            canonical_message = data_dict.get("CanonicalMessage")
            if canonical_message is None:
                return None
            return _extract_user_id_from_xml_canonical(canonical_message)
        except Exception:
            return None


def _fetch_bop_users_by_ids(user_ids: list[str]) -> dict:
    """
    Fetch BOP user data for the given user_ids in one call.

    Dedupes while preserving order. Returns a map of user_id (str) -> principal dict
    for users found in BOP. Missing keys mean the user was not returned (deleted/inactive).
    """
    unique_ids = list(dict.fromkeys(user_ids))
    if not unique_ids:
        return {}

    logger.info(
        "process_principal_events_from_kafka: Batch BOP lookup for %d unique user_id(s) (from %d message(s))",
        len(unique_ids),
        len(user_ids),
    )
    bop_resp = PROXY.request_filtered_principals(unique_ids, options={"query_by": "user_id", "return_id": True})

    if "data" not in bop_resp:
        raise KeyError(f"BOP response missing 'data' key: status_code={bop_resp.get('status_code')}")

    result = {}
    for item in bop_resp["data"] or []:
        uid = item.get("user_id")
        if uid is not None:
            result[str(uid)] = item
    return result


def _collect_user_ids_from_kafka_messages(messages) -> list[str]:
    """Collect user_ids from messages for BOP batching (duplicates preserved for logging)."""
    user_ids = []
    for message in messages:
        user_id = _try_extract_user_id_from_kafka_message(message)
        if user_id is not None:
            user_ids.append(user_id)
    return user_ids


def _commit_kafka_message(consumer, message) -> None:
    """
    Commit the offset for a single message.

    Must use an explicit offset: messages are buffered into a batch before processing,
    so the consumer position already points past the whole batch. A bare ``commit()``
    would incorrectly mark unprocessed messages as done.
    """
    tp = TopicPartition(message.topic, message.partition)
    consumer.commit({tp: OffsetAndMetadata(message.offset + 1, "")})


def _process_kafka_message_batch(
    consumer,
    messages,
    bootstrap_service: TenantBootstrapService,
    dlq_producer,
    dry_run: bool,
) -> bool:
    """
    Process a batch of Kafka messages with a single deduped BOP call.

    Prefetches BOP data for unique user_ids in the batch, then processes and commits
    each message in order (each message still runs ``update_user``; only the BOP
    lookup is deduped). Returns False if the consumer should stop.
    """
    if not messages:
        return True

    bop_user_by_id = None
    if not dry_run:
        user_ids = _collect_user_ids_from_kafka_messages(messages)
        try:
            bop_user_by_id = _fetch_bop_users_by_ids(user_ids)
        except Exception as e:
            logger.error(
                "process_principal_events_from_kafka: Batch BOP lookup failed: %s. "
                "Stopping consumer without committing batch offsets.",
                str(e),
            )
            capture_exception(e)
            return False

    for message in messages:
        mode_suffix = " (DRY RUN)" if dry_run else ""
        logger.info(
            "process_principal_events_from_kafka: Processing message from partition %d at offset %d%s",
            message.partition,
            message.offset,
            mode_suffix,
        )
        result = process_kafka_message(
            message,
            bootstrap_service,
            dlq_producer,
            dry_run=dry_run,
            bop_user_by_id=bop_user_by_id,
        )
        if not result.should_continue:
            logger.info("process_principal_events_from_kafka: Lock contention detected, aborting consumer.")
            return False

        if not result.success:
            logger.warning(
                "process_principal_events_from_kafka: Message processing failed at offset %d. "
                "Stopping consumer to preserve at-least-once semantics. "
                "Consumer will retry from this offset on restart.",
                message.offset,
            )
            return False

        try:
            _commit_kafka_message(consumer, message)
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
            return False

    return True


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
    message,
    bootstrap_service: TenantBootstrapService,
    dlq_producer=None,
    dry_run: bool = False,
    bop_user_by_id: Optional[dict] = None,
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
        dry_run: If True, validate message structure and commit without BOP or DB writes (shadow mode)
        bop_user_by_id: Optional prefetched BOP results keyed by user_id (str). When provided
            (including empty dict), skips per-message BOP calls.

    Returns:
        MessageProcessingResult with:
        - should_continue: False if another listener is running (lock contention), True otherwise
        - success: True if message was processed successfully, False if it failed
    """
    # --- 1. Parse message (+ resolve user via BOP unless dry-run / prefetched) ---
    # Live mode calls BOP (network I/O) so can raise transient errors too.
    # Dry-run skips BOP and builds User from the payload only for structural validation.
    # When bop_user_by_id is provided, BOP was already fetched for the batch.
    try:
        user, marker = _parse_kafka_message_to_user(
            message, skip_bop=dry_run, bop_user_by_id=None if dry_run else bop_user_by_id
        )
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

    # --- 3. Dry-run mode: structure validated above (no BOP, no DB writes) ---
    if dry_run:
        logger.debug(
            "DRY RUN: Validated message without BOP (user_id=%s); skipping DB writes",
            getattr(user, "user_id", None),
        )
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
            "KAFKA SHADOW MODE: Messages will be parsed without BOP and with NO database writes. "
            "This is for consume/commit validation only."
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
    # creating a 45-second gap between consumption periods. This matches the historical
    # message-bus consumption pattern. For continuous consumption, a persistent
    # consumer (like launch-rbac-kafka-consumer) would be more appropriate, but this
    # approach maintains compatibility with the existing periodic architecture.

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
        # idle-poll stop; aligned with wall-clock drain so a quiet topic ends the cycle promptly
        "consumer_timeout_ms": settings.KAFKA_PRINCIPAL_CLEANUP_DRAIN_TIMEOUT_MS,
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
        batch_size = settings.KAFKA_PRINCIPAL_CLEANUP_BOP_BATCH_SIZE
        batch = []

        # Process messages in batches so we can dedupe user_ids and make one BOP call per batch.
        for message in consumer:
            if time.monotonic() >= drain_deadline:
                logger.info(
                    "process_principal_events_from_kafka: Drain window of %dms elapsed, stopping cycle.",
                    drain_timeout_ms,
                )
                break

            batch.append(message)
            if len(batch) >= batch_size:
                if not _process_kafka_message_batch(consumer, batch, bootstrap_service, dlq_producer, dry_run=dry_run):
                    batch = []
                    break
                batch = []

        if batch and not _process_kafka_message_batch(
            consumer, batch, bootstrap_service, dlq_producer, dry_run=dry_run
        ):
            logger.info("process_principal_events_from_kafka: Final batch processing stopped the consumer.")

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
