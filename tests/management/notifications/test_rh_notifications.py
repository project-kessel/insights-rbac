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
"""Tests for the rate-limited sending of Red Hat system notifications."""

import datetime
import json
from unittest.mock import ANY, MagicMock, call, patch

from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
from management.notifications.notification_handlers import (
    collect_rh_notifications,
    enqueue_rh_notifications,
    handle_platform_group_role_change_notification,
    handle_system_role_change_notification,
    queue_rh_notification,
    rh_notification_collector,
    skip_rh_notifications,
)
from management.tasks import (
    RH_NOTIFICATIONS_MAX_FAILED_ATTEMPTS,
    RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
    _rh_notifications_quiet_window_wait_seconds,
    send_rh_notifications_in_worker,
)
from tests.core.test_kafka import copy_call_args
from tests.identity_request import IdentityRequest

from api.models import Tenant

ENQUEUE = "management.notifications.notification_handlers.enqueue_rh_notifications"
NOTIFY = "management.notifications.notification_handlers.notify"
PRODUCER = "management.notifications.notification_handlers.noto_producer"
LOGGER = "management.notifications.notification_handlers.logger"
APPLY_ASYNC = "management.tasks.send_rh_notifications_in_worker.apply_async"


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


@override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_RH_ENABLED=True)
class RhNotificationCollectionTests(IdentityRequest):
    """Test that the RH handlers register events on commit instead of sending."""

    def setUp(self):
        """Keep the global production lock isolated from parallel test workers."""
        super().setUp()
        cache_mock = MagicMock()
        cache_mock.add.return_value = True
        cache_patch = patch("management.notifications.notification_handlers.cache", cache_mock)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def test_event_is_collected_after_commit(self):
        """The event only reaches the collector once the transaction commits."""
        events = []
        token = rh_notification_collector.set(events)
        try:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                handle_system_role_change_notification(make_role("new"), "created")
                self.assertEqual(events, [])
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
        finally:
            rh_notification_collector.reset(token)

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

    def test_event_types_are_unchanged(self):
        """The five RH event types are still produced."""
        events = []
        token = rh_notification_collector.set(events)
        group = make_role("group")
        try:
            with self.captureOnCommitCallbacks(execute=True):
                handle_system_role_change_notification(make_role(platform_default=False), "created")
                handle_system_role_change_notification(make_role(platform_default=True), "updated")
                handle_system_role_change_notification(make_role(platform_default=False), "updated")
                handle_platform_group_role_change_notification(group, make_role(), "added")
                handle_platform_group_role_change_notification(group, make_role(), "removed")
        finally:
            rh_notification_collector.reset(token)

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

    def test_rolled_back_transaction_contributes_nothing(self):
        """A rolled-back attempt never runs its callback, so only the committed one is collected."""
        events = []
        token = rh_notification_collector.set(events)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        handle_system_role_change_notification(make_role("rolled-back"), "created")
                        raise RuntimeError("rollback")
                except RuntimeError:
                    pass
                with transaction.atomic():
                    handle_system_role_change_notification(make_role("committed"), "created")
        finally:
            rh_notification_collector.reset(token)

        self.assertEqual([e["payload"]["name"] for e in events], ["committed"])

    def test_skip_flag_collects_nothing(self):
        """skip_rh_notifications suppresses collection."""
        events = []
        collector_token = rh_notification_collector.set(events)
        skip_token = skip_rh_notifications.set(True)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                handle_system_role_change_notification(make_role(), "created")
                handle_platform_group_role_change_notification(make_role(), make_role(), "added")
        finally:
            skip_rh_notifications.reset(skip_token)
            rh_notification_collector.reset(collector_token)

        self.assertEqual(events, [])

    @patch(APPLY_ASYNC)
    @patch("management.notifications.notification_handlers.cache.add", return_value=False)
    def test_active_chain_prevents_overlapping_initial_enqueue(self, cache_add, apply_async):
        """Only one seeding run acquires the shared active-chain marker."""
        enqueue_rh_notifications([{"event_type": "a", "payload": {}}])

        cache_add.assert_called_once()
        apply_async.assert_not_called()

    @override_settings(NOTIFICATIONS_RH_ENABLED=False)
    def test_disabled_setting_collects_nothing(self):
        """NOTIFICATIONS_RH_ENABLED=False suppresses collection."""
        events = []
        token = rh_notification_collector.set(events)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                handle_system_role_change_notification(make_role(), "created")
        finally:
            rh_notification_collector.reset(token)

        self.assertEqual(events, [])

    @patch(ENQUEUE)
    def test_no_collector_enqueues_single_event_on_commit(self, enqueue):
        """Without an open collector the commit callback enqueues the one event."""
        with self.captureOnCommitCallbacks(execute=True):
            handle_system_role_change_notification(make_role("lonely"), "created")
            enqueue.assert_not_called()

        enqueue.assert_called_once()
        (events,) = enqueue.call_args.args
        self.assertEqual([e["payload"]["name"] for e in events], ["lonely"])

    @patch(LOGGER)
    @patch(APPLY_ASYNC, side_effect=ConnectionError("broker down"))
    def test_no_collector_enqueue_error_is_logged_not_raised(self, apply_async, logger):
        """A broker failure in the on-commit callback does not break the committing seeding step."""
        with self.captureOnCommitCallbacks(execute=True):
            handle_system_role_change_notification(make_role("lonely"), "created")

        apply_async.assert_called_once()
        self.assertEqual(apply_async.call_args.kwargs["retry"], True)
        self.assertEqual(apply_async.call_args.kwargs["retry_policy"]["max_retries"], 3)
        logger.exception.assert_called_once()


