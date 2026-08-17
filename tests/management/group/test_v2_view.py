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

"""Tests for the Group V2 API."""

from importlib import reload
from unittest.mock import patch

from django.test.utils import override_settings
from django.urls import clear_url_caches
from management.models import Group, Principal, RoleBinding, RoleBindingGroup
from management.role.v2_model import CustomRoleV2
from rest_framework import status
from rest_framework.test import APIClient
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test

from api.models import Tenant
from rbac import urls

V2_URL = "/api/rbac/v2/groups/"


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class GroupV2ViewTests(IdentityRequest):
    """Test the Group V2 API."""

    def setUp(self):
        """Set up the group v2 tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_tenant_for_v2_test(self.tenant)

        self.enterContext(
            patch(
                "management.permissions.group_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )

        self.mock_check_access = self.enterContext(
            patch(
                "management.permissions.group_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        self.mock_dual_write_handler_cls = self.enterContext(
            patch("management.group.v2_service.RelationApiDualWriteGroupHandler")
        )

        self.user_principal_1 = Principal.objects.create(
            username="alice",
            type=Principal.Types.USER,
            user_id="100001",
            tenant=self.tenant,
        )
        self.user_principal_2 = Principal.objects.create(
            username="bob",
            type=Principal.Types.USER,
            user_id="100002",
            tenant=self.tenant,
        )

        self.group1 = Group.objects.create(
            name="group1",
            description="Test group 1",
            tenant=self.tenant,
        )
        self.group1.principals.add(self.user_principal_1)

        self.group2 = Group.objects.create(
            name="group2",
            description="Test group 2",
            tenant=self.tenant,
        )

        self.platform_default_group = Group.objects.create(
            name="platform-default-group",
            description="Platform default group",
            platform_default=True,
            tenant=self.tenant,
        )

        self.admin_default_group = Group.objects.create(
            name="admin-default-group",
            description="Admin default group",
            admin_default=True,
            system=True,
            tenant=self.tenant,
        )

    def test_list_all_groups(self):
        """List returns all groups for the tenant."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIn("data", data)
        self.assertGreaterEqual(len(data["data"]), 4)

        names = [g["name"] for g in data["data"]]
        self.assertIn("group1", names)
        self.assertIn("group2", names)

    def test_list_includes_admin_default_for_org_admin(self):
        """Admin default group appears in list for org admins."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [g["name"] for g in response.data["data"]]
        self.assertIn("admin-default-group", names)

    def test_list_excludes_other_tenant_groups(self):
        """Groups from another tenant are not visible."""
        other_tenant = Tenant.objects.create(
            tenant_name="other_tenant",
            org_id="other_org_id",
            account_id="other_account_id",
            ready=True,
        )
        other_group = Group.objects.create(name="other-group", tenant=other_tenant)

        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_ids = [g["id"] for g in response.data["data"]]
        self.assertNotIn(str(other_group.uuid), returned_ids)

        response = client.get(f"{V2_URL}{other_group.uuid}/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_filter_by_name_substring(self):
        """Filter by name uses case-insensitive substring match."""
        client = APIClient()
        response = client.get(f"{V2_URL}?name=group1", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["name"], "group1")

    def test_list_filter_by_name_glob(self):
        """Filter by name with glob pattern uses wildcard matching."""
        client = APIClient()
        response = client.get(f"{V2_URL}?name=group*", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data["data"]), 2)
        returned_names = [g["name"] for g in response.data["data"]]
        for name in returned_names:
            self.assertTrue(name.startswith("group"), f"Name '{name}' does not match glob 'group*'")
        self.assertNotIn("admin-default-group", returned_names)

    def test_retrieve_group(self):
        """Retrieve returns group details with principal_count."""
        client = APIClient()
        response = client.get(f"{V2_URL}{self.group1.uuid}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["id"], str(self.group1.uuid))
        self.assertEqual(data["name"], "group1")
        self.assertEqual(data["description"], "Test group 1")
        self.assertEqual(data["principal_count"], 1)
        self.assertIn("last_modified", data)

    def test_retrieve_group_with_no_principals(self):
        """Retrieve returns principal_count=0 for groups with no principals."""
        client = APIClient()
        response = client.get(f"{V2_URL}{self.group2.uuid}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["principal_count"], 0)

    def test_create_group(self):
        """Create a new group."""
        client = APIClient()
        payload = {
            "name": "new-group",
            "description": "A new test group",
        }
        response = client.post(V2_URL, data=payload, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data
        self.assertEqual(data["name"], "new-group")
        self.assertEqual(data["description"], "A new test group")
        self.assertEqual(data["principal_count"], 0)
        self.assertIn("id", data)

        group = Group.objects.get(uuid=data["id"])
        self.assertEqual(group.name, "new-group")
        self.assertEqual(group.tenant, self.tenant)

    def test_create_group_without_description(self):
        """Create a group without description (optional field)."""
        client = APIClient()
        payload = {"name": "minimal-group"}
        response = client.post(V2_URL, data=payload, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "minimal-group")
        self.assertEqual(response.data["description"], "")

        group = Group.objects.get(uuid=response.data["id"])
        self.assertEqual(group.description, "")
        self.assertEqual(group.tenant, self.tenant)

    def test_create_group_duplicate_name_same_tenant(self):
        """Creating a group with duplicate name in same tenant fails."""
        client = APIClient()
        payload = {"name": "group1"}
        response = client.post(V2_URL, data=payload, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)
        self.assertEqual(Group.objects.filter(name="group1", tenant=self.tenant).count(), 1)

    def test_create_group_as_platform_default_rejected(self):
        """Creating a group with platform_default=True is rejected."""
        client = APIClient()
        payload = {
            "name": "bad-platform-group",
            "platform_default": True,
        }
        response = client.post(V2_URL, data=payload, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Group.objects.filter(name="bad-platform-group").exists())

    def test_create_group_as_admin_default_rejected(self):
        """Creating a group with admin_default=True is rejected."""
        client = APIClient()
        payload = {
            "name": "bad-admin-group",
            "admin_default": True,
        }
        response = client.post(V2_URL, data=payload, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Group.objects.filter(name="bad-admin-group").exists())

    def test_update_group_name(self):
        """Update group name."""
        client = APIClient()
        payload = {"name": "group1-renamed"}
        response = client.put(f"{V2_URL}{self.group1.uuid}/", data=payload, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "group1-renamed")

        self.group1.refresh_from_db()
        self.assertEqual(self.group1.name, "group1-renamed")

    def test_update_group_description(self):
        """Update group description."""
        client = APIClient()
        payload = {
            "name": "group1",
            "description": "Updated description",
        }
        response = client.put(f"{V2_URL}{self.group1.uuid}/", data=payload, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["description"], "Updated description")

        self.group1.refresh_from_db()
        self.assertEqual(self.group1.description, "Updated description")

    def test_update_admin_default_group_rejected(self):
        """Updating admin_default group is rejected."""
        client = APIClient()
        payload = {"name": "admin-default-renamed"}
        response = client.put(
            f"{V2_URL}{self.admin_default_group.uuid}/",
            data=payload,
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.admin_default_group.refresh_from_db()
        self.assertEqual(self.admin_default_group.name, "admin-default-group")

    def test_update_platform_default_group_rejected(self):
        """Updating platform_default group is rejected."""
        client = APIClient()
        payload = {"name": "platform-default-renamed"}
        response = client.put(
            f"{V2_URL}{self.platform_default_group.uuid}/",
            data=payload,
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.platform_default_group.refresh_from_db()
        self.assertEqual(self.platform_default_group.name, "platform-default-group")

    def test_delete_group(self):
        """Delete a group."""
        client = APIClient()
        response = client.delete(f"{V2_URL}{self.group2.uuid}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Group.objects.filter(uuid=self.group2.uuid).exists())

    def test_delete_group_with_role_binding_rejected(self):
        """Deleting a group that has active role bindings is rejected."""
        role = CustomRoleV2.objects.create(
            name="test-role",
            description="Test role",
            tenant=self.tenant,
        )
        role_binding = RoleBinding.objects.create(
            role=role,
            resource_type="workspace",
            resource_id=str(self.tenant.tenant_resource_id()),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            binding=role_binding,
            group=self.group1,
        )

        client = APIClient()
        response = client.delete(f"{V2_URL}{self.group1.uuid}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)
        errors = response.data["errors"]
        self.assertGreater(len(errors), 0)
        error_message = str(errors[0])
        self.assertIn("role binding", error_message.lower())
        self.assertTrue(Group.objects.filter(uuid=self.group1.uuid).exists())

    def test_delete_admin_default_group_rejected(self):
        """Deleting admin_default group is rejected."""
        client = APIClient()
        response = client.delete(f"{V2_URL}{self.admin_default_group.uuid}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Group.objects.filter(uuid=self.admin_default_group.uuid).exists())

    def test_delete_platform_default_group_rejected(self):
        """Deleting platform_default group is rejected."""
        client = APIClient()
        response = client.delete(f"{V2_URL}{self.platform_default_group.uuid}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Group.objects.filter(uuid=self.platform_default_group.uuid).exists())

    def test_add_principals_to_group(self):
        """Add principals to a group."""
        client = APIClient()
        payload = {
            "principals": [
                {"id": str(self.user_principal_2.uuid)},
            ],
        }
        response = client.post(
            f"{V2_URL}{self.group2.uuid}/principals/",
            data=payload,
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.group2.refresh_from_db()
        self.assertIn(self.user_principal_2, self.group2.principals.all())

        mock_handler = self.mock_dual_write_handler_cls.return_value
        mock_handler.replicate_new_principals.assert_called_once()

    def test_add_multiple_principals_to_group(self):
        """Add multiple principals to a group."""
        client = APIClient()
        payload = {
            "principals": [
                {"id": str(self.user_principal_1.uuid)},
                {"id": str(self.user_principal_2.uuid)},
            ],
        }
        response = client.post(
            f"{V2_URL}{self.group2.uuid}/principals/",
            data=payload,
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.group2.refresh_from_db()
        member_uuids = set(self.group2.principals.values_list("uuid", flat=True))
        self.assertSetEqual(member_uuids, {self.user_principal_1.uuid, self.user_principal_2.uuid})

    def test_add_duplicate_principal_ids_accepted(self):
        """Adding the same principal UUID twice in one request succeeds (deduplication)."""
        client = APIClient()
        payload = {
            "principals": [
                {"id": str(self.user_principal_2.uuid)},
                {"id": str(self.user_principal_2.uuid)},
            ],
        }
        response = client.post(
            f"{V2_URL}{self.group2.uuid}/principals/",
            data=payload,
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIn(self.user_principal_2, self.group2.principals.all())

    def test_add_principal_nonexistent_principal(self):
        """Adding a principal that doesn't exist returns 400."""
        client = APIClient()
        from uuid import uuid4

        payload = {
            "principals": [
                {"id": str(uuid4())},
            ],
        }
        response = client.post(
            f"{V2_URL}{self.group2.uuid}/principals/",
            data=payload,
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.group2.principals.count(), 0)

    def test_add_principals_to_admin_default_rejected(self):
        """Adding principals to admin_default group is rejected."""
        client = APIClient()
        payload = {
            "principals": [
                {"id": str(self.user_principal_1.uuid)},
            ],
        }
        response = client.post(
            f"{V2_URL}{self.admin_default_group.uuid}/principals/",
            data=payload,
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.admin_default_group.principals.count(), 0)

    def test_remove_principals_from_group(self):
        """Remove principals from a group."""
        client = APIClient()
        response = client.delete(
            f"{V2_URL}{self.group1.uuid}/principals/{self.user_principal_1.uuid}/",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.group1.refresh_from_db()
        self.assertNotIn(self.user_principal_1, self.group1.principals.all())

        mock_handler = self.mock_dual_write_handler_cls.return_value
        mock_handler.replicate_removed_principals.assert_called_once()

    def test_remove_principal_not_in_group(self):
        """Removing a principal that's not in the group returns 404."""
        client = APIClient()
        response = client.delete(
            f"{V2_URL}{self.group2.uuid}/principals/{self.user_principal_1.uuid}/",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_remove_principals_from_admin_default_rejected(self):
        """Removing principals from admin_default group is rejected."""
        self.admin_default_group.principals.add(self.user_principal_1)

        client = APIClient()
        response = client.delete(
            f"{V2_URL}{self.admin_default_group.uuid}/principals/{self.user_principal_1.uuid}/",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(self.user_principal_1, self.admin_default_group.principals.all())

    def test_list_principals_in_group(self):
        """List principals in a group."""
        self.group1.principals.add(self.user_principal_2)

        client = APIClient()
        response = client.get(
            f"{V2_URL}{self.group1.uuid}/principals/",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(len(data["data"]), 2)
        usernames = [p["username"] for p in data["data"]]
        self.assertIn("alice", usernames)
        self.assertIn("bob", usernames)

    def test_list_with_ordering_param(self):
        """List groups honors the order_by parameter."""
        client = APIClient()

        response = client.get(f"{V2_URL}?order_by=group.name", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names_asc = [g["name"] for g in response.data["data"]]
        self.assertEqual(names_asc, sorted(names_asc))

        response = client.get(f"{V2_URL}?order_by=-group.name", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names_desc = [g["name"] for g in response.data["data"]]
        self.assertEqual(names_desc, sorted(names_desc, reverse=True))

    def test_list_denied_by_kessel(self):
        """List returns 403 when Kessel denies access."""
        self.mock_check_access.return_value = False

        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_denied_by_kessel(self):
        """Create returns 403 when Kessel denies access."""
        self.mock_check_access.return_value = False

        client = APIClient()
        payload = {"name": "denied-group"}
        response = client.post(V2_URL, data=payload, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Group.objects.filter(name="denied-group").exists())

    def test_retrieve_includes_all_fields(self):
        """Retrieve group includes all response fields."""
        client = APIClient()
        response = client.get(
            f"{V2_URL}{self.group1.uuid}/",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        # Check all expected fields are present
        self.assertIn("id", data)
        self.assertIn("name", data)
        self.assertIn("description", data)
        self.assertIn("principal_count", data)
        self.assertIn("created", data)
        self.assertIn("last_modified", data)
        self.assertIn("platform_default", data)
        self.assertIn("admin_default", data)
