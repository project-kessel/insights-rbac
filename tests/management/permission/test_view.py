#
# Copyright 2019 Red Hat, Inc.
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
"""Test the permission viewset."""

from unittest.mock import patch

from django.db.models import Q
from django.test.utils import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from api.models import User
from management.models import Group, Permission, Policy, Principal, Role, Access
from tests.identity_request import IdentityRequest

OPTION_URL = reverse("v1_management:permission-options")
LIST_URL = reverse("v1_management:permission-list")
CLIENT = APIClient()


class PermissionViewsetTests(IdentityRequest):
    """Test the permission viewset."""

    def setUp(self):
        """Set up the permission viewset tests."""
        super().setUp()
        request = self.request_context["request"]
        user = User()
        user.username = self.user_data["username"]
        user.account = self.customer_data["account_id"]
        request.user = user

        self.display_fields = {"application", "resource_type", "verb", "permission"}

        self.permissionA = Permission.objects.create(permission="rbac:roles:read", tenant=self.tenant)
        self.permissionB = Permission.objects.create(permission="rbac:*:*", tenant=self.tenant)
        self.permissionC = Permission.objects.create(permission="acme:*:*", tenant=self.tenant)
        self.permissionD = Permission.objects.create(permission="acme:*:write", tenant=self.tenant)
        self.permissionE = Permission.objects.create(permission="*:*:*", tenant=self.tenant)
        self.permissionF = Permission.objects.create(permission="*:bar:*", tenant=self.tenant)
        self.permissionG = Permission.objects.create(permission="*:*:baz", tenant=self.tenant)
        self.permissionH = Permission.objects.create(permission="*:bar:baz", tenant=self.tenant)
        self.permissionI = Permission.objects.create(
            permission="foo:bar:*", description="Description test.", tenant=self.tenant
        )
        self.permissionI.permissions.add(self.permissionA)
        self.permissionJ = Permission.objects.create(permission="cost-management:*:baz", tenant=self.tenant)

        self.roleA = Role.objects.create(name="roleA", tenant=self.tenant)
        self.roleB = Role.objects.create(name="roleB", tenant=self.tenant)

        self.accessA = Access.objects.create(permission=self.permissionA, role=self.roleA, tenant=self.tenant)
        self.accessB = Access.objects.create(permission=self.permissionB, role=self.roleA, tenant=self.tenant)
        self.accessC = Access.objects.create(permission=self.permissionC, role=self.roleA, tenant=self.tenant)

    def tearDown(self):
        """Tear down permission viewset tests."""
        Permission.objects.all().delete()

    def test_read_permission_list_success(self):
        """Test that we can read a list of permissions."""
        response = CLIENT.get(LIST_URL, **self.headers)

        # three parts in response: meta, links and data
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 10)

        for perm in response.data.get("data"):
            self.assertIsNotNone(perm.get("application"))
            self.assertIsNotNone(perm.get("resource_type"))
            self.assertIsNotNone(perm.get("verb"))
            self.assertIsNotNone(perm.get("permission"))
            if perm["permission"] == "foo:bar:*":
                self.assertEqual(perm["description"], "Description test.")
                self.assertEqual(perm["requires"], ["rbac:roles:read"])
            else:
                self.assertEqual(perm["description"], "")
                self.assertEqual(perm["requires"], [])

    def test_read_permission_list_application_filter(self):
        """Test that we can filter a list of permissions by application."""
        url = LIST_URL
        url = f"{url}?application=rbac"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)

    def test_read_permission_list_resource_type_filter(self):
        """Test that we can filter a list of permissions by resource_type."""
        url = LIST_URL
        url = f"{url}?resource_type=roles"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("data")[0].get("permission"), self.permissionA.permission)

    def test_read_permission_list_verb_filter(self):
        """Test that we can filter a list of permissions by verb."""
        url = LIST_URL
        url = f"{url}?verb=read"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("data")[0].get("permission"), self.permissionA.permission)

    def test_read_permission_list_permission_filter(self):
        """Test that we can filter a list of permissions by permission."""
        url = LIST_URL
        url = f"{url}?permission=rbac:*:*"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("data")[0].get("permission"), self.permissionB.permission)

    def test_list_permission_filter_alphabetical_order_success(self):
        """Test that the filtered list of permissions are in alphabetical order."""
        expected_permissions = list(
            Permission.objects.filter(application__in=["rbac", "acme"]).values_list("permission", flat=True)
        )
        url = LIST_URL
        url = f"{url}?application=rbac,acme"
        response = CLIENT.get(url, **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 4)
        expected_list = ["acme:*:*", "acme:*:write", "rbac:*:*", "rbac:roles:read"]
        self.assertCountEqual(expected_permissions, response_permissions)
        self.assertEqual(response_permissions, expected_list)

    def test_list_permission_filter_descending_order_success(self):
        """Test that the filtered list of permissions are in alphabetical order."""
        expected_permissions = list(
            Permission.objects.filter(application__in=["rbac", "acme"]).values_list("permission", flat=True)
        )
        url = LIST_URL
        url = f"{url}?application=rbac,acme&order_by=-permission"
        response = CLIENT.get(url, **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 4)
        expected_list = ["rbac:roles:read", "rbac:*:*", "acme:*:write", "acme:*:*"]
        self.assertCountEqual(expected_permissions, response_permissions)
        self.assertEqual(response_permissions, expected_list)

    def test_get_list_is_the_only_valid_method(self):
        """Test GET on /permissions/ is the only valid method."""
        response = CLIENT.post(LIST_URL, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        response = CLIENT.put(LIST_URL, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        response = CLIENT.delete(LIST_URL, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_filters_multiple_application_values(self):
        """Test that we can filter permissions with multiple application values."""
        expected_permissions = list(
            Permission.objects.filter(application__in=["rbac", "acme"]).values_list("permission", flat=True)
        )

        url = LIST_URL
        url = f"{url}?application=rbac,acme"
        response = CLIENT.get(url, **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 4)
        self.assertCountEqual(expected_permissions, response_permissions)

    def test_filters_multiple_resource_type_values(self):
        """Test that we can filter permissions with multiple resource_type values."""
        expected_permissions = list(
            Permission.objects.filter(resource_type__in=["roles", "*"]).values_list("permission", flat=True)
        )

        url = LIST_URL
        url = f"{url}?resource_type=roles,*"
        response = CLIENT.get(url, **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 7)
        self.assertCountEqual(expected_permissions, response_permissions)

    def test_filters_multiple_verb_values(self):
        """Test that we can filter permissions with multiple verb values."""
        expected_permissions = list(
            Permission.objects.values_list("permission", flat=True).filter(verb__in=["read", "write"])
        )

        url = LIST_URL
        url = f"{url}?verb=read,write"
        response = CLIENT.get(url, **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        self.assertCountEqual(expected_permissions, response_permissions)

    def test_query_invalid_field_fail(self):
        """Test that query invalid field fail."""

        url = f"{OPTION_URL}?field=invalid"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_query_without_field_fail(self):
        """Test that query invalid field fail."""

        url = f"{OPTION_URL}"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_fields_with_limit(self):
        """Test that we can obtain the expected field with pagination."""
        url = f"{OPTION_URL}?field=application&limit=1"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("meta").get("count"), 5)
        self.assertEqual(response.data.get("meta").get("limit"), 1)

    def test_get_fields_without_limit(self):
        """Test that we can obtain the expected field without pagination."""

        url = f"{OPTION_URL}?field=application"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(response.data.get("meta").get("limit"), 1000)

    def test_return_options_of_application(self):
        """Test that we can return options of application."""
        expected_all = Permission.objects.values_list("application", flat=True).distinct()
        expected_filtered = (
            Permission.objects.filter(verb__in=["read", "write"]).values_list("application", flat=True).distinct()
        )

        url_all = f"{OPTION_URL}?field=application"
        url_filtered = f"{OPTION_URL}?field=application&verb=read,write"
        response_all = CLIENT.get(url_all, **self.headers)
        response_filtered = CLIENT.get(url_filtered, **self.headers)

        self.assertEqual(response_all.status_code, status.HTTP_200_OK)
        self.assertEqual(response_filtered.status_code, status.HTTP_200_OK)
        self.assertCountEqual(expected_all, response_all.data.get("data"))
        self.assertCountEqual(expected_filtered, response_filtered.data.get("data"))

    def test_return_options_of_resource_type(self):
        """Test that we can return options of resource_type."""
        expected_all = Permission.objects.values_list("resource_type", flat=True).distinct()
        expected_filtered = (
            Permission.objects.filter(application="acme").values_list("resource_type", flat=True).distinct()
        )

        url_all = f"{OPTION_URL}?field=resource_type"
        url_filtered = f"{OPTION_URL}?field=resource_type&application=acme"
        response_all = CLIENT.get(url_all, **self.headers)
        response_filtered = CLIENT.get(url_filtered, **self.headers)

        self.assertEqual(response_all.status_code, status.HTTP_200_OK)
        self.assertEqual(response_filtered.status_code, status.HTTP_200_OK)
        self.assertCountEqual(expected_all, response_all.data.get("data"))
        self.assertCountEqual(expected_filtered, response_filtered.data.get("data"))

    def test_return_options_of_verb(self):
        """Test that we can return options of verb."""
        expected_all = Permission.objects.values_list("verb", flat=True).distinct()
        expected_filtered = Permission.objects.filter(resource_type="roles").values_list("verb", flat=True).distinct()

        url_all = f"{OPTION_URL}?field=verb"
        url_filtered = f"{OPTION_URL}?field=verb&resource_type=roles"
        response_all = CLIENT.get(url_all, **self.headers)
        response_filtered = CLIENT.get(url_filtered, **self.headers)

        self.assertEqual(response_all.status_code, status.HTTP_200_OK)
        self.assertEqual(response_filtered.status_code, status.HTTP_200_OK)
        self.assertCountEqual(expected_all, response_all.data.get("data"))
        self.assertCountEqual(expected_filtered, response_filtered.data.get("data"))

    def test_return_options_of_application_without_globals(self):
        """Test that we can return options of application without globals."""
        expected = (
            Permission.objects.values_list("application", flat=True)
            .distinct()
            .exclude(Q(application="*") | Q(resource_type="*") | Q(verb="*"))
        )

        url = f"{OPTION_URL}?field=application&exclude_globals=true"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(expected, response.data.get("data"))

    def test_return_options_of_resource_type_without_globals(self):
        """Test that we can return options of resource_type without globals."""
        expected = (
            Permission.objects.values_list("resource_type", flat=True)
            .distinct()
            .exclude(Q(application="*") | Q(resource_type="*") | Q(verb="*"))
        )

        url = f"{OPTION_URL}?field=resource_type&exclude_globals=true"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(expected, response.data.get("data"))

    def test_return_options_of_verb_without_globals(self):
        """Test that we can return options of verb without globals."""
        expected = (
            Permission.objects.values_list("verb", flat=True)
            .distinct()
            .exclude(Q(application="*") | Q(resource_type="*") | Q(verb="*"))
        )

        url = f"{OPTION_URL}?field=verb&exclude_globals=true"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(expected, response.data.get("data"))

    def test_return_options_with_comma_separated_filter(self):
        """Test that we can return options with comma separated filter."""
        expected = (
            Permission.objects.filter(resource_type__in=["roles", "*"]).values_list("verb", flat=True).distinct()
        )

        url = f"{OPTION_URL}?field=verb&resource_type=roles,*"
        response = CLIENT.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(expected, response.data.get("data"))

    def test_exclude_globals_filters_any_globals_out_when_true(self):
        """Test that we filter out any global permissions when exclude_globals=true."""
        expected = list(
            Permission.objects.filter(permission=self.permissionA.permission).values_list("permission", flat=True)
        )

        response = CLIENT.get(f"{LIST_URL}?exclude_globals=true", **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertCountEqual(expected, response_permissions)

    def test_exclude_globals_filters_no_globals_out_when_false(self):
        """Test that we do not filter out any global permissions when exclude_globals=false."""
        expected = list(Permission.objects.values_list("permission", flat=True))

        response = CLIENT.get(f"{LIST_URL}?exclude_globals=false", **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 10)
        self.assertCountEqual(expected, response_permissions)

    def test_exclude_globals_filters_no_globals_out_by_default(self):
        """Test that we do not filter out any global permissions when exclude_globals is unset."""
        expected = list(Permission.objects.values_list("permission", flat=True))

        response = CLIENT.get(LIST_URL, **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 10)
        self.assertCountEqual(expected, response_permissions)

    def test_exclude_globals_fails_with_invalid_value(self):
        """Test that we return a 400 when exclude_globals is not a supported value."""
        response = CLIENT.get(f"{LIST_URL}?exclude_globals=foo", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_exclude_roles_filters_to_permissions_not_in_roles(self):
        """Test that we filter out any permissions attached to the supplied role(s)."""
        access_list = [self.accessA, self.accessB, self.accessC]
        expected = list(Permission.objects.values_list("permission", flat=True))
        total_count = len(expected)

        response = CLIENT.get(f"{LIST_URL}?exclude_roles={self.roleA.uuid},{self.roleB.uuid}", **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(len(response.data.get("data")), total_count - len(access_list))
        for access in access_list:
            permission = Permission.objects.get(id=access.permission_id)
            self.assertTrue(permission.permission not in response_permissions)

    def test_exclude_roles_when_non_uuid_supplied(self):
        """Test that we respond correctly when invalid data is supplied."""
        response = CLIENT.get(f"{LIST_URL}?exclude_roles=abc123", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_allowed_only_filters_any_roles_not_in_allow_list_out_when_true(self):
        """Test that we filter out any permissions not in the allow list when allowed_only=true."""
        expected = list(
            Permission.objects.filter(permission=self.permissionJ.permission).values_list("permission", flat=True)
        )

        response = CLIENT.get(f"{LIST_URL}?allowed_only=true", **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertCountEqual(expected, response_permissions)

    def test_allowed_only_filters_any_roles_not_in_allow_list_out_when_true_in_chain(self):
        """
        Test that we filter out any permissions not in the allow list when allowed_only=true,
        chained with other filters.
        """
        response = CLIENT.get(f"{LIST_URL}?allowed_only=true&exclude_globals=true", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 0)

    def test_allowed_only_filters_no_permissions_out_when_false(self):
        """Test that we do not filter out any permissions not in the allow list when allowed_only=false."""
        expected = list(Permission.objects.values_list("permission", flat=True))

        response = CLIENT.get(f"{LIST_URL}?allowed_only=false", **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 10)
        self.assertCountEqual(expected, response_permissions)

    def test_allowed_only_filters_no_permissions_out_by_default(self):
        """Test that we do not filter out any permissions not in the allow list when allowed_only is unset."""
        expected = list(Permission.objects.values_list("permission", flat=True))

        response = CLIENT.get(LIST_URL, **self.headers)
        response_permissions = [p.get("permission") for p in response.data.get("data")]

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 10)
        self.assertCountEqual(expected, response_permissions)

    def test_allowed_only_fails_with_invalid_value(self):
        """Test that we return a 400 when allowed_only is not a supported value."""
        response = CLIENT.get(f"{LIST_URL}?allowed_only=foo", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_allowed_only_on_options_filters_any_roles_not_in_allow_list_out_when_true(self):
        """Test that we filter out any permissions not in the allow list when allowed_only=true."""
        expected = list(
            Permission.objects.filter(permission=self.permissionJ.permission)
            .values_list("application", flat=True)
            .distinct()
        )

        response = CLIENT.get(f"{OPTION_URL}?field=application&allowed_only=true", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertCountEqual(expected, response.data.get("data"))

    def test_allowed_only_on_options_filters_no_permissions_out_when_false(self):
        """Test that we do not filter out any permissions not in the allow list when allowed_only=false."""
        expected = Permission.objects.values_list("application", flat=True).distinct()

        response = CLIENT.get(f"{OPTION_URL}?field=application&allowed_only=false", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 5)
        self.assertCountEqual(expected, response.data.get("data"))

    def test_allowed_only_on_options_filters_no_permissions_out_by_default(self):
        """Test that we do not filter out any permissions not in the allow list when allowed_only is unset."""
        expected = Permission.objects.values_list("application", flat=True).distinct()

        response = CLIENT.get(f"{OPTION_URL}?field=application", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 5)
        self.assertCountEqual(expected, response.data.get("data"))

    def test_allowed_only_on_options_fails_with_invalid_value(self):
        """Test that we return a 400 when allowed_only is not a supported value."""
        response = CLIENT.get(f"{OPTION_URL}?field=application&allowed_only=foo", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    @patch("management.permission.view.is_v2_edit_enabled_for_request", return_value=True)
    def test_permission_list_v2_tenant_excludes_exclude_list_apps(self, _mock_v2):
        """V2 tenants do not see permissions whose application is in V2_MIGRATION_APP_EXCLUDE_LIST."""
        response = CLIENT.get(LIST_URL, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        apps = {p["application"] for p in response.data.get("data", [])}
        self.assertNotIn("cost-management", apps)
        expected_count = Permission.objects.exclude(application="cost-management").count()
        self.assertEqual(response.data["meta"]["count"], expected_count)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["cost-management"])
    @patch("management.permission.view.is_v2_edit_enabled_for_request", return_value=False)
    def test_permission_list_v1_tenant_unaffected_by_exclude_list(self, _mock_v2):
        """V1 tenants still see all permissions regardless of the exclude list."""
        response = CLIENT.get(LIST_URL, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        total = Permission.objects.count()
        self.assertEqual(response.data.get("meta", {}).get("count"), total)


class PermissionViewsetTestsNonAdmin(IdentityRequest):
    """Test the permission viewset."""

    def setUp(self):
        """Set up the permission viewset tests."""
        super().setUp()
        self.request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = self.request_context["request"]
        self.headers = request.META

        self.permission = Permission.objects.create(permission="rbac:roles:read", tenant=self.tenant)
        self.permission.save()

    def tearDown(self):
        """Tear down permission viewset tests."""
        Permission.objects.all().delete()

    def test_read_permission_list_fail(self):
        """Test that we can not read a list of permissions as a non-admin."""
        response = CLIENT.get(LIST_URL, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_read_permission_list_success_when_rbac_admin_role_assigned(self):
        """Test that we can read a list of permissions as a non-admin if he is assigned rbac admin role."""
        # Create admin group and add rbac admin permission to it
        group = Group.objects.create(name="Admin_group", tenant=self.tenant)
        policy = Policy.objects.create(name="Admin_policy", tenant=self.tenant, group=group)
        permission = Permission.objects.create(permission="rbac:*:*", tenant=self.tenant)
        admin_role = Role.objects.create(name="Admin_role", tenant=self.tenant)
        access = Access.objects.create(permission=permission, role=admin_role, tenant=self.tenant)
        policy.roles.add(admin_role)
        # Add principal to that admin group
        principal = Principal.objects.create(username=self.user_data["username"], tenant=self.tenant)
        group.principals.add(principal)
        response = CLIENT.get(LIST_URL, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_read_permission_options_list_fail(self):
        """Test that we can not read a list of filed options of permissions  as a non-admin."""
        url = f"{OPTION_URL}?field=application"
        response = CLIENT.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
