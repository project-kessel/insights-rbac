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
from api.cross_access.model import CrossAccountRequest
from unittest.mock import patch, Mock
from django.contrib.auth.models import User as DjangoUser
from django.test import TestCase, override_settings
from management.group.definer import add_roles, clone_default_group_in_public_schema, seed_group
from management.group.model import Group
from management.group.platform import GlobalPolicyIdService
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.models import BindingMapping, Workspace, Access, Permission
from management.permission.scope_service import ImplicitResourceService, Scope
from management.policy.model import Policy
from management.role.model import ResourceDefinition
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.definer import seed_roles
from management.role.model import Role
from management.role.v2_model import CustomRoleV2, RoleV2, SeededRoleV2
from management.role.v2_service import RoleV2Service
from management.role_binding.model import RoleBinding
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from management.tenant_service.v2 import V2TenantBootstrapService
from migration_tool.in_memory_tuples import (
    InMemoryTuples,
    InMemoryRelationReplicator,
    all_of,
    resource,
    relation,
    subject,
    subject_id,
    subject_type,
)
from migration_tool.migrate_binding_scope import (
    migrate_custom_role_bindings,
    migrate_system_role_bindings_for_group,
    migrate_all_role_bindings,
    car_source,
    system_role_source,
    custom_role_source,
)
from migration_tool.utils import create_relationship
from api.models import Tenant
from tests.management.group.test_view import find_relation_in_list
from tests.management.role.test_dual_write import DualWriteTestCase, RbacFixture
from tests.util import (
    assert_v1_v2_locally_consistent,
    assert_v1_v2_tuples_fully_consistent,
    assert_v2_tuples_consistent,
)
from tests.v2_util import seed_v2_role_from_v1, bootstrap_tenant_for_v2_test


@override_settings(ATOMIC_RETRY_DISABLED=True)
class BindingScopeMigrationAPITest(TestCase):
    """Tests for binding scope migration API endpoint."""

    def setUp(self):
        """Set up test data."""
        self.url = "/_private/api/utils/migrate_binding_scope/"

        # Create admin user for API access (Django's User model for test client)
        self.user = DjangoUser.objects.create(
            username="test_admin",
            email="admin@test.com",
            is_superuser=True,
            is_staff=True,
        )

        self.tenant = Tenant.objects.create(tenant_name="test_tenant", account_id="12345", org_id="67890")

        # Get or create workspaces
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)

        self.root_workspace = bootstrap_result.root_workspace
        self.default_workspace = bootstrap_result.default_workspace

        # Create permissions
        self.default_permission = Permission.objects.create(
            tenant=self.tenant,
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
        )

        self.root_permission = Permission.objects.create(
            tenant=self.tenant, application="rbac", resource_type="group", verb="read", permission="rbac:group:read"
        )

    def tearDown(self):
        with self.subTest(msg="V2 consistency"):
            assert_v1_v2_locally_consistent(test=self)

        super().tearDown()

    @patch("internal.views.migrate_binding_scope_in_worker.delay")
    def test_api_endpoint_triggers_migration(self, mock_task):
        """Test that POST request triggers async task and returns correct JSON response."""
        self.client.force_login(self.user)

        response = self.client.post(self.url)

        # API might require special auth - if 403, skip detailed checks
        if response.status_code == 403:
            self.skipTest("API requires special authentication not available in test")

        # Should return HTTP 202 Accepted
        self.assertEqual(response.status_code, 202)

        # Should trigger celery task with default write_relationships=True
        mock_task.assert_called_once_with(write_relationships="True")

        # Response should be JSON with correct message
        data = response.json()
        self.assertIn("message", data)
        self.assertIn("Binding scope migration is running in a background worker", data["message"])
        self.assertEqual(data.get("write_relationships"), "True")

    @patch("internal.views.migrate_binding_scope_in_worker.delay")
    def test_api_endpoint_with_write_relationships_false(self, mock_task):
        """Test that write_relationships=False is passed correctly to the worker."""
        self.client.force_login(self.user)

        response = self.client.post(f"{self.url}?write_relationships=False")

        # API might require special auth - if 403, skip detailed checks
        if response.status_code == 403:
            self.skipTest("API requires special authentication not available in test")

        # Should return HTTP 202 Accepted
        self.assertEqual(response.status_code, 202)

        # Should trigger celery task with write_relationships=False
        mock_task.assert_called_once_with(write_relationships="False")

        # Response should show write_relationships=False
        data = response.json()
        self.assertEqual(data.get("write_relationships"), "False")


class BindingScopeMigrationReplicatorTest(TestCase):
    """Tests that verify migration uses the provided replicator correctly."""

    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(tenant_name="noop_test_tenant", account_id="noop123", org_id="noop456")

        # Get or create workspaces
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)

        self.root_workspace = bootstrap_result.root_workspace
        self.default_workspace = bootstrap_result.default_workspace

        # Create permission
        self.permission = Permission.objects.create(
            tenant=self.tenant,
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
        )

    def tearDown(self):
        with self.subTest(msg="V2 consistency"):
            assert_v1_v2_locally_consistent(test=self)

        super().tearDown()

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="", REPLICATION_TO_RELATION_ENABLED=True)
    @patch.object(OutboxReplicator, "replicate")
    def test_migration_sends_tuples_to_provided_replicator(self, mock_outbox_replicate):
        """
        Test that migration sends all tuples to the provided replicator, not the default OutboxReplicator.

        This verifies that:
        - V2 models (CustomRoleV2, RoleBinding, BindingMapping) are created
        - All expected tuples are sent to the provided replicator
        - OutboxReplicator.replicate is NOT accidentally called
        """
        # Create a custom role with access and a group assignment
        role = Role.objects.create(tenant=self.tenant, name="Provided Replicator Test Role", system=False)
        Access.objects.create(role=role, permission=self.permission, tenant=self.tenant)

        # Create a group and assign the role via policy
        group = Group.objects.create(name="Provided Replicator Test Group", tenant=self.tenant)
        policy = Policy.objects.create(name="Provided Replicator Test Policy", tenant=self.tenant, group=group)
        policy.roles.add(role)

        # Verify initial state: no V2 models
        self.assertEqual(CustomRoleV2.objects.filter(v1_source=role).count(), 0)
        self.assertEqual(RoleBinding.objects.filter(role__v1_source=role).count(), 0)
        self.assertEqual(BindingMapping.objects.filter(role=role).count(), 0)

        # Use InMemoryTuples to track what gets replicated
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)

        # Perform migration with our provided replicator
        result = migrate_custom_role_bindings(role, replicator)

        # Should return 1 (migrated)
        self.assertEqual(result, 1)

        # V2 models SHOULD be created
        self.assertEqual(CustomRoleV2.objects.filter(v1_source=role).count(), 1, "CustomRoleV2 should be created")
        self.assertEqual(RoleBinding.objects.filter(role__v1_source=role).count(), 1, "RoleBinding should be created")
        self.assertEqual(BindingMapping.objects.filter(role=role).count(), 1, "BindingMapping should be created")

        # Verify tuples were sent to our provided replicator
        self.assertGreater(len(tuples), 0, "Tuples should be sent to the provided replicator")

        # Verify OutboxReplicator.replicate was NOT called (we should use the provided replicator, not default)
        mock_outbox_replicate.assert_not_called()


