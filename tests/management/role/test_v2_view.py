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
"""Test the RoleV2ViewSet."""

import uuid
from collections.abc import Iterable
from importlib import reload
from urllib.parse import urlencode
from unittest.mock import ANY, patch

from django.conf import settings
from django.test import override_settings
from django.urls import clear_url_caches, reverse
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.test import APIClient

from api.models import Tenant
from management import v2_urls
from management.audit_log.model import AuditLog
from management.models import Permission, Workspace
from management.permission.scope_service import ImplicitResourceService, PermissionScopeCache
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.role.definer import seed_roles
from management.role.v2_model import CustomRoleV2, PlatformRoleV2, RoleV2, SeededRoleV2
from management.role.v2_role_scope import v2_role_excluded_application_permission_ids_cache
from management.role.v2_service import RoleV2Service
from management.tenant_service import V2TenantBootstrapService
from management.utils import PRINCIPAL_CACHE, as_uuid
from rbac import urls
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test

CACHE_PATCH_TARGET = "management.role.v2_service.permission_scope_cache"


def _scope_cache(tenant_perms="", root_perms="", default_perms=""):
    """Build a PermissionScopeCache backed by a test ImplicitResourceService."""
    scope_service = ImplicitResourceService(
        tenant_scope_permissions=[p.strip() for p in tenant_perms.split(",") if p.strip()],
        root_scope_permissions=[p.strip() for p in root_perms.split(",") if p.strip()],
        default_scope_permissions=[p.strip() for p in default_perms.split(",") if p.strip()],
    )
    return PermissionScopeCache(scope_service)


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class RoleV2RetrieveViewTest(IdentityRequest):
    """Test the RoleV2ViewSet retrieve endpoint."""

    def setUp(self):
        """Set up test data."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        # Bootstrap tenant so V2 writes (create/update/destroy) can run ensure_v2_write_activated
        bootstrap_tenant_for_v2_test(self.tenant)
        self.client = APIClient()

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )

        self.mock_check_access = self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        # Create permissions
        self.permission1 = Permission.objects.create(
            permission="inventory:hosts:read",
            tenant=self.tenant,
        )
        self.permission2 = Permission.objects.create(
            permission="inventory:hosts:write",
            tenant=self.tenant,
        )
        self.permission3 = Permission.objects.create(
            permission="cost:reports:read",
            tenant=self.tenant,
        )

        # Create a custom role
        self.custom_role = CustomRoleV2.objects.create(
            name="Test Custom Role",
            description="A test custom role",
            tenant=self.tenant,
        )
        self.custom_role.permissions.add(self.permission1, self.permission2)

        # Create a platform role
        self.platform_role = PlatformRoleV2.objects.create(
            name="Test Platform Role",
            description="A test platform role",
            tenant=self.tenant,
        )
        self.platform_role.permissions.add(self.permission3)

    def tearDown(self):
        """Tear down test data."""
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def _get_role_url(self, role_uuid):
        """Get the role detail URL."""
        return reverse("v2_management:roles-detail", kwargs={"uuid": str(role_uuid)})

    def test_retrieve_custom_role_success(self):
        """Test retrieving a custom role with all fields."""
        url = self._get_role_url(self.custom_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Verify default retrieve fields are present (per API spec)
        self.assertEqual(data["id"], str(self.custom_role.uuid))
        self.assertEqual(data["name"], "Test Custom Role")
        self.assertEqual(data["description"], "A test custom role")
        self.assertIn("last_modified", data)
        self.assertIn("permissions", data)
        # org_id and permissions_count are not returned by default
        self.assertNotIn("org_id", data)
        self.assertNotIn("permissions_count", data)

        # Verify permissions
        self.assertEqual(len(data["permissions"]), 2)
        permission_strings = {f"{p['application']}:{p['resource_type']}:{p['operation']}" for p in data["permissions"]}
        self.assertEqual(permission_strings, {"inventory:hosts:read", "inventory:hosts:write"})

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost"])
    def test_retrieve_excluded_app_role_returns_404(self):
        """Test that retrieving a role whose permissions are all from an excluded app returns 404."""
        v2_role_excluded_application_permission_ids_cache.invalidate()
        try:
            excluded_role = CustomRoleV2.objects.create(
                name="Excluded App Role",
                description="Has only excluded-app permissions",
                tenant=self.tenant,
            )
            excluded_role.permissions.add(self.permission3)  # cost:reports:read

            url = self._get_role_url(excluded_role.uuid)
            response = self.client.get(url, **self.headers)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        finally:
            v2_role_excluded_application_permission_ids_cache.invalidate()

    def test_retrieve_platform_role_returns_404(self):
        """Test that retrieving a platform role returns 404 (platform roles are not exposed)."""
        url = self._get_role_url(self.platform_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_role_not_found(self):
        """Test retrieving a non-existent role."""
        non_existent_uuid = uuid.uuid4()
        url = self._get_role_url(non_existent_uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_role_invalid_uuid_format(self):
        """Test retrieving a role with invalid UUID format."""
        url = reverse("v2_management:roles-detail", kwargs={"uuid": "not-a-uuid"})
        response = self.client.get(url, **self.headers)

        # Should return 404 or 400 depending on URL routing
        self.assertIn(response.status_code, [status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST])

    def test_retrieve_role_from_different_tenant(self):
        """Test that users cannot retrieve roles from other tenants."""
        # Create a role for a different tenant
        from api.models import Tenant

        other_tenant = Tenant.objects.create(
            tenant_name="other_tenant", account_id="999999", org_id="999999", ready=True
        )

        other_role = CustomRoleV2.objects.create(
            name="Other Tenant Role",
            description="Role from another tenant",
            tenant=other_tenant,
        )

        url = self._get_role_url(other_role.uuid)
        response = self.client.get(url, **self.headers)

        # Should not be able to access role from different tenant
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Cleanup
        other_role.delete()
        other_tenant.delete()

    def test_retrieve_role_without_authentication(self):
        """Test retrieving a role without authentication headers."""
        url = self._get_role_url(self.custom_role.uuid)
        response = self.client.get(url)

        # Should fail without authentication
        self.assertIn(response.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_retrieve_role_permissions_structure(self):
        """Test that permissions are properly structured in the response."""
        url = self._get_role_url(self.custom_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Verify permission structure
        for permission in data["permissions"]:
            self.assertIn("application", permission)
            self.assertIn("resource_type", permission)
            self.assertIn("operation", permission)

            # Permission strings should be split correctly
            # inventory:hosts:read -> application=inventory, resource_type=hosts, operation=read
            if permission["application"] == "inventory":
                self.assertEqual(permission["resource_type"], "hosts")
                self.assertIn(permission["operation"], ["read", "write"])

    def test_retrieve_role_with_no_permissions(self):
        """Test retrieving a role that has no permissions assigned."""
        empty_role = CustomRoleV2.objects.create(
            name="Empty Role",
            description="Role with no permissions",
            tenant=self.tenant,
        )

        url = self._get_role_url(empty_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["permissions"], [])
        # permissions_count is not in default retrieve fields
        self.assertNotIn("permissions_count", data)

        empty_role.delete()

    def test_retrieve_role_last_modified_field(self):
        """Test that last_modified field is present and valid."""
        url = self._get_role_url(self.custom_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertIn("last_modified", data)
        # Verify it's a valid ISO 8601 datetime string
        from datetime import datetime

        try:
            datetime.fromisoformat(data["last_modified"].replace("Z", "+00:00"))
        except ValueError:
            self.fail("last_modified is not a valid ISO 8601 datetime")

    def test_retrieve_role_permissions_count_field(self):
        """Test that permissions_count field returns correct count when explicitly requested."""
        url = self._get_role_url(self.custom_role.uuid)
        # Explicitly request permissions_count field (not in default)
        response = self.client.get(f"{url}?fields=permissions_count", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["permissions_count"], 2)
        # Only permissions_count should be in response when requested alone
        self.assertEqual(set(data.keys()), {"permissions_count"})

    def test_retrieve_role_all_fields_present(self):
        """Test that default retrieve fields are present in the response (per API spec)."""
        url = self._get_role_url(self.custom_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Default retrieve fields per API spec
        expected_fields = {"id", "name", "description", "permissions", "last_modified"}
        actual_fields = set(data.keys())

        self.assertEqual(actual_fields, expected_fields)

    def test_retrieve_role_permissions_alphabetical_order(self):
        """Test that permissions are returned in alphabetical order."""
        # Create role with permissions in random order
        ordered_role = CustomRoleV2.objects.create(
            name="Ordered Permissions Role",
            description="Role to test alphabetical ordering",
            tenant=self.tenant,
        )
        # Add permissions in non-alphabetical order
        # Expected alphabetical order: cost:reports:read, inventory:hosts:read, inventory:hosts:write
        ordered_role.permissions.add(self.permission2)  # inventory:hosts:write
        ordered_role.permissions.add(self.permission3)  # cost:reports:read
        ordered_role.permissions.add(self.permission1)  # inventory:hosts:read

        url = self._get_role_url(ordered_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Verify permissions are sorted alphabetically
        permission_strings = [f"{p['application']}:{p['resource_type']}:{p['operation']}" for p in data["permissions"]]
        self.assertEqual(permission_strings, ["cost:reports:read", "inventory:hosts:read", "inventory:hosts:write"])

        ordered_role.delete()

    def test_retrieve_role_uses_queryset_not_service(self):
        """Test that retrieve uses DRF's get_object() from queryset."""
        url = self._get_role_url(self.custom_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Verify correct role was retrieved via queryset
        self.assertEqual(data["id"], str(self.custom_role.uuid))
        self.assertEqual(data["name"], "Test Custom Role")

    def test_retrieve_role_permission_denied(self):
        """Test retrieving a role when user lacks permission."""
        self.mock_check_access.return_value = False

        url = self._get_role_url(self.custom_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.mock_check_access.return_value = True

    def test_retrieve_role_with_special_characters_in_name(self):
        """Test retrieving a role with special characters in name and description."""
        special_role = CustomRoleV2.objects.create(
            name="Role with Special Chars: @#$%",
            description="Description with 'quotes' and \"double quotes\" and line\nbreaks",
            tenant=self.tenant,
        )
        special_role.permissions.add(self.permission1)

        url = self._get_role_url(special_role.uuid)
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["name"], "Role with Special Chars: @#$%")
        self.assertIn("quotes", data["description"])

        special_role.delete()

    def test_retrieve_role_performance_prefetch(self):
        """Test that queryset prefetches permissions to avoid N+1 queries."""
        # Create a role with many permissions
        many_permissions_role = CustomRoleV2.objects.create(
            name="Many Permissions Role",
            description="Role with many permissions",
            tenant=self.tenant,
        )

        # Add 10 permissions
        permissions = []
        for i in range(10):
            perm = Permission.objects.create(
                permission=f"app{i}:resource{i}:action{i}",
                tenant=self.tenant,
            )
            permissions.append(perm)
            many_permissions_role.permissions.add(perm)

        url = self._get_role_url(many_permissions_role.uuid)

        # Query count should be minimal due to queryset's prefetch_related
        # Expected queries: tenant lookup, role fetch with prefetch
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data["permissions"]), 10)

        # Cleanup
        many_permissions_role.delete()
        for perm in permissions:
            perm.delete()

    def test_create_and_retrieve_consistency(self):
        """Test that create and retrieve operations are consistent."""
        # Create a role with specific permission order
        create_data = {
            "name": "Consistency Test Role",
            "description": "Testing create/retrieve consistency",
            "permissions": [
                {"application": "inventory", "resource_type": "hosts", "operation": "write"},
                {"application": "cost", "resource_type": "reports", "operation": "read"},
                {"application": "inventory", "resource_type": "hosts", "operation": "read"},
            ],
        }

        # Create the role
        create_response = self.client.post(
            reverse("v2_management:roles-list"), create_data, format="json", **self.headers
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        role_id = create_response.data["id"]

        # Create response preserves input order
        create_permissions = [
            f"{p['application']}:{p['resource_type']}:{p['operation']}" for p in create_response.data["permissions"]
        ]
        self.assertEqual(create_permissions, ["inventory:hosts:write", "cost:reports:read", "inventory:hosts:read"])

        # Retrieve the same role
        retrieve_url = reverse("v2_management:roles-detail", kwargs={"uuid": role_id})
        retrieve_response = self.client.get(retrieve_url, **self.headers)
        self.assertEqual(retrieve_response.status_code, status.HTTP_200_OK)

        # Retrieve response returns alphabetical order
        retrieve_permissions = [
            f"{p['application']}:{p['resource_type']}:{p['operation']}" for p in retrieve_response.data["permissions"]
        ]
        self.assertEqual(retrieve_permissions, ["cost:reports:read", "inventory:hosts:read", "inventory:hosts:write"])

        # Both should have same permission set, just different order
        self.assertEqual(set(create_permissions), set(retrieve_permissions))


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class RoleV2ViewSetTests(IdentityRequest):
    """Test the RoleV2ViewSet."""

    def setUp(self):
        """Set up the RoleV2ViewSet tests."""
        # Reload URLs to pick up v2 management routes
        reload(v2_urls)
        reload(urls)
        clear_url_caches()

        super().setUp()
        # Bootstrap tenant so V2 writes (create/update/destroy) can run ensure_v2_write_activated
        bootstrap_tenant_for_v2_test(self.tenant)
        self.client = APIClient()
        self.client.credentials(HTTP_X_RH_IDENTITY=self.headers.get("HTTP_X_RH_IDENTITY"))

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        # URL for roles endpoint
        self.url = reverse("v2_management:roles-list")
        self.list_url = f"{self.url}?resource_type=workspace"
        self.delete_url = reverse("v2_management:roles-bulk-destroy")

        # Create test permissions
        self.permission1 = Permission.objects.create(permission="test:resource:read", tenant=self.tenant)
        self.permission2 = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        self.permission3 = Permission.objects.create(permission="inventory:hosts:write", tenant=self.tenant)
        self.permission4 = Permission.objects.create(permission="cost:reports:read", tenant=self.tenant)

        self.permission1_data = {"application": "inventory", "resource_type": "hosts", "operation": "read"}

        # Create a role for list tests
        self.role = RoleV2.objects.create(name="test_role", description="Test description", tenant=self.tenant)
        self.role.permissions.add(self.permission1)

    def _assert_audit_log(self, action: str, description: str):
        audit_logs = self.client.get("/api/rbac/v1/auditlogs/").data["data"]

        self.assertIn(
            {
                "action": action,
                "description": description,
                "resource_type": AuditLog.ROLE_V2,
                "principal_username": self.user_data["username"],
            },
            [
                {k: log[k] for k in ["action", "description", "resource_type", "principal_username"]}
                for log in audit_logs
            ],
        )

    def tearDown(self):
        """Tear down RoleV2ViewSet tests."""
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()

        # Clear principal cache to avoid test isolation issues
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)
        super().tearDown()

    # ==========================================================================
    # Tests for GET /api/v2/roles/ (list)
    # ==========================================================================

    def test_list_roles_returns_paginated_response(self):
        """Test that list endpoint returns paginated response."""
        response = self.client.get(self.list_url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("meta", response.data)
        self.assertIn("links", response.data)
        self.assertIn("data", response.data)

        self.assertIn("limit", response.data["meta"])
        self.assertIn("next", response.data["links"])
        self.assertIn("previous", response.data["links"])
        self.assertIsInstance(response.data["data"], list)

    def test_list_roles_returns_default_fields(self):
        """Test that roles return default fields when no fields parameter is provided."""
        response = self.client.get(self.list_url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        role_data = response.data["data"][0]
        expected_fields = {"id", "name", "description", "last_modified"}
        self.assertEqual(set(role_data.keys()), expected_fields)

        self.assertEqual(role_data["name"], "test_role")
        self.assertEqual(role_data["description"], "Test description")
        self.assertNotIn("org_id", role_data)
        self.assertNotIn("permissions", role_data)
        self.assertNotIn("permissions_count", role_data)

    def test_list_roles_excludes_platform_roles(self):
        """Test that platform roles are excluded from list responses."""
        RoleV2.objects.create(
            name="platform_role",
            description="Platform description",
            type=RoleV2.Types.PLATFORM,
            tenant=self.tenant,
        )

        response = self.client.get(self.list_url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["name"], "test_role")

    def test_list_roles_with_custom_fields(self):
        """Test that fields parameter returns only requested fields."""
        url = f"{self.list_url}&fields=id,name,permissions_count"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        role_data = response.data["data"][0]
        self.assertEqual(set(role_data.keys()), {"id", "name", "permissions_count"})
        self.assertEqual(role_data["name"], "test_role")
        self.assertEqual(role_data["permissions_count"], 1)

    def test_list_roles_with_name_filter(self):
        """Test that name filter returns only matching roles."""
        RoleV2.objects.create(name="other_role", description="Other", tenant=self.tenant)

        response = self.client.get(self.list_url, **self.headers)
        self.assertEqual(len(response.data["data"]), 2)

        url = f"{self.list_url}&name=test_role"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["name"], "test_role")

    def test_list_roles_with_wildcard_name_filter(self):
        """Test that name=test* returns roles starting with 'test'."""
        RoleV2.objects.create(name="other_role", description="Other", tenant=self.tenant)

        url = f"{self.url}?name=test*"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["name"], "test_role")

    def test_list_roles_wildcard_no_match(self):
        """Test that a wildcard pattern matching nothing returns empty list."""
        url = f"{self.url}?name=zzz*"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])

    def test_list_roles_with_order_by_name(self):
        """Test that order_by parameter returns roles sorted by name."""
        RoleV2.objects.create(name="other_role", description="Other", tenant=self.tenant)
        RoleV2.objects.create(name="first_role", description="First", tenant=self.tenant)

        url = f"{self.list_url}&order_by=name"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 3)
        self.assertEqual(response.data["data"][0]["name"], "first_role")
        self.assertEqual(response.data["data"][1]["name"], "other_role")
        self.assertEqual(response.data["data"][2]["name"], "test_role")
        # order by descending name
        url = f"{self.list_url}&order_by=-name"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 3)
        self.assertEqual(response.data["data"][0]["name"], "test_role")
        self.assertEqual(response.data["data"][1]["name"], "other_role")
        self.assertEqual(response.data["data"][2]["name"], "first_role")

    def test_list_roles_with_order_by_last_modified(self):
        """Test that order_by parameter returns roles sorted by last_modified."""
        # last_modified field is added automatically by the model
        RoleV2.objects.create(name="first_role", description="First", tenant=self.tenant)
        RoleV2.objects.create(name="other_role", description="Other", tenant=self.tenant)

        # Ascending order
        url = f"{self.list_url}&order_by=last_modified"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 3)

        data = response.data["data"]
        # Ensure we got the expected roles, regardless of order
        self.assertEqual(
            set(role["name"] for role in data),
            {"test_role", "first_role", "other_role"},
        )

        # Assert that last_modified is sorted ascending
        last_modified_values = [parse_datetime(role["last_modified"]) for role in data]
        self.assertEqual(last_modified_values, sorted(last_modified_values))

        # Descending order
        url = f"{self.list_url}&order_by=-last_modified"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 3)

        data = response.data["data"]
        self.assertEqual(
            set(role["name"] for role in data),
            {"test_role", "first_role", "other_role"},
        )

        last_modified_values = [parse_datetime(role["last_modified"]) for role in data]
        self.assertEqual(last_modified_values, sorted(last_modified_values, reverse=True))

    def test_list_roles_with_name_filter_and_order_by(self):
        """Test that name filter and order_by can be combined."""
        RoleV2.objects.create(name="test_role_alpha", description="Alpha", tenant=self.tenant)
        RoleV2.objects.create(name="test_role_beta", description="Beta", tenant=self.tenant)
        RoleV2.objects.create(name="other_role", description="Other", tenant=self.tenant)

        # Filter by substring "test_role" and order by last_modified descending
        url = f"{self.list_url}&name=test_role&order_by=-last_modified"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Substring "test_role" matches test_role, test_role_alpha, test_role_beta
        self.assertEqual(len(response.data["data"]), 3)
        names = [r["name"] for r in response.data["data"]]
        self.assertCountEqual(names, ["test_role", "test_role_alpha", "test_role_beta"])

    def test_list_roles_with_invalid_order_by(self):
        """Test that invalid order_by field returns 400 error."""
        url = f"{self.list_url}&order_by=foobar"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid ordering field", str(response.data["detail"]))

    def test_list_roles_with_invalid_order_by_permissions_count(self):
        """Test invalid but real field in ?order_by= returns 400 error."""
        url = f"{self.list_url}&order_by=permissions_count"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid ordering field", str(response.data["detail"]))

    def test_list_roles_with_limit_parameter(self):
        """Test that limit parameter restricts the number of returned roles."""
        # Create additional roles
        RoleV2.objects.create(name="role_2", description="Second", tenant=self.tenant)
        RoleV2.objects.create(name="role_3", description="Third", tenant=self.tenant)

        url = f"{self.list_url}&limit=2"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)
        self.assertEqual(response.data["meta"]["limit"], 2)

    def test_list_roles_meta_contains_correct_limit(self):
        """Test that meta.limit reflects the requested limit value."""
        url = f"{self.list_url}&limit=5"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["limit"], 5)

    def test_list_roles_pagination_generates_next_link(self):
        """Test that links.next is non-null when there are more roles than limit."""
        # Create more roles than the limit
        for i in range(5):
            RoleV2.objects.create(name=f"extra_role_{i}", description=f"Extra {i}", tenant=self.tenant)

        url = f"{self.list_url}&limit=2"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)
        self.assertIsNotNone(response.data["links"]["next"])

    def test_list_roles_pagination_previous_link_null_on_first_page(self):
        """Test that links.previous is null on the first page."""
        response = self.client.get(self.list_url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["links"]["previous"])

    def test_list_roles_with_limit_minus_one_returns_all(self):
        """Test that limit=-1 returns all roles without pagination."""
        # Create additional roles
        for i in range(15):
            RoleV2.objects.create(name=f"role_{i}", description=f"Role {i}", tenant=self.tenant)

        url = f"{self.list_url}&limit=-1"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return all roles (16 total: 1 from setUp + 15 created)
        self.assertEqual(len(response.data["data"]), 16)
        # Meta limit should equal the total count
        self.assertEqual(response.data["meta"]["limit"], 16)
        # No pagination links when returning all results
        self.assertIsNone(response.data["links"]["next"])
        self.assertIsNone(response.data["links"]["previous"])

    def test_list_roles_normal_limits_still_work_after_limit_minus_one_added(self):
        """Test that various normal limit values still work correctly."""
        # Create additional roles for testing
        for i in range(20):
            RoleV2.objects.create(name=f"role_{i}", description=f"Role {i}", tenant=self.tenant)

        # Test limit=5
        url = f"{self.list_url}&limit=5"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 5)
        self.assertEqual(response.data["meta"]["limit"], 5)
        # Should have next link since there are 21 total roles
        self.assertIsNotNone(response.data["links"]["next"])

        # Test limit=10
        url = f"{self.list_url}&limit=10"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 10)
        self.assertEqual(response.data["meta"]["limit"], 10)
        self.assertIsNotNone(response.data["links"]["next"])

        # Test limit=15
        url = f"{self.list_url}&limit=15"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 15)
        self.assertEqual(response.data["meta"]["limit"], 15)
        self.assertIsNotNone(response.data["links"]["next"])

    def test_list_roles_with_empty_name_filter(self):
        """Test that empty name filter returns all roles."""
        RoleV2.objects.create(name="other_role", description="Other", tenant=self.tenant)

        url = f"{self.list_url}&name="
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)

    def test_list_roles_with_whitespace_name_filter(self):
        """Test that whitespace-only name filter is treated as empty."""
        RoleV2.objects.create(name="other_role", description="Other", tenant=self.tenant)

        url = f"{self.list_url}&name=%20"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)

    def test_list_roles_name_filter_is_case_insensitive(self):
        """Test that name filter is case insensitive."""
        RoleV2.objects.create(name="Test_Role", description="Uppercase", tenant=self.tenant)

        # Should match both "Test_Role" and "test_role" (from setUp)
        url = f"{self.list_url}&name=Test_Role"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)

        returned_names = {role["name"] for role in response.data["data"]}
        self.assertEqual(returned_names, {"Test_Role", "test_role"})

    def test_list_roles_with_permission_filter(self):
        """Test that permission filter returns only roles containing the specified permission."""
        role_with_perm = RoleV2.objects.create(name="host_reader", tenant=self.tenant)
        role_with_perm.permissions.add(self.permission2)

        role_without_perm = RoleV2.objects.create(name="no_match_role", tenant=self.tenant)
        role_without_perm.permissions.add(self.permission4)

        url = f"{self.list_url}&permission=inventory:hosts:read"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        role_names = [r["name"] for r in response.data["data"]]
        self.assertIn("host_reader", role_names)
        self.assertNotIn("no_match_role", role_names)

    def test_list_roles_with_permission_filter_multiple(self):
        """Test that comma-separated permissions return union of matches."""
        role_a = RoleV2.objects.create(name="host_reader", tenant=self.tenant)
        role_a.permissions.add(self.permission2)

        role_b = RoleV2.objects.create(name="cost_reader", tenant=self.tenant)
        role_b.permissions.add(self.permission4)

        url = f"{self.list_url}&permission=inventory:hosts:read,cost:reports:read"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        role_names = [r["name"] for r in response.data["data"]]
        self.assertIn("host_reader", role_names)
        self.assertIn("cost_reader", role_names)

    def test_list_roles_with_permission_filter_no_match(self):
        """Test that a non-existent permission returns empty results."""
        url = f"{self.list_url}&permission=nonexistent:perm:here"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 0)

    def test_list_roles_with_permission_filter_distinct(self):
        """Test that a role appears once even when it matches multiple comma-separated permissions."""
        role = RoleV2.objects.create(name="multi_perm_role", tenant=self.tenant)
        role.permissions.add(self.permission2, self.permission3)

        url = f"{self.list_url}&permission=inventory:hosts:read,inventory:hosts:write"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        role_names = [r["name"] for r in response.data["data"]]
        self.assertEqual(role_names.count("multi_perm_role"), 1)

    def test_list_roles_with_permissions_field(self):
        """Test that requesting permissions field returns permissions array."""
        url = f"{self.list_url}&fields=id,name,permissions"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        role_data = response.data["data"][0]
        self.assertEqual(set(role_data.keys()), {"id", "name", "permissions"})
        self.assertIsInstance(role_data["permissions"], list)
        self.assertEqual(len(role_data["permissions"]), 1)

        perm = role_data["permissions"][0]
        self.assertEqual(perm["application"], "test")
        self.assertEqual(perm["resource_type"], "resource")
        self.assertEqual(perm["operation"], "read")

    def test_list_roles_with_all_available_fields(self):
        """Test that all available fields can be requested, including opt-in org_id."""
        url = f"{self.list_url}&fields=id,name,description,permissions_count,permissions,last_modified,org_id"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        role_data = response.data["data"][0]

        expected = {"id", "name", "description", "permissions_count", "permissions", "last_modified", "org_id"}
        self.assertEqual(set(role_data.keys()), expected)
        self.assertEqual(role_data["org_id"], str(self.tenant.org_id))

    def test_list_roles_with_only_id_field(self):
        """Test minimal field request returns only id."""
        url = f"{self.list_url}&fields=id"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        role_data = response.data["data"][0]
        self.assertEqual(set(role_data.keys()), {"id"})

    def test_list_roles_with_invalid_fields_raises_validation_error(self):
        """Test that requesting only invalid fields raises a validation error."""
        url = f"{self.list_url}&fields=invalid_field,another_invalid"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unknown field: 'invalid_field'", str(response.data["detail"]))
        self.assertIn("Unknown field: 'another_invalid'", str(response.data["detail"]))

    def test_list_roles_returns_empty_list_when_no_roles(self):
        """Test that empty list is returned when no roles exist for tenant."""
        # Delete the role created in setUp
        RoleV2.objects.filter(tenant=self.tenant).delete()

        response = self.client.get(self.list_url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])
        self.assertIn("meta", response.data)
        self.assertIn("links", response.data)

    def test_list_roles_with_no_matching_name(self):
        """Test that empty list is returned when name filter matches nothing."""
        url = f"{self.list_url}&name=nonexistent_role"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])

    # ==========================================================================
    # Tests for GET /api/v2/roles/?resource_type=... (list with resource_type)
    # ==========================================================================

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*", root_perms="root_app:*:*"))
    def test_list_roles_filter_by_resource_type_tenant(self):
        """Test that resource_type=tenant returns only tenant-scoped roles."""
        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant", tenant=self.tenant)
        tenant_role.permissions.add(tenant_perm)

        url = f"{self.url}?resource_type=tenant"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in response.data["data"]}
        self.assertIn("tenant_role", names)
        self.assertNotIn("test_role", names)

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*", root_perms="root_app:*:*"))
    def test_list_roles_filter_by_resource_type_workspace(self):
        """Test that resource_type=workspace (no resource_id) returns only DEFAULT-scoped roles."""
        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant", tenant=self.tenant)
        tenant_role.permissions.add(tenant_perm)

        url = f"{self.url}?resource_type=workspace"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in response.data["data"]}
        self.assertIn("test_role", names)
        self.assertNotIn("tenant_role", names)

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*"))
    def test_list_roles_filter_excludes_mixed_scope_from_workspace(self):
        """A role with both default and tenant permissions should not appear for resource_type=workspace."""
        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        mixed_role = RoleV2.objects.create(name="mixed_role", description="Mixed", tenant=self.tenant)
        mixed_role.permissions.add(self.permission1, tenant_perm)

        url = f"{self.url}?resource_type=workspace"
        response = self.client.get(url, **self.headers)

        names = {r["name"] for r in response.data["data"]}
        self.assertNotIn("mixed_role", names)

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*", root_perms="root_app:*:*"))
    def test_list_roles_resource_type_workspace_root_resource_id_only_root_scoped(self):
        """resource_id=root workspace UUID lists only roles whose highest scope is ROOT."""
        root_perm = Permission.objects.create(permission="root_app:res:read", tenant=self.tenant)
        def_perm = Permission.objects.create(permission="def_only:res:read", tenant=self.tenant)
        root_only = RoleV2.objects.create(name="root_only", description="R", tenant=self.tenant)
        root_only.permissions.add(root_perm)
        def_only = RoleV2.objects.create(name="def_only", description="D", tenant=self.tenant)
        def_only.permissions.add(def_perm)

        root = Workspace.objects.root(tenant=self.tenant)
        url = f"{self.url}?resource_type=workspace&resource_id={root.id}"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in response.data["data"]}
        self.assertIn("root_only", names)
        self.assertNotIn("def_only", names)
        self.assertNotIn("test_role", names)

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*", root_perms="root_app:*:*"))
    def test_list_roles_resource_type_workspace_default_resource_id_only_default_scoped(self):
        """resource_id=default (or any non-root) workspace lists only DEFAULT-scoped roles."""
        root_perm = Permission.objects.create(permission="root_app:res:read", tenant=self.tenant)
        def_perm = Permission.objects.create(permission="def_for_cmp:res:read", tenant=self.tenant)
        root_r = RoleV2.objects.create(name="root_scoped", description="R2", tenant=self.tenant)
        root_r.permissions.add(root_perm)
        def_r = RoleV2.objects.create(name="def_scoped", description="D2", tenant=self.tenant)
        def_r.permissions.add(def_perm)

        default_ws = Workspace.objects.default(tenant=self.tenant)
        url = f"{self.url}?resource_type=workspace&resource_id={default_ws.id}"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in response.data["data"]}
        self.assertIn("def_scoped", names)
        self.assertIn("test_role", names)
        self.assertNotIn("root_scoped", names)

    def test_list_roles_resource_id_invalid_uuid_returns_400_for_workspace(self):
        """Invalid resource_id is rejected for workspace before listing."""
        url = f"{self.url}?resource_type=workspace&resource_id=not-a-uuid"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*"))
    def test_list_roles_tenant_resource_id_accepted(self):
        """Tenant resource_id (domain/org_id) is accepted for resource_type=tenant."""
        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant", tenant=self.tenant)
        tenant_role.permissions.add(tenant_perm)

        tenant_resource_id = self.tenant.tenant_resource_id()
        url = f"{self.url}?{urlencode({'resource_type': 'tenant', 'resource_id': tenant_resource_id})}"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in response.data["data"]}
        self.assertIn("tenant_role", names)

    @patch(
        CACHE_PATCH_TARGET,
        _scope_cache(tenant_perms="tenant_app:*:*", default_perms="inventory:hosts:read"),
    )
    def test_list_roles_resource_type_tenant_includes_custom_tenant_scoped_role(self):
        """resource_type=tenant returns custom roles whose highest scope is tenant."""
        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        custom_tenant_role = CustomRoleV2.objects.create(
            name="custom_tenant_role",
            description="Custom tenant scoped",
            tenant=self.tenant,
        )
        custom_tenant_role.permissions.add(tenant_perm)

        custom_workspace_role = CustomRoleV2.objects.create(
            name="custom_workspace_role",
            description="Custom workspace scoped",
            tenant=self.tenant,
        )
        custom_workspace_role.permissions.add(self.permission2)

        url = f"{self.url}?resource_type=tenant"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in response.data["data"]}
        self.assertIn("custom_tenant_role", names)
        self.assertNotIn("custom_workspace_role", names)

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*"))
    def test_list_roles_resource_tenant_org_id_accepted(self):
        """resource.tenant.org_id resolves to tenant resource_id like role bindings."""
        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant", tenant=self.tenant)
        tenant_role.permissions.add(tenant_perm)

        url = f"{self.url}?resource.tenant.org_id={self.tenant.org_id}"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in response.data["data"]}
        self.assertIn("tenant_role", names)

    @patch(CACHE_PATCH_TARGET, _scope_cache())
    def test_list_roles_resource_id_unknown_workspace_returns_empty(self):
        """resource_id with no matching workspace for the tenant returns an empty list."""
        unknown = uuid.uuid4()
        url = f"{self.url}?resource_type=workspace&resource_id={unknown}"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*", root_perms="root_app:*:*"))
    def test_list_roles_without_resource_type_returns_all_scopes(self):
        """Test that omitting resource_type returns roles from all scopes."""
        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant", tenant=self.tenant)
        tenant_role.permissions.add(tenant_perm)

        response = self.client.get(self.url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {r["name"] for r in response.data["data"]}
        self.assertIn("test_role", names)
        self.assertIn("tenant_role", names)

    def test_list_roles_resource_id_without_resource_type_returns_400(self):
        """Test that providing resource_id without resource_type returns 400."""
        url = f"{self.url}?resource_id=00000000-0000-0000-0000-0000000000aa"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_roles_resource_id_with_resource_type_accepted(self):
        """Test that providing a valid resource_id with resource_type is accepted."""
        root = Workspace.objects.root(tenant=self.tenant)
        url = f"{self.url}?resource_type=workspace&resource_id={root.id}"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch(CACHE_PATCH_TARGET, _scope_cache())
    def test_list_roles_unknown_resource_type_returns_empty(self):
        """Test that an unrecognized resource_type returns an empty list."""
        url = f"{self.url}?resource_type=unknown_type"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])

    @patch(CACHE_PATCH_TARGET, _scope_cache(tenant_perms="tenant_app:*:*"))
    def test_list_roles_resource_type_combined_with_name(self):
        """Test that resource_type and name filters can be combined."""
        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant", tenant=self.tenant)
        tenant_role.permissions.add(tenant_perm)

        url = f"{self.url}?resource_type=tenant&name=tenant_role"
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["name"], "tenant_role")

    # ==========================================================================
    # Tests for seeded roles visibility (public tenant)
    # ==========================================================================

    def test_list_roles_includes_seeded_roles_from_public_tenant(self):
        """Test that seeded roles from the public tenant are included in list responses.

        Seeded roles belong to the public tenant but should be visible to all tenants.
        This is the same pattern as v1 roles.
        """
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        SeededRoleV2.objects.create(name="Seeded Role 1", description="A seeded role", tenant=public_tenant)
        SeededRoleV2.objects.create(name="Seeded Role 2", description="Another seeded role", tenant=public_tenant)

        response = self.client.get(self.url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_names = {r["name"] for r in response.data["data"]}
        self.assertIn("Seeded Role 1", returned_names)
        self.assertIn("Seeded Role 2", returned_names)

    def test_list_roles_excludes_platform_roles_from_public_tenant(self):
        """Test that platform roles from public tenant are still excluded."""
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        SeededRoleV2.objects.create(name="Visible Seeded", description="Should appear", tenant=public_tenant)
        RoleV2.objects.create(
            name="Hidden Platform", description="Should not appear", type=RoleV2.Types.PLATFORM, tenant=public_tenant
        )

        response = self.client.get(self.url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_names = {r["name"] for r in response.data["data"]}
        self.assertIn("Visible Seeded", returned_names)
        self.assertNotIn("Hidden Platform", returned_names)

    def test_retrieve_seeded_role_from_public_tenant(self):
        """Test that a seeded role from the public tenant can be retrieved by UUID."""
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        seeded_role = SeededRoleV2.objects.create(
            name="Retrievable Seeded", description="Should be retrievable", tenant=public_tenant
        )

        detail_url = reverse("v2_management:roles-detail", kwargs={"uuid": seeded_role.uuid})
        response = self.client.get(detail_url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Retrievable Seeded")

    # ==========================================================================
    # Tests for POST /api/v2/roles/ (create)
    # ==========================================================================

    @patch("management.permissions.v2_edit_api_access.FEATURE_FLAGS.is_v2_edit_api_enabled", return_value=False)
    def test_create_role_blocked_when_feature_flag_disabled(self, mock_is_v2_edit_enabled):
        """Test that V2 role create returns 403 when workspaces feature flag is disabled for the org."""
        data = {
            "name": "Blocked Role",
            "description": "Should be blocked",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        response = self.client.post(self.url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("workspaces", str(response.data).lower())
        mock_is_v2_edit_enabled.assert_called_once_with(self.customer_data["org_id"])

    @override_settings(NOTIFICATIONS_ENABLED=True)
    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_create_role_success(self, send_kafka_message):
        """Test creating a role via API returns 201"""
        data = {
            "name": "API Test Role",
            "description": "Created via API",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "API Test Role")
        self.assertIn("id", response.data)

        # Verify permissions are returned in response
        self.assertEqual(
            response.data["permissions"],
            [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        )

        self._assert_audit_log(action=AuditLog.CREATE, description=f"Created V2 role: {data['name']}")

        send_kafka_message.assert_called_with(
            settings.NOTIFICATIONS_TOPIC,
            {
                "bundle": "console",
                "application": "rbac",
                "event_type": "custom-v2-role-created",
                "timestamp": ANY,
                "events": [
                    {
                        "metadata": {},
                        "payload": {
                            "name": data["name"],
                            "username": self.user_data["username"],
                            "uuid": response.data["id"],
                        },
                    }
                ],
                "org_id": self.tenant.org_id,
            },
            ANY,
        )

    def test_create_role_multiple_permissions(self):
        """Test creating a role with multiple permissions returns all permissions."""
        data = {
            "name": "Multi Permission API Role",
            "description": "Has multiple permissions",
            "permissions": [
                {"application": "inventory", "resource_type": "hosts", "operation": "read"},
                {"application": "inventory", "resource_type": "hosts", "operation": "write"},
            ],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify permissions match (order-independent)
        self.assertCountEqual(
            response.data["permissions"],
            [
                {"application": "inventory", "resource_type": "hosts", "operation": "read"},
                {"application": "inventory", "resource_type": "hosts", "operation": "write"},
            ],
        )

    def test_create_role_preserves_permission_order(self):
        """Test that response permissions are returned in input order."""
        # Request permissions in specific order (cost first, then inventory)
        data = {
            "name": "Order Test Role",
            "description": "Testing permission order preservation",
            "permissions": [
                {"application": "cost", "resource_type": "reports", "operation": "read"},
                {"application": "inventory", "resource_type": "hosts", "operation": "write"},
                {"application": "inventory", "resource_type": "hosts", "operation": "read"},
            ],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify permissions are returned in the same order as input
        self.assertEqual(
            response.data["permissions"],
            [
                {"application": "cost", "resource_type": "reports", "operation": "read"},
                {"application": "inventory", "resource_type": "hosts", "operation": "write"},
                {"application": "inventory", "resource_type": "hosts", "operation": "read"},
            ],
        )

    def test_create_role_missing_name_returns_400(self):
        """Test that missing name returns 400."""
        data = {
            "description": "No name",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)
        self.assertIn("errors", response.data)
        self.assertTrue(any(e.get("field") == "name" for e in response.data["errors"]))

    def test_create_role_missing_permissions_returns_400(self):
        """Test that missing permissions returns 400."""
        data = {
            "name": "No Permissions Role",
            "description": "Missing permissions",
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)
        self.assertIn("errors", response.data)
        self.assertTrue(any(e.get("field") == "permissions" for e in response.data["errors"]))

    def test_create_role_empty_permissions_returns_400(self):
        """Test that empty permissions array returns 400."""
        data = {
            "name": "Empty Permissions Role",
            "description": "Has empty permissions",
            "permissions": [],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)

    def test_create_role_invalid_permission_returns_400(self):
        """Test that non-existent permission returns 400."""
        data = {
            "name": "Invalid Permission Role",
            "description": "Has invalid permission",
            "permissions": [{"application": "nonexistent", "resource_type": "resource", "operation": "action"}],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)

    def test_create_role_duplicate_name_returns_400(self):
        """Test that duplicate role name returns 400."""
        # Create first role
        CustomRoleV2.objects.create(
            name="Duplicate API Role",
            description="First role",
            tenant=self.tenant,
        )

        # Try to create via API
        data = {
            "name": "Duplicate API Role",
            "description": "Second role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)
        self.assertIn("title", response.data)
        self.assertIn("detail", response.data)
        self.assertEqual(response.data["status"], 400)
        self.assertIn("already exists", response.data["detail"])

    def test_create_role_with_seeded_role_name_returns_400(self):
        """Test that creating a custom role with the same name as a seeded role is rejected."""
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        SeededRoleV2.objects.create(name="Vulnerability viewer", description="Seeded", tenant=public_tenant)

        data = {
            "name": "vulnerability viewer",
            "description": "Custom role with seeded name",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.post(self.url, data, format="json")

        # ProblemDetails response: verify HTTP status, structure, and detail message
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)
        self.assertIn("detail", response.data)
        self.assertEqual(response.data["status"], 400)
        self.assertIn("conflicts with an existing system role", response.data["detail"])

    def test_update_role_to_seeded_role_name_returns_400(self):
        """Test that updating a custom role to a seeded role name is rejected."""
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        SeededRoleV2.objects.create(name="Patch viewer", description="Seeded", tenant=public_tenant)

        role = CustomRoleV2.objects.create(name="My Custom Role", description="Custom", tenant=self.tenant)
        role.permissions.add(self.permission2)

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "PATCH VIEWER",
            "description": "Trying to rename to seeded name",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        # ProblemDetails response: verify HTTP status, structure, and detail message
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)
        self.assertIn("detail", response.data)
        self.assertEqual(response.data["status"], 400)
        self.assertIn("conflicts with an existing system role", response.data["detail"])

    def test_create_role_missing_permissions_returns_problem_details(self):
        """Test that missing permissions returns ProblemDetails format."""
        data = {
            "name": "No Permissions Role",
            "description": "Missing permissions",
            "permissions": [],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)
        self.assertIn("title", response.data)
        self.assertIn("detail", response.data)
        self.assertEqual(response.data["status"], 400)

    def test_create_role_invalid_permission_returns_problem_details(self):
        """Test that invalid permission returns ProblemDetails format."""
        data = {
            "name": "Invalid Permission Role",
            "description": "Invalid permission",
            "permissions": [{"application": "nonexistent", "resource_type": "foo", "operation": "bar"}],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)
        self.assertIn("title", response.data)
        self.assertIn("detail", response.data)
        self.assertEqual(response.data["status"], 400)
        self.assertIn("do not exist", response.data["detail"])

    def test_create_role_empty_body_returns_all_required_field_errors(self):
        """Test that empty request body returns errors array with all required fields."""
        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)

        error_fields = {e.get("field") for e in response.data["errors"]}
        self.assertIn("name", error_fields)
        self.assertIn("permissions", error_fields)
        self.assertNotIn("description", error_fields)

    def test_create_role_without_description_succeeds(self):
        """Test that creating a role without description returns 201 with empty description."""
        data = {
            "name": "No Description Role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "No Description Role")
        self.assertEqual(response.data["description"], "")
        self.assertIn("id", response.data)

    def test_create_role_returns_response_format(self):
        """Test that create returns proper response format with all fields."""
        data = {
            "name": "Response Format Role",
            "description": "Testing response format",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("id", response.data)
        self.assertIn("name", response.data)
        self.assertIn("description", response.data)
        self.assertIn("permissions", response.data)
        self.assertIn("last_modified", response.data)

    def test_create_role_with_fields_parameter(self):
        """Test that create respects fields query parameter for response."""
        data = {
            "name": "Fields Test Role",
            "description": "Testing fields parameter",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        url = f"{self.url}?fields=id,name,permissions_count"
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(set(response.data.keys()), {"id", "name", "permissions_count"})
        self.assertEqual(response.data["name"], "Fields Test Role")
        self.assertEqual(response.data["permissions_count"], 1)
        self.assertNotIn("description", response.data)
        self.assertNotIn("permissions", response.data)

    def test_create_role_with_invalid_fields_parameter(self):
        """Test that create returns 400 for invalid fields parameter."""
        data = {
            "name": "Invalid Fields Role",
            "description": "Testing invalid fields",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        url = f"{self.url}?fields=id,nonexistent_field"
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        self.assertIn("nonexistent_field", str(response.data))
        self.assertEqual(response.data["status"], 400)
        self.assertEqual(response.data["errors"][0]["field"], "fields")

    def test_create_role_with_asterisk_in_name_returns_400(self):
        """Test that creating a role with '*' in the name returns 400."""
        data = {
            "name": "role_*_admin",
            "description": "Should be rejected",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.post(self.url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Role name must not contain asterisks (*).")
        self.assertIn("errors", response.data)
        self.assertGreater(len(response.data["errors"]), 0)
        self.assertEqual(response.data["errors"][0]["field"], "name")

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost"])
    def test_create_role_rejects_migration_excluded_application(self):
        """Permissions in V2_MIGRATION_APP_EXCLUDE_LIST cannot be used on create."""
        v2_role_excluded_application_permission_ids_cache.invalidate()
        try:
            data = {
                "name": "Excluded App API Role",
                "description": "Should fail",
                "permissions": [{"application": "cost", "resource_type": "reports", "operation": "read"}],
            }
            response = self.client.post(self.url, data, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("migration-excluded", response.data["detail"])
        finally:
            v2_role_excluded_application_permission_ids_cache.invalidate()

    # ==========================================================================
    # Tests for PUT /api/v2/roles/{uuid}/ (update)
    # ==========================================================================

    @override_settings(NOTIFICATIONS_ENABLED=True)
    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_update_role_success(self, send_kafka_message):
        """Test updating a role via API returns 200."""
        role = CustomRoleV2.objects.create(
            name="Original Role",
            description="Original description",
            tenant=self.tenant,
        )
        role.permissions.add(self.permission2)

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Updated Role",
            "description": "Updated description",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "write"}],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Updated Role")
        self.assertEqual(response.data["description"], "Updated description")
        self.assertEqual(response.data["id"], str(role.uuid))

        # Verify permissions are updated in response
        self.assertEqual(
            response.data["permissions"],
            [{"application": "inventory", "resource_type": "hosts", "operation": "write"}],
        )

        self._assert_audit_log(
            action=AuditLog.EDIT,
            description=f"V2 role {role.name}:\nEdited name\nEdited description\nEdited permissions",
        )

        send_kafka_message.assert_called_with(
            settings.NOTIFICATIONS_TOPIC,
            {
                "bundle": "console",
                "application": "rbac",
                "event_type": "custom-v2-role-updated",
                "timestamp": ANY,
                "events": [
                    {
                        "metadata": {},
                        "payload": {
                            "name": data["name"],
                            "username": self.user_data["username"],
                            "uuid": response.data["id"],
                        },
                    }
                ],
                "org_id": self.tenant.org_id,
            },
            ANY,
        )

    def test_update_role_changes_permissions(self):
        """Test that updating a role replaces all permissions."""
        role = CustomRoleV2.objects.create(
            name="Test Role",
            description="Test description",
            tenant=self.tenant,
        )
        role.permissions.add(self.permission2)

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Test Role",
            "description": "Test description",
            "permissions": [
                {"application": "inventory", "resource_type": "hosts", "operation": "write"},
                {"application": "cost", "resource_type": "reports", "operation": "read"},
            ],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["permissions"]), 2)

        # Verify role in database has updated permissions
        role.refresh_from_db()
        self.assertEqual(role.permissions.count(), 2)
        self.assertIn(self.permission3, role.permissions.all())
        self.assertIn(self.permission4, role.permissions.all())
        self.assertNotIn(self.permission2, role.permissions.all())

    def test_update_role_preserves_permission_order(self):
        """Test that update response permissions are returned in input order."""
        role = CustomRoleV2.objects.create(
            name="Order Test Role",
            description="Testing order",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Order Test Role",
            "description": "Testing permission order preservation",
            "permissions": [
                {"application": "cost", "resource_type": "reports", "operation": "read"},
                {"application": "inventory", "resource_type": "hosts", "operation": "write"},
                {"application": "inventory", "resource_type": "hosts", "operation": "read"},
            ],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify permissions are returned in the same order as input
        self.assertEqual(
            response.data["permissions"],
            [
                {"application": "cost", "resource_type": "reports", "operation": "read"},
                {"application": "inventory", "resource_type": "hosts", "operation": "write"},
                {"application": "inventory", "resource_type": "hosts", "operation": "read"},
            ],
        )

    def test_update_role_nonexistent_returns_404(self):
        """Test that updating a nonexistent role returns 404 with Problem RFC format."""
        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": "550e8400-e29b-41d4-a716-446655440000"})
        data = {
            "name": "Updated Role",
            "description": "Updated description",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        self.assertEqual(response.data["status"], 404)
        self.assertIn("title", response.data)
        self.assertIn("detail", response.data)

    def test_update_role_duplicate_name_returns_400(self):
        """Test that updating a role to duplicate name returns 400 with Problem RFC format."""
        role1 = CustomRoleV2.objects.create(
            name="Role One",
            description="First role",
            tenant=self.tenant,
        )

        role2 = CustomRoleV2.objects.create(
            name="Role Two",
            description="Second role",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role2.uuid)})
        data = {
            "name": "Role One",
            "description": "Second role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        self.assertEqual(response.data["status"], 400)
        self.assertEqual(response.data["title"], "The request payload contains invalid syntax.")
        self.assertIn("detail", response.data)
        self.assertEqual(len(response.data["errors"]), 1)
        self.assertEqual(response.data["errors"][0]["field"], "name")
        self.assertIn("Role One", response.data["errors"][0]["message"])

    def test_update_role_empty_permissions_returns_400(self):
        """Test that updating a role with empty permissions returns 400 with Problem RFC format."""
        role = CustomRoleV2.objects.create(
            name="Test Role",
            description="Test description",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Test Role",
            "description": "Test description",
            "permissions": [],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        self.assertEqual(response.data["status"], 400)
        self.assertEqual(response.data["title"], "The request payload contains invalid syntax.")
        self.assertIn("detail", response.data)
        self.assertEqual(len(response.data["errors"]), 1)
        self.assertEqual(response.data["errors"][0]["field"], "permissions")
        self.assertEqual(response.data["errors"][0]["message"], "permissions is required")

    def test_update_role_missing_description_succeeds(self):
        """Test that updating a role without description succeeds with empty description."""
        role = CustomRoleV2.objects.create(
            name="Test Role",
            description="Test description",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Test Role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Test Role")
        self.assertEqual(response.data["description"], "")

        self._assert_audit_log(
            action=AuditLog.EDIT,
            description=f"V2 role {role.name}:\nEdited description\nEdited permissions",
        )

    def test_update_role_returns_response_format(self):
        """Test that update returns proper response format with all fields."""
        role = CustomRoleV2.objects.create(
            name="Response Format Role",
            description="Original description",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Updated Response Format Role",
            "description": "Updated description",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("id", response.data)
        self.assertIn("name", response.data)
        self.assertIn("description", response.data)
        self.assertIn("permissions", response.data)
        self.assertIn("last_modified", response.data)

    def test_update_role_with_fields_parameter(self):
        """Test that update respects fields query parameter for response."""
        role = CustomRoleV2.objects.create(
            name="Fields Test Role",
            description="Original description",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        url = f"{update_url}?fields=id,name,permissions_count"
        data = {
            "name": "Updated Fields Test Role",
            "description": "Updated description",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data.keys()), {"id", "name", "permissions_count"})
        self.assertEqual(response.data["name"], "Updated Fields Test Role")
        self.assertEqual(response.data["permissions_count"], 1)
        self.assertNotIn("description", response.data)
        self.assertNotIn("permissions", response.data)

    def test_update_role_default_fields_includes_permissions(self):
        """Test that update returns permissions by default (per API spec)."""
        role = CustomRoleV2.objects.create(
            name="Default Fields Role",
            description="Test description",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Default Fields Role",
            "description": "Test description",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Default should include permissions per spec
        self.assertIn("permissions", response.data)
        self.assertEqual(len(response.data["permissions"]), 1)

    def test_update_role_with_asterisk_in_name_returns_400(self):
        """Test that updating a role name to include '*' returns 400."""
        role = CustomRoleV2.objects.create(
            name="Original Role For Star Test",
            description="Original description",
            tenant=self.tenant,
        )
        role.permissions.add(self.permission1)

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Updated*Role",
            "description": "Should be rejected",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Role name must not contain asterisks (*).")
        self.assertIn("errors", response.data)
        self.assertGreater(len(response.data["errors"]), 0)
        self.assertEqual(response.data["errors"][0]["field"], "name")

    def test_update_platform_role_returns_404(self):
        """Test that attempting to update a platform role returns 404 with Problem RFC format."""
        # Create a platform role
        platform_role = PlatformRoleV2.objects.create(
            name="Test Platform Role",
            description="A platform role",
            tenant=self.tenant,
        )
        platform_role.permissions.add(self.permission1)

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(platform_role.uuid)})
        data = {
            "name": "Attempt to Update Platform Role",
            "description": "This should fail",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        # Platform roles are filtered out by assignable() so they're invisible (404)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        self.assertEqual(response.data["status"], 404)
        self.assertIn("title", response.data)
        self.assertIn("detail", response.data)

    def test_update_seeded_role_returns_400(self):
        """Test that attempting to update a seeded role returns 400, not 404."""
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        seeded_role = SeededRoleV2.objects.create(
            name="Seeded Role For Update Test",
            description="A seeded role",
            tenant=public_tenant,
        )
        seeded_role.permissions.add(self.permission1)

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(seeded_role.uuid)})
        data = {
            "name": "Attempt to Update Seeded Role",
            "description": "This should fail with 400",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        self.assertEqual(response.data["status"], 400)
        self.assertIn("System roles may not be updated", str(response.data))

    # --- Problem RFC format on errors ---

    def test_update_role_error_responses_use_problem_format(self):
        """Test that update role error responses use full Problem RFC 9457 shape."""
        role = CustomRoleV2.objects.create(
            name="Test Role",
            description="Test description",
            tenant=self.tenant,
        )
        role.permissions.add(self.permission1)

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
        data = {
            "name": "Updated Role",
            "description": "Updated description",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        error_cases = [
            ("invalid_field_name", f"{update_url}?fields=permission", "fields"),
            ("multiple_invalid_fields", f"{update_url}?fields=permission,invalid_field", "fields"),
            ("mixed_valid_invalid_fields", f"{update_url}?fields=name,permission,invalid", "fields"),
        ]

        for label, request_url, expected_field in error_cases:
            with self.subTest(label=label):
                response = self.client.put(request_url, data, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, f"Failed for {label}")
                self.assertEqual(response["Content-Type"], "application/problem+json")
                self.assertEqual(response.data["status"], 400)
                self.assertEqual(response.data["title"], "The request payload contains invalid syntax.")
                self.assertIn("detail", response.data)
                self.assertEqual(len(response.data["errors"]), 1)
                self.assertEqual(response.data["errors"][0]["field"], expected_field)
                self.assertIn("message", response.data["errors"][0])

    def test_update_description_noop_audit_log(self):
        """Test that name and description are not included in audit log if not modified."""
        role = CustomRoleV2.objects.create(
            name="Description Role",
            description="",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})

        # We omit the description here, which should be treated as an empty string.
        data = {
            "name": "Description Role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(action=AuditLog.EDIT, description=f"V2 role {role.name}:\nEdited permissions")

    def test_update_permissions_audit_log(self):
        """Test that permissions are not included in audit log if not modified."""
        role = CustomRoleV2.objects.create(
            name="Permissions Role",
            description="",
            tenant=self.tenant,
        )

        update_url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})

        # We omit the description here, which should be treated as an empty string.
        data = {
            "name": "A Better Role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        response = self.client.put(update_url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(
            action=AuditLog.EDIT, description=f"V2 role {role.name}:\nEdited name\nEdited permissions"
        )

        data["name"] = "An Even Better Role"

        response = self.client.put(update_url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(action=AuditLog.EDIT, description=f"V2 role A Better Role:\nEdited name")

    # ==========================================================================
    # Tests for POST /api/v2/roles:bulkDelete/ (bulk destroy)
    # ==========================================================================

    def _create_role(self) -> dict:
        response = self.client.post(
            self.url,
            {
                "name": f"Test Role {str(uuid.uuid4())}",
                "description": "A role for testing",
                "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.data

    def _request_delete(self, data: dict):
        return self.client.post(self.delete_url, data, format="json")

    def _assert_delete_not_found(self, response, uuids: Iterable[str | uuid.UUID]):
        uuids = {as_uuid(u) for u in uuids}

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        data = response.data

        self.assertIn("status", data)
        self.assertEqual(response.status_code, data["status"])

        self.assertIn("title", data)
        self.assertEqual(data["title"], "Not found.")

        self.assertIn("detail", data)

        for u in uuids:
            self.assertIn(str(u), data["detail"])

        self.assertIn("errors", data)
        self.assertEqual(data["errors"], [{"message": data["detail"], "field": "ids"}])

    @override_settings(NOTIFICATIONS_ENABLED=True)
    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_delete(self, send_kafka_message):
        create_response = self._create_role()
        response = self._request_delete({"ids": [create_response["id"]]})

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(RoleV2.objects.filter(uuid=create_response["id"]).exists())

        self._assert_audit_log(action=AuditLog.DELETE, description=f"Deleted V2 role: {create_response['name']}")

        send_kafka_message.assert_called_with(
            settings.NOTIFICATIONS_TOPIC,
            {
                "bundle": "console",
                "application": "rbac",
                "event_type": "custom-v2-role-deleted",
                "timestamp": ANY,
                "events": [
                    {
                        "metadata": {},
                        "payload": {
                            "name": create_response["name"],
                            "username": self.user_data["username"],
                            "uuid": create_response["id"],
                        },
                    }
                ],
                "org_id": self.tenant.org_id,
            },
            ANY,
        )

    def test_delete_empty(self):
        """Test that deleting with an empty ids list returns 400."""
        response = self._request_delete({"ids": []})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["errors"][0]["field"], "ids")
        self.assertEqual(response.data["errors"][0]["message"], "Ensure this field has at least 1 elements.")

    def test_delete_nonexistent_role(self):
        """Test that deleting a nonexistent role fails with status 404."""
        create_response = self._create_role()
        fake_role_id = str(uuid.uuid4())

        response = self._request_delete({"ids": [create_response["id"], fake_role_id]})

        self._assert_delete_not_found(response, [fake_role_id])
        self.assertNotIn(create_response["id"], response.data["detail"])  # Existing role should not be in the error.

        # The existing role should not have been deleted.
        self.assertTrue(RoleV2.objects.filter(uuid=create_response["id"]).exists())

    def test_delete_outside_tenant(self):
        """Test that deleting a role outside the user's tenant fails with status 404."""
        tenant2 = V2TenantBootstrapService(OutboxReplicator()).new_bootstrapped_tenant("t2").tenant
        role = RoleV2Service().create("test role", "test role", [self.permission1_data], tenant2)

        response = self._request_delete({"ids": [str(role.uuid)]})

        self._assert_delete_not_found(response, [role.uuid])
        self.assertTrue(RoleV2.objects.filter(pk=role.pk).exists())

    def test_delete_seeded(self):
        """Test that deleting a seeded role fails with status 400, not 404."""
        seed_roles()

        seeded_role = SeededRoleV2.objects.first()
        self.assertIsNotNone(seeded_role)

        response = self._request_delete({"ids": [str(seeded_role.uuid)]})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("System roles may not be deleted", str(response.data))
        self.assertTrue(RoleV2.objects.filter(pk=seeded_role.pk).exists())

    def test_delete_mix_of_custom_and_seeded_returns_400(self):
        """Test that bulk-deleting a mix of custom + seeded roles returns 400 and deletes nothing."""
        seed_roles()
        seeded_role = SeededRoleV2.objects.first()
        self.assertIsNotNone(seeded_role)

        custom_role = self._create_role()

        response = self._request_delete({"ids": [custom_role["id"], str(seeded_role.uuid)]})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("System roles may not be deleted", str(response.data))
        self.assertTrue(RoleV2.objects.filter(uuid=custom_role["id"]).exists())
        self.assertTrue(RoleV2.objects.filter(pk=seeded_role.pk).exists())

    def test_delete_missing_ids(self):
        """Test that a delete request with no IDs fails with status 400."""
        response = self._request_delete({})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertIn("title", response.data)
        self.assertEqual(response.data["title"], "The request payload contains invalid syntax.")

        self.assertIn("detail", response.data)
        self.assertEqual(response.data["detail"], "This field is required.")

        self.assertIn("errors", response.data)
        self.assertEqual(response.data["errors"], [{"message": "This field is required.", "field": "ids"}])

    def test_delete_non_array_ids(self):
        """Test that a delete request with a non-array ids field fails with status 400."""
        response = self._request_delete({"ids": 42})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertIn("title", response.data)
        self.assertEqual(response.data["title"], "The request payload contains invalid syntax.")

        self.assertIn("detail", response.data)
        self.assertEqual(response.data["detail"], 'Expected a list of items but got type "int".')

        self.assertIn("errors", response.data)
        self.assertEqual(
            response.data["errors"], [{"message": 'Expected a list of items but got type "int".', "field": "ids"}]
        )

    def test_delete_invalid_id(self):
        """Test that a delete request with an invalid ID fails with status 400."""
        for invalid_id in [42, "d5111b1de8104822b54ba5cb590dceb6", "not a UUID"]:
            with self.subTest(id=invalid_id):
                response = self._request_delete({"ids": [invalid_id]})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

                self.assertIn("title", response.data)
                self.assertEqual(response.data["title"], "The request payload contains invalid syntax.")

                self.assertIn("detail", response.data)
                self.assertEqual(response.data["detail"], "Must be a valid UUID.")

                self.assertIn("errors", response.data)
                self.assertEqual(response.data["errors"], [{"message": "Must be a valid UUID.", "field": "ids.0"}])


_SENTINEL = RuntimeError("_atomic_action sentinel")
_ATOMIC_ACTION_PATH = "management.v2_mixins.AtomicOperationsMixin._atomic_action"


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class RoleV2ViewSetAtomicWiringTests(IdentityRequest):
    """Verify RoleV2ViewSet write endpoints delegate to _atomic_action."""

    def setUp(self):
        reload(v2_urls)
        reload(urls)
        clear_url_caches()
        super().setUp()
        bootstrap_tenant_for_v2_test(self.tenant)
        self.client = APIClient()
        self.client.credentials(HTTP_X_RH_IDENTITY=self.headers.get("HTTP_X_RH_IDENTITY"))

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        self.role = RoleV2.objects.create(name="wiring_role", tenant=self.tenant)

    def tearDown(self):
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    @patch(_ATOMIC_ACTION_PATH, side_effect=_SENTINEL)
    def test_create_delegates_to_atomic_action(self, _mock):
        url = reverse("v2_management:roles-list")
        with self.assertRaises(RuntimeError, msg="_atomic_action sentinel"):
            self.client.post(
                url,
                {
                    "name": "x",
                    "permissions": [{"application": "app", "resource_type": "resource", "operation": "read"}],
                },
                format="json",
                **self.headers,
            )
        _mock.assert_called_once()

    @patch(_ATOMIC_ACTION_PATH, side_effect=_SENTINEL)
    def test_update_delegates_to_atomic_action(self, _mock):
        url = reverse("v2_management:roles-detail", kwargs={"uuid": str(self.role.uuid)})
        with self.assertRaises(RuntimeError, msg="_atomic_action sentinel"):
            self.client.put(
                url,
                {
                    "name": "x",
                    "permissions": [{"application": "app", "resource_type": "resource", "operation": "read"}],
                },
                format="json",
                **self.headers,
            )
        _mock.assert_called_once()

    @patch(_ATOMIC_ACTION_PATH, side_effect=_SENTINEL)
    def test_bulk_destroy_delegates_to_atomic_action(self, _mock):
        url = reverse("v2_management:roles-bulk-destroy")
        with self.assertRaises(RuntimeError, msg="_atomic_action sentinel"):
            self.client.post(
                url,
                {"ids": [str(self.role.uuid)]},
                format="json",
                **self.headers,
            )
        _mock.assert_called_once()
