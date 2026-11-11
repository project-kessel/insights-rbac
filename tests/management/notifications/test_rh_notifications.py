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
"""Tests for the queueing of Red Hat system notifications."""

import json
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
from management.notifications.model import RhNotificationEvent
from management.notifications.notification_handlers import (
    handle_platform_group_role_change_notification,
    handle_system_role_change_notification,
    queue_rh_notification,
    skip_rh_notifications,
)
from tests.identity_request import IdentityRequest
from tests.management.notifications.utils import collected_rh_events

NOTIFY = "management.notifications.notification_handlers.notify"


def make_role(name="role", platform_default=False):
    """Return a stand-in system role object."""
    role = MagicMock()
    role.name = name
    role.uuid = "00000000-0000-0000-0000-000000000001"
    role.platform_default = platform_default
    role.system = True
    return role


@override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_RH_ENABLED=True)
class RhNotificationQueueTests(IdentityRequest):
    """Test that the RH handlers save events in the caller's transaction instead of sending them."""

    def test_event_is_saved_in_the_callers_transaction(self):
        """The handler stores the event as a row, without sending anything."""
        with patch(NOTIFY) as notify, collected_rh_events() as events:
            with transaction.atomic():
                handle_system_role_change_notification(make_role("new"), "created")

        notify.assert_not_called()
        self.assertEqual(
            events,
            [
                {
                    "event_type": "rh-new-role-available",
                    "payload": {
                        "username": "Red Hat",
                        "name": "new",
                        "uuid": "00000000-0000-0000-0000-000000000001",
                    },
                }
            ],
        )
        self.assertIsNone(RhNotificationEvent.objects.get().delivery)

    def test_event_types_are_unchanged(self):
        """The five RH event types are still produced."""
        group = make_role("group")
        with collected_rh_events() as events:
            handle_system_role_change_notification(make_role(platform_default=False), "created")
            handle_system_role_change_notification(make_role(platform_default=True), "updated")
            handle_system_role_change_notification(make_role(platform_default=False), "updated")
            handle_platform_group_role_change_notification(group, make_role(), "added")
            handle_platform_group_role_change_notification(group, make_role(), "removed")

        self.assertEqual(
            [e["event_type"] for e in events],
            [
                "rh-new-role-available",
                "rh-platform-default-role-updated",
                "rh-non-platform-default-role-updated",
                "rh-new-role-added-to-default-access",
                "rh-role-removed-from-default-access",
            ],
        )
        json.dumps(events)

    def test_rolled_back_transaction_queues_nothing(self):
        """An event rolls back with the change it reports."""
        with collected_rh_events() as events:
            try:
                with transaction.atomic():
                    handle_system_role_change_notification(make_role("rolled-back"), "created")
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
            with transaction.atomic():
                handle_system_role_change_notification(make_role("committed"), "created")

        self.assertEqual([e["payload"]["name"] for e in events], ["committed"])

    def test_skip_flag_queues_nothing(self):
        """skip_rh_notifications suppresses queueing."""
        skip_token = skip_rh_notifications.set(True)
        try:
            handle_system_role_change_notification(make_role(), "created")
            handle_platform_group_role_change_notification(make_role(), make_role(), "added")
        finally:
            skip_rh_notifications.reset(skip_token)

        self.assertFalse(RhNotificationEvent.objects.exists())

    @override_settings(NOTIFICATIONS_RH_ENABLED=False)
    def test_disabled_setting_queues_nothing(self):
        """NOTIFICATIONS_RH_ENABLED=False suppresses queueing."""
        handle_system_role_change_notification(make_role(), "created")
        handle_platform_group_role_change_notification(make_role(), make_role(), "added")

        self.assertFalse(RhNotificationEvent.objects.exists())

    @patch("management.management.commands.seeds.role_seeding")
    def test_seeds_command_keeps_events_of_committed_changes_when_a_later_step_fails(self, role_seeding):
        """A seeding run that dies after a change committed still leaves that change's event queued."""

        def seed_then_fail(**kwargs):
            with transaction.atomic():
                queue_rh_notification("rh-new-role-available", {"name": "a"})
            raise RuntimeError("seeding failed")

        role_seeding.side_effect = seed_then_fail

        with self.assertRaises(RuntimeError):
            call_command("seeds", "--roles")

        self.assertEqual(
            list(RhNotificationEvent.objects.values("event_type", "payload")),
            [{"event_type": "rh-new-role-available", "payload": {"name": "a"}}],
        )
