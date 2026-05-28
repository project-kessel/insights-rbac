"""Tests for disaster recovery corrective event writer."""

from django.test import TestCase

from management.disaster_recovery.corrective_writer import (
    generate_corrective_actions,
    write_corrective_events,
)
from management.disaster_recovery.kafka_reader import ParsedReplicationEvent
from management.relation_replicator.outbox_replicator import InMemoryLog, OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.relation_replicator.types import (
    ObjectReference,
    ObjectType,
    RelationTuple,
    SubjectReference,
)


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


def _make_event(relations_to_add=None, relations_to_remove=None, offset=0):
    return ParsedReplicationEvent(
        offset=offset,
        partition=0,
        timestamp_ms=1000,
        event_type="create_workspace",
        relations_to_add=relations_to_add or [],
        relations_to_remove=relations_to_remove or [],
    )


class GenerateCorrectiveActionsTest(TestCase):
    def test_add_exists_skip(self):
        t = _make_tuple()
        event = _make_event(relations_to_add=[t])
        existence_map = {("workspace", "ws-123"): True}

        actions = generate_corrective_actions([event], existence_map)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "skip")
        self.assertIn("exists", actions[0].reason)

    def test_add_not_exists_corrective_delete(self):
        t = _make_tuple()
        event = _make_event(relations_to_add=[t])
        existence_map = {("workspace", "ws-123"): False}

        actions = generate_corrective_actions([event], existence_map)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "remove")
        self.assertIn("orphaned", actions[0].reason)

    def test_remove_exists_corrective_add(self):
        t = _make_tuple()
        event = _make_event(relations_to_remove=[t])
        existence_map = {("workspace", "ws-123"): True}

        actions = generate_corrective_actions([event], existence_map)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "add")
        self.assertIn("restoration", actions[0].reason)

    def test_remove_not_exists_skip(self):
        t = _make_tuple()
        event = _make_event(relations_to_remove=[t])
        existence_map = {("workspace", "ws-123"): False}

        actions = generate_corrective_actions([event], existence_map)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "skip")
        self.assertIn("correct", actions[0].reason)

    def test_unknown_resource_defaults_to_skip(self):
        t = _make_tuple(resource_type="unknown_thing", resource_id="x")
        event = _make_event(relations_to_add=[t])
        existence_map = {}

        actions = generate_corrective_actions([event], existence_map)

        self.assertEqual(actions[0].action, "skip")

    def test_multiple_events(self):
        t1 = _make_tuple(resource_id="ws-1")
        t2 = _make_tuple(resource_id="ws-2")
        t3 = _make_tuple(resource_id="ws-3")
        event = _make_event(relations_to_add=[t1, t2], relations_to_remove=[t3])
        existence_map = {
            ("workspace", "ws-1"): True,
            ("workspace", "ws-2"): False,
            ("workspace", "ws-3"): True,
        }

        actions = generate_corrective_actions([event], existence_map)

        self.assertEqual(len(actions), 3)
        self.assertEqual(actions[0].action, "skip")
        self.assertEqual(actions[1].action, "remove")
        self.assertEqual(actions[2].action, "add")

    def test_preserves_source_event_info(self):
        t = _make_tuple()
        event = _make_event(relations_to_add=[t], offset=42)
        existence_map = {("workspace", "ws-123"): False}

        actions = generate_corrective_actions([event], existence_map)

        self.assertEqual(actions[0].source_event_offset, 42)
        self.assertEqual(actions[0].source_event_partition, 0)


class WriteCorrectiveEventsTest(TestCase):
    def test_writes_corrective_add_to_outbox(self):
        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        t = _make_tuple()
        event = _make_event(relations_to_remove=[t])
        existence_map = {("workspace", "ws-123"): True}
        actions = generate_corrective_actions([event], existence_map)

        result = write_corrective_events(actions, replicator)

        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(result["corrective_removes"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(len(log), 1)

        outbox_entry = log.first()
        self.assertEqual(outbox_entry.event_type, ReplicationEventType.DR_CORRECTIVE_ADD)
        self.assertIn("relations_to_add", outbox_entry.payload)
        self.assertEqual(len(outbox_entry.payload["relations_to_add"]), 1)

    def test_writes_corrective_remove_to_outbox(self):
        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        t = _make_tuple()
        event = _make_event(relations_to_add=[t])
        existence_map = {("workspace", "ws-123"): False}
        actions = generate_corrective_actions([event], existence_map)

        result = write_corrective_events(actions, replicator)

        self.assertEqual(result["corrective_adds"], 0)
        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(len(log), 1)

        outbox_entry = log.first()
        self.assertEqual(outbox_entry.event_type, ReplicationEventType.DR_CORRECTIVE_REMOVE)
        self.assertIn("relations_to_remove", outbox_entry.payload)
        self.assertEqual(len(outbox_entry.payload["relations_to_remove"]), 1)

    def test_skips_not_written_to_outbox(self):
        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        t = _make_tuple()
        event = _make_event(relations_to_add=[t])
        existence_map = {("workspace", "ws-123"): True}
        actions = generate_corrective_actions([event], existence_map)

        result = write_corrective_events(actions, replicator)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(log), 0)

    def test_mixed_actions(self):
        log = InMemoryLog()
        replicator = OutboxReplicator(log=log)

        t_add = _make_tuple(resource_id="ws-orphaned")
        t_remove = _make_tuple(resource_id="ws-restored")
        t_skip = _make_tuple(resource_id="ws-ok")

        event = _make_event(
            relations_to_add=[t_add, t_skip],
            relations_to_remove=[t_remove],
        )
        existence_map = {
            ("workspace", "ws-orphaned"): False,
            ("workspace", "ws-restored"): True,
            ("workspace", "ws-ok"): True,
        }
        actions = generate_corrective_actions([event], existence_map)

        result = write_corrective_events(actions, replicator)

        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(log), 2)
