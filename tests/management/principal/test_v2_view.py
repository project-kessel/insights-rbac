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

"""Tests for the Principal V2 API."""

from importlib import reload
from unittest.mock import patch
from uuid import uuid4

from django.test.utils import override_settings
from django.urls import clear_url_caches
from management.models import Principal
from rest_framework import status
from rest_framework.test import APIClient
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test

from api.models import Tenant
from rbac import urls

V2_URL = "/api/rbac/v2/principals/"


def _mock_check_resource_access(*args, **kwargs):
    """Mock that always grants access."""
    return True


def _mock_check_resource_access_denied(*args, **kwargs):
    """Mock that always denies access."""
    return False


@override_settings(V2_APIS_ENABLED=True)
@patch(
    "management.permissions.principal_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
    _mock_check_resource_access,
)
@patch(
    "management.permissions.principal_v2_access.get_kessel_principal_id",
    return_value="localhost/12345",
)
class PrincipalV2ViewTests(IdentityRequest):
    """Test the Principal V2 API."""

    def setUp(self):
        """Set up the principal v2 tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_tenant_for_v2_test(self.tenant)

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
        self.sa_principal = Principal.objects.create(
            username="service-account-abc123",
            type=Principal.Types.SERVICE_ACCOUNT,
            service_account_id="abc123",
            tenant=self.tenant,
        )
        self.cross_account_principal = Principal.objects.create(
            username="crossuser",
            type=Principal.Types.USER,
            user_id="100099",
            cross_account=True,
            tenant=self.tenant,
        )

    def test_list_all_principals(self, mock_kessel_id):
        """List returns all non-cross-account principals for the tenant."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["meta"]["count"], 3)
        self.assertEqual(len(data["data"]), 3)

        usernames = [p["username"] for p in data["data"]]
        self.assertIn("alice", usernames)
        self.assertIn("bob", usernames)
        self.assertIn("service-account-abc123", usernames)
        self.assertNotIn("crossuser", usernames)

    def test_list_excludes_cross_account_principals(self, mock_kessel_id):
        """Cross-account principals are excluded from list results."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [p["username"] for p in response.data["data"]]
        self.assertNotIn("crossuser", usernames)

    def test_list_filter_by_type_user(self, mock_kessel_id):
        """Filter by type=user returns only user principals."""
        client = APIClient()
        response = client.get(f"{V2_URL}?type=user", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 2)
        types = {p["type"] for p in response.data["data"]}
        self.assertEqual(types, {"user"})

    def test_list_filter_by_type_service_account(self, mock_kessel_id):
        """Filter by type=service-account returns only service account principals."""
        client = APIClient()
        response = client.get(f"{V2_URL}?type=service-account", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["username"], "service-account-abc123")
        self.assertEqual(response.data["data"][0]["service_account_id"], "abc123")

    def test_list_filter_by_username_exact(self, mock_kessel_id):
        """Filter by username with exact match returns only the matching principal."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=alice", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["username"], "alice")

    def test_list_filter_by_username_partial(self, mock_kessel_id):
        """Filter by username with partial match returns substring matches."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=li&match_criteria=partial", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["username"], "alice")

    def test_list_filter_by_username_partial_case_insensitive(self, mock_kessel_id):
        """Partial username match is case-insensitive."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=ALICE&match_criteria=partial", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["username"], "alice")

    def test_list_sort_order_asc(self, mock_kessel_id):
        """Default sort order is ascending by username."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [p["username"] for p in response.data["data"]]
        self.assertEqual(usernames, sorted(usernames))

    def test_list_sort_order_desc(self, mock_kessel_id):
        """Sort order desc returns principals in descending username order."""
        client = APIClient()
        response = client.get(f"{V2_URL}?sort_order=desc", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [p["username"] for p in response.data["data"]]
        self.assertEqual(usernames, sorted(usernames, reverse=True))

    def test_list_pagination(self, mock_kessel_id):
        """Pagination returns correct meta and links."""
        client = APIClient()
        response = client.get(f"{V2_URL}?limit=2&offset=0", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 3)
        self.assertEqual(response.data["meta"]["limit"], 2)
        self.assertEqual(response.data["meta"]["offset"], 0)
        self.assertEqual(len(response.data["data"]), 2)
        self.assertIsNotNone(response.data["links"]["next"])

    def test_list_pagination_second_page(self, mock_kessel_id):
        """Second page returns remaining principals."""
        client = APIClient()
        response = client.get(f"{V2_URL}?limit=2&offset=2", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 3)
        self.assertEqual(len(response.data["data"]), 1)

    def test_retrieve_by_uuid(self, mock_kessel_id):
        """Retrieve a single principal by UUID."""
        client = APIClient()
        url = f"{V2_URL}{self.user_principal_1.uuid}/"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], "alice")
        self.assertEqual(response.data["type"], "user")
        self.assertEqual(response.data["user_id"], "100001")
        self.assertEqual(str(response.data["uuid"]), str(self.user_principal_1.uuid))

    def test_retrieve_nonexistent_uuid(self, mock_kessel_id):
        """Retrieve with a nonexistent UUID returns 404."""
        client = APIClient()
        url = f"{V2_URL}{uuid4()}/"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_cross_tenant_returns_404(self, mock_kessel_id):
        """Retrieving a principal from a different tenant returns 404, not 403."""
        other_tenant = Tenant.objects.create(
            tenant_name="other_tenant",
            org_id="other_org_id",
            account_id="other_account_id",
            ready=True,
        )
        other_principal = Principal.objects.create(
            username="other_user",
            type=Principal.Types.USER,
            user_id="999999",
            tenant=other_tenant,
        )

        client = APIClient()
        url = f"{V2_URL}{other_principal.uuid}/"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_empty_tenant(self, mock_kessel_id):
        """List with no principals returns empty data with count 0."""
        Principal.objects.filter(tenant=self.tenant).delete()

        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 0)
        self.assertEqual(response.data["data"], [])

    def test_response_fields(self, mock_kessel_id):
        """Response includes all expected fields."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        principal = response.data["data"][0]
        self.assertIn("uuid", principal)
        self.assertIn("username", principal)
        self.assertIn("type", principal)
        self.assertIn("user_id", principal)
        self.assertIn("service_account_id", principal)

    def test_invalid_type_filter(self, mock_kessel_id):
        """Invalid type filter value returns 400."""
        client = APIClient()
        response = client.get(f"{V2_URL}?type=invalid", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_sort_order(self, mock_kessel_id):
        """Invalid sort_order value returns 400."""
        client = APIClient()
        response = client.get(f"{V2_URL}?sort_order=invalid", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_match_criteria(self, mock_kessel_id):
        """Invalid match_criteria value returns 400."""
        client = APIClient()
        response = client.get(f"{V2_URL}?match_criteria=invalid", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_write_methods_not_allowed(self, mock_kessel_id):
        """POST, PUT, PATCH, DELETE are not allowed."""
        client = APIClient()
        self.assertEqual(client.post(V2_URL, {}, **self.headers).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(
            client.put(f"{V2_URL}{self.user_principal_1.uuid}/", {}, **self.headers).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            client.patch(f"{V2_URL}{self.user_principal_1.uuid}/", {}, **self.headers).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            client.delete(f"{V2_URL}{self.user_principal_1.uuid}/", **self.headers).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def test_principal_with_null_user_id(self, mock_kessel_id):
        """Principals with null user_id are returned with null in response."""
        Principal.objects.create(
            username="legacy_user",
            type=Principal.Types.USER,
            user_id=None,
            tenant=self.tenant,
        )

        client = APIClient()
        response = client.get(f"{V2_URL}?username=legacy_user", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertIsNone(response.data["data"][0]["user_id"])


@override_settings(V2_APIS_ENABLED=True)
@patch(
    "management.permissions.principal_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
    _mock_check_resource_access_denied,
)
@patch(
    "management.permissions.principal_v2_access.get_kessel_principal_id",
    return_value="localhost/12345",
)
class PrincipalV2AccessDeniedTests(IdentityRequest):
    """Test that Kessel access denial works correctly."""

    def setUp(self):
        """Set up access denied tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()
        bootstrap_tenant_for_v2_test(self.tenant)

    def test_list_denied_returns_403(self, mock_kessel_id):
        """Kessel denying access returns 403 on list."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_retrieve_denied_returns_403(self, mock_kessel_id):
        """Kessel denying access returns 403 on retrieve."""
        client = APIClient()
        response = client.get(f"{V2_URL}{uuid4()}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PrincipalV2DisabledTests(IdentityRequest):
    """Test that v2 endpoints are not available when V2_APIS_ENABLED is False."""

    @override_settings(V2_APIS_ENABLED=False)
    def test_v2_disabled_returns_404(self):
        """V2 API disabled returns 404 (routes not registered)."""
        reload(urls)
        clear_url_caches()

        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
