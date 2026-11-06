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

"""Notification handlers of object change."""

import json
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Optional
from uuid import uuid4

from core.kafka import RBACProducer
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from management.role.v2_model import RoleV2

EVENT_TYPE_RH_TAM_REQUEST_CREATED = "rh-new-tam-request-created"

# When set to True, suppresses RH system notifications (e.g. during seeding runs).
skip_rh_notifications: ContextVar[bool] = ContextVar("skip_rh_notifications", default=False)

# Collects the RH system notification events of one seeding run so they can be sent by a single rate-limited task.
rh_notification_collector: ContextVar[Optional[list]] = ContextVar("rh_notification_collector", default=None)

RH_NOTIFICATIONS_CHAIN_LOCK_KEY = "rh-notifications:active-chain"
RH_NOTIFICATIONS_PENDING_EVENTS_KEY = "rh-notifications:pending-events"
# Holds the events and tenant cursor of a chain whose continuation could not be published.
RH_NOTIFICATIONS_RESUME_KEY = "rh-notifications:resume"
RH_NOTIFICATIONS_COORDINATION_LOCK_KEY = "rh-notifications:coordination-lock"
RH_NOTIFICATIONS_COORDINATION_LOCK_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name
noto_producer = RBACProducer()
noto_topic = settings.NOTIFICATIONS_TOPIC
with open(os.path.join(settings.BASE_DIR, "management", "notifications", "message_template.json")) as template:
    message_template = json.load(template)


def build_notifications_message(event_type, payload, org_id=None):
    """Create message based on template."""
    message = message_template
    message["org_id"] = org_id
    message["event_type"] = event_type
    message["timestamp"] = datetime.now().isoformat()
    message["events"][0]["payload"] = payload
    return message


def notify(event_type, payload, org_id=None):
    """Actually send notifications message. Returns True if the message was handed to the producer."""
    noto_message = build_notifications_message(event_type, payload, org_id)
    noto_headers = [("rh-message-id", str(uuid4()).encode("utf-8"))]
    return noto_producer.send_kafka_message(noto_topic, noto_message, noto_headers)


def enqueue_rh_notifications(events):
    """Queue events for the active chain, or start a chain when none is active."""
    from management.tasks import (
        RH_NOTIFICATIONS_CHAIN_TIMEOUT_SECONDS,
        RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        send_rh_notifications_in_worker,
    )

    lock_token = _acquire_rh_notifications_coordination_lock()
    if lock_token is None:
        logger.error("Could not queue %d RH system notification event(s); coordination lock unavailable.", len(events))
        return

    try:
        pending_events = cache.get(RH_NOTIFICATIONS_PENDING_EVENTS_KEY, []) or []
        active_chain_id = cache.get(RH_NOTIFICATIONS_CHAIN_LOCK_KEY)
        resume = cache.get(RH_NOTIFICATIONS_RESUME_KEY)
        if active_chain_id or resume:
            pending_events.extend(events)
            cache.set(RH_NOTIFICATIONS_PENDING_EVENTS_KEY, pending_events, timeout=None)
            if active_chain_id:
                logger.info("Queued %d RH notification event(s) behind active chain %s.", len(events), active_chain_id)
                return
            logger.warning("Resuming an interrupted RH notification chain; the new events stay queued behind it.")
            _publish_rh_notifications_chain(
                resume["events"],
                send_rh_notifications_in_worker,
                RH_NOTIFICATIONS_CHAIN_TIMEOUT_SECONDS,
                RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
                resume=resume,
            )
            return

        if pending_events:
            logger.warning(
                "Found %d stranded RH notification event(s) with no active chain; sending them with the new events.",
                len(pending_events),
            )
        _publish_rh_notifications_chain(
            pending_events + events,
            send_rh_notifications_in_worker,
            RH_NOTIFICATIONS_CHAIN_TIMEOUT_SECONDS,
            RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        )
    except Exception:
        logger.exception("Failed to queue %d RH system notification event(s).", len(events))
    finally:
        _release_rh_notifications_coordination_lock(lock_token)


def _acquire_rh_notifications_coordination_lock():
    """Serialize event enqueue and chain handoff, waiting briefly for concurrent operations."""
    token = str(uuid4())
    deadline = time.monotonic() + RH_NOTIFICATIONS_COORDINATION_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            if cache.add(
                RH_NOTIFICATIONS_COORDINATION_LOCK_KEY,
                token,
                timeout=RH_NOTIFICATIONS_COORDINATION_LOCK_TIMEOUT_SECONDS,
            ):
                return token
        except Exception:
            logger.exception("Failed to acquire RH notification coordination lock.")
            return None
        if time.monotonic() >= deadline:
            logger.error("Timed out waiting for RH notification coordination lock.")
            return None
        time.sleep(0.05)


def _release_rh_notifications_coordination_lock(token):
    """Release the coordination lock only when it still belongs to this operation."""
    try:
        if cache.get(RH_NOTIFICATIONS_COORDINATION_LOCK_KEY) == token:
            cache.delete(RH_NOTIFICATIONS_COORDINATION_LOCK_KEY)
    except Exception:
        logger.exception("Failed to release RH notification coordination lock.")


