"""Tests for disaster recovery service orchestrator."""

from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from api.models import Tenant
from management.disaster_recovery.kafka_reader import ParsedReplicationEvent
from management.disaster_recovery.service import reconcile
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
