"""Integration tests: DR corrective events verified by parity checks.

These tests simulate realistic disaster recovery scenarios where:
1. A DB restore occurs, creating divergence between RBAC and Kessel/SpiceDB
2. DR reconciliation generates corrective events
3. Parity checks verify the corrective events would fix the divergence (happy path)
4. Parity checks catch failures when corrective events fail to apply (unhappy path)

The corrective events drive the test scenarios -- not manual test setup of
matching DB/PDP state. Each test starts from "after the disaster" and exercises
the full chain: Kafka event reading → resource checking → corrective event
generation → outbox writing → parity verification.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from tests.identity_request import IdentityRequest
from tests.management.disaster_recovery.helpers import _make_tuple

from api.models import Tenant
from management.disaster_recovery.corrective_writer import (
    generate_corrective_actions,
    write_corrective_events,
)
from management.disaster_recovery.kafka_reader import ParsedReplicationEvent
from management.disaster_recovery.service import reconcile
from management.group.model import Group
from management.parity_check.checker import ParityAccessChecker, ParityCheckResult
from management.principal.model import Principal
from management.relation_replicator.outbox_replicator import InMemoryLog, OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import Role
from management.role.v2_model import CustomRoleV2
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from management.tenant_mapping.model import TenantMapping
from management.workspace.model import Workspace


class DRParityHappyPathTest(IdentityRequest):
    """After successful DR reconciliation, parity checks should confirm consistency."""

    @classmethod
    def setUpClass(cls):
        # Skip IdentityRequest's tenant creation — we manage our own DR fixtures
        super(IdentityRequest, cls).setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="dr-parity-happy",
            account_id="parity-account",
            org_id="parity-org",
            ready=True,
        )
        cls.mapping = TenantMapping.objects.create(
            tenant=cls.tenant,
            v2_write_activated_at=timezone.now(),
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
        cls.mapping.delete()
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super(IdentityRequest, cls).tearDownClass()

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_corrective_add_restores_parity_for_workspace_access(self, mock_pdp_cls, mock_read):
        """Scenario: DB restored a workspace that was deleted during the lost window.

        The delete event in Kafka has relations_to_remove for a workspace that now
        exists again in RBAC. DR generates corrective ADD. After the corrective ADD
        is applied to SpiceDB, the parity check should confirm the principal's
        workspace access matches between RBAC and PDP.
        """
        ws = Workspace.objects.create(
            name="Restored WS",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
            parent=self.default_ws,
        )
        principal = Principal.objects.create(
            username="parity-user", user_id="parity-uid", tenant=self.tenant, type=Principal.Types.USER
        )
        role = CustomRoleV2.objects.create(name="Parity Role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=role, resource_type="workspace", resource_id=str(ws.id), tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(binding=binding, principal=principal, source="test")

        t = _make_tuple(resource_type="workspace", resource_id=str(ws.id))
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
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_ADD)

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = {str(ws.id)}
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertEqual(parity_result.only_in_rbac, set(), "Unexpected workspaces only in RBAC")
        self.assertEqual(parity_result.only_in_pdp, set(), "Unexpected workspaces only in PDP")
        self.assertTrue(parity_result.match, f"Parity should match after corrective ADD, got: {parity_result}")

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_corrective_remove_cleans_orphaned_access(self, mock_pdp_cls, mock_read):
        """Scenario: Workspace was created during the lost window but DB restore rolled it back.

        The create event in Kafka has relations_to_add for a workspace that no longer
        exists in RBAC. DR generates corrective REMOVE. After the corrective REMOVE is
        applied, PDP should no longer grant access to the orphaned workspace.
        """
        principal = Principal.objects.create(
            username="cleanup-user", user_id="cleanup-uid", tenant=self.tenant, type=Principal.Types.USER
        )
        orphaned_ws_id = str(uuid4())

        t = _make_tuple(resource_type="workspace", resource_id=orphaned_ws_id)
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
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_REMOVE)

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = set()
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertTrue(parity_result.match, "Parity should match after corrective REMOVE")

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_multi_resource_dr_all_corrective_events_restore_parity(self, mock_pdp_cls, mock_read):
        """Scenario: Lost window contains events for workspace, role, and group.

        DR generates a mix of corrective ADDs and REMOVEs. After all corrective
        events are applied, the full parity check shows no discrepancies.
        """
        ws = Workspace.objects.create(
            name="Multi WS", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_ws
        )
        role = Role.objects.create(name="Multi Role", tenant=self.tenant)
        principal = Principal.objects.create(
            username="multi-user", user_id="multi-uid", tenant=self.tenant, type=Principal.Types.USER
        )
        v2_role = CustomRoleV2.objects.create(name="Multi V2 Role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=v2_role, resource_type="workspace", resource_id=str(ws.id), tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(binding=binding, principal=principal, source="test")

        fake_group_id = str(uuid4())

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="delete_workspace",
                relations_to_add=[],
                relations_to_remove=[_make_tuple("workspace", str(ws.id))],
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
                relations_to_add=[_make_tuple("group", fake_group_id)],
                relations_to_remove=[],
            ),
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_adds"], 2)
        self.assertEqual(result["corrective_removes"], 1)

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = {str(ws.id)}
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertTrue(parity_result.match, "Full multi-resource DR should restore parity")

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_role_binding_corrective_add_restores_access_parity(self, mock_pdp_cls, mock_read):
        """Scenario: A role_binding was unassigned during the lost window but DB restored it.

        DR generates corrective ADD for the role_binding relation. After it is applied
        in SpiceDB, the principal should regain workspace access in PDP → parity matches.
        """
        ws = Workspace.objects.create(
            name="RB WS", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_ws
        )
        principal = Principal.objects.create(
            username="rb-user", user_id="rb-uid", tenant=self.tenant, type=Principal.Types.USER
        )
        v2_role = CustomRoleV2.objects.create(name="RB V2 Role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=v2_role, resource_type="workspace", resource_id=str(ws.id), tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(binding=binding, principal=principal, source="test")

        t = _make_tuple(resource_type="role_binding", resource_id=str(binding.uuid))
        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="unassign_role",
                relations_to_add=[],
                relations_to_remove=[t],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_ADD)

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = {str(ws.id)}
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertTrue(parity_result.match, "role_binding corrective ADD should restore access parity")

    @override_settings(DR_SKIP_EVENT_TYPES=[])
    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_tenant_corrective_remove_cleans_orphaned_tenant(self, mock_pdp_cls, mock_read):
        """Scenario: A tenant was bootstrapped during the lost window but DB restore rolled it back.

        DR generates corrective REMOVE for the orphaned tenant relation. After it is applied,
        PDP should no longer know about the tenant → parity check for other tenants unaffected.
        """
        orphaned_org_id = "orphaned-org-" + str(uuid4())[:8]

        t = _make_tuple(resource_type="tenant", resource_id=f"localhost/{orphaned_org_id}")
        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="bootstrap_tenant",
                org_id=orphaned_org_id,
                relations_to_add=[t],
                relations_to_remove=[],
            )
        ]

        log = InMemoryLog()
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(log.first().event_type, ReplicationEventType.DR_CORRECTIVE_REMOVE)
        self.assertEqual(orphaned_org_id, log.first().payload["resource_context"]["org_id"])

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_group_membership_corrective_add_restores_group_access(self, mock_pdp_cls, mock_read):
        """Scenario: A principal was removed from a group during the lost window but DB restored it.

        DR generates corrective ADD for the group membership relation. After it is applied,
        the principal should have group-based workspace access in PDP → parity matches.
        """
        ws = Workspace.objects.create(
            name="Group WS", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_ws
        )
        principal = Principal.objects.create(
            username="grp-user", user_id="grp-uid", tenant=self.tenant, type=Principal.Types.USER
        )
        group = Group.objects.create(name="DR Group", tenant=self.tenant)
        group.principals.add(principal)

        v2_role = CustomRoleV2.objects.create(name="Grp V2 Role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=v2_role, resource_type="workspace", resource_id=str(ws.id), tenant=self.tenant
        )
        RoleBindingGroup.objects.create(binding=binding, group=group)

        t = _make_tuple(resource_type="group", resource_id=str(group.uuid), relation="member")
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

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = {str(ws.id)}
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertTrue(parity_result.match, "Group membership corrective ADD should restore access parity")


class DRParityUnhappyPathTest(IdentityRequest):
    """When DR corrective events fail, parity checks should detect the remaining divergence."""

    @classmethod
    def setUpClass(cls):
        super(IdentityRequest, cls).setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="dr-parity-unhappy",
            account_id="unhappy-account",
            org_id="unhappy-org",
            ready=True,
        )
        cls.mapping = TenantMapping.objects.create(
            tenant=cls.tenant,
            v2_write_activated_at=timezone.now(),
        )
        cls.root_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT, type=Workspace.Types.ROOT, tenant=cls.tenant
        )
        cls.default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT, type=Workspace.Types.DEFAULT, tenant=cls.tenant, parent=cls.root_ws
        )

    @classmethod
    def tearDownClass(cls):
        cls.mapping.delete()
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super(IdentityRequest, cls).tearDownClass()

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_failed_corrective_add_detected_by_parity(self, mock_pdp_cls, mock_read):
        """Scenario: DR corrective ADD fails to write → parity catches the discrepancy.

        The workspace exists in RBAC (DB restored it), principal has a role binding,
        but the corrective ADD that would re-add the relation in SpiceDB fails. PDP
        still has the old state (workspace missing). Parity check detects: workspace
        accessible via RBAC but not via PDP.
        """
        ws = Workspace.objects.create(
            name="Failed ADD WS", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_ws
        )
        principal = Principal.objects.create(
            username="fail-add-user", user_id="fail-add-uid", tenant=self.tenant, type=Principal.Types.USER
        )
        v2_role = CustomRoleV2.objects.create(name="Fail ADD Role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=v2_role, resource_type="workspace", resource_id=str(ws.id), tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(binding=binding, principal=principal, source="test")

        t = _make_tuple(resource_type="workspace", resource_id=str(ws.id))
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

        failing_replicator = MagicMock()
        failing_replicator.replicate.side_effect = RuntimeError("Outbox write failed")

        result = reconcile(restore_timestamp_ms=1000000, replicator=failing_replicator)

        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(result["errors"], 1)

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = set()
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertFalse(parity_result.match, "Parity should detect discrepancy when corrective ADD failed")
        self.assertIn(str(ws.id), parity_result.only_in_rbac)
        self.assertEqual(parity_result.only_in_pdp, set())

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_failed_corrective_remove_leaves_orphaned_access_detected_by_parity(self, mock_pdp_cls, mock_read):
        """Scenario: DR corrective REMOVE fails → orphaned access remains in PDP.

        A workspace was created during the lost window but DB restore rolled it back.
        DR tries to remove the orphaned relation from SpiceDB but the write fails.
        PDP still grants access to the ghost workspace. Parity detects: workspace
        accessible via PDP but not via RBAC.
        """
        orphaned_ws_id = str(uuid4())
        principal = Principal.objects.create(
            username="fail-rm-user", user_id="fail-rm-uid", tenant=self.tenant, type=Principal.Types.USER
        )

        t = _make_tuple(resource_type="workspace", resource_id=orphaned_ws_id)
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

        failing_replicator = MagicMock()
        failing_replicator.replicate.side_effect = RuntimeError("Outbox write failed")

        result = reconcile(restore_timestamp_ms=1000000, replicator=failing_replicator)

        self.assertEqual(result["corrective_removes"], 1)
        self.assertEqual(result["errors"], 1)

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = {orphaned_ws_id}
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertFalse(parity_result.match, "Parity should detect orphaned workspace when corrective REMOVE failed")
        self.assertIn(orphaned_ws_id, parity_result.only_in_pdp)
        self.assertEqual(parity_result.only_in_rbac, set())

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_partial_dr_failure_parity_catches_specific_failures(self, mock_pdp_cls, mock_read):
        """Scenario: Multi-event DR where some corrective events succeed, some fail.

        Two workspaces need corrective ADDs. One succeeds, one fails. Parity check
        should show the successful one as matching and the failed one as discrepancy.
        """
        ws_success = Workspace.objects.create(
            name="Success WS", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_ws
        )
        ws_fail = Workspace.objects.create(
            name="Fail WS", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_ws
        )
        principal = Principal.objects.create(
            username="partial-user", user_id="partial-uid", tenant=self.tenant, type=Principal.Types.USER
        )
        v2_role = CustomRoleV2.objects.create(name="Partial Role", tenant=self.tenant)
        binding_success = RoleBinding.objects.create(
            role=v2_role, resource_type="workspace", resource_id=str(ws_success.id), tenant=self.tenant
        )
        binding_fail = RoleBinding.objects.create(
            role=v2_role, resource_type="workspace", resource_id=str(ws_fail.id), tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(binding=binding_success, principal=principal, source="test")
        RoleBindingPrincipal.objects.create(binding=binding_fail, principal=principal, source="test")

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="delete_workspace",
                relations_to_add=[],
                relations_to_remove=[_make_tuple("workspace", str(ws_success.id))],
            ),
            ParsedReplicationEvent(
                offset=1,
                partition=0,
                timestamp_ms=1100,
                event_type="delete_workspace",
                relations_to_add=[],
                relations_to_remove=[_make_tuple("workspace", str(ws_fail.id))],
            ),
        ]

        call_count = 0

        def fail_second_write(event):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Second outbox write failed")

        partial_replicator = MagicMock()
        partial_replicator.replicate.side_effect = fail_second_write

        result = reconcile(restore_timestamp_ms=1000000, replicator=partial_replicator)

        self.assertEqual(result["corrective_adds"], 2)
        self.assertEqual(result["errors"], 1)

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = {str(ws_success.id)}
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertFalse(parity_result.match, "Parity should fail when one corrective event failed")
        self.assertIn(str(ws_fail.id), parity_result.only_in_rbac)
        self.assertNotIn(str(ws_success.id), parity_result.only_in_rbac)

    @patch("management.disaster_recovery.service.read_events_in_window")
    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_corrective_add_written_but_pdp_not_yet_converged_detected_by_parity(self, mock_pdp_cls, mock_read):
        """Scenario: Corrective ADD written to outbox but SpiceDB hasn't processed it yet.

        The corrective event was successfully written, but there's replication lag between
        outbox → Debezium → Kafka → SpiceDB sink. PDP still returns stale state.
        Parity check should detect this eventual consistency gap.
        """
        ws = Workspace.objects.create(
            name="Lag WS", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_ws
        )
        principal = Principal.objects.create(
            username="lag-user", user_id="lag-uid", tenant=self.tenant, type=Principal.Types.USER
        )
        v2_role = CustomRoleV2.objects.create(name="Lag Role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=v2_role, resource_type="workspace", resource_id=str(ws.id), tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(binding=binding, principal=principal, source="test")

        t = _make_tuple(resource_type="workspace", resource_id=str(ws.id))
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
        result = reconcile(restore_timestamp_ms=1000000, replicator=OutboxReplicator(log=log))

        self.assertEqual(result["corrective_adds"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(len(log), 1)

        mock_pdp = MagicMock()
        mock_pdp.lookup_accessible_workspaces.return_value = set()
        mock_pdp_cls.return_value = mock_pdp

        checker = ParityAccessChecker(tenant_sample_size=1, principal_sample_size=1)
        checker.inventory_checker = mock_pdp
        parity_result = checker.check_principal_parity(principal, self.tenant)

        self.assertFalse(parity_result.match, "Parity detects eventual consistency lag")
        self.assertIn(str(ws.id), parity_result.only_in_rbac)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_outbox_write_error_is_recorded_in_dr_result(self, mock_read):
        """DR result should accurately report the number of failed writes."""
        ws = Workspace.objects.create(
            name="Err Count WS", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_ws
        )

        mock_read.return_value = [
            ParsedReplicationEvent(
                offset=0,
                partition=0,
                timestamp_ms=1000,
                event_type="delete_workspace",
                relations_to_add=[],
                relations_to_remove=[_make_tuple("workspace", str(ws.id))],
            ),
            ParsedReplicationEvent(
                offset=1,
                partition=0,
                timestamp_ms=1100,
                event_type="delete_workspace",
                relations_to_add=[],
                relations_to_remove=[_make_tuple("workspace", str(ws.id))],
            ),
        ]

        failing_replicator = MagicMock()
        failing_replicator.replicate.side_effect = RuntimeError("DB connection lost")

        result = reconcile(restore_timestamp_ms=1000000, replicator=failing_replicator)

        self.assertEqual(result["corrective_adds"], 2)
        self.assertEqual(result["errors"], 2)

    @patch("management.disaster_recovery.service.read_events_in_window")
    def test_negative_buffer_seconds_raises(self, mock_read):
        """reconcile() should reject invalid buffer_seconds."""
        with self.assertRaises(ValueError) as ctx:
            reconcile(restore_timestamp_ms=1000000, buffer_seconds=-1)
        self.assertIn("non-negative", str(ctx.exception))
        mock_read.assert_not_called()


class DRWorkspaceParityIntegrationTest(IdentityRequest):
    """Integration tests for workspace DR (HBI channel) with corrective event verification."""

    @classmethod
    def setUpClass(cls):
        # Skip IdentityRequest's tenant creation — we manage our own DR fixtures
        super(IdentityRequest, cls).setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="dr-ws-parity",
            account_id="ws-account",
            org_id="ws-org",
            ready=True,
        )
        cls.root_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT, type=Workspace.Types.ROOT, tenant=cls.tenant
        )
        cls.default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            tenant=cls.tenant,
            parent=cls.root_ws,
        )

    @classmethod
    def tearDownClass(cls):
        Workspace.objects.filter(tenant=cls.tenant, type=Workspace.Types.STANDARD).delete()
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super(IdentityRequest, cls).tearDownClass()

    def test_all_six_truth_table_cases_produce_correct_outbox_events(self):
        """Run all 6 workspace DR truth table cases and verify each outbox event.

        This consolidates the truth table into one scenario to prove the full set
        of corrective event types fires correctly from realistic Kafka events.
        """
        from core.kafka_dr import KafkaEvent
        from management.relation_replicator.outbox_replicator import InMemoryLog
        from management.relation_replicator.relation_replicator import AggregateTypes, ReplicationEventType
        from management.workspace.dr_recovery import generate_corrective_workspace_events

        ws_exists_1 = Workspace.objects.create(
            name="Exists 1", type=Workspace.Types.STANDARD, parent=self.default_ws, tenant=self.tenant
        )
        ws_exists_2 = Workspace.objects.create(
            name="Exists 2", type=Workspace.Types.STANDARD, parent=self.default_ws, tenant=self.tenant
        )
        ws_exists_3 = Workspace.objects.create(
            name="Exists 3", type=Workspace.Types.STANDARD, parent=self.default_ws, tenant=self.tenant
        )
        gone_id_1 = str(uuid4())
        gone_id_2 = str(uuid4())
        gone_id_3 = str(uuid4())

        def _ws_kafka_event(ws_id, operation, ws_name="WS", ts=1000000):
            return KafkaEvent(
                topic="outbox.event.workspace",
                partition=0,
                offset=0,
                timestamp_ms=ts,
                value={
                    "aggregatetype": AggregateTypes.WORKSPACE.value,
                    "aggregateid": "production",
                    "type": f"{operation}_workspace",
                    "payload": {
                        "org_id": self.tenant.org_id,
                        "account_number": self.tenant.account_id,
                        "operation": operation,
                        "workspace": {
                            "id": ws_id,
                            "name": ws_name,
                            "type": "standard",
                            "created": "2026-01-01T00:00:00Z",
                            "modified": "2026-01-01T00:00:00Z",
                        },
                    },
                },
            )

        kafka_events = [
            _ws_kafka_event(gone_id_1, "create", ts=1000),
            _ws_kafka_event(str(ws_exists_1.id), "create", ts=2000),
            _ws_kafka_event(str(ws_exists_2.id), "delete", ts=3000),
            _ws_kafka_event(gone_id_2, "delete", ts=4000),
            _ws_kafka_event(str(ws_exists_3.id), "update", ts=5000),
            _ws_kafka_event(gone_id_3, "update", ts=6000),
        ]

        log = InMemoryLog()
        stats = generate_corrective_workspace_events(kafka_events, outbox_log=log)

        self.assertEqual(stats["corrective_deletes"], 2)
        self.assertEqual(stats["corrective_creates"], 1)
        self.assertEqual(stats["corrective_updates"], 1)
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(len(log), 4)

        operations = [entry.payload["operation"] for entry in log]
        self.assertEqual(operations.count("delete"), 2)
        self.assertEqual(operations.count("create"), 1)
        self.assertEqual(operations.count("update"), 1)

        delete_ids = {entry.payload["workspace"]["id"] for entry in log if entry.payload["operation"] == "delete"}
        create_ids = {entry.payload["workspace"]["id"] for entry in log if entry.payload["operation"] == "create"}
        update_ids = {entry.payload["workspace"]["id"] for entry in log if entry.payload["operation"] == "update"}

        self.assertEqual(
            delete_ids,
            {gone_id_1, gone_id_3},
            "Corrective deletes: create+gone and update+gone both get deleted; delete+gone is skipped",
        )
        self.assertEqual(
            create_ids, {str(ws_exists_2.id)}, "Corrective create should target the deleted-but-existing ws"
        )
        self.assertEqual(
            update_ids, {str(ws_exists_3.id)}, "Corrective update should target the updated-but-existing ws"
        )

        for entry in log:
            self.assertEqual(entry.aggregatetype, "workspace")
            self.assertIn("org_id", entry.payload)
            self.assertIn("workspace", entry.payload)
            self.assertIn("id", entry.payload["workspace"])

    def test_workspace_corrective_create_uses_db_state_not_kafka_event(self):
        """After DR, corrective CREATE events should reflect current RBAC DB workspace name.

        This verifies the corrective event payload is based on the restored DB state,
        not the stale Kafka event data -- ensuring HBI receives the correct current state.
        """
        from core.kafka_dr import KafkaEvent
        from management.relation_replicator.outbox_replicator import InMemoryLog
        from management.relation_replicator.relation_replicator import AggregateTypes
        from management.workspace.dr_recovery import generate_corrective_workspace_events

        ws = Workspace.objects.create(
            name="Current DB Name", type=Workspace.Types.STANDARD, parent=self.default_ws, tenant=self.tenant
        )
        kafka_events = [
            KafkaEvent(
                topic="outbox.event.workspace",
                partition=0,
                offset=0,
                timestamp_ms=1000,
                value={
                    "aggregatetype": AggregateTypes.WORKSPACE.value,
                    "aggregateid": "production",
                    "type": "delete_workspace",
                    "payload": {
                        "org_id": self.tenant.org_id,
                        "account_number": self.tenant.account_id,
                        "operation": "delete",
                        "workspace": {
                            "id": str(ws.id),
                            "name": "Stale Kafka Name",
                            "type": "standard",
                            "created": "2026-01-01T00:00:00Z",
                            "modified": "2026-01-01T00:00:00Z",
                        },
                    },
                },
            )
        ]

        log = InMemoryLog()
        stats = generate_corrective_workspace_events(kafka_events, outbox_log=log)

        self.assertEqual(stats["corrective_creates"], 1)
        self.assertEqual(log[0].payload["workspace"]["name"], "Current DB Name")
        self.assertNotEqual(log[0].payload["workspace"]["name"], "Stale Kafka Name")

        ws.delete()

    def test_workspace_dr_error_in_single_event_does_not_stop_processing(self):
        """If one workspace corrective event write fails, others should still proceed."""
        from core.kafka_dr import KafkaEvent
        from management.relation_replicator.relation_replicator import AggregateTypes
        from management.workspace.dr_recovery import generate_corrective_workspace_events

        gone_id_1 = str(uuid4())
        gone_id_2 = str(uuid4())

        def _ws_kafka_event(ws_id, operation, ts=1000000):
            return KafkaEvent(
                topic="outbox.event.workspace",
                partition=0,
                offset=0,
                timestamp_ms=ts,
                value={
                    "aggregatetype": AggregateTypes.WORKSPACE.value,
                    "aggregateid": "production",
                    "type": f"{operation}_workspace",
                    "payload": {
                        "org_id": self.tenant.org_id,
                        "account_number": self.tenant.account_id,
                        "operation": operation,
                        "workspace": {
                            "id": ws_id,
                            "name": "WS",
                            "type": "standard",
                            "created": "2026-01-01T00:00:00Z",
                            "modified": "2026-01-01T00:00:00Z",
                        },
                    },
                },
            )

        kafka_events = [
            _ws_kafka_event(gone_id_1, "create", ts=1000),
            _ws_kafka_event(gone_id_2, "create", ts=2000),
        ]

        class FailFirstLog:
            def __init__(self):
                self.entries = []
                self.call_count = 0

            def log(self, outbox):
                self.call_count += 1
                if self.call_count == 1:
                    raise RuntimeError("First write fails")
                self.entries.append(outbox)

        fail_log = FailFirstLog()
        stats = generate_corrective_workspace_events(kafka_events, outbox_log=fail_log)

        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["corrective_deletes"], 1)
        self.assertEqual(len(fail_log.entries), 1)
