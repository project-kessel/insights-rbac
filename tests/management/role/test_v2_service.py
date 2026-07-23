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
"""Test the RoleV2Service."""

import uuid
from unittest.mock import patch

from django.test import override_settings
from management.exceptions import RequiredFieldError
from management.models import Group, Workspace, Permission
from management.permission.scope_service import ImplicitResourceService, PermissionScopeCache
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.role.definer import seed_roles
from management.role.v2_exceptions import RoleAlreadyExistsError, RolesNotFoundError, CustomRoleRequiredError
from management.role.v2_model import CustomRoleV2, RoleV2, SeededRoleV2, PlatformRoleV2
from management.role.v2_service import RoleV2Service
from management.role_binding.model import RoleBinding
from management.role_binding.service import RoleBindingService
from management.tenant_service import V2TenantBootstrapService
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    all_of,
    relation,
    resource,
    subject,
)
from tests.identity_request import IdentityRequest

from api.models import Tenant


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RoleV2ServiceTests(IdentityRequest):
    """Test the RoleV2Service domain service."""

    def setUp(self):
        """Set up the RoleV2Service tests."""
        super().setUp()
        self.service = RoleV2Service(tenant=self.tenant)

        # Create test permissions - use unique names to avoid collisions with seed_roles()
        self.permission1 = Permission.objects.create(permission="test_app:resources:read", tenant=self.tenant)
        self.permission2 = Permission.objects.create(permission="test_app:resources:write", tenant=self.tenant)
        self.permission3 = Permission.objects.create(permission="test_app:reports:read", tenant=self.tenant)

        self.permission1_data = {"application": "test_app", "resource_type": "resources", "operation": "read"}

    def tearDown(self):
        """Tear down RoleV2Service tests."""
        from management.utils import PRINCIPAL_CACHE

        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()

        # Clear principal cache to avoid test isolation issues
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)

        super().tearDown()

    # ==========================================================================
    # Tests for create()
    # ==========================================================================

    def test_create_role_with_single_permission(self):
        """Test creating a role with a single permission."""
        permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
        ]

        role = self.service.create(
            name="Test Role",
            description="A test role",
            permission_data=permission_data,
            tenant=self.tenant,
        )

        self.assertIsInstance(role, CustomRoleV2)
        self.assertEqual(role.name, "Test Role")
        self.assertEqual(role.description, "A test role")
        self.assertEqual(role.type, RoleV2.Types.CUSTOM)
        self.assertEqual(role.tenant, self.tenant)
        self.assertEqual(role.permissions.count(), 1)
        self.assertIn(self.permission1, role.permissions.all())

    def test_create_role_with_multiple_permissions(self):
        """Test creating a role with multiple permissions."""
        permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
            {"application": "test_app", "resource_type": "resources", "operation": "write"},
            {"application": "test_app", "resource_type": "reports", "operation": "read"},
        ]

        role = self.service.create(
            name="Multi Permission Role",
            description="Role with multiple permissions",
            permission_data=permission_data,
            tenant=self.tenant,
        )

        self.assertEqual(role.permissions.count(), 3)
        self.assertIn(self.permission1, role.permissions.all())
        self.assertIn(self.permission2, role.permissions.all())
        self.assertIn(self.permission3, role.permissions.all())

    def test_create_role_with_empty_description_succeeds(self):
        """Test that creating a role with empty description succeeds."""
        permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
        ]

        role = self.service.create(
            name="No Description Role",
            description="",
            permission_data=permission_data,
            tenant=self.tenant,
        )

        self.assertEqual(role.description, "")
        self.assertEqual(role.permissions.count(), 1)

    def test_create_role_with_whitespace_only_description_succeeds(self):
        """Test that creating a role with whitespace-only description succeeds."""
        permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
        ]

        role = self.service.create(
            name="Whitespace Description Role",
            description="   ",
            permission_data=permission_data,
            tenant=self.tenant,
        )

        self.assertEqual(role.description, "   ")
        self.assertEqual(role.permissions.count(), 1)

    def test_create_role_with_empty_permissions_raises_error(self):
        """Test that creating a role with empty permissions raises RequiredFieldError."""
        with self.assertRaises(RequiredFieldError) as context:
            self.service.create(
                name="Empty Permissions Role",
                description="Valid description",
                permission_data=[],
                tenant=self.tenant,
            )

        self.assertEqual(context.exception.field_name, "permissions")

    def test_create_role_with_permission_missing_required_field_raises_error(self):
        """Test that permission missing a required field raises RequiredFieldError."""
        permission_data = [
            {"resource_type": "hosts", "operation": "read"},  # Missing 'application'
        ]

        with self.assertRaises(RequiredFieldError) as context:
            self.service.create(
                name="Missing App Permission Role",
                description="Valid description",
                permission_data=permission_data,
                tenant=self.tenant,
            )

        self.assertEqual(context.exception.field_name, "application")

    def test_create_role_with_missing_name_raises_error(self):
        """Test that creating a role with blank name raises RequiredFieldError."""
        permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
        ]

        with self.assertRaises(RequiredFieldError) as context:
            self.service.create(
                name="",
                description="Valid description",
                permission_data=permission_data,
                tenant=self.tenant,
            )

        self.assertEqual(context.exception.field_name, "name")

    def test_create_role_with_duplicate_name_raises_error(self):
        """Test that creating a role with a duplicate name raises RoleAlreadyExistsError."""
        # Uses permissions created in setUp: inventory:hosts:read and inventory:hosts:write
        permission_data1 = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
        ]
        permission_data2 = [
            {"application": "test_app", "resource_type": "resources", "operation": "write"},
        ]

        # Create first role
        self.service.create(
            name="Duplicate Name",
            description="First role",
            permission_data=permission_data1,
            tenant=self.tenant,
        )

        # Attempt to create second role with same name
        with self.assertRaises(RoleAlreadyExistsError) as cm:
            self.service.create(
                name="Duplicate Name",
                description="Second role",
                permission_data=permission_data2,
                tenant=self.tenant,
            )

        self.assertIn("Duplicate Name", str(cm.exception))
        self.assertEqual(cm.exception.name, "Duplicate Name")

    def test_create_role_with_mixed_scope_permissions_raises_error(self):
        """Creating a role with permissions from different scopes is rejected."""
        from management.permission.scope_service import ImplicitResourceService
        from management.role.v2_exceptions import InvalidRolePermissionsError

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=["tenant_app:*:*"],
            root_scope_permissions=["root_app:*:*"],
        )
        Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        Permission.objects.create(permission="root_app:res:read", tenant=self.tenant)

        with patch("management.role.v2_service.default_implicit_resource_service", scope_service):
            with self.assertRaises(InvalidRolePermissionsError) as ctx:
                self.service.create(
                    name="Mixed Scope Role",
                    description="Should fail",
                    permission_data=[
                        {"application": "tenant_app", "resource_type": "res", "operation": "read"},
                        {"application": "root_app", "resource_type": "res", "operation": "read"},
                    ],
                    tenant=self.tenant,
                )
            msg = str(ctx.exception)
            self.assertIn("same scope", msg)
            self.assertIn("tenant_app:res:read", msg)
            self.assertIn("root_app:res:read", msg)

    def test_create_role_with_same_scope_permissions_succeeds(self):
        """Creating a role where all permissions share the same scope succeeds."""
        from management.permission.scope_service import ImplicitResourceService

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=["tenant_app:*:*"],
            root_scope_permissions=[],
        )
        Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        Permission.objects.create(permission="tenant_app:res:write", tenant=self.tenant)

        with patch("management.role.v2_service.default_implicit_resource_service", scope_service):
            role = self.service.create(
                name="Same Scope Role",
                description="Should pass",
                permission_data=[
                    {"application": "tenant_app", "resource_type": "res", "operation": "read"},
                    {"application": "tenant_app", "resource_type": "res", "operation": "write"},
                ],
                tenant=self.tenant,
            )
        self.assertEqual(role.permissions.count(), 2)

    def test_create_role_generates_uuid(self):
        """Test that creating a role auto-generates a UUID."""
        role = self.service.create(
            name="UUID Test Role",
            description="Testing UUID generation",
            permission_data=[self.permission1_data],
            tenant=self.tenant,
        )

        self.assertIsNotNone(role.uuid)

    def test_create_role_sets_type_to_custom(self):
        """Test that created roles are always of type CUSTOM."""
        role = self.service.create(
            name="Custom Type Role",
            description="Should be custom",
            permission_data=[self.permission1_data],
            tenant=self.tenant,
        )

        self.assertEqual(role.type, RoleV2.Types.CUSTOM)

    # ==========================================================================
    # Tests for replication
    # ==========================================================================

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_create_role_replicates_permission_tuples(self):
        """Test that creating a role replicates permission tuples to SpiceDB."""
        # Create test permissions for this test
        Permission.objects.create(permission="test_app:test_resource:read", tenant=self.tenant)
        Permission.objects.create(permission="test_app:test_resource:write", tenant=self.tenant)

        # Set up in-memory replicator (stub, not mock!)
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        service = RoleV2Service(tenant=self.tenant, replicator=replicator)

        permission_data = [
            {"application": "test_app", "resource_type": "test_resource", "operation": "read"},
            {"application": "test_app", "resource_type": "test_resource", "operation": "write"},
        ]

        # When: Create a role
        role = service.create(
            name="Replication Test Role",
            description="A test role for replication",
            permission_data=permission_data,
            tenant=self.tenant,
        )

        # Then: Permission tuples and owner tuple are replicated
        role_uuid = str(role.uuid)

        # Should have 2 permission tuples + 1 owner tuple
        self.assertEqual(len(tuples), 3)

        # Verify the read permission tuple exists
        read_tuples = tuples.find_tuples(
            all_of(
                resource("rbac", "role", role_uuid),
                relation("test_app_test_resource_read"),
                subject("rbac", "principal", "*"),
            )
        )
        self.assertEqual(len(read_tuples), 1, "Expected 1 read permission tuple")

        # Verify the write permission tuple exists
        write_tuples = tuples.find_tuples(
            all_of(
                resource("rbac", "role", role_uuid),
                relation("test_app_test_resource_write"),
                subject("rbac", "principal", "*"),
            )
        )
        self.assertEqual(len(write_tuples), 1, "Expected 1 write permission tuple")

        # Verify the owner tuple exists
        owner_tuples = tuples.find_tuples(
            all_of(
                resource("rbac", "role", role_uuid),
                relation("owner"),
                subject("rbac", "tenant", self.tenant.tenant_resource_id()),
            )
        )
        self.assertEqual(len(owner_tuples), 1, "Expected 1 owner tuple")

    def test_update_role_with_empty_description_succeeds(self):
        """Test that updating a role with empty description succeeds."""
        permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
        ]

        role = self.service.create(
            name="Test Role",
            description="Original description",
            permission_data=permission_data,
            tenant=self.tenant,
        )

        updated_role = self.service.update(
            role_uuid=str(role.uuid),
            name="Test Role",
            description="",
            permission_data=permission_data,
            tenant=self.tenant,
        )

        self.assertEqual(updated_role.description, "")

    def test_update_role_with_empty_permissions_raises_error(self):
        """Test that updating a role with empty permissions raises RequiredFieldError."""
        permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
        ]

        role = self.service.create(
            name="Test Role",
            description="A test role",
            permission_data=permission_data,
            tenant=self.tenant,
        )

        with self.assertRaises(RequiredFieldError) as context:
            self.service.update(
                role_uuid=str(role.uuid),
                name="Test Role",
                description="A test role",
                permission_data=[],
                tenant=self.tenant,
            )

        self.assertEqual(context.exception.field_name, "permissions")

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_update_role_replicates_permission_tuples(self):
        """Test that updating a role replicates old and new permission tuples to SpiceDB."""
        # Set up in-memory replicator
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        service = RoleV2Service(tenant=self.tenant, replicator=replicator)

        # Create initial role with read and write permissions
        initial_permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
            {"application": "test_app", "resource_type": "resources", "operation": "write"},
        ]

        role = service.create(
            name="Update Replication Test Role",
            description="Initial description",
            permission_data=initial_permission_data,
            tenant=self.tenant,
        )

        # Don't clear - we need the initial state for delta computation to work correctly
        # The delta will remove {write} and add {cost}, resulting in {read, cost}
        role_uuid = str(role.uuid)

        # Update the role to have different permissions (read and cost:reports:read)
        updated_permission_data = [
            {"application": "test_app", "resource_type": "resources", "operation": "read"},
            {"application": "test_app", "resource_type": "reports", "operation": "read"},
        ]

        updated_role = service.update(
            role_uuid=role_uuid,
            name="Updated Replication Test Role",
            description="Updated description",
            permission_data=updated_permission_data,
            tenant=self.tenant,
        )

        # Then: Verify the update produced the correct replication events
        # The replicator uses delta computation, so it should have:
        # - Removed: {write} (only permissions no longer in the role)
        # - Added: {cost:reports:read} (only new permissions)
        # - Kept: {read} (unchanged, so not touched by delta)

        # The InMemoryTuples tracks the final state after all operations
        # After update, we should have exactly 3 tuples (2 permissions + 1 owner)
        self.assertEqual(len(tuples), 3)

        # Verify the read permission tuple still exists (it was in both old and new)
        read_tuples = tuples.find_tuples(
            all_of(
                resource("rbac", "role", role_uuid),
                relation("test_app_resources_read"),
                subject("rbac", "principal", "*"),
            )
        )
        self.assertEqual(len(read_tuples), 1, "Expected 1 read permission tuple")

        # Verify the write permission tuple was removed (not in new permissions)
        write_tuples = tuples.find_tuples(
            all_of(
                resource("rbac", "role", role_uuid),
                relation("test_app_resources_write"),
                subject("rbac", "principal", "*"),
            )
        )
        self.assertEqual(len(write_tuples), 0, "Write permission should be removed")

        # Verify the cost:reports:read permission tuple was added
        cost_tuples = tuples.find_tuples(
            all_of(
                resource("rbac", "role", role_uuid),
                relation("test_app_reports_read"),
                subject("rbac", "principal", "*"),
            )
        )
        self.assertEqual(len(cost_tuples), 1, "Expected 1 cost:reports:read permission tuple")

    # ==========================================================================
    # Tests for bulk_delete()
    # ==========================================================================

    def test_delete_empty(self):
        """Test that calling bulk_delete with no roles is successful."""
        try:
            self.service.bulk_delete([])
        except Exception as e:
            self.fail(f"Unexpected exception: {e}")

    def test_delete_roles_across_tenants(self):
        """Test that custom roles can be deleted."""
        tenant2 = V2TenantBootstrapService(OutboxReplicator()).new_bootstrapped_tenant("t2").tenant

        r1 = self.service.create("r1", "r1", [self.permission1_data], self.tenant)
        r2 = self.service.create("r2", "r2", [self.permission1_data], tenant2)

        self.service.bulk_delete([str(r1.uuid), str(r2.uuid)])

        self.assertFalse(RoleV2.objects.filter(pk=r1.pk).exists())
        self.assertFalse(RoleV2.objects.filter(pk=r2.pk).exists())

    def test_delete_roles_within_tenant(self):
        """Test that multiple custom roles within a tenant can be deleted."""
        r1 = self.service.create("r1", "r1", [self.permission1_data], self.tenant)
        r2 = self.service.create("r2", "r2", [self.permission1_data], self.tenant)

        self.service.bulk_delete([str(r1.uuid), str(r2.uuid)], from_tenant=self.tenant)

        self.assertFalse(RoleV2.objects.filter(pk=r1.pk).exists())
        self.assertFalse(RoleV2.objects.filter(pk=r2.pk).exists())

    def test_delete_role_without_tenant(self):
        """Test that custom roles cannot be deleted outside the provided tenant."""
        tenant2 = V2TenantBootstrapService(OutboxReplicator()).new_bootstrapped_tenant("t2").tenant

        r1 = self.service.create("r1", "r1", [self.permission1_data], self.tenant)
        r2 = self.service.create("r2", "r2", [self.permission1_data], tenant2)

        with self.assertRaises(RolesNotFoundError) as context:
            self.service.bulk_delete([str(r1.uuid), str(r2.uuid)], from_tenant=self.tenant)

        self.assertIn(r2.uuid, context.exception.uuids)

        self.assertTrue(CustomRoleV2.objects.filter(pk=r1.pk).exists())
        self.assertTrue(CustomRoleV2.objects.filter(pk=r2.pk).exists())

    def test_delete_nonexistent(self):
        """Test that a nonexistent role cannot be deleted."""
        fake_a = uuid.uuid4()
        fake_b = uuid.uuid4()

        with self.assertRaises(RolesNotFoundError) as context:
            self.service.bulk_delete([str(fake_a), fake_b])

        self.assertIn(fake_a, context.exception.uuids)
        self.assertIn(fake_b, context.exception.uuids)

    def test_delete_seeded_role(self):
        """Test that a seeded role cannot be deleted."""
        # Create some seeded roles.
        seed_roles()

        seeded_role = SeededRoleV2.objects.first()
        custom_role = self.service.create("custom", "custom", [self.permission1_data], self.tenant)

        self.assertIsNotNone(seeded_role)

        with self.assertRaises(CustomRoleRequiredError) as context:
            self.service.bulk_delete([str(seeded_role.uuid), str(custom_role.uuid)])

        self.assertIn(str(seeded_role.uuid), str(context.exception))

        # No role should have been deleted.
        self.assertTrue(CustomRoleV2.objects.filter(pk=custom_role.pk).exists())
        self.assertTrue(SeededRoleV2.objects.filter(pk=seeded_role.pk).exists())

    def test_delete_platform_role(self):
        """Test that a platform role cannot be deleted."""
        # Create platform roles.
        seed_roles()

        platform_role = PlatformRoleV2.objects.first()
        custom_role = self.service.create("custom", "custom", [self.permission1_data], self.tenant)

        self.assertIsNotNone(platform_role)

        with self.assertRaises(CustomRoleRequiredError) as context:
            self.service.bulk_delete([str(platform_role.uuid), str(custom_role.uuid)])

        self.assertIn(str(platform_role.uuid), str(context.exception))
        self.assertTrue(PlatformRoleV2.objects.filter(pk=platform_role.pk).exists())

    def test_delete_duplicate_ids(self):
        """Test that a role is deleted if its ID is provided multiple times."""
        role = self.service.create("role", "role", [self.permission1_data], self.tenant)
        self.service.bulk_delete([role.uuid, str(role.uuid), str(role.uuid)])

        self.assertFalse(RoleV2.objects.filter(pk=role.pk).exists())

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_delete_replication(self):
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)

        V2TenantBootstrapService(replicator=replicator).bootstrap_tenant(self.tenant, force=True)

        initial_tuples = set(tuples)

        service = RoleV2Service(tenant=self.tenant, replicator=replicator)

        def assert_permission_count(count: int, role_uuid: str):
            self.assertEqual(
                count,
                tuples.count_tuples(
                    all_of(
                        resource("rbac", "role", role_uuid),
                        relation("test_app_resources_read"),
                        subject("rbac", "principal", "*"),
                    )
                ),
            )

        def assert_role_binding_group_count(count: int, role_binding_uuid: str, group_uuid: str):
            self.assertEqual(
                count,
                tuples.count_tuples(
                    all_of(
                        resource("rbac", "role_binding", role_binding_uuid),
                        relation("subject"),
                        subject("rbac", "group", group_uuid, "member"),
                    )
                ),
            )

        role = service.create("a role", "a description", [self.permission1_data], self.tenant)
        assert_permission_count(1, str(role.uuid))

        group = Group.objects.create(tenant=self.tenant, name="a group")
        workspace = Workspace.objects.default(tenant=self.tenant)

        RoleBindingService(tenant=self.tenant, replicator=replicator).update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(workspace.id),
            subject_type="group",
            subject_id=str(group.uuid),
            role_ids=[str(role.uuid)],
        )

        role_binding = RoleBinding.objects.get(role=role)

        assert_role_binding_group_count(1, role_binding_uuid=str(role_binding.uuid), group_uuid=str(group.uuid))

        service.bulk_delete([str(role.uuid)])

        assert_permission_count(0, str(role.uuid))
        assert_role_binding_group_count(0, role_binding_uuid=str(role_binding.uuid), group_uuid=str(group.uuid))

        # We have created a role and a role binding, then destroyed them. We should have exactly the same tuples as
        # when we started.
        self.assertEqual(set(tuples), initial_tuples)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RoleV2ServiceListTests(IdentityRequest):
    """Test the RoleV2Service.list() method."""

    def setUp(self):
        """Set up the RoleV2Service list tests."""
        super().setUp()
        self.service = RoleV2Service(tenant=self.tenant)

        # Create test permissions - use unique names to avoid collisions with seed_roles()
        self.permission1 = Permission.objects.create(permission="test_app:resources:read", tenant=self.tenant)
        self.permission2 = Permission.objects.create(permission="test_app:resources:write", tenant=self.tenant)

        # Create test roles
        self.role1 = RoleV2.objects.create(name="role_one", description="First role", tenant=self.tenant)
        self.role1.permissions.add(self.permission1)

        self.role2 = RoleV2.objects.create(name="role_two", description="Second role", tenant=self.tenant)
        self.role2.permissions.add(self.permission1, self.permission2)

    def tearDown(self):
        """Tear down RoleV2Service list tests."""
        from management.utils import PRINCIPAL_CACHE

        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()

        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)
        super().tearDown()

    # ==========================================================================
    # Tests for list()
    # ==========================================================================

    def test_list_returns_roles_for_tenant(self):
        """Test that list() returns all roles belonging to the tenant."""
        queryset = self.service.list({"resource_type": "workspace"})

        self.assertEqual(queryset.count(), 2)
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"role_one", "role_two"})

    def test_list_filters_by_exact_name(self):
        """Test that name param filters using case-insensitive exact match."""
        queryset = self.service.list({"resource_type": "workspace", "name": "ROLE_ONE"})

        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "role_one")

    def test_list_name_filter_no_match_returns_empty(self):
        """Test that a name filter matching nothing returns an empty queryset."""
        queryset = self.service.list({"resource_type": "workspace", "name": "nonexistent_role"})

        self.assertEqual(queryset.count(), 0)

    def test_list_filters_by_name_wildcard_prefix(self):
        """Test that name=role_o* matches names starting with 'role_o'."""
        queryset = self.service.list({"name": "role_o*"})

        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "role_one")

    def test_list_filters_by_name_wildcard_case_insensitive(self):
        """Test that wildcard name filtering is case-insensitive."""
        queryset = self.service.list({"name": "ROLE_O*"})

        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "role_one")

    def test_list_filters_by_name_wildcard_suffix(self):
        """Test that name=*two matches names ending with 'two'."""
        queryset = self.service.list({"name": "*two"})

        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "role_two")

    def test_list_filters_by_name_wildcard_contains(self):
        """Test that name=*ole_* matches names containing 'ole_'."""
        queryset = self.service.list({"name": "*ole_*"})

        self.assertEqual(queryset.count(), 2)
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"role_one", "role_two"})

    def test_list_filters_by_name_wildcard_complex(self):
        """Test that a multi-wildcard pattern like r*_on* matches correctly."""
        queryset = self.service.list({"name": "r*_on*"})

        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "role_one")

    def test_list_name_substring_match(self):
        """Test that name without wildcard does substring match."""
        queryset = self.service.list({"name": "role"})

        self.assertEqual(queryset.count(), 2)
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"role_one", "role_two"})

    def test_list_filters_by_name_wildcard_no_match(self):
        """Test that a wildcard pattern matching nothing returns empty."""
        queryset = self.service.list({"name": "zzz*"})

        self.assertEqual(queryset.count(), 0)

    def test_list_without_name_returns_all(self):
        """Test that omitting the name param returns all roles for the tenant."""
        RoleV2.objects.create(name="role_other", description="Other role", tenant=self.tenant)

        queryset = self.service.list({"resource_type": "workspace"})

        self.assertEqual(queryset.count(), 3)

    def test_list_name_with_literal_star_matches_as_glob(self):
        """Test that a role whose name contains '*' is matched by glob, not literally.

        If a role is named 'role_*_admin', searching for 'role_*_admin' treats
        the '*' as a wildcard, so it also matches 'role_foo_admin'.
        """
        RoleV2.objects.create(name="role_*_admin", description="Star role", tenant=self.tenant)
        RoleV2.objects.create(name="role_foo_admin", description="Foo role", tenant=self.tenant)

        queryset = self.service.list({"name": "role_*_admin"})

        names = set(queryset.values_list("name", flat=True))
        self.assertIn("role_*_admin", names)
        self.assertIn("role_foo_admin", names)
        self.assertEqual(queryset.count(), 2)

    def test_list_name_with_literal_star_no_exact_escape(self):
        """Test there is no way to search for a literal '*' via the name filter.

        Since any '*' triggers glob mode, a role named 'star*role' cannot be
        targeted exclusively — the filter will always treat '*' as a wildcard.
        """
        RoleV2.objects.create(name="star*role", description="Has star", tenant=self.tenant)
        RoleV2.objects.create(name="starXrole", description="No star", tenant=self.tenant)

        queryset = self.service.list({"name": "star*role"})

        self.assertEqual(queryset.count(), 2)

    def test_list_name_regex_metacharacters_escaped(self):
        """Test that regex metacharacters in role names are properly escaped.

        A role named 'role.one' must NOT match 'role_one' when searched by
        exact name, because '.' is escaped by re.escape().
        """
        RoleV2.objects.create(name="role.one", description="Dot role", tenant=self.tenant)

        exact = self.service.list({"name": "role.one"})
        self.assertEqual(exact.count(), 1)
        self.assertEqual(exact.first().name, "role.one")

        # role_one and role_two already exist from setUp; '.' must NOT act as regex wildcard
        glob = self.service.list({"name": "role.*"})
        names = set(glob.values_list("name", flat=True))
        self.assertEqual(names, {"role.one"})

    def test_list_name_glob_star_only_matches_all(self):
        """Test that name='*' matches every role for the tenant."""
        queryset = self.service.list({"name": "*"})

        self.assertEqual(queryset.count(), 2)

    def test_list_name_glob_excess_wildcards_become_literal(self):
        """Test that wildcards beyond the maxsplit limit are treated as literal '*'."""

        # Build a pattern with >10 wildcards: "a*b*c*...*<last>"
        segments = [chr(ord("a") + i) for i in range(13)]
        search_pattern = "*".join(segments)

        # Role whose name uses literal '*' where the excess wildcards are
        role_with_stars = RoleV2.objects.create(
            name="*".join(segments),
            description="Has literal stars",
            tenant=self.tenant,
        )
        # Role that replaces the excess '*' with regular chars — should NOT match
        partial = list(segments)
        partial[-2] = "X" + partial[-2]
        partial[-1] = "X" + partial[-1]
        RoleV2.objects.create(
            name="X".join(segments),
            description="No literal stars",
            tenant=self.tenant,
        )

        queryset = self.service.list({"name": search_pattern})

        names = set(queryset.values_list("name", flat=True))
        self.assertIn(role_with_stars.name, names)
        self.assertEqual(queryset.count(), 1)

    def test_list_annotates_permissions_count(self):
        """Test that queryset has permissions_count_annotation when fields includes permissions_count."""
        queryset = self.service.list({"resource_type": "workspace", "fields": {"permissions_count"}})

        for role in queryset:
            self.assertTrue(hasattr(role, "permissions_count_annotation"))

        role_one = queryset.get(name="role_one")
        role_two = queryset.get(name="role_two")
        self.assertEqual(role_one.permissions_count_annotation, 1)
        self.assertEqual(role_two.permissions_count_annotation, 2)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RoleV2ServiceListResourceTypeTests(IdentityRequest):
    """Test the RoleV2Service.list() resource_type filtering."""

    def setUp(self):
        """Set up resource_type filter tests."""
        super().setUp()
        self.service = RoleV2Service(tenant=self.tenant)

        self._root_ws, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
            defaults={
                "name": Workspace.SpecialNames.ROOT,
                "description": Workspace.SpecialDescriptions.ROOT,
            },
        )
        self._default_ws, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            defaults={
                "name": Workspace.SpecialNames.DEFAULT,
                "description": Workspace.SpecialDescriptions.DEFAULT,
                "parent": self._root_ws,
            },
        )

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=["tenant_app:*:*"],
            root_scope_permissions=["root_app:*:*"],
        )
        test_cache = PermissionScopeCache(scope_service)
        self._cache_patcher = patch("management.role.v2_service.permission_scope_cache", test_cache)
        self._cache_patcher.start()

        self.default_perm = Permission.objects.create(permission="default_app:resource:read", tenant=self.tenant)
        self.root_perm = Permission.objects.create(permission="root_app:resource:read", tenant=self.tenant)
        self.tenant_perm = Permission.objects.create(permission="tenant_app:resource:read", tenant=self.tenant)

        self.default_role = RoleV2.objects.create(
            name="default_role", description="Default scoped", tenant=self.tenant
        )
        self.default_role.permissions.add(self.default_perm)

        self.root_role = RoleV2.objects.create(name="root_role", description="Root scoped", tenant=self.tenant)
        self.root_role.permissions.add(self.root_perm)

        self.tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant scoped", tenant=self.tenant)
        self.tenant_role.permissions.add(self.tenant_perm)

        self.mixed_role = RoleV2.objects.create(
            name="mixed_role", description="Has both default and tenant perms", tenant=self.tenant
        )
        self.mixed_role.permissions.add(self.default_perm, self.tenant_perm)

    def tearDown(self):
        """Tear down resource_type filter tests."""
        from management.utils import PRINCIPAL_CACHE

        self._cache_patcher.stop()
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)
        super().tearDown()

    def test_list_resource_type_tenant_returns_tenant_scoped_roles(self):
        """Roles whose highest scope is TENANT should be returned for resource_type=tenant."""
        queryset = self.service.list({"resource_type": "tenant"})
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"tenant_role", "mixed_role"})

    def test_list_resource_type_workspace_returns_default_scoped_roles_only(self):
        """resource_type=workspace without resource_id returns only DEFAULT-scoped roles."""
        queryset = self.service.list({"resource_type": "workspace"})
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"default_role"})

    def test_list_resource_type_workspace_excludes_mixed_role(self):
        """A role with both default and tenant permissions has highest scope TENANT and should not appear for workspace."""
        queryset = self.service.list({"resource_type": "workspace"})
        names = set(queryset.values_list("name", flat=True))
        self.assertNotIn("mixed_role", names)

    def test_list_without_resource_type_returns_all_roles(self):
        """Omitting resource_type returns roles from all scopes."""
        queryset = self.service.list({})
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"default_role", "root_role", "tenant_role", "mixed_role"})

    def test_list_unknown_resource_type_returns_empty(self):
        """An unknown resource_type returns an empty queryset."""
        queryset = self.service.list({"resource_type": "unknown"})
        self.assertEqual(queryset.count(), 0)

    def test_list_resource_type_combined_with_name_filter(self):
        """resource_type and name filters can be combined."""
        queryset = self.service.list({"resource_type": "tenant", "name": "tenant_role"})
        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "tenant_role")

    def test_list_resource_type_combined_with_name_filter_no_match(self):
        """Combined filters that contradict each other return empty."""
        queryset = self.service.list({"resource_type": "workspace", "name": "tenant_role"})
        self.assertEqual(queryset.count(), 0)

    def test_list_resource_type_workspace_with_root_resource_id_returns_root_scoped_only(self):
        """resource_type=workspace with the root workspace id returns only ROOT-scoped roles."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._root_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"root_role"})

    def test_list_resource_type_workspace_with_default_resource_id_returns_default_scoped_only(self):
        """resource_type=workspace with the default workspace id returns only DEFAULT-scoped roles."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._default_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"default_role"})

    def test_list_resource_type_workspace_with_nonexistent_resource_id_returns_empty(self):
        """Unknown workspace id yields no roles for resource_type=workspace."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": uuid.uuid4()})
        self.assertEqual(queryset.count(), 0)

    def test_list_resource_type_workspace_with_standard_resource_id_returns_default_scoped_only(self):
        """A non-root (e.g. standard) workspace id lists only workspace-granular roles (no explicit config)."""
        child = Workspace.objects.create(
            name="Sub WS",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self._default_ws,
        )
        queryset = self.service.list({"resource_type": "workspace", "resource_id": child.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"default_role"})


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RoleV2ServiceListExplicitDefaultScopeTests(IdentityRequest):
    """Test workspace-level role scoping with explicit DEFAULT_SCOPE_PERMISSIONS.

    When DEFAULT_SCOPE_PERMISSIONS is configured, roles with explicit-DEFAULT
    permissions should only appear at the default workspace, not at standard
    (child) workspaces.  Workspace-granular roles (fallback-DEFAULT) should
    appear at both.
    """

    def setUp(self):
        super().setUp()
        self.service = RoleV2Service(tenant=self.tenant)

        self._root_ws, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
            defaults={
                "name": Workspace.SpecialNames.ROOT,
                "description": Workspace.SpecialDescriptions.ROOT,
            },
        )
        self._default_ws, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            defaults={
                "name": Workspace.SpecialNames.DEFAULT,
                "description": Workspace.SpecialDescriptions.DEFAULT,
                "parent": self._root_ws,
            },
        )
        self._standard_ws = Workspace.objects.create(
            name="Standard WS",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self._default_ws,
        )

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=["tenant_app:*:*"],
            root_scope_permissions=["root_app:*:*"],
            default_scope_permissions=["explicit_app:*:*"],
        )
        test_cache = PermissionScopeCache(scope_service)
        self._cache_patcher = patch("management.role.v2_service.permission_scope_cache", test_cache)
        self._cache_patcher.start()

        self.ws_granular_perm = Permission.objects.create(permission="granular_app:resource:read", tenant=self.tenant)
        self.explicit_default_perm = Permission.objects.create(
            permission="explicit_app:resource:read", tenant=self.tenant
        )
        self.root_perm = Permission.objects.create(permission="root_app:resource:read", tenant=self.tenant)
        self.tenant_perm = Permission.objects.create(permission="tenant_app:resource:read", tenant=self.tenant)

        self.ws_granular_role = RoleV2.objects.create(
            name="ws_granular_role", description="Workspace-granular (fallback DEFAULT)", tenant=self.tenant
        )
        self.ws_granular_role.permissions.add(self.ws_granular_perm)

        self.explicit_default_role = RoleV2.objects.create(
            name="explicit_default_role", description="Explicit DEFAULT scoped", tenant=self.tenant
        )
        self.explicit_default_role.permissions.add(self.explicit_default_perm)

        self.mixed_granular_explicit_role = RoleV2.objects.create(
            name="mixed_granular_explicit_role",
            description="Has both workspace-granular and explicit-DEFAULT perms",
            tenant=self.tenant,
        )
        self.mixed_granular_explicit_role.permissions.add(self.ws_granular_perm, self.explicit_default_perm)

        self.permissionless_role = RoleV2.objects.create(
            name="permissionless_role", description="No permissions (OCM external)", tenant=self.tenant
        )

        self.root_role = RoleV2.objects.create(name="root_role", description="Root scoped", tenant=self.tenant)
        self.root_role.permissions.add(self.root_perm)

        self.tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant scoped", tenant=self.tenant)
        self.tenant_role.permissions.add(self.tenant_perm)

    def tearDown(self):
        from management.utils import PRINCIPAL_CACHE

        self._cache_patcher.stop()
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)
        super().tearDown()

    def test_standard_workspace_returns_only_workspace_granular_roles(self):
        """Standard workspace should only return workspace-granular (fallback-DEFAULT) roles."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._standard_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"ws_granular_role"})

    def test_standard_workspace_excludes_explicit_default_roles(self):
        """Standard workspace should exclude roles with explicit-DEFAULT permissions."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._standard_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertNotIn("explicit_default_role", names)

    def test_standard_workspace_excludes_mixed_granular_and_explicit_roles(self):
        """Standard workspace should exclude roles that mix workspace-granular with explicit-DEFAULT permissions."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._standard_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertNotIn("mixed_granular_explicit_role", names)

    def test_standard_workspace_excludes_permissionless_roles(self):
        """Standard workspace should exclude roles with no permissions."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._standard_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertNotIn("permissionless_role", names)

    def test_default_workspace_returns_explicit_default_and_workspace_granular(self):
        """Default workspace should return both explicit-DEFAULT and workspace-granular roles."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._default_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertIn("ws_granular_role", names)
        self.assertIn("explicit_default_role", names)
        self.assertIn("mixed_granular_explicit_role", names)

    def test_default_workspace_returns_permissionless_roles(self):
        """Default workspace should include permissionless roles."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._default_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertIn("permissionless_role", names)

    def test_default_workspace_excludes_root_and_tenant_roles(self):
        """Default workspace should still exclude ROOT and TENANT scoped roles."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._default_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertNotIn("root_role", names)
        self.assertNotIn("tenant_role", names)

    def test_workspace_without_resource_id_returns_all_default_scoped(self):
        """resource_type=workspace without resource_id returns all DEFAULT-scoped roles (both types)."""
        queryset = self.service.list({"resource_type": "workspace"})
        names = set(queryset.values_list("name", flat=True))
        self.assertIn("ws_granular_role", names)
        self.assertIn("explicit_default_role", names)
        self.assertIn("mixed_granular_explicit_role", names)
        self.assertIn("permissionless_role", names)

    def test_root_workspace_returns_root_roles_only(self):
        """Root workspace should still return only ROOT-scoped roles."""
        queryset = self.service.list({"resource_type": "workspace", "resource_id": self._root_ws.id})
        names = set(queryset.values_list("name", flat=True))
        self.assertEqual(names, {"root_role"})
