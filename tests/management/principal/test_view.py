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
"""Test the principal viewset."""

from datetime import datetime
from unittest.mock import patch, ANY
from uuid import uuid4

from django.urls import reverse
from django.test.utils import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from api.common.pagination import StandardResultsSetPagination
from api.models import Tenant, User
from management.models import *
from management.principal.unexpected_status_code_from_it import UnexpectedStatusCodeFromITError
from tests.identity_request import IdentityRequest
from management.principal.proxy import PrincipalProxy


class PrincipalViewNonAdminTests(IdentityRequest):
    """Test the principal view for nonadmin user."""

    def setUp(self):
        """Set up the principal view nonadmin tests."""
        super().setUp()
        non_admin_tenant_name = "acct1234"
        self.non_admin_tenant = Tenant.objects.create(
            tenant_name=non_admin_tenant_name, account_id="1234", org_id="4321"
        )

        self.user_data = {"username": "non_admin", "email": "non_admin@example.com"}
        self.customer = {"account_id": "1234", "org_id": "4321", "tenant_name": non_admin_tenant_name}
        self.request_context = self._create_request_context(self.customer, self.user_data, is_org_admin=False)

        request = self.request_context["request"]
        self.headers = request.META

        self.principal = Principal(username="test_user", tenant=self.tenant)
        self.principal.save()

    def tearDown(self):
        """Tear down principal nonadmin viewset tests."""
        Principal.objects.all().delete()

    def test_non_admin_cannot_read_principal_list_without_permissions(self):
        """Test that we can not read a list of principals as a non-admin without permissions."""
        url = reverse("v1_management:principals")
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={"status_code": 200, "data": {"userCount": "1", "users": [{"username": "test_user"}]}},
    )
    def test_non_admin_can_read_principal_list_with_permissions(self, mock_request):
        """Test that we can read a list of principals as a non-admin with proper permissions."""
        non_admin_principal = Principal.objects.create(username="non_admin", tenant=self.non_admin_tenant)
        group = Group.objects.create(name="Non-admin group", tenant=self.non_admin_tenant)
        group.principals.add(non_admin_principal)
        policy = Policy.objects.create(name="Non-admin policy", group=group, tenant=self.non_admin_tenant)
        role = Role.objects.create(name="Non-admin role", tenant=self.non_admin_tenant)
        policy.roles.add(role)
        permission = Permission.objects.create(
            application="rbac",
            resource_type="principals",
            verb="read",
            permission="rbac:principal:read",
            tenant=self.non_admin_tenant,
        )
        access = Access.objects.create(permission=permission, role=role, tenant=self.non_admin_tenant)

        url = reverse("v1_management:principals")
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "admin_only": "false",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer["org_id"],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

        principal = response.data.get("data")[0]
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), self.principal.username)