class CollectRhNotificationsTests(IdentityRequest):
    """Test the per-run collector context manager."""

    def setUp(self):
        """Keep the global production lock isolated from parallel test workers."""
        super().setUp()
        cache_mock = MagicMock()
        cache_mock.add.return_value = True
        cache_patch = patch("management.notifications.notification_handlers.cache", cache_mock)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    @patch(ENQUEUE)
    def test_enqueues_all_events_once(self, enqueue):
        """All events collected in the block are enqueued in a single call."""
        with collect_rh_notifications():
            rh_notification_collector.get().extend(
                [{"event_type": "a", "payload": {}}, {"event_type": "b", "payload": {}}]
            )

        enqueue.assert_called_once_with([{"event_type": "a", "payload": {}}, {"event_type": "b", "payload": {}}])
        self.assertIsNone(rh_notification_collector.get())

    @patch(ENQUEUE)
    def test_empty_collector_enqueues_nothing(self, enqueue):
        """A run without changes enqueues no task."""
        with collect_rh_notifications():
            pass

        enqueue.assert_not_called()

    @patch(ENQUEUE)
    def test_enqueues_even_when_the_run_raises(self, enqueue):
        """Committed events are still enqueued and the original error propagates."""
        with self.assertRaises(RuntimeError):
            with collect_rh_notifications():
                rh_notification_collector.get().append({"event_type": "a", "payload": {}})
                raise RuntimeError("seeding failed")

        enqueue.assert_called_once_with([{"event_type": "a", "payload": {}}])

    @patch(LOGGER)
    @patch(APPLY_ASYNC, side_effect=ConnectionError("broker down"))
    def test_enqueue_errors_are_logged_not_raised(self, apply_async, logger):
        """A broker failure is logged and does not hide the seeding outcome."""
        with collect_rh_notifications():
            rh_notification_collector.get().append({"event_type": "a", "payload": {}})

        apply_async.assert_called_once()
        self.assertEqual(apply_async.call_args.kwargs["args"], [[{"event_type": "a", "payload": {}}]])
        self.assertEqual(apply_async.call_args.kwargs["retry"], True)
        self.assertEqual(apply_async.call_args.kwargs["retry_policy"]["max_retries"], 3)
        logger.exception.assert_called_once()
        logger.info.assert_not_called()

    @patch(APPLY_ASYNC, side_effect=ConnectionError("broker down"))
    def test_enqueue_error_does_not_mask_seeding_error(self, apply_async):
        """The seeding error, not the enqueue error, reaches the caller."""
        with self.assertRaises(RuntimeError):
            with collect_rh_notifications():
                rh_notification_collector.get().append({"event_type": "a", "payload": {}})
                raise RuntimeError("seeding failed")

    @patch(ENQUEUE)
    @patch("management.management.commands.seeds.role_seeding")
    def test_seeds_command_enqueues_after_failure(self, role_seeding, enqueue):
        """The seeds command enqueues the events of already committed changes when a later step raises."""

        def seed_then_fail(**kwargs):
            with self.captureOnCommitCallbacks(execute=True):
                queue_rh_notification("a", {})
            raise RuntimeError("seeding failed")

        role_seeding.side_effect = seed_then_fail

        with self.assertRaises(RuntimeError):
            call_command("seeds", "--roles")

        enqueue.assert_called_once_with([{"event_type": "a", "payload": {}}])

    @patch(ENQUEUE)
    @patch("management.management.commands.seeds.role_seeding")
    def test_seeds_command_without_changes_enqueues_nothing(self, role_seeding, enqueue):
        """A seeding run that collected nothing enqueues no task."""
        call_command("seeds", "--roles", "--skip-notifications")

        role_seeding.assert_called_once()
        enqueue.assert_not_called()


