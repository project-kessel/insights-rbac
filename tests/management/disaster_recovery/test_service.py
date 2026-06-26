"""Tests for disaster recovery service orchestrator."""

from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from api.models import Tenant
from management.disaster_recovery.kafka_reader import ParsedReplicationEvent
from management.disaster_recovery.service import _partition_bootstrap_events, reconcile
from management.relation_replicator.outbox_replicator import InMemoryLog, OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.relation_replicator.types import (
    ObjectReference,
    ObjectType,
    RelationTuple,
    SubjectReference,
)
from management.workspace.model import Workspace


def _make_tuple(resource_type="workspace", resource_id="ws-123"):
    return RelationTuple(
        resource=ObjectReference(
            type=ObjectType(namespace="rbac", name=resource_type),
            id=resource_id,
        ),
        relation="parent",
        subject=SubjectReference(
            subject=ObjectReference(
                type=ObjectType(namespace="rbac", name="workspace"),
                id="ws-parent",
            ),
        ),
    )


class ReconcileServiceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="test-dr-svc",
            account_id="svc-account",
            org_id="svc-org",
            ready=True,
        )
        cls.root_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=cls.tenant,
        )
        cls.default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            tenant=cls.tenant,
            parent=cls.root_ws,
        )

    @classmethod
    def tearDownClass(cls):
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super().tearDownClass()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_empty_window(self, mock_read):
        mock_read.return_value = []
        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=1000000,
            buffer_seconds=300,
            replicator=replicator,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["events_read"], 0)
        self.assertEqual(result["tuples_processed"], 0)
        self.assertEqual(len(log), 0)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_orphaned_tuple_corrective_delete(self, mock_read):
        fake_id = str(uuid4())
        t = _make_tuple(resource_id=fake_id)

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="create_workspace",
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=1000000,
            buffer_seconds=300,
            replicator=replicator,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(result["corrective_adds"], 0)
        self.assertEqual(len(log), 1)

        outbox_entry = log.first()
        self.assertEqual(outbox_entry.event_type, ReplicationEventType.DR_CORRECTIVE_REMOVE)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_missing_tuple_corrective_add(self, mock_read):
        ws = Workspace.objects.create(
            name="Restored Workspace",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
        )
        t = _make_tuple(resource_id=str(ws.id))

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="delete_workspace",
                relations_to_add=[],
                relations_to_remove=[t],
            )
        ]

        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=1000000,
            buffer_seconds=300,
            replicator=replicator,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(result["corrective_removes"], 0)
        self.assertEqual(len(log), 1)

        outbox_entry = log.first()
        self.assertEqual(outbox_entry.event_type, ReplicationEventType.DR_CORRECTIVE_ADD)
        ws.delete()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_result_structure(self, mock_read):
        mock_read.return_value = []
        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=2000000,
            buffer_seconds=60,
            replicator=replicator,
        )

        self.assertIn("status", result)
        self.assertIn("time_window", result)
        self.assertIn("start_ms", result["time_window"])
        self.assertIn("end_ms", result["time_window"])
        self.assertEqual(result["time_window"]["start_ms"], 2000000 - 60000)
        self.assertEqual(result["time_window"]["end_ms"], 2000000)
        self.assertIn("events_read", result)
        self.assertIn("tuples_processed", result)
        self.assertIn("duration_seconds", result)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_both_correct_states_skipped(self, mock_read):
        ws = Workspace.objects.create(
            name="Correct Workspace",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
        )
        fake_id = str(uuid4())

        t_add_exists = _make_tuple(resource_id=str(ws.id))
        t_remove_not_exists = _make_tuple(resource_id=fake_id)

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="mixed",
                relations_to_add=[t_add_exists],
                relations_to_remove=[t_remove_not_exists],
            )
        ]

        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=1000000,
            buffer_seconds=300,
            replicator=replicator,
        )

        self.assertEqual(result["corrective_adds"], 0)
        self.assertEqual(result["corrective_removes"], 0)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(len(log), 0)
        ws.delete()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_dry_run_skips_writes(self, mock_read):
        fake_id = str(uuid4())
        t = _make_tuple(resource_id=fake_id)

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=42,
                partition=1,
                timestamp_ms=1000,
                event_type="create_workspace",
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=1000000,
            buffer_seconds=300,
            replicator=replicator,
            dry_run=True,
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(result["corrective_adds"], 0)
        self.assertEqual(len(log), 0)
        self.assertEqual(len(result["actions"]), 1)
        action = result["actions"][0]
        self.assertEqual(action["action"], "remove")
        self.assertEqual(action["source_partition"], 1)
        self.assertEqual(action["source_offset"], 42)
        self.assertIn(fake_id, action["tuple"])