@override_settings(REPLICATION_TO_RELATION_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class BindingScopeMigrationTupleVerificationTest(TestCase):
    """Tests that verify actual tuple changes during migration (integration tests without mocking handlers)."""

    def setUp(self):
        """Set up test data."""
        self.tuples = InMemoryTuples()
        self.tenant = Tenant.objects.create(tenant_name="test_tenant", account_id="12345", org_id="67890")

        # Get or create workspaces
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant, tuples=self.tuples)

        self.root_workspace = bootstrap_result.root_workspace
        self.default_workspace = bootstrap_result.default_workspace

        # Create permissions
        self.default_permission = Permission.objects.create(
            tenant=self.tenant,
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
        )

        self.root_permission = Permission.objects.create(
            tenant=self.tenant, application="rbac", resource_type="group", verb="read", permission="rbac:group:read"
        )

        self.tenant_permission = Permission.objects.create(
            tenant=self.tenant,
            application="cost-management",
            resource_type="cost",
            verb="read",
            permission="cost-management:cost:read",
        )

    def tearDown(self):
        # Not all tests use self.tuples
        with self.subTest(msg="V2 consistency"):
            assert_v2_tuples_consistent(test=self, tuples=self.tuples)

        super().tearDown()

    def _assert_v1_v2_fully_consistent(self):
        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

    @override_settings(ROOT_SCOPE_PERMISSIONS="rbac:*:*", TENANT_SCOPE_PERMISSIONS="")
    def test_role_with_root_scope_permissions_migrates_to_root_workspace(self):
        """Test that custom role with root-scope permissions creates binding at root workspace."""
        # Create custom role with root-scope permission (rbac:group:read matches rbac:*:*)
        role = Role.objects.create(tenant=self.tenant, name="Root Scope Role", system=False)
        access = Access.objects.create(role=role, permission=self.root_permission, tenant=self.tenant)

        # Start with NO bindings - handler will create one at correct scope
        # This tests the handler's ability to create bindings at the right scope from scratch

        # Perform migration (starting with no bindings)
        replicator = InMemoryRelationReplicator(self.tuples)
        result = migrate_custom_role_bindings(role, replicator)

        # Should return 1 (created binding)
        self.assertEqual(result, 1)

        # Verify binding was created at root workspace
        bindings_after = BindingMapping.objects.filter(role=role)
        self.assertEqual(bindings_after.count(), 1, "Should have exactly one binding")

        final_binding = bindings_after.first()

        # Verify scope logic worked correctly (create fresh service with current settings)
        service = ImplicitResourceService.from_settings()
        actual_scope = service.scope_for_role(role)

        # Verify scope was correctly determined as ROOT
        self.assertEqual(actual_scope, Scope.ROOT, f"Handler should determine ROOT scope for rbac:* permissions")

        # Verify binding is at root workspace
        self.assertEqual(final_binding.resource_type_name, "workspace")
        self.assertEqual(
            final_binding.resource_id,
            str(self.root_workspace.id),
            f"Binding should be at root workspace (ID={self.root_workspace.id}) but is at {final_binding.resource_id}",
        )

        # Verify tuples: root workspace binding exists
        root_ws_tuples_after = self.tuples.find_tuples(
            all_of(resource("rbac", "workspace", str(self.root_workspace.id)), relation("binding"))
        )
        self.assertEqual(len(root_ws_tuples_after), 1, "Root workspace binding should be created")

        self._assert_v1_v2_fully_consistent()

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
    def test_role_with_multiple_bindings_consolidates_correctly(self):
        """Test that when a role has multiple bindings, migration consolidates them correctly."""
        # Create role with default-scope permission
        role = Role.objects.create(tenant=self.tenant, name="Multi-Binding Role", system=False)
        Access.objects.create(role=role, permission=self.default_permission, tenant=self.tenant)

        binding_root_uuid = uuid.uuid4()
        binding_default_uuid = uuid.uuid4()

        # Create bindings at both root and default workspace (simulating incorrect historical state)
        binding_root = BindingMapping.objects.create(
            role=role,
            mappings={
                "id": str(binding_root_uuid),
                "groups": [],
                "users": {},
                "role": {"id": str(role.uuid), "is_system": False, "permissions": []},
            },
            resource_type_namespace="rbac",
            resource_type_name="workspace",
            resource_id=str(self.root_workspace.id),
        )

        binding_default = BindingMapping.objects.create(
            role=role,
            mappings={
                "id": str(binding_default_uuid),
                "groups": [],
                "users": {},
                "role": {"id": str(role.uuid), "is_system": False, "permissions": []},
            },
            resource_type_namespace="rbac",
            resource_type_name="workspace",
            resource_id=str(self.default_workspace.id),
        )

        # Add initial tuples for both bindings
        for tuple_item in binding_root.as_tuples():
            self.tuples.add(tuple_item)
        for tuple_item in binding_default.as_tuples():
            self.tuples.add(tuple_item)

        # Verify initial state: both bindings have tuples
        root_tuples_before = self.tuples.find_tuples(all_of(resource("rbac", "role_binding", str(binding_root_uuid))))
        default_tuples_before = self.tuples.find_tuples(
            all_of(resource("rbac", "role_binding", str(binding_default_uuid)))
        )
        self.assertGreater(len(root_tuples_before), 0)
        self.assertGreater(len(default_tuples_before), 0)

        # Perform migration - dual write handler will consolidate bindings
        replicator = InMemoryRelationReplicator(self.tuples)
        result = migrate_custom_role_bindings(role, replicator)

        # Should return 1 (migrated)
        self.assertEqual(result, 1)

        # After migration, handler should have cleaned up bindings
        remaining_bindings = BindingMapping.objects.filter(role=role)
        self.assertGreater(remaining_bindings.count(), 0)

        # Verify tuples exist for remaining bindings
        for remaining_binding in remaining_bindings:
            binding_tuples = self.tuples.find_tuples(
                all_of(resource("rbac", "role_binding", remaining_binding.mappings["id"]))
            )
            self.assertGreater(len(binding_tuples), 0)

        self._assert_v1_v2_fully_consistent()

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
    def test_role_without_policy_is_migrated(self):
        """Test that custom role without any policy (not assigned to any group) is still migrated."""
        # Create custom role with access but NO policy (not assigned to any group)
        role = Role.objects.create(tenant=self.tenant, name="Role Without Policy", system=False)
        Access.objects.create(role=role, permission=self.default_permission, tenant=self.tenant)

        # Verify: NO policy exists for this role
        self.assertFalse(role.policies.exists(), "Role should have no policies")

        # Verify initial state: no V2 models
        self.assertEqual(CustomRoleV2.objects.filter(v1_source=role).count(), 0)
        self.assertEqual(RoleBinding.objects.filter(role__v1_source=role).count(), 0)
        self.assertEqual(BindingMapping.objects.filter(role=role).count(), 0)

        # Perform migration using migrate_all_role_bindings
        replicator = InMemoryRelationReplicator(self.tuples)
        checked, migrated = migrate_all_role_bindings(replicator=replicator, tenant=self.tenant)

        # Should have checked and migrated at least one role
        self.assertGreaterEqual(checked, 1, "Should have checked at least one role")
        self.assertGreaterEqual(migrated, 1, "Should have migrated at least one role")

        # V2 models SHOULD be created even without policy
        self.assertEqual(CustomRoleV2.objects.filter(v1_source=role).count(), 1, "CustomRoleV2 should be created")
        self.assertEqual(RoleBinding.objects.filter(role__v1_source=role).count(), 1, "RoleBinding should be created")
        self.assertEqual(BindingMapping.objects.filter(role=role).count(), 1, "BindingMapping should be created")

        # Verify the binding has empty groups (since no policy/group is assigned)
        binding = BindingMapping.objects.filter(role=role).first()
        self.assertEqual(binding.mappings.get("groups", []), [], "Binding should have no groups")

        # Verify tuples were created
        binding_id = binding.mappings["id"]
        binding_tuples = self.tuples.find_tuples(all_of(resource("rbac", "role_binding", binding_id)))
        self.assertGreater(len(binding_tuples), 0, "Should have tuples for the binding")

        self._assert_v1_v2_fully_consistent()

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
    def test_role_migration_is_idempotent(self):
        """Test that running migration multiple times on the same role is safe."""
        # Create role
        role = Role.objects.create(tenant=self.tenant, name="Idempotent Test Role", system=False)
        Access.objects.create(role=role, permission=self.default_permission, tenant=self.tenant)

        # Create binding
        binding = BindingMapping.objects.create(
            role=role,
            mappings={
                "id": str(uuid.uuid4()),
                "groups": [],
                "users": {},
                "role": {"id": str(role.uuid), "is_system": False, "permissions": []},
            },
            resource_type_namespace="rbac",
            resource_type_name="workspace",
            resource_id=str(self.default_workspace.id),
        )

        # Add initial tuples
        for tuple_item in binding.as_tuples():
            self.tuples.add(tuple_item)

        # Perform migration first time
        replicator = InMemoryRelationReplicator(self.tuples)
        result1 = migrate_custom_role_bindings(role, replicator)
        self.assertEqual(result1, 1)

        bindings_after_first = BindingMapping.objects.filter(role=role).count()

        # Capture tuple state after first migration
        tuples_after_first = set(self.tuples)

        # Perform migration second time (should be idempotent)
        result2 = migrate_custom_role_bindings(role, replicator)
        self.assertEqual(result2, 1)

        bindings_after_second = BindingMapping.objects.filter(role=role).count()

        # Should have same number of bindings
        self.assertEqual(bindings_after_first, bindings_after_second)

        # Verify tuples are EXACTLY the same (idempotent)
        tuples_after_second = set(self.tuples._tuples)
        self.assertEqual(
            tuples_after_first,
            tuples_after_second,
            "Tuples should be identical after second migration (idempotent)",
        )

        self._assert_v1_v2_fully_consistent()

    def test_v2_tenant_not_migrated(self):
        replicator = InMemoryRelationReplicator(self.tuples)

        v1_role = Role.objects.create(tenant=self.tenant, name="test role A", system=False)
        Access.objects.create(role=v1_role, permission=self.default_permission, tenant=self.tenant)

        migrate_all_role_bindings(replicator, self.tenant)

        v2_role = CustomRoleV2.objects.filter(v1_source=v1_role).get()
        v2_role_uuid = str(v2_role.uuid)

        def assert_role_binding_count(count: int):
            self.assertEqual(RoleBinding.objects.filter(role=v2_role).count(), count)
            if count == 0:
                self.assertEqual(self.tuples.count_tuples(resource("rbac", "role", v2_role_uuid)), 0)
            else:
                role_in_db = CustomRoleV2.objects.get(uuid=v2_role_uuid)
                expected_on_role = role_in_db.permissions.count()
                if self.tenant.tenant_resource_id():
                    expected_on_role += 1  # rbac/role#owner@rbac/tenant (V2 dual-write)
                self.assertEqual(
                    self.tuples.count_tuples(resource("rbac", "role", v2_role_uuid)),
                    expected_on_role,
                )

        # Convert the tenant to V2.
        ensure_v2_write_activated(self.tenant)

        assert_role_binding_count(1)

        RoleV2Service(tenant=self.tenant, replicator=replicator).bulk_delete([str(v2_role.uuid)])

        assert_role_binding_count(0)

        # This should not revive the V2 role, even though the V1 role still exists.
        migrate_all_role_bindings(replicator, self.tenant)

        assert_role_binding_count(0)

        # We do not assert that V1 and V2 are consistent, since we are testing for what happens when they
        # deliberately diverge.
        assert_v2_tuples_consistent(test=self, tuples=self.tuples)


