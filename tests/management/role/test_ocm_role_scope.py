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
"""Tests for OCM V2 role seeding visibility and default-workspace restriction."""

from django.test import override_settings
from management.exceptions import InvalidFieldError
from management.models import Permission, Role
from management.role.model import Access, ExtRoleRelation, ExtTenant
from management.role.v2_role_scope import (
    is_ocm_v2_role,
    ocm_roles_allowed_for_workspace_binding,
)
from management.role.v2_model import RoleV2, SeededRoleV2
from management.role.v2_role_scope import v2_role_excluded_application_permission_ids_cache
from management.role.v2_service import RoleV2Service
from management.role_binding.service import RoleBindingService
from management.workspace.model import Workspace
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test


def create_ocm_seeded_role(name: str = "OCM Cluster Viewer", *, with_permission: bool = False) -> SeededRoleV2:
    """Create a seeded OCM external role backed by a V1 role and ExtRoleRelation."""
    from api.models import Tenant

    public_tenant = Tenant.objects.get(tenant_name="public")
    ocm_ext_tenant, _ = ExtTenant.objects.get_or_create(name="ocm")
    v1_role = Role.objects.create(name=name, system=True, tenant=public_tenant, description="OCM external role")
    ExtRoleRelation.objects.create(ext_id=f"{name.replace(' ', '')}Id", ext_tenant=ocm_ext_tenant, role=v1_role)
    v2_role, _ = SeededRoleV2.objects.update_or_create(
        uuid=v1_role.uuid,
        defaults={
            "name": name,
            "description": "OCM external role",
            "tenant": public_tenant,
            "v1_source": v1_role,
        },
    )
    if with_permission:
        ocm_perm = Permission.objects.create(permission="ocm:cluster:view", tenant=public_tenant)
        v2_role.permissions.set([ocm_perm])
        Access.objects.create(permission=ocm_perm, role=v1_role, tenant=public_tenant)
    else:
        v2_role.permissions.clear()
    return v2_role