class QuietWindowTests(IdentityRequest):
    """Test the quiet window evaluation."""

    def test_defaults_cross_midnight(self):
        """The default 23:30-00:30 window blocks late evening and early morning only."""
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(12)), 0)
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(23, 29, 59)), 0)
        self.assertGreater(_rh_notifications_quiet_window_wait_seconds(utc(23, 30)), 0)
        self.assertGreater(_rh_notifications_quiet_window_wait_seconds(utc(23, 59)), 0)
        self.assertGreater(_rh_notifications_quiet_window_wait_seconds(utc(0, 0)), 0)
        self.assertGreater(_rh_notifications_quiet_window_wait_seconds(utc(0, 29, 59)), 0)

    def test_end_boundary_allows_sending(self):
        """Sending is allowed at exactly the end time."""
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(0, 30)), 0)

    def test_countdown_is_capped_at_300_seconds(self):
        """A long remaining wait is capped so the countdown never exceeds 300 seconds."""
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(23, 30)), 300)
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(0, 0)), 300)

    def test_countdown_is_remaining_time_when_shorter(self):
        """The countdown shrinks to the remaining time near the end of the window."""
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(0, 28)), 120)
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(0, 29, 59)), 1)

    @override_settings(NOTIFICATIONS_RH_QUIET_START="01:00", NOTIFICATIONS_RH_QUIET_END="02:00")
    def test_same_day_window(self):
        """A window inside one day blocks only between start (inclusive) and end (exclusive)."""
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(0, 59, 59)), 0)
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(1, 0)), 300)
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(1, 59, 30)), 30)
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(2, 0)), 0)

    @override_settings(NOTIFICATIONS_RH_QUIET_START="05:00", NOTIFICATIONS_RH_QUIET_END="05:00")
    def test_equal_start_and_end_disables_window(self):
        """Equal start and end times disable the window."""
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(5, 0)), 0)
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(5, 1)), 0)

    @patch("management.tasks.logger")
    @override_settings(NOTIFICATIONS_RH_QUIET_START="bogus", NOTIFICATIONS_RH_QUIET_END="25:99")
    def test_invalid_values_fall_back_to_defaults(self, logger):
        """Invalid values log a warning and use the 23:30-00:30 defaults."""
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(12)), 0)
        self.assertEqual(_rh_notifications_quiet_window_wait_seconds(utc(23, 45)), 300)
        self.assertEqual(logger.warning.call_count, 4)