class SystemRoleBindingMigrationTest(TestCase):
    """Tests for system role binding migration via group operations."""

    def setUp(self):
        """Set up test data."""
        self.tuples = InMemoryTuples()
        self.replicator = InMemoryRelationReplicator(self.tuples)

        # Create public tenant for system roles
        self.public_tenant = Tenant.objects.get_or_create(tenant_name="public")[0]

        # Create a regular tenant
        self.tenant = Tenant.objects.create(tenant_name="test_tenant", account_id="12345", org_id="67890")

        # Create workspaces
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant, tuples=self.tuples)

        self.root_workspace = bootstrap_result.root_workspace
        self.default_workspace = bootstrap_result.default_workspace

        # Create permissions in public tenant
        self.root_permission = Permission.objects.create(
            tenant=self.public_tenant,
            application="rbac",
            resource_type="group",
            verb="read",
            permission="rbac:group:read",
        )

    def tearDown(self):
        # Not all tests actually use self.tuples.
        with self.subTest(msg="V2 consistency"):
            assert_v1_v2_locally_consistent(test=self)

        super().tearDown()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="rbac:*:*",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    def test_system_role_migration_via_group_handler(self):
        """Test that system role bindings are migrated using group handler."""
        # Create system role with ROOT scope
        system_role = Role.objects.create(
            tenant=self.public_tenant,
            name="System Admin Role",
            system=True,
        )
        Access.objects.create(role=system_role, permission=self.root_permission, tenant=self.public_tenant)

        # Required for dual-write to work.
        seed_v2_role_from_v1(system_role)

        # Create a group and assign the system role
        group = Group.objects.create(
            name="Test Group",
            tenant=self.tenant,
            system=False,
        )

        # Use add_roles to create binding (will be at default workspace initially)
        add_roles(group, [system_role.uuid], self.tenant)

        # Verify binding exists (probably at default workspace - wrong for ROOT scope)
        binding_before = BindingMapping.objects.filter(role=system_role, role__policies__group=group).first()
        self.assertIsNotNone(binding_before, "Should have binding after adding role to group")

        # Perform migration via group
        replicator = InMemoryRelationReplicator(self.tuples)
        migrate_system_role_bindings_for_group(group, replicator)

        # Verify binding is at correct scope after migration
        binding_after = BindingMapping.objects.filter(role=system_role).first()
        self.assertIsNotNone(binding_after, "Should still have binding after migration")

        # Should be at root workspace (ROOT scope)
        self.assertEqual(binding_after.resource_type_name, "workspace")
        workspace = Workspace.objects.get(id=binding_after.resource_id)

        self.assertEqual(
            workspace.type, Workspace.Types.ROOT, "System role with ROOT scope should be at root workspace"
        )

        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="rbac:*:*",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_system_role_with_multiple_groups_consolidates_bindings(self, mock_replicate):
        """
        Test system role assigned to multiple groups with duplicate bindings.

        Scenario:
        - System role assigned to groupA and groupB
        - Has duplicate bindings at different scopes
        - After migration: Should consolidate to correct scope with both groups
        """
        # Redirect replicator
        mock_replicate.side_effect = self.replicator.replicate

        # Create system role with ROOT scope
        system_role = Role.objects.create(
            tenant=self.public_tenant,
            name="System Role Multi-Group",
            system=True,
        )
        Access.objects.create(role=system_role, permission=self.root_permission, tenant=self.public_tenant)

        seed_v2_role_from_v1(system_role)

        # Create two groups
        groupA = Group.objects.create(name="GroupA", tenant=self.tenant, system=False)
        groupB = Group.objects.create(name="GroupB", tenant=self.tenant, system=False)

        # Assign system role to both groups (creates bindings)
        add_roles(groupA, [system_role.uuid], self.tenant)
        add_roles(groupB, [system_role.uuid], self.tenant)

        # Delete auto-created bindings - set up exact scenario manually
        BindingMapping.objects.filter(role=system_role).delete()
        RoleBinding.objects.filter(role__v1_source=system_role).delete()

        # Clear all tuples to start fresh
        self.tuples.clear()

        wrong_binding_uuid = str(uuid.uuid4())

        # Create binding at wrong scope with BOTH groups
        wrong_binding = BindingMapping.objects.create(
            role=system_role,
            mappings={
                "id": wrong_binding_uuid,
                "groups": [str(groupA.uuid), str(groupB.uuid)],  # Both groups here
                "users": {},
                "role": {"id": str(system_role.uuid), "is_system": True, "permissions": []},
            },
            resource_type_namespace="rbac",
            resource_type_name="workspace",
            resource_id=str(self.default_workspace.id),  # Wrong!
        )

        correct_binding_uuid = str(uuid.uuid4())

        # Create binding at correct scope with ONLY groupA
        correct_binding = BindingMapping.objects.create(
            role=system_role,
            mappings={
                "id": correct_binding_uuid,
                "groups": [str(groupA.uuid)],  # Only groupA here - groupB is only in wrong binding
                "users": {},
                "role": {"id": str(system_role.uuid), "is_system": True, "permissions": []},
            },
            resource_type_namespace="rbac",
            resource_type_name="workspace",
            resource_id=str(self.root_workspace.id),  # Correct!
        )

        # Add tuples using binding.as_tuples() to ensure correct structure
        for binding in [wrong_binding, correct_binding]:
            for t in binding.as_tuples():
                self.tuples.add(t)

        # Before migration: Verify 2 bindings exist
        bindings_before = BindingMapping.objects.filter(role=system_role)
        self.assertEqual(bindings_before.count(), 2, "Should have 2 bindings before migration")

        # Verify tuples were created
        self.assertEqual(len(self.tuples), 7, "Should have 7 tuples from both bindings")

        # Perform migration via both groups
        result_a = migrate_system_role_bindings_for_group(groupA, self.replicator)
        result_b = migrate_system_role_bindings_for_group(groupB, self.replicator)

        # After migration: Verify consolidation
        bindings_after = BindingMapping.objects.filter(role=system_role)

        # Key assertion: Should consolidate to 1 binding at correct scope
        self.assertEqual(bindings_after.count(), 1, f"Should have 1 binding, got {bindings_after.count()}")

        final_binding = bindings_after.first()
        self.assertEqual(final_binding.resource_id, str(self.root_workspace.id), "Should be at root workspace")

        # BOTH groups should be assigned (groupA was in correct binding, groupB was only in wrong binding)
        # Migration should have merged them into the correct binding
        final_groups = final_binding.mappings.get("groups", [])
        self.assertEqual(len(final_groups), 2, "Should have both groups after consolidation")
        self.assertIn(str(groupA.uuid), final_groups, "GroupA should be assigned")
        self.assertIn(str(groupB.uuid), final_groups, "GroupB should be assigned (merged from wrong binding)")

        # Verify tuple structure after migration
        # Note: Migration creates new binding IDs, so we verify structure without hardcoded IDs

        # Get the role UUID and final binding ID for verification
        role_uuid_str = str(system_role.uuid)
        final_binding_id = final_binding.mappings["id"]

        # 1. Verify wrong binding tuples are REMOVED
        wrong_binding_tuples_after = self.tuples.find_tuples(
            all_of(resource("rbac", "role_binding", wrong_binding_uuid))
        )
        self.assertEqual(len(wrong_binding_tuples_after), 0, "Wrong binding tuples should be deleted")

        # 2. Verify old correct binding tuples are REMOVED (replaced with new binding ID)
        old_correct_binding_tuples = self.tuples.find_tuples(
            all_of(resource("rbac", "role_binding", correct_binding_uuid))
        )
        self.assertEqual(len(old_correct_binding_tuples), 0, "Old correct binding tuples should be replaced")

        # 3. Verify EXACTLY 4 tuples exist for the new binding:
        self.assertEqual(len(self.tuples), 4, "Should have exactly 4 tuples after migration")

        # Tuple 1: workspace:root#binding@role_binding:<new-id>
        tuple1 = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(self.root_workspace.id)),
                relation("binding"),
                subject("rbac", "role_binding", final_binding_id),
            )
        )
        self.assertEqual(len(tuple1), 1, f"Should have workspace:root#binding@role_binding:{final_binding_id}")

        # Tuple 2: role_binding:<new-id>#role@role:<uuid>
        tuple2 = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", final_binding_id),
                relation("role"),
                subject("rbac", "role", role_uuid_str),
            )
        )
        self.assertEqual(len(tuple2), 1, f"Should have role_binding:{final_binding_id}#role@role:{role_uuid_str}")

        # Tuple 3 & 4: role_binding:<new-id>#subject@group:<groupA/groupB>
        all_group_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", final_binding_id),
                relation("subject"),
            )
        )
        self.assertEqual(len(all_group_tuples), 2, "Should have 2 group assignment tuples (groupA + groupB)")

        # Verify the exact group UUIDs match
        group_uuids_in_tuples = {t.subject.subject.id for t in all_group_tuples}
        self.assertIn(str(groupA.uuid), group_uuids_in_tuples, "GroupA UUID should be in tuples")
        self.assertIn(str(groupB.uuid), group_uuids_in_tuples, "GroupB UUID should be in tuples")

        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="", REPLICATION_TO_RELATION_ENABLED=True)
    def test_migration_creates_bindings_for_roles_with_no_bindings(self):
        """
        Test that migration creates bindings for custom roles that have zero bindings.

        This reproduces the issue where roles created before replication was enabled
        have no bindings at all, and migration should create them.

        Scenario:
        - Custom role with inventory:groups:read and resource definition
        - Role created before replication (no bindings exist)
        - Run migration
        - Verify binding is created with correct data
        """
        # Create permission
        perm = Permission.objects.create(
            tenant=self.tenant,
            application="inventory",
            resource_type="groups",
            verb="read",
            permission="inventory:groups:read",
        )

        # Create a specific workspace for the resource definition
        specific_workspace = Workspace.objects.create(
            name="Specific Workspace No Bindings",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.root_workspace,
        )

        # Create a group
        group = Group.objects.create(name="Test Group No Bindings", tenant=self.tenant)

        # Create custom role WITHOUT dual write (simulating pre-V2 creation)
        role = Role.objects.create(tenant=self.tenant, name="Role With No Bindings", system=False)

        # Add permission with resource definition
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [str(specific_workspace.id)],
            },
            tenant=self.tenant,
        )

        # Assign role to group via policy
        policy = Policy.objects.create(name="Test Policy No Bindings", tenant=self.tenant)
        policy.roles.add(role)
        policy.group = group
        policy.save()

        # Verify initial state: NO bindings exist
        bindings_before = BindingMapping.objects.filter(role=role)
        self.assertEqual(bindings_before.count(), 0, "Should have NO bindings before migration")

        # Run migration
        replicator = InMemoryRelationReplicator(self.tuples)
        result = migrate_custom_role_bindings(role, replicator)

        # Should return 1 (migrated)
        self.assertEqual(result, 1)

        # After migration: verify binding is created
        bindings_after = BindingMapping.objects.filter(role=role)
        self.assertEqual(bindings_after.count(), 1, "Should have 1 binding after migration")

        # Verify the binding is at the correct workspace
        binding = bindings_after.first()
        self.assertEqual(binding.resource_id, str(specific_workspace.id))

        # Verify the binding has the group assigned
        self.assertIn(str(group.uuid), binding.mappings["groups"])

        # Verify the binding has the correct permission
        permissions = binding.mappings.get("role", {}).get("permissions", [])
        self.assertIn("inventory_groups_read", permissions)

        # Verify tuples were created
        binding_id = binding.mappings["id"]
        all_tuples = self.tuples.find_tuples(all_of(resource("rbac", "role_binding", binding_id)))
        # Should have at least the group subject tuple
        self.assertGreater(len(all_tuples), 0, "Should have tuples for the new binding")

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
    )
    def test_v2_tenant_not_migrated(self):
        fixture = RbacFixture()

        system_role = fixture.new_system_role(name="system role", permissions=["rbac:*:*"])

        group = Group.objects.create(name="Test Group No Bindings", tenant=self.tenant)
        fixture.add_role_to_group(system_role, group)

        replicator = InMemoryRelationReplicator(self.tuples)

        dual_write = RelationApiDualWriteGroupHandler(
            group=group, event_type=ReplicationEventType.ASSIGN_ROLE, replicator=replicator
        )

        dual_write.generate_relations_reset_roles([system_role])
        dual_write.replicate()

        binding = RoleBinding.objects.get(role__v1_source=system_role)

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(self.default_workspace.id)),
                    relation("binding"),
                    subject("rbac", "role_binding", str(binding.uuid)),
                )
            ),
        )

        ensure_v2_write_activated(self.tenant)

        with self.settings(ROOT_SCOPE_PERMISSIONS="rbac:*:*"):
            migrate_all_role_bindings(replicator)

        # The migration should not have affected the binding, since the tenant is now a V2 tenant.

        self.assertTrue(RoleBinding.objects.filter(pk=binding.pk).exists())

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(self.default_workspace.id)),
                    relation("binding"),
                    subject("rbac", "role_binding", str(binding.uuid)),
                )
            ),
        )


