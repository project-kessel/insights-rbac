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
"""Tests for the queued, rate-limited sending of Red Hat system notifications."""

import datetime
import json
from unittest.mock import ANY, MagicMock, call, patch

from django.conf import settings
from django.core.management import call_command
from django.db import connections, transaction
from django.test import override_settings
from django.utils import timezone
from management.notifications.model import RhNotificationDelivery, RhNotificationEvent
from management.notifications.notification_handlers import (
    handle_platform_group_role_change_notification,
    handle_system_role_change_notification,
    queue_rh_notification,
    skip_rh_notifications,
)
from management.tasks import (
    RH_NOTIFICATIONS_FLUSH_TIMEOUT_SECONDS,
    RH_NOTIFICATIONS_LOCK_ID,
    RH_NOTIFICATIONS_MAX_FAILED_ATTEMPTS,
    RH_NOTIFICATIONS_SETTLE_SECONDS,
    _in_rh_notifications_quiet_window,
    _try_rh_notifications_lock,
    send_rh_notifications_in_worker,
)
from tests.core.test_kafka import copy_call_args
from tests.identity_request import IdentityRequest
from tests.management.notifications.utils import collected_rh_events

from api.models import Tenant

NOTIFY = "management.notifications.notification_handlers.notify"
PRODUCER = "management.notifications.notification_handlers.noto_producer"
QUIET_WINDOW = "management.tasks._in_rh_notifications_quiet_window"


def utc(hour, minute=0, second=0):
    """Return a UTC datetime on a fixed day at the given time."""
    return datetime.datetime(2026, 11, 4, hour, minute, second, tzinfo=datetime.timezone.utc)


def make_role(name="role", platform_default=False):
    """Return a stand-in system role object."""
    role = MagicMock()
    role.name = name
    role.uuid = "00000000-0000-0000-0000-000000000001"
    role.platform_default = platform_default
    role.system = True
    return role


def queue_settled_events(*event_types):
    """Queue events old enough to be bundled into a delivery, in the given order."""
    base = timezone.now() - datetime.timedelta(seconds=RH_NOTIFICATIONS_SETTLE_SECONDS + 60)
    return [
        RhNotificationEvent.objects.create(
            event_type=event_type, payload={"name": event_type}, created=base + datetime.timedelta(microseconds=i)
        )
        for i, event_type in enumerate(event_types)
    ]


def run_next_batch():
    """Run the task as the next beat tick, with the previous batch sent a minute ago."""
    RhNotificationDelivery.objects.exclude(last_batch_at=None).update(
        last_batch_at=timezone.now() - datetime.timedelta(seconds=60)
    )
    send_rh_notifications_in_worker()


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


