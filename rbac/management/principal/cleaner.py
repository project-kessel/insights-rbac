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
import enum
import json
import logging
from typing import NamedTuple, Optional
from xml.parsers.expat import ExpatError

import xmltodict
from core.base_consumer import (
    BaseConsumerMetrics,
    BaseRBACKafkaConsumer,
    CommitConfig,
    RetryConfig,
    make_consumer_metrics,
)
from core.kafka import RBACProducer, get_cluster_config
from django.conf import settings
from django.db import connection
from kafka import TopicPartition
from kafka.consumer.fetcher import ConsumerRecord
from management.atomic_transactions import run_atomic_with_retry
from management.inventory_replicator.outbox_replicator import OutboxReplicator
from management.principal.model import Principal
from management.principal.proxy import PrincipalProxy, external_principal_to_user
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

base_metrics = make_consumer_metrics("rbac_principal_consumer")

# KAFKA Shadow Mode Metrics
METRIC_KAFKA_DRY_RUN_MESSAGES_TOTAL = "rbac_principal_consumer_dry_run_messages_total"
METRIC_KAFKA_DRY_RUN_ERRORS_TOTAL = "rbac_principal_consumer_dry_run_errors_total"
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
        # Existence checks must not apply the default status=enabled filter. On-prem
        # BYPASS enrichment can mark Keycloak-disabled users inactive; those users
        # still exist and must not be deleted (and lose group/role bindings).
        options = {}
        if settings.BYPASS_BOP_VERIFICATION:
            options["status"] = "all"
        resp = PROXY.request_filtered_principals([principal.username], org_id=org_id, options=options)
        status_code = resp.get("status_code")
        data = resp.get("data")
        usernames = []
        if isinstance(data, list):
            usernames = [item.get("username") for item in data if isinstance(item, dict)]
        logger.info(
            "clean_tenant_principals: Response code: %s user_count=%s usernames=%s",
            str(status_code),
            len(data) if isinstance(data, list) else None,
            usernames,
        )
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


class MessageProcessingResult(NamedTuple):
    """
    Result of processing a Kafka message.

    Attributes:
        success: True if message was processed successfully, False if it failed
    """

    success: bool


def _parse_kafka_message_to_user(message, *, skip_bop: bool = False, bop_user_by_id: Optional[dict] = None):
    """Parse a Kafka message into a User object.

    Handles JSON and XML formats.
    Raises on parse failure.

    Args:
        message: Kafka message
        skip_bop: If True, do not call BOP (message-payload User only). Used for dry-run.
        bop_user_by_id: Optional prefetched BOP results keyed by user_id (str).
    """
    message_value = message.value.decode("utf-8") if isinstance(message.value, bytes) else message.value

    try:
        message_data = json.loads(message_value)
        canonical_message = message_data.get("CanonicalMessage", message_data)
        return retrieve_user_info_kafka(canonical_message, skip_bop=skip_bop, bop_user_by_id=bop_user_by_id)
    except json.JSONDecodeError as json_error:
        try:
            data_dict = xmltodict.parse(message_value)
            canonical_message = data_dict.get("CanonicalMessage")
            return retrieve_user_info_xml(canonical_message, skip_bop=skip_bop, bop_user_by_id=bop_user_by_id)
        except ExpatError as xml_error:
            raise Exception(
                f"Message is neither valid JSON nor valid XML. " f"JSON error: {json_error}. XML error: {xml_error}"
            ) from xml_error


def _try_extract_user_id_from_kafka_message(message) -> Optional[str]:
    """Best-effort user_id extraction for BOP batching. Returns None for unparseable."""
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
        "Batch BOP lookup for %d unique user_id(s) (from %d message(s))",
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


class _MetricStatus(enum.StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    DLQ_SENT = "dlq"
    DLQ_ERROR = "dlq_error"
    DLQ_ERROR_IGNORED = "dlq_error_ignored"


def _send_to_dlq(message, error, dlq_producer, dry_run) -> tuple[MessageProcessingResult, _MetricStatus]:
    """Attempt to send a failed message to DLQ. Returns a MessageProcessingResult."""
    if not dlq_producer or not hasattr(settings, "KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC"):
        logger.warning(
            "No DLQ producer configured. Failed message at offset %d will be retried.",
            message.offset,
        )
        return MessageProcessingResult(success=dry_run), _MetricStatus.ERROR

    dlq_topic = settings.KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC
    if not dlq_topic:
        logger.warning(
            "No DLQ topic configured. Failed message at offset %d will be retried.",
            message.offset,
        )
        return MessageProcessingResult(success=dry_run), _MetricStatus.ERROR

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
            "Sent failed message to DLQ topic %s (partition=%d, offset=%d)",
            dlq_topic,
            message.partition,
            message.offset,
        )
        return MessageProcessingResult(success=True), _MetricStatus.DLQ_SENT

    except Exception as dlq_error:
        logger.error(
            "Failed to send message to DLQ: %s. Message will be retried.",
            str(dlq_error),
        )
        capture_exception(dlq_error)

        if dry_run:
            logger.warning("DLQ failure in dry-run mode, committing offset anyway")
            return MessageProcessingResult(success=True), _MetricStatus.DLQ_ERROR_IGNORED

        return MessageProcessingResult(success=False), _MetricStatus.DLQ_ERROR