@override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_RH_ENABLED=True, NOTIFICATIONS_RH_RATE_PER_MINUTE=6)
class SendRhNotificationsTaskTests(IdentityRequest):
    """Test the batched sending task."""

    EVENTS = [
        {"event_type": "rh-new-role-available", "payload": {"name": "a"}},
        {"event_type": "rh-platform-default-role-updated", "payload": {"name": "b"}},
    ]

    def setUp(self):
        """Create ready and unready tenants."""
        super().setUp()
        Tenant.objects.exclude(tenant_name="public").delete()
        self.ready = [Tenant.objects.create(tenant_name=f"t{i}", org_id=f"org{i}", ready=True) for i in range(5)]
        Tenant.objects.create(tenant_name="unready", org_id="unready", ready=False)

        quiet = patch("management.tasks._rh_notifications_quiet_window_wait_seconds", return_value=0)
        quiet.start()
        self.addCleanup(quiet.stop)

        notify = patch(NOTIFY)
        self.notify = notify.start()
        self.addCleanup(notify.stop)

        producer = patch(PRODUCER)
        self.producer = producer.start()
        self.addCleanup(producer.stop)

        apply_async = patch.object(send_rh_notifications_in_worker, "apply_async")
        self.apply_async = apply_async.start()
        self.addCleanup(apply_async.stop)

    def test_first_batch_sends_and_chains_with_cursor(self):
        """A batch holds rate // events tenants, flushes, and re-enqueues 60 seconds later with a cursor."""
        send_rh_notifications_in_worker(self.EVENTS)

        # 6 messages per minute / 2 events = 3 tenants per batch
        self.assertEqual(self.notify.call_count, 6)
        self.assertEqual(
            self.notify.call_args_list[:2],
            [
                call("rh-new-role-available", {"name": "a"}, "org0"),
                call("rh-platform-default-role-updated", {"name": "b"}, "org0"),
            ],
        )
        self.producer.get_producer.return_value.flush.assert_called_once()
        self.apply_async.assert_called_once_with(
            args=[self.EVENTS],
            kwargs={"after_tenant_id": self.ready[2].id},
            countdown=60,
            retry=True,
            retry_policy=RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        )

    def test_duplicate_continuation_publish_does_not_repeat_batch(self):
        """A replayed Celery publish token is ignored after its batch completed."""
        state = {"rh-notifications:active-chain": "chain"}
        cache_mock = MagicMock()
        cache_mock.get.side_effect = state.get

        def cache_add(key, value, timeout):
            if key in state:
                return False
            state[key] = value
            return True

        cache_mock.add.side_effect = cache_add
        cache_mock.set.side_effect = lambda key, value, timeout: state.__setitem__(key, value)
        cache_mock.delete.side_effect = lambda key: state.pop(key, None) is not None

        with patch("management.tasks.cache", cache_mock):
            send_rh_notifications_in_worker(self.EVENTS, chain_id="chain", batch_id="batch")
            send_rh_notifications_in_worker(self.EVENTS, chain_id="chain", batch_id="batch")

        self.assertEqual(self.notify.call_count, 6)
        self.apply_async.assert_called_once()
        continuation = self.apply_async.call_args.kwargs
        self.assertEqual(continuation["kwargs"]["chain_id"], "chain")
        self.assertEqual(continuation["task_id"], continuation["kwargs"]["batch_id"])
        self.assertEqual(continuation["retry_policy"]["max_retries"], 3)

    def test_last_batch_flushes_and_ends_chain(self):
        """A short batch is the last one: it flushes and does not re-enqueue."""
        send_rh_notifications_in_worker(self.EVENTS, after_tenant_id=self.ready[2].id)

        self.assertEqual([c.args[2] for c in self.notify.call_args_list], ["org3", "org3", "org4", "org4"])
        self.producer.get_producer.return_value.flush.assert_called_once()
        self.apply_async.assert_not_called()

    def test_every_ready_tenant_gets_every_event_exactly_once(self):
        """Chaining batches covers all ready non-public tenants, once per event, and skips unready ones."""
        after = 0
        for _ in range(10):
            self.apply_async.reset_mock()
            send_rh_notifications_in_worker(self.EVENTS, after_tenant_id=after)
            if not self.apply_async.called:
                break
            after = self.apply_async.call_args.kwargs["kwargs"]["after_tenant_id"]

        sent = [(c.args[0], c.args[2]) for c in self.notify.call_args_list]
        expected = [(e["event_type"], t.org_id) for t in self.ready for e in self.EVENTS]
        self.assertCountEqual(sent, expected)
        self.assertEqual(len(sent), len(set(sent)))

    @override_settings(NOTIFICATIONS_RH_RATE_PER_MINUTE=4)
    def test_full_final_batch_ends_with_an_empty_batch(self):
        """When the tenants divide evenly into batches the chain ends on an empty batch."""
        Tenant.objects.filter(org_id="org4").delete()

        send_rh_notifications_in_worker(self.EVENTS)
        after = self.apply_async.call_args.kwargs["kwargs"]["after_tenant_id"]
        send_rh_notifications_in_worker(self.EVENTS, after_tenant_id=after)
        self.assertEqual(self.notify.call_count, 8)
        after = self.apply_async.call_args.kwargs["kwargs"]["after_tenant_id"]

        self.notify.reset_mock()
        self.apply_async.reset_mock()
        send_rh_notifications_in_worker(self.EVENTS, after_tenant_id=after)

        self.notify.assert_not_called()
        self.apply_async.assert_not_called()

    def test_no_ready_tenants_finishes_with_zero_totals(self):
        """With no ready tenants the first batch is empty and the chain ends."""
        Tenant.objects.exclude(tenant_name="public").update(ready=False)

        with patch("management.tasks.logger") as logger:
            send_rh_notifications_in_worker(self.EVENTS)

        self.notify.assert_not_called()
        self.apply_async.assert_not_called()
        totals = logger.info.call_args_list[0]
        self.assertEqual(totals.args[1:3], (0, 0))

    @override_settings(NOTIFICATIONS_RH_RATE_PER_MINUTE=1)
    def test_rate_below_event_count_sends_one_tenant_per_batch(self):
        """The batch size never drops below one tenant."""
        send_rh_notifications_in_worker(self.EVENTS)

        self.assertEqual({c.args[2] for c in self.notify.call_args_list}, {"org0"})
        self.apply_async.assert_called_once_with(
            args=[self.EVENTS],
            kwargs={"after_tenant_id": self.ready[0].id},
            countdown=60,
            retry=True,
            retry_policy=RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        )

    @override_settings(NOTIFICATIONS_RH_ENABLED=False)
    def test_disabled_setting_exits_early(self):
        """The task checks NOTIFICATIONS_RH_ENABLED when it runs."""
        send_rh_notifications_in_worker(self.EVENTS)

        self.notify.assert_not_called()
        self.apply_async.assert_not_called()

    @override_settings(NOTIFICATIONS_ENABLED=False)
    def test_notifications_disabled_exits_early(self):
        """Turning NOTIFICATIONS_ENABLED off also stops a running chain."""
        send_rh_notifications_in_worker(self.EVENTS)

        self.notify.assert_not_called()
        self.apply_async.assert_not_called()

    def test_empty_events_exit_early(self):
        """A task without events does nothing."""
        send_rh_notifications_in_worker([])

        self.notify.assert_not_called()
        self.apply_async.assert_not_called()

    def test_quiet_window_only_reenqueues_itself(self):
        """Inside the quiet window nothing is sent and the task retries with the same cursor."""
        with patch("management.tasks._rh_notifications_quiet_window_wait_seconds", return_value=300):
            send_rh_notifications_in_worker(self.EVENTS, after_tenant_id=7)

        self.notify.assert_not_called()
        self.producer.get_producer.return_value.flush.assert_not_called()
        self.apply_async.assert_called_once_with(
            args=[self.EVENTS],
            kwargs={"after_tenant_id": 7},
            countdown=300,
            retry=True,
            retry_policy=RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        )

    def test_quiet_window_starting_mid_batch_stops_after_the_last_completed_tenant(self):
        """A window that starts while the batch runs stops it and resumes from the last completed tenant."""
        with patch("management.tasks._rh_notifications_quiet_window_wait_seconds", side_effect=[0, 0, 120]):
            send_rh_notifications_in_worker(self.EVENTS)

        self.assertEqual([c.args[2] for c in self.notify.call_args_list], ["org0", "org0"])
        self.producer.get_producer.return_value.flush.assert_called_once()
        self.apply_async.assert_called_once_with(
            args=[self.EVENTS],
            kwargs={"after_tenant_id": self.ready[0].id},
            countdown=120,
            retry=True,
            retry_policy=RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        )

    def test_failed_send_stops_the_batch_and_retries_from_the_last_completed_tenant(self):
        """A failed send does not advance the cursor past the tenant it failed for."""
        self.notify.side_effect = [True, True, True, False]

        send_rh_notifications_in_worker(self.EVENTS)

        self.assertEqual(self.notify.call_count, 4)
        self.assertEqual(
            [call_args.args[2] for call_args in self.notify.call_args_list], ["org0", "org0", "org1", "org1"]
        )
        self.apply_async.assert_called_once_with(
            args=[self.EVENTS],
            kwargs={"after_tenant_id": self.ready[0].id, "failed_attempts": 1},
            countdown=60,
            retry=True,
            retry_policy=RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        )

        # The retry starts after the last fully completed tenant, replaying org1's accepted first event.
        self.notify.reset_mock(side_effect=True)
        self.apply_async.reset_mock()
        send_rh_notifications_in_worker(self.EVENTS, after_tenant_id=self.ready[0].id, failed_attempts=1)

        self.assertEqual(
            [call_args.args[2] for call_args in self.notify.call_args_list],
            ["org1", "org1", "org2", "org2", "org3", "org3"],
        )
        self.apply_async.assert_called_once_with(
            args=[self.EVENTS],
            kwargs={"after_tenant_id": self.ready[3].id},
            countdown=60,
            retry=True,
            retry_policy=RH_NOTIFICATIONS_PUBLISH_RETRY_POLICY,
        )

    def test_repeated_failed_sends_give_up(self):
        """After the maximum number of consecutive failed attempts the chain ends."""
        self.notify.return_value = False

        with patch("management.tasks.logger") as logger:
            send_rh_notifications_in_worker(self.EVENTS, failed_attempts=RH_NOTIFICATIONS_MAX_FAILED_ATTEMPTS - 1)

        self.apply_async.assert_not_called()
        logger.error.assert_called_once()

    def test_continuation_enqueue_error_is_logged_and_raised(self):
        """A failing continuation enqueue is logged with the cursor and fails the task."""
        self.apply_async.side_effect = RuntimeError("broker down")

        with patch("management.tasks.logger") as logger:
            with self.assertRaises(RuntimeError):
                send_rh_notifications_in_worker(self.EVENTS)

        logger.exception.assert_called_once()
        self.assertEqual(logger.exception.call_args.args[1], self.ready[2].id)

    def test_flush_failure_does_not_stop_the_chain(self):
        """A failing flush is logged and the next batch is still scheduled."""
        self.producer.get_producer.return_value.flush.side_effect = RuntimeError("flush failed")

        send_rh_notifications_in_worker(self.EVENTS)

        self.apply_async.assert_called_once()


@override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_RH_ENABLED=True, NOTIFICATIONS_RH_RATE_PER_MINUTE=10)
class SendRhNotificationsKafkaMessageTests(IdentityRequest):
    """Test the Kafka messages the task produces, without mocking notify()."""

    def setUp(self):
        """Create one ready, one unready tenant and patch the Kafka send."""
        super().setUp()
        Tenant.objects.exclude(tenant_name="public").delete()
        Tenant.objects.create(tenant_name="ready", org_id="ready", ready=True)
        Tenant.objects.create(tenant_name="unready", org_id="unready", ready=False)

        quiet = patch("management.tasks._rh_notifications_quiet_window_wait_seconds", return_value=0)
        quiet.start()
        self.addCleanup(quiet.stop)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_ready_tenants_receive_the_full_message(self, send_kafka_message):
        """Each ready tenant gets the unchanged message envelope; public and unready tenants get nothing."""
        kafka_mock = copy_call_args(send_kafka_message)
        payload = {"username": "Red Hat", "name": "role", "uuid": "00000000-0000-0000-0000-000000000001"}

        send_rh_notifications_in_worker([{"event_type": "rh-new-role-available", "payload": payload}])

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