class QuietWindowTests(IdentityRequest):
    """Test the quiet window evaluation."""

    def test_defaults_cross_midnight(self):
        """The default 23:30-00:30 window covers late evening and early morning only."""
        self.assertFalse(_in_rh_notifications_quiet_window(utc(12)))
        self.assertFalse(_in_rh_notifications_quiet_window(utc(23, 29, 59)))
        self.assertTrue(_in_rh_notifications_quiet_window(utc(23, 30)))
        self.assertTrue(_in_rh_notifications_quiet_window(utc(23, 59)))
        self.assertTrue(_in_rh_notifications_quiet_window(utc(0, 0)))
        self.assertTrue(_in_rh_notifications_quiet_window(utc(0, 29, 59)))

    def test_end_boundary_allows_sending(self):
        """Sending is allowed at exactly the end time."""
        self.assertFalse(_in_rh_notifications_quiet_window(utc(0, 30)))

    def test_window_is_evaluated_in_utc(self):
        """A non-UTC time is converted before the comparison."""
        cet = datetime.timezone(datetime.timedelta(hours=1))
        self.assertTrue(_in_rh_notifications_quiet_window(datetime.datetime(2026, 11, 5, 0, 45, tzinfo=cet)))
        self.assertFalse(_in_rh_notifications_quiet_window(datetime.datetime(2026, 11, 5, 1, 45, tzinfo=cet)))

    @override_settings(NOTIFICATIONS_RH_QUIET_START="01:00", NOTIFICATIONS_RH_QUIET_END="02:00")
    def test_same_day_window(self):
        """A window inside one day covers only start (inclusive) to end (exclusive)."""
        self.assertFalse(_in_rh_notifications_quiet_window(utc(0, 59, 59)))
        self.assertTrue(_in_rh_notifications_quiet_window(utc(1, 0)))
        self.assertTrue(_in_rh_notifications_quiet_window(utc(1, 59, 59)))
        self.assertFalse(_in_rh_notifications_quiet_window(utc(2, 0)))

    @override_settings(NOTIFICATIONS_RH_QUIET_START="05:00", NOTIFICATIONS_RH_QUIET_END="05:00")
    def test_equal_start_and_end_disables_window(self):
        """Equal start and end times disable the window."""
        self.assertFalse(_in_rh_notifications_quiet_window(utc(5, 0)))
        self.assertFalse(_in_rh_notifications_quiet_window(utc(5, 1)))

    @patch("management.tasks.logger")
    @override_settings(NOTIFICATIONS_RH_QUIET_START="bogus", NOTIFICATIONS_RH_QUIET_END="25:99")
    def test_invalid_values_fall_back_to_defaults(self, logger):
        """Invalid values log a warning and use the 23:30-00:30 defaults."""
        self.assertFalse(_in_rh_notifications_quiet_window(utc(12)))
        self.assertTrue(_in_rh_notifications_quiet_window(utc(23, 45)))
        self.assertEqual(logger.warning.call_count, 4)