def _process_kafka_message(
    message,
    *,
    bootstrap_service: TenantBootstrapService,
    dlq_producer=None,
    dry_run: bool = False,
    bop_user_by_id: Optional[dict] = None,
    metrics: BaseConsumerMetrics,
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
        - success: True if message was processed successfully, False if it failed
    """

    def record_status(message_type: str, recorded_status: _MetricStatus):
        metrics.messages_processed_total.labels(message_type=message_type, status=str(recorded_status)).inc()

    if message is None:
        raise TypeError("Tombstone should have been handled in base consumer")

    # --- 1. Parse message (+ resolve user via BOP unless dry-run / prefetched) ---
    # Live mode calls BOP (network I/O) so can raise transient errors too.
    # Dry-run skips BOP and builds User from the payload only for structural validation.
    # When bop_user_by_id is provided, BOP was already fetched for the batch.
    try:
        user = _parse_kafka_message_to_user(
            message, skip_bop=dry_run, bop_user_by_id=None if dry_run else bop_user_by_id
        )
    except Exception as e:
        mode_msg = " (DRY RUN)" if dry_run else ""
        logger.error("Error parsing Kafka message%s: %s", mode_msg, str(e))
        capture_exception(e)

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
                result, dlq_status = _send_to_dlq(message, e, dlq_producer, dry_run=True)
                record_status("unknown", dlq_status)
                return result
            else:
                # Transient error in dry-run: commit offset and continue
                logger.warning("DRY RUN: Transient error detected - message would be retried in production.")
                record_status("unknown", _MetricStatus.ERROR)
                return MessageProcessingResult(success=True)

        if is_permanent_error:
            result, dlq_status = _send_to_dlq(message, e, dlq_producer, dry_run=False)
            record_status("unknown", dlq_status)
            return result
        else:
            logger.warning(
                "Transient error at offset %d. Will not commit offset - message will be retried.",
                message.offset,
            )
            record_status("unknown", _MetricStatus.ERROR)
            return MessageProcessingResult(success=False)

    # --- 2. Dry-run mode: structure validated above (no BOP, no DB writes) ---
    if dry_run:
        logger.debug(
            "DRY RUN: Validated message without BOP (user_id=%s); skipping DB writes",
            getattr(user, "user_id", None),
        )
        record_status("principal", _MetricStatus.SUCCESS)
        kafka_dry_run_messages_total.inc()
        return MessageProcessingResult(success=True)

    # --- 3. Normal mode: DB work with proper outer retry ---
    try:

        def _db_work():
            if not _lock_listener():
                raise RuntimeError("Could not acquire lock")
            if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA:
                bootstrap_service.update_user(user, ready_tenant=False)

        run_atomic_with_retry(5, _db_work)
        record_status("principal", _MetricStatus.SUCCESS)
        return MessageProcessingResult(success=True)

    except Exception as e:
        logger.error("Error processing Kafka message: %s", str(e))
        capture_exception(e)

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
                "Transient error at offset %d. Will not commit offset - message will be retried.",
                message.offset,
            )
            record_status("principal", _MetricStatus.ERROR)
            return MessageProcessingResult(success=False)

        result, dlq_status = _send_to_dlq(message, e, dlq_producer, dry_run=False)
        record_status("principal", dlq_status)
        return result


def _lock_listener() -> bool:
    """Attempt to acquire a lock for the listener and if acquired return True, else False."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s);", [LOCK_ID])
        result = cursor.fetchone()
    if result is None:
        raise Exception("Advisory lock returned none, expected bool.")
    return result[0]  # Returns True if lock acquired, False otherwise


def _env_name() -> str:
    return getattr(settings, "ENV_NAME", "stage")


