"""
Copyright 2025 Red Hat, Inc.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import uuid
from unittest.mock import patch
from django.test import override_settings
from management.models import BindingMapping, Workspace
from management.principal.model import Principal
from management.relation_replicator.noop_replicator import NoopReplicator
from management.role.model import Role
from management.role.v2_model import CustomRoleV2, SeededRoleV2
from management.role_binding.model import RoleBinding
from management.tenant_service import V2TenantBootstrapService
from management.workspace.service import WorkspaceService
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    all_of,
    resource,
    relation,
    subject,
    resource_type,
    subject_type,
)
from migration_tool.migrate_binding_scope import migrate_all_role_bindings
from migration_tool.utils import create_relationship
from internal.migrations.remove_orphan_relations import (
    cleanup_tenant_orphaned_relationships,
    cleanup_tenant_orphan_bindings,
)
from internal.utils import rebuild_tenant_workspace_relations
from tests.management.role.test_dual_write import DualWriteTestCase
from tests.util import assert_v1_v2_tuples_fully_consistent
from tests.v2_util import make_read_tuples_mock


@override_settings(ATOMIC_RETRY_DISABLED=True)
class CleanupOrphanBindingsTest(DualWriteTestCase):
    """Tests for the cleanup_tenant_orphan_bindings endpoint."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self._bootstrap_tenant()

    def _bootstrap_tenant(self):
        """Set up workspace relationships and default access for test tenant."""
        V2TenantBootstrapService(replicator=InMemoryRelationReplicator(self.tuples)).bootstrap_tenant(
            self.tenant, force=True
        )

    def _expect_v2_consistent(self):
        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

    def _create_kessel_read_tuples_mock(self):
        """Create a mock function that reads tuples from our InMemoryTuples store."""
        return make_read_tuples_mock(self.tuples)

    def _enable_replicate_mock(self, mock_replicate):
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

    def _disable_replicate_mock(self, mock_replicate):
        mock_replicate.side_effect = NoopReplicator().replicate

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_cleanup_orphaned_group_relationships(self, mock_replicate):
        """
        Test cleanup of orphaned group relationships after a group is deleted.

        Scenario:
        1. Create a custom role and assign it to a group
        2. Manually delete the group from DB but leave orphaned tuples in Kessel
        3. Run cleanup API
        4. Verify orphaned group relationships are removed
        5. Run migration to recreate correct state
        """
        # Redirect replicator to in-memory tuples
        self._enable_replicate_mock(mock_replicate)

        # Step 1: Create custom role with default scope permission
        role = self.given_v1_role(
            "Test Role",
            default=["inventory:hosts:read"],
        )

        # Step 2: Create a group and assign the role
        group, _ = self.given_group("Test Group", ["user1"])
        self.given_roles_assigned_to_group(group, [role])

        member_relation_predicate = all_of(
            resource_type("rbac", "role_binding"),
            relation("subject"),
            subject("rbac", "group", str(group.uuid), "member"),
        )

        # Verify binding exists with group
        binding = BindingMapping.objects.filter(role=role).first()
        self.assertIsNotNone(binding)
        self.assertIn(str(group.uuid), binding.mappings["groups"])

        # Verify tuples exist for the group assignment
        group_tuples_before = self.tuples.find_tuples(member_relation_predicate)
        self.assertEqual(len(group_tuples_before), 1, "Should have group assignment tuples")

        # Step 3: Simulate orphaned state by deleting group from DB but dropping the replication event.
        # This simulates what happens when replication fails during deletion
        self.given_group_removed(group, replicator=NoopReplicator())

        # Check that the tuples still exist.
        orphaned_group_tuples = self.tuples.find_tuples(member_relation_predicate)
        self.assertEqual(len(orphaned_group_tuples), 1, "Should have orphaned group tuples")

        # Step 4: Run cleanup using the utility function
        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=False,
        )

        # Verify cleanup found bindings
        self.assertEqual(result["ordinary_bindings_altered_count"], 1, "Should have altered the role binding")

        # Step 5: Verify orphaned group tuples are removed
        orphaned_group_tuples_after = self.tuples.find_tuples(member_relation_predicate)
        self.assertEqual(len(orphaned_group_tuples_after), 0, "Orphaned group tuples should be removed")

        # Step 6: Verify scope binding relationships are preserved. Since the role is a custom role, these should be
        # unaffected by the removal of the group.
        binding_id = binding.mappings["id"]
        default_workspace = Workspace.objects.default(tenant=self.tenant)
        scope_binding_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(default_workspace.id)),
                relation("binding"),
                subject("rbac", "role_binding", binding_id),
            )
        )
        self.assertEqual(len(scope_binding_tuples), 1, "Scope binding tuples should not have been removed")

        self._expect_v2_consistent()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_cleanup_orphaned_principal_relationships(self, mock_replicate):
        """Test that an orphaned role binding to principal subject relation is removed."""
        self._enable_replicate_mock(mock_replicate)

        role = self.given_v1_system_role(name="system role", permissions=["rbac:roles:read"])

        # Create the user and assign the group to the role (to prevent the role binding from being fully deleted).
        group, _ = self.given_group("group", ["u1"])
        self.given_roles_assigned_to_group(group, [role])

        car = self.given_car("u1", [role])
        role_binding = RoleBinding.objects.filter(role__v1_source=role).get()

        subject_relation = all_of(
            resource("rbac", "role_binding", str(role_binding.uuid)),
            relation("subject"),
            subject("rbac", "principal", Principal.user_id_to_principal_resource_id("u1")),
        )

        self.assertEqual(self.tuples.count_tuples(subject_relation), 1, "Subject relation should have been replicated")

        self.given_car_expired(car, replicator=NoopReplicator())

        self.assertEqual(
            self.tuples.count_tuples(subject_relation), 1, "Subject relation should not have been removed"
        )

        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=False,
        )

        self.assertEqual(self.tuples.count_tuples(subject_relation), 0, "Subject relation should not been removed")

        self.assertEqual(result["relations_removed_count"], 1, "Only the subject relation should have been removed")
        self.assertEqual(result["ordinary_bindings_altered_count"], 1, "One role binding should have been altered")

        self._expect_v2_consistent()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_cleanup_orphaned_custom_role_relationships(self, mock_replicate):
        """
        Test cleanup of orphaned custom V2 role relationships after a role is deleted.

        Scenario:
        1. Create a custom role and assign it to a group
        2. Manually delete the role from DB but leave orphaned tuples in Kessel
        3. Run cleanup API
        4. Verify orphaned role and permission relationships are removed
        """
        # Redirect replicator to in-memory tuples
        self._enable_replicate_mock(mock_replicate)

        # Step 1: Create custom role
        role = self.given_v1_role(
            "Custom Role To Delete",
            default=["inventory:hosts:read", "inventory:hosts:write"],
        )

        # Step 2: Create a group and assign the role
        group, _ = self.given_group("Test Group", ["user1"])
        self.given_roles_assigned_to_group(group, [role])

        # Get the V2 role ID from the binding
        binding = BindingMapping.objects.filter(role=role).first()
        self.assertIsNotNone(binding)
        v2_role_id = binding.mappings["role"]["id"]
        binding_id = binding.mappings["id"]

        role_relation_predicate = all_of(
            resource("rbac", "role_binding", binding_id),
            relation("role"),
            subject("rbac", "role", v2_role_id),
        )

        role_permission_predicate = all_of(
            resource("rbac", "role", v2_role_id),
            subject("rbac", "principal", "*"),
        )

        # Verify role tuples exist
        role_tuples_before = self.tuples.find_tuples(role_relation_predicate)
        self.assertEqual(len(role_tuples_before), 1, "Should have role binding tuple")

        # Verify permission tuples exist for the V2 role
        permission_tuples_before = self.tuples.find_tuples(role_permission_predicate)
        self.assertEqual(len(permission_tuples_before), 2, "Should have permission tuples")

        # Step 3: Delete the role from DB while dropping the replication event.
        self.given_v1_role_removed(role, replicator=NoopReplicator())

        # Verify role is gone from DB
        self.assertFalse(Role.objects.filter(uuid=role.uuid).exists())

        # But tuples still exist (orphaned)
        orphaned_role_tuples = self.tuples.find_tuples(role_relation_predicate)
        self.assertEqual(len(orphaned_role_tuples), 1, "Should have orphaned role tuples")

        # Step 4: Run cleanup
        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=False,
        )

        # Verify cleanup found custom V2 roles to clean
        self.assertEqual(result["custom_v2_roles_altered_count"], 1, "Should find custom V2 roles to clean")

        # Step 5: Verify orphaned tuples are removed
        # Role binding to role tuple should be gone
        role_tuples_after = self.tuples.find_tuples(role_relation_predicate)
        self.assertEqual(len(role_tuples_after), 0, "Role binding tuples should be removed")

        # Permission tuples should be gone
        permission_tuples_after = self.tuples.find_tuples(role_permission_predicate)
        self.assertEqual(len(permission_tuples_after), 0, "Permission tuples should be removed")

        # Scope binding relationships should also be removed
        default_workspace = Workspace.objects.default(tenant=self.tenant)
        scope_binding_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(default_workspace.id)),
                relation("binding"),
                subject("rbac", "role_binding", binding_id),
            )
        )
        self.assertEqual(len(scope_binding_tuples), 0, "Scope binding tuples should be removed")

        self._expect_v2_consistent()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_custom_role_removed_permission(self, mock_replicate):
        self._enable_replicate_mock(mock_replicate)

        role_v1 = self.given_v1_role("custom role", default=["rbac:roles:read", "rbac:roles:write"])
        role_v2 = CustomRoleV2.objects.filter(v1_source=role_v1).get()

        read_permission_predicate = all_of(
            resource("rbac", "role", str(role_v2.uuid)),
            relation("rbac_roles_read"),
            subject("rbac", "principal", "*"),
        )

        write_permission_predicate = all_of(
            resource("rbac", "role", str(role_v2.uuid)),
            relation("rbac_roles_write"),
            subject("rbac", "principal", "*"),
        )

        self.assertEqual(
            self.tuples.count_tuples(write_permission_predicate),
            1,
            "Write permission should have been replicated.",
        )

        self.assertEqual(
            self.tuples.count_tuples(read_permission_predicate),
            1,
            "Read permission should have been replicated.",
        )

        # Remove the write permission without updating relations.
        self.given_update_to_v1_role(role_v1, default=["rbac:roles:read"], replicator=NoopReplicator())

        self.assertEqual(
            self.tuples.count_tuples(write_permission_predicate),
            1,
            "Removal of write permission should not have been replicated.",
        )

        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=False,
        )

        self.assertEqual(result["custom_v2_roles_altered_count"], 1, "The custom role should have been altered.")
        self.assertEqual(result["relations_removed_count"], 1, "The write permission should have been removed.")

        self.assertEqual(
            self.tuples.count_tuples(write_permission_predicate),
            0,
            "Migration should have removed write permission.",
        )

        self.assertEqual(
            self.tuples.count_tuples(read_permission_predicate),
            1,
            "Migration should not have removed read permission.",
        )

        self._expect_v2_consistent()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_cleanup_orphaned_scope_binding_relationships(self, mock_replicate):
        """
        Test that cleanup removes orphaned scope binding relationships.

        Scenario:
        1. Create a role and assign it to a group
        2. Delete the binding from DB but leave scope tuples in Kessel
        3. Run cleanup API
        4. Verify workspace → binding → role_binding tuples are removed
        """
        # Redirect replicator to in-memory tuples
        self._enable_replicate_mock(mock_replicate)

        # Step 1: Create role and assign to group
        role = self.given_v1_role("Test Role", default=["inventory:hosts:read"])
        group, _ = self.given_group("Test Group", ["user1"])
        self.given_roles_assigned_to_group(group, [role])

        # Get binding info
        binding = BindingMapping.objects.filter(role=role).first()
        self.assertIsNotNone(binding)
        binding_id = binding.mappings["id"]

        # Get workspace
        default_workspace = Workspace.objects.default(tenant=self.tenant)

        binding_relation_predicate = all_of(
            resource("rbac", "workspace", str(default_workspace.id)),
            relation("binding"),
            subject("rbac", "role_binding", binding_id),
        )

        # Verify relationship from workspace to tuple still exists.
        scope_tuples_before = self.tuples.find_tuples(binding_relation_predicate)
        self.assertEqual(len(scope_tuples_before), 1, "Should have scope binding tuples")

        # Step 2: Remove the role (and thus the relevant role binding) from the database while dropping the replication
        # event (thus orphaning the tuples).
        self.given_v1_role_removed(role, replicator=NoopReplicator())

        # Check that the tuples still exist.
        orphaned_scope_tuples = self.tuples.find_tuples(binding_relation_predicate)
        self.assertEqual(len(orphaned_scope_tuples), 1, "Should have orphaned scope tuples")

        # Step 3: Run cleanup
        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=False,
        )

        # Verify cleanup found bindings
        self.assertEqual(result["ordinary_bindings_altered_count"], 1, "Should have altered a single role binding")

        # Step 4: Verify orphaned scope binding tuples are removed
        scope_tuples_after = self.tuples.find_tuples(binding_relation_predicate)
        self.assertEqual(len(scope_tuples_after), 0, "Orphaned scope binding tuples should be removed")

        self._expect_v2_consistent()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_cleanup_orphaned_workspace_and_stale_parent(self, mock_replicate):
        """
        Test cleanup of orphaned workspace and stale parent relationships.

        Scenario - complex hierarchy:
        - Original: default-ws -> ws1 -> ws2 -> ws3
        - ws2 gets deleted from DB, ws3 is re-parented to ws1
        - Current state:
          - DB:     default-ws -> ws1 -> ws3 (ws2 doesn't exist)
          - Kessel: default-ws -> ws1 -> ws2 -> ws3 (ws2 still exists)
        - After cleanup:
          - ws2 is orphaned (not in DB) -> ws2's parent tuple removed
          - ws3 has stale parent (Kessel says ws2, DB says ws1) -> ws3's stale parent tuple removed
          - Any bindings on ws2 should be cleaned
        """
        # Redirect replicator to in-memory tuples
        self._enable_replicate_mock(mock_replicate)

        workspace_service = WorkspaceService()

        default_workspace = Workspace.objects.default(tenant=self.tenant)
        default_ws_id = str(default_workspace.id)

        # Create the relevant workspaces (and replicate the relevant tuples).
        ws1 = workspace_service.create({"name": "Workspace 1", "parent_id": default_ws_id}, request_tenant=self.tenant)
        ws2 = workspace_service.create({"name": "Workspace 2", "parent_id": str(ws1.id)}, request_tenant=self.tenant)
        ws3 = workspace_service.create({"name": "Workspace 3", "parent_id": str(ws2.id)}, request_tenant=self.tenant)

        ws1_id = str(ws1.id)
        ws2_id = str(ws2.id)
        ws3_id = str(ws3.id)

        # Create a role binding that references Workspace 2.
        # We use inventory:groups:* so that the resource definition is properly removed when the workspace is.
        self.given_v1_role("Workspace 2 role", default=[], **{ws2_id: ["inventory:groups:*"]})

        # Remove ws2 and move ws3 directly under ws1
        self._disable_replicate_mock(mock_replicate)
        workspace_service.move(ws3, ws1.id)
        workspace_service.destroy(ws2)

        ws2_parent_predicate = all_of(
            resource("rbac", "workspace", ws2_id),
            relation("parent"),
            subject("rbac", "workspace", ws1_id),
        )

        ws3_parent_predicate = all_of(
            resource("rbac", "workspace", ws3_id),
            relation("parent"),
            subject("rbac", "workspace", ws2_id),
        )

        ws2_binding_predicate = all_of(
            resource("rbac", "workspace", ws2_id),
            relation("binding"),
            subject_type("rbac", "role_binding"),
        )

        # Verify initial state
        # ws2 -> parent -> ws1 exists
        ws2_parent_before = self.tuples.find_tuples(ws2_parent_predicate)
        self.assertEqual(len(ws2_parent_before), 1, "ws2 should have parent tuple")

        # ws3 -> parent -> ws2 exists (stale)
        ws3_parent_before = self.tuples.find_tuples(ws3_parent_predicate)
        self.assertEqual(len(ws3_parent_before), 1, "ws3 should have stale parent tuple to ws2")

        # ws2 binding exists
        ws2_binding_before = self.tuples.find_tuples(ws2_binding_predicate)
        self.assertEqual(len(ws2_binding_before), 1, "ws2 should have binding tuple")

        orphan_binding_id = ws2_binding_before.only.subject.subject.id

        # Run cleanup.
        self._enable_replicate_mock(mock_replicate)
        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=False,
        )

        self.assertEqual(
            result["orphaned_workspace_relations_cleaned_count"], 1, "Should have removed one orphaned workspace (ws2)"
        )

        self.assertEqual(
            result["stale_parent_workspace_relations_cleaned_count"],
            1,
            "Should have removed one stale parent relation (ws3 -> ws2)",
        )

        self.assertEqual(
            result["ordinary_bindings_altered_count"], 1, "Should have removed one role binding (for ws2)"
        )

        # Verify ws2's parent tuple (ws2 -> parent -> ws1) is removed
        ws2_parent_after = self.tuples.find_tuples(ws2_parent_predicate)
        self.assertEqual(len(ws2_parent_after), 0, "ws2's parent tuple should be removed")

        # Verify ws3's stale parent tuple (ws3 -> parent -> ws2) is removed
        ws3_stale_parent_after = self.tuples.find_tuples(ws3_parent_predicate)
        self.assertEqual(len(ws3_stale_parent_after), 0, "ws3's stale parent tuple to ws2 should be removed")

        # Verify ws2's binding tuple is removed
        ws2_binding_after = self.tuples.find_tuples(ws2_binding_predicate)
        self.assertEqual(len(ws2_binding_after), 0, "ws2's binding tuple should be removed")

        # Verify binding's role tuple is removed
        binding_role_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", orphan_binding_id),
                relation("role"),
            )
        )
        self.assertEqual(len(binding_role_after), 0, "Orphaned binding's role tuple should be removed")

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_cleanup_skips_builtin_bindings_without_custom_default_group(self, mock_replicate):
        """
        Test that cleanup skips built-in bindings when tenant has no custom default group.

        The 6 built-in bindings (user/admin for tenant/root/default scope) should be
        skipped entirely when the tenant doesn't have a custom default group.
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Get TenantMapping built-in binding UUIDs
        tenant_mapping = self.tenant.tenant_mapping
        builtin_binding_uuids = {
            str(tenant_mapping.default_role_binding_uuid),
            str(tenant_mapping.default_admin_role_binding_uuid),
            str(tenant_mapping.root_scope_default_role_binding_uuid),
            str(tenant_mapping.root_scope_default_admin_role_binding_uuid),
            str(tenant_mapping.tenant_scope_default_role_binding_uuid),
            str(tenant_mapping.tenant_scope_default_admin_role_binding_uuid),
        }

        # Create a custom role to have something to clean
        role = self.given_v1_role("Test Role", default=["inventory:hosts:read"])
        group, _ = self.given_group("Test Group", ["user1"])
        self.given_roles_assigned_to_group(group, [role])

        # Run cleanup in dry_run mode
        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=True,
        )

        # Verify no built-in scope bindings were cleaned (count should be 0 since no custom default group)
        self.assertEqual(
            result["builtin_bindings_scope_cleaned_count"], 0, "No built-in scope bindings should be cleaned"
        )

        self._expect_v2_consistent()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_cleanup_does_not_remove_system_role_permissions(self, mock_replicate):
        """
        Test that cleanup does NOT remove permission tuples for system roles.

        System roles are never deleted, so their permission tuples should be preserved.
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Create a system role
        system_role = self.given_v1_system_role(
            "System Role",
            permissions=["inventory:hosts:read"],
        )
        system_role_uuid = str(system_role.uuid)

        # Create a group and assign the system role
        group, _ = self.given_group("Test Group", ["user1"])
        self.given_roles_assigned_to_group(group, [system_role])

        # Run cleanup
        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=True,
        )

        # Verify system role is NOT counted as custom V2 role
        # System roles should be filtered out because they're in system_role_uuids
        self.assertEqual(
            result["custom_v2_roles_altered_count"],
            0,
            "System role should NOT be counted as custom V2 role to clean",
        )

        self._expect_v2_consistent()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_cleanup_and_migration_fixes_orphans(self, mock_replicate):
        """
        End-to-end test: cleanup orphans then run migration to restore correct state.

        Scenario:
        1. Create role, group, and assign role to group
        2. Simulate orphaned relationships (group removed from role but tuple remains)
        3. Run cleanup
        4. Run migration
        5. Verify correct final state
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Step 1: Create role and groups
        role = self.given_v1_role("Test Role", default=["inventory:hosts:read"])

        group1, _ = self.given_group("Group 1", ["user1"])
        group2, _ = self.given_group("Group 2", ["user2"])

        # Assign role to both groups
        self.given_roles_assigned_to_group(group1, [role])
        self.given_roles_assigned_to_group(group2, [role])

        # Verify both groups are in the binding
        binding = BindingMapping.objects.filter(role=role).first()
        self.assertIn(str(group1.uuid), binding.mappings["groups"])
        self.assertIn(str(group2.uuid), binding.mappings["groups"])

        binding_id = binding.mappings["id"]

        # Verify tuples for both groups
        group1_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("subject"),
                subject("rbac", "group", str(group1.uuid), "member"),
            )
        )
        self.assertEqual(len(group1_tuples), 1)

        group2_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("subject"),
                subject("rbac", "group", str(group2.uuid), "member"),
            )
        )
        self.assertEqual(len(group2_tuples), 1)

        # Step 2: Simulate orphan - remove group2 from DB but leave tuple
        group2_uuid = str(group2.uuid)

        # Remove group2 from binding mappings (simulating DB cleanup)
        binding.mappings["groups"] = [str(group1.uuid)]
        binding.save()

        # Unassign role from group2
        self.given_roles_unassigned_from_group(group2, [role])

        # Delete group2
        group2.delete()

        # Manually add orphaned tuple back (simulating failed replication)
        orphan_tuple = create_relationship(
            ("rbac", "role_binding"),
            binding_id,
            ("rbac", "group"),
            group2_uuid,
            "subject",
            subject_relation="member",
        )
        self.tuples.add(orphan_tuple)

        # Verify orphaned tuple exists
        orphan_check = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("subject"),
                subject("rbac", "group", group2_uuid, "member"),
            )
        )
        self.assertEqual(len(orphan_check), 1, "Orphan tuple should exist")

        # Step 3: Run cleanup
        cleanup_result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=False,
        )

        # Step 4: Run migration
        checked, migrated = migrate_all_role_bindings(
            replicator=InMemoryRelationReplicator(self.tuples),
            tenant=self.tenant,
        )

        # Step 5: Verify final state
        # Orphaned group2 tuple should be gone
        orphan_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("subject"),
                subject("rbac", "group", group2_uuid, "member"),
            )
        )
        self.assertEqual(len(orphan_after), 0, "Orphan tuple should be removed after cleanup")

        # Group1 tuple should still exist (from migration)
        final_binding = BindingMapping.objects.filter(role=role).first()
        if final_binding:
            final_binding_id = final_binding.mappings["id"]
            group1_after = self.tuples.find_tuples(
                all_of(
                    resource("rbac", "role_binding", final_binding_id),
                    relation("subject"),
                    subject("rbac", "group", str(group1.uuid), "member"),
                )
            )
            self.assertEqual(len(group1_after), 1, "Group1 tuple should exist after migration")

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_dry_run_does_not_modify_tuples(self, mock_replicate):
        """
        Test that dry_run=True only reports what would be deleted without making changes.
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Create role and group
        role = self.given_v1_role("Test Role", default=["inventory:hosts:read"])
        group, _ = self.given_group("Test Group", ["user1"])
        self.given_roles_assigned_to_group(group, [role])

        # Make a change that needs to be fixed.
        self.given_group_removed(group, replicator=NoopReplicator())

        # Record tuple count before
        tuple_count_before = len(self.tuples)

        # Run cleanup in dry_run mode
        result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=True,
        )

        # Verify dry_run flag in result
        self.assertTrue(result["dry_run"])

        # Nothing should have been replicated.
        mock_replicate.assert_not_called()

        # Verify tuple count unchanged
        tuple_count_after = len(self.tuples)
        self.assertEqual(tuple_count_before, tuple_count_after, "Dry run should not modify tuples")

        # Should have relations_removed_count in result
        self.assertIn("relations_removed_count", result)
        self.assertEqual(result["relations_removed_count"], 1, "Should have removed one subject relations")
        self.assertEqual(result["ordinary_bindings_altered_count"], 1, "Should have altered one role binding")

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_preserve_system(self, replicate):
        """Test that the full migration does not break system role bindings with multiple groups."""
        replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        r = self.given_v1_system_role("system_role", ["rbac:roles:read"])

        g1, _ = self.given_group("g1", ["p1"])
        g2, _ = self.given_group("g2", ["p2"])

        self.given_roles_assigned_to_group(g1, [r])
        self.given_roles_assigned_to_group(g2, [r])

        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

        cleanup_tenant_orphan_bindings(
            org_id=self.tenant.org_id,
            dry_run=False,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
        )

        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_miscellaneous_unchanged(self, replicate):
        """Test that a state with several interesting cases (all properly replicated) is left unchanged."""
        self._enable_replicate_mock(replicate)

        workspace_service = WorkspaceService()
        ws = workspace_service.create(validated_data={"name": "a workspace"}, request_tenant=self.tenant)

        custom_role = self.given_v1_role(
            "custom role", default=["rbac:roles:read"], **{str(ws.id): ["inventory:groups:read"]}
        )

        system_role = self.given_v1_system_role("system role", ["rbac:roles:read", "rbac:roles:write"])

        group, _ = self.given_group("group", ["u1"])

        self.given_car("u1", [system_role])
        self.given_roles_assigned_to_group(group, [system_role, custom_role])

        initial_tuples = set(self.tuples)

        # Test the removal part of the script.
        cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=True,
        )

        self.assertSetEqual(set(self.tuples), initial_tuples, "No relations should have been affected.")

        # Test the full script (with both removal and addition).
        cleanup_tenant_orphan_bindings(
            org_id=self.tenant.org_id,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=True,
        )

        self.assertSetEqual(set(self.tuples), initial_tuples, "No relations should have been affected.")


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RebuildTenantWorkspaceRelationsTest(DualWriteTestCase):
    """Tests for the rebuild_tenant_workspace_relations endpoint."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Switch to a new tenant because we care about the exact number of workspaces in the tenant (and
        # DualWriteTestCase creates some extra ones for its own purposes).
        self.switch_to_new_tenant(name="another tenant", org_id="7654321")

        # Clear tuples, since we want to test rebuilding it
        self.tuples.clear()

    def _create_kessel_read_tuples_mock(self):
        """Create a mock function that reads tuples from our InMemoryTuples store."""

        def read_tuples_fn(resource_type_name, resource_id, relation_name, subject_type_name, subject_id):
            """Mock function to read tuples from InMemoryTuples."""
            # Build a filter based on the provided parameters
            filters = [resource_type("rbac", resource_type_name)]

            if resource_id:
                filters.append(resource("rbac", resource_type_name, resource_id))

            if relation_name:
                filters.append(relation(relation_name))

            tuples = self.tuples.find_tuples(all_of(*filters))

            # Convert to dict format matching Kessel gRPC response
            # Format: {"tuple": {"resource": {...}, "relation": "...", "subject": {...}}, ...}
            result = []
            for t in tuples:
                # Filter by subject type and id if provided
                if subject_type_name and t.subject.subject.type.name != subject_type_name:
                    continue
                if subject_id and t.subject.subject.id != subject_id:
                    continue

                result.append(
                    {
                        "tuple": {
                            "resource": {
                                "type": {
                                    "namespace": t.resource.type.namespace,
                                    "name": t.resource.type.name,
                                },
                                "id": t.resource.id,
                            },
                            "relation": t.relation,
                            "subject": {
                                "subject": {
                                    "type": {
                                        "namespace": t.subject.subject.type.namespace,
                                        "name": t.subject.subject.type.name,
                                    },
                                    "id": t.subject.subject.id,
                                },
                                "relation": t.subject.relation,
                            },
                        },
                    }
                )
            return result

        return read_tuples_fn

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_rebuild_creates_missing_workspace_parent_relations(self, mock_replicate):
        """
        Test that rebuild_tenant_workspace_relations creates missing parent relations.

        Scenario:
        1. Workspaces exist in DB but have no parent tuples in Kessel (in-memory)
        2. Run rebuild_tenant_workspace_relations
        3. Verify parent tuples are created for all workspaces
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        root_workspace = Workspace.objects.root(tenant=self.tenant)
        default_workspace = Workspace.objects.default(tenant=self.tenant)
        root_ws_id = str(root_workspace.id)
        default_ws_id = str(default_workspace.id)
        tenant_resource_id = self.tenant.tenant_resource_id()

        # Verify NO parent tuples exist initially for root workspace
        root_parent_before = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", root_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(root_parent_before), 0, "Root workspace should have no parent tuple initially")

        # Verify NO parent tuples exist initially for default workspace
        default_parent_before = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(default_parent_before), 0, "Default workspace should have no parent tuple initially")

        # Run rebuild
        result = rebuild_tenant_workspace_relations(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            replicator=InMemoryRelationReplicator(self.tuples),
            dry_run=False,
        )

        # Verify result
        self.assertEqual(result["org_id"], self.tenant.org_id)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["workspaces_checked"], 1)
        self.assertEqual(result["workspaces_missing_parent"], 1)
        self.assertEqual(result["relations_to_add"], 1)
        self.assertEqual(result["relations_added"], 1)

        # Root workspaces are not linked to the tenant in Kessel.
        root_parent_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", root_ws_id),
                relation("parent"),
                subject("rbac", "tenant", tenant_resource_id),
            )
        )
        self.assertEqual(len(root_parent_after), 0, "Root workspace should not get a parent tuple to tenant")

        # Verify default workspace now has parent tuple -> root workspace
        default_parent_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("parent"),
                subject("rbac", "workspace", root_ws_id),
            )
        )
        self.assertEqual(len(default_parent_after), 1, "Default workspace should have parent tuple to root workspace")

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_rebuild_detects_orphaned_workspaces_with_bindings(self, mock_replicate):
        """
        Test that rebuild identifies workspaces that have bindings but no parent.

        Scenario:
        1. Workspace exists in DB with no parent tuple in Kessel
        2. Workspace has binding tuples in Kessel (orphaned state)
        3. Run rebuild
        4. Verify workspace is identified as orphaned
        5. Verify parent relation is created
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        default_workspace = Workspace.objects.default(tenant=self.tenant)
        default_ws_id = str(default_workspace.id)

        # Simulate orphaned state: add binding tuples but no parent tuple
        orphan_binding_id = str(uuid.uuid4())
        self.tuples.add(
            create_relationship(
                ("rbac", "workspace"),
                default_ws_id,
                ("rbac", "role_binding"),
                orphan_binding_id,
                "binding",
            )
        )

        # Verify binding exists but no parent
        binding_before = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("binding"),
            )
        )
        self.assertEqual(len(binding_before), 1, "Should have binding tuple")

        parent_before = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(parent_before), 0, "Should have no parent tuple")

        # Run rebuild
        result = rebuild_tenant_workspace_relations(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            replicator=InMemoryRelationReplicator(self.tuples),
            dry_run=False,
        )

        # Verify workspace with missing parent was detected
        self.assertGreater(result["workspaces_missing_parent"], 0, "Should detect workspaces with missing parent")

        # Verify parent relation was created (default -> root chain only)
        parent_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(parent_after), 1, "Should have parent tuple after rebuild")

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_rebuild_dry_run_does_not_create_tuples(self, mock_replicate):
        """
        Test that dry_run=True only reports what would be added without making changes.
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        root_workspace = Workspace.objects.root(tenant=self.tenant)
        root_ws_id = str(root_workspace.id)

        # Verify no parent tuples exist initially
        parent_before = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", root_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(parent_before), 0, "Should have no parent tuple initially")

        # Record tuple count before
        tuple_count_before = len(self.tuples)

        # Run rebuild in dry_run mode
        result = rebuild_tenant_workspace_relations(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            replicator=InMemoryRelationReplicator(self.tuples),
            dry_run=True,
        )

        # Verify dry_run flag in result
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["relations_to_add"], 1, "Should only plan default workspace parent relation")
        self.assertEqual(result["relations_added"], 0, "Should not have added relations in dry run")

        # Verify tuple count unchanged
        tuple_count_after = len(self.tuples)
        self.assertEqual(tuple_count_before, tuple_count_after, "Dry run should not modify tuples")

        # Verify parent tuples still don't exist
        parent_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", root_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(parent_after), 0, "Should still have no parent tuple after dry run")

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_rebuild_skips_existing_parent_relations(self, mock_replicate):
        """
        Test that rebuild does not recreate parent relations that already exist.
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        root_workspace = Workspace.objects.root(tenant=self.tenant)
        default_workspace = Workspace.objects.default(tenant=self.tenant)
        root_ws_id = str(root_workspace.id)
        default_ws_id = str(default_workspace.id)
        tenant_resource_id = self.tenant.tenant_resource_id()

        # Pre-create parent relation for root workspace only
        self.tuples.add(
            create_relationship(
                ("rbac", "workspace"),
                root_ws_id,
                ("rbac", "tenant"),
                tenant_resource_id,
                "parent",
            )
        )

        # Verify root has parent, default doesn't
        root_parent_before = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", root_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(root_parent_before), 1, "Root workspace should have parent tuple")

        default_parent_before = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(default_parent_before), 0, "Default workspace should have no parent tuple")

        # Run rebuild (legacy root->tenant tuple is ignored for rebuild purposes)
        result = rebuild_tenant_workspace_relations(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            replicator=InMemoryRelationReplicator(self.tuples),
            dry_run=False,
        )

        # Verify only the default workspace was checked (root is not replicated to tenant)
        self.assertEqual(result["workspaces_checked"], 1)
        self.assertEqual(result["workspaces_missing_parent"], 1)
        self.assertEqual(result["relations_to_add"], 1)
        self.assertEqual(result["relations_added"], 1)

        # Legacy root parent tuple remains until removed by cleanup/cron
        root_parent_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", root_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(root_parent_after), 1, "Legacy root parent tuple should be unchanged")

        # Verify default now has parent tuple
        default_parent_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(default_parent_after), 1, "Default workspace should now have parent tuple")

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_rebuild_then_cleanup_finds_all_workspaces(self, mock_replicate):
        """
        Integration test: rebuild workspace relations, then cleanup can discover all workspaces.

        Uses DB-backed workspace seeds for Kessel reads; root workspaces are not parent-linked to the tenant.
        """
        # Redirect replicator to in-memory tuples
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        root_workspace = Workspace.objects.root(tenant=self.tenant)
        default_workspace = Workspace.objects.default(tenant=self.tenant)
        root_ws_id = str(root_workspace.id)
        default_ws_id = str(default_workspace.id)

        # Layer 3: Create child workspace (parent = default)
        child_workspace = Workspace.objects.create(
            name="Child Workspace",
            tenant=self.tenant,
            parent=default_workspace,
            type=Workspace.Types.STANDARD,
        )
        child_ws_id = str(child_workspace.id)

        # Layer 4: Create grandchild workspace (parent = child)
        grandchild_workspace = Workspace.objects.create(
            name="Grandchild Workspace",
            tenant=self.tenant,
            parent=child_workspace,
            type=Workspace.Types.STANDARD,
        )
        grandchild_ws_id = str(grandchild_workspace.id)

        # Simulate orphaned state: add bindings to workspaces but no parent relations anywhere
        # Add binding to child workspace
        orphan_binding_1 = str(uuid.uuid4())
        self.tuples.add(
            create_relationship(
                ("rbac", "workspace"),
                child_ws_id,
                ("rbac", "role_binding"),
                orphan_binding_1,
                "binding",
            )
        )

        # Add binding to grandchild workspace (deepest level)
        orphan_binding_2 = str(uuid.uuid4())
        self.tuples.add(
            create_relationship(
                ("rbac", "workspace"),
                grandchild_ws_id,
                ("rbac", "role_binding"),
                orphan_binding_2,
                "binding",
            )
        )

        # Verify no parent relations exist initially
        all_parent_tuples = self.tuples.find_tuples(
            all_of(
                resource_type("rbac", "workspace"),
                relation("parent"),
            )
        )
        self.assertEqual(len(all_parent_tuples), 0, "Should have no parent tuples initially")

        # Step 1: Run rebuild
        rebuild_result = rebuild_tenant_workspace_relations(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            replicator=InMemoryRelationReplicator(self.tuples),
            dry_run=False,
        )

        # Verify default + nested workspaces get parent relations (root has no parent tuple)
        self.assertEqual(rebuild_result["workspaces_checked"], 3)
        self.assertEqual(rebuild_result["relations_to_add"], 3)
        self.assertEqual(rebuild_result["relations_added"], 3)

        # Verify parent tuples were created
        all_parent_after = self.tuples.find_tuples(
            all_of(
                resource_type("rbac", "workspace"),
                relation("parent"),
            )
        )
        self.assertEqual(len(all_parent_after), 3, "Should have 3 parent tuples after rebuild")

        # Root should remain without a parent tuple
        root_parent = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", root_ws_id),
                relation("parent"),
            )
        )
        self.assertEqual(len(root_parent), 0, "Root should have no parent tuples")

        # Default -> root
        default_parent = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("parent"),
                subject("rbac", "workspace", root_ws_id),
            )
        )
        self.assertEqual(len(default_parent), 1, "Default should have parent = root")

        # Layer 3: child -> parent -> default
        child_parent = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", child_ws_id),
                relation("parent"),
                subject("rbac", "workspace", default_ws_id),
            )
        )
        self.assertEqual(len(child_parent), 1, "Child should have parent = default")

        # Layer 4: grandchild -> parent -> child
        grandchild_parent = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", grandchild_ws_id),
                relation("parent"),
                subject("rbac", "workspace", child_ws_id),
            )
        )
        self.assertEqual(len(grandchild_parent), 1, "Grandchild should have parent = child")

        # Step 2: Run cleanup - DFS should now discover all workspaces
        cleanup_result = cleanup_tenant_orphaned_relationships(
            tenant=self.tenant,
            read_tuples_fn=self._create_kessel_read_tuples_mock(),
            dry_run=True,
        )

        # Verification traverses parent edges only; root has no parent tuple so it is not counted.
        self.assertEqual(
            cleanup_result["workspaces_discovered_count"],
            3,
            "DFS records workspaces reached via workspace#parent@workspace (not the root)",
        )
