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
from contextvars import ContextVar
from datetime import datetime
from uuid import uuid4

from core.kafka import RBACProducer
from django.conf import settings
from management.role.v2_model import RoleV2

from api.models import Tenant

EVENT_TYPE_RH_TAM_REQUEST_CREATED = "rh-new-tam-request-created"

# When set to True, suppresses RH system notifications (e.g. during seeding runs).
skip_rh_notifications: ContextVar[bool] = ContextVar("skip_rh_notifications", default=False)

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
    """Actually send notifications message."""
    noto_message = build_notifications_message(event_type, payload, org_id)
    noto_headers = [("rh-message-id", str(uuid4()).encode("utf-8"))]
    noto_producer.send_kafka_message(noto_topic, noto_message, noto_headers)


def notify_all(event_type, payload):
    """Notify all tenants."""
    # To avoid memory overloaded, use iterator:
    # https://docs.djangoproject.com/en/4.0/ref/models/querysets/#django.db.models.query.QuerySet.iterator
    for tenant in Tenant.objects.exclude(tenant_name="public").filter(ready=True).iterator():
        # Tenant name pattern is acct12345
        notify(event_type, payload, tenant.org_id)


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

    notify_all(event_type, payload)


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

    notify_all(event_type, payload)


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