@override_settings(ATOMIC_RETRY_DISABLED=True)
class OcmRoleScopeHelperTests(IdentityRequest):
    """Unit tests for OCM role scope helpers."""

    def test_is_ocm_v2_role_true_for_external_relation(self):
        role = create_ocm_seeded_role()
        self.assertTrue(is_ocm_v2_role(role))

    def test_is_ocm_v2_role_false_for_custom_role(self):
        role = RoleV2.objects.create(name="custom", description="", tenant=self.tenant)
        self.assertFalse(is_ocm_v2_role(role))

    def test_ocm_roles_allowed_only_for_default_workspace(self):
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        standard_ws = Workspace.objects.create(
            name="child ws",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=bootstrap_result.default_workspace,
        )

        self.assertTrue(
            ocm_roles_allowed_for_workspace_binding("workspace", None, self.tenant),
        )
        self.assertTrue(
            ocm_roles_allowed_for_workspace_binding(
                "workspace", str(bootstrap_result.default_workspace.id), self.tenant
            ),
        )
        self.assertFalse(
            ocm_roles_allowed_for_workspace_binding("workspace", str(standard_ws.id), self.tenant),
        )
        self.assertFalse(
            ocm_roles_allowed_for_workspace_binding("workspace", str(bootstrap_result.root_workspace.id), self.tenant),
        )
        self.assertFalse(
            ocm_roles_allowed_for_workspace_binding("tenant", self.tenant.tenant_resource_id(), self.tenant)
        )


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_MIGRATION_APP_EXCLUDE_LIST=[])
class OcmRoleV2ListTests(IdentityRequest):
    """V2 role list filtering for OCM external roles."""

    def setUp(self):
        super().setUp()
        v2_role_excluded_application_permission_ids_cache.invalidate()
        self.service = RoleV2Service(tenant=self.tenant)
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace
        self.standard_workspace = Workspace.objects.create(
            name="Standard WS",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )
        self.ocm_role = create_ocm_seeded_role()
        self.non_ocm_role = RoleV2.objects.create(name="inventory role", description="", tenant=self.tenant)
        inv_perm = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        self.non_ocm_role.permissions.add(inv_perm)

    def tearDown(self):
        v2_role_excluded_application_permission_ids_cache.invalidate()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        super().tearDown()

    def test_list_includes_ocm_role_for_default_workspace(self):
        names = set(
            self.service.list({"resource_type": "workspace", "resource_id": self.default_workspace.id}).values_list(
                "name", flat=True
            )
        )
        self.assertIn(self.ocm_role.name, names)

    def test_list_excludes_ocm_role_for_standard_workspace(self):
        names = set(
            self.service.list({"resource_type": "workspace", "resource_id": self.standard_workspace.id}).values_list(
                "name", flat=True
            )
        )
        self.assertNotIn(self.ocm_role.name, names)
        self.assertIn(self.non_ocm_role.name, names)

    def test_list_excludes_ocm_role_for_root_workspace(self):
        names = set(
            self.service.list({"resource_type": "workspace", "resource_id": self.root_workspace.id}).values_list(
                "name", flat=True
            )
        )
        self.assertNotIn(self.ocm_role.name, names)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["ocm"])
    def test_list_hides_ocm_role_with_excluded_application_permissions(self):
        v2_role_excluded_application_permission_ids_cache.invalidate()
        ocm_perm_role = create_ocm_seeded_role("OCM Cluster Editor", with_permission=True)
        names = set(self.service.list({}).values_list("name", flat=True))
        self.assertNotIn(ocm_perm_role.name, names)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=[])
    def test_list_shows_ocm_role_when_not_in_exclude_list(self):
        v2_role_excluded_application_permission_ids_cache.invalidate()
        ocm_perm_role = create_ocm_seeded_role("OCM Cluster Editor", with_permission=True)
        names = set(
            self.service.list({"resource_type": "workspace", "resource_id": self.default_workspace.id}).values_list(
                "name", flat=True
            )
        )
        self.assertIn(ocm_perm_role.name, names)


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_MIGRATION_APP_EXCLUDE_LIST=[])
class OcmRoleBindingValidationTests(IdentityRequest):
    """Role binding validation for OCM external roles."""

    def setUp(self):
        super().setUp()
        v2_role_excluded_application_permission_ids_cache.invalidate()
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace
        self.standard_workspace = Workspace.objects.create(
            name="Standard WS",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )
        self.ocm_role = create_ocm_seeded_role()
        self.service = RoleBindingService(tenant=self.tenant)
        self.group = self._create_group()

    def tearDown(self):
        v2_role_excluded_application_permission_ids_cache.invalidate()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        super().tearDown()

    def _create_group(self):
        from management.group.model import Group

        return Group.objects.create(name="test group", tenant=self.tenant)

    def test_allows_ocm_role_on_default_workspace(self):
        result = self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            subject_type="group",
            subject_id=str(self.group.uuid),
            role_ids=[str(self.ocm_role.uuid)],
        )
        self.assertEqual({r.uuid for r in result.roles}, {self.ocm_role.uuid})

    def test_rejects_ocm_role_on_standard_workspace(self):
        with self.assertRaises(InvalidFieldError) as ctx:
            self.service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.standard_workspace.id),
                subject_type="group",
                subject_id=str(self.group.uuid),
                role_ids=[str(self.ocm_role.uuid)],
            )
        self.assertIn("OCM Cluster Viewer", str(ctx.exception))

    def test_rejects_ocm_role_on_root_workspace(self):
        with self.assertRaises(InvalidFieldError) as ctx:
            self.service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.root_workspace.id),
                subject_type="group",
                subject_id=str(self.group.uuid),
                role_ids=[str(self.ocm_role.uuid)],
            )
        self.assertIn("OCM Cluster Viewer", str(ctx.exception))

    def test_rejects_ocm_role_on_tenant(self):
        with self.assertRaises(InvalidFieldError) as ctx:
            self.service.update_role_bindings_for_subject(
                resource_type="tenant",
                resource_id=self.tenant.tenant_resource_id(),
                subject_type="group",
                subject_id=str(self.group.uuid),
                role_ids=[str(self.ocm_role.uuid)],
            )
        self.assertIn("OCM Cluster Viewer", str(ctx.exception))

    def test_rejects_ocm_role_with_permissions_on_standard_workspace(self):
        """OCM-specific validation applies even when the role has workspace-granular permissions."""
        ocm_perm_role = create_ocm_seeded_role("OCM Cluster Provisioner", with_permission=True)
        with self.assertRaises(InvalidFieldError) as ctx:
            self.service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.standard_workspace.id),
                subject_type="group",
                subject_id=str(self.group.uuid),
                role_ids=[str(ocm_perm_role.uuid)],
            )
        self.assertIn("OCM roles can only be assigned at the Default Workspace", str(ctx.exception))