def _consumer_group_id() -> str:
    # Include ENV_NAME in group_id to prevent offset interference across environments
    # In multi-env setups (staging, ephemeral, CI) that share a Kafka cluster, environments
    # must use distinct consumer groups to avoid message loss and offset conflicts
    return f"{settings.SA_NAME}-{_env_name()}-principal-cleanup"


def _consumer_config() -> dict:
    it_kafka_servers, consumer_auth = get_cluster_config("it_managed", for_consumer=True)

    if not it_kafka_servers or not consumer_auth:
        raise RuntimeError("IT-managed Kafka cluster is not configured (missing bootstrap servers or credentials). ")

    kafka_config = {
        "bootstrap_servers": it_kafka_servers,
        # No value_deserializer - leave as bytes to handle UTF-8 errors
        # idle-poll stop; aligned with wall-clock drain so a quiet topic ends the cycle promptly
        "session_timeout_ms": settings.KAFKA_PRINCIPAL_CLEANUP_SESSION_TIMEOUT_MS,
        "heartbeat_interval_ms": settings.KAFKA_PRINCIPAL_CLEANUP_HEARTBEAT_INTERVAL_MS,
        "max_poll_interval_ms": settings.KAFKA_PRINCIPAL_CLEANUP_MAX_POLL_INTERVAL_MS,
    }

    kafka_config.update(consumer_auth)
    return kafka_config


def _make_dlq_producer() -> Optional[RBACProducer]:
    # Initialize DLQ producer if DLQ topic is configured. The DLQ topic is also on the IT-managed
    # cluster, so the producer must target that profile (not the default Clowder one).
    dlq_topic = getattr(settings, "KAFKA_PRINCIPAL_CLEANUP_DLQ_TOPIC", None)

    if not dlq_topic:
        return None

    try:
        dlq_producer = RBACProducer(cluster="it_managed")
        logger.info("DLQ producer initialized for topic: %s", dlq_topic)

        return dlq_producer
    except Exception as e:
        logger.warning(
            "Failed to initialize DLQ producer: %s. Failed messages will be retried instead of sent to DLQ.",
            str(e),
        )


class PrincipalKafkaConsumer(BaseRBACKafkaConsumer):
    """Consumer for processing user update events."""

    _dry_run: bool

    def __init__(
        self,
        *,
        topic: Optional[str] = None,
        health_check_interval: int = 30,
        retry_config: Optional[RetryConfig] = None,
        commit_config: Optional[CommitConfig] = None,
        bootstrap_service: Optional[TenantBootstrapService] = None,
        dry_run: bool = False,
    ):
        """Initialize the Kafka consumer."""
        if topic is None:
            topic = settings.KAFKA_PRINCIPAL_CLEANUP_TOPIC

        if not topic:
            raise RuntimeError("Topic not configured. Cannot process principal events from Kafka.")

        super().__init__(
            topic=topic,
            group_id=_consumer_group_id(),
            metrics=base_metrics,
            health_check_interval=health_check_interval,
            retry_config=retry_config,
            commit_config=commit_config,
            extra_consumer_config=_consumer_config(),
        )

        self._dry_run = dry_run
        self._bootstrap_service = bootstrap_service or get_tenant_bootstrap_service(OutboxReplicator())
        self._dlq_producer = _make_dlq_producer()

    def _do_handle_partitions_revoked(self, revoked_partitions: list[TopicPartition]):
        pass

    def _do_handle_partitions_assigned(self, assigned_partitions: list[TopicPartition]):
        pass

    def _batch_size(self) -> int:
        return settings.KAFKA_PRINCIPAL_CLEANUP_BOP_BATCH_SIZE

    def _prepare_for_batch(self, batch: tuple[ConsumerRecord, ...]) -> Optional[dict]:
        if self._dry_run:
            return None

        user_ids = _collect_user_ids_from_kafka_messages(list(batch))

        try:
            return _fetch_bop_users_by_ids(user_ids)
        except Exception as e:
            logger.exception("Batch BOP lookup failed.", exc_info=e)
            raise

    def _prepare_for_message(self, message, first_message: bool):
        pass

    def _process_single(self, message: ConsumerRecord, batch_data: Optional[dict]) -> bool:
        if (batch_data is None) and (not self._dry_run):
            raise AssertionError("batch_data should be present when not in dry-run mode")

        result = _process_kafka_message(
            message,
            bootstrap_service=self._bootstrap_service,
            dlq_producer=self._dlq_producer,
            dry_run=self._dry_run,
            bop_user_by_id=batch_data,
            metrics=self.metrics,
        )

        if not result.success:
            return False

        return True
