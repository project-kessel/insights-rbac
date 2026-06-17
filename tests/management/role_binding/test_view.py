#
# Copyright 2025 Red Hat, Inc.
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
"""Test the RoleBindingViewSet."""

import base64
import json
import uuid
from datetime import timedelta
from importlib import reload
from unittest import skip
from unittest.mock import ANY, patch
from urllib.parse import parse_qs, urlparse

from django.test import TestCase
from django.test.utils import override_settings
from django.urls import clear_url_caches, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.test import APIClient

from api.models import Tenant
from management.audit_log.model import AuditLog
from management.group.definer import seed_group
from management.group.platform import GlobalPolicyIdService
from management.models import Group, Permission, Principal, Workspace
from management.permission.scope_service import Scope
from management.role.definer import seed_roles
from management.role.platform import platform_v2_role_uuid_for
from management.role.v2_model import PlatformRoleV2, RoleV2, SeededRoleV2
from management.role.v2_service import RoleV2Service
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from management.role_binding.service import RoleBindingService, API_PRINCIPAL_SOURCE
from management.subject import SubjectType
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from management.tenant_service.v2 import V2TenantBootstrapService
from migration_tool.in_memory_tuples import InMemoryRelationReplicator
from rbac import urls
from tests.identity_request import IdentityRequest, TransactionalIdentityRequest


def _coerce_api_datetime(value):
    """Normalize JSON/datetime values from API responses for comparisons."""
    if isinstance(value, str):
        return parse_datetime(value)
    return value