def save_rh_notifications_resume(resume):
    """Keep the events and tenant cursor of a chain whose continuation could not be published."""
    cache.set(RH_NOTIFICATIONS_RESUME_KEY, resume, timeout=None)


def _publish_rh_notifications_chain(events, task, chain_timeout, retry_policy, resume=None):
    """
    Start a chain while holding the coordination lock; keep failed publishes queued.

    With resume, the chain continues an interrupted one from its saved tenant cursor, and the queued pending events
    are left in place for the handoff that follows it.
    """
    chain_id = str(uuid4())
    batch_id = str(uuid4())
    kwargs = {"chain_id": chain_id, "batch_id": batch_id}
    if resume:
        kwargs.update(
            after_tenant_id=resume["after_tenant_id"],
            failed_attempts=resume.get("failed_attempts", 0),
        )
    else:
        cache.set(RH_NOTIFICATIONS_PENDING_EVENTS_KEY, events, timeout=None)
    cache.set(RH_NOTIFICATIONS_CHAIN_LOCK_KEY, chain_id, timeout=chain_timeout)
    try:
        task.apply_async(
            args=[events],
            kwargs=kwargs,
            task_id=batch_id,
            retry=True,
            retry_policy=retry_policy,
        )
    except Exception:
        if cache.get(RH_NOTIFICATIONS_CHAIN_LOCK_KEY) == chain_id:
            cache.delete(RH_NOTIFICATIONS_CHAIN_LOCK_KEY)
        logger.exception("Failed to enqueue %d RH system notification event(s).", len(events))
        return False

    if resume:
        cache.delete(RH_NOTIFICATIONS_RESUME_KEY)
    else:
        cache.delete(RH_NOTIFICATIONS_PENDING_EVENTS_KEY)
    logger.info("Enqueued %d RH system notification event(s) for sending.", len(events))
    return True


def finish_rh_notifications_chain(chain_id):
    """Drain queued events into a new chain before releasing the active-chain marker."""
    from management.tasks import (
        RH_NOTIFICATIONS_CHAIN_TIMEOUT_SECONDS,
        RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        send_rh_notifications_in_worker,
    )

    lock_token = _acquire_rh_notifications_coordination_lock()
    if lock_token is None:
        logger.error("Could not finish RH notification chain %s; coordination lock unavailable.", chain_id)
        return
    try:
        if cache.get(RH_NOTIFICATIONS_CHAIN_LOCK_KEY) != chain_id:
            return
        resume = cache.get(RH_NOTIFICATIONS_RESUME_KEY)
        pending_events = cache.get(RH_NOTIFICATIONS_PENDING_EVENTS_KEY, []) or []
        if resume:
            logger.warning(
                "Resuming an interrupted RH notification chain after tenant id %d.", resume["after_tenant_id"]
            )
            _publish_rh_notifications_chain(
                resume["events"],
                send_rh_notifications_in_worker,
                RH_NOTIFICATIONS_CHAIN_TIMEOUT_SECONDS,
                RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
                resume=resume,
            )
        elif pending_events:
            logger.info("Starting next RH notification chain with %d queued event(s).", len(pending_events))
            _publish_rh_notifications_chain(
                pending_events,
                send_rh_notifications_in_worker,
                RH_NOTIFICATIONS_CHAIN_TIMEOUT_SECONDS,
                RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
            )
        else:
            cache.delete(RH_NOTIFICATIONS_CHAIN_LOCK_KEY)
    except Exception:
        logger.exception("Failed to finish RH notification chain %s.", chain_id)
    finally:
        _release_rh_notifications_coordination_lock(lock_token)


def queue_rh_notification(event_type, payload):
    """Queue an RH system notification for every tenant once the current transaction commits."""
    collector = rh_notification_collector.get()

    def register():
        event = {"event_type": event_type, "payload": payload}
        if collector is None:
            enqueue_rh_notifications([event])
        else:
            collector.append(event)

    transaction.on_commit(register)


@contextmanager
def collect_rh_notifications():
    """Collect RH system notifications of a seeding run and enqueue them in one task when the run ends."""
    events = []
    token = rh_notification_collector.set(events)
    try:
        yield
    finally:
        rh_notification_collector.reset(token)
        if events:
            enqueue_rh_notifications(events)


def _rh_notifications_suppressed() -> bool:
    """Return True if RH system notifications should not be sent."""
    return not settings.NOTIFICATIONS_RH_ENABLED or skip_rh_notifications.get()


def handle_system_role_change_notification(role_obj, operation):
    """Signal handler for sending notification message when system Role object changes."""
    if _rh_notifications_suppressed():
        return

    payload = payload_builder("Red Hat", role_obj)
    # Role created
    if operation == "created":
        event_type = "rh-new-role-available"
    # Role updated (including access/resourceDefinition update)
    elif operation == "updated":
        if role_obj.platform_default:
            event_type = "rh-platform-default-role-updated"
        else:
            event_type = "rh-non-platform-default-role-updated"
    else:
        raise Exception("Not recognized operation for updating Red Hat managed group.")

    queue_rh_notification(event_type, payload)