@override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_RH_ENABLED=True, NOTIFICATIONS_RH_RATE_PER_MINUTE=6)
class SendRhNotificationsTaskTests(IdentityRequest):
    """Test the beat task that sends queued events in rate-limited batches."""

    def setUp(self):
        """Create ready and unready tenants and stub the producer."""
        super().setUp()
        Tenant.objects.exclude(tenant_name="public").delete()
        self.ready = [Tenant.objects.create(tenant_name=f"t{i}", org_id=f"org{i}", ready=True) for i in range(5)]
        Tenant.objects.create(tenant_name="unready", org_id="unready", ready=False)

        quiet = patch(QUIET_WINDOW, return_value=False)
        self.quiet = quiet.start()
        self.addCleanup(quiet.stop)

        notify = patch(NOTIFY, return_value=True)
        self.notify = notify.start()
        self.addCleanup(notify.stop)

        producer = patch(PRODUCER)
        self.producer = producer.start()
        self.addCleanup(producer.stop)

    def sent_org_ids(self):
        """Return the org ids notified so far, one entry per message."""
        return [c.args[2] for c in self.notify.call_args_list]

    def test_first_batch_sends_every_event_to_rate_worth_of_tenants(self):
        """A batch holds rate // events tenants, sends each its events in queue order, flushes and saves the cursor."""
        queue_settled_events("a", "b")

        send_rh_notifications_in_worker()

        # 6 messages per minute / 2 events = 3 tenants per batch
        self.assertEqual(
            self.notify.call_args_list,
            [call(event, {"name": event}, f"org{i}") for i in range(3) for event in ("a", "b")],
        )
        self.producer.get_producer.return_value.flush.assert_called_once_with(RH_NOTIFICATIONS_FLUSH_TIMEOUT_SECONDS)
        delivery = RhNotificationDelivery.objects.get()
        self.assertEqual(delivery.status, RhNotificationDelivery.Status.ACTIVE)
        self.assertEqual(delivery.after_tenant_id, self.ready[2].id)
        self.assertIsNotNone(delivery.last_batch_at)
        self.assertCountEqual(delivery.events.values_list("event_type", flat=True), ["a", "b"])

    def test_next_run_continues_from_the_cursor_and_finishes(self):
        """The following run sends the remaining tenants and marks the delivery done."""
        queue_settled_events("a", "b")
        send_rh_notifications_in_worker()
        self.notify.reset_mock()

        run_next_batch()

        self.assertEqual(self.sent_org_ids(), ["org3", "org3", "org4", "org4"])
        delivery = RhNotificationDelivery.objects.get()
        self.assertEqual(delivery.status, RhNotificationDelivery.Status.DONE)
        self.assertIsNotNone(delivery.finished_at)

        self.notify.reset_mock()
        run_next_batch()
        self.notify.assert_not_called()

    def test_every_ready_tenant_gets_every_event_exactly_once(self):
        """Public and unready tenants are skipped; every ready tenant gets each event once."""
        queue_settled_events("a", "b")

        for _ in range(5):
            run_next_batch()

        self.assertCountEqual(self.sent_org_ids(), [f"org{i}" for i in range(5) for _ in range(2)])

    @override_settings(NOTIFICATIONS_RH_RATE_PER_MINUTE=10)
    def test_batch_that_reaches_the_last_tenant_finishes_without_an_empty_batch(self):
        """A batch that covers exactly the remaining tenants marks the delivery done right away."""
        queue_settled_events("a", "b")

        send_rh_notifications_in_worker()

        self.assertEqual(len(self.notify.call_args_list), 10)
        self.assertEqual(RhNotificationDelivery.objects.get().status, RhNotificationDelivery.Status.DONE)

    @override_settings(NOTIFICATIONS_RH_RATE_PER_MINUTE=1)
    def test_rate_below_event_count_sends_one_tenant_per_batch(self):
        """A tenant's events are never split, even when they exceed the rate."""
        queue_settled_events("a", "b")

        send_rh_notifications_in_worker()

        self.assertEqual(self.sent_org_ids(), ["org0", "org0"])

    def test_runs_less_than_a_minute_apart_send_nothing(self):
        """A run within a minute of the previous batch sends nothing, also when it would start a new delivery."""
        queue_settled_events("a", "b")
        send_rh_notifications_in_worker()
        self.notify.reset_mock()

        send_rh_notifications_in_worker()
        self.notify.assert_not_called()

        with override_settings(NOTIFICATIONS_RH_RATE_PER_MINUTE=100):
            run_next_batch()
        self.assertEqual(RhNotificationDelivery.objects.get().status, RhNotificationDelivery.Status.DONE)
        queue_settled_events("c")
        self.notify.reset_mock()

        send_rh_notifications_in_worker()

        self.notify.assert_not_called()
        self.assertEqual(RhNotificationDelivery.objects.count(), 1)

    def test_new_delivery_waits_for_queued_events_to_settle(self):
        """Events queued within the settle time are not bundled yet, so a seeding run is sent as one delivery."""
        queue_settled_events("a")
        RhNotificationEvent.objects.create(event_type="b", payload={"name": "b"})

        send_rh_notifications_in_worker()

        self.notify.assert_not_called()
        self.assertFalse(RhNotificationDelivery.objects.exists())

    @override_settings(NOTIFICATIONS_RH_RATE_PER_MINUTE=3)
    def test_events_queued_during_a_delivery_go_to_the_next_one(self):
        """An active delivery keeps its events; later events wait for the next delivery."""
        queue_settled_events("a")
        send_rh_notifications_in_worker()
        queue_settled_events("b")
        self.notify.reset_mock()

        run_next_batch()

        self.assertEqual(self.sent_org_ids(), ["org3", "org4"])
        self.assertEqual({c.args[0] for c in self.notify.call_args_list}, {"a"})
        first = RhNotificationDelivery.objects.get()
        self.assertEqual(first.status, RhNotificationDelivery.Status.DONE)
        self.assertTrue(RhNotificationEvent.objects.filter(event_type="b", delivery=None).exists())

        self.notify.reset_mock()
        run_next_batch()

        self.assertEqual(self.notify.call_args_list[0], call("b", {"name": "b"}, "org0"))
        second = RhNotificationDelivery.objects.exclude(id=first.id).get()
        self.assertEqual(list(second.events.values_list("event_type", flat=True)), ["b"])

    def test_failed_send_keeps_the_cursor_and_retries_on_the_next_run(self):
        """A failed send stops the batch at the last complete tenant; the next run replays the failed tenant."""
        queue_settled_events("a", "b")
        self.notify.side_effect = lambda event_type, payload, org_id: not (org_id == "org1" and event_type == "b")

        send_rh_notifications_in_worker()

        delivery = RhNotificationDelivery.objects.get()
        self.assertEqual(delivery.after_tenant_id, self.ready[0].id)
        self.assertEqual(delivery.failed_attempts, 1)
        self.assertEqual(delivery.status, RhNotificationDelivery.Status.ACTIVE)

        self.notify.reset_mock()
        self.notify.side_effect = None
        run_next_batch()

        self.assertEqual(self.sent_org_ids(), ["org1", "org1", "org2", "org2", "org3", "org3"])
        delivery.refresh_from_db()
        self.assertEqual(delivery.failed_attempts, 0)

    @patch("management.tasks.logger")
    def test_delivery_fails_after_max_consecutive_failures(self, logger):
        """Consecutive failures mark the delivery failed, and the failure is logged."""
        queue_settled_events("a")
        self.notify.return_value = False

        for _ in range(RH_NOTIFICATIONS_MAX_FAILED_ATTEMPTS):
            run_next_batch()

        delivery = RhNotificationDelivery.objects.get()
        self.assertEqual(delivery.status, RhNotificationDelivery.Status.FAILED)
        self.assertEqual(delivery.failed_attempts, RH_NOTIFICATIONS_MAX_FAILED_ATTEMPTS)
        self.assertIsNotNone(delivery.finished_at)
        logger.error.assert_called_once()

    def test_unexpected_error_rolls_back_so_the_next_run_resends_the_batch(self):
        """A run that dies mid-batch commits nothing, so the next run resends from the last committed cursor."""
        queue_settled_events("a")
        self.notify.side_effect = [True, RuntimeError("worker lost")]

        with self.assertRaises(RuntimeError):
            send_rh_notifications_in_worker()

        self.assertFalse(RhNotificationDelivery.objects.exists())
        self.assertTrue(RhNotificationEvent.objects.filter(delivery=None).exists())

        self.notify.reset_mock()
        self.notify.side_effect = None
        send_rh_notifications_in_worker()

        self.assertEqual(self.sent_org_ids(), [f"org{i}" for i in range(5)])

    def test_quiet_window_skips_the_run(self):
        """No delivery starts and nothing is sent during the quiet window."""
        queue_settled_events("a")
        self.quiet.return_value = True

        send_rh_notifications_in_worker()

        self.notify.assert_not_called()
        self.assertFalse(RhNotificationDelivery.objects.exists())

    def test_quiet_window_reached_mid_batch_stops_after_the_completed_tenants(self):
        """A batch stops at the window and keeps the delivery active after the last tenant it completed."""
        queue_settled_events("a", "b")
        # The task checks once before the batch, then before each tenant.
        self.quiet.side_effect = [False, False, True]

        send_rh_notifications_in_worker()

        self.assertEqual(self.sent_org_ids(), ["org0", "org0"])
        delivery = RhNotificationDelivery.objects.get()
        self.assertEqual(delivery.after_tenant_id, self.ready[0].id)
        self.assertEqual(delivery.status, RhNotificationDelivery.Status.ACTIVE)

    def test_disabled_settings_exit_early(self):
        """Either flag turned off stops sending, and the queued events stay for later."""
        queue_settled_events("a")
        for flags in (
            {"NOTIFICATIONS_ENABLED": False, "NOTIFICATIONS_RH_ENABLED": True},
            {"NOTIFICATIONS_ENABLED": True, "NOTIFICATIONS_RH_ENABLED": False},
        ):
            with self.subTest(**flags), override_settings(**flags):
                send_rh_notifications_in_worker()

        self.notify.assert_not_called()
        self.assertTrue(RhNotificationEvent.objects.filter(delivery=None).exists())

    @patch("management.tasks._try_rh_notifications_lock", return_value=False)
    def test_run_skips_while_another_run_holds_the_lock(self, _lock):
        """A concurrent run neither starts a delivery nor sends."""
        queue_settled_events("a")

        send_rh_notifications_in_worker()

        self.notify.assert_not_called()
        self.assertFalse(RhNotificationDelivery.objects.exists())

    def test_flush_failure_does_not_stop_the_delivery(self):
        """A flush error is logged and the cursor still advances."""
        queue_settled_events("a", "b")
        self.producer.get_producer.return_value.flush.side_effect = RuntimeError("flush failed")

        send_rh_notifications_in_worker()

        self.assertEqual(RhNotificationDelivery.objects.get().after_tenant_id, self.ready[2].id)

    def test_finished_deliveries_are_deleted_after_seven_days(self):
        """Deliveries finished more than seven days ago are deleted with their events; recent ones are kept."""
        now = timezone.now()
        old = RhNotificationDelivery.objects.create(
            status=RhNotificationDelivery.Status.DONE, finished_at=now - datetime.timedelta(days=8)
        )
        RhNotificationEvent.objects.create(event_type="old", payload={}, delivery=old)
        recent = RhNotificationDelivery.objects.create(
            status=RhNotificationDelivery.Status.FAILED, finished_at=now - datetime.timedelta(days=6)
        )

        send_rh_notifications_in_worker()

        self.assertEqual(list(RhNotificationDelivery.objects.values_list("id", flat=True)), [recent.id])
        self.assertFalse(RhNotificationEvent.objects.exists())


