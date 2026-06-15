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
"""Tests for system role binding scope updates during seeding."""

from unittest.mock import patch
from django.conf import settings
from django.test import override_settings
from management.group.definer import add_roles, seed_group
from management.group.model import Group
from management.models import Permission
from management.permission.scope_service import Scope
from management.role.definer import (
    seed_roles,
    _determine_old_scopes,
    _log_scope_change_and_migrate,
)
from management.role.model import Role
from management.tenant_mapping.model import TenantMapping
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from migration_tool.in_memory_tuples import (
    InMemoryTuples,
    InMemoryRelationReplicator,
    all_of,
    resource,
    relation,
    subject,
)
from tests.management.role.test_dual_write import DualWriteTestCase, RbacFixture

from api.models import Tenant


@override_settings(ATOMIC_RETRY_DISABLED=True, REPLICATION_TO_RELATION_ENABLED=True)
class SystemRoleBindingScopeUpdateTests(DualWriteTestCase):
    """Test that system role bindings are updated when role scope changes during seeding."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.public_tenant = Tenant.objects.get(tenant_name="public")

        # Deactivate V2 writes to make this a V1 tenant for testing
        # This is required because _determine_old_scopes only checks V1 tenants
        TenantMapping.objects.filter(tenant=self.tenant).update(v2_write_activated_at=None)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_bindings_migrate_from_default_to_tenant_scope_v1_tenant(self, mock_replicate):
        """Test that bindings actually migrate from default workspace to tenant when scope changes in V1 tenant."""
        # Redirect replicator
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Create a permission in public tenant that will initially have DEFAULT scope
        permission = Permission.objects.create(
            tenant=self.public_tenant,
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
        )

        # Seed roles with DEFAULT scope for inventory permissions
        # (permissions not in ROOT or TENANT automatically fall to DEFAULT)
        with self.settings(
            ROOT_SCOPE_PERMISSIONS="",
            TENANT_SCOPE_PERMISSIONS="",
        ):
            seed_group()
            seed_roles()

        # Find the specific Inventory Hosts Viewer Local Test role from test data
        # This role has inventory permissions, is platform_default, but NOT admin_default
        # (avoiding the admin_default ROOT scope override)
        test_role = Role.objects.filter(
            system=True,
            tenant=self.public_tenant,
            name="Inventory Hosts Viewer Local Test",
        ).first()

        self.assertIsNotNone(test_role, "Expected 'Inventory Hosts Viewer Local Test' role to exist in seeded roles")

        # Verify the role has inventory permissions
        role_permissions = list(test_role.access.all().values_list("permission__permission", flat=True))
        self.assertTrue(
            any("inventory" in perm for perm in role_permissions),
            f"Expected role to have inventory permissions, got {role_permissions}",
        )

        # Create a non-default group and manually assign the role (not via default access)
        # This simulates a user creating a group and assigning a system role to it
        group = Group.objects.create(
            name="Test Group", tenant=self.tenant, system=False, platform_default=False, admin_default=False
        )
        add_roles(group, [test_role.uuid], self.tenant)

        # Verify initial binding at DEFAULT workspace using in-memory tuples
        default_ws_id = self.default_workspace()
        initial_bindings = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("binding"),
            )
        ).traverse_subject(
            [
                all_of(
                    relation("subject"),
                    subject("rbac", "group", str(group.uuid), "member"),
                )
            ],
            require_full_match=False,
        )
        self.assertGreater(
            len(initial_bindings),
            0,
            f"Should have binding at default workspace for group {group.uuid} initially",
        )

        # Verify NO binding at tenant level initially
        tenant_resource_id = Tenant.org_id_to_tenant_resource_id(self.tenant.org_id)
        initial_tenant_bindings = self.tuples.find_tuples(
            all_of(
                resource("rbac", "tenant", tenant_resource_id),
                relation("binding"),
            )
        ).traverse_subject(
            [
                all_of(
                    relation("subject"),
                    subject("rbac", "group", str(group.uuid), "member"),
                )
            ],
            require_full_match=False,
        )
        self.assertEqual(
            len(initial_tenant_bindings), 0, f"Should NOT have binding at tenant for group {group.uuid} initially"
        )

        # Change scope to TENANT by moving inventory permissions to TENANT_SCOPE_PERMISSIONS
        with self.settings(
            ROOT_SCOPE_PERMISSIONS="",
            TENANT_SCOPE_PERMISSIONS="inventory:*:*",
        ):
            seed_roles(force_update_relationships=True)

        # Verify binding REMOVED from default workspace
        final_default_bindings = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", default_ws_id),
                relation("binding"),
            )
        ).traverse_subject(
            [
                all_of(
                    relation("subject"),
                    subject("rbac", "group", str(group.uuid), "member"),
                )
            ],
            require_full_match=False,
        )
        self.assertEqual(
            len(final_default_bindings),
            0,
            f"Should have removed binding from default workspace for group {group.uuid}",
        )

        # Verify binding ADDED at tenant level
        final_tenant_bindings = self.tuples.find_tuples(
            all_of(
                resource("rbac", "tenant", tenant_resource_id),
                relation("binding"),
            )
        ).traverse_subject(
            [
                all_of(
                    relation("subject"),
                    subject("rbac", "group", str(group.uuid), "member"),
                )
            ],
            require_full_match=False,
        )
        self.assertGreater(
            len(final_tenant_bindings), 0, f"Should have added binding at tenant for group {group.uuid}"
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_determine_old_scopes_skips_v2_tenants(self, mock_replicate):
        """Test that _determine_old_scopes only checks V1 tenants, not V2 tenants."""
        # Redirect replicator
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Create a V1 tenant (already set up in setUp)
        # Create a permission in public tenant
        permission = Permission.objects.create(
            tenant=self.public_tenant,
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
        )

        # Seed roles with DEFAULT scope
        with self.settings(
            ROOT_SCOPE_PERMISSIONS="",
            TENANT_SCOPE_PERMISSIONS="",
        ):
            seed_group()
            seed_roles()

        # Find the Inventory Hosts Viewer Local Test role
        test_role = Role.objects.filter(
            system=True,
            tenant=self.public_tenant,
            name="Inventory Hosts Viewer Local Test",
        ).first()

        self.assertIsNotNone(test_role, "Expected 'Inventory Hosts Viewer Local Test' role to exist")

        # Create a group and assign the role in V1 tenant
        group_v1 = Group.objects.create(
            name="Test Group V1", tenant=self.tenant, system=False, platform_default=False, admin_default=False
        )
        add_roles(group_v1, [test_role.uuid], self.tenant)

        # Verify that _determine_old_scopes finds the V1 tenant binding
        old_scopes_v1 = _determine_old_scopes(test_role)
        self.assertGreater(len(old_scopes_v1), 0, "Should detect scopes for role in V1 tenant")

        # Now activate V2 writes for this tenant
        ensure_v2_write_activated(self.tenant)

        # After V2 activation, _determine_old_scopes should return empty
        # because the query filters for v2_write_activated_at=None
        old_scopes_after_v2 = _determine_old_scopes(test_role)
        self.assertEqual(
            old_scopes_after_v2,
            set(),
            "Should not detect scopes after tenant is activated for V2 (query filters out V2 tenants)",
        )

    def test_determine_old_scopes_returns_empty_for_new_role(self):
        """Test that _determine_old_scopes returns empty set when there's no existing V1 role."""
        result = _determine_old_scopes(None)
        self.assertEqual(result, set(), "Should return empty set for None role")

    def test_determine_old_scopes_returns_empty_for_role_without_bindings(self):
        """Test that _determine_old_scopes returns empty set when role has no bindings in V1 tenants."""
        # Create a role with no bindings
        test_role = Role.objects.create(
            name="Test Role No Bindings",
            tenant=self.public_tenant,
            system=True,
        )
        result = _determine_old_scopes(test_role)
        self.assertEqual(result, set(), "Should return empty set for role without bindings")

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_determine_old_scopes_detects_scope_from_bindings(self, mock_replicate):
        """Test that _determine_old_scopes correctly identifies scopes from actual bindings."""
        # Redirect replicator
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Seed roles to create bindings
        seed_group()
        seed_roles()

        # Find a specific seeded role with known properties
        v1_role = Role.objects.filter(
            system=True, tenant=self.public_tenant, name="Inventory Hosts Viewer Local Test"
        ).first()

        self.assertIsNotNone(v1_role, "Expected 'Inventory Hosts Viewer Local Test' role to exist")

        # Create a group and assign the role to create bindings
        group = Group.objects.create(name="Test Scope Detection", tenant=self.tenant, system=False)
        add_roles(group, [v1_role.uuid], self.tenant)

        detected_scopes = _determine_old_scopes(v1_role)

        # Should have at least one scope detected (the role should have bindings somewhere)
        self.assertGreater(
            len(detected_scopes),
            0,
            f"Should detect at least one scope for role with bindings, got {detected_scopes}",
        )

        # All detected scopes should be valid
        for scope in detected_scopes:
            self.assertIn(
                scope,
                [Scope.DEFAULT, Scope.TENANT, Scope.ROOT],
                f"All scopes should be valid, got {scope}",
            )

    @patch("management.role.definer._migrate_bindings_for_scope_change")
    def test_log_scope_change_and_migrate_does_nothing_when_scopes_equal(self, mock_migrate):
        """Test that no migration is triggered when old and new scopes are the same."""
        role = Role.objects.create(name="Test Role", tenant=self.public_tenant, system=True)
        _log_scope_change_and_migrate(role, "Test Role", {Scope.DEFAULT}, Scope.DEFAULT)
        mock_migrate.assert_not_called()

    @patch("management.role.definer._migrate_bindings_for_scope_change")
    def test_log_scope_change_and_migrate_does_nothing_when_old_scopes_empty(self, mock_migrate):
        """Test that no migration is triggered when old_scopes is empty (no existing bindings)."""
        role = Role.objects.create(name="Test Role", tenant=self.public_tenant, system=True)
        _log_scope_change_and_migrate(role, "Test Role", set(), Scope.DEFAULT)
        mock_migrate.assert_not_called()

    @patch("management.role.definer._migrate_bindings_for_scope_change")
    def test_log_scope_change_and_migrate_triggers_when_scopes_differ(self, mock_migrate):
        """Test that migration is triggered when scopes differ."""
        role = Role.objects.create(name="Test Role", tenant=self.public_tenant, system=True)
        _log_scope_change_and_migrate(role, "Test Role", {Scope.DEFAULT}, Scope.TENANT)
        mock_migrate.assert_called_once_with(role, {Scope.DEFAULT}, Scope.TENANT)