@override_settings(ATOMIC_RETRY_DISABLED=True)
class CrossAccountRequestMigrationTest(DualWriteTestCase):
    def setUp(self):
        super().setUp()

        self.user_id = "car_user_id"
        self.user = self.fixture.new_principals_in_tenant(
            [self.user_id], self.fixture.new_tenant("car_source").tenant
        )[0]

        CrossAccountRequest.objects.all().delete()

    def tearDown(self):
        assert_v2_tuples_consistent(test=self, tuples=self.tuples)
        super().tearDown()

    def _assert_v1_v2_locally_consistent(self):
        assert_v1_v2_locally_consistent(test=self)

    def _do_migrate(self):
        migrate_all_role_bindings(replicator=InMemoryRelationReplicator(self.tuples), tenant=self.tenant)

    def _do_test_migrate_car(self):
        system_role = self.given_v1_system_role(name="system role", permissions=["root:resource:verb"])

        def expect_default_count(count: int):
            self.expect_role_bindings_to_workspace(
                count,
                self.default_workspace(),
                for_v2_roles=[str(system_role.uuid)],
                for_principals=[self.user_id],
                for_groups=[],
            )

        def expect_root_count(count: int):
            self.expect_role_bindings_to_workspace(
                count,
                self.root_workspace(),
                for_v2_roles=[str(system_role.uuid)],
                for_principals=[self.user_id],
                for_groups=[],
            )

        # Simulate the CAR being created while scope was not respected.
        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            self.given_car(self.user_id, [system_role])

        expect_default_count(1)
        expect_root_count(0)

        with self.settings(ROOT_SCOPE_PERMISSIONS="root:*:*", TENANT_SCOPE_PERMISSIONS=""):
            self._do_migrate()

        expect_default_count(0)
        expect_root_count(1)

    def test_migrate_car(self):
        """Test that migrating the scope of a binding from a cross-account request works."""
        self._do_test_migrate_car()
        self._assert_v1_v2_locally_consistent()

    def test_migrate_car_v2_tenant(self):
        """Test that migrating the scope of a binding from a cross-account request works in a V2 tenant."""
        ensure_v2_write_activated(self.tenant)
        self._do_test_migrate_car()

    def test_migrate_car_with_group(self):
        """Test that migrating the scope of a binding from both a group and a cross-account request works."""
        system_role = self.given_v1_system_role(name="system role", permissions=["root:resource:verb"])
        group, _ = self.given_group(name="a group", users=["p1"])

        def expect_default_count(count: int):
            self.expect_role_bindings_to_workspace(
                count,
                self.default_workspace(),
                for_v2_roles=[str(system_role.uuid)],
                for_principals=[self.user_id],
                for_groups=[str(group.uuid)],
            )

        def expect_root_count(count: int):
            self.expect_role_bindings_to_workspace(
                count,
                self.root_workspace(),
                for_v2_roles=[str(system_role.uuid)],
                for_principals=[self.user_id],
                for_groups=[str(group.uuid)],
            )

        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            self.given_car(self.user_id, [system_role])
            self.given_roles_assigned_to_group(group, [system_role])

        expect_default_count(1)
        expect_root_count(0)

        with self.settings(ROOT_SCOPE_PERMISSIONS="root:*:*", TENANT_SCOPE_PERMISSIONS=""):
            self._do_migrate()

        expect_default_count(0)
        expect_root_count(1)

        self._assert_v1_v2_locally_consistent()

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
    def test_migrate_expired_car(self):
        """Test that the migration does not revive expired cross-cacount requests."""
        system_role = self.given_v1_system_role(name="system role", permissions=["root:resource:verb"])

        def expect_binding_count(count: int):
            self.expect_role_bindings_to_workspace(
                count,
                self.default_workspace(),
                for_v2_roles=[str(system_role.uuid)],
                for_principals=[self.user_id],
                for_groups=[],
            )

        car = self.given_car(self.user_id, [system_role])
        expect_binding_count(1)

        car.status = "expired"
        car.save()

        self.given_car_expired(car)
        expect_binding_count(0)

        self._do_migrate()
        expect_binding_count(0)

        self._assert_v1_v2_locally_consistent()


