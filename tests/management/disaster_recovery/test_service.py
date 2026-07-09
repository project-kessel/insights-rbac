"""Tests for disaster recovery service orchestrator."""

from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from api.models import Tenant
from management.disaster_recovery.kafka_reader import ParsedReplicationEvent
from management.disaster_recovery.service import reconcile
from management.group.model import Group
from management.principal.model import Principal
from management.relation_replicator.outbox_replicator import InMemoryLog, OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.relation_replicator.types import (
    ObjectReference,
    ObjectType,
    RelationTuple,
    SubjectReference,
)
from management.role.model import Role
from management.tenant_mapping.model import TenantMapping
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


class ReconcileAllResourceTypesTest(TestCase):
    """Verify the full reconciliation pipeline for every supported resource type."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="test-dr-resource-types",
            account_id="rt-account",
            org_id="rt-org",
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
    def test_role_corrective_remove(self, mock_read):
        """Role in relations_to_add that doesn't exist -> corrective REMOVE."""
        fake_uuid = str(uuid4())
        t = _make_tuple(resource_type="role", resource_id=fake_uuid)

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="create_custom_role",
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(result["corrective_adds"], 0)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_REMOVE)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_role_corrective_add(self, mock_read):
        """Role in relations_to_remove that exists -> corrective ADD."""
        role = Role.objects.create(name="DR Existing Role", tenant=self.tenant)
        t = _make_tuple(resource_type="role", resource_id=str(role.uuid))

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="delete_custom_role",
                relations_to_add=[],
                relations_to_remove=[t],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(result["corrective_removes"], 0)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_ADD)
        role.delete()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_group_corrective_remove(self, mock_read):
        """Group in relations_to_add that doesn't exist -> corrective REMOVE."""
        fake_uuid = str(uuid4())
        t = _make_tuple(resource_type="group", resource_id=fake_uuid)

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="create_group",
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_REMOVE)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_group_corrective_add(self, mock_read):
        """Group in relations_to_remove that exists -> corrective ADD."""
        group = Group.objects.create(name="DR Existing Group", tenant=self.tenant)
        t = _make_tuple(resource_type="group", resource_id=str(group.uuid))

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="delete_group",
                relations_to_add=[],
                relations_to_remove=[t],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_ADD)
        group.delete()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_principal_with_domain_prefix_corrective_remove(self, mock_read):
        """Principal with domain prefix that doesn't exist -> corrective REMOVE."""
        t = _make_tuple(resource_type="principal", resource_id="localhost/nonexistent-uid")

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="add_principals_to_group",
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_removes"], 1)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_principal_with_domain_prefix_corrective_add(self, mock_read):
        """Principal with domain prefix that exists -> corrective ADD when in relations_to_remove."""
        principal = Principal.objects.create(username="dr-user", user_id="dr-uid-123", tenant=self.tenant)
        t = _make_tuple(resource_type="principal", resource_id=f"localhost/{principal.user_id}")

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="remove_principals_from_group",
                relations_to_add=[],
                relations_to_remove=[t],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_adds"], 1)
        principal.delete()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_role_binding_corrective_remove(self, mock_read):
        """RoleBinding in relations_to_add that doesn't exist -> corrective REMOVE."""
        fake_uuid = str(uuid4())
        t = _make_tuple(resource_type="role_binding", resource_id=fake_uuid)

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="assign_role",
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_removes"], 1)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_tenant_with_domain_prefix_corrective(self, mock_read):
        """Tenant with domain prefix that exists -> SKIP when in relations_to_add."""
        t = _make_tuple(resource_type="tenant", resource_id=f"localhost/{self.tenant.org_id}")

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="bootstrap_tenant",
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["corrective_adds"], 0)
        self.assertEqual(result["corrective_removes"], 0)
        self.assertEqual(len(log), 0)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_unknown_resource_type_defaults_to_skip(self, mock_read):
        """Unknown resource type -> defaults to 'exists' (safe skip)."""
        t = _make_tuple(resource_type="some_future_resource", resource_id="xyz")

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="some_event",
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(log), 0)


