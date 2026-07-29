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
"""Tests for workspace disaster recovery corrective event generation."""

import uuid

from django.test import TestCase

from api.models import Tenant
from core.kafka_dr import KafkaEvent
from management.relation_replicator.outbox_replicator import InMemoryLog
from management.relation_replicator.relation_replicator import AggregateTypes, ReplicationEventType
from management.workspace.dr_recovery import (
    generate_corrective_workspace_events,
    parse_workspace_kafka_events,
)
from management.workspace.model import Workspace


def _make_kafka_event(
    workspace_id: str,
    operation: str,
    org_id: str = "12345",
    account_number: str = "67890",
    ws_name: str = "Test Workspace",
    ws_type: str = "standard",
    timestamp_ms: int = 1000000,
    fmt: str = "nested",
) -> KafkaEvent:
    """Build a KafkaEvent that mimics a Debezium workspace outbox message.

    fmt="nested" (default): pre-SMT format with aggregatetype + nested payload.
    fmt="flat": EventRouter SMT format where the payload IS the message value.
    fmt="envelope": Kafka Connect JsonConverter envelope with schema + payload.
    """
    payload = {
        "org_id": org_id,
        "account_number": account_number,
        "operation": operation,
        "workspace": {
            "id": workspace_id,
            "name": ws_name,
            "type": ws_type,
            "created": "2026-05-15T10:00:00Z",
            "modified": "2026-05-15T10:00:00Z",
        },
    }

    if fmt == "flat":
        value = payload
    elif fmt == "envelope":
        value = {
            "schema": {"type": "struct", "fields": []},
            "payload": payload,
        }
    else:
        value = {
            "aggregatetype": AggregateTypes.WORKSPACE.value,
            "aggregateid": "production",
            "type": f"{operation}_workspace",
            "payload": payload,
        }

    return KafkaEvent(
        topic="outbox.event.workspace",
        partition=0,
        offset=0,
        timestamp_ms=timestamp_ms,
        value=value,
    )


class TestParseWorkspaceKafkaEvents(TestCase):
    """Tests for parse_workspace_kafka_events."""

    def test_filters_non_workspace_events(self):
        """Events with non-workspace aggregate type are filtered out."""
        event = KafkaEvent(
            topic="outbox.event.workspace",
            partition=0,
            offset=0,
            timestamp_ms=1000,
            value={
                "aggregatetype": "relations-replication-event",
                "aggregateid": "production",
                "type": "create_workspace",
                "payload": {
                    "relations_to_add": [],
                    "relations_to_remove": [],
                },
            },
        )
        result = parse_workspace_kafka_events([event])
        self.assertEqual(result, [])

    def test_filters_system_workspace_types(self):
        """ROOT and UNGROUPED_HOSTS workspace types are filtered out."""
        root_event = _make_kafka_event(str(uuid.uuid4()), "create", ws_type="root")
        ungrouped_event = _make_kafka_event(str(uuid.uuid4()), "create", ws_type="ungrouped-hosts")
        result = parse_workspace_kafka_events([root_event, ungrouped_event])
        self.assertEqual(result, [])

    def test_passes_standard_and_default_types(self):
        """STANDARD and DEFAULT workspace types are included."""
        standard_id = str(uuid.uuid4())
        default_id = str(uuid.uuid4())
        events = [
            _make_kafka_event(standard_id, "create", ws_type="standard"),
            _make_kafka_event(default_id, "create", ws_type="default"),
        ]
        result = parse_workspace_kafka_events(events)
        ws_ids = {e["workspace_id"] for e in result}
        self.assertEqual(ws_ids, {standard_id, default_id})

    def test_deduplicates_by_workspace_id_keeps_latest(self):
        """Multiple events for the same workspace keep only the latest."""
        ws_id = str(uuid.uuid4())
        create_event = _make_kafka_event(ws_id, "create", timestamp_ms=1000)
        update_event = _make_kafka_event(ws_id, "update", timestamp_ms=2000)
        result = parse_workspace_kafka_events([create_event, update_event])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation"], "update")

    def test_skips_invalid_operations(self):
        """Events with invalid operation values are skipped."""
        event = _make_kafka_event(str(uuid.uuid4()), "invalid_op")
        # Manually patch the operation to something invalid
        event.value["payload"]["operation"] = "invalid_op"
        result = parse_workspace_kafka_events([event])
        self.assertEqual(result, [])

    def test_skips_missing_workspace_id(self):
        """Events without a workspace ID are skipped."""
        event = _make_kafka_event("", "create")
        result = parse_workspace_kafka_events([event])
        self.assertEqual(result, [])

    def test_empty_input(self):
        """Empty event list returns empty result."""
        result = parse_workspace_kafka_events([])
        self.assertEqual(result, [])

    def test_flat_event_router_format(self):
        """EventRouter SMT format (flat payload) is parsed correctly."""
        ws_id = str(uuid.uuid4())
        event = _make_kafka_event(ws_id, "create", fmt="flat")
        result = parse_workspace_kafka_events([event])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["workspace_id"], ws_id)
        self.assertEqual(result[0]["operation"], "create")
        self.assertEqual(result[0]["org_id"], "12345")

    def test_flat_format_filters_system_types(self):
        """EventRouter SMT format still filters root and ungrouped-hosts types."""
        root_event = _make_kafka_event(str(uuid.uuid4()), "create", ws_type="root", fmt="flat")
        ungrouped_event = _make_kafka_event(str(uuid.uuid4()), "create", ws_type="ungrouped-hosts", fmt="flat")
        result = parse_workspace_kafka_events([root_event, ungrouped_event])
        self.assertEqual(result, [])

    def test_flat_format_deduplicates(self):
        """EventRouter SMT format deduplicates by workspace ID."""
        ws_id = str(uuid.uuid4())
        create_event = _make_kafka_event(ws_id, "create", timestamp_ms=1000, fmt="flat")
        update_event = _make_kafka_event(ws_id, "update", timestamp_ms=2000, fmt="flat")
        result = parse_workspace_kafka_events([create_event, update_event])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation"], "update")

    def test_json_converter_envelope_format(self):
        """Kafka Connect JsonConverter envelope (schema + payload) is parsed correctly."""
        ws_id = str(uuid.uuid4())
        event = _make_kafka_event(ws_id, "update", fmt="envelope")
        result = parse_workspace_kafka_events([event])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["workspace_id"], ws_id)
        self.assertEqual(result[0]["operation"], "update")

    def test_mixed_all_formats(self):
        """All three formats (nested, flat, envelope) work together."""
        ws_id_nested = str(uuid.uuid4())
        ws_id_flat = str(uuid.uuid4())
        ws_id_envelope = str(uuid.uuid4())
        events = [
            _make_kafka_event(ws_id_nested, "delete", fmt="nested"),
            _make_kafka_event(ws_id_flat, "create", fmt="flat"),
            _make_kafka_event(ws_id_envelope, "update", fmt="envelope"),
        ]
        result = parse_workspace_kafka_events(events)
        ws_ids = {e["workspace_id"] for e in result}
        self.assertEqual(ws_ids, {ws_id_nested, ws_id_flat, ws_id_envelope})


