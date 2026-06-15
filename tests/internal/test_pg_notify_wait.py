"""Tests for PostgreSQL NOTIFY coordination helpers."""

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

from unittest.mock import Mock, patch

from django.test import TestCase
from internal.migration_coordination import (
    MIGRATION_NOTIFY_COORDINATIONS,
    MIGRATION_NOTIFY_TIMEOUT_SECONDS,
    build_migration_notify_resource_context,
    notify_migration_batch_completion,
)
from internal.pg_notify_wait import (
    NotifyCoordinatedReplicator,
    replicate_with_notify,
)
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    ReplicationEvent,
    ReplicationEventType,
)
from migration_tool.utils import create_relationship


class ReplicateWithNotifyTests(TestCase):
    """Tests for replicate_with_notify."""

    @patch("internal.pg_notify_wait.connection")
    @patch("internal.pg_notify_wait.wait_for_pg_notify")
    @patch("internal.pg_notify_wait.uuid.uuid4", return_value="test-notify-token")
    def test_replicate_injects_notify_token_and_waits(self, _mock_uuid, mock_wait, mock_connection):
        mock_connection.in_atomic_block = False
        replicator = Mock()
        coordination = MIGRATION_NOTIFY_COORDINATIONS[ReplicationEventType.MIGRATE_BINDING_SCOPE.value]
        relation = create_relationship(("rbac", "role"), "r1", ("rbac", "principal"), "localhost/p1", "member")
        event = ReplicationEvent(
            add=[relation],
            remove=[],
            event_type=ReplicationEventType.MIGRATE_BINDING_SCOPE,
            info={"org_id": "123456"},
            partition_key=PartitionKey.byEnvironment(),
        )

        replicate_with_notify(replicator, event)

        replicator.replicate.assert_called_once_with(event)
        self.assertEqual(event.event_info["notify_token"], "test-notify-token")
        mock_wait.assert_called_once_with(
            channel=coordination.channel,
            expected_payload="test-notify-token",
            timeout_seconds=MIGRATION_NOTIFY_TIMEOUT_SECONDS,
            log_label=coordination.log_label,
        )

    @patch("internal.pg_notify_wait.connection")
    @patch("internal.pg_notify_wait.transaction.on_commit")
    @patch("internal.pg_notify_wait.wait_for_pg_notify")
    @patch("internal.pg_notify_wait.uuid.uuid4", return_value="test-notify-token")
    def test_defer_wait_until_transaction_commits(self, _mock_uuid, mock_wait, mock_on_commit, mock_connection):
        mock_connection.in_atomic_block = True
        replicator = Mock()
        relation = create_relationship(("rbac", "role"), "r1", ("rbac", "principal"), "localhost/p1", "member")
        event = ReplicationEvent(
            add=[relation],
            remove=[],
            event_type=ReplicationEventType.MIGRATE_BINDING_SCOPE,
            info={"org_id": "123456"},
            partition_key=PartitionKey.byEnvironment(),
        )

        replicate_with_notify(replicator, event)

        replicator.replicate.assert_called_once_with(event)
        mock_wait.assert_not_called()
        mock_on_commit.assert_called_once()
        wait_callback = mock_on_commit.call_args[0][0]
        wait_callback()
        mock_wait.assert_called_once()

    @patch("internal.pg_notify_wait.connection")
    @patch("internal.pg_notify_wait.wait_for_pg_notify")
    @patch("internal.pg_notify_wait.uuid.uuid4", return_value="test-notify-token")
    def test_skips_notify_wait_for_empty_replication_event(self, _mock_uuid, mock_wait, mock_connection):
        mock_connection.in_atomic_block = False
        replicator = Mock()
        event = ReplicationEvent(
            add=[],
            remove=[],
            event_type=ReplicationEventType.MIGRATE_BINDING_SCOPE,
            info={"org_id": "123456"},
            partition_key=PartitionKey.byEnvironment(),
        )

        replicate_with_notify(replicator, event)

        replicator.replicate.assert_called_once_with(event)
        self.assertNotIn("notify_token", event.event_info)
        mock_wait.assert_not_called()


class NotifyCoordinatedReplicatorTests(TestCase):
    """Tests for NotifyCoordinatedReplicator."""

    @patch("internal.pg_notify_wait.replicate_with_notify")
    def test_replicate_delegates_to_replicate_with_notify(self, mock_replicate_with_notify):
        inner = Mock()
        replicator = NotifyCoordinatedReplicator(
            inner,
            event_type=ReplicationEventType.MIGRATE_BINDING_SCOPE,
        )
        relation = create_relationship(("rbac", "role"), "r1", ("rbac", "principal"), "localhost/p1", "member")
        event = ReplicationEvent(
            add=[relation],
            remove=[],
            event_type=ReplicationEventType.MIGRATE_BINDING_SCOPE,
            info={"org_id": "123456"},
            partition_key=PartitionKey.byEnvironment(),
        )

        replicator.replicate(event)

        mock_replicate_with_notify.assert_called_once_with(inner, event)


class MigrationNotifyResourceContextTests(TestCase):
    """Tests for build_migration_notify_resource_context."""

    def test_includes_notify_token_for_migrate_binding_scope(self):
        coordination = MIGRATION_NOTIFY_COORDINATIONS[ReplicationEventType.MIGRATE_BINDING_SCOPE.value]
        context = build_migration_notify_resource_context(
            ReplicationEventType.MIGRATE_BINDING_SCOPE,
            {"org_id": "123456", "notify_token": "scope-batch-token"},
            coordination,
        )
        self.assertEqual(context["org_id"], "123456")
        self.assertEqual(context["event_type"], ReplicationEventType.MIGRATE_BINDING_SCOPE.value)
        self.assertEqual(context["notify_token"], "scope-batch-token")

    def test_without_token_returns_none(self):
        coordination = MIGRATION_NOTIFY_COORDINATIONS[
            ReplicationEventType.REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS.value
        ]
        context = build_migration_notify_resource_context(
            ReplicationEventType.REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS,
            {"batch_size": 1},
            coordination,
        )
        self.assertIsNone(context)

    @patch("internal.migration_coordination.logger")
    def test_without_optional_token_does_not_warn(self, mock_logger):
        coordination = MIGRATION_NOTIFY_COORDINATIONS[ReplicationEventType.MIGRATE_BINDING_SCOPE.value]
        context = build_migration_notify_resource_context(
            ReplicationEventType.MIGRATE_BINDING_SCOPE,
            {"org_id": "123456"},
            coordination,
        )
        self.assertIsNone(context)
        mock_logger.warning.assert_not_called()
        mock_logger.debug.assert_called_once()


class NotifyMigrationBatchCompletionTests(TestCase):
    """Tests for notify_migration_batch_completion."""

    def test_sends_notify_for_coordinated_event(self):
        send_notify = Mock()
        notify_migration_batch_completion(
            ReplicationEventType.MIGRATE_BINDING_SCOPE.value,
            {"notify_token": "batch-ack-token"},
            send_notify,
        )
        send_notify.assert_called_once_with(
            MIGRATION_NOTIFY_COORDINATIONS[ReplicationEventType.MIGRATE_BINDING_SCOPE.value].channel,
            "batch-ack-token",
            f"{ReplicationEventType.MIGRATE_BINDING_SCOPE.value} batch (token=batch-ack-token)",
        )

    def test_ignores_non_coordinated_event(self):
        send_notify = Mock()
        notify_migration_batch_completion("create_group", {"notify_token": "token"}, send_notify)
        send_notify.assert_not_called()