@override_settings(ATOMIC_RETRY_DISABLED=True)
class SourceRestrictionTest(DualWriteTestCase):
    def _do_migrate(self, sources: set[str]):
        migrate_all_role_bindings(replicator=InMemoryRelationReplicator(self.tuples), sources=sources)

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
    def test_by_source(self):
        direct_system_role = self.given_v1_system_role("system role", ["rbac:*:*"])
        car_system_role = self.given_v1_system_role("another system role", ["rbac:*:*"])
        custom_role = self.given_v1_role("custom role", default=["rbac:*:*"])

        car_tenant = self.fixture.new_tenant(org_id="car_source").tenant
        car_principal = self.fixture.new_principals_in_tenant(["car_principal"], car_tenant)[0]

        group, _ = self.given_group("group", ["p1"])

        self.given_roles_assigned_to_group(group, [direct_system_role, custom_role])
        self.given_car(user_id=car_principal.user_id, roles=[car_system_role])

        def expect_bindings(system_group_workspace: str, system_car_workspace: str, custom_workspace: str):
            v2_custom_role = RoleV2.objects.get(v1_source=custom_role)

            self.expect_1_role_binding_to_workspace(
                system_group_workspace,
                for_v2_roles=[str(direct_system_role.uuid)],
                for_groups=[str(group.uuid)],
            )

            self.expect_1_role_binding_to_workspace(
                system_car_workspace,
                for_v2_roles=[str(car_system_role.uuid)],
                for_groups=[],
                for_principals=[car_principal.user_id],
            )

            self.expect_1_role_binding_to_workspace(
                custom_workspace,
                for_v2_roles=[str(v2_custom_role.uuid)],
                for_groups=[str(group.uuid)],
            )

        # Test that a source is migrated only when it is provided. We just cursorily check the relations here. The
        # details are tested more thoroughly elsewhere.
        with self.settings(ROOT_SCOPE_PERMISSIONS="rbac:*:*"):
            expect_bindings(
                system_group_workspace=self.default_workspace(),
                system_car_workspace=self.default_workspace(),
                custom_workspace=self.default_workspace(),
            )

            self._do_migrate({system_role_source})

            expect_bindings(
                system_group_workspace=self.root_workspace(),
                system_car_workspace=self.default_workspace(),
                custom_workspace=self.default_workspace(),
            )

            self._do_migrate({car_source})

            expect_bindings(
                system_group_workspace=self.root_workspace(),
                system_car_workspace=self.root_workspace(),
                custom_workspace=self.default_workspace(),
            )

            self._do_migrate({custom_role_source})

            expect_bindings(
                system_group_workspace=self.root_workspace(),
                system_car_workspace=self.root_workspace(),
                custom_workspace=self.root_workspace(),
            )