def role_obj_change_notification_handler(role_obj, operation, user=None):
    """Signal handler for sending notification message when Role object changes."""
    if not settings.NOTIFICATIONS_ENABLED:
        return

    if role_obj.system:
        handle_system_role_change_notification(role_obj, operation)
        return

    org_id = user.org_id
    payload = payload_builder(user.username, role_obj)
    # Role created
    if operation == "created":
        event_type = "custom-role-created"
    # Role deleted
    elif operation == "deleted":
        event_type = "custom-role-deleted"
    # Role updated (including access/resourceDefinition update)
    elif operation == "updated":
        event_type = "custom-role-updated"

    notify(event_type, payload, org_id)


def custom_v2_role_obj_change_notification_handler(role_obj: RoleV2, operation: str, user):
    """Signal handler for setting notification message when RoleV2 object changes."""
    if not settings.NOTIFICATIONS_ENABLED:
        return

    if role_obj.type != RoleV2.Types.CUSTOM:
        raise ValueError("Notifications are supported only for custom V2 roles")

    org_id = user.org_id
    payload = payload_builder(user.username, role_obj)

    event_type = {
        "created": "custom-v2-role-created",
        "deleted": "custom-v2-role-deleted",
        "updated": "custom-v2-role-updated",
    }.get(operation)

    if event_type is None:
        raise ValueError(f"Invalid operation: {operation!r}")

    notify(event_type, payload, org_id)


def group_obj_change_notification_handler(user, group_obj, operation):
    """Signal handler for sending notification message when Group object changes."""
    if not settings.NOTIFICATIONS_ENABLED:
        return
    org_id = user.org_id
    payload = payload_builder(user.username, group_obj)
    # Group created
    if operation == "created":
        if not group_obj.system:
            event_type = "group-created"
    # Group deleted
    elif operation == "deleted":
        if not group_obj.system:
            event_type = "group-deleted"
    # Group updated
    else:
        event_type = "group-updated"
    notify(event_type, payload, org_id)


def handle_platform_group_role_change_notification(group_obj, role_obj, operation):
    """Signal handler for sending notification message when roles of platform group changes."""
    if _rh_notifications_suppressed():
        return
    payload = payload_builder("Red Hat", group_obj, extra_info=("role", role_obj))

    if operation == "added":
        event_type = "rh-new-role-added-to-default-access"
    elif operation == "removed":
        event_type = "rh-role-removed-from-default-access"
    else:
        raise Exception("Not recognized operation for updating Red Hat managed platform default access group.")

    queue_rh_notification(event_type, payload)


def group_role_change_notification_handler(user, group_obj, role_obj, operation):
    """Signal handler for sending notification message when role of group changes."""
    if not settings.NOTIFICATIONS_ENABLED:
        return

    # Handle Red Hat managed platform group

    if (group_obj.platform_default or group_obj.admin_default) and group_obj.system:
        handle_platform_group_role_change_notification(group_obj, role_obj, operation)
        return

    # Handle custom group
    org_id = user.org_id
    payload = payload_builder(user.username, group_obj, operation, ("role", role_obj))

    if group_obj.platform_default:
        event_type = "custom-default-access-updated"
    else:
        event_type = "group-updated"

    notify(event_type, payload, org_id)


def group_principal_change_notification_handler(user, group_obj, principal, operation):
    """Signal handler for sending notification message when principal of group changes."""
    if not settings.NOTIFICATIONS_ENABLED:
        return

    org_id = user.org_id
    payload = payload_builder(user.username, group_obj, operation, ("principal", principal))

    event_type = "group-updated"
    notify(event_type, payload, org_id)


def group_flag_change_notification_handler(user, group_obj):
    """Signal handler for sending notification message when flag of group changes."""
    if not settings.NOTIFICATIONS_ENABLED:
        return
    org_id = user.org_id
    payload = payload_builder(user.username, group_obj)

    event_type = "platform-default-group-turned-into-custom"

    notify(event_type, payload, org_id)


def payload_builder(username, resource_obj, operation=None, extra_info=None):
    """Payload builder for notifications message."""
    payload = {"username": username, "name": resource_obj.name, "uuid": str(resource_obj.uuid)}
    if operation:
        payload["operation"] = operation

    if extra_info:
        if extra_info[0] == "role":
            payload["role"] = {"name": extra_info[1].name, "uuid": str(extra_info[1].uuid)}
        elif extra_info[0] == "principal":
            payload["principal"] = extra_info[1]
        else:
            raise Exception(f"Unknown extra_info {extra_info[0]}, valid ones are role/principal")

    return payload


def cross_account_access_handler(cross_request, request_user):
    """Signal handler for sending notification message when cross access request created."""
    if not settings.NOTIFICATIONS_ENABLED:
        return

    org_id = cross_request.target_org

    payload = {
        "username": request_user.username,
        "request_id": str(cross_request.request_id),
    }

    notify(EVENT_TYPE_RH_TAM_REQUEST_CREATED, payload, org_id)