class PartitionBootstrapEventsTest(TestCase):
    """Tests for _partition_bootstrap_events."""

    def _make_event(self, event_type, offset=0):
        return ParsedReplicationEvent(
            offset=offset,
            partition=0,
            timestamp_ms=1000,
            event_type=event_type,
            org_id="test-org",
            relations_to_add=[_make_tuple()],
            relations_to_remove=[],
        )

    def test_bootstrap_tenant_filtered_out(self):
        events = [self._make_event("bootstrap_tenant")]
        non_bootstrap, skipped = _partition_bootstrap_events(events)
        self.assertEqual(non_bootstrap, [])
        self.assertEqual(skipped, 1)

    def test_bulk_bootstrap_tenant_filtered_out(self):
        events = [self._make_event("bulk_bootstrap_tenant")]
        non_bootstrap, skipped = _partition_bootstrap_events(events)
        self.assertEqual(non_bootstrap, [])
        self.assertEqual(skipped, 1)

    def test_non_bootstrap_events_kept(self):
        events = [
            self._make_event("create_workspace"),
            self._make_event("add_principal_to_group"),
        ]
        non_bootstrap, skipped = _partition_bootstrap_events(events)
        self.assertEqual(len(non_bootstrap), 2)
        self.assertEqual(skipped, 0)

    def test_mixed_events_partitioned(self):
        events = [
            self._make_event("create_workspace", offset=1),
            self._make_event("bootstrap_tenant", offset=2),
            self._make_event("add_principal_to_group", offset=3),
            self._make_event("bulk_bootstrap_tenant", offset=4),
        ]
        non_bootstrap, skipped = _partition_bootstrap_events(events)
        self.assertEqual(len(non_bootstrap), 2)
        self.assertEqual(skipped, 2)
        self.assertEqual([e.offset for e in non_bootstrap], [1, 3])

    def test_empty_events(self):
        non_bootstrap, skipped = _partition_bootstrap_events([])
        self.assertEqual(non_bootstrap, [])
        self.assertEqual(skipped, 0)


class ReconcileBootstrapSkipTest(TestCase):
    """Integration tests for bootstrap event skipping in reconcile."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="test-dr-bootstrap",
            account_id="bootstrap-account",
            org_id="bootstrap-org",
            ready=True,
        )
        cls.root_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=cls.tenant,
        )
        cls.default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            tenant=cls.tenant,
            parent=cls.root_ws,
        )

    @classmethod
    def tearDownClass(cls):
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super().tearDownClass()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_bootstrap_only_window_returns_no_corrective_actions(self, mock_read):
        """Window with only bootstrap events produces zero corrective actions."""
        group_uuid = str(uuid4())
        rb_uuid = str(uuid4())

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=100,
                partition=0,
                timestamp_ms=1000,
                event_type="bootstrap_tenant",
                org_id="some-org",
                relations_to_add=[
                    _make_tuple("group", group_uuid),
                    _make_tuple("role_binding", rb_uuid),
                ],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=1000000,
            buffer_seconds=300,
            replicator=replicator,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["corrective_adds"], 0)
        self.assertEqual(result["corrective_removes"], 0)
        self.assertEqual(result["skipped_bootstrap_events"], 1)
        self.assertEqual(result["events_read"], 1)
        self.assertEqual(result["tuples_processed"], 0)
        self.assertEqual(len(log), 0)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_mixed_bootstrap_and_regular_events(self, mock_read):
        """Bootstrap events are skipped while regular events are processed normally."""
        fake_ws_id = str(uuid4())

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=100,
                partition=0,
                timestamp_ms=1000,
                event_type="bootstrap_tenant",
                org_id="some-org",
                relations_to_add=[_make_tuple("group", str(uuid4()))],
                relations_to_remove=[],
            ),
            ParsedReplicationEvent(
                offset=101,
                partition=0,
                timestamp_ms=1001,
                event_type="create_workspace",
                relations_to_add=[_make_tuple("workspace", fake_ws_id)],
                relations_to_remove=[],
            ),
        ]

        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=1000000,
            buffer_seconds=300,
            replicator=replicator,
        )

        self.assertEqual(result["skipped_bootstrap_events"], 1)
        self.assertEqual(result["events_read"], 2)
        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(len(log), 1)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_dry_run_reports_skipped_bootstrap(self, mock_read):
        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=100,
                partition=0,
                timestamp_ms=1000,
                event_type="bulk_bootstrap_tenant",
                org_id="some-org",
                relations_to_add=[_make_tuple("group", str(uuid4()))],
                relations_to_remove=[],
            ),
        ]

        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        result = reconcile(
            restore_timestamp_ms=1000000,
            buffer_seconds=300,
            replicator=replicator,
            dry_run=True,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["skipped_bootstrap_events"], 1)
        self.assertEqual(len(log), 0)