def _create_seeded_role_binding(tenant, workspace, permission, role_name, group_name):
    """Create a seeded role from the public tenant with a binding and group in the given tenant."""
    public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
    seeded_role = SeededRoleV2.objects.create(name=role_name, description=role_name, tenant=public_tenant)
    seeded_role.permissions.add(permission)
    seeded_binding = RoleBinding.objects.create(
        role=seeded_role, resource_type="workspace", resource_id=str(workspace.id), tenant=tenant
    )
    seeded_group = Group.objects.create(name=group_name, description=group_name, tenant=tenant)
    RoleBindingGroup.objects.create(group=seeded_group, binding=seeded_binding)
    return seeded_role, seeded_binding, seeded_group


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingListViewSetTest(IdentityRequest):
    """Test the RoleBindingViewSet list endpoint."""

    def setUp(self):
        """Set up test data."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.client = APIClient()

        # Create workspace hierarchy (root -> default -> standard)
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=self.root_workspace,
        )
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            description="Test workspace description",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        # Create permission and roles
        self.permission = Permission.objects.create(
            permission="app:resource:read",
            tenant=self.tenant,
        )

        # Create multiple roles and bindings for testing
        self.roles = []
        self.bindings = []
        self.groups = []

        for i in range(15):
            role = RoleV2.objects.create(
                name=f"list_test_role_{i:02d}",  # Zero-padded for consistent ordering
                tenant=self.tenant,
            )
            role.permissions.add(self.permission)
            self.roles.append(role)

            group = Group.objects.create(
                name=f"list_test_group_{i}",
                description=f"List test group {i} description",
                tenant=self.tenant,
            )
            self.groups.append(group)

            binding = RoleBinding.objects.create(
                role=role,
                resource_type="workspace",
                resource_id=str(self.workspace.id),
                tenant=self.tenant,
            )
            self.bindings.append(binding)

            RoleBindingGroup.objects.create(
                group=group,
                binding=binding,
            )

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.filter(binding__tenant=self.tenant).delete()
        RoleBinding.objects.filter(tenant=self.tenant).delete()
        for group in self.groups:
            group.principals.clear()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        super().tearDown()

    def _get_list_url(self):
        """Get the list URL."""
        return reverse("v2_management:role-bindings-list")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_returns_paginated_response(self, mock_permission):
        """Test that list returns a paginated response structure."""
        url = self._get_list_url()
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("meta", response.data)
        self.assertIn("links", response.data)
        self.assertIn("data", response.data)
        self.assertIn("limit", response.data["meta"])
        self.assertIn("next", response.data["links"])
        self.assertIn("previous", response.data["links"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_default_limit(self, mock_permission):
        """Test that default limit is 10."""
        url = self._get_list_url()
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["limit"], 10)
        self.assertEqual(len(response.data["data"]), 10)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_custom_limit(self, mock_permission):
        """Test that custom limit is respected."""
        url = self._get_list_url()
        response = self.client.get(f"{url}?limit=5", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["limit"], 5)
        self.assertEqual(len(response.data["data"]), 5)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_cursor_pagination(self, mock_permission):
        """Test that cursor pagination works correctly."""
        url = self._get_list_url()

        # Get first page
        response1 = self.client.get(f"{url}?limit=5", **self.headers)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        page1_role_ids = [item["role"]["id"] for item in response1.data["data"]]

        # Get next page using cursor
        next_link = response1.data["links"]["next"]
        self.assertIsNotNone(next_link)

        parsed = urlparse(next_link)
        cursor = parse_qs(parsed.query).get("cursor", [None])[0]
        self.assertIsNotNone(cursor)

        response2 = self.client.get(f"{url}?limit=5&cursor={cursor}", **self.headers)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        page2_role_ids = [item["role"]["id"] for item in response2.data["data"]]

        # Pages should have different role bindings
        self.assertEqual(len(set(page1_role_ids) & set(page2_role_ids)), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_empty_results(self, mock_permission):
        """Test that empty results return valid structure."""
        # Delete all bindings for this tenant
        RoleBindingGroup.objects.filter(binding__tenant=self.tenant).delete()
        RoleBinding.objects.filter(tenant=self.tenant).delete()

        url = self._get_list_url()
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])
        self.assertIsNone(response.data["links"]["next"])
        self.assertIsNone(response.data["links"]["previous"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_first_page_has_no_previous(self, mock_permission):
        """Test that first page has no previous link."""
        url = self._get_list_url()
        response = self.client.get(f"{url}?limit=5", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["links"]["previous"])
        self.assertIsNotNone(response.data["links"]["next"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_last_page_has_no_next(self, mock_permission):
        """Test that last page has no next link."""
        url = self._get_list_url()
        response = self.client.get(f"{url}?limit=100", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["links"]["next"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_data_structure(self, mock_permission):
        """Test that response data matches expected structure.

        Default behavior returns:
        - role: id only
        - subject: id and type
        - resource: id only
        - sources: list with id only
        """
        url = self._get_list_url()
        response = self.client.get(f"{url}?limit=1", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        item = response.data["data"][0]

        # Verify structure has role, subject, resource, sources (no last_modified, no roles)
        self.assertIn("role", item)
        self.assertIn("subject", item)
        self.assertIn("resource", item)
        self.assertIn("sources", item)
        self.assertNotIn("last_modified", item)
        self.assertNotIn("roles", item)

        # Verify role structure - only id by default
        role = item["role"]
        self.assertIn("id", role)

        # Verify subject structure - id and type
        subject = item["subject"]
        self.assertIn("id", subject)
        self.assertIn("type", subject)
        self.assertEqual(subject["type"], "group")

        # Verify resource structure - only id by default
        resource = item["resource"]
        self.assertIn("id", resource)

        # Verify sources structure - list with id only by default
        sources = item["sources"]
        self.assertIsInstance(sources, list)
        self.assertEqual(len(sources), 1)
        self.assertIn("id", sources[0])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_role_id(self, mock_permission):
        """Test filtering by role_id."""
        target_role = self.roles[0]
        url = self._get_list_url()
        response = self.client.get(f"{url}?role_id={target_role.uuid}", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        # Verify the returned binding has the correct role
        item = response.data["data"][0]
        self.assertEqual(str(item["role"]["id"]), str(target_role.uuid))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_role_id_no_match(self, mock_permission):
        """Test filtering by non-existent role_id returns empty results."""
        non_existent_uuid = "00000000-0000-0000-0000-000000000000"
        url = self._get_list_url()
        response = self.client.get(f"{url}?role_id={non_existent_uuid}", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_invalid_role_id(self, mock_permission):
        """Test filtering by invalid role_id returns validation error."""
        url = self._get_list_url()
        response = self.client.get(f"{url}?role_id=not-a-uuid", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_tenant_isolation(self, mock_permission):
        """Test that list only returns bindings for the current tenant."""
        # Create another tenant with its own bindings
        other_tenant = Tenant.objects.create(
            tenant_name="other_test_tenant",
            org_id="other_test_org",
        )
        other_role = RoleV2.objects.create(
            name="other_tenant_role",
            tenant=other_tenant,
        )
        other_binding = RoleBinding.objects.create(
            role=other_role,
            resource_type="workspace",
            resource_id="other-resource",
            tenant=other_tenant,
        )

        try:
            url = self._get_list_url()
            response = self.client.get(f"{url}?limit=100", **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            # Should only see our 15 bindings, not the other tenant's
            self.assertEqual(len(response.data["data"]), 15)

            # Verify none of the returned bindings belong to the other tenant
            returned_role_ids = [str(item["role"]["id"]) for item in response.data["data"]]
            self.assertNotIn(str(other_role.uuid), returned_role_ids)
        finally:
            other_binding.delete()
            other_role.delete()
            other_tenant.delete()

    # --- Functional: verify actual data content ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_returns_correct_role_subject_resource_data(self, mock_permission):
        """Test that response contains correct actual values for a known binding."""
        target_role = self.roles[0]
        target_group = self.groups[0]
        target_binding = self.bindings[0]

        url = self._get_list_url()
        response = self.client.get(f"{url}?role_id={target_role.uuid}", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        item = response.data["data"][0]
        self.assertEqual(str(item["role"]["id"]), str(target_role.uuid))
        self.assertEqual(str(item["subject"]["id"]), str(target_group.uuid))
        self.assertEqual(item["subject"]["type"], "group")
        self.assertEqual(item["resource"]["id"], str(self.workspace.id))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_default_excludes_optional_fields(self, mock_permission):
        """Test that default response excludes role.name and resource.type."""
        url = self._get_list_url()
        response = self.client.get(f"{url}?limit=1", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = response.data["data"][0]

        self.assertNotIn("name", item["role"])
        self.assertNotIn("type", item["resource"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_multiple_bindings_for_same_role(self, mock_permission):
        """Test that multiple bindings for the same role are all returned."""
        # Create a second binding for the same role
        second_group = Group.objects.create(
            name="second_group",
            tenant=self.tenant,
        )
        second_binding = RoleBinding.objects.create(
            role=self.roles[0],
            resource_type="workspace",
            resource_id="other-resource",
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(group=second_group, binding=second_binding)

        try:
            url = self._get_list_url()
            response = self.client.get(f"{url}?role_id={self.roles[0].uuid}&limit=100", **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            # Original binding + new binding
            self.assertEqual(len(response.data["data"]), 2)
        finally:
            RoleBindingGroup.objects.filter(binding=second_binding).delete()
            second_binding.delete()
            second_group.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_binding_without_group_returns_type_only_subject(self, mock_permission):
        """Test that a binding with no group entry returns subject with type only."""
        orphan_role = RoleV2.objects.create(name="orphan_role", tenant=self.tenant)
        orphan_binding = RoleBinding.objects.create(
            role=orphan_role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )

        try:
            url = self._get_list_url()
            response = self.client.get(f"{url}?role_id={orphan_role.uuid}", **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["data"]), 1)
            self.assertEqual(response.data["data"][0]["subject"], {"type": "group"})
        finally:
            orphan_binding.delete()
            orphan_role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_pagination_returns_all_bindings(self, mock_permission):
        """Test that paginating through all pages returns all bindings."""
        url = self._get_list_url()
        all_role_ids = set()
        next_url = f"{url}?limit=4"

        while next_url:
            response = self.client.get(next_url, **self.headers)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            for item in response.data["data"]:
                all_role_ids.add(str(item["role"]["id"]))

            next_link = response.data["links"]["next"]
            if next_link:
                # Extract path + query from full URL
                from urllib.parse import urlparse

                parsed = urlparse(next_link)
                next_url = f"{parsed.path}?{parsed.query}"
            else:
                next_url = None

        # Should have collected all 15 bindings
        expected_ids = {str(r.uuid) for r in self.roles}
        self.assertEqual(all_role_ids, expected_ids)

    # --- Field selection (end-to-end) ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_field_selection(self, mock_permission):
        """Test that field selection controls which fields are returned."""
        url = self._get_list_url()
        test_cases = [
            (
                "role_name",
                "role(name)",
                lambda item: "name" in item["role"],
            ),
            (
                "resource_type",
                "resource(type)",
                lambda item: "type" in item["resource"],
            ),
            (
                "subject_group_name",
                "subject(group.name)",
                lambda item: ("group" in item["subject"] and "name" in item["subject"]["group"]),
            ),
            (
                "sources_type",
                "sources(type)",
                lambda item: ("sources" in item and len(item["sources"]) > 0 and "type" in item["sources"][0]),
            ),
            (
                "combined",
                "role(name),resource(type)",
                lambda item: ("name" in item["role"] and "type" in item["resource"]),
            ),
        ]
        for label, fields_value, check_fn in test_cases:
            with self.subTest(label=label):
                response = self.client.get(f"{url}?fields={fields_value}&limit=1", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                item = response.data["data"][0]
                self.assertTrue(
                    check_fn(item),
                    f"Field selection check failed for {label}: {item}",
                )

    # --- Resource filtering ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_resource(self, mock_permission):
        """Test filtering by resource_id and resource_type."""
        url = self._get_list_url()
        resource_id = str(self.workspace.id)

        cases = [
            ("matching_resource", f"resource_id={resource_id}&resource_type=workspace", 15),
            ("non_matching_id", f"resource_id={uuid.uuid4()}&resource_type=workspace", 0),
            ("non_matching_type", f"resource_id={resource_id}&resource_type=other", 0),
            # Single-field filters require exclude_sources=indirect (no inherited lookup)
            ("resource_type_only", "resource_type=workspace&exclude_sources=indirect", 15),
            ("resource_type_only_no_match", "resource_type=other&exclude_sources=indirect", 0),
            ("resource_id_only", f"resource_id={resource_id}&exclude_sources=indirect", 15),
            ("resource_id_only_no_match", f"resource_id={uuid.uuid4()}&exclude_sources=indirect", 0),
        ]
        for label, query, expected_count in cases:
            with self.subTest(label=label):
                response = self.client.get(f"{url}?{query}&limit=100", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(len(response.data["data"]), expected_count)

    # --- Subject filtering ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_subject_id(self, mock_permission):
        """Test filtering by subject_id (group UUID)."""
        url = self._get_list_url()
        target_group = self.groups[0]

        cases = [
            ("matching_group", str(target_group.uuid), 1),
            ("non_matching_group", str(uuid.uuid4()), 0),
        ]
        for label, subject_id, expected_count in cases:
            with self.subTest(label=label):
                response = self.client.get(f"{url}?subject_id={subject_id}&limit=100", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(len(response.data["data"]), expected_count)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_subject_type(self, mock_permission):
        """Test filtering by subject_type."""
        url = self._get_list_url()

        # Create a principal-bound role binding
        from management.role_binding.model import RoleBindingPrincipal

        user_principal = Principal.objects.create(username="filter_user", tenant=self.tenant, user_id="filter-uid")
        user_role = RoleV2.objects.create(name="user_role", tenant=self.tenant)
        user_role.permissions.add(self.permission)
        user_binding = RoleBinding.objects.create(
            role=user_role, resource_type="workspace", resource_id=str(self.workspace.id), tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(
            principal=user_principal, binding=user_binding, source=API_PRINCIPAL_SOURCE
        )

        try:
            cases = [
                ("group_returns_group_bindings", "group", 15),
                ("user_returns_user_bindings", "user", 1),
                ("unknown_returns_empty", "unknown", 0),
            ]
            for label, subject_type, expected_count in cases:
                with self.subTest(label=label):
                    response = self.client.get(f"{url}?subject_type={subject_type}&limit=100", **self.headers)
                    self.assertEqual(response.status_code, status.HTTP_200_OK)
                    self.assertEqual(len(response.data["data"]), expected_count)

            # Verify user binding response has correct subject type
            response = self.client.get(f"{url}?subject_type=user&limit=100", **self.headers)
            self.assertEqual(response.data["data"][0]["subject"]["type"], "user")
        finally:
            RoleBindingPrincipal.objects.filter(binding=user_binding).delete()
            user_binding.delete()
            user_role.delete()
            user_principal.delete()

    # --- granted_subject filters ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_granted_subject_type_group(self, mock_permission):
        """Test filtering by granted_subject_type=group returns group's bindings."""
        url = self._get_list_url()
        target_group = self.groups[0]

        response = self.client.get(
            f"{url}?granted_subject_type=group&granted_subject_id={target_group.uuid}&limit=100",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_granted_subject_type_user(self, mock_permission):
        """Test that granted_subject_type=user returns direct + group bindings."""
        url = self._get_list_url()

        user_principal = Principal.objects.create(username="granted_user", tenant=self.tenant, user_id="granted-uid")
        user_role = RoleV2.objects.create(name="granted_user_role", tenant=self.tenant)
        user_role.permissions.add(self.permission)
        user_binding = RoleBinding.objects.create(
            role=user_role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(
            principal=user_principal, binding=user_binding, source=API_PRINCIPAL_SOURCE
        )

        target_group = self.groups[0]
        target_group.principals.add(user_principal)

        try:
            response = self.client.get(
                f"{url}?granted_subject_type=user&granted_subject_id={user_principal.uuid}&limit=100",
                **self.headers,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            returned_binding_roles = {str(item["role"]["id"]) for item in response.data["data"]}
            self.assertIn(str(user_role.uuid), returned_binding_roles)
            self.assertIn(str(self.roles[0].uuid), returned_binding_roles)
        finally:
            target_group.principals.remove(user_principal)
            RoleBindingPrincipal.objects.filter(binding=user_binding).delete()
            user_binding.delete()
            user_role.delete()
            user_principal.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_granted_subject_nonexistent_principal(self, mock_permission):
        """Test that granted_subject_type=user with non-existent principal returns empty."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?granted_subject_type=user&granted_subject_id={uuid.uuid4()}&limit=100",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_granted_subject_mutual_exclusivity_with_subject(self, mock_permission):
        """Test that granted_subject params cannot be combined with subject_type/subject_id."""
        url = self._get_list_url()

        cases = [
            (
                "with_subject_type",
                f"granted_subject_type=user&granted_subject_id={uuid.uuid4()}&subject_type=group",
            ),
            (
                "with_subject_id",
                f"granted_subject_type=user&granted_subject_id={uuid.uuid4()}&subject_id={uuid.uuid4()}",
            ),
        ]
        for label, query in cases:
            with self.subTest(label=label):
                response = self.client.get(f"{url}?{query}", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_granted_subject_type_without_id_returns_400(self, mock_permission):
        """Test that providing granted_subject_type without granted_subject_id returns 400."""
        url = self._get_list_url()
        response = self.client.get(f"{url}?granted_subject_type=user", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- granted_subject_type=principal ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_granted_subject_type_principal_with_user_id(self, mock_permission):
        """Test that granted_subject_type=principal with user_id returns direct + group bindings."""
        url = self._get_list_url()

        user_principal = Principal.objects.create(
            username="principal_user", tenant=self.tenant, user_id="ext-principal-uid"
        )
        user_role = RoleV2.objects.create(name="principal_user_role", tenant=self.tenant)
        user_role.permissions.add(self.permission)
        user_binding = RoleBinding.objects.create(
            role=user_role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(
            principal=user_principal, binding=user_binding, source=API_PRINCIPAL_SOURCE
        )

        target_group = self.groups[0]
        target_group.principals.add(user_principal)

        try:
            response = self.client.get(
                f"{url}?granted_subject_type=principal"
                f"&granted_subject.principal.user_id=ext-principal-uid&limit=100",
                **self.headers,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            returned_binding_roles = {str(item["role"]["id"]) for item in response.data["data"]}
            self.assertIn(str(user_role.uuid), returned_binding_roles)
            self.assertIn(str(self.roles[0].uuid), returned_binding_roles)
        finally:
            target_group.principals.remove(user_principal)
            RoleBindingPrincipal.objects.filter(binding=user_binding).delete()
            user_binding.delete()
            user_role.delete()
            user_principal.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_granted_subject_type_principal_nonexistent_user(self, mock_permission):
        """Test that granted_subject_type=principal with non-existent user_id returns empty."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?granted_subject_type=principal" f"&granted_subject.principal.user_id=nonexistent-user&limit=100",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_granted_subject_type_principal_without_user_id_returns_400(self, mock_permission):
        """Test that granted_subject_type=principal without user_id returns 400."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?granted_subject_type=principal&limit=100",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- resource.tenant.org_id ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_resource_tenant_org_id(self, mock_permission):
        """Test that resource.tenant.org_id filters by tenant resource ID."""
        url = self._get_list_url()
        org_id = self.tenant.org_id
        tenant_resource_id = Tenant.org_id_to_tenant_resource_id(org_id)

        role = RoleV2.objects.create(name="tenant_role", tenant=self.tenant)
        role.permissions.add(self.permission)
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="tenant",
            resource_id=tenant_resource_id,
            tenant=self.tenant,
        )
        group = Group.objects.create(name="tenant_group", tenant=self.tenant)
        RoleBindingGroup.objects.create(group=group, binding=binding)

        try:
            response = self.client.get(
                f"{url}?resource.tenant.org_id={org_id}&limit=100",
                **self.headers,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["data"]), 1)
            self.assertEqual(response.data["data"][0]["resource"]["id"], tenant_resource_id)
        finally:
            RoleBindingGroup.objects.filter(binding=binding).delete()
            binding.delete()
            group.delete()
            role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_resource_tenant_org_id_no_match(self, mock_permission):
        """Test that resource.tenant.org_id with non-matching org returns empty."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?resource.tenant.org_id=nonexistent-org&limit=100",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_resource_tenant_org_id_with_resource_id_returns_400(self, mock_permission):
        """Test that combining resource.tenant.org_id with resource_id returns 400."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?resource.tenant.org_id=12345&resource_id=redhat/12345",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- Combined filters ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_combined_filters(self, mock_permission):
        """Test combining role_id, resource, and subject filters."""
        url = self._get_list_url()
        target_role = self.roles[0]
        target_group = self.groups[0]
        resource_id = str(self.workspace.id)

        cases = [
            (
                "role_and_resource",
                f"role_id={target_role.uuid}&resource_id={resource_id}&resource_type=workspace",
                1,
            ),
            (
                "role_and_subject",
                f"role_id={target_role.uuid}&subject_id={target_group.uuid}",
                1,
            ),
            (
                "all_filters_match",
                f"role_id={target_role.uuid}&resource_id={resource_id}&resource_type=workspace"
                f"&subject_type=group&subject_id={target_group.uuid}",
                1,
            ),
            (
                "all_filters_no_match",
                f"role_id={target_role.uuid}&resource_id={uuid.uuid4()}&resource_type=workspace"
                f"&subject_type=group&subject_id={target_group.uuid}",
                0,
            ),
        ]
        for label, query, expected_count in cases:
            with self.subTest(label=label):
                response = self.client.get(f"{url}?{query}&limit=100", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(len(response.data["data"]), expected_count)

    # --- Problem RFC format on errors ---

    @skip("Flaky: multiprocessing serialization error in CI parallel execution - see MaybeEncodingError")
    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_error_responses_use_problem_format(self, mock_permission):
        """Test that all 400 error responses use full Problem RFC 9457 shape."""
        url = self._get_list_url()
        error_cases = [
            ("invalid_role_id", f"{url}?role_id=not-a-uuid", "role_id"),
            ("empty_role_id", f"{url}?role_id=", "role_id"),
            ("direct_order_by", f"{url}?order_by=name", "order_by"),
            ("unknown_order_by", f"{url}?order_by=foo.bar", "order_by"),
            ("group_order_by", f"{url}?order_by=group.name", "order_by"),
            ("unknown_fields_object", f"{url}?fields=bogus(nope)", "fields"),
            ("invalid_role_field", f"{url}?fields=role(nonexistent)", "fields"),
            ("invalid_resource_id", f"{url}?resource_id=not-a-uuid&resource_type=workspace", "resource_id"),
            ("invalid_subject_id", f"{url}?subject_id=not-a-uuid", "subject_id"),
        ]
        for label, request_url, expected_field in error_cases:
            with self.subTest(label=label):
                response = self.client.get(request_url, **self.headers)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response["Content-Type"], "application/problem+json")
                self.assertEqual(
                    response.data,
                    {
                        "status": 400,
                        "title": "The request payload contains invalid syntax.",
                        "detail": ANY,
                        "errors": [{"message": ANY, "field": expected_field}],
                    },
                )

    # --- NUL byte sanitization ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_strips_nul_bytes_from_role_id(self, mock_permission):
        """Test that NUL bytes are stripped from role_id before validation."""
        target_role = self.roles[0]
        url = self._get_list_url()
        response = self.client.get(f"{url}?role_id=\x00{target_role.uuid}\x00", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_strips_nul_bytes_from_fields(self, mock_permission):
        """Test that NUL bytes are stripped from fields parameter."""
        url = self._get_list_url()
        response = self.client.get(f"{url}?fields=\x00role(name)\x00&limit=1", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = response.data["data"][0]
        self.assertIn("name", item["role"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_strips_nul_bytes_from_resource_and_subject_params(self, mock_permission):
        """Test that NUL bytes are stripped from resource and subject parameters."""
        url = self._get_list_url()
        resource_id = str(self.workspace.id)
        target_group = self.groups[0]

        response = self.client.get(
            f"{url}?resource_id=\x00{resource_id}\x00&resource_type=\x00workspace\x00"
            f"&subject_type=\x00group\x00&subject_id=\x00{target_group.uuid}\x00&limit=100",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_includes_bindings_with_seeded_roles(self, mock_permission):
        """Test that role bindings referencing seeded roles from public tenant are returned."""
        seeded_role, _, _ = _create_seeded_role_binding(
            self.tenant, self.workspace, self.permission, "Seeded Role for Binding", "seeded_role_group"
        )

        url = self._get_list_url()
        response = self.client.get(f"{url}?limit=100", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_role_ids = {str(item["role"]["id"]) for item in response.data["data"]}
        self.assertIn(str(seeded_role.uuid), returned_role_ids)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_filter_by_seeded_role_id(self, mock_permission):
        """Test that filtering by a seeded role's UUID returns its bindings."""
        seeded_role, _, _ = _create_seeded_role_binding(
            self.tenant, self.workspace, self.permission, "Filterable Seeded Role", "filter_seeded_group"
        )

        url = self._get_list_url()
        response = self.client.get(f"{url}?role_id={seeded_role.uuid}&fields=role(name)&limit=100", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(str(response.data["data"][0]["role"]["id"]), str(seeded_role.uuid))
        self.assertEqual(response.data["data"][0]["role"]["name"], "Filterable Seeded Role")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_seeded_role_binding_has_correct_role_data(self, mock_permission):
        """Test that bindings with seeded roles include correct role name and ID in response."""
        seeded_role, _, _ = _create_seeded_role_binding(
            self.tenant, self.workspace, self.permission, "Detailed Seeded Role", "detail_seeded_group"
        )

        url = self._get_list_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&fields=role(name)&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        seeded_items = [item for item in response.data["data"] if str(item["role"]["id"]) == str(seeded_role.uuid)]
        self.assertEqual(len(seeded_items), 1)
        self.assertEqual(seeded_items[0]["role"]["name"], "Detailed Seeded Role")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_includes_resource_name_when_requested(self, mock_permission):
        """List returns resource.name when fields includes resource(name)."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?fields=resource(id,name),role(id),subject(id,type)&limit=3",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for item in response.data["data"]:
            self.assertEqual(item["resource"]["id"], str(self.workspace.id))
            self.assertEqual(item["resource"]["name"], "Test Workspace")

    # --- Ordering tests for list endpoint ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_order_by_role_name_ascending(self, mock_permission):
        """Test ordering by role.name ascending."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?order_by=role.name&fields=role(id,name),subject(id,type),resource(id)&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        names = [item["role"]["name"] for item in data]
        self.assertEqual(names, sorted(names))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_order_by_role_name_descending(self, mock_permission):
        """Test ordering by role.name descending."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?order_by=-role.name&fields=role(id,name),subject(id,type),resource(id)&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        names = [item["role"]["name"] for item in data]
        self.assertEqual(names, sorted(names, reverse=True))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_order_by_role_created(self, mock_permission):
        """Test ordering by role.created ascending."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?order_by=role.created&fields=role(id,name),subject(id,type),resource(id)&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Roles were created in order, so role_00 should come first
        names = [item["role"]["name"] for item in data]
        self.assertEqual(names[0], "list_test_role_00")
        self.assertEqual(names[-1], "list_test_role_14")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_order_by_role_modified_descending(self, mock_permission):
        """Test ordering by role.modified descending."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?order_by=-role.modified&fields=role(id,name),subject(id,type),resource(id)&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Verify role UUIDs are in descending modified order
        role_uuids = [item["role"]["id"] for item in data]
        roles = RoleV2.objects.filter(uuid__in=role_uuids)
        role_modified_map = {str(r.uuid): r.modified for r in roles}
        modified_times = [role_modified_map[str(uid)] for uid in role_uuids]
        self.assertEqual(modified_times, sorted(modified_times, reverse=True))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_order_by_resource_type(self, mock_permission):
        """Test ordering by resource.type ascending."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?order_by=resource.type&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_order_by_role_name_with_pagination(self, mock_permission):
        """Test that cursor pagination maintains ordering across pages."""
        url = self._get_list_url()

        # Get first page (5 items)
        response = self.client.get(
            f"{url}?order_by=role.name&fields=role(id,name),subject(id,type),resource(id)&limit=5",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        page1_data = response.data["data"]
        self.assertEqual(len(page1_data), 5)
        self.assertIsNotNone(response.data["links"]["next"])

        # Get second page via cursor
        next_url = response.data["links"]["next"]
        response = self.client.get(next_url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        page2_data = response.data["data"]
        self.assertEqual(len(page2_data), 5)

        # Verify ordering is maintained across pages
        page1_names = [item["role"]["name"] for item in page1_data]
        page2_names = [item["role"]["name"] for item in page2_data]
        all_names = page1_names + page2_names
        self.assertEqual(all_names, sorted(all_names))

        # Verify no overlap between pages
        self.assertEqual(len(set(page1_names) & set(page2_names)), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_order_by_role_name_descending_with_pagination(self, mock_permission):
        """Test that cursor pagination maintains descending ordering across pages."""
        url = self._get_list_url()

        # Get first page
        response = self.client.get(
            f"{url}?order_by=-role.name&fields=role(id,name),subject(id,type),resource(id)&limit=5",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        page1_data = response.data["data"]
        self.assertEqual(len(page1_data), 5)

        # Get second page via cursor
        self.assertIsNotNone(response.data["links"]["next"])
        next_url = response.data["links"]["next"]
        response = self.client.get(next_url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        page2_data = response.data["data"]
        self.assertEqual(len(page2_data), 5)

        # Verify descending ordering is maintained across pages
        page1_names = [item["role"]["name"] for item in page1_data]
        page2_names = [item["role"]["name"] for item in page2_data]
        all_names = page1_names + page2_names
        self.assertEqual(all_names, sorted(all_names, reverse=True))

    # --- Platform role expansion ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_platform_role_expanded_to_children(self, mock_permission):
        """Test that platform-role bindings are expanded to child roles in list response."""
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")

        # Create a platform role with two children
        platform_role = PlatformRoleV2.objects.create(
            name="Platform Admin",
            tenant=public_tenant,
        )
        child_role_1 = SeededRoleV2.objects.create(name="Child Role A", tenant=public_tenant)
        child_role_2 = SeededRoleV2.objects.create(name="Child Role B", tenant=public_tenant)
        platform_role.children.add(child_role_1, child_role_2)

        # Bind the platform role to a group
        platform_binding = RoleBinding.objects.create(
            role=platform_role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        platform_group = Group.objects.create(name="platform_group", tenant=self.tenant)
        RoleBindingGroup.objects.create(group=platform_group, binding=platform_binding)

        # Register cleanup so resources are freed even if assertions fail
        self.addCleanup(RoleBindingGroup.objects.filter(binding=platform_binding).delete)
        self.addCleanup(platform_binding.delete)
        self.addCleanup(platform_group.delete)
        self.addCleanup(platform_role.children.clear)
        self.addCleanup(child_role_1.delete)
        self.addCleanup(child_role_2.delete)
        self.addCleanup(platform_role.delete)

        url = self._get_list_url()
        response = self.client.get(
            f"{url}?fields=role(id,name),subject(id,type),resource(id)&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find entries for the child roles
        child_role_uuids = {child_role_1.uuid, child_role_2.uuid}
        expanded_entries = [item for item in response.data["data"] if item["role"]["id"] in child_role_uuids]

        # Should have 2 entries (one per child), not 1 entry with the platform role
        self.assertEqual(len(expanded_entries), 2)
        returned_names = {item["role"]["name"] for item in expanded_entries}
        self.assertEqual(returned_names, {"Child Role A", "Child Role B"})

        # The platform role itself should NOT appear
        platform_entries = [item for item in response.data["data"] if item["role"]["id"] == platform_role.uuid]
        self.assertEqual(len(platform_entries), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_non_platform_role_unchanged(self, mock_permission):
        """Test that non-platform roles are returned as-is (no expansion)."""
        url = self._get_list_url()
        response = self.client.get(
            f"{url}?fields=role(id,name),subject(id,type),resource(id)&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # All entries should match the roles we created in setUp (all custom, non-platform)
        returned_role_uuids = {item["role"]["id"] for item in response.data["data"]}
        expected_role_uuids = {role.uuid for role in self.roles}
        self.assertEqual(returned_role_uuids, expected_role_uuids)

    # --- Inherited role bindings (exclude_sources) ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_exclude_sources_indirect_returns_direct_only(self, mock_permission):
        """Test that exclude_sources=indirect returns only direct bindings."""
        url = self._get_list_url()
        resource_id = str(self.workspace.id)

        response = self.client.get(
            f"{url}?resource_id={resource_id}&resource_type=workspace&exclude_sources=indirect&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return all 15 direct bindings
        self.assertEqual(len(response.data["data"]), 15)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_without_exclude_sources_defaults_to_none(self, mock_permission):
        """Test that omitting exclude_sources defaults to 'none'."""
        url = self._get_list_url()
        resource_id = str(self.workspace.id)

        response = self.client.get(
            f"{url}?resource_id={resource_id}&resource_type=workspace&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return all 15 direct bindings (Relations API not mocked, so no inherited)
        self.assertEqual(len(response.data["data"]), 15)

    @patch(
        "management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations",
        return_value=None,
    )
    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_exclude_sources_direct_without_relations_server(self, mock_permission, mock_lookup):
        """Test that exclude_sources=direct without RELATION_API_SERVER returns empty."""
        url = self._get_list_url()
        resource_id = str(self.workspace.id)

        response = self.client.get(
            f"{url}?resource_id={resource_id}&resource_type=workspace&exclude_sources=direct&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Cannot determine inherited bindings without Relations API, return empty
        self.assertEqual(len(response.data["data"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations")
    def test_list_exclude_sources_direct_shows_inherited_only(self, mock_lookup, mock_permission):
        """Test that exclude_sources=direct shows only inherited bindings from Relations API."""
        # Create a binding on parent workspace
        parent_role = RoleV2.objects.create(
            name="parent_role_list",
            tenant=self.tenant,
        )
        parent_group = Group.objects.create(
            name="parent_group_list",
            tenant=self.tenant,
        )
        parent_binding = RoleBinding.objects.create(
            role=parent_role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=parent_group,
            binding=parent_binding,
        )

        # Mock Relations API to return the parent binding UUID
        mock_lookup.return_value = [str(parent_binding.uuid)]

        url = self._get_list_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=direct&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should include only the 1 inherited binding (direct are excluded)
        self.assertEqual(len(response.data["data"]), 1)

        # Verify the returned binding is the inherited one
        item = response.data["data"][0]
        self.assertEqual(str(item["role"]["id"]), str(parent_role.uuid))

        # Verify sources shows the parent workspace (where the binding is attached)
        self.assertIn("sources", item)
        self.assertEqual(len(item["sources"]), 1)
        self.assertEqual(item["sources"][0]["id"], str(self.default_workspace.id))

        # Cleanup
        RoleBindingGroup.objects.filter(binding=parent_binding).delete()
        parent_binding.delete()
        parent_group.delete()
        parent_role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations")
    def test_list_exclude_sources_none_includes_both(self, mock_lookup, mock_permission):
        """Test that exclude_sources=none includes both direct and inherited bindings."""
        # Create a binding on parent workspace
        parent_role = RoleV2.objects.create(
            name="parent_role_both",
            tenant=self.tenant,
        )
        parent_group = Group.objects.create(
            name="parent_group_both",
            tenant=self.tenant,
        )
        parent_binding = RoleBinding.objects.create(
            role=parent_role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=parent_group,
            binding=parent_binding,
        )

        # Mock Relations API to return the parent binding UUID
        mock_lookup.return_value = [str(parent_binding.uuid)]

        url = self._get_list_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=none&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should include 15 direct bindings + 1 inherited binding
        self.assertEqual(len(response.data["data"]), 16)

        # Verify parent role is in the response
        returned_role_ids = [str(item["role"]["id"]) for item in response.data["data"]]
        self.assertIn(str(parent_role.uuid), returned_role_ids)

        # Find the inherited binding and verify its sources point to parent workspace
        inherited_item = next(
            item for item in response.data["data"] if str(item["role"]["id"]) == str(parent_role.uuid)
        )
        self.assertEqual(inherited_item["sources"][0]["id"], str(self.default_workspace.id))

        # Find a direct binding and verify its sources point to current workspace
        direct_item = next(
            item for item in response.data["data"] if str(item["role"]["id"]) == str(self.roles[0].uuid)
        )
        self.assertEqual(direct_item["sources"][0]["id"], str(self.workspace.id))

        # Cleanup
        RoleBindingGroup.objects.filter(binding=parent_binding).delete()
        parent_binding.delete()
        parent_group.delete()
        parent_role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_exclude_sources_accepts_valid_values(self, mock_permission):
        """Test that exclude_sources accepts 'direct', 'indirect', and 'none' values."""
        url = self._get_list_url()
        resource_id = str(self.workspace.id)

        for value in ["direct", "indirect", "none"]:
            response = self.client.get(
                f"{url}?resource_id={resource_id}&resource_type=workspace&exclude_sources={value}",
                **self.headers,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, f"exclude_sources={value} should be accepted")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_list_exclude_sources_rejects_invalid_value(self, mock_permission):
        """Test that exclude_sources rejects invalid values."""
        url = self._get_list_url()
        resource_id = str(self.workspace.id)

        response = self.client.get(
            f"{url}?resource_id={resource_id}&resource_type=workspace&exclude_sources=invalid",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class RoleBindingViewSetTest(IdentityRequest):
    """Test the RoleBindingViewSet by-subject endpoint."""

    def setUp(self):
        """Set up test data."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.client = APIClient()

        # Create workspace hierarchy (root -> default -> standard)
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=self.root_workspace,
        )
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            description="Test workspace description",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        # TenantMapping required for V2 write operations (e.g. PUT by-subject)
        TenantMapping.objects.get_or_create(tenant=self.tenant, defaults={})

        # Create permission and role
        self.permission = Permission.objects.create(
            permission="app:resource:read",
            tenant=self.tenant,
        )

        self.role = RoleV2.objects.create(
            name="test_role",
            tenant=self.tenant,
        )
        self.role.permissions.add(self.permission)

        # Create multiple roles and bindings to test pagination
        self.groups = []
        self.bindings = []
        self.roles = [self.role]

        for i in range(15):
            # Create a unique role for each binding
            role = RoleV2.objects.create(
                name=f"test_role_{i}",
                tenant=self.tenant,
            )
            role.permissions.add(self.permission)
            self.roles.append(role)

            group = Group.objects.create(
                name=f"test_group_{i}",
                description=f"Test group {i} description",
                tenant=self.tenant,
            )
            self.groups.append(group)

            principal = Principal.objects.create(
                username=f"user_{i}",
                tenant=self.tenant,
                type=Principal.Types.USER,
            )
            group.principals.add(principal)

            binding = RoleBinding.objects.create(
                role=role,
                resource_type="workspace",
                resource_id=str(self.workspace.id),
                tenant=self.tenant,
            )
            self.bindings.append(binding)

            RoleBindingGroup.objects.create(
                group=group,
                binding=binding,
            )

            # Create RoleBindingPrincipal for user-type queries
            RoleBindingPrincipal.objects.create(
                principal=principal,
                binding=binding,
                source=API_PRINCIPAL_SOURCE,
            )

    def tearDown(self):
        """Tear down test data."""
        RoleBindingPrincipal.objects.filter(binding__tenant=self.tenant).delete()
        RoleBindingGroup.objects.filter(binding__tenant=self.tenant).delete()
        RoleBinding.objects.filter(tenant=self.tenant).delete()
        for group in self.groups:
            group.principals.clear()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        # Delete workspaces in correct order (children first)
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def _get_by_subject_url(self):
        """Get the by-subject URL."""
        return reverse("v2_management:role-bindings-by-subject")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_returns_paginated_response(self, mock_permission):
        """Test that by_subject returns a paginated response structure."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("meta", response.data)
        self.assertIn("links", response.data)
        self.assertIn("data", response.data)
        self.assertIn("limit", response.data["meta"])
        self.assertIn("next", response.data["links"])
        self.assertIn("previous", response.data["links"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_default_limit(self, mock_permission):
        """Test that default limit is 10."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["limit"], 10)
        self.assertEqual(len(response.data["data"]), 10)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_custom_limit(self, mock_permission):
        """Test that custom limit is respected."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&limit=5",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["limit"], 5)
        self.assertEqual(len(response.data["data"]), 5)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_cursor_pagination(self, mock_permission):
        """Test that cursor pagination works correctly."""
        url = self._get_by_subject_url()

        # Get first page
        response1 = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&limit=5",
            **self.headers,
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        page1_subjects = [item["subject"]["id"] for item in response1.data["data"]]

        # Get next page using cursor
        next_link = response1.data["links"]["next"]
        self.assertIsNotNone(next_link)

        parsed = urlparse(next_link)
        cursor = parse_qs(parsed.query).get("cursor", [None])[0]
        self.assertIsNotNone(cursor)

        response2 = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&limit=5&cursor={cursor}",
            **self.headers,
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        page2_subjects = [item["subject"]["id"] for item in response2.data["data"]]

        # Pages should have different subjects
        self.assertEqual(len(set(page1_subjects) & set(page2_subjects)), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_empty_results(self, mock_permission):
        """Test that empty results return valid structure."""
        url = self._get_by_subject_url()

        # Use a non-existent workspace ID
        response = self.client.get(
            f"{url}?resource_id=00000000-0000-0000-0000-000000000000&resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])
        self.assertIsNone(response.data["links"]["next"])
        self.assertIsNone(response.data["links"]["previous"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_first_page_has_no_previous(self, mock_permission):
        """Test that first page has no previous link."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&limit=5",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["links"]["previous"])
        self.assertIsNotNone(response.data["links"]["next"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_last_page_has_no_next(self, mock_permission):
        """Test that last page has no next link."""
        url = self._get_by_subject_url()

        # Request all items in one page
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["links"]["next"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_requires_resource_id(self, mock_permission):
        """Test that resource_id is required."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_requires_resource_type(self, mock_permission):
        """Test that resource_type is required."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_data_structure(self, mock_permission):
        """Test that response data matches expected default structure.

        Default behavior returns only basic required fields:
        - subject: id, type (no group details)
        - roles: id only
        - resource: id only
        """
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&limit=1",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        item = response.data["data"][0]

        self.assertIn("subject", item)
        self.assertIn("roles", item)
        self.assertIn("resource", item)

        # Verify subject structure - only id and type by default
        subject = item["subject"]
        self.assertIn("id", subject)
        self.assertIn("type", subject)
        self.assertEqual(subject["type"], "group")
        self.assertNotIn("group", subject)

        # Verify roles structure - only id by default
        self.assertIsInstance(item["roles"], list)
        if item["roles"]:
            self.assertIn("id", item["roles"][0])
            self.assertNotIn("name", item["roles"][0])

        # Verify resource structure - only id by default
        resource = item["resource"]
        self.assertIn("id", resource)
        self.assertNotIn("name", resource)
        self.assertNotIn("type", resource)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_returns_role_name_with_fields_param(self, mock_permission):
        """Test that fields=roles(name) returns role name in the response."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace" f"&fields=roles(name)&limit=1",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data["data"]), 1)

        item = response.data["data"][0]
        self.assertIn("roles", item)
        self.assertIsInstance(item["roles"], list)
        self.assertGreater(len(item["roles"]), 0, "Expected at least one role in response")

        role = item["roles"][0]
        self.assertIn("name", role, "Role name should be present when fields=roles(name) is requested")
        self.assertIsInstance(role["name"], str)
        self.assertGreater(len(role["name"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_strips_nul_bytes_from_resource_id(self, mock_permission):
        """Test that NUL bytes are stripped from resource_id parameter."""
        url = self._get_by_subject_url()
        # Include NUL byte in resource_id - should be stripped and return empty results
        response = self.client.get(
            f"{url}?resource_id=\x00{self.workspace.id}\x00&resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_strips_nul_bytes_from_resource_type(self, mock_permission):
        """Test that NUL bytes are stripped from resource_type parameter."""
        url = self._get_by_subject_url()
        # Include NUL byte in resource_type - should be stripped
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=\x00workspace\x00",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_nul_only_resource_id_returns_error(self, mock_permission):
        """Test that resource_id with only NUL bytes returns validation error."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id=\x00&resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_nul_only_resource_type_returns_error(self, mock_permission):
        """Test that resource_type with only NUL bytes returns validation error."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=\x00",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # Ordering tests using dot notation

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_group_name_ascending(self, mock_permission):
        """Test ordering by group.name ascending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=group.name&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract group names and verify ascending order
        group_uuids = [item["subject"]["id"] for item in data]
        groups = Group.objects.filter(uuid__in=group_uuids)
        group_name_map = {str(g.uuid): g.name for g in groups}
        names = [group_name_map[str(item["subject"]["id"])] for item in data]
        self.assertEqual(names, sorted(names))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_group_name_descending(self, mock_permission):
        """Test ordering by group.name descending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=-group.name&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract group names and verify descending order
        group_uuids = [item["subject"]["id"] for item in data]
        groups = Group.objects.filter(uuid__in=group_uuids)
        group_name_map = {str(g.uuid): g.name for g in groups}
        names = [group_name_map[str(item["subject"]["id"])] for item in data]
        self.assertEqual(names, sorted(names, reverse=True))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_role_name_ascending(self, mock_permission):
        """Test ordering by role.name ascending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=role.name&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract role UUIDs and look up names from database to verify ascending order
        role_uuids = [item["roles"][0]["id"] for item in data if item["roles"]]
        roles = RoleV2.objects.filter(uuid__in=role_uuids)
        role_name_map = {str(r.uuid): r.name for r in roles}
        role_names = [role_name_map[str(item["roles"][0]["id"])] for item in data if item["roles"]]
        self.assertEqual(role_names, sorted(role_names))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_role_name_descending(self, mock_permission):
        """Test ordering by role.name descending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=-role.name&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract role UUIDs and look up names from database to verify descending order
        role_uuids = [item["roles"][0]["id"] for item in data if item["roles"]]
        roles = RoleV2.objects.filter(uuid__in=role_uuids)
        role_name_map = {str(r.uuid): r.name for r in roles}
        role_names = [role_name_map[str(item["roles"][0]["id"])] for item in data if item["roles"]]
        self.assertEqual(role_names, sorted(role_names, reverse=True))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_group_modified(self, mock_permission):
        """Test ordering by group.modified descending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=-group.modified&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract group modified timestamps and verify descending order
        group_uuids = [item["subject"]["id"] for item in data]
        groups = Group.objects.filter(uuid__in=group_uuids)
        group_modified_map = {str(g.uuid): g.modified for g in groups}
        modified_times = [group_modified_map[str(item["subject"]["id"])] for item in data]
        self.assertEqual(modified_times, sorted(modified_times, reverse=True))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_group_created(self, mock_permission):
        """Test ordering by group.created ascending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=group.created&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract group created timestamps and verify ascending order
        group_uuids = [item["subject"]["id"] for item in data]
        groups = Group.objects.filter(uuid__in=group_uuids)
        group_created_map = {str(g.uuid): g.created for g in groups}
        created_times = [group_created_map[str(item["subject"]["id"])] for item in data]
        self.assertEqual(created_times, sorted(created_times))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_rejects_direct_field_name(self, mock_permission):
        """Test that ordering by direct field name (without dot notation) is rejected."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=name",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid ordering field", str(response.data))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_rejects_unknown_field(self, mock_permission):
        """Test that ordering by unknown dot notation field is rejected."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=foo.bar",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid ordering field", str(response.data))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_comma_separated_fields(self, mock_permission):
        """Test ordering by multiple comma-separated fields."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=group.name,-group.modified&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Verify primary ordering by group.name ascending
        group_uuids = [item["subject"]["id"] for item in data]
        groups = Group.objects.filter(uuid__in=group_uuids)
        group_name_map = {str(g.uuid): g.name for g in groups}
        names = [group_name_map[str(item["subject"]["id"])] for item in data]
        self.assertEqual(names, sorted(names))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_multiple_params(self, mock_permission):
        """Test ordering by multiple order_by parameters."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=group.name&order_by=-group.modified&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Verify primary ordering by group.name ascending
        group_uuids = [item["subject"]["id"] for item in data]
        groups = Group.objects.filter(uuid__in=group_uuids)
        group_name_map = {str(g.uuid): g.name for g in groups}
        names = [group_name_map[str(item["subject"]["id"])] for item in data]
        self.assertEqual(names, sorted(names))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_rejects_mixed_valid_invalid(self, mock_permission):
        """Test that ordering with mixed valid and invalid fields is rejected."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=group.name,invalid_field",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid ordering field", str(response.data))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_group_uuid(self, mock_permission):
        """Test ordering by group.uuid ascending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=group.uuid&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Verify uuid ordering (convert to strings for consistent comparison)
        uuids = [str(item["subject"]["id"]) for item in data]
        self.assertEqual(uuids, sorted(uuids))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_group_uuid_descending(self, mock_permission):
        """Test ordering by group.uuid descending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=-group.uuid&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Verify uuid ordering descending (convert to strings for consistent comparison)
        uuids = [str(item["subject"]["id"]) for item in data]
        self.assertEqual(uuids, sorted(uuids, reverse=True))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_role_uuid(self, mock_permission):
        """Test ordering by role.uuid ascending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=role.uuid&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract role UUIDs and verify ascending order
        role_uuids = [str(item["roles"][0]["id"]) for item in data if item["roles"]]
        self.assertEqual(role_uuids, sorted(role_uuids))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_role_modified(self, mock_permission):
        """Test ordering by role.modified descending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=-role.modified&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract role UUIDs and verify modified times are in descending order
        role_uuids = [item["roles"][0]["id"] for item in data if item["roles"]]
        roles = RoleV2.objects.filter(uuid__in=role_uuids)
        role_modified_map = {str(r.uuid): r.modified for r in roles}
        modified_times = [role_modified_map[str(item["roles"][0]["id"])] for item in data if item["roles"]]
        self.assertEqual(modified_times, sorted(modified_times, reverse=True))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_role_created(self, mock_permission):
        """Test ordering by role.created ascending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=role.created&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract role UUIDs and verify created times are in ascending order
        role_uuids = [item["roles"][0]["id"] for item in data if item["roles"]]
        roles = RoleV2.objects.filter(uuid__in=role_uuids)
        role_created_map = {str(r.uuid): r.created for r in roles}
        created_times = [role_created_map[str(item["roles"][0]["id"])] for item in data if item["roles"]]
        self.assertEqual(created_times, sorted(created_times))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_last_modified(self, mock_permission):
        """Test ordering by last_modified (assignment time) matches response field semantics."""
        g_early, g_late = self.groups[0], self.groups[1]
        rbg_early = RoleBindingGroup.objects.get(group=g_early, binding__resource_id=str(self.workspace.id))
        rbg_late = RoleBindingGroup.objects.get(group=g_late, binding__resource_id=str(self.workspace.id))
        base = timezone.now()
        RoleBindingGroup.objects.filter(pk=rbg_early.pk).update(created=base - timedelta(hours=2))
        RoleBindingGroup.objects.filter(pk=rbg_late.pk).update(created=base - timedelta(hours=1))

        url = self._get_by_subject_url()

        response_asc = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&order_by=last_modified&fields=subject(id),last_modified&limit=100",
            **self.headers,
        )
        self.assertEqual(response_asc.status_code, status.HTTP_200_OK)
        data_asc = response_asc.data["data"]
        self.assertGreater(len(data_asc), 1)
        times_asc = [_coerce_api_datetime(item["last_modified"]) for item in data_asc]
        self.assertEqual(times_asc, sorted(times_asc))
        uuids_asc = [str(item["subject"]["id"]) for item in data_asc]
        self.assertLess(uuids_asc.index(str(g_early.uuid)), uuids_asc.index(str(g_late.uuid)))

        response_desc = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&order_by=-last_modified&fields=subject(id),last_modified&limit=100",
            **self.headers,
        )
        self.assertEqual(response_desc.status_code, status.HTTP_200_OK)
        data_desc = response_desc.data["data"]
        times_desc = [_coerce_api_datetime(item["last_modified"]) for item in data_desc]
        self.assertEqual(times_desc, sorted(times_desc, reverse=True))
        uuids_desc = [str(item["subject"]["id"]) for item in data_desc]
        self.assertLess(uuids_desc.index(str(g_late.uuid)), uuids_desc.index(str(g_early.uuid)))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_order_by_role_name_with_pagination(self, mock_permission):
        """Test that cursor pagination maintains role.name ordering across pages."""
        url = self._get_by_subject_url()

        # Get first page (5 items)
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&order_by=role.name&limit=5",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        page1_data = response.data["data"]
        self.assertEqual(len(page1_data), 5)
        self.assertIsNotNone(response.data["links"]["next"])

        # Get second page via cursor
        next_url = response.data["links"]["next"]
        response = self.client.get(next_url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        page2_data = response.data["data"]
        self.assertEqual(len(page2_data), 5)

        # Verify ordering is maintained across pages by looking up role names
        def extract_role_names(data):
            role_uuids = [item["roles"][0]["id"] for item in data if item["roles"]]
            roles = RoleV2.objects.filter(uuid__in=role_uuids)
            role_name_map = {str(r.uuid): r.name for r in roles}
            return [role_name_map[str(item["roles"][0]["id"])] for item in data if item["roles"]]

        page1_names = extract_role_names(page1_data)
        page2_names = extract_role_names(page2_data)
        all_names = page1_names + page2_names
        self.assertEqual(all_names, sorted(all_names))

    # User subject type tests

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_returns_users(self, mock_permission):
        """Test that subject_type=user returns users (principals)."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]

        # Should return users (we have 15 groups with 1 user each)
        self.assertEqual(len(data), 15)

        # Verify all subjects are type="user"
        for item in data:
            self.assertEqual(item["subject"]["type"], "user")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_data_structure(self, mock_permission):
        """Test that user subject type response has correct structure."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user&limit=1",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        item = response.data["data"][0]

        # Verify structure - no last_modified by default
        self.assertNotIn("last_modified", item)
        self.assertIn("subject", item)
        self.assertIn("roles", item)
        self.assertIn("resource", item)

        # Verify subject structure - id and type, no user details by default
        subject = item["subject"]
        self.assertIn("id", subject)
        self.assertIn("type", subject)
        self.assertEqual(subject["type"], "user")
        self.assertNotIn("user", subject)

        # Verify roles structure - only id by default
        self.assertIsInstance(item["roles"], list)
        if item["roles"]:
            self.assertIn("id", item["roles"][0])
            self.assertNotIn("name", item["roles"][0])

        # Verify resource structure - only id by default
        resource = item["resource"]
        self.assertIn("id", resource)
        self.assertNotIn("name", resource)
        self.assertNotIn("type", resource)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_subject_created_reflects_assignment(self, mock_permission):
        """Test that last_modified reflects when a subject was assigned to a role binding."""
        url = self._get_by_subject_url()
        group = self.groups[0]

        # Assign a role to a group via PUT
        put_response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={group.uuid}&subject_type=group&fields=last_modified",
            data={"roles": [{"id": str(self.role.uuid)}]},
            format="json",
            **self.headers,
        )
        self.assertEqual(put_response.status_code, status.HTTP_200_OK)

        # GET by-subject with fields=last_modified and verify the value
        get_response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_type=group&subject_id={group.uuid}&fields=last_modified&limit=1",
            **self.headers,
        )
        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        data = get_response.data["data"]
        self.assertGreater(len(data), 0)

        item = data[0]
        self.assertIn("last_modified", item)
        last_modified = item["last_modified"]
        self.assertIsNotNone(last_modified)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_with_fields_parameter(self, mock_permission):
        """Test that user subject type works with fields parameter."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user"
            "&fields=subject(user.username),roles(name),resource(type)&limit=1",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

        item = response.data["data"][0]

        # Verify subject includes user.username
        subject = item["subject"]
        self.assertEqual(subject["type"], "user")
        self.assertIn("user", subject)
        self.assertIn("username", subject["user"])

        # Verify roles include name
        if item["roles"]:
            self.assertIn("name", item["roles"][0])

        # Verify resource includes type
        self.assertIn("type", item["resource"])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_pagination(self, mock_permission):
        """Test that user subject type supports pagination."""
        url = self._get_by_subject_url()

        # Get first page
        response1 = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user&limit=5",
            **self.headers,
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response1.data["data"]), 5)
        page1_subjects = [item["subject"]["id"] for item in response1.data["data"]]

        # Get next page
        next_link = response1.data["links"]["next"]
        self.assertIsNotNone(next_link)

        parsed = urlparse(next_link)
        cursor = parse_qs(parsed.query).get("cursor", [None])[0]
        self.assertIsNotNone(cursor)

        response2 = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user&limit=5&cursor={cursor}",
            **self.headers,
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        page2_subjects = [item["subject"]["id"] for item in response2.data["data"]]

        # Pages should have different subjects
        self.assertEqual(len(set(page1_subjects) & set(page2_subjects)), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_order_by_username(self, mock_permission):
        """Test ordering by user.username ascending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user"
            "&order_by=user.username&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract usernames and verify ascending order
        user_uuids = [item["subject"]["id"] for item in data]
        principals = Principal.objects.filter(uuid__in=user_uuids)
        username_map = {str(p.uuid): p.username for p in principals}
        usernames = [username_map[str(item["subject"]["id"])] for item in data]
        self.assertEqual(usernames, sorted(usernames))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_order_by_username_descending(self, mock_permission):
        """Test ordering by user.username descending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user"
            "&order_by=-user.username&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Extract usernames and verify descending order
        user_uuids = [item["subject"]["id"] for item in data]
        principals = Principal.objects.filter(uuid__in=user_uuids)
        username_map = {str(p.uuid): p.username for p in principals}
        usernames = [username_map[str(item["subject"]["id"])] for item in data]
        self.assertEqual(usernames, sorted(usernames, reverse=True))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_rejects_group_ordering_fields(self, mock_permission):
        """Test that group ordering fields are rejected for user subject type."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user" "&order_by=group.name",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid ordering field", str(response.data))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_group_type_rejects_user_ordering_fields(self, mock_permission):
        """Test that user ordering fields are rejected for group subject type."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=group"
            "&order_by=user.username",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid ordering field", str(response.data))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_order_by_user_uuid(self, mock_permission):
        """Test ordering by user.uuid ascending."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user"
            "&order_by=user.uuid&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertGreater(len(data), 1)

        # Verify uuid ordering
        uuids = [str(item["subject"]["id"]) for item in data]
        self.assertEqual(uuids, sorted(uuids))

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_empty_results(self, mock_permission):
        """Test that non-existent resource returns empty results for user type."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id=00000000-0000-0000-0000-000000000000&resource_type=workspace&subject_type=user",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_user_type_filter_by_subject_id(self, mock_permission):
        """Test filtering by subject_id with user type."""
        # Get a specific user's UUID
        first_principal = Principal.objects.filter(
            tenant=self.tenant,
            type=Principal.Types.USER,
            username__startswith="user_",
        ).first()

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&subject_type=user"
            f"&subject_id={first_principal.uuid}",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(str(response.data["data"][0]["subject"]["id"]), str(first_principal.uuid))

    # Exclude sources tests

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_exclude_sources_indirect_returns_direct_only(self, mock_permission):
        """Test that exclude_sources=indirect returns only direct bindings."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=indirect&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return direct bindings for the workspace
        self.assertEqual(len(response.data["data"]), 15)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_without_exclude_sources_defaults_to_none(self, mock_permission):
        """Test that omitting exclude_sources defaults to 'none' (shows all, falls back to direct without Relations API)."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return direct bindings for the workspace
        self.assertEqual(len(response.data["data"]), 15)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.util.relations_api_client.settings")
    def test_by_subject_exclude_sources_direct_without_relations_server(self, mock_settings, mock_permission):
        """Test that exclude_sources=direct without RELATION_API_SERVER returns empty."""
        mock_settings.RELATION_API_SERVER = None

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=direct&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Cannot determine inherited bindings without Relations API, return empty
        self.assertEqual(len(response.data["data"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations")
    def test_by_subject_exclude_sources_direct_shows_inherited_only(self, mock_lookup, mock_permission):
        """Test that exclude_sources=direct shows only inherited bindings from Relations API."""
        # Create a binding on parent workspace
        parent_role = RoleV2.objects.create(
            name="parent_role",
            tenant=self.tenant,
        )
        parent_group = Group.objects.create(
            name="parent_group",
            tenant=self.tenant,
        )
        parent_binding = RoleBinding.objects.create(
            role=parent_role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=parent_group,
            binding=parent_binding,
        )

        # Mock Relations API to return the parent binding UUID
        mock_lookup.return_value = [str(parent_binding.uuid)]

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=direct&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should include only the 1 inherited binding (direct are excluded)
        self.assertEqual(len(response.data["data"]), 1)

        # Verify parent_group is in the response with non-empty roles
        parent_group_item = response.data["data"][0]
        self.assertEqual(str(parent_group_item["subject"]["id"]), str(parent_group.uuid))
        self.assertIn("roles", parent_group_item)
        self.assertGreater(
            len(parent_group_item["roles"]),
            0,
            "Inherited-only response should have roles populated from parent binding",
        )
        self.assertEqual(
            str(parent_group_item["roles"][0]["id"]),
            str(parent_role.uuid),
            "Inherited group should have the correct role from parent binding",
        )

        # Cleanup
        RoleBindingGroup.objects.filter(binding=parent_binding).delete()
        parent_binding.delete()
        parent_group.delete()
        parent_role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations")
    def test_by_subject_exclude_sources_direct_excludes_direct_when_relations_returns_both(
        self, mock_lookup, mock_permission
    ):
        """Test that exclude_sources=direct excludes direct bindings even when Relations API returns both.

        This tests the fix for a bug where the Relations API returns both direct and inherited
        binding UUIDs, but exclude_sources=direct showed both binding types.
        """
        # Create an inherited binding on parent workspace
        inherited_role = RoleV2.objects.create(
            name="inherited_role",
            tenant=self.tenant,
        )
        inherited_group = Group.objects.create(
            name="inherited_group",
            tenant=self.tenant,
        )
        inherited_binding = RoleBinding.objects.create(
            role=inherited_role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=inherited_group,
            binding=inherited_binding,
        )

        # Get a direct binding UUID from setUp (these are on self.workspace)
        direct_binding = self.bindings[0]

        # Mock Relations API to return BOTH direct and inherited binding UUIDs
        # This simulates real behavior where the "binding" relation returns all bindings
        mock_lookup.return_value = [str(direct_binding.uuid), str(inherited_binding.uuid)]

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=direct&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only include the inherited binding, not the direct one
        self.assertEqual(len(response.data["data"]), 1)

        # Verify only inherited_group is returned
        returned_subject_ids = [str(item["subject"]["id"]) for item in response.data["data"]]
        self.assertIn(str(inherited_group.uuid), returned_subject_ids)

        # Verify direct binding's group is NOT returned
        direct_group = self.groups[0]
        self.assertNotIn(str(direct_group.uuid), returned_subject_ids)

        # Cleanup
        RoleBindingGroup.objects.filter(binding=inherited_binding).delete()
        inherited_binding.delete()
        inherited_group.delete()
        inherited_role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations")
    def test_by_subject_exclude_sources_direct_excludes_direct_for_users(self, mock_lookup, mock_permission):
        """Test that exclude_sources=direct excludes direct bindings for user subject type.

        This tests the fix for a bug where the Relations API returns both direct and inherited
        binding UUIDs, but exclude_sources=direct showed both binding types.
        """
        # Create an inherited binding on parent workspace with a user
        inherited_role = RoleV2.objects.create(
            name="inherited_user_role",
            tenant=self.tenant,
        )
        inherited_user = Principal.objects.create(
            username="inherited_user",
            tenant=self.tenant,
            type=Principal.Types.USER,
        )
        inherited_binding = RoleBinding.objects.create(
            role=inherited_role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(
            principal=inherited_user,
            binding=inherited_binding,
            source=API_PRINCIPAL_SOURCE,
        )

        # Get a direct binding UUID from setUp (these are on self.workspace)
        direct_binding = self.bindings[0]

        # Mock Relations API to return BOTH direct and inherited binding UUIDs
        mock_lookup.return_value = [str(direct_binding.uuid), str(inherited_binding.uuid)]

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_type=user&exclude_sources=direct&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only include the inherited user, not the direct ones
        self.assertEqual(len(response.data["data"]), 1)

        # Verify only inherited_user is returned
        returned_subject_ids = [str(item["subject"]["id"]) for item in response.data["data"]]
        self.assertIn(str(inherited_user.uuid), returned_subject_ids)

        # Cleanup
        RoleBindingPrincipal.objects.filter(binding=inherited_binding).delete()
        inherited_binding.delete()
        inherited_user.delete()
        inherited_role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations")
    def test_by_subject_exclude_sources_direct_with_empty_inherited(self, mock_lookup, mock_permission):
        """Test that exclude_sources=direct with no inherited bindings returns empty."""
        # Mock Relations API to return empty list
        mock_lookup.return_value = []

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=direct&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return no bindings (no inherited, direct excluded)
        self.assertEqual(len(response.data["data"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations")
    def test_by_subject_exclude_sources_direct_with_relations_error(self, mock_lookup, mock_permission):
        """Test that exclude_sources=direct returns empty when Relations API errors."""
        # Mock Relations API to return None (error case)
        mock_lookup.return_value = None

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=direct&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Cannot determine inherited bindings, return empty
        self.assertEqual(len(response.data["data"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_exclude_sources_accepts_valid_values(self, mock_permission):
        """Test that exclude_sources accepts 'direct', 'indirect', and 'none' values."""
        url = self._get_by_subject_url()

        for value in ("indirect", "direct", "none"):
            response = self.client.get(
                f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources={value}",
                **self.headers,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, f"exclude_sources={value} should be accepted")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._lookup_binding_uuids_via_relations")
    def test_by_subject_exclude_sources_none_includes_both(self, mock_lookup, mock_permission):
        """Test that exclude_sources=none includes both direct and inherited bindings."""
        # Create a binding on parent workspace
        parent_role = RoleV2.objects.create(
            name="parent_role",
            tenant=self.tenant,
        )
        parent_group = Group.objects.create(
            name="parent_group",
            tenant=self.tenant,
        )
        parent_binding = RoleBinding.objects.create(
            role=parent_role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=parent_group,
            binding=parent_binding,
        )

        # Mock Relations API to return the parent binding UUID
        mock_lookup.return_value = [str(parent_binding.uuid)]

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=none&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should include 15 direct bindings + 1 inherited binding
        self.assertEqual(len(response.data["data"]), 16)

        # Verify parent_group is in the response
        subject_ids = [str(item["subject"]["id"]) for item in response.data["data"]]
        self.assertIn(str(parent_group.uuid), subject_ids)

        # Verify inherited group has non-empty roles (inherited from parent workspace)
        parent_group_item = next(
            item for item in response.data["data"] if str(item["subject"]["id"]) == str(parent_group.uuid)
        )
        self.assertIn("roles", parent_group_item)
        self.assertGreater(
            len(parent_group_item["roles"]),
            0,
            "Inherited group should have roles populated from parent workspace binding",
        )
        self.assertEqual(
            str(parent_group_item["roles"][0]["id"]),
            str(parent_role.uuid),
            "Inherited group should have the correct role from parent binding",
        )

        # Cleanup
        RoleBindingGroup.objects.filter(binding=parent_binding).delete()
        parent_binding.delete()
        parent_group.delete()
        parent_role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_exclude_sources_rejects_invalid_value(self, mock_permission):
        """Test that exclude_sources rejects invalid values."""
        url = self._get_by_subject_url()

        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&exclude_sources=invalid",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_includes_bindings_with_seeded_roles(self, mock_permission):
        """Test that by-subject returns groups whose bindings reference seeded roles from public tenant."""
        _, _, seeded_group = _create_seeded_role_binding(
            self.tenant, self.workspace, self.permission, "Seeded Role for BySubject", "seeded_bysubject_group"
        )

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_subject_ids = {str(item["subject"]["id"]) for item in response.data["data"]}
        self.assertIn(str(seeded_group.uuid), returned_subject_ids)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_seeded_role_appears_in_roles_list(self, mock_permission):
        """Test that by-subject response includes seeded role ID in the roles list for a group."""
        seeded_role, _, seeded_group = _create_seeded_role_binding(
            self.tenant, self.workspace, self.permission, "Seeded Role in Roles List", "seeded_roles_list_group"
        )

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={seeded_group.uuid}&limit=100",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        role_ids = {str(r["id"]) for r in response.data["data"][0]["roles"]}
        self.assertIn(str(seeded_role.uuid), role_ids)

    # --- resource.tenant.org_id on by-subject ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_filter_by_resource_tenant_org_id(self, mock_permission):
        """Test that resource.tenant.org_id resolves and returns tenant bindings."""
        url = self._get_by_subject_url()
        org_id = self.tenant.org_id
        tenant_resource_id = Tenant.org_id_to_tenant_resource_id(org_id)

        role = RoleV2.objects.create(name="tenant_role_bs", tenant=self.tenant)
        role.permissions.add(self.permission)
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="tenant",
            resource_id=tenant_resource_id,
            tenant=self.tenant,
        )
        group = Group.objects.create(name="tenant_group_bs", tenant=self.tenant)
        RoleBindingGroup.objects.create(group=group, binding=binding)

        try:
            response = self.client.get(
                f"{url}?resource.tenant.org_id={org_id}&limit=100",
                **self.headers,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertGreaterEqual(len(response.data["data"]), 1)
            resource_id = response.data["data"][0]["resource"]["id"]
            self.assertEqual(resource_id, tenant_resource_id)
        finally:
            RoleBindingGroup.objects.filter(binding=binding).delete()
            binding.delete()
            group.delete()
            role.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_resource_tenant_org_id_no_match(self, mock_permission):
        """Test that resource.tenant.org_id with non-matching org returns empty."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource.tenant.org_id=nonexistent-org&limit=100",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 0)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_resource_tenant_org_id_with_resource_id_returns_400(self, mock_permission):
        """Test that combining resource.tenant.org_id with resource_id returns 400."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource.tenant.org_id=12345&resource_id=redhat/12345",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_resource_tenant_org_id_with_wrong_resource_type_returns_400(self, mock_permission):
        """Test that resource.tenant.org_id with resource_type != 'tenant' returns 400."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource.tenant.org_id=12345&resource_type=workspace",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_by_subject_no_resource_params_returns_400(self, mock_permission):
        """Test that omitting all resource params returns 400."""
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?subject_type=group",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(V2_APIS_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class DefaultBindingsAPITests(TestCase):
    """Test lazy creation of default role bindings via API calls."""

    def setUp(self):
        """Set up test data for each test."""
        super().setUp()
        reload(urls)
        clear_url_caches()

        # Clear caches to ensure fresh state - other tests might have modified these
        GlobalPolicyIdService.clear_shared()
        Tenant._public_tenant = None  # Clear the public tenant cache

        # Seed platform roles and default groups (required for default bindings)
        # Moved to setUp() instead of setUpClass() to avoid race conditions in parallel test execution
        seed_roles()
        seed_group()

        # Use V2 bootstrap to create tenant with TenantMapping
        # Use a unique org_id to avoid conflicts when running tests in parallel
        unique_org_id = f"test-default-bindings-api-{uuid.uuid4().hex[:8]}"

        self.replicator = InMemoryRelationReplicator()
        self.bootstrap_service = V2TenantBootstrapService(self.replicator)
        bootstrapped = self.bootstrap_service.new_bootstrapped_tenant(unique_org_id)
        self.tenant = bootstrapped.tenant
        self.mapping = bootstrapped.mapping
        self.default_workspace = bootstrapped.default_workspace

        # Set up API client with proper headers
        self.client = APIClient()
        self.headers = {
            "HTTP_X_RH_IDENTITY": self._create_identity_header(),
        }

        self.service = RoleBindingService(tenant=self.tenant)

    def _create_identity_header(self):
        """Create an identity header for API requests."""
        identity = {
            "identity": {
                "account_number": "12345",
                "org_id": self.tenant.org_id,
                "type": "User",
                "user": {
                    "username": "test_user",
                    "email": "test@example.com",
                    "is_org_admin": True,
                    "is_internal": False,
                    "user_id": "123456",
                },
                "internal": {"org_id": self.tenant.org_id},
            }
        }
        return base64.b64encode(json.dumps(identity).encode()).decode()

    def tearDown(self):
        """Clean up test data."""
        # Clean up in reverse dependency order
        RoleBindingGroup.objects.filter(binding__tenant=self.tenant).delete()
        RoleBinding.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        # Delete workspaces in order: standard, default, root (child to parent)
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        self.tenant.delete()

    def _get_by_subject_url(self):
        """Get the by-subject URL."""
        return reverse("v2_management:role-bindings-by-subject")

    def _count_default_bindings(self, access_type: DefaultAccessType) -> int:
        """Count existing default bindings for the given access type."""
        binding_uuids = [self.mapping.default_role_binding_uuid_for(access_type, s) for s in Scope]
        return RoleBinding.objects.filter(uuid__in=binding_uuids).count()

    @skip("Flaky: fails intermittently in CI when tests run in parallel due to test isolation issues")
    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_default_bindings_created_on_api_call(self, mock_permission):
        """Test that default bindings are created when API is called.

        Also verifies that platform roles return their children (seeded roles)
        instead of the platform role itself in the API response.
        """
        # Initially, no default bindings should exist
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 0)

        # Call the API
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.default_workspace.id}&resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Now all 6 default bindings should exist (3 USER + 3 ADMIN for each scope)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 3)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 3)

        # Verify that platform roles return their children, not the platform role itself
        data = response.data["data"]
        public_tenant = Tenant.objects.get(tenant_name="public")

        # Get platform default groups (used for default bindings)
        platform_default_group = Group.objects.get(platform_default=True, tenant=public_tenant)
        admin_default_group = Group.objects.get(admin_default=True, tenant=public_tenant)

        # Find these groups in the response
        platform_group_data = None
        admin_group_data = None
        for item in data:
            if str(item["subject"]["id"]) == str(platform_default_group.uuid):
                platform_group_data = item
            if str(item["subject"]["id"]) == str(admin_default_group.uuid):
                admin_group_data = item

        # Assert that both groups are present before verifying their roles
        self.assertIsNotNone(
            platform_group_data,
            f"Platform default group {platform_default_group.uuid} should be in response",
        )
        self.assertIsNotNone(
            admin_group_data,
            f"Admin default group {admin_default_group.uuid} should be in response",
        )

        # Get the platform roles used in default bindings
        policy_service = GlobalPolicyIdService.shared()

        # Verify platform default group returns children
        role_ids = [str(role["id"]) for role in platform_group_data["roles"]]
        platform_role_uuid = platform_v2_role_uuid_for(DefaultAccessType.USER, Scope.DEFAULT, policy_service)
        platform_role = PlatformRoleV2.objects.get(uuid=platform_role_uuid)

        # Platform role should NOT be in response
        self.assertNotIn(str(platform_role.uuid), role_ids, "Platform role should not be returned")

        # Children should be in response
        child_uuids = [str(child.uuid) for child in platform_role.children.all()]
        for child_uuid in child_uuids:
            self.assertIn(child_uuid, role_ids, f"Child role {child_uuid} should be returned")

        # Verify admin default group returns children
        role_ids = [str(role["id"]) for role in admin_group_data["roles"]]
        admin_role_uuid = platform_v2_role_uuid_for(DefaultAccessType.ADMIN, Scope.DEFAULT, policy_service)
        admin_role = PlatformRoleV2.objects.get(uuid=admin_role_uuid)

        # Platform role should NOT be in response
        self.assertNotIn(str(admin_role.uuid), role_ids, "Admin platform role should not be returned")

        # Children should be in response
        child_uuids = [str(child.uuid) for child in admin_role.children.all()]
        for child_uuid in child_uuids:
            self.assertIn(
                child_uuid,
                role_ids,
                f"Admin child role {child_uuid} should be returned",
            )

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_user_bindings_skipped_with_custom_default_group(self, mock_permission):
        """Test that USER bindings are skipped when tenant has a custom default group."""
        # Create a custom default group for this tenant
        custom_group = Group.objects.create(
            name="Custom default access",
            tenant=self.tenant,
            platform_default=True,
            system=False,
        )

        # Initially no bindings
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 0)

        # Call the API
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.default_workspace.id}&resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # USER bindings should NOT be created (custom group exists)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)
        # ADMIN bindings should still be created
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 3)

        # Cleanup
        custom_group.delete()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_delete_user_default_bindings(self, mock_permission):
        """Test that delete_user_default_bindings removes USER bindings."""
        # First create the default bindings via API
        url = self._get_by_subject_url()
        self.client.get(
            f"{url}?resource_id={self.default_workspace.id}&resource_type=workspace",
            **self.headers,
        )

        # Verify bindings exist
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 3)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 3)

        # Delete USER bindings using the service (simulating custom group creation)
        self.service.delete_user_default_bindings()

        # USER bindings should be deleted, ADMIN bindings should remain
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 3)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_restore_user_default_bindings(self, mock_permission):
        """Test that restore_user_default_bindings recreates USER bindings."""
        # First create all default bindings via API
        url = self._get_by_subject_url()
        self.client.get(
            f"{url}?resource_id={self.default_workspace.id}&resource_type=workspace",
            **self.headers,
        )

        # Delete USER bindings (simulating custom group creation)
        self.service.delete_user_default_bindings()
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)

        # Restore USER bindings (simulating custom group deletion)
        self.service.restore_user_default_bindings()

        # USER bindings should be restored
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 3)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 3)


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class BatchCreateViewTests(IdentityRequest):
    """Tests for the :batchCreate endpoint on RoleBindingViewSet."""

    def setUp(self):
        """Set up test data."""
        reload(urls)
        clear_url_caches()
        super().setUp()

        # Clean up any existing bootstrap data to ensure fresh state
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant).delete()

        bootstrapped = V2TenantBootstrapService(InMemoryRelationReplicator()).bootstrap_tenant(self.tenant)
        self.root_workspace = bootstrapped.root_workspace
        self.default_workspace = bootstrapped.default_workspace
        self.client = APIClient()

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        self.permission = Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        self.role_service = RoleV2Service()
        self.role = self.role_service.create(
            name="test_role",
            description="Test role",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "read"}],
            tenant=self.tenant,
        )

        self.group = Group.objects.create(
            name="test_group",
            description="Test group",
            tenant=self.tenant,
        )
        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            user_id="testuser",
            type=Principal.Types.USER,
        )

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.filter(binding__tenant=self.tenant).delete()
        RoleBinding.objects.filter(tenant=self.tenant).delete()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        super().tearDown()

    def _get_batch_create_url(self):
        return reverse("v2_management:role-bindings-batch-create")

    def _valid_payload(self):
        return {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                }
            ]
        }

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_returns_201(self, mock_permission):
        """Valid request returns 201 with role_bindings list."""
        url = self._get_batch_create_url()
        response = self.client.post(url, self._valid_payload(), format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("role_bindings", response.data)
        self.assertEqual(len(response.data["role_bindings"]), 1)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_response_structure(self, mock_permission):
        """Each item in response has role, subject, resource with correct keys."""
        url = self._get_batch_create_url()
        response = self.client.post(url, self._valid_payload(), format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item = response.data["role_bindings"][0]

        self.assertIn("id", item["role"])
        self.assertEqual(item["role"]["id"], self.role.uuid)

        self.assertIn("id", item["subject"])
        self.assertEqual(item["subject"]["id"], self.group.uuid)
        self.assertEqual(item["subject"]["type"], "group")

        self.assertIn("id", item["resource"])
        self.assertEqual(item["resource"]["id"], str(self.workspace.id))
        self.assertIn("type", item["resource"])
        self.assertEqual(item["resource"]["type"], "workspace")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_with_fields_param(self, mock_permission):
        """Query param ?fields=role(name,id) strips unrequested top-level sections."""
        url = self._get_batch_create_url()
        response = self.client.post(
            f"{url}?fields=role(name,id)", self._valid_payload(), format="json", **self.headers
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item = response.data["role_bindings"][0]
        self.assertIn("id", item["role"])
        self.assertIn("name", item["role"])
        self.assertNotIn("subject", item)
        self.assertNotIn("resource", item)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_rollback_on_partial_failure(self, mock_permission):
        """One invalid subject in a batch rolls back the entire transaction."""
        url = self._get_batch_create_url()
        fake_group_id = str(uuid.uuid4())
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                },
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": fake_group_id, "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                },
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)

        expected_detail = f"group with id '{fake_group_id}' not found"
        expected = {
            "status": 404,
            "title": "Not found.",
            "detail": expected_detail,
            "errors": [{"message": expected_detail, "field": "detail"}],
        }
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, expected)

        self.assertFalse(
            RoleBinding.objects.filter(
                resource_id=str(self.workspace.id), resource_type="workspace", tenant=self.tenant
            ).exists(),
            "No bindings should exist after a failed batch",
        )

    def _assert_problem_details(self, response, expected_status, expected_detail, expected_field):
        """Assert the response matches the ProblemDetails format exactly."""
        TITLES = {400: "The request payload contains invalid syntax.", 404: "Not found."}
        expected = {
            "status": expected_status,
            "title": TITLES[expected_status],
            "detail": expected_detail,
            "errors": [{"message": expected_detail, "field": expected_field}],
        }
        self.assertEqual(response.status_code, expected_status)
        self.assertEqual(response.data, expected)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_empty_body_returns_400(self, mock_permission):
        """Empty JSON body returns 400."""
        url = self._get_batch_create_url()
        response = self.client.post(url, {}, format="json", **self.headers)
        self._assert_problem_details(response, 400, "This field is required.", "requests")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_empty_requests_returns_400(self, mock_permission):
        """Empty requests array returns 400 in ProblemDetails format."""
        url = self._get_batch_create_url()
        response = self.client.post(url, {"requests": []}, format="json", **self.headers)
        self._assert_problem_details(
            response, 400, "Ensure this field has at least 1 elements.", "requests.non_field_errors"
        )

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_over_100_items_returns_400(self, mock_permission):
        """Exceeding the max items limit returns 400."""
        url = self._get_batch_create_url()
        item = {
            "resource": {"id": str(self.workspace.id), "type": "workspace"},
            "subject": {"id": str(self.group.uuid), "type": "group"},
            "role": {"id": str(self.role.uuid)},
        }
        response = self.client.post(url, {"requests": [item] * 101}, format="json", **self.headers)
        self._assert_problem_details(
            response, 400, "Ensure this field has no more than 100 elements.", "requests.non_field_errors"
        )

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_missing_role_returns_400(self, mock_permission):
        """Missing role key in request item returns 400."""
        url = self._get_batch_create_url()
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(response, 400, "This field is required.", "requests.role")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_missing_resource_returns_400(self, mock_permission):
        """Missing resource key in request item returns 400."""
        url = self._get_batch_create_url()
        payload = {
            "requests": [
                {
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(response, 400, "This field is required.", "requests.resource")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_missing_subject_returns_400(self, mock_permission):
        """Missing subject key in request item returns 400."""
        url = self._get_batch_create_url()
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "role": {"id": str(self.role.uuid)},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(response, 400, "This field is required.", "requests.subject")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_invalid_uuid_returns_400(self, mock_permission):
        """Invalid UUID in role id returns 400."""
        url = self._get_batch_create_url()
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": "not-a-uuid"},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(response, 400, "Must be a valid UUID.", "requests.role.id")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_invalid_subject_type_returns_400(self, mock_permission):
        """Invalid subject type returns 400."""
        url = self._get_batch_create_url()
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "serviceaccount"},
                    "role": {"id": str(self.role.uuid)},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(response, 400, '"serviceaccount" is not a valid choice.', "requests.subject.type")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_invalid_role_returns_400(self, mock_permission):
        """Non-existent role UUID returns 400."""
        url = self._get_batch_create_url()
        fake_role_id = str(uuid.uuid4())
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": fake_role_id},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(
            response, 400, f"Invalid field 'roles': The following roles do not exist: {fake_role_id}", "roles"
        )

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_invalid_subject_returns_404(self, mock_permission):
        """Non-existent group UUID returns 404."""
        url = self._get_batch_create_url()
        fake_group_id = str(uuid.uuid4())
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": fake_group_id, "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(response, 404, f"group with id '{fake_group_id}' not found", "detail")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_invalid_user_returns_404(self, mock_permission):
        """Non-existent user UUID returns 404."""
        url = self._get_batch_create_url()
        fake_user_id = str(uuid.uuid4())
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": fake_user_id, "type": "user"},
                    "role": {"id": str(self.role.uuid)},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(response, 404, f"user with id '{fake_user_id}' not found", "detail")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_nonexistent_resource_returns_404(self, mock_permission):
        """Non-existent workspace UUID returns 404."""
        url = self._get_batch_create_url()
        fake_ws_id = str(uuid.uuid4())
        payload = {
            "requests": [
                {
                    "resource": {"id": fake_ws_id, "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                }
            ]
        }
        response = self.client.post(url, payload, format="json", **self.headers)
        self._assert_problem_details(response, 404, f"workspace with id '{fake_ws_id}' not found", "detail")


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class UpdateRoleBindingsBySubjectAPITests(IdentityRequest):
    """Tests for PUT /role-bindings/by-subject/ endpoint."""

    def setUp(self):
        """Set up test data using services."""
        reload(urls)
        clear_url_caches()
        super().setUp()

        # Clean up any existing bootstrap data to ensure fresh state
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant).delete()

        bootstrapped = V2TenantBootstrapService(InMemoryRelationReplicator()).bootstrap_tenant(self.tenant)
        self.root_workspace = bootstrapped.root_workspace
        self.default_workspace = bootstrapped.default_workspace
        self.client = APIClient()

        # Create workspace hierarchy (root and default from bootstrap)
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            description="Test workspace description",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        # Create permissions and roles using RoleV2Service
        Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        Permission.objects.create(permission="app:resource:write", tenant=self.tenant)

        from management.role.v2_service import RoleV2Service

        role_service = RoleV2Service()
        self.role1 = role_service.create(
            name="role1",
            description="Test role 1",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "read"}],
            tenant=self.tenant,
        )
        self.role2 = role_service.create(
            name="role2",
            description="Test role 2",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "write"}],
            tenant=self.tenant,
        )

        # Create group and principal
        self.group = Group.objects.create(
            name="test_group",
            description="Test group description",
            tenant=self.tenant,
        )
        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            user_id="testuser",
            type=Principal.Types.USER,
        )

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.filter(binding__tenant=self.tenant).delete()
        RoleBinding.objects.filter(tenant=self.tenant).delete()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def _get_by_subject_url(self):
        """Get the by-subject URL."""
        return reverse("v2_management:role-bindings-by-subject")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_success_for_group(self, mock_permission):
        """Test successful update for a group subject."""
        url = self._get_by_subject_url()
        response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": str(self.role1.uuid)}, {"id": str(self.role2.uuid)}]},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        actual = response.data
        actual["roles"] = sorted(actual["roles"], key=lambda r: str(r["id"]))
        expected_roles = sorted([{"id": self.role1.uuid}, {"id": self.role2.uuid}], key=lambda r: str(r["id"]))
        expected = {
            "subject": {"id": self.group.uuid, "type": "group"},
            "roles": expected_roles,
            "resource": {"id": str(self.workspace.id)},
        }
        self.assertEqual(actual, expected)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_empty_roles_removes_all_bindings(self, mock_permission):
        """Test that empty roles list removes all bindings for the subject."""
        url = self._get_by_subject_url()
        # First assign roles
        self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": str(self.role1.uuid)}]},
            format="json",
            **self.headers,
        )
        # Then remove all with empty roles
        response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": []},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["roles"], [])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_success_for_principal(self, mock_permission):
        """Test successful update for a principal/user subject."""
        url = self._get_by_subject_url()
        response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.principal.uuid}&subject_type=user",
            data={"roles": [{"id": str(self.role1.uuid)}]},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        expected = {
            "subject": {"id": self.principal.uuid, "type": "user"},
            "roles": [{"id": self.role1.uuid}],
            "resource": {"id": str(self.workspace.id)},
        }
        self.assertEqual(response.data, expected)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_replaces_existing_bindings(self, mock_permission):
        """Test that PUT replaces existing bindings."""
        url = self._get_by_subject_url()

        # First update with role1
        self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": str(self.role1.uuid)}]},
            format="json",
            **self.headers,
        )

        # Second update with role2 (should replace role1)
        response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": str(self.role2.uuid)}]},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only have role2 (role1 was replaced)
        expected = {
            "subject": {"id": self.group.uuid, "type": "group"},
            "roles": [{"id": self.role2.uuid}],
            "resource": {"id": str(self.workspace.id)},
        }
        self.assertEqual(response.data, expected)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_missing_required_query_params_returns_400(self, mock_permission):
        """Test that missing required query parameters return 400."""
        url = self._get_by_subject_url()
        body = {"roles": [{"id": str(self.role1.uuid)}]}

        # Each case: (query_string, missing_field, expected_message)
        test_cases = [
            # Missing resource_id
            (
                f"resource_type=workspace&subject_id={self.group.uuid}&subject_type=group",
                "resource_id",
                "resource_id is required (or use resource.tenant.org_id).",
            ),
            # Missing resource_type
            (
                f"resource_id={self.workspace.id}&subject_id={self.group.uuid}&subject_type=group",
                "resource_type",
                "resource_type is required (or use resource.tenant.org_id).",
            ),
            # Missing subject_id
            (
                f"resource_id={self.workspace.id}&resource_type=workspace&subject_type=group",
                "subject_id",
                "This field is required.",
            ),
            # Missing subject_type
            (
                f"resource_id={self.workspace.id}&resource_type=workspace&subject_id={self.group.uuid}",
                "subject_type",
                "This field is required.",
            ),
        ]

        for query_string, missing_field, expected_message in test_cases:
            with self.subTest(missing_field=missing_field):
                response = self.client.put(
                    f"{url}?{query_string}",
                    data=body,
                    format="json",
                    **self.headers,
                )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                expected = {
                    "status": 400,
                    "title": "The request payload contains invalid syntax.",
                    "detail": expected_message,
                    "errors": [{"message": expected_message, "field": missing_field}],
                    "instance": "/api/rbac/v2/role-bindings/by-subject/",
                }
                self.assertEqual(response.data, expected)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_missing_or_invalid_body_returns_400(self, mock_permission):
        """Test that missing or invalid request body returns 400."""
        url = self._get_by_subject_url()
        query_string = (
            f"resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group"
        )

        # Each case: (body, expected_field, expected_message)
        test_cases = [
            # Missing roles key
            (
                {},
                "roles",
                "This field is required.",
            ),
            # Empty body (None becomes {} in DRF)
            (
                None,
                "roles",
                "This field is required.",
            ),
            # Invalid UUID in role id
            (
                {"roles": [{"id": "not-a-uuid"}]},
                "roles.id",
                "Must be a valid UUID.",
            ),
        ]

        for body, expected_field, expected_message in test_cases:
            with self.subTest(body=body, expected_field=expected_field):
                response = self.client.put(
                    f"{url}?{query_string}",
                    data=body,
                    format="json",
                    **self.headers,
                )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                expected = {
                    "status": 400,
                    "title": "The request payload contains invalid syntax.",
                    "detail": expected_message,
                    "errors": [{"message": expected_message, "field": expected_field}],
                    "instance": "/api/rbac/v2/role-bindings/by-subject/",
                }
                self.assertEqual(response.data, expected)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_not_found_returns_404(self, mock_permission):
        """Test that non-existent entities return 404."""
        url = self._get_by_subject_url()

        # Each case: (description, query_params_fn, body_fn, detail_fn)
        # Using lambdas to generate dynamic UUIDs per test case
        def make_cases():
            fake_group_id = str(uuid.uuid4())
            fake_principal_id = str(uuid.uuid4())
            fake_workspace_id = str(uuid.uuid4())

            return [
                # Non-existent group
                (
                    "invalid_group",
                    f"resource_id={self.workspace.id}&resource_type=workspace"
                    f"&subject_id={fake_group_id}&subject_type=group",
                    {"roles": [{"id": str(self.role1.uuid)}]},
                    f"group with id '{fake_group_id}' not found",
                ),
                # Non-existent principal/user
                (
                    "invalid_principal",
                    f"resource_id={self.workspace.id}&resource_type=workspace"
                    f"&subject_id={fake_principal_id}&subject_type=user",
                    {"roles": [{"id": str(self.role1.uuid)}]},
                    f"user with id '{fake_principal_id}' not found",
                ),
                # Non-existent workspace
                (
                    "invalid_resource",
                    f"resource_id={fake_workspace_id}&resource_type=workspace"
                    f"&subject_id={self.group.uuid}&subject_type=group",
                    {"roles": [{"id": str(self.role1.uuid)}]},
                    f"workspace with id '{fake_workspace_id}' not found",
                ),
            ]

        for description, query_params, body, expected_detail in make_cases():
            with self.subTest(case=description):
                response = self.client.put(
                    f"{url}?{query_params}",
                    data=body,
                    format="json",
                    **self.headers,
                )

                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
                expected = {
                    "status": 404,
                    "title": "Not found.",
                    "detail": expected_detail,
                    "errors": [{"message": expected_detail, "field": "detail"}],
                    "instance": "/api/rbac/v2/role-bindings/by-subject/",
                }
                self.assertEqual(response.data, expected)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_domain_validation_returns_400(self, mock_permission):
        """Test that domain validation errors return 400."""
        url = self._get_by_subject_url()

        def make_cases():
            fake_role_id = str(uuid.uuid4())

            return [
                # Non-existent role
                (
                    "invalid_role",
                    f"resource_id={self.workspace.id}&resource_type=workspace"
                    f"&subject_id={self.group.uuid}&subject_type=group",
                    {"roles": [{"id": fake_role_id}]},
                    f"Invalid field 'roles': The following roles do not exist: {fake_role_id}",
                    "roles",
                ),
                # Unsupported subject type
                (
                    "unsupported_subject_type",
                    f"resource_id={self.workspace.id}&resource_type=workspace"
                    f"&subject_id={self.group.uuid}&subject_type=invalid_type",
                    {"roles": [{"id": str(self.role1.uuid)}]},
                    "Unsupported subject type: 'invalid_type'. Supported types: group, user",
                    "subject_type",
                ),
                # Invalid fields query parameter
                (
                    "invalid_fields_param",
                    f"resource_id={self.workspace.id}&resource_type=workspace"
                    f"&subject_id={self.group.uuid}&subject_type=group"
                    f"&fields=bogus_field",
                    {"roles": [{"id": str(self.role1.uuid)}]},
                    "Invalid field(s): Unknown field: 'bogus_field'."
                    " Valid resource fields: ['id', 'name', 'type']."
                    " Valid roles fields: ['id', 'name']."
                    " Valid sources fields: ['id', 'name', 'type']."
                    " Valid subject fields: ['group.description', 'group.name',"
                    " 'group.user_count', 'id', 'type', 'user.username']."
                    " Valid root fields: ['last_modified'].",
                    "fields",
                ),
            ]

        for description, query_params, body, expected_detail, expected_field in make_cases():
            with self.subTest(case=description):
                response = self.client.put(
                    f"{url}?{query_params}",
                    data=body,
                    format="json",
                    **self.headers,
                )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                expected = {
                    "status": 400,
                    "title": "The request payload contains invalid syntax.",
                    "detail": expected_detail,
                    "errors": [{"message": expected_detail, "field": expected_field}],
                    "instance": "/api/rbac/v2/role-bindings/by-subject/",
                }
                self.assertEqual(response.data, expected)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_nul_bytes_in_query_params_sanitized(self, mock_permission):
        """Test that NUL bytes in query parameters are stripped and the request succeeds."""
        url = self._get_by_subject_url()
        query_string = (
            f"resource_id={self.workspace.id}\x00&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group"
        )
        response = self.client.put(
            f"{url}?{query_string}",
            data={"roles": [{"id": str(self.role1.uuid)}]},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_duplicate_role_ids_are_deduplicated(self, mock_permission):
        """Test that duplicate role IDs in the payload are silently deduplicated."""
        url = self._get_by_subject_url()
        duplicate_role_id = str(self.role1.uuid)

        response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": duplicate_role_id}, {"id": duplicate_role_id}]},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Only one binding should be created despite the duplicate
        expected = {
            "subject": {"id": self.group.uuid, "type": "group"},
            "roles": [{"id": self.role1.uuid}],
            "resource": {"id": str(self.workspace.id)},
        }
        self.assertEqual(response.data, expected)

        # Verify only one RoleBinding row exists in the DB
        binding_count = RoleBinding.objects.filter(
            role=self.role1,
            resource_id=str(self.workspace.id),
            resource_type="workspace",
            tenant=self.tenant,
        ).count()
        self.assertEqual(binding_count, 1)

    # --- resource.tenant.org_id on PUT by-subject ---

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch("management.role_binding.service.RoleBindingService._validate_role_scopes")
    def test_update_with_resource_tenant_org_id(self, mock_scope_validation, mock_permission):
        """Test that PUT by-subject works with resource.tenant.org_id."""
        url = self._get_by_subject_url()
        org_id = self.tenant.org_id
        tenant_resource_id = Tenant.org_id_to_tenant_resource_id(org_id)

        response = self.client.put(
            f"{url}?resource.tenant.org_id={org_id}&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": str(self.role1.uuid)}]},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["resource"]["id"], tenant_resource_id)
        self.assertEqual(response.data["roles"], [{"id": self.role1.uuid}])

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_update_with_resource_tenant_org_id_and_resource_id_returns_400(self, mock_permission):
        """Test that combining resource.tenant.org_id with resource_id returns 400."""
        url = self._get_by_subject_url()
        response = self.client.put(
            f"{url}?resource.tenant.org_id=12345&resource_id=redhat/12345"
            f"&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": str(self.role1.uuid)}]},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_update_without_resource_params_returns_400(self, mock_permission):
        """Test that omitting all resource params returns 400 on PUT."""
        url = self._get_by_subject_url()
        response = self.client.put(
            f"{url}?subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": str(self.role1.uuid)}]},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


