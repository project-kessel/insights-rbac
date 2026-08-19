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
from uuid import uuid4

from django.test.utils import override_settings
from django.urls import clear_url_caches
from management.group.model import Group
from management.models import Principal
from management.principal.v2_serializer import PrincipalV2OutputSerializer
from rest_framework import status
from rest_framework.test import APIClient
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test

from api.models import Tenant
from rbac import urls

V2_URL = "/api/rbac/v2/principals/"


@override_settings(V2_APIS_ENABLED=True)
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

    def test_list_all_principals(self):
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

    def test_list_excludes_cross_account_principals(self):
        """Cross-account principals are excluded from list results."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [p["username"] for p in response.data["data"]]
        self.assertNotIn("crossuser", usernames)

    def test_list_filter_by_type_user(self):
        """Filter by type=user returns only user principals."""
        client = APIClient()
        response = client.get(f"{V2_URL}?type=user", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 2)
        types = {p["type"] for p in response.data["data"]}
        self.assertEqual(types, {"user"})

    def test_list_filter_by_type_service_account(self):
        """Filter by type=service-account returns only service account principals."""
        client = APIClient()
        response = client.get(f"{V2_URL}?type=service-account", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["username"], "service-account-abc123")
        self.assertEqual(response.data["data"][0]["service_account_id"], "abc123")

    def test_list_filter_by_username_substring(self):
        """Filter by username uses case-insensitive substring match by default."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=li", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["username"], "alice")

    def test_list_filter_by_username_case_insensitive(self):
        """Username substring match is case-insensitive."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=ALICE", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["username"], "alice")

    def test_list_filter_by_username_glob(self):
        """Filter by username with glob pattern uses wildcard matching."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=ali*", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["username"], "alice")

    def test_list_filter_by_username_glob_star_matches_all(self):
        """Bare * matches all principals (no filter applied)."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=*", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 3)

    def test_list_order_by_username_asc(self):
        """Default sort order is ascending by username."""
        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [p["username"] for p in response.data["data"]]
        self.assertEqual(usernames, sorted(usernames))

    def test_list_order_by_username_desc(self):
        """order_by=-username returns principals in descending order."""
        client = APIClient()
        response = client.get(f"{V2_URL}?order_by=-username", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [p["username"] for p in response.data["data"]]
        self.assertEqual(usernames, sorted(usernames, reverse=True))

    def test_list_order_by_invalid_field(self):
        """Invalid order_by value returns 400."""
        client = APIClient()
        response = client.get(f"{V2_URL}?order_by=invalid", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_pagination(self):
        """Pagination returns correct meta and links."""
        client = APIClient()
        response = client.get(f"{V2_URL}?limit=2&offset=0", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 3)
        self.assertEqual(response.data["meta"]["limit"], 2)
        self.assertEqual(response.data["meta"]["offset"], 0)
        self.assertEqual(len(response.data["data"]), 2)
        self.assertIsNotNone(response.data["links"]["next"])

    def test_list_pagination_second_page(self):
        """Second page returns remaining principals."""
        client = APIClient()
        response = client.get(f"{V2_URL}?limit=2&offset=2", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 3)
        self.assertEqual(len(response.data["data"]), 1)

    def test_retrieve_by_uuid(self):
        """Retrieve a single principal by UUID."""
        client = APIClient()
        url = f"{V2_URL}{self.user_principal_1.uuid}/"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], "alice")
        self.assertEqual(response.data["type"], "user")
        self.assertEqual(response.data["user_id"], "100001")
        self.assertEqual(str(response.data["uuid"]), str(self.user_principal_1.uuid))

    def test_retrieve_nonexistent_uuid(self):
        """Retrieve with a nonexistent UUID returns 404."""
        client = APIClient()
        url = f"{V2_URL}{uuid4()}/"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_cross_tenant_returns_404(self):
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

    def test_list_empty_tenant(self):
        """List with no principals returns empty data with count 0."""
        Principal.objects.filter(tenant=self.tenant).delete()

        client = APIClient()
        response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 0)
        self.assertEqual(response.data["data"], [])

    def test_response_fields(self):
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
        self.assertIn("group_count", principal)
        self.assertIsInstance(principal["group_count"], int)
        self.assertGreaterEqual(principal["group_count"], 0)

    def test_invalid_type_filter(self):
        """Invalid type filter value returns 400."""
        client = APIClient()
        response = client.get(f"{V2_URL}?type=invalid", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_write_methods_not_allowed(self):
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

    def test_principal_with_null_user_id(self):
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
class PrincipalV2GroupCountTests(IdentityRequest):
    """Test group_count field in the Principal V2 API responses."""

    def setUp(self):
        """Set up principals and groups for group_count tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_tenant_for_v2_test(self.tenant)

        self.principal_no_groups = Principal.objects.create(
            username="loner",
            type=Principal.Types.USER,
            user_id="200001",
            tenant=self.tenant,
        )
        self.principal_one_group = Principal.objects.create(
            username="single_group_user",
            type=Principal.Types.USER,
            user_id="200002",
            tenant=self.tenant,
        )
        self.principal_multi_groups = Principal.objects.create(
            username="multi_group_user",
            type=Principal.Types.USER,
            user_id="200003",
            tenant=self.tenant,
        )

        self.group_a = Group.objects.create(name="Group A", tenant=self.tenant)
        self.group_b = Group.objects.create(name="Group B", tenant=self.tenant)
        self.group_c = Group.objects.create(name="Group C", tenant=self.tenant)

        self.group_a.principals.add(self.principal_one_group)
        self.group_a.principals.add(self.principal_multi_groups)
        self.group_b.principals.add(self.principal_multi_groups)
        self.group_c.principals.add(self.principal_multi_groups)

    def test_list_group_count_zero(self):
        """Principal with no groups has group_count 0."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=loner", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["group_count"], 0)

    def test_list_group_count_one(self):
        """Principal in one group has group_count 1."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=single_group_user", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["group_count"], 1)

    def test_list_group_count_multiple(self):
        """Principal in multiple groups has correct group_count."""
        client = APIClient()
        response = client.get(f"{V2_URL}?username=multi_group_user", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["group_count"], 3)

    def test_retrieve_group_count(self):
        """Retrieve endpoint includes group_count."""
        client = APIClient()
        url = f"{V2_URL}{self.principal_multi_groups.uuid}/"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["group_count"], 3)

    def test_retrieve_group_count_zero(self):
        """Retrieve endpoint returns group_count 0 for ungrouped principal."""
        client = APIClient()
        url = f"{V2_URL}{self.principal_no_groups.uuid}/"
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["group_count"], 0)

    def test_group_count_service_account(self):
        """Service accounts also get group_count."""
        sa = Principal.objects.create(
            username="service-account-xyz",
            type=Principal.Types.SERVICE_ACCOUNT,
            service_account_id="xyz",
            tenant=self.tenant,
        )
        self.group_a.principals.add(sa)

        client = APIClient()
        response = client.get(f"{V2_URL}?username=service-account-xyz", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"][0]["group_count"], 1)

    def test_group_count_excludes_cross_tenant_groups(self):
        """Cross-tenant group membership does not inflate group_count."""
        other_tenant = Tenant.objects.create(tenant_name="other_org", org_id="99999")
        cross_tenant_group = Group.objects.create(name="Cross-Tenant Group", tenant=other_tenant)
        cross_tenant_group.principals.add(self.principal_one_group)

        client = APIClient()
        response = client.get(f"{V2_URL}?username=single_group_user", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"][0]["group_count"], 1)

    def test_group_count_zero_when_only_cross_tenant_groups(self):
        """Principal belonging only to cross-tenant groups has group_count 0."""
        other_tenant = Tenant.objects.create(tenant_name="cross_only_org", org_id="88888")
        cross_group = Group.objects.create(name="Foreign Group", tenant=other_tenant)
        cross_group.principals.add(self.principal_no_groups)

        client = APIClient()
        response = client.get(f"{V2_URL}?username=loner", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"][0]["group_count"], 0)

    def test_list_group_count_no_n_plus_one(self):
        """Listing principals uses a fixed number of queries regardless of group membership."""
        for i in range(5):
            p = Principal.objects.create(
                username=f"extra_user_{i}",
                type=Principal.Types.USER,
                user_id=str(300000 + i),
                tenant=self.tenant,
            )
            g = Group.objects.create(name=f"Extra Group {i}", tenant=self.tenant)
            g.principals.add(p)

        client = APIClient()
        # 3 queries: tenant lookup, pagination COUNT, annotated data fetch
        with self.assertNumQueries(3):
            response = client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(response.data["meta"]["count"], 5)

    def test_fallback_group_count_without_annotation(self):
        """Serializer falls back to a DB query when annotation is missing."""
        principal = Principal.objects.get(pk=self.principal_one_group.pk)
        serializer = PrincipalV2OutputSerializer(principal)
        self.assertEqual(serializer.data["group_count"], 1)


@override_settings(V2_APIS_ENABLED=True)
class PrincipalV2AccessDeniedTests(IdentityRequest):
    """Test that non-admin users without principal:read are denied."""

    def setUp(self):
        """Set up access denied tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()
        bootstrap_tenant_for_v2_test(self.tenant)

        non_admin_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        self.non_admin_request = non_admin_context["request"]
        self.non_admin_headers = self.non_admin_request.META

    def test_list_denied_for_non_admin_without_read(self):
        """Non-admin user without principal:read gets 403 on list."""
        client = APIClient()
        response = client.get(V2_URL, **self.non_admin_headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_retrieve_denied_for_non_admin_without_read(self):
        """Non-admin user without principal:read gets 403 on retrieve."""
        client = APIClient()
        response = client.get(f"{V2_URL}{uuid4()}/", **self.non_admin_headers)

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