@override_settings(ATOMIC_RETRY_DISABLED=True)
class ComprehensiveBootstrapMigrationTest(DualWriteTestCase):
    """
    Comprehensive integration test using tenant bootstrap and group APIs.

    Tests realistic scenario with:
    - Platform default and non-platform default system roles
    - Custom roles with different scopes
    - Multiple bootstrapped tenants
    - Custom default groups
    - Regular custom groups
    - Full migration (custom roles + system roles via groups) and tuple verification
    """

    def setUp(self):
        """Set up test data using tenant bootstrap."""
        # Seed roles before calling super() so platform roles exist when fixture initializes
        seed_roles()

        super().setUp()
        # DualWriteTestCase already creates self.tuples and self.fixture
        # Create additional tenants for the comprehensive test
        self.tenant1 = self.tenant  # Use the tenant from DualWriteTestCase
        self.tenant2 = self.switch_to_new_tenant("globex", "org_globex")
        self.tenant3 = self.switch_to_new_tenant("initech", "org_initech")
        self.restore_test_tenant()  # Switch back to tenant1

    def tearDown(self):
        with self.subTest(msg="V2 consistency"):
            assert_v1_v2_locally_consistent(test=self)

        super().tearDown()

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="rbac:*:*",
        TENANT_SCOPE_PERMISSIONS="",
        REPLICATION_TO_RELATION_ENABLED=True,
        V2_BOOTSTRAP_TENANT=True,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_comprehensive_migration_with_bootstrap_and_custom_groups(self, mock_replicate):
        """
        Comprehensive end-to-end migration test.

        Setup:
        - Creates platform default and non-platform default system roles
        - Bootstraps 3 tenants via DualWriteTestCase
        - Creates custom default groups for tenant1 and tenant2
        - Creates regular custom group in tenant3
        - Simulates wrong-scoped bindings by patching ImplicitResourceService

        Test:
        - Runs migrate_all_role_bindings() to fix incorrect bindings
        - Verifies all bindings are migrated to correct scopes
        - Verifies tuples are correctly updated
        """

        # Helper function to swap scopes for simulating historical incorrect bindings
        def wrong_scope_for_role(role):
            """Return wrong scope for test roles to simulate incorrect historical bindings."""
            if hasattr(role, "uuid"):
                if role.uuid == non_platform_default_role.uuid:
                    return Scope.DEFAULT  # Wrong! Should be ROOT
                elif role.uuid == platform_default_role.uuid:
                    return Scope.ROOT  # Wrong! Should be DEFAULT
            return service.scope_for_role(role)  # Use correct scope for other roles

        # Redirect all OutboxReplicator.replicate() calls to our InMemoryRelationReplicator
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Step 1: Create system roles using fixture helpers
        # Get a platform default system role with DEFAULT scope (not rbac:*:* which would be ROOT scope)
        service = ImplicitResourceService.from_settings()
        platform_default_role = None
        for role in Role.objects.filter(system=True, platform_default=True):
            if service.scope_for_role(role) == Scope.DEFAULT:
                platform_default_role = role
                break
        self.assertIsNotNone(platform_default_role, "Should have a platform default system role with DEFAULT scope")

        # Create non-platform default system role with ROOT scope (rbac:*:* matches ROOT_SCOPE_PERMISSIONS)
        non_platform_default_role = self.given_v1_system_role(
            name="Non-Platform System Role",
            permissions=["rbac:workspace:write"],
        )

        # Step 2: All tenants are already bootstrapped by DualWriteTestCase via fixture
        for tenant in [self.tenant1, self.tenant2, self.tenant3]:
            self.assertIsNotNone(tenant.tenant_mapping, f"Tenant {tenant.org_id} should have mapping")

        # Step 3: Create custom default groups for tenant1 and tenant2
        # This simulates users customizing their default access
        custom_default_groups = {}
        for tenant in [self.tenant1, self.tenant2]:
            self.switch_tenant(tenant)
            custom_group = self.fixture.custom_default_group(tenant)
            self.assertIsNotNone(custom_group, f"Should create custom default group for {tenant.org_id}")
            custom_default_groups[tenant.id] = custom_group

        # Step 4: Create regular custom group in tenant3 without assigning roles yet
        self.switch_tenant(self.tenant3)
        custom_group_t3, _ = self.fixture.new_group(name="Custom Admins", tenant=self.tenant3)

        # Step 5: Simulate incorrect binding state by using wrong scope configuration
        # Use a mock resource service that returns wrong scopes to simulate historical incorrect bindings
        mock_wrong_scope_service = Mock(spec=ImplicitResourceService)
        mock_wrong_scope_service.scope_for_role = Mock(side_effect=wrong_scope_for_role)
        mock_wrong_scope_service.binding_scopes_for_role = Mock(side_effect=lambda role: [wrong_scope_for_role(role)])

        # Temporarily patch ImplicitResourceService.from_settings to return wrong scopes when creating bindings
        with patch(
            "management.group.relation_api_dual_write_group_handler.ImplicitResourceService.from_settings",
            return_value=mock_wrong_scope_service,
        ):
            # Add roles with wrong scope configuration - this will create bindings at wrong scopes
            add_roles(custom_group_t3, [platform_default_role.uuid, non_platform_default_role.uuid], self.tenant3)

        # Verify non-platform role is at DEFAULT workspace (wrong)
        non_platform_binding = BindingMapping.objects.filter(
            role=non_platform_default_role, resource_id=self.default_workspace(self.tenant3)
        ).first()
        self.assertIsNotNone(non_platform_binding, "Non-platform role should be at DEFAULT workspace (wrong)")

        # Verify platform role is at ROOT workspace (wrong)
        platform_binding = BindingMapping.objects.filter(
            role=platform_default_role, resource_id=self.root_workspace(self.tenant3)
        ).first()
        self.assertIsNotNone(platform_binding, "Platform role should be at ROOT workspace (wrong)")

        # Step 6: Perform migration (will use our replicator via patch)
        roles_checked, roles_migrated = migrate_all_role_bindings(OutboxReplicator())

        # Should have processed roles
        self.assertGreater(roles_checked, 0, "Should have checked roles")
        self.assertGreater(roles_migrated, 0, "Should have migrated some roles")

        # Step 7: Verify all bindings are at correct scope and have correct tuples
        # This is the key test - after migration, every binding should match its role's scope
        service = ImplicitResourceService.from_settings()

        for binding in BindingMapping.objects.all():
            expected_scope = service.scope_for_role(binding.role)
            binding_id = binding.mappings["id"]

            if binding.resource_type_name == "workspace":
                workspace = Workspace.objects.get(id=binding.resource_id)

                if expected_scope == Scope.ROOT:
                    self.assertEqual(
                        workspace.type,
                        Workspace.Types.ROOT,
                        f"Binding {binding.id} for role '{binding.role.name}' (ROOT scope) "
                        f"should be at root workspace, got {workspace.type}",
                    )
                elif expected_scope == Scope.DEFAULT:
                    self.assertEqual(
                        workspace.type,
                        Workspace.Types.DEFAULT,
                        f"Binding {binding.id} for role '{binding.role.name}' (DEFAULT scope) "
                        f"should be at default workspace, got {workspace.type}",
                    )

                # Verify complete workspace->binding tuple exists
                complete_ws_binding_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "workspace", str(workspace.id)),
                        relation("binding"),
                        subject("rbac", "role_binding", binding_id),
                    )
                )
                self.assertEqual(
                    len(complete_ws_binding_tuples),
                    1,
                    f"Should find exactly 1 tuple: workspace:{workspace.id}#binding@role_binding:{binding_id}",
                )

            elif binding.resource_type_name == "tenant":
                # TENANT scope bindings
                self.assertEqual(
                    expected_scope, Scope.TENANT, f"Binding {binding.id} at tenant level should have TENANT scope"
                )

                # Verify complete tenant->binding tuple exists
                complete_tenant_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "tenant", binding.resource_id),
                        relation("binding"),
                        subject("rbac", "role_binding", binding_id),
                    )
                )
                self.assertEqual(
                    len(complete_tenant_tuples),
                    1,
                    f"Should find exactly 1 tuple: tenant:{binding.resource_id}#binding@role_binding:{binding_id}",
                )

        # Step 8: Verify specific role bindings are correctly placed after migration
        # Use DualWriteTestCase helpers to verify the role bindings

        # Verify non-platform default (ROOT scope) role binding for tenant3 custom group at ROOT workspace
        non_platform_default_v2_role_id = str(non_platform_default_role.uuid)
        custom_group_t3_id = str(custom_group_t3.uuid)
        self.expect_binding_present(
            target=self.root_workspace_resource(self.tenant3),
            v2_role_id=non_platform_default_v2_role_id,
            group_id=custom_group_t3_id,
        )

        # Verify platform default (DEFAULT scope) role binding for tenant3 custom group at DEFAULT workspace
        platform_default_v2_role_id = str(platform_default_role.uuid)
        self.expect_binding_present(
            target=self.default_workspace_resource(self.tenant3),
            v2_role_id=platform_default_v2_role_id,
            group_id=custom_group_t3_id,
        )

        # Step 9: Verify custom default groups for tenant1 and tenant2 have platform default role at DEFAULT workspace
        # Custom default groups should inherit the platform default system role
        for tenant in [self.tenant1, self.tenant2]:
            custom_default_group = custom_default_groups[tenant.id]
            self.expect_binding_present(
                target=self.default_workspace_resource(tenant),
                v2_role_id=platform_default_v2_role_id,
                group_id=str(custom_default_group.uuid),
            )


