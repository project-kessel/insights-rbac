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
from typing import Optional

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


def retrieve_user_info(message) -> User:
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

    logger.debug("retrieve_user_info: Processing message with instance_id=%s", instance_id)

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


def process_kafka_message(message, bootstrap_service: TenantBootstrapService) -> tuple[bool, bool]:
    """
    Process each Kafka message.

    Args:
        message: Kafka message containing user event data
        bootstrap_service: Service for updating user/tenant state

    Returns:
        tuple[bool, bool]: (should_continue, success)
        - should_continue: False if another listener is running (lock contention), True otherwise
        - success: True if message was processed successfully, False if it failed
    """
    with transaction.atomic():
        # This is locked per transaction to ensure another listener process does not run concurrently.
        if not _lock_listener():
            # If there is another listener, let it run and abort this one.
            logger.info("process_kafka_message: Another listener is running. Aborting.")
            return (False, False)

        try:
            # Parse JSON message
            message_data = json.loads(message.value)
            canonical_message = message_data.get("CanonicalMessage", message_data)

            user = retrieve_user_info(canonical_message)
            # By default, only process disabled users.
            # If the setting is enabled, process all users.
            if not user.is_active or settings.PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA:
                # If Tenant is not already ready, don't ready it
                bootstrap_service.update_user(user, ready_tenant=False)

            kafka_messages_success_total.inc()
            return (True, True)  # Continue processing, message succeeded
        except Exception as e:
            logger.error("process_kafka_message: Error processing Kafka message: %s", str(e))
            capture_exception(e)
            kafka_messages_failure_total.inc()
            # Continue processing next message, but mark this one as failed
            # Failed message offset will not be committed, so it can be retried on restart
            return (True, False)  # Continue processing, message failed


def process_principal_events_from_kafka(bootstrap_service: Optional[TenantBootstrapService] = None):
    """Process principal events from Kafka."""
    logger.info("process_principal_events_from_kafka: Start processing principal events from Kafka.")
    bootstrap_service = bootstrap_service or get_tenant_bootstrap_service(OutboxReplicator())

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

    # Get topic name from settings
    topic = settings.KAFKA_PRINCIPAL_CLEANUP_TOPIC

    # Initialize consumer to None to avoid UnboundLocalError in finally block
    consumer = None

    try:
        consumer = KafkaConsumer(topic, **kafka_config)
        logger.info("process_principal_events_from_kafka: Connected to Kafka, subscribed to topic: %s", topic)

        # Process messages
        for message in consumer:
            logger.info(
                "process_principal_events_from_kafka: Processing message from partition %d at offset %d",
                message.partition,
                message.offset,
            )
            should_continue, success = process_kafka_message(message, bootstrap_service)
            if not should_continue:
                # Lock contention - another listener is running, abort this consumer
                logger.info("process_principal_events_from_kafka: Lock contention detected, aborting consumer.")
                break

            if not success:
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