class TestGenerateCorrectiveWorkspaceEvents(TestCase):
    """Tests for the 6 truth table cases of corrective event generation."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="acct_dr_test",
            account_id="67890",
            org_id="12345",
            ready=True,
        )
        cls.root_ws = Workspace.objects.create(
            name="Root Workspace",
            type=Workspace.Types.ROOT,
            tenant=cls.tenant,
        )
        cls.default_ws = Workspace.objects.create(
            name="Default Workspace",
            type=Workspace.Types.DEFAULT,
            parent=cls.root_ws,
            tenant=cls.tenant,
        )

    @classmethod
    def tearDownClass(cls):
        """Clean up test fixtures."""
        Workspace.objects.filter(tenant=cls.tenant, type=Workspace.Types.STANDARD).delete()
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super().tearDownClass()

    def _create_test_workspace(self, name: str = "Test Workspace", ws_type: str = "standard") -> Workspace:
        """Create a workspace for testing."""
        return Workspace.objects.create(
            name=name,
            type=ws_type,
            parent=self.default_ws,
            tenant=self.tenant,
        )

    def test_create_event_workspace_not_in_db_writes_delete(self):
        """create event + workspace NOT in RBAC -> write delete corrective event."""
        nonexistent_id = str(uuid.uuid4())
        events = [_make_kafka_event(nonexistent_id, "create")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["corrective_deletes"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(len(log), 1)
        outbox_entry = log[0]
        self.assertEqual(outbox_entry.aggregatetype, "workspace")
        self.assertEqual(outbox_entry.payload["operation"], "delete")
        self.assertEqual(outbox_entry.payload["workspace"]["id"], nonexistent_id)

    def test_create_event_workspace_exists_skips(self):
        """create event + workspace EXISTS in RBAC -> SKIP."""
        ws = self._create_test_workspace()
        events = [_make_kafka_event(str(ws.id), "create")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["corrective_deletes"], 0)
        self.assertEqual(stats["corrective_creates"], 0)
        self.assertEqual(len(log), 0)
        ws.delete()

    def test_delete_event_workspace_exists_writes_create(self):
        """delete event + workspace EXISTS in RBAC -> write create corrective event."""
        ws = self._create_test_workspace(name="Recovered WS")
        events = [_make_kafka_event(str(ws.id), "delete")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["corrective_creates"], 1)
        self.assertEqual(len(log), 1)
        outbox_entry = log[0]
        self.assertEqual(outbox_entry.payload["operation"], "create")
        self.assertEqual(outbox_entry.payload["workspace"]["id"], str(ws.id))
        self.assertEqual(outbox_entry.payload["workspace"]["name"], "Recovered WS")
        self.assertEqual(outbox_entry.payload["org_id"], str(self.tenant.org_id))
        ws.delete()

    def test_delete_event_workspace_not_in_db_skips(self):
        """delete event + workspace NOT in RBAC -> SKIP."""
        nonexistent_id = str(uuid.uuid4())
        events = [_make_kafka_event(nonexistent_id, "delete")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["corrective_creates"], 0)
        self.assertEqual(len(log), 0)

    def test_update_event_workspace_exists_writes_update(self):
        """update event + workspace EXISTS in RBAC -> write update corrective event."""
        ws = self._create_test_workspace(name="Stale WS")
        events = [_make_kafka_event(str(ws.id), "update")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["corrective_updates"], 1)
        self.assertEqual(len(log), 1)
        outbox_entry = log[0]
        self.assertEqual(outbox_entry.payload["operation"], "update")
        self.assertEqual(outbox_entry.payload["workspace"]["name"], "Stale WS")
        ws.delete()

    def test_update_event_workspace_not_in_db_writes_delete(self):
        """update event + workspace NOT in RBAC -> write delete corrective event."""
        nonexistent_id = str(uuid.uuid4())
        events = [_make_kafka_event(nonexistent_id, "update")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["corrective_deletes"], 1)
        self.assertEqual(len(log), 1)
        outbox_entry = log[0]
        self.assertEqual(outbox_entry.payload["operation"], "delete")

    def test_empty_event_window(self):
        """No events produces zero corrective events."""
        log = InMemoryLog()
        stats = generate_corrective_workspace_events([], outbox_log=log)

        self.assertEqual(stats["total_events"], 0)
        self.assertEqual(stats["corrective_creates"], 0)
        self.assertEqual(stats["corrective_deletes"], 0)
        self.assertEqual(stats["corrective_updates"], 0)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(len(log), 0)

    def test_multiple_events_for_same_workspace_deduplicates(self):
        """Multiple Kafka events for the same workspace only produce one corrective event."""
        nonexistent_id = str(uuid.uuid4())
        events = [
            _make_kafka_event(nonexistent_id, "create", timestamp_ms=1000),
            _make_kafka_event(nonexistent_id, "update", timestamp_ms=2000),
        ]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["total_events"], 1)
        self.assertEqual(stats["corrective_deletes"], 1)
        self.assertEqual(len(log), 1)

    def test_mixed_events_across_workspaces(self):
        """Multiple workspaces with different operations are handled correctly."""
        ws_exists = self._create_test_workspace(name="Existing WS")
        ws_missing_id = str(uuid.uuid4())
        ws_stale = self._create_test_workspace(name="Stale Data WS")

        events = [
            _make_kafka_event(str(ws_exists.id), "create"),
            _make_kafka_event(ws_missing_id, "create"),
            _make_kafka_event(str(ws_stale.id), "update"),
        ]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["total_events"], 3)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["corrective_deletes"], 1)
        self.assertEqual(stats["corrective_updates"], 1)
        self.assertEqual(len(log), 2)

        ws_exists.delete()
        ws_stale.delete()

    def test_corrective_create_uses_current_db_state(self):
        """Create corrective events use current RBAC workspace data, not Kafka event data."""
        ws = self._create_test_workspace(name="Current Name")
        events = [_make_kafka_event(str(ws.id), "delete", ws_name="Old Kafka Name")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["corrective_creates"], 1)
        outbox_entry = log[0]
        self.assertEqual(outbox_entry.payload["workspace"]["name"], "Current Name")
        ws.delete()

    def test_corrective_delete_uses_kafka_event_data(self):
        """Delete corrective events use the original Kafka event data (workspace no longer in DB)."""
        nonexistent_id = str(uuid.uuid4())
        events = [_make_kafka_event(nonexistent_id, "create", ws_name="Kafka Name")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["corrective_deletes"], 1)
        outbox_entry = log[0]
        self.assertEqual(outbox_entry.payload["workspace"]["name"], "Kafka Name")
        self.assertEqual(outbox_entry.payload["workspace"]["id"], nonexistent_id)

    def test_outbox_entry_format_matches_production(self):
        """Corrective outbox entries match the production workspace event format."""
        ws = self._create_test_workspace(name="Format Check WS")
        events = [_make_kafka_event(str(ws.id), "delete")]
        log = InMemoryLog()

        generate_corrective_workspace_events(events, outbox_log=log)

        outbox_entry = log[0]
        self.assertEqual(outbox_entry.aggregatetype, "workspace")
        self.assertEqual(outbox_entry.event_type, ReplicationEventType.CREATE_WORKSPACE)

        payload = outbox_entry.payload
        self.assertIn("org_id", payload)
        self.assertIn("account_number", payload)
        self.assertIn("operation", payload)
        self.assertIn("workspace", payload)

        ws_data = payload["workspace"]
        self.assertIn("id", ws_data)
        self.assertIn("name", ws_data)
        self.assertIn("type", ws_data)
        self.assertIn("created", ws_data)
        self.assertIn("modified", ws_data)

        ws.delete()

    def test_default_workspace_type_is_processed(self):
        """Default workspace type events are processed (not filtered out)."""
        default_ws_id = str(uuid.uuid4())
        events = [_make_kafka_event(default_ws_id, "create", ws_type="default")]
        log = InMemoryLog()

        stats = generate_corrective_workspace_events(events, outbox_log=log)

        self.assertEqual(stats["corrective_deletes"], 1)
        self.assertEqual(len(log), 1)
