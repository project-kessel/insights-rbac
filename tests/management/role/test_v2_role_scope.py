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
"""Tests for v2 role list filtering when applications are migration-excluded."""

from django.test import override_settings
from management.exceptions import InvalidFieldError
from management.models import Permission
from management.role.v2_exceptions import InvalidRolePermissionsError
from management.role.v2_model import RoleV2
from management.role.v2_role_scope import (
    v2_role_excluded_application_permission_ids_cache,
    v2_role_excluded_applications,
)
from management.role.v2_service import RoleV2Service
from management.role_binding.service import RoleBindingService
from tests.identity_request import IdentityRequest


@override_settings(ATOMIC_RETRY_DISABLED=True)
class V2RoleScopeTests(IdentityRequest):
    """V2 role list filtering by permission application."""

    def setUp(self):
        super().setUp()
        self.service = RoleV2Service(tenant=self.tenant)
        v2_role_excluded_application_permission_ids_cache.invalidate()

        self.inv_perm = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        self.cost_perm = Permission.objects.create(permission="cost-management:cost:read", tenant=self.tenant)

        self.inventory_role = RoleV2.objects.create(name="inv_role", description="", tenant=self.tenant)
        self.inventory_role.permissions.add(self.inv_perm)

        self.cost_role = RoleV2.objects.create(name="cost_role", description="", tenant=self.tenant)
        self.cost_role.permissions.add(self.cost_perm)

        self.mixed_role = RoleV2.objects.create(name="mixed_role", description="", tenant=self.tenant)
        self.mixed_role.permissions.add(self.inv_perm, self.cost_perm)

    def tearDown(self):
        v2_role_excluded_application_permission_ids_cache.invalidate()
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def test_v2_role_excluded_applications_uses_migration_exclude_list(self):
        with override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["foo", "bar", "baz", ""]):
            apps = v2_role_excluded_applications()
        self.assertEqual(apps, frozenset({"foo", "bar", "baz"}))

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    def test_permission_ids_cache_matches_table(self):
        ids = v2_role_excluded_application_permission_ids_cache.permission_ids()
        expected = frozenset(Permission.objects.filter(application="cost-management").values_list("id", flat=True))
        self.assertEqual(ids, expected)
        self.assertIn(self.cost_perm.id, ids)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    def test_cache_invalidate_picks_up_new_permission(self):
        v2_role_excluded_application_permission_ids_cache.permission_ids()
        extra = Permission.objects.create(permission="cost-management:foo:read", tenant=self.tenant)
        v2_role_excluded_application_permission_ids_cache.invalidate()
        ids = v2_role_excluded_application_permission_ids_cache.permission_ids()
        self.assertIn(extra.id, ids)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    def test_list_hides_roles_with_any_excluded_application(self):
        qs = self.service.list({})
        names = set(qs.values_list("name", flat=True))
        self.assertEqual(names, {"inv_role"})

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["inventory"])
    def test_list_hides_roles_matching_migration_exclude_list(self):
        qs = self.service.list({})
        names = set(qs.values_list("name", flat=True))
        self.assertEqual(names, {"cost_role"})

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    def test_role_binding_rejects_excluded_app_role(self):
        """Assigning an out-of-scope role to a binding raises InvalidFieldError."""
        service = RoleBindingService(tenant=self.tenant)
        with self.assertRaises(InvalidFieldError):
            service._get_roles([str(self.cost_role.uuid)])

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    def test_role_binding_accepts_in_scope_role(self):
        """Assigning an in-scope role to a binding succeeds."""
        service = RoleBindingService(tenant=self.tenant)
        roles = service._get_roles([str(self.inventory_role.uuid)])
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0].uuid, self.inventory_role.uuid)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    def test_create_custom_role_rejects_excluded_application(self):
        """Creating a V2 role with only migration-excluded permissions fails."""
        with self.assertRaises(InvalidRolePermissionsError):
            self.service.create(
                name="excluded_only",
                description="",
                permission_data=[
                    {"application": "cost-management", "resource_type": "cost", "operation": "read"},
                ],
                tenant=self.tenant,
            )

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    def test_create_custom_role_rejects_mixed_if_any_excluded(self):
        """Creating a V2 role fails if any permission uses a migration-excluded application."""
        with self.assertRaises(InvalidRolePermissionsError):
            self.service.create(
                name="mixed_bad",
                description="",
                permission_data=[
                    {"application": "inventory", "resource_type": "hosts", "operation": "read"},
                    {"application": "cost-management", "resource_type": "cost", "operation": "read"},
                ],
                tenant=self.tenant,
            )

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    def test_update_custom_role_rejects_excluded_application(self):
        """Updating a V2 role to add migration-excluded permissions fails."""
        role = self.service.create(
            name="then_update",
            description="",
            permission_data=[{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
            tenant=self.tenant,
        )
        with self.assertRaises(InvalidRolePermissionsError):
            self.service.update(
                role_uuid=str(role.uuid),
                name="then_update",
                description="",
                permission_data=[
                    {"application": "cost-management", "resource_type": "cost", "operation": "read"},
                ],
                tenant=self.tenant,
            )