class ReconcileBootstrapTenantTest(TestCase):
    """Verify reconciliation correctly handles bootstrap_tenant events.

    Bootstrap events are structurally different: they create many tuples at once
    across multiple resource types (workspace, group, role_binding, tenant).
    Groups and role_bindings from bootstrap are "virtual" -- they exist as UUIDs
    in TenantMapping but not as actual model instances.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="test-dr-bootstrap",
            account_id="bs-account",
            org_id="bs-org",
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
        cls.mapping = TenantMapping.objects.create(tenant=cls.tenant)

    @classmethod
    def tearDownClass(cls):
        cls.mapping.delete()
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super().tearDownClass()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_bootstrap_event_with_existing_tenant(self, mock_read):
        """Bootstrap event where tenant, workspace, and virtual resources all exist -> all SKIP."""
        tuples = [
            _make_tuple(resource_type="workspace", resource_id=str(self.root_ws.id)),
            _make_tuple(resource_type="group", resource_id=str(self.mapping.default_group_uuid)),
            _make_tuple(resource_type="group", resource_id=str(self.mapping.default_admin_group_uuid)),
            _make_tuple(resource_type="role_binding", resource_id=str(self.mapping.default_role_binding_uuid)),
            _make_tuple(
                resource_type="role_binding", resource_id=str(self.mapping.root_scope_default_role_binding_uuid)
            ),
            _make_tuple(resource_type="tenant", resource_id=f"localhost/{self.tenant.org_id}"),
        ]

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="bootstrap_tenant",
                org_id=self.tenant.org_id,
                relations_to_add=tuples,
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["skipped"], len(tuples))
        self.assertEqual(result["corrective_adds"], 0)
        self.assertEqual(result["corrective_removes"], 0)
        self.assertEqual(len(log), 0)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_bootstrap_event_with_deleted_tenant(self, mock_read):
        """Bootstrap event where all resources were deleted -> all REMOVE."""
        fake_ws = str(uuid4())
        fake_group = str(uuid4())
        fake_rb = str(uuid4())

        tuples = [
            _make_tuple(resource_type="workspace", resource_id=fake_ws),
            _make_tuple(resource_type="group", resource_id=fake_group),
            _make_tuple(resource_type="role_binding", resource_id=fake_rb),
            _make_tuple(resource_type="tenant", resource_id="localhost/deleted-org"),
        ]

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="bootstrap_tenant",
                org_id="deleted-org",
                relations_to_add=tuples,
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_removes"], len(tuples))
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(log), len(tuples))
        for entry in log:
            self.assertEqual(entry.event_type, ReplicationEventType.DR_CORRECTIVE_REMOVE)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_bootstrap_event_partial_state(self, mock_read):
        """Bootstrap event: workspace exists, but group was deleted -> mixed SKIP + REMOVE."""
        fake_group = str(uuid4())

        tuples_add = [
            _make_tuple(resource_type="workspace", resource_id=str(self.root_ws.id)),
            _make_tuple(resource_type="group", resource_id=fake_group),
        ]

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="bootstrap_tenant",
                org_id=self.tenant.org_id,
                relations_to_add=tuples_add,
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(len(log), 1)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_REMOVE)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_bootstrap_event_virtual_role_bindings_via_tenant_mapping(self, mock_read):
        """All 6 TenantMapping role binding UUIDs are recognized as existing."""
        tuples = [
            _make_tuple(resource_type="role_binding", resource_id=str(self.mapping.default_role_binding_uuid)),
            _make_tuple(resource_type="role_binding", resource_id=str(self.mapping.default_admin_role_binding_uuid)),
            _make_tuple(
                resource_type="role_binding", resource_id=str(self.mapping.root_scope_default_role_binding_uuid)
            ),
            _make_tuple(
                resource_type="role_binding",
                resource_id=str(self.mapping.root_scope_default_admin_role_binding_uuid),
            ),
            _make_tuple(
                resource_type="role_binding",
                resource_id=str(self.mapping.tenant_scope_default_role_binding_uuid),
            ),
            _make_tuple(
                resource_type="role_binding",
                resource_id=str(self.mapping.tenant_scope_default_admin_role_binding_uuid),
            ),
        ]

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="bootstrap_tenant",
                org_id=self.tenant.org_id,
                relations_to_add=tuples,
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["skipped"], 6)
        self.assertEqual(result["corrective_removes"], 0)
        self.assertEqual(len(log), 0)


class ReconcileMultiEventTest(TestCase):
    """Verify reconciliation handles multiple event types in the same window."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="test-dr-multi",
            account_id="multi-account",
            org_id="multi-org",
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
    def test_multiple_event_types_in_window(self, mock_read):
        """Window contains workspace, role, and group events with different outcomes."""
        ws = Workspace.objects.create(
            name="Multi Workspace",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
        )
        role = Role.objects.create(name="Multi Role", tenant=self.tenant)

        fake_group_uuid = str(uuid4())
        fake_rb_uuid = str(uuid4())

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="create_workspace",
                relations_to_add=[_make_tuple("workspace", str(ws.id))],
                relations_to_remove=[],
            ),
            ParsedReplicationEvent(
                offset=1,
                partition=0,
                timestamp_ms=1100,
                event_type="delete_custom_role",
                relations_to_add=[],
                relations_to_remove=[_make_tuple("role", str(role.uuid))],
            ),
            ParsedReplicationEvent(
                offset=2,
                partition=0,
                timestamp_ms=1200,
                event_type="create_group",
                relations_to_add=[_make_tuple("group", fake_group_uuid)],
                relations_to_remove=[],
            ),
            ParsedReplicationEvent(
                offset=3,
                partition=0,
                timestamp_ms=1300,
                event_type="assign_role",
                relations_to_add=[_make_tuple("role_binding", fake_rb_uuid)],
                relations_to_remove=[],
            ),
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["events_read"], 4)
        self.assertEqual(result["tuples_processed"], 4)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(result["corrective_removes"], 2)
        self.assertEqual(len(log), 3)

        event_types = [entry.event_type for entry in log]
        self.assertEqual(event_types.count(ReplicationEventType.DR_CORRECTIVE_ADD), 1)
        self.assertEqual(event_types.count(ReplicationEventType.DR_CORRECTIVE_REMOVE), 2)

        ws.delete()
        role.delete()

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_overlapping_events_same_tuple(self, mock_read):
        """Same tuple appearing in multiple events is processed independently (idempotent)."""
        fake_ws = str(uuid4())
        t = _make_tuple("workspace", fake_ws)

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="create_workspace",
                relations_to_add=[t],
                relations_to_remove=[],
            ),
            ParsedReplicationEvent(
                offset=1,
                partition=0,
                timestamp_ms=1100,
                event_type="update_workspace",
                relations_to_add=[t],
                relations_to_remove=[],
            ),
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_removes"], 2)
        self.assertEqual(len(log), 2)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_different_relation_kinds_same_resource(self, mock_read):
        """Tuples with different relation kinds ('parent', 'member') for the same resource."""
        ws = Workspace.objects.create(
            name="Relation Kind WS",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
        )
        t_parent = RelationTuple(
            resource=ObjectReference(type=ObjectType(namespace="rbac", name="workspace"), id=str(ws.id)),
            relation="parent",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="workspace"), id="ws-parent"),
            ),
        )
        t_user_grant = RelationTuple(
            resource=ObjectReference(type=ObjectType(namespace="rbac", name="workspace"), id=str(ws.id)),
            relation="user_grant",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="role_binding"), id="rb-1"),
            ),
        )

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="create_workspace",
                relations_to_add=[t_parent, t_user_grant],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["skipped"], 2)
        self.assertEqual(len(log), 0)
        ws.delete()
