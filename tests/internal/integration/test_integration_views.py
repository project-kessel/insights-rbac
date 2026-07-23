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
"""Test the internal viewset."""

import uuid
from rest_framework import status
from rest_framework.test import APIClient
from unittest.mock import patch

from api.models import User, Tenant
from management.models import Group, Policy, Principal, Role
from tests.identity_request import IdentityRequest


class IntegrationViewsTests(IdentityRequest):
    """Test the integration views."""

    def setUp(self):
        """Set up the integration view tests."""
        test_roles = ["Role Admin", "Role A", "Role B"]
        test_principals = ["user_admin", "user_a", "user_b", "roles_test"]

        super().setUp()
        self.client = APIClient()
        self.customer = self.customer_data
        self.internal_request_context = self._create_request_context(self.customer, self.user_data, is_internal=True)

        self.request = self.internal_request_context["request"]
        user = User()
        user.username = self.user_data["username"]
        user.account = self.customer_data["account_id"]
        self.request.user = user

        for username in test_principals:
            principal = Principal.objects.create(username=username, tenant=self.tenant)
            principal.save()

        for role_name in test_roles:
            role = Role.objects.create(
                name=role_name, description="A role for a group.", system=True, tenant=self.tenant
            )
            role.save()

        group = Group(name="Group Admin", system=True, tenant=self.tenant)
        group.save()
        group.principals.add(Principal.objects.get(username="user_admin"))
        policy = Policy.objects.create(name="Admin Policy", group=group, tenant=self.tenant)
        policy.roles.add(Role.objects.get(name="Role Admin"))
        policy.save()
        group.policies.add(policy)
        group.save()

        group = Group(name="Group All", system=True, tenant=self.tenant)
        group.save()
        for principal in test_principals:
            group.principals.add(Principal.objects.get(username=principal))
        policy = Policy.objects.create(name="All Policy", group=group, tenant=self.tenant)
        for role in test_roles:
            policy.roles.add(Role.objects.get(name=role))
        group.policies.add(policy)
        group.save()

        group = Group(name="Group A", system=True, tenant=self.tenant)
        group.save()
        group.principals.add(Principal.objects.get(username="user_admin"))
        group.principals.add(Principal.objects.get(username="user_a"))
        policy = Policy.objects.create(name="A Policy", group=group, tenant=self.tenant)
        policy.roles.add(Role.objects.get(name="Role A"))
        group.policies.add(policy)
        group.save()

        self.modifiedTenant1 = Tenant.objects.create(tenant_name="Modified Group", org_id=1111)
        modifiedTenant1Group = Group.objects.create(
            system=False, name="modifiedTenant1Group", tenant=self.modifiedTenant1
        )
        self.modifiedTenant2 = Tenant.objects.create(tenant_name="Modified Role", org_id=1212)
        modifiedTenant2Role = Role.objects.create(
            system=False, name="modifiedTenant2Role", tenant=self.modifiedTenant2
        )

    def tearDown(self):
        """Tear down internal viewset tests."""
        Group.objects.all().delete()
        Role.objects.all().delete()
        Policy.objects.all().delete()
        # Clear the principal cache to avoid test isolation issues
        from management.utils import PRINCIPAL_CACHE

        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "user_admin",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_groups_valid_account(self, mock_request):
        """Test that a request to /tenant/<id>/groups/?username= from an internal account works."""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/?username=user_admin",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Group All", "Group A", "Group Admin"]
        self.assertEqual(response.data.get("meta").get("count"), 3)

    def test_groups_invalid_account(self):
        """Test that a /tenant/<id>/groups/?username= request from an external account fails."""
        external_request_context = self._create_request_context(self.customer, self.user_data, is_internal=False)
        request = external_request_context["request"]
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/?username=user_a",
            **request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "user_a",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_groups_user_filter(self, mock_request):
        """Test that only the groups a user is a member of are returned for a /tenant/<id>/groups/?username= request."""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/?username=user_a",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Group All", "Group A"]
        expected = []
        expected.append(Group.objects.get(name="Group A"))
        expected.append(Group.objects.get(name="Group All"))
        self.assertEqual(response.data.get("meta").get("count"), 2)
        self.assertTrue(expected, response.data)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    def test_groups_nonexistent_user(self, mock_request):
        """Test that a request for groups of a nonexistent user returns 0."""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/?username=user_x",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_groups_for_principal_valid_account(self):
        """Test that a request to /tenant/<id>/principal/<username>/groups/ from an internal account works."""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/principal/user_admin/groups/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Group All", "Group A", "Group Admin"]
        self.assertEqual(response.data.get("meta").get("count"), 3)

    def test_groups_for_principal_invalid_account(self):
        """Test that a /tenant/<id>/principal/<username>/groups/ request from an external account fails."""
        external_request_context = self._create_request_context(self.customer, self.user_data, is_internal=False)
        request = external_request_context["request"]
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/principal/user_a/groups/",
            **request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_groups_for_principal_roles_in_username(self):
        """
        Test that a request to /tenant/<id>/principal/<username>/groups/ from an internal account works
        with "roles" substring in the principal's username (RHCLOUD-32144).
        """
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/principal/roles_test/groups/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Group All"]
        self.assertEqual(response.data.get("meta").get("count"), 1)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "user_a",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_groups_for_principal_filter(self, mock_request):
        """Test that only the groups a user is a member of are returned for a /tenant/<id>/groups/?username= request."""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/?username=user_a",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Group All", "Group A"]
        self.assertEqual(response.data.get("meta").get("count"), 2)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    def test_groups_for_principal_nonexistent_user(self, mock_request):
        """Test that an error is return for nonexistent ."""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/?username=user_x",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_roles_from_group_valid(self):
        """Test that a valid request to /tenant/<id>/groups/<uuid>/roles/ from an internal account works."""
        group_all_uuid = Group.objects.get(name="Group All").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_all_uuid}/roles/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Role Admin", "Role A", "Role B"]
        self.assertEqual(response.data.get("meta").get("count"), 3)

    def test_roles_from_group_invalid_uuid(self):
        """Test that a request to /tenant/<id>/groups/<uuid>/roles/ with an invalid uuid fails."""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{uuid.uuid4}/roles/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_roles_from_group_invalid_account(self):
        """Test that a /tenant/<id>/groups/<uuid>/roles/ request from an external account fails."""
        group_all_uuid = Group.objects.get(name="Group All").uuid
        external_request_context = self._create_request_context(self.customer, self.user_data, is_internal=False)
        request = external_request_context["request"]
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_all_uuid}/roles/",
            **request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "user_a",
                    "account_number": "1111111",
                    "is_active": True,
                },
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567474,
                    "username": "user_admin",
                    "account_number": "1111111",
                    "is_active": True,
                },
            ],
        },
    )
    def test_principals_for_group(self, mock_request):
        """Test that a valid request to /tenant/<id>/groups/<uuid>/principals/ returns principals in group."""
        group_a_uuid = Group.objects.get(name="Group A").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_a_uuid}/principals/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["user_admin", "user_a"]
        self.assertEqual(response.data.get("meta").get("count"), 2)

    def test_roles_from_group_filter(self):
        """Test that a valid request to /tenant/<id>/groups/<uuid>/roles/ from an internal properly filters groups."""
        group_a_uuid = Group.objects.get(name="Group A").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_a_uuid}/roles/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Role A"]
        self.assertEqual(response.data.get("meta").get("count"), 1)

    def test_roles_for_group_principal_valid(self):
        """
        Test that a valid request to /tenant/<id>/principal/user_admin/groups/<uuid>/roles/
        from an internal account works.
        """
        group_all_uuid = Group.objects.get(name="Group All").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/principal/user_admin/groups/{group_all_uuid}/roles/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Role Admin", "Role A", "Role B"]
        self.assertEqual(response.data.get("meta").get("count"), 3)

    def test_roles_for_group_principal_invalid_account(self):
        """
        Test that a valid request to /tenant/<id>/principal/user_admin/groups/<uuid>/roles/
        from an external account fails.
        """
        group_all_uuid = Group.objects.get(name="Group All").uuid
        external_request_context = self._create_request_context(self.customer, self.user_data, is_internal=False)
        request = external_request_context["request"]
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/principal/user_admin/groups/{group_all_uuid}/roles/",
            **request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_roles_for_group_principal_invalid_uuid(self):
        """Test that a request to /tenant/<id>/principal/user_admin/groups/<uuid>/roles/ with an invalid uuid fails."""
        group_all_uuid = Group.objects.get(name="Group All").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/principal/user_admin/groups/{uuid.uuid4}/roles/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_roles_for_group_principal_filter(self):
        """Test that a valid request to /tenant/<id>/principal/user_admin/groups/<uuid>/roles/ filters properly."""
        # user_a in Group A
        group_a_uuid = Group.objects.get(name="Group A").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/principal/user_a/groups/{group_a_uuid}/roles/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Expecting ["Role A"]
        self.assertEqual(response.data.get("meta").get("count"), 1)

        # user_b not in Group A
        group_a_uuid = Group.objects.get(name="Group A").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/principal/user_b/groups/{group_a_uuid}/roles/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "isolated_principal",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_roles_for_org(self, mock_request):
        """Test that a valid request to /tenant/<id>>/roles/ | /tenant/<id>>/roles/?username=foo filters properly."""
        isolated_role = Role.objects.create(name="isolated_role", tenant=self.tenant)
        isolated_principal = Principal.objects.create(username="isolated_principal", tenant=self.tenant)
        isolated_group = Group.objects.create(name="isolated_group", tenant=self.tenant)
        isolated_policy = Policy.objects.create(name="isolated_policy", group=isolated_group, tenant=self.tenant)
        isolated_policy.roles.add(isolated_role)
        isolated_group.principals.add(isolated_principal)

        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/roles/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)

        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/roles/?username=isolated_principal",
            **self.request.META,
            follow=True,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 1)

    def test_tenants(self):
        """Test that we get tenants back on /tenant/"""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)

    def test_tenants_modified(self):
        """Test that we get tenants back on /tenant/"""
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/?modified_only=true",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)
        expected_org_ids = [t.org_id for t in [self.modifiedTenant1, self.modifiedTenant2]]
        actual_org_ids = [t["org_id"] for t in response.data.get("data")]
        self.assertEqual(sorted(expected_org_ids), sorted(actual_org_ids))

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "user_a",
                    "account_number": "1111111",
                    "is_active": True,
                },
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567474,
                    "username": "user_admin",
                    "account_number": "1111111",
                    "is_active": True,
                },
            ],
        },
    )
    def test_principals_for_group_offset_limit(self, mock_request):
        """Test that a valid request to /tenant/<id>/groups/<uuid>/principals/ returns principals in group with offset & limit set."""
        group_a_uuid = Group.objects.get(name="Group A").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_a_uuid}/principals/?offset=1&limit=10",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "user_a",
                    "account_number": "1111111",
                    "is_active": True,
                },
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567474,
                    "username": "user_admin",
                    "account_number": "1111111",
                    "is_active": True,
                },
            ],
        },
    )
    def test_principals_for_group_pagination_exists(self, mock_request):
        """Test that a valid request to /tenant/<id>/groups/<uuid>/principals/ returns principals in group with correct pagination links after pagination change."""
        group_a_uuid = Group.objects.get(name="Group A").uuid
        response = self.client.get(
            f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_a_uuid}/principals/",
            **self.request.META,
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that pagination contains correct links
        links = response.data.get("links")
        self.assertIn("next", links)
        self.assertIn("previous", links)
        self.assertIn("first", links)
        self.assertIn("last", links)

        # Check links have correct path set inside
        self.assertTrue(
            links["first"].startswith(
                f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_a_uuid}/principals/"
            )
        )
        self.assertTrue(
            links["last"].startswith(
                f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_a_uuid}/principals/"
            )
        )
        if links["previous"] is not None:
            self.assertTrue(
                links["previous"].startswith(
                    f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_a_uuid}/principals/"
                )
            )
        if links["next"] is not None:
            self.assertTrue(
                links["next"].startswith(
                    f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_a_uuid}/principals/"
                )
            )