class PrincipalViewsetTests(IdentityRequest):
    """Test the principal viewset."""

    def setUp(self):
        """Set up the principal viewset tests."""
        super().setUp()
        request = self.request_context["request"]
        user = User()
        user.username = self.user_data["username"]
        user.account = self.customer_data["account_id"]
        user.org_id = self.customer_data["org_id"]
        request.user = user

        self.principal = Principal(username="test_user", tenant=self.tenant)
        self.principal.save()

        # Create second tenant with 2 user based principals
        customer_data = self._create_customer_data()
        self.tenant_B = Tenant.objects.create(
            tenant_name="tenantB",
            account_id=customer_data["account_id"],
            org_id=customer_data["org_id"],
            ready=True,
        )
        self.principal_B1 = Principal.objects.create(username="test_user_B1", tenant=self.tenant_B)
        self.principal_B2 = Principal.objects.create(username="test_user_B2", tenant=self.tenant_B)

    def tearDown(self):
        """Tear down principal viewset tests."""
        Principal.objects.all().delete()

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user", "account_number": "1234", "org_id": "4321"}],
        },
    )
    def test_read_principal_list_success(self, mock_request):
        """Test that we can read a list of principals."""
        # Create a cross_account user in rbac.
        cross_account_principal = Principal.objects.create(
            username="cross_account_user", cross_account=True, tenant=self.tenant
        )

        url = reverse("v1_management:principals")
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "admin_only": "false",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )
        # /principals/ endpoint won't return the cross_account_principal, which does not exist in IT.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

        principal = response.data.get("data")[0]
        self.assertCountEqual(list(principal.keys()), ["username", "account_number", "org_id"])
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), self.principal.username)

        cross_account_principal.delete()

    def test_read_principal_list_username_only_true_success(self):
        """Test that we can read a list of principals with username_only=true."""
        url = f'{reverse("v1_management:principals")}?username_only=true'
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

        principal = response.data.get("data")[0]
        self.assertCountEqual(list(principal.keys()), ["username"])
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), self.principal.username)

        # Check we list only 1 principal even we have 3 in db (rest 2 belongs to different tenant)
        self.assertEqual(len(Principal.objects.all()), 3)

    @override_settings(BYPASS_BOP_VERIFICATION=True)
    def test_read_principal_list_username_only_false_success(self):
        """Test that we can read a list of principals with username_only=false."""
        url = f'{reverse("v1_management:principals")}?username_only=false'
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

        principal = response.data.get("data")[0]
        self.assertCountEqual(
            list(principal.keys()), ["username", "first_name", "last_name", "email", "user_id", "type"]
        )
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), self.principal.username)

    def test_read_principal_list_username_only_invalid(self):
        """Test that we get a 400 back with username_only=foo."""
        url = f'{reverse("v1_management:principals")}?username_only=foo'
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_read_principal_list_username_only_pagination(self):
        """Test the pagination is correct when we read a list of principals with username_only=true."""
        # Create few principals for the pagination test
        for i in range(5):
            Principal.objects.create(username=f"test_user{i}", tenant=self.tenant)
        # now in DB we have these principals:
        # 1) test_user   2) test_user0  3) test_user1
        # 4) test_user2  5) test_user3  6) test_user4

        client = APIClient()
        base_url = f'{reverse("v1_management:principals")}?username_only=true'

        # TEST 1
        limit = 2
        url = f"{base_url}&limit={limit}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # With limit=2 the response contains 2 principals from 6
        self.assertEqual(int(response.data.get("meta").get("count")), 6)
        self.assertEqual(len(response.data.get("data")), limit)

        principals = response.data.get("data")
        self.assertEqual(principals[0].get("username"), "test_user")
        self.assertEqual(principals[1].get("username"), "test_user0")

        # test that data contains only the 'username' and nothing else
        self.assertEqual(len(principals[0].keys()), 1)
        self.assertEqual(len(principals[1].keys()), 1)

        # TEST 2
        offset = 2
        limit = 3
        url = f"{base_url}&limit={limit}&offset={offset}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # With limit=3 the response contains 3 principals from 6
        self.assertEqual(int(response.data.get("meta").get("count")), 6)
        self.assertEqual(len(response.data.get("data")), limit)

        principals = response.data.get("data")
        self.assertEqual(principals[0].get("username"), "test_user1")
        self.assertEqual(principals[1].get("username"), "test_user2")
        self.assertEqual(principals[2].get("username"), "test_user3")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": {
                "userCount": "2",
                "users": [
                    {"username": "test_user1", "is_org_admin": "true"},
                    {"username": "test_user2", "is_org_admin": "false"},
                ],
            },
        },
    )
    def test_check_principal_admin(self, mock_request):
        """Test that we can read a list of principals."""
        url = reverse("v1_management:principals")
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "admin_only": "false",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 2)
        self.assertEqual(len(response.data.get("data")), 2)

        principal = response.data.get("data")[0]
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), "test_user1")
        self.assertIsNotNone(principal.get("is_org_admin"))
        self.assertTrue(principal.get("is_org_admin"))

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": [{"username": "test_user"}]},
    )
    def test_read_principal_filtered_list_success_without_cross_account_user(self, mock_request):
        """Test that we can read a filtered list of principals."""
        # Create a cross_account user in rbac.
        cross_account_principal = Principal.objects.create(
            username="cross_account_user", cross_account=True, tenant=self.tenant
        )

        url = f'{reverse("v1_management:principals")}?usernames=test_user,cross_account_user&offset=30'
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            ["test_user", "cross_account_user"],
            org_id=ANY,
            limit=10,
            offset=30,
            options={
                "limit": 10,
                "offset": 30,
                "sort_order": "asc",
                "status": "enabled",
                "username_only": "false",
                "principal_type": "user",
            },
        )
        # Cross account user won't be returned.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("meta").get("count"), 1)

        principal = response.data.get("data")[0]
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), "test_user")

        cross_account_principal.delete()

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": [{"username": "test_user"}]},
    )
    def test_read_principal_filtered_list_username_only_success_without_cross_account_user(self, mock_request):
        """Test that we can list usernames only for filtered principals without cross account user."""
        # Create a cross_account user
        cross_account_principal = Principal.objects.create(
            username="cross_account_user", cross_account=True, tenant=self.tenant
        )
        base_url = f'{reverse("v1_management:principals")}'
        username_only = "username_only=true"
        filter_principals = "usernames=test_user,cross_account_user"
        url = base_url + f"?{filter_principals}&{username_only}"
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            ["test_user", "cross_account_user"],
            org_id=ANY,
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "username_only": "true",
                "principal_type": "user",
            },
        )
        # Cross account user won't be returned.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("meta").get("count"), 1)

        principal = response.data.get("data")[0]
        self.assertEqual(len(principal.keys()), 1)  # we return only usernames, no other fields
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), "test_user")

        cross_account_principal.delete()

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": [{"username": "test_user1"}, {"username": "test_user2"}]},
    )
    def test_read_principal_filtered_list_with_untrimmed_values(self, mock_request):
        """Test that we can read a filtered list of principals and username values are processed as trimmed values."""
        client = APIClient()
        for gap in ("", " ", "     "):
            url = f'{reverse("v1_management:principals")}?usernames=test_user1,{gap}test_user2'
            response = client.get(url, **self.headers)
            # Regardless of the size of the gap between the values, the function is called with the same parameters
            # => the spaces are correctly removed before the function call.
            mock_request.assert_called_with(
                ["test_user1", "test_user2"],
                org_id=ANY,
                limit=10,
                offset=0,
                options={
                    "limit": 10,
                    "offset": 0,
                    "sort_order": "asc",
                    "status": "enabled",
                    "username_only": "false",
                    "principal_type": "user",
                },
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data.get("data")), 2)

        # The function is called three times in this test.
        self.assertEqual(mock_request.call_count, 3)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": [{"username": "test_user"}]},
    )
    def test_read_principal_filtered_list_success(self, mock_request):
        """Test that we can read a filtered list of principals."""
        url = f'{reverse("v1_management:principals")}?usernames=test_user75&offset=30'
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            ["test_user75"],
            org_id=ANY,
            limit=10,
            offset=30,
            options={
                "limit": 10,
                "offset": 30,
                "sort_order": "asc",
                "status": "enabled",
                "username_only": "false",
                "principal_type": "user",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("meta").get("count"), 1)

        principal = response.data.get("data")[0]
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), "test_user")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={"status_code": 200, "data": [{"username": "test_user"}]},
    )
    def test_read_principal_partial_matching(self, mock_request):
        """Test that we can read a list of principals by partial matching."""
        url = f'{reverse("v1_management:principals")}?usernames=test_us,no_op&offset=30&match_criteria=partial'
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            input={"principalStartsWith": "test_us"},
            limit=10,
            offset=30,
            options={
                "limit": 10,
                "offset": 30,
                "sort_order": "asc",
                "status": "enabled",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("meta").get("count"), 1)

        principal = response.data.get("data")[0]
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), "test_user")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={"status_code": 200, "data": [{"username": "test_user"}]},
    )
    def test_read_principal_multi_filter(self, mock_request):
        """Test that we can read a list of principals by partial matching."""
        url = f'{reverse("v1_management:principals")}?usernames=test_us&email=test&offset=30&match_criteria=partial'
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            input={"principalStartsWith": "test_us", "emailStartsWith": "test"},
            limit=10,
            offset=30,
            options={
                "limit": 10,
                "offset": 30,
                "sort_order": "asc",
                "status": "enabled",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("meta").get("count"), 1)

        principal = response.data.get("data")[0]
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), "test_user")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={"status_code": 200, "data": [{"username": "test_user"}]},
    )
    def test_bad_query_param_limit(self, mock_request):
        """Test handling of bad limit. Invalid limit value should be replaced by the default limit."""
        default_limit = StandardResultsSetPagination.default_limit
        client = APIClient()

        for limit in ["foo", -10, 0, ""]:
            url = f'{reverse("v1_management:principals")}?limit={limit}'
            response = client.get(url, **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data.get("meta").get("limit"), default_limit)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={"status_code": 200, "data": [{"username": "test_user"}]},
    )
    def test_bad_query_param_offset(self, mock_request):
        """Test handling of bad offset. Invalid offset value should be replaced by the default offset value."""
        client = APIClient()

        for offset in ["foo", -10, 0, ""]:
            url = f'{reverse("v1_management:principals")}?offset={offset}'
            response = client.get(url, **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data.get("meta").get("offset"), 0)

    def test_bad_query_param_of_sort_order(self):
        """Test handling of bad query params."""
        url = f'{reverse("v1_management:principals")}?sort_order=det'
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={"status_code": status.HTTP_500_INTERNAL_SERVER_ERROR, "errors": [{"detail": "error"}]},
    )
    def test_read_principal_list_fail(self, mock_request):
        """Test that we can handle a failure with listing principals."""
        url = reverse("v1_management:principals")
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        error = response.data.get("errors")[0]
        self.assertIsNotNone(error.get("detail"))

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user", "account_number": "1234", "org_id": "4321", "id": "5678"}],
        },
    )
    def test_read_principal_list_account(self, mock_request):
        """Test that we can handle a request with matching accounts"""
        url = f'{reverse("v1_management:principals")}?usernames=test_user&offset=30&sort_order=desc'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            ["test_user"],
            org_id=ANY,
            limit=10,
            offset=30,
            options={
                "limit": 10,
                "offset": 30,
                "sort_order": "desc",
                "status": "enabled",
                "username_only": "false",
                "principal_type": "user",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        resp = proxy._process_data(response.data.get("data"), org_id="4321", org_id_filter=True, return_id=True)
        self.assertEqual(len(resp), 1)

        self.assertEqual(resp[0]["username"], "test_user")
        self.assertEqual(resp[0]["user_id"], "5678")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user", "account_number": "54321", "org_id": "54322"}],
        },
    )
    def test_read_principal_list_account_fail(self, mock_request):
        """Test that we can handle a request with matching accounts"""
        url = f'{reverse("v1_management:principals")}?usernames=test_user&offset=30'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        resp = proxy._process_data(response.data.get("data"), org_id="4321", org_id_filter=True)
        self.assertEqual(len(resp), 0)

        self.assertNotEqual(resp, "test_user")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user", "account_number": "54321", "org_id": "54322"}],
        },
    )
    def test_read_principal_list_account_filter(self, mock_request):
        """Test that we can handle a request with matching accounts"""
        url = f'{reverse("v1_management:principals")}?usernames=test_user&offset=30'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        resp = proxy._process_data(response.data.get("data"), org_id="4321", org_id_filter=False)

        self.assertEqual(len(resp), 1)

        self.assertEqual(resp[0]["username"], "test_user")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user", "account_number": "54321", "org_id": "54322"}],
        },
    )
    def test_read_principal_list_by_email(self, mock_request):
        """Test that we can handle a request with an email address"""
        url = f'{reverse("v1_management:principals")}?email=test_user@example.com'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        resp = proxy._process_data(response.data.get("data"), org_id="54322", org_id_filter=False)
        self.assertEqual(len(resp), 1)

        mock_request.assert_called_once_with(
            input={"primaryEmail": "test_user@example.com"},
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )

        self.assertEqual(resp[0]["username"], "test_user")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": {"userCount": "1", "users": [{"username": "test_user", "is_org_admin": "true"}]},
        },
    )
    def test_read_users_of_desired_status(self, mock_request):
        """Test that we can return users of desired status within an account"""
        url = f'{reverse("v1_management:principals")}?status=disabled'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(response.data.get("meta").get("count"), "1")
        mock_request.assert_called_once_with(
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "disabled",
                "admin_only": "false",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": {"userCount": "1", "users": [{"username": "test_user", "is_org_admin": "true"}]},
        },
    )
    def test_principal_default_status_enabled(self, mock_request):
        """Tests when not passing in status the user active status will be enabled"""
        url = f'{reverse("v1_management:principals")}'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(response.data.get("meta").get("count"), "1")
        mock_request.assert_called_once_with(
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "admin_only": "false",
                "status": "enabled",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": {"userCount": "1", "users": [{"username": "test_user", "is_org_admin": "true"}]},
        },
    )
    def test_read_list_of_admins(self, mock_request):
        """Test that we can return only org admins within an account"""
        url = f'{reverse("v1_management:principals")}?admin_only=true'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(response.data.get("meta").get("count"), "1")
        mock_request.assert_called_once_with(
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "admin_only": "true",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )

    def test_read_users_with_invalid_status_value(self):
        """Test that reading user with invalid status value returns 400"""
        url = f'{reverse("v1_management:principals")}?status=invalid'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_read_users_with_invalid_admin_only_value(self):
        """Test that reading user with invalid status value returns 400"""
        url = f'{reverse("v1_management:principals")}?admin_only=invalid'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "account_number": "54321",
                    "org_id": "54322",
                    "email": "test_user@example.com",
                }
            ],
        },
    )
    def test_read_principal_list_by_email_partial_matching(self, mock_request):
        """Test that we can handle a request with a partial email address"""
        url = f'{reverse("v1_management:principals")}?email=test_use&match_criteria=partial'
        client = APIClient()
        proxy = PrincipalProxy()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        resp = proxy._process_data(response.data.get("data"), org_id="54322", org_id_filter=False)
        self.assertEqual(len(resp), 1)

        mock_request.assert_called_once_with(
            input={"emailStartsWith": "test_use"},
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )

        self.assertEqual(resp[0]["username"], "test_user")

    def test_read_principal_invalid_type_query_params(self):
        """
        Test that when an invalid "principal type" query parameter is specified,
        a bad request response is returned
        """
        url = reverse("v1_management:principals")
        client = APIClient()

        invalidQueryParams = ["hello", "world", "service-accounts", "users"]
        for invalidQueryParam in invalidQueryParams:
            response = client.get(url, {"type": invalidQueryParam}, **self.headers)

            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user", "account_number": "1234", "org_id": "4321"}],
        },
    )
    def test_read_principal_users(self, mock_request):
        """Test that when the "user" query parameter is specified, the real users are returned."""
        # Create a cross_account user in rbac.
        cross_account_principal = Principal.objects.create(
            username="cross_account_user", cross_account=True, tenant=self.tenant
        )

        url = reverse("v1_management:principals")
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            limit=10,
            offset=0,
            options={
                "limit": 10,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "admin_only": "false",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.customer_data["org_id"],
        )
        # /principals/ endpoint won't return the cross_account_principal, which does not exist in IT.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

        principal = response.data.get("data")[0]
        self.assertCountEqual(list(principal.keys()), ["username", "account_number", "org_id"])
        self.assertIsNotNone(principal.get("username"))
        self.assertEqual(principal.get("username"), self.principal.username)

        cross_account_principal.delete()

    def test_read_principal_invalid_type(self):
        """Test that an invalid principal's type returns an error response."""
        invalid_type = "invalid_value"
        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type={invalid_type}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        error_message = response.json().get("errors")[0].get("detail")
        expected_substring = f"type query parameter value '{invalid_type}' is invalid."
        self.assertIn(expected_substring, error_message)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    def test_read_principal_100(self, mock_request):
        """Test we can read 100 principals in one query."""
        # Principal Proxy mock that returns 100 principals
        principals_mock_100 = [
            {"username": f"test_user_{i}", "account_number": self.tenant.account_id, "org_id": self.tenant.org_id}
            for i in range(1, 101)
        ]
        mock_request.return_value = {"status_code": 200, "data": principals_mock_100}

        url = reverse("v1_management:principals") + "?limit=100"
        client = APIClient()
        response = client.get(url, **self.headers)

        mock_request.assert_called_once_with(
            limit=100,
            offset=0,
            options={
                "limit": 100,
                "offset": 0,
                "sort_order": "asc",
                "status": "enabled",
                "admin_only": "false",
                "username_only": "false",
                "principal_type": "user",
            },
            org_id=self.tenant.org_id,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response.data.get("meta").get("count")), 100)
        self.assertEqual(len(response.data.get("data")), 100)

    def test_read_principal_username_only_100(self):
        """Test we can read username only for 100 principals."""
        # Create 100 principals for the test in database.
        principals_count_we_need_to_create = 100 - len(Principal.objects.filter(tenant=self.tenant))

        created_principals = []
        for i in range(principals_count_we_need_to_create):
            principal = Principal.objects.create(username=f"test_user_{i + 1}", tenant=self.tenant)
            created_principals.append(principal)

        client = APIClient()
        # with different limit we get same count
        count = 100
        for limit in (1000, 100, 20, 11, 10, 9, 5, 1):
            url = reverse("v1_management:principals") + f"?username_only=true&limit={limit}"
            response = client.get(url, **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(int(response.data.get("meta").get("count")), count)
            expected_count = min(limit, count)
            self.assertEqual(len(response.data.get("data")), expected_count)

        # with different offset we get same count
        count = 100
        for offset in (1000, 100, 99, 20, 11, 10, 9, 5, 1, 0):
            url = reverse("v1_management:principals") + f"?username_only=true&offset={offset}"
            response = client.get(url, **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(int(response.data.get("meta").get("count")), count)
            expected_count = min(10, count - offset) if count - offset > 0 else 0
            self.assertEqual(len(response.data.get("data")), expected_count)

        # Remove created principals
        for principal in created_principals:
            principal.delete()


class PrincipalViewsetServiceAccountTests(IdentityRequest):
    """Tests the principal view set - only service accounts tests"""

    def setUp(self):
        """Set up the principal viewset tests for service accounts."""
        super().setUp()
        self.sa_client_ids = [
            "1c8d4c5a-1602-4ba3-8766-0c894612d4f5",
            "d907e308-fe91-41e4-8282-686d7dd56b13",
            "ae50f3e0-4b37-45f4-a3db-1d684d4b39bd",
        ]

        for uuid in self.sa_client_ids:
            Principal.objects.create(
                username="service_account-" + uuid,
                tenant=self.tenant,
                type="service-account",
                service_account_id=uuid,
            )

        self.mocked_values = []
        for uuid in self.sa_client_ids:
            self.mocked_values.append(
                {
                    "clientId": uuid,
                    "name": f"service_account_name_{uuid.split('-')[0]}",
                    "description": f"Service Account description {uuid.split('-')[0]}",
                    "owner": "jsmith",
                    "username": "service_account-" + uuid,
                    "time_created": 1706784741,
                    "type": "service-account",
                }
            )

    def tearDown(self):
        """Tear down principal viewset tests for service accounts."""
        Principal.objects.all().delete()

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_filter_by_name(self, mock_request):
        """Test that we can filter service accounts by name"""
        mock_request.return_value = self.mocked_values

        sa_id = self.sa_client_ids[0]
        test_name = f"service_account_name_{sa_id.split('-')[0]}"
        url = f"{reverse('v1_management:principals')}?type=service-account&name={test_name}"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)

        sa = response.data.get("data")[0]
        self.assertCountEqual(
            list(sa.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        sa_id = self.sa_client_ids[0]
        self.assertEqual(sa.get("clientId"), sa_id)
        self.assertEqual(sa.get("name"), f"service_account_name_{sa_id.split('-')[0]}")
        self.assertEqual(sa.get("description"), f"Service Account description {sa_id.split('-')[0]}")
        self.assertEqual(sa.get("owner"), "jsmith")
        self.assertEqual(sa.get("type"), "service-account")
        self.assertEqual(sa.get("username"), "service_account-" + sa_id)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_filter_by_owner(self, mock_request):
        """Test that we can filter service accounts by owner"""
        mocked_values = self.mocked_values
        mocked_values[0]["owner"] = "test_owner"
        mock_request.return_value = mocked_values

        test_owner = "test_owner"
        url = f"{reverse('v1_management:principals')}?type=service-account&owner={test_owner}"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)

        sa = response.data.get("data")[0]
        self.assertCountEqual(
            list(sa.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        sa_id1 = self.sa_client_ids[0]
        self.assertEqual(sa.get("clientId"), sa_id1)
        self.assertEqual(sa.get("name"), f"service_account_name_{sa_id1.split('-')[0]}")
        self.assertEqual(sa.get("description"), f"Service Account description {sa_id1.split('-')[0]}")
        self.assertEqual(sa.get("owner"), test_owner)
        self.assertEqual(sa.get("type"), "service-account")
        self.assertEqual(sa.get("username"), "service_account-" + sa_id1)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_filter_by_owner_wrong_returns_empty(self, mock_request):
        """Test that we can filter service accounts by owner with wrong input returns an empty array"""
        mock_request.return_value = self.mocked_values

        url = f"{reverse('v1_management:principals')}?type=service-account&owner=wrong_owner"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 0)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_filter_by_name_wrong_returns_empty(self, mock_request):
        """Test that we can filter service accounts by name with wrong input returns an empty array"""
        # Create SA in the database
        sa_client_id = "b6636c60-a31d-013c-b93d-6aa2427b506c"
        sa_username = "service_account-" + sa_client_id

        Principal.objects.create(
            username=sa_username,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_id,
        )

        mock_request.return_value = [
            {
                "clientId": sa_client_id,
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username,
                "time_created": 1706784741,
                "type": "service-account",
            }
        ]

        url = f"{reverse('v1_management:principals')}?type=service-account&name=wrong_name"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 0)
        self.assertEqual(len(response.data.get("data")), 0)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_filter_by_owner_with_limit_offset(self, mock_request):
        mock_request.return_value = self.mocked_values
        test_owner = "jsmith"
        url = f"{reverse('v1_management:principals')}?type=service-account&owner={test_owner}&limit=2&offset=1"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 3)
        self.assertEqual(len(response.data.get("data")), 2)

        sa = response.data.get("data")[0]
        self.assertCountEqual(
            list(sa.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        sa_id = self.sa_client_ids[1]
        self.assertEqual(sa.get("clientId"), sa_id)
        self.assertEqual(sa.get("name"), f"service_account_name_{sa_id.split('-')[0]}")
        self.assertEqual(sa.get("description"), f"Service Account description {sa_id.split('-')[0]}")
        self.assertEqual(sa.get("owner"), test_owner)
        self.assertEqual(sa.get("type"), "service-account")
        self.assertEqual(sa.get("username"), "service_account-" + sa_id)

        sa2 = response.data.get("data")[1]
        self.assertCountEqual(
            list(sa.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        sa_id2 = self.sa_client_ids[2]
        self.assertEqual(sa2.get("clientId"), sa_id2)
        self.assertEqual(sa2.get("name"), f"service_account_name_{sa_id2.split('-')[0]}")
        self.assertEqual(sa2.get("description"), f"Service Account description {sa_id2.split('-')[0]}")
        self.assertEqual(sa2.get("owner"), test_owner)
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), "service_account-" + sa_id2)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_filter_by_description(self, mock_request):
        """Test that we can filter service accounts by description"""
        mock_request.return_value = self.mocked_values

        sa_id = self.sa_client_ids[0]
        test_description = f"Service Account description {sa_id.split('-')[0]}"
        url = f"{reverse('v1_management:principals')}?type=service-account&description={test_description}"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

        sa = response.data.get("data")[0]
        self.assertCountEqual(
            list(sa.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa.get("clientId"), sa_id)
        self.assertEqual(sa.get("name"), f"service_account_name_{sa_id.split('-')[0]}")
        self.assertEqual(sa.get("description"), test_description)
        self.assertEqual(sa.get("owner"), "jsmith")
        self.assertEqual(sa.get("type"), "service-account")
        self.assertEqual(sa.get("username"), f"service_account-{sa_id}")

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_filter_by_owner_name_description(self, mock_request):
        """Test that we can filter service accounts by all filter options"""
        mock_request.return_value = self.mocked_values

        sa_id = self.sa_client_ids[0]
        test_name = f"service_account_name_{sa_id.split('-')[0]}"
        test_owner = "jsmith"
        test_description = f"Service Account description {sa_id.split('-')[0]}"
        url = f"{reverse('v1_management:principals')}?type=service-account&name={test_name}&owner={test_owner}&description={test_description}"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

        sa = response.data.get("data")[0]
        self.assertCountEqual(
            list(sa.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa.get("clientId"), sa_id)
        self.assertEqual(sa.get("name"), test_name)
        self.assertEqual(sa.get("description"), test_description)
        self.assertEqual(sa.get("owner"), test_owner)
        self.assertEqual(sa.get("type"), "service-account")
        self.assertEqual(sa.get("username"), f"service_account-{sa_id}")

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_time_created_desc(self, mock_request):
        """Test that we can sort service accounts by time_created descending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []
        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=-time_created"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa1.get("name"), "service_account_name")
        self.assertEqual(sa1.get("description"), "Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username1)
        self.assertEqual(sa1.get("time_created"), 1706784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa2.get("name"), "service_account_name")
        self.assertEqual(sa2.get("description"), "Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username2)
        self.assertEqual(sa2.get("time_created"), 1306784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_time_created_asc(self, mock_request):
        """Test that we can sort service accounts by time_created ascending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=time_created"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[1]
        sa2 = response.data.get("data")[0]

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa2.get("name"), "service_account_name")
        self.assertEqual(sa2.get("description"), "Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username2)
        self.assertEqual(sa2.get("time_created"), 1306784741)

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa1.get("name"), "service_account_name")
        self.assertEqual(sa1.get("description"), "Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username1)
        self.assertEqual(sa1.get("time_created"), 1706784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_owner_asc(self, mock_request):
        """Test that we can sort service accounts by owner ascending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "acasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=owner"
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa1.get("name"), "service_account_name")
        self.assertEqual(sa1.get("description"), "Service Account description")
        self.assertEqual(sa1.get("owner"), "acasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username1)
        self.assertEqual(sa1.get("time_created"), 1706784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa2.get("name"), "service_account_name")
        self.assertEqual(sa2.get("description"), "Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username2)
        self.assertEqual(sa2.get("time_created"), 1306784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_owner_desc(self, mock_request):
        """Test that we can sort service accounts by owner descending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "acasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=-owner"
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa1.get("name"), "service_account_name")
        self.assertEqual(sa1.get("description"), "Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username1)
        self.assertEqual(sa1.get("time_created"), 1706784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa2.get("name"), "service_account_name")
        self.assertEqual(sa2.get("description"), "Service Account description")
        self.assertEqual(sa2.get("owner"), "acasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username2)
        self.assertEqual(sa2.get("time_created"), 1306784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_name_asc(self, mock_request):
        """Test that we can sort service accounts by name ascending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "a_service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "z_service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=name"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa1.get("name"), "a_service_account_name")
        self.assertEqual(sa1.get("description"), "Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username1)
        self.assertEqual(sa1.get("time_created"), 1706784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa2.get("name"), "z_service_account_name")
        self.assertEqual(sa2.get("description"), "Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username2)
        self.assertEqual(sa2.get("time_created"), 1306784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_name_desc(self, mock_request):
        """Test that we can sort service accounts by name descending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "a_service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "z_service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=-name"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa1.get("name"), "z_service_account_name")
        self.assertEqual(sa1.get("description"), "Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username2)
        self.assertEqual(sa1.get("time_created"), 1306784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa2.get("name"), "a_service_account_name")
        self.assertEqual(sa2.get("description"), "Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username1)
        self.assertEqual(sa2.get("time_created"), 1706784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_description_asc(self, mock_request):
        """Test that we can sort service accounts by description ascending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "a_service_account_name",
                "description": "A Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "z_service_account_name",
                "description": "Z Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=description"
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa1.get("name"), "a_service_account_name")
        self.assertEqual(sa1.get("description"), "A Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username1)
        self.assertEqual(sa1.get("time_created"), 1706784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa2.get("name"), "z_service_account_name")
        self.assertEqual(sa2.get("description"), "Z Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username2)
        self.assertEqual(sa2.get("time_created"), 1306784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_name_desc(self, mock_request):
        """Test that we can sort service accounts by name descending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "a_service_account_name",
                "description": "A Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "z_service_account_name",
                "description": "Z Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=-name"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa1.get("name"), "z_service_account_name")
        self.assertEqual(sa1.get("description"), "Z Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username2)
        self.assertEqual(sa1.get("time_created"), 1306784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa2.get("name"), "a_service_account_name")
        self.assertEqual(sa2.get("description"), "A Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username1)
        self.assertEqual(sa2.get("time_created"), 1706784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_clientid_asc(self, mock_request):
        """Test that we can sort service accounts by clientId ascending"""
        # Create SA in the database
        sa_client_ids = ["b6636c60-a31d-013c-b93d-6aa2427b506c", "69a116a0-a3d4-013c-b940-6aa2427b506c"]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=clientId"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa1.get("name"), "service_account_name")
        self.assertEqual(sa1.get("description"), "Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username2)
        self.assertEqual(sa1.get("time_created"), 1306784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa2.get("name"), "service_account_name")
        self.assertEqual(sa2.get("description"), "Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username1)
        self.assertEqual(sa2.get("time_created"), 1706784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_principal_service_account_sort_by_clientid_desc(self, mock_request):
        """Test that we can sort service accounts by clientId descending"""
        # Create SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
        ]
        sa_username1 = "service_account-" + sa_client_ids[0]
        sa_username2 = "service_account-" + sa_client_ids[1]

        Principal.objects.create(
            username=sa_username1,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[0],
        )

        Principal.objects.create(
            username=sa_username2,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_ids[1],
        )

        mocked_values = []

        mocked_values.append(
            {
                "clientId": sa_client_ids[0],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username1,
                "time_created": 1706784741,
                "type": "service-account",
            }
        )

        mocked_values.append(
            {
                "clientId": sa_client_ids[1],
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "ecasey",
                "username": sa_username2,
                "time_created": 1306784741,
                "type": "service-account",
            }
        )
        mock_request.return_value = mocked_values
        url = f"{reverse('v1_management:principals')}?type=service-account&order_by=-clientId"

        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        sa1 = response.data.get("data")[0]
        sa2 = response.data.get("data")[1]

        self.assertCountEqual(
            list(sa1.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa1.get("clientId"), sa_client_ids[0])
        self.assertEqual(sa1.get("name"), "service_account_name")
        self.assertEqual(sa1.get("description"), "Service Account description")
        self.assertEqual(sa1.get("owner"), "ecasey")
        self.assertEqual(sa1.get("type"), "service-account")
        self.assertEqual(sa1.get("username"), sa_username1)
        self.assertEqual(sa1.get("time_created"), 1706784741)

        self.assertCountEqual(
            list(sa2.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa2.get("clientId"), sa_client_ids[1])
        self.assertEqual(sa2.get("name"), "service_account_name")
        self.assertEqual(sa2.get("description"), "Service Account description")
        self.assertEqual(sa2.get("owner"), "ecasey")
        self.assertEqual(sa2.get("type"), "service-account")
        self.assertEqual(sa2.get("username"), sa_username2)
        self.assertEqual(sa2.get("time_created"), 1306784741)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_read_principal_service_account_list_success(self, mock_request):
        """Test that we can read a list of service accounts."""
        # Create SA in the database
        sa_client_id = "b6636c60-a31d-013c-b93d-6aa2427b506c"
        sa_username = "service_account-" + sa_client_id

        Principal.objects.create(
            username=sa_username,
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_client_id,
        )

        mock_request.return_value = [
            {
                "clientId": sa_client_id,
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "jsmith",
                "username": sa_username,
                "time_created": 1706784741,
                "type": "service-account",
            }
        ]

        url = f"{reverse('v1_management:principals')}?type=service-account"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

        sa = response.data.get("data")[0]
        self.assertCountEqual(
            list(sa.keys()),
            ["clientId", "name", "description", "owner", "time_created", "type", "username"],
        )
        self.assertEqual(sa.get("clientId"), sa_client_id)
        self.assertEqual(sa.get("name"), "service_account_name")
        self.assertEqual(sa.get("description"), "Service Account description")
        self.assertEqual(sa.get("owner"), "jsmith")
        self.assertEqual(sa.get("type"), "service-account")
        self.assertEqual(sa.get("username"), sa_username)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_read_principal_service_account_list_empty_response(self, mock_request):
        """Test that empty response is returned when tenant doesn't have a service account in RBAC database."""

        sa_client_id = "026f5290-a3d3-013c-b93f-6aa2427b506c"
        mock_request.return_value = [
            {
                "clientId": sa_client_id,
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "jsmith",
                "username": "service_account-" + sa_client_id,
                "time_created": 1706784741,
                "type": "service-account",
            }
        ]

        url = f"{reverse('v1_management:principals')}?type=service-account"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(int(response.data.get("meta").get("count")), 0)
        self.assertEqual(len(response.data.get("data")), 0)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_read_principal_service_account_valid_limit_offset(self, mock_request):
        """Test that we can read a list of service accounts according to the given limit and offset."""
        mock_request.return_value = self.mocked_values

        # without limit and offset the default values are used
        # limit=10, offset=0
        url = f"{reverse('v1_management:principals')}?type=service-account"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response.data.get("meta").get("count")), 3)
        self.assertEqual(len(response.data.get("data")), 3)

        # set custom limit and offset
        test_values = [(1, 1), (2, 2), (5, 5)]
        for limit, offset in test_values:
            url = f"{reverse('v1_management:principals')}?type=service-account&limit={limit}&offset={offset}"
            client = APIClient()
            response = client.get(url, **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(int(response.data.get("meta").get("count")), 3)
            # for limit=1, offset=1, count=3 is the result min(1, max(0, 2)) = 1
            # for limit=2, offset=2, count=3 is the result min(2, max(0, 1)) = 1
            # for limit=5, offset=5, count=3 is the result min(5, max(0, -2)) = 0
            self.assertEqual(len(response.data.get("data")), min(limit, max(0, 3 - offset)))

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_read_principal_service_account_invalid_limit_offset(self, mock_request):
        """Test that default values are used for invalid limit and offset"""
        sa_client_id = "026f5290-a3d3-013c-b93f-6aa2427b506c"
        mock_request.return_value = [
            {
                "clientId": sa_client_id,
                "name": "service_account_name",
                "description": "Service Account description",
                "owner": "jsmith",
                "username": "service_account-" + sa_client_id,
                "time_created": 1706784741,
                "type": "service-account",
            }
        ]

        test_values = [(-1, -1), ("foo", "foo"), (0, 0)]
        default_limit = StandardResultsSetPagination.default_limit
        client = APIClient()

        for limit, offset in test_values:
            url = f"{reverse('v1_management:principals')}?type=service-account&limit={limit}&offset={offset}"
            response = client.get(url, **self.headers)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data.get("meta").get("offset"), 0)
            self.assertEqual(response.data.get("meta").get("limit"), default_limit)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch(
        "management.principal.it_service.ITService.get_service_accounts",
        side_effect=UnexpectedStatusCodeFromITError("Mocked error"),
    )
    def test_read_principal_service_account_unexpected_internal_error(self, mock_request):
        """
        Test the expected error message is returned in case of unexpected internal error that is returned from
        method ITService.get_service_accounts().
        """
        expected_message = "Unexpected internal error."
        url = f"{reverse('v1_management:principals')}?type=service-account"
        client = APIClient()
        response = client.get(url, **self.headers)
        err = response.json()["errors"][0]
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(err["detail"], expected_message)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_read_principal_service_account_usernames_only(self, mock_request):
        """Test the pagination is correct when we read a list of service accounts with username_only=true."""
        # Create 5 SA in the database
        sa_client_ids = [
            "b6636c60-a31d-013c-b93d-6aa2427b506c",
            "69a116a0-a3d4-013c-b940-6aa2427b506c",
            "6f3c2700-a3d4-013c-b941-6aa2427b506c",
            "1a67d137-374a-4aeb-8f27-83a321e876f9",
            "eb594f55-3a84-436b-8d3a-36d0f1f3dc2e",
        ]
        for uuid in sa_client_ids:
            Principal.objects.create(
                username="service_account-" + uuid,
                tenant=self.tenant,
                type="service-account",
                service_account_id=uuid,
            )

        # create a return value for the mock
        mocked_values = []
        for uuid in sa_client_ids:
            mocked_values.append(
                {
                    "clientId": uuid,
                    "name": f"service_account_name_{uuid.split('-')[0]}",
                    "description": f"Service Account description {uuid.split('-')[0]}",
                    "owner": "jsmith",
                    "username": "service_account-" + uuid,
                    "time_created": 1706784741,
                    "type": "service-account",
                }
            )

        mock_request.return_value = mocked_values

        client = APIClient()
        base_url = f"{reverse('v1_management:principals')}?type=service-account&username_only=true"

        # TEST 1
        limit = 2
        url = f"{base_url}&limit={limit}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # With limit=2 the response contains 2 principals from 5
        self.assertEqual(int(response.data.get("meta").get("count")), 5)
        self.assertEqual(len(response.data.get("data")), limit)

        sa = response.data.get("data")
        self.assertEqual(sa[0].get("username"), "service_account-" + sa_client_ids[0])
        self.assertEqual(sa[1].get("username"), "service_account-" + sa_client_ids[1])
        # test that data contains only the 'username' and nothing else
        self.assertEqual(len(sa[0].keys()), 1)
        self.assertEqual(len(sa[1].keys()), 1)

        # TEST 2
        offset = 2
        limit = 3
        url = f"{base_url}&limit={limit}&offset={offset}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # With limit=3 the response contains principals from 5
        self.assertEqual(int(response.data.get("meta").get("count")), 5)
        self.assertEqual(len(response.data.get("data")), limit)

        sa = response.data.get("data")
        self.assertEqual(sa[0].get("username"), "service_account-" + sa_client_ids[2])
        self.assertEqual(sa[1].get("username"), "service_account-" + sa_client_ids[3])
        self.assertEqual(sa[2].get("username"), "service_account-" + sa_client_ids[4])

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_read_principal_service_account_filtered_list_success(self, mock_request):
        """Test that we can read a filtered list of service accounts."""
        mock_request.return_value = self.mocked_values

        # Without the 'usernames' filter we get all values
        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=service-account"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response.data.get("meta").get("count")), 3)
        self.assertEqual(len(response.data.get("data")), 3)

        # With the 'usernames' filter we get only filtered values
        sa1 = self.mocked_values[0]
        url = f"{reverse('v1_management:principals')}?type=service-account&usernames={sa1['username']}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_service_account_filtered_list_with_untrimmed_values(self, mock_request):
        """
        Test that we can read a filtered list of service accounts
        and username values are processed as trimmed values.
        """
        mock_request.return_value = self.mocked_values[:2], 2

        client = APIClient()
        sa1 = self.mocked_values[0]
        sa2 = self.mocked_values[1]
        for gap in ("", " ", "     "):
            url = f"{reverse('v1_management:principals')}?type=service-account&usernames={sa1['username']},{gap}{sa2['username']}"
            response = client.get(url, **self.headers)
            # Regardless of the size of the gap between the values, the function is called with the same parameters
            # => the spaces are correctly removed before the function call.
            mock_request.assert_called_with(
                user=ANY,
                options={
                    "limit": 10,
                    "offset": 0,
                    "sort_order": "asc",
                    "status": "enabled",
                    "principal_type": "service-account",
                    "usernames": f"{sa1['username']},{sa2['username']}",
                    "email": None,
                    "match_criteria": None,
                    "username_only": None,
                },
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data.get("data")), 2)

        # The function is called three times in this test.
        self.assertEqual(mock_request.call_count, 3)

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_read_principal_service_account_usernames_only_filtered_list(self, mock_request):
        """Test we can read filtered list of service account usernames."""
        mock_request.return_value = self.mocked_values

        # Without the 'usernames' filter we get all usernames
        client = APIClient()
        username_only = "username_only=true&"
        url = f"{reverse('v1_management:principals')}?type=service-account&{username_only}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response.data.get("meta").get("count")), 3)
        self.assertEqual(len(response.data.get("data")), 3)
        for record in response.data.get("data"):
            self.assertEqual(list(record.keys()), ["username"])

        # With the 'usernames' filter we get only filtered values
        sa1 = self.mocked_values[0]
        usernames = f"usernames={sa1['username']}"
        url = f"{reverse('v1_management:principals')}?type=service-account&{username_only}&{usernames}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response.data.get("meta").get("count")), 1)
        self.assertEqual(len(response.data.get("data")), 1)
        sa = response.data.get("data")[0]
        self.assertEqual(list(sa.keys()), ["username"])
        self.assertEqual(sa["username"], sa1["username"])


@override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
class PrincipalViewsetAllTypesTests(IdentityRequest):
    """Tests the principal view set - only tests with 'type=all' query param."""

    def setUp(self):
        """Set up the principal viewset tests for service accounts."""
        super().setUp()
        self.sa_client_ids = [
            "1c8d4c5a-1602-4ba3-8766-0c894612d4f5",
            "d907e308-fe91-41e4-8282-686d7dd56b13",
            "ae50f3e0-4b37-45f4-a3db-1d684d4b39bd",
        ]

        for uuid in self.sa_client_ids:
            Principal.objects.create(
                username="service_account-" + uuid,
                tenant=self.tenant,
                type="service-account",
                service_account_id=uuid,
            )

        self.mocked_service_accounts = []
        for uuid in self.sa_client_ids:
            self.mocked_service_accounts.append(
                {
                    "clientId": uuid,
                    "name": f"service_account_name_{uuid.split('-')[0]}",
                    "description": f"Service Account description {uuid.split('-')[0]}",
                    "owner": "jsmith",
                    "username": "service_account-" + uuid,
                    "time_created": 1706784741,
                    "type": "service-account",
                }
            )

        self.mocked_users = {
            "status_code": 200,
            "data": {
                "userCount": "3",
                "users": [
                    {"username": "test_user1"},
                    {"username": "test_user2"},
                    {"username": "test_user3"},
                ],
            },
        }

    def tearDown(self):
        """Tear down principal viewset tests for service accounts."""
        Principal.objects.all().delete()

    def generate_tenant_and_headers(self):
        customer_data = self._create_customer_data()
        user_data = self._create_user_data()
        request_context = self._create_request_context(customer_data, user_data)
        tenant_name = customer_data.get("tenant_name")
        tenant = Tenant.objects.create(
            tenant_name=tenant_name,
            account_id=customer_data["account_id"],
            org_id=customer_data["org_id"],
            ready=True,
        )
        headers = request_context["request"].META
        return tenant, headers

    @staticmethod
    def generate_user_based_principals(tenant, user_count, limit=10, incl_mock_return_value=True, username_only=False):
        for i in range(user_count):
            Principal.objects.create(username=f"test_user_{i + 1}", tenant=tenant)

        return_value = None
        if incl_mock_return_value:
            if username_only:
                users = [{"username": f"test_user_{i + 1}"} for i in range(min(limit, user_count))]
            else:
                users = [
                    {"username": f"test_user_{i + 1}", "org_id": tenant.org_id, "is_active": True}
                    for i in range(min(limit, user_count))
                ]
            return_value = {
                "status_code": 200,
                "data": {"userCount": user_count, "users": users},
            }
        return return_value

    @staticmethod
    def generate_service_accounts(tenant, sa_count=3, limit=10, incl_mock_return_value=True, username_only=False):
        sa_client_ids = [str(uuid4()) for _ in range(sa_count)]
        for sa_id in sa_client_ids:
            Principal.objects.create(
                username=f"service_account-{sa_id}",
                tenant=tenant,
                type="service-account",
                service_account_id=sa_id,
            )

        mocked_service_accounts = []
        if incl_mock_return_value:
            if username_only:
                for sa_id in sa_client_ids[: min(limit, sa_count)]:
                    mocked_service_accounts.append({"username": "service_account-" + sa_id})
            else:
                for sa_id in sa_client_ids[: min(limit, sa_count)]:
                    mocked_service_accounts.append(
                        {
                            "clientId": sa_id,
                            "name": f"service_account_name_{sa_id.split('-')[0]}",
                            "description": f"Service Account description {sa_id.split('-')[0]}",
                            "owner": "jsmith",
                            "username": "service_account-" + sa_id,
                            "time_created": int(datetime.now().timestamp()),
                            "type": "service-account",
                        }
                    )
        return mocked_service_accounts

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all(self, mock_sa, mock_user):
        """Test that we can read both principal types in one request."""
        mock_sa.return_value = self.mocked_service_accounts, 3
        mock_user.return_value = self.mocked_users

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])

        sa = response.data.get("data").get("serviceAccounts")
        users = response.data.get("data").get("users")
        self.assertEqual(len(sa), 3)
        self.assertEqual(len(users), 3)

        self.assertEqual(response.data.get("meta").get("count"), 6)

    @patch("management.principal.view.PrincipalView.users_from_proxy")
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_read_principal_all_pagination(self, mock_sa, mock_user):
        """Test the pagination when we read both principal types in one request."""
        mock_sa.return_value = self.mocked_service_accounts

        # Mock the User based Principals return value
        # Because of limit and offset, we return only 2 user based principals from 3 existing
        mock_user.return_value = {
            "status_code": 200,
            "data": {
                "userCount": 3,
                "users": [
                    {"username": "test_user1"},
                    {"username": "test_user2"},
                ],
            },
        }, ""

        client = APIClient()

        # TEST 1 - 3 SA (service accounts) and 3 U (user based principals) -> 1 SA + 2 U in the response
        limit = 3
        offset = 2
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}&offset={offset}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        sa = response.data.get("data").get("serviceAccounts")
        users = response.data.get("data").get("users")
        self.assertEqual(len(sa), 1)
        self.assertEqual(len(users), 2)

        self.assertEqual(response.data.get("meta").get("count"), 6)
        self.assertEqual(response.data.get("meta").get("limit"), limit)
        self.assertEqual(response.data.get("meta").get("offset"), offset)

        # The query for user based principals was called with new limit and offset
        new_limit = 2
        new_offset = 0
        mock_user.assert_called_once_with(ANY, ANY, ANY, new_limit, new_offset)

        # TEST 2 - 3 SA (service accounts) and 3 U (user based principals) -> only 2 SA in the response
        limit = 2
        offset = 0
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}&offset={offset}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        sa = response.data.get("data").get("serviceAccounts")
        self.assertNotIn("users", response.data.get("data").keys())
        self.assertEqual(len(sa), 2)

        self.assertEqual(response.data.get("meta").get("count"), 6)
        self.assertEqual(response.data.get("meta").get("limit"), limit)
        self.assertEqual(response.data.get("meta").get("offset"), offset)

        # TEST 3 - 3 SA (service accounts) and 3 U (user based principals) -> only 2 U in the response
        limit = 2
        offset = 3
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}&offset={offset}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        users = response.data.get("data").get("users")
        self.assertEqual(len(users), 2)
        self.assertNotIn("serviceAccounts", response.data.get("data").keys())

        self.assertEqual(response.data.get("meta").get("count"), 6)
        self.assertEqual(response.data.get("meta").get("limit"), limit)
        self.assertEqual(response.data.get("meta").get("offset"), offset)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_username_only(self, mock_sa, mock_user):
        """Test that we can read both principal types in one request username only."""
        # Create a return value for the mock
        mocked_sa = []
        for uuid in self.sa_client_ids:
            mocked_sa.append(
                {
                    "username": "service_account-" + uuid,
                }
            )

        mock_sa.return_value = mocked_sa, 3

        # Mock the User based Principals return value
        mock_user.return_value = self.mocked_users

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all&username_only=true"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])

        sa = response.data.get("data").get("serviceAccounts")
        users = response.data.get("data").get("users")
        self.assertEqual(len(sa), 3)
        self.assertEqual(len(users), 3)

        self.assertEqual(response.data.get("meta").get("count"), 6)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_filtered_principal_all(self, mock_sa, mock_user):
        """Test that we can read filtered list of both principal types in one request."""
        mock_sa.return_value = [self.mocked_service_accounts[0]], 1
        mock_user.return_value = {"status_code": 200, "data": [{"username": "test_user1"}]}

        client = APIClient()
        sa_username = f"service-account-{self.sa_client_ids[0]}"
        user_username = "test_user1"
        usernames = f"usernames={sa_username},{user_username}"
        url = f"{reverse('v1_management:principals')}?type=all&{usernames}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])

        sa = response.data.get("data").get("serviceAccounts")
        users = response.data.get("data").get("users")
        self.assertEqual(len(sa), 1)
        self.assertEqual(len(users), 1)

        self.assertEqual(response.data.get("meta").get("count"), 2)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_filtered_principal_all_username_only(self, mock_sa, mock_user):
        """Test that we can read filtered list of both principal types usernames in one request."""
        mocked_sa = [
            {
                "username": "service_account-" + self.sa_client_ids[0],
            }
        ]
        mock_sa.return_value = mocked_sa, 1
        mock_user.return_value = {"status_code": 200, "data": [{"username": "test_user1"}]}

        client = APIClient()
        sa_username = f"service-account-{self.sa_client_ids[0]}"
        user_username = "test_user1"
        usernames = f"usernames={sa_username},{user_username}"
        username_only = "username_only=true"
        url = f"{reverse('v1_management:principals')}?type=all&{usernames}&{username_only}"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])

        sa = response.data.get("data").get("serviceAccounts")
        user = response.data.get("data").get("users")
        self.assertEqual(len(sa), 1)
        self.assertEqual(len(user), 1)
        self.assertEqual(response.data.get("meta").get("count"), 2)

        # Only "usernames" in the response
        self.assertEqual(list(sa[0].keys()), ["username"])
        self.assertEqual(list(user[0].keys()), ["username"])

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_many_principals_limit_10(self, mock_sa, mock_user):
        """Test that we can read both principal types in one request for many principals and limit=10."""
        tenant, headers = self.generate_tenant_and_headers()
        sa_count = user_count = 15

        limit = 10
        mock_sa.return_value = self.generate_service_accounts(tenant, limit=limit, sa_count=sa_count), sa_count
        mock_user.return_value = self.generate_user_based_principals(tenant, limit=limit, user_count=user_count)

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts"])

        sa = response.data.get("data").get("serviceAccounts")
        self.assertEqual(len(sa), limit)

        self.assertEqual(response.data.get("meta").get("count"), sa_count + user_count)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_many_principals_limit_20(self, mock_sa, mock_user):
        """Test that we can read both principal types in one request for many principals and limit=20."""
        tenant, headers = self.generate_tenant_and_headers()
        sa_count = user_count = 15

        limit = 20
        mock_sa.return_value = self.generate_service_accounts(tenant, limit=limit, sa_count=sa_count), sa_count
        mock_user.return_value = self.generate_user_based_principals(
            tenant, limit=limit - sa_count, user_count=user_count
        )

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])

        sa = response.data.get("data").get("serviceAccounts")
        users = response.data.get("data").get("users")
        self.assertEqual(len(sa), 15)
        self.assertEqual(len(users), 5)

        self.assertEqual(response.data.get("meta").get("count"), 30)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_many_principals_limit_1000(self, mock_sa, mock_user):
        """Test that we can read both principal types in one request for many principals and limit=1000."""
        tenant, headers = self.generate_tenant_and_headers()
        sa_count = user_count = 15

        limit = 1000
        mock_sa.return_value = self.generate_service_accounts(tenant, limit=limit, sa_count=sa_count), sa_count
        mock_user.return_value = self.generate_user_based_principals(
            tenant, limit=limit - sa_count, user_count=user_count
        )

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])

        sa = response.data.get("data").get("serviceAccounts")
        users = response.data.get("data").get("users")
        self.assertEqual(len(sa), 15)
        self.assertEqual(len(users), 15)

        self.assertEqual(response.data.get("meta").get("count"), 30)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_username_only_many_principals_limit_10(self, mock_sa, mock_user):
        """Test that we can read usernames only for both principal types or many principals and limit=10."""
        tenant, headers = self.generate_tenant_and_headers()
        sa_count = user_count = 15

        limit = 10
        mock_sa.return_value = (
            self.generate_service_accounts(tenant, limit=limit, sa_count=sa_count, username_only=True),
            sa_count,
        )
        mock_user.return_value = self.generate_user_based_principals(
            tenant, limit=limit, user_count=user_count, username_only=True
        )

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}&username_only=true"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts"])

        sa_list = response.data.get("data").get("serviceAccounts")
        self.assertEqual(len(sa_list), limit)
        for sa in sa_list:
            self.assertEqual(len(sa.keys()), 1)
            self.assertEqual(list(sa.keys())[0], "username")

        self.assertEqual(response.data.get("meta").get("count"), sa_count + user_count)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_username_only_many_principals_limit_20(self, mock_sa, mock_user):
        """Test that we can read usernames only for both principal types or many principals and limit=20."""
        tenant, headers = self.generate_tenant_and_headers()
        sa_count = user_count = 15

        limit = 20
        mock_sa.return_value = (
            self.generate_service_accounts(tenant, limit=limit, sa_count=sa_count, username_only=True),
            sa_count,
        )
        mock_user.return_value = self.generate_user_based_principals(
            tenant, limit=limit - sa_count, user_count=user_count, username_only=True
        )

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}&username_only=true"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])

        sa_list = response.data.get("data").get("serviceAccounts")
        users_list = response.data.get("data").get("users")

        self.assertEqual(len(sa_list), 15)
        for sa in sa_list:
            self.assertEqual(len(sa.keys()), 1)
            self.assertEqual(list(sa.keys())[0], "username")

        self.assertEqual(len(users_list), 5)
        for user in users_list:
            self.assertEqual(len(user.keys()), 1)
            self.assertEqual(list(user.keys())[0], "username")

        self.assertEqual(response.data.get("meta").get("count"), sa_count + user_count)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_username_only_many_principals_limit_1000(self, mock_sa, mock_user):
        """Test that we can read usernames only for both principal types or many principals and limit=1000."""
        tenant, headers = self.generate_tenant_and_headers()
        sa_count = user_count = 15

        limit = 1000
        mock_sa.return_value = (
            self.generate_service_accounts(tenant, limit=limit, sa_count=sa_count, username_only=True),
            sa_count,
        )
        mock_user.return_value = self.generate_user_based_principals(
            tenant, limit=limit - sa_count, user_count=user_count, username_only=True
        )

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}&username_only=true"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])

        sa_list = response.data.get("data").get("serviceAccounts")
        users_list = response.data.get("data").get("users")

        self.assertEqual(len(sa_list), 15)
        for sa in sa_list:
            self.assertEqual(len(sa.keys()), 1)
            self.assertEqual(list(sa.keys())[0], "username")

        self.assertEqual(len(users_list), 15)
        for user in users_list:
            self.assertEqual(len(user.keys()), 1)
            self.assertEqual(list(user.keys())[0], "username")

        self.assertEqual(response.data.get("meta").get("count"), sa_count + user_count)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_without_service_accounts(self, mock_sa, mock_user):
        """Test that we can read list of both principal types when only user based principals present."""
        tenant, headers = self.generate_tenant_and_headers()
        user_count = 15
        limit = 10
        mock_sa.return_value = [], 0
        mock_user.return_value = self.generate_user_based_principals(
            tenant, limit=limit, user_count=user_count, username_only=True
        )

        client = APIClient()
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}&username_only=true"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["users"])

        users_list = response.data.get("data").get("users")
        self.assertEqual(len(users_list), 10)

        self.assertEqual(response.data.get("meta").get("count"), user_count)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_without_service_accounts_limit_100(self, mock_sa, mock_user):
        """Test that we can read list of both principal types when only user based principals present."""
        tenant, headers = self.generate_tenant_and_headers()
        user_count = 15
        limit = 100
        mock_sa.return_value = [], 0
        mock_user.return_value = self.generate_user_based_principals(
            tenant, limit=limit, user_count=user_count, username_only=True
        )
        url = f"{reverse('v1_management:principals')}?type=all&limit={limit}&username_only=true"
        client = APIClient()
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["users"])

        users_list = response.data.get("data").get("users")
        self.assertEqual(len(users_list), user_count)

        self.assertEqual(response.data.get("meta").get("count"), user_count)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_partial_filters(self, mock_sa, mock_user):
        """Test that we can read both principal types in one request and filter by partial matching."""
        tenant, headers = self.generate_tenant_and_headers()

        mock_sa.return_value = (
            [
                {
                    "clientId": "c16e33c3-cf42-4df7-8330-1f6550f6ffd1",
                    "name": "service_account_name_c16e33c3",
                    "description": "Service Account description c16e33c3",
                    "owner": "jsmith",
                    "username": "service_account-c16e33c3-cf42-4df7-8330-1f6550f6ffd1",
                    "time_created": 1762725547,
                    "type": "service-account",
                },
                {
                    "clientId": "cdff8555-5b01-4746-ba51-e8d200590719",
                    "name": "service_account_name_cdff8555",
                    "description": "Service Account description cdff8555",
                    "owner": "jsmith",
                    "username": "service_account-cdff8555-5b01-4746-ba51-e8d200590719",
                    "time_created": 1762725547,
                    "type": "service-account",
                },
                {
                    "clientId": "60281e53-5ad1-4e0c-b4eb-12d57ca326a5",
                    "name": "service_account_name_60281e53",
                    "description": "Service Account description 60281e53",
                    "owner": "jsmith",
                    "username": "service_account-60281e53-5ad1-4e0c-b4eb-12d57ca326a5",
                    "time_created": 1762725547,
                    "type": "service-account",
                },
            ],
            3,
        )
        mock_user.return_value = {
            "status_code": 200,
            "data": [
                {"username": "test_user_1", "org_id": "94268706", "is_active": True},
                {"username": "test_user_2", "org_id": "94268706", "is_active": True},
                {"username": "test_user_3", "org_id": "94268706", "is_active": True},
                {"username": "test_user_4", "org_id": "94268706", "is_active": True},
            ],
        }

        client = APIClient()
        limit = "limit=100"
        match_criteria = "match_criteria=partial"
        # Filtering by usernames is done on the 3 party service, so it doesn't matter what we put here
        # We just test that the responses were properly processed and the final count is correct
        usernames = "usernames=XXX"

        url = f"{reverse('v1_management:principals')}?type=all&{limit}&{match_criteria}&{usernames}"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        for key in response.data.get("data").keys():
            self.assertIn(key, ["serviceAccounts", "users"])
        users = response.data.get("data").get("users")
        sa = response.data.get("data").get("serviceAccounts")
        self.assertEqual(len(sa), 3)
        self.assertEqual(len(users), 4)
        self.assertEqual(response.data.get("meta").get("count"), 7)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_partial_filter_returns_only_service_accounts(self, mock_sa, mock_user):
        """
        Test that we can read both principal types in one request and filter by partial matching
        that leads to only service accounts being returned.
        """
        tenant, headers = self.generate_tenant_and_headers()

        mock_sa.return_value = (
            [
                {
                    "clientId": "c16e33c3-cf42-4df7-8330-1f6550f6ffd1",
                    "name": "service_account_name_c16e33c3",
                    "description": "Service Account description c16e33c3",
                    "owner": "jsmith",
                    "username": "service_account-c16e33c3-cf42-4df7-8330-1f6550f6ffd1",
                    "time_created": 1762725547,
                    "type": "service-account",
                },
                {
                    "clientId": "cdff8555-5b01-4746-ba51-e8d200590719",
                    "name": "service_account_name_cdff8555",
                    "description": "Service Account description cdff8555",
                    "owner": "jsmith",
                    "username": "service_account-cdff8555-5b01-4746-ba51-e8d200590719",
                    "time_created": 1762725547,
                    "type": "service-account",
                },
                {
                    "clientId": "60281e53-5ad1-4e0c-b4eb-12d57ca326a5",
                    "name": "service_account_name_60281e53",
                    "description": "Service Account description 60281e53",
                    "owner": "jsmith",
                    "username": "service_account-60281e53-5ad1-4e0c-b4eb-12d57ca326a5",
                    "time_created": 1762725547,
                    "type": "service-account",
                },
            ],
            3,
        )

        mock_user.return_value = {"status_code": 200, "data": []}

        client = APIClient()
        limit = "limit=100"
        match_criteria = "match_criteria=partial"
        # Filtering by usernames is done on the 3 party service, so it doesn't matter what we put here
        # We just test that the responses were properly processed and the final count is correct
        usernames = "usernames=XXX"

        url = f"{reverse('v1_management:principals')}?type=all&{limit}&{match_criteria}&{usernames}"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        for key in response.data.get("data").keys():
            self.assertEqual(key, "serviceAccounts")
        sa = response.data.get("data").get("serviceAccounts")
        self.assertEqual(len(sa), 3)
        self.assertEqual(response.data.get("meta").get("count"), 3)

    @patch("management.principal.proxy.PrincipalProxy.request_principals")
    @patch("management.principal.it_service.ITService.get_service_accounts")
    def test_read_principal_all_partial_filter_return_only_users(self, mock_sa, mock_user):
        """
        Test that we can read both principal types in one request and filter by partial matching
        that leads to only users being returned.
        """
        tenant, headers = self.generate_tenant_and_headers()

        mock_sa.return_value = ([], 0)
        mock_user.return_value = {
            "status_code": 200,
            "data": [
                {"username": "test_user_1", "org_id": "94268706", "is_active": True},
                {"username": "test_user_2", "org_id": "94268706", "is_active": True},
                {"username": "test_user_3", "org_id": "94268706", "is_active": True},
                {"username": "test_user_4", "org_id": "94268706", "is_active": True},
            ],
        }

        client = APIClient()
        limit = "limit=100"
        match_criteria = "match_criteria=partial"
        # Filtering by usernames is done on the 3 party service, so it doesn't matter what we put here
        # We just test that the responses were properly processed and the final count is correct
        usernames = "usernames=XXX"

        url = f"{reverse('v1_management:principals')}?type=all&{limit}&{match_criteria}&{usernames}"
        response = client.get(url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)
        for key in response.data.get("data").keys():
            self.assertEqual(key, "users")
        users = response.data.get("data").get("users")
        self.assertEqual(len(users), 4)
        self.assertEqual(response.data.get("meta").get("count"), 4)