class RhNotificationsLockTests(IdentityRequest):
    """Test the advisory lock against a second database session."""

    def test_lock_excludes_other_sessions_until_the_transaction_ends(self):
        """While one transaction holds the lock, another session cannot take it."""
        default = connections["default"]
        other = type(default)(default.settings_dict.copy(), alias="default")
        try:
            with transaction.atomic():
                self.assertTrue(_try_rh_notifications_lock())
                with other.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_xact_lock(%s);", [RH_NOTIFICATIONS_LOCK_ID])
                    self.assertFalse(cursor.fetchone()[0])
        finally:
            other.close()


@override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_RH_ENABLED=True)
class SendRhNotificationsKafkaMessageTests(IdentityRequest):
    """Test the Kafka messages the task produces, without mocking notify()."""

    def setUp(self):
        """Create one ready and one unready tenant."""
        super().setUp()
        Tenant.objects.exclude(tenant_name="public").delete()
        Tenant.objects.create(tenant_name="ready", org_id="ready", ready=True)
        Tenant.objects.create(tenant_name="unready", org_id="unready", ready=False)

        quiet = patch(QUIET_WINDOW, return_value=False)
        quiet.start()
        self.addCleanup(quiet.stop)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_ready_tenants_receive_the_full_message(self, send_kafka_message):
        """Each ready tenant gets the unchanged message envelope; public and unready tenants get nothing."""
        kafka_mock = copy_call_args(send_kafka_message)
        payload = {"username": "Red Hat", "name": "role", "uuid": "00000000-0000-0000-0000-000000000001"}
        RhNotificationEvent.objects.create(
            event_type="rh-new-role-available",
            payload=payload,
            created=timezone.now() - datetime.timedelta(seconds=RH_NOTIFICATIONS_SETTLE_SECONDS + 60),
        )

        send_rh_notifications_in_worker()

        kafka_mock.assert_called_once_with(
            settings.NOTIFICATIONS_TOPIC,
            {
                "bundle": "console",
                "application": "rbac",
                "event_type": "rh-new-role-available",
                "timestamp": ANY,
                "events": [{"metadata": {}, "payload": payload}],
                "org_id": "ready",
            },
            ANY,
        )