_SENTINEL = RuntimeError("_atomic_action sentinel")
_ATOMIC_ACTION_PATH = "management.v2_mixins.AtomicOperationsMixin._atomic_action"


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class RoleBindingViewSetAtomicWiringTests(IdentityRequest):
    """Verify RoleBindingViewSet write endpoints delegate to _atomic_action."""

    def setUp(self):
        reload(urls)
        clear_url_caches()
        super().setUp()
        bootstrapped = V2TenantBootstrapService(InMemoryRelationReplicator()).bootstrap_tenant(self.tenant)
        self.root_workspace = bootstrapped.root_workspace
        self.default_workspace = bootstrapped.default_workspace
        self.client = APIClient()

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        role_service = RoleV2Service()
        self.role = role_service.create(
            name="wiring_role",
            description="Test",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "read"}],
            tenant=self.tenant,
        )

        self.group = Group.objects.create(name="wiring_group", description="Test", tenant=self.tenant)
        self.principal = Principal.objects.create(
            username="wiring_user", tenant=self.tenant, user_id="wiring_user", type=Principal.Types.USER
        )

    def tearDown(self):
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        super().tearDown()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch(_ATOMIC_ACTION_PATH, side_effect=_SENTINEL)
    def test_batch_create_delegates_to_atomic_action(self, mock_atomic, _mock_permission):
        url = reverse("v2_management:role-bindings-batch-create")
        with self.assertRaises(RuntimeError, msg="_atomic_action sentinel"):
            self.client.post(
                url,
                {
                    "requests": [
                        {
                            "resource": {"id": str(self.workspace.id), "type": "workspace"},
                            "subject": {"id": str(self.group.uuid), "type": "group"},
                            "role": {"id": str(self.role.uuid)},
                        }
                    ]
                },
                format="json",
                **self.headers,
            )
        mock_atomic.assert_called_once()

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    @patch(_ATOMIC_ACTION_PATH, side_effect=_SENTINEL)
    def test_update_by_subject_delegates_to_atomic_action(self, mock_atomic, _mock_permission):
        url = reverse("v2_management:role-bindings-by-subject")
        with self.assertRaises(RuntimeError, msg="_atomic_action sentinel"):
            self.client.put(
                f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
                f"&subject_id={self.group.uuid}&subject_type=group",
                data={"roles": [{"id": str(self.role.uuid)}]},
                format="json",
                **self.headers,
            )
        mock_atomic.assert_called_once()


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True)
class RoleBindingAuditLogTests(TransactionalIdentityRequest):
    """Tests for role binding audit logging."""

    def setUp(self):
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()
        self.client = APIClient()

        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=self.root_workspace,
        )
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        self.permission1 = Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        self.permission2 = Permission.objects.create(permission="app:resource:write", tenant=self.tenant)

        self.role1 = RoleV2.objects.create(name="role1", description="Test role 1", tenant=self.tenant)
        self.role1.permissions.add(self.permission1)
        self.role2 = RoleV2.objects.create(name="role2", description="Test role 2", tenant=self.tenant)
        self.role2.permissions.add(self.permission2)

        self.group = Group.objects.create(
            name="test_group",
            description="Test group",
            tenant=self.tenant,
        )
        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            user_id="testuser",
            type=Principal.Types.USER,
        )

        TenantMapping.objects.get_or_create(tenant=self.tenant)

    def tearDown(self):
        AuditLog.objects.filter(tenant=self.tenant).delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.update(parent=None)
        Workspace.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def _assert_audit_log(self, action, description):
        audit_logs = self.client.get("/api/rbac/v1/auditlogs/", **self.headers).data["data"]

        matching = [
            log
            for log in audit_logs
            if log["action"] == action
            and log["description"] == description
            and log["resource_type"] == AuditLog.ROLE_BINDING
            and log["principal_username"] == self.user_data["username"]
        ]
        self.assertTrue(len(matching) > 0, f"No audit log found with action={action}, description={description}")

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_batch_create_audit_log(self, mock_permission):
        """Batch create produces an audit log entry with correct counts."""
        url = reverse("v2_management:role-bindings-batch-create")
        payload = {
            "requests": [
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role1.uuid)},
                },
                {
                    "resource": {"id": str(self.workspace.id), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role2.uuid)},
                },
            ]
        }

        response = self.client.post(url, payload, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self._assert_audit_log(
            action="create",
            description="Created 2 role binding(s) for 1 subject(s) on 1 resource(s)",
        )

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_update_by_subject_audit_log(self, mock_permission):
        """Update by subject produces an audit log entry with subject and role count."""
        url = reverse("v2_management:role-bindings-by-subject")
        response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.group.uuid}&subject_type=group",
            data={"roles": [{"id": str(self.role1.uuid)}, {"id": str(self.role2.uuid)}]},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(
            action="edit",
            description=(
                f"Updated role bindings for group 'test_group'" f" on workspace 'Test Workspace': 2 role(s) assigned"
            ),
        )

    @patch(
        "management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission",
        return_value=True,
    )
    def test_update_by_subject_user_audit_log(self, mock_permission):
        """Update by subject for a user uses username in the audit log description."""
        url = reverse("v2_management:role-bindings-by-subject")
        response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace"
            f"&subject_id={self.principal.uuid}&subject_type=user",
            data={"roles": [{"id": str(self.role1.uuid)}]},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(
            action="edit",
            description=(
                f"Updated role bindings for user 'testuser'" f" on workspace 'Test Workspace': 1 role(s) assigned"
            ),
        )