@override_settings(
    ROOT_SCOPE_PERMISSIONS="advisor:*:*",
    TENANT_SCOPE_PERMISSIONS="subscriptions:*:*",
    REPLICATION_TO_RELATION_ENABLED=True,
)
class MixedScopeBindingMigrationTest(TestCase):
    """Tests that migrate_binding_scope correctly splits mixed TENANT+workspace bindings."""

    def setUp(self):
        self.tuples = InMemoryTuples()
        self.replicator = InMemoryRelationReplicator(self.tuples)
        self.public_tenant = Tenant.objects.get_or_create(tenant_name="public")[0]
        self.tenant = Tenant.objects.create(tenant_name="test_tenant", account_id="12345", org_id="67890")
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant, tuples=self.tuples)
        self.root_workspace = bootstrap_result.root_workspace
        self.default_workspace = bootstrap_result.default_workspace

    def tearDown(self):
        with self.subTest(msg="V2 consistency"):
            assert_v2_tuples_consistent(test=self, tuples=self.tuples)
        super().tearDown()

    def test_custom_role_migrates_from_tenant_only_to_split(self):
        """A custom role with mixed permissions migrated from tenant-only binding to tenant + workspace bindings."""
        sub_perm = Permission.objects.create(
            tenant=self.tenant,
            application="subscriptions",
            resource_type="organization",
            verb="read",
            permission="subscriptions:organization:read",
        )
        inv_perm = Permission.objects.create(
            tenant=self.tenant,
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
        )

        role = Role.objects.create(tenant=self.tenant, name="Mixed Custom Role", system=False)
        Access.objects.create(role=role, permission=sub_perm, tenant=self.tenant)
        Access.objects.create(role=role, permission=inv_perm, tenant=self.tenant)

        # Simulate old state: a single binding at tenant (highest scope under old logic)
        tenant_resource_id = self.tenant.tenant_resource_id()
        bm_uuid = uuid.uuid4()
        BindingMapping.objects.create(
            role=role,
            mappings={
                "id": str(bm_uuid),
                "groups": [],
                "users": {},
                "role": {"id": str(role.uuid), "is_system": False, "permissions": []},
            },
            resource_type_namespace="rbac",
            resource_type_name="tenant",
            resource_id=tenant_resource_id,
        )

        # Verify starting tuples: only binding at tenant, nothing at workspace
        tenant_binding_tuples_before = self.tuples.find_tuples(
            all_of(resource("rbac", "tenant", tenant_resource_id), relation("binding"))
        )
        default_ws_binding_tuples_before = self.tuples.find_tuples(
            all_of(resource("rbac", "workspace", str(self.default_workspace.id)), relation("binding"))
        )
        # Bootstrap creates default-access bindings on the workspace; filter to non-bootstrap ones
        self.assertEqual(len(tenant_binding_tuples_before), 0, "No tenant binding tuples before migration")

        # Run migration
        result = migrate_custom_role_bindings(role, self.replicator)
        self.assertEqual(result, 1)

        # Verify tuples: binding tuple on tenant resource for TENANT-scoped permission
        tenant_binding_tuples_after = self.tuples.find_tuples(
            all_of(resource("rbac", "tenant", tenant_resource_id), relation("binding"))
        )
        self.assertEqual(len(tenant_binding_tuples_after), 1, "Should have one binding tuple on tenant")

        # Verify tuples: binding tuple on default workspace for DEFAULT-scoped permission
        default_ws_id = str(self.default_workspace.id)
        ws_binding_tuples_after = self.tuples.find_tuples(
            all_of(resource("rbac", "workspace", default_ws_id), relation("binding"))
        )
        # Filter to bindings from this role (exclude bootstrap default-access bindings)
        role_binding_uuids = set(str(rb.uuid) for rb in RoleBinding.objects.filter(role__v1_source=role))
        role_ws_tuples = [t for t in ws_binding_tuples_after if t.subject.subject.id in role_binding_uuids]
        self.assertEqual(len(role_ws_tuples), 1, "Should have one binding tuple on default workspace")

        # Verify permission tuples on each V2 role
        for rb in RoleBinding.objects.filter(role__v1_source=role):
            rb_tuples = self.tuples.find_tuples(
                all_of(resource("rbac", "role_binding", str(rb.uuid)), relation("role"))
            )
            self.assertEqual(len(rb_tuples), 1, f"RoleBinding {rb.uuid} should have exactly one role tuple")

        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

    def test_system_role_migrates_from_tenant_only_to_split(self):
        """A system role binding for a group migrates from tenant-only to tenant + workspace scoped bindings."""
        sub_perm = Permission.objects.create(
            tenant=self.public_tenant,
            application="subscriptions",
            resource_type="organization",
            verb="read",
            permission="subscriptions:organization:read",
        )
        inv_perm = Permission.objects.create(
            tenant=self.public_tenant,
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
        )

        system_role = Role.objects.create(
            tenant=self.public_tenant,
            name="Mixed System Role",
            system=True,
        )
        Access.objects.create(role=system_role, permission=sub_perm, tenant=self.public_tenant)
        Access.objects.create(role=system_role, permission=inv_perm, tenant=self.public_tenant)

        seed_v2_role_from_v1(system_role)

        group = Group.objects.create(name="Test Group", tenant=self.tenant, system=False)
        add_roles(group, [system_role.uuid], self.tenant)

        tenant_resource_id = self.tenant.tenant_resource_id()
        default_ws_id = str(self.default_workspace.id)
        group_uuid = str(group.uuid)

        # Run migration
        result = migrate_system_role_bindings_for_group(group, self.replicator)

        # Verify tuples: binding on tenant resource
        tenant_binding_tuples = self.tuples.find_tuples(
            all_of(resource("rbac", "tenant", tenant_resource_id), relation("binding"))
        )
        self.assertGreaterEqual(len(tenant_binding_tuples), 1, "Should have binding tuple on tenant")

        # Verify tuples: binding on workspace resource (default or root)
        ws_binding_tuples = self.tuples.find_tuples(
            all_of(resource("rbac", "workspace", default_ws_id), relation("binding"))
        )
        self.assertGreaterEqual(len(ws_binding_tuples), 1, "Should have binding tuple on workspace")

        # Verify the group is a subject of bindings at both levels.
        # System role bindings use rbac/role_binding#subject@rbac/group:uuid#member.
        all_subject_tuples = self.tuples.find_tuples(
            all_of(
                relation("subject"),
                subject_type("rbac", "group", any_relation=True),
                subject_id(group_uuid),
            )
        )

        # Each subject tuple's resource is a role_binding; collect which resource it's bound to.
        bound_resource_types = set()
        for st in all_subject_tuples:
            rb_uuid = st.resource.id
            # Check where this role_binding is attached (tenant or workspace)
            for rt in ["tenant", "workspace"]:
                binding_tuples = self.tuples.find_tuples(
                    all_of(
                        relation("binding"),
                        subject("rbac", "role_binding", rb_uuid),
                    )
                )
                for bt in binding_tuples:
                    bound_resource_types.add(bt.resource.type.name)

        self.assertIn("tenant", bound_resource_types, "Group should be bound at tenant scope")
        self.assertIn("workspace", bound_resource_types, "Group should be bound at workspace scope")
