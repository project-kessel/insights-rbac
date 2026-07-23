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

from django.test import override_settings
from management.group.definer import add_roles, seed_group
from management.group.model import Group
from management.models import Permission
from management.permission.scope_service import Scope
from management.role.definer import (
    seed_roles,
)
from management.role.model import Role
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    all_of,
    relation,
    resource,
    subject,
)
from tests.management.role.test_dual_write import DualWriteTestCase

from api.models import Tenant


@override_settings(
    ATOMIC_RETRY_DISABLED=True, REPLICATION_TO_RELATION_ENABLED=True, AUTOMATIC_SCOPE_MIGRATION_ENABLED=True
)
class SystemRoleBindingScopeUpdateTests(DualWriteTestCase):
    """Test that system role bindings are updated when role scope changes during seeding."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.public_tenant = Tenant.objects.get(tenant_name="public")

        # We are testing scope migration, which only considers groups when they are in a V1 tenant, so ensure that we
        # are testing with a V1 tenant.
        self.assertIsNone(self.tenant.tenant_mapping.v2_write_activated_at)

    def _load_test_role(self) -> Role:
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

        return test_role

    def test_scope_state(self):
        with self.settings(
            ROOT_SCOPE_PERMISSIONS="",
            TENANT_SCOPE_PERMISSIONS="",
        ):
            seed_roles()

        test_role = self._load_test_role()

        self.assertEqual(test_role.scope_state.version, 0)
        self.assertCountEqual(test_role.scope_state.computed_scopes, [Scope.DEFAULT])
        self.assertEqual(test_role.scope_state.migrated, True)

        with self.settings(
            ROOT_SCOPE_PERMISSIONS="",
            TENANT_SCOPE_PERMISSIONS="inventory:*:*",
        ):
            seed_roles()

        test_role.refresh_from_db()

        self.assertEqual(test_role.scope_state.version, 1)
        self.assertCountEqual(test_role.scope_state.computed_scopes, [Scope.TENANT])
        self.assertEqual(test_role.scope_state.migrated, True)

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

        test_role = self._load_test_role()

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
