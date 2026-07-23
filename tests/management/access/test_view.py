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
"""Test the access view."""

from unittest.mock import patch, Mock

from api.models import CrossAccountRequest
from django.urls import reverse
from django.utils import timezone
from django.test.utils import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from api.models import Tenant, User
from datetime import timedelta

from management.cache import TenantCache
from management.models import Group, Permission, Principal, ResourceDefinition, Policy, Role, Access, Workspace
from management.relation_replicator.noop_replicator import NoopReplicator
from management.tenant_service.v2 import V2TenantBootstrapService
from tests.identity_request import IdentityRequest


class AccessViewTests(IdentityRequest):
    """Test the access view."""

    def setUp(self):
        """Set up the access view tests."""
        super().setUp()
        request = self.request_context["request"]
        user = User()
        user.username = self.user_data["username"]
        user.account = self.customer_data["account_id"]
        user.org_id = self.customer_data["org_id"]
        request.user = user
        self.public_tenant = Tenant.objects.get(tenant_name="public")

        self.access_data = {
            "permission": "app:*:*",
            "resourceDefinitions": [{"attributeFilter": {"key": "key1.id", "operation": "equal", "value": "value1"}}],
        }

        test_tenant_org_id = "100001"

        # we need to delete old test_tenant's that may exist in cache
        TENANTS = TenantCache()
        TENANTS.delete_tenant(test_tenant_org_id)

        # items with test_ prefix have hard coded attributes for new BOP requests
        self.test_tenant = Tenant(
            tenant_name="acct1111111", account_id="1111111", org_id=test_tenant_org_id, ready=True
        )
        self.test_tenant.save()
        bootstrap_service = V2TenantBootstrapService(replicator=NoopReplicator())
        bootstrap_service.bootstrap_tenant(self.tenant)
        bootstrap_service.bootstrap_tenant(self.test_tenant)
        self.test_principal = Principal(username="test_user", tenant=self.test_tenant)
        self.test_principal.save()
        self.test_group = Group(name="test_groupA", tenant=self.test_tenant)
        self.test_group.save()
        self.test_group.principals.add(self.test_principal)
        self.test_group.save()
        self.test_permission = Permission.objects.create(permission="app:test_*:test_*", tenant=self.test_tenant)
        Permission.objects.create(permission="app:test_foo:test_bar", tenant=self.test_tenant)
        user_data = {"username": "test_user", "email": "test@gmail.com"}
        request_context = self._create_request_context(
            {"account_id": "1111111", "tenant_name": "acct1111111", "org_id": "100001"}, user_data, is_org_admin=True
        )
        request = request_context["request"]
        self.test_headers = request.META
        test_tenant_root_workspace, _ = Workspace.objects.get_or_create(
            tenant=self.test_tenant,
            type=Workspace.Types.ROOT,
            defaults={"name": "Test Tenant Root Workspace"},
        )
        Workspace.objects.get_or_create(
            tenant=self.test_tenant,
            type=Workspace.Types.DEFAULT,
            defaults={"name": "Test Tenant Default Workspace", "parent": test_tenant_root_workspace},
        )

        self.principal = Principal(username=user.username, tenant=self.tenant)
        self.principal.save()
        self.admin_principal = Principal(username="user_admin", tenant=self.tenant)
        self.admin_principal.save()
        self.group = Group(name="groupA", tenant=self.tenant)
        self.group.save()
        self.group.principals.add(self.principal)
        self.group.save()
        self.permission = Permission.objects.create(permission="app:*:*", tenant=self.tenant)
        Permission.objects.create(permission="app:foo:bar", tenant=self.tenant)
        tenant_root_workspace, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
            defaults={"name": "root", "description": "Root workspace"},
        )
        self.default_ws, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            defaults={"name": "Tenant Default Workspace", "parent": tenant_root_workspace},
        )

        customer_data = {
            "account_id": self.tenant.account_id,
            "tenant_name": self.tenant.tenant_name,
            "org_id": self.tenant.org_id,
        }

        service_account_data = self._create_service_account_data()
        self.service_account = Principal(
            username=service_account_data["username"],
            tenant=self.tenant,
            type="service-account",
            service_account_id=service_account_data["client_id"],
        )
        self.service_account.save()

        request_context_service_account_principal = self._create_request_context(
            customer_data=customer_data,
            service_account_data=service_account_data,
            is_org_admin=False,
        )
        self.headers_service_account = request_context_service_account_principal["request"].META

    def tearDown(self):
        """Tear down access view tests."""
        # Clear the principal cache for the test tenant to avoid test isolation issues
        from management.utils import PRINCIPAL_CACHE

        PRINCIPAL_CACHE.delete_all_principals_for_tenant("100001")
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)

        super().tearDown()

    def create_role(self, role_name, headers, in_access_data=None):
        """Create a role."""
        access_data = self.access_data
        if in_access_data:
            access_data = in_access_data
        test_data = {"name": role_name, "access": [access_data]}

        # create a role
        url = reverse("v1_management:role-list")
        client = APIClient()
        response = client.post(url, test_data, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response

    def create_policy(self, policy_name, group, roles, tenant):
        """Create a policy."""
        # create a policy
        policy = Policy.objects.create(name=policy_name, tenant=tenant, system=True)
        for role in Role.objects.filter(uuid__in=roles):
            policy.roles.add(role)
        policy.group = Group.objects.get(uuid=group)
        policy.save()

    def create_platform_default_resource(self):
        """Setup default group and role."""
        default_permission = Permission.objects.create(permission="default:*:*", tenant=self.tenant)
        default_role = Role.objects.create(name="default role", platform_default=True, system=True, tenant=self.tenant)
        default_access = Access.objects.create(permission=default_permission, role=default_role, tenant=self.tenant)
        default_policy = Policy.objects.create(name="default policy", system=True, tenant=self.tenant)
        default_policy.roles.add(default_role)
        default_group = Group.objects.create(
            name="default group", system=True, platform_default=True, tenant=self.tenant
        )
        default_group.policies.add(default_policy)

    def create_role_and_permission(self, role_name, permission, system=False):
        role = Role.objects.create(name=role_name, tenant=self.tenant, system=system)
        assigned_permission = Permission.objects.create(permission=permission, tenant=self.tenant)
        access = Access.objects.create(role=role, permission=assigned_permission, tenant=self.tenant)
        return role

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch("management.access.view.is_v2_edit_enabled_for_request", return_value=False)
    def test_get_access_success(self, _mock_v2):
        """Test that we can obtain the expected access without pagination."""
        from management.access.view import v1_access_by_v2_org_total

        role_name = "roleA"
        response = self.create_role(role_name, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.permission, tenant=self.tenant)
        policy_name = "policyA"
        self.create_policy(policy_name, self.group.uuid, [role_uuid], tenant=self.tenant)
        # Create platform default group, and add roles to it.
        self.create_platform_default_resource()

        # Test that we can retrieve the principal access
        url = "{}?application={}".format(reverse("v1_management:access"), "app")
        client = APIClient()
        before = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="app", caller_type="user"
        )._value.get()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 2)
        self.assertEqual(response.data.get("meta").get("limit"), 2)
        self.assertEqual(self.access_data, response.data.get("data")[0])
        after = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="app", caller_type="user"
        )._value.get()
        self.assertEqual(after, before)

        # the platform default permission could also be retrieved
        url = "{}?application={}".format(reverse("v1_management:access"), "default")
        response = client.get(url, **self.headers)
        self.assertEqual({"permission": "default:*:*", "resourceDefinitions": []}, response.data.get("data")[0])

    def test_get_empty_access_with_service_account(self):
        """Test the service account that not belongs to any custom group returns no permissions."""
        url = reverse("v1_management:access") + f"?application="
        client = APIClient()
        response = client.get(url, **self.headers_service_account)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertEqual(response.data.get("data"), [])
        self.assertEqual(response.data.get("meta").get("limit"), 0)
        self.assertEqual(response.data.get("meta").get("offset"), 0)
        self.assertEqual(response.data.get("meta").get("count"), 0)

    @override_settings(ROLE_CREATE_ALLOW_LIST="my_appA,my_appB")
    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    @patch("management.principal.it_service.ITService.request_service_accounts")
    def test_get_access_with_service_account(self, sa_mock):
        """Test that we can obtain the expected access without pagination with service account in headers."""
        # Create service account and headers
        customer_data = {
            "account_id": self.tenant.account_id,
            "tenant_name": self.tenant.tenant_name,
            "org_id": self.tenant.org_id,
        }

        sa_data = self._create_service_account_data()
        sa = Principal.objects.create(
            username=sa_data["username"],
            tenant=self.tenant,
            type="service-account",
            service_account_id=sa_data["client_id"],
        )

        request_context_sa_principal = self._create_request_context(
            customer_data=customer_data,
            service_account_data=sa_data,
            is_org_admin=False,
        )
        headers_sa = request_context_sa_principal["request"].META

        # Create a custom role with 2 permissions
        p1 = Permission.objects.create(permission="my_appA:resource_A:*", tenant=self.tenant)
        p2 = Permission.objects.create(permission="my_appB:resource_B:*", tenant=self.tenant)
        access_data = [
            {"permission": "my_appA:resource_A:*", "resourceDefinitions": []},
            {"permission": "my_appB:resource_B:*", "resourceDefinitions": []},
        ]
        test_data = {"name": "service account test role", "access": access_data}
        client = APIClient()
        url = reverse("v1_management:role-list")
        response = client.post(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)

        # Create a custom group
        test_data = {"name": "service account test group"}
        url = reverse("v1_management:group-list")
        response = client.post(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        group_uuid = response.data.get("uuid")
        group = Group.objects.get(uuid=group_uuid)

        # Add the service account into group
        sa_uuid = str(sa.service_account_id)
        sa_mock.return_value = [
            {
                "clientId": sa_uuid,
                "name": f"service_account_name_{sa_uuid.split('-')[0]}",
                "description": f"Service Account description {sa_uuid.split('-')[0]}",
                "owner": "jsmith",
                "username": "service_account-" + sa_uuid,
                "time_created": 1706784741,
                "type": "service-account",
            }
        ]
        url = reverse("v1_management:group-principals", kwargs={"uuid": group.uuid})
        request_body = {"principals": [{"clientId": sa_uuid, "type": "service-account"}]}
        response = client.post(url, request_body, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Add the role into group
        url = reverse("v1_management:group-roles", kwargs={"uuid": group.uuid})
        test_data = {"roles": [role.uuid]}
        response = client.post(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check we get all permissions for the service account
        url = reverse("v1_management:access") + "?application="
        response = client.get(url, **headers_sa)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data.get("data")
        self.assertIsNotNone(data)
        permissions = [p["permission"] for p in data]
        self.assertIn(p1.permission, permissions)
        self.assertIn(p2.permission, permissions)
        self.assertEqual(response.data.get("meta").get("limit"), 2)
        self.assertEqual(response.data.get("meta").get("offset"), 0)
        self.assertEqual(response.data.get("meta").get("count"), 2)

        # Check we get only expected permissions when app name is in the query
        url = reverse("v1_management:access") + "?application=my_appA"
        response = client.get(url, **headers_sa)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data.get("data")
        self.assertIsNotNone(data)
        permissions = [p["permission"] for p in data]
        self.assertIn(p1.permission, permissions)
        self.assertEqual(response.data.get("meta").get("limit"), 1)
        self.assertEqual(response.data.get("meta").get("offset"), 0)
        self.assertEqual(response.data.get("meta").get("count"), 1)

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    def test_get_access_to_return_unique_records(self):
        """Test that we can obtain the expected access without pagination."""
        role_name = "roleA"
        response = self.create_role(role_name, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.permission, tenant=self.tenant)
        policy_name = "policyA"
        self.create_policy(policy_name, self.group.uuid, [role_uuid], tenant=self.tenant)
        # Create platform default group, and add roles to it.
        self.create_platform_default_resource()

        role_name_b = "roleB"
        response = self.create_role(role_name_b, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_b_uuid = response.data.get("uuid")
        Role.objects.get(uuid=role_b_uuid)
        policy_name = "policyB"
        self.create_policy(policy_name, self.group.uuid, [role_b_uuid], tenant=self.tenant)

        # Test that we can retrieve the principal access
        url = "{}?application={}".format(reverse("v1_management:access"), "app")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 2)
        self.assertEqual(response.data.get("meta").get("limit"), 2)
        for access_data in response.data.get("data"):
            if access_data.get("resourceDefinitions"):
                self.assertEqual(self.access_data, access_data)

        self.assertEqual(Access.objects.filter(permission__id=self.permission.id).count(), 3)

        # the platform default permission could also be retrieved
        url = "{}?application={}".format(reverse("v1_management:access"), "default")
        response = client.get(url, **self.headers)
        self.assertEqual({"permission": "default:*:*", "resourceDefinitions": []}, response.data.get("data")[0])

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch("rbac.middleware.FEATURE_FLAGS.is_add_ungrouped_hosts_id_enabled", return_value=False)
    @patch("rbac.middleware.FEATURE_FLAGS.is_remove_null_value_enabled", return_value=False)
    def test_get_access_replace_null_value(
        self, ff_is_remove_null_value_enabled: Mock, ff_is_add_ungrouped_hosts_id_enabled: Mock
    ):
        """Test that we can obtain the expected access without pagination."""
        role_name = "roleA"
        response = self.create_role(role_name, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.permission, tenant=self.tenant)
        resourceDef = ResourceDefinition.objects.create(
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [None, "uuid"],
            },
            access=access,
            tenant=self.tenant,
        )
        policy_name = "policyA"
        self.create_policy(policy_name, self.group.uuid, [role_uuid], tenant=self.tenant)

        # Test that we can retrieve the principal access and null value is not replaced
        url = f'{reverse("v1_management:access")}?application='
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for access_data in response.data.get("data"):
            for resourceDef in access_data.get("resourceDefinitions", []):
                if resourceDef.get("key", None) == "group.id":
                    self.assertEqual(resourceDef.get("value"), [None, "uuid"])
                    break

        # Test that we can retrieve the principal access
        # and null value is not replaced if there is no ungrouped workspace
        ff_is_add_ungrouped_hosts_id_enabled.return_value = True
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for access_data in response.data.get("data"):
            for resourceDef in access_data.get("resourceDefinitions", []):
                if resourceDef.get("key", None) == "group.id":
                    self.assertEqual(resourceDef.get("value"), [None, "uuid"])
                    break

        # Test that we can retrieve the principal access
        # if there is an ungrouped workspace, its id will be added
        ungrouped_hosts_id = Workspace.objects.create(
            name="Ungrouped Workspace",
            type=Workspace.Types.UNGROUPED_HOSTS,
            tenant=self.tenant,
            parent=self.default_ws,
        )

        ff_is_add_ungrouped_hosts_id_enabled.return_value = False
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for access_data in response.data.get("data"):
            for resourceDef in access_data.get("resourceDefinitions", []):
                if resourceDef.get("key", None) == "group.id":
                    self.assertEqual(resourceDef.get("value"), [None, str(ungrouped_hosts_id), "uuid"])
                    break

        # Test that we can retrieve the principal access
        # and null value is replaced if there is a ungrouped workspace
        ff_is_add_ungrouped_hosts_id_enabled.return_value = False
        ff_is_remove_null_value_enabled.return_value = False
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for access_data in response.data.get("data"):
            for resourceDef in access_data.get("resourceDefinitions", []):
                if resourceDef.get("key", None) == "group.id":
                    self.assertEqual(resourceDef.get("value"), [str(ungrouped_hosts_id), "uuid"])
                    break

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch("rbac.middleware.FEATURE_FLAGS.is_add_ungrouped_hosts_id_enabled", return_value=True)
    @patch("rbac.middleware.FEATURE_FLAGS.is_remove_null_value_enabled", return_value=True)
    def test_get_access_with_attribute_filter_value_of_none(
        self, ff_is_remove_null_value_enabled: Mock, ff_is_add_ungrouped_hosts_id_enabled: Mock
    ):
        """Test that access view handles attributeFilter values of None correctly."""
        role_name = "roleB"
        response = self.create_role(role_name, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.permission, tenant=self.tenant)

        # Create ungrouped workspace for the test
        ungrouped_hosts = Workspace.objects.create(
            name="Ungrouped Workspace",
            type=Workspace.Types.UNGROUPED_HOSTS,
            tenant=self.tenant,
            parent=self.default_ws,
        )
        ungrouped_hosts_id = str(ungrouped_hosts.id)

        # Test case 1: attributeFilter value is None (not a list)
        ResourceDefinition.objects.create(
            attributeFilter={
                "key": "group.id",
                "operation": "equal",
                "value": None,
            },
            access=access,
            tenant=self.tenant,
        )

        # Test case 2: attributeFilter value is a string (not a list)
        access2 = Access.objects.create(role=role, permission=self.permission, tenant=self.tenant)
        ResourceDefinition.objects.create(
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": ["some-uuid", None],
            },
            access=access2,
            tenant=self.tenant,
        )

        policy_name = "policyB"
        self.create_policy(policy_name, self.group.uuid, [role_uuid], tenant=self.tenant)

        # Test that the view handles non-list values without crashing
        url = f'{reverse("v1_management:access")}?application='
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify that the response contains data and doesn't crash
        response_data = response.data.get("data")
        self.assertIsNotNone(response_data)

        for access_data in response_data:
            for resourceDef in access_data.get("resourceDefinitions", []):
                attributeFilter = resourceDef.get("attributeFilter")
                if attributeFilter.get("key") != "group.id":
                    continue
                if attributeFilter.get("operation") == "in":
                    self.assertEqual(attributeFilter.get("value"), ["some-uuid", ungrouped_hosts_id])
                else:
                    self.assertEqual(attributeFilter.get("value"), ungrouped_hosts_id)

    def test_access_for_cross_account_principal_return_permissions_based_on_assigned_system_role(self):
        self.create_platform_default_resource()
        client = APIClient()
        url = "{}?application=".format(reverse("v1_management:access"))
        account_id = self.customer_data["account_id"]
        org_id = self.customer_data["org_id"]
        user_id = "123456"
        user_name = f"{account_id}-{user_id}"

        # create system role with access
        system_role = Role.objects.create(name="Test Role one", tenant=self.public_tenant, system=True)
        assigned_permission = Permission.objects.create(
            permission="test:assigned:permission1", tenant=self.public_tenant
        )
        Access.objects.create(role=system_role, permission=assigned_permission, tenant=self.tenant)

        cross_account_request = CrossAccountRequest.objects.create(
            user_id=user_id,
            target_org=org_id,
            end_date=timezone.now() + timedelta(10),
            status="approved",
        )
        cross_account_request.roles.add(system_role)

        # create non-system role with access and same name as system role
        role = Role.objects.create(name="Test Role one", tenant=self.tenant, system=False)
        assigned_permission = Permission.objects.create(
            permission="test:assigned:permission2", tenant=self.public_tenant
        )
        Access.objects.create(role=role, permission=assigned_permission, tenant=self.tenant)

        # Create cross_account principal and role, permission in the account
        user_data = {"username": user_name, "email": "test@gmail.com"}
        request_context = self._create_request_context(self.customer_data, user_data, is_org_admin=False)
        request = request_context["request"]
        self.test_headers = request.META
        Principal.objects.create(username=user_name, cross_account=True, tenant=self.tenant)

        response = client.get(url, **self.test_headers)

        # only assigned role permissions without platform default permission
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 1)

        self.assertEqual(response.data.get("data")[0]["permission"], "test:assigned:permission1")

    def test_access_for_cross_account_principal_return_permissions_based_on_assigned_role(self):
        """Test that the expected access for cross account principal return permissions based on assigned role."""
        # setup default group/role
        self.create_platform_default_resource()
        client = APIClient()
        url = "{}?application=".format(reverse("v1_management:access"))
        account_id = self.customer_data["account_id"]
        org_id = self.customer_data["org_id"]
        user_id = "123456"
        user_name = f"{account_id}-{user_id}"

        # setup cross account request, role and permission in public schema
        ## This CAR will provide permission: "test:assigned:permission"
        role = self.create_role_and_permission("Test Role one", "test:assigned:permission1", True)
        cross_account_request = CrossAccountRequest.objects.create(
            user_id=user_id,
            target_org=org_id,
            end_date=timezone.now() + timedelta(10),
            status="approved",
        )
        cross_account_request.roles.add(role)
        ## CAR below will provide permission: "app:*:*"
        role = self.create_role_and_permission("Test Role two", "test:assigned:permission2", True)
        cross_account_request = CrossAccountRequest.objects.create(
            user_id=user_id,
            target_org=org_id,
            end_date=timezone.now() + timedelta(20),
            status="approved",
        )
        cross_account_request.roles.add(role)

        # Create cross_account principal and role, permission in the account
        user_data = {"username": user_name, "email": "test@gmail.com"}
        request_context = self._create_request_context(self.customer_data, user_data, is_org_admin=False)
        request = request_context["request"]
        self.test_headers = request.META
        Principal.objects.create(username=user_name, cross_account=True, tenant=self.tenant)

        response = client.get(url, **self.test_headers)

        # only assigned role permissions without platform default permission
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), 2)
        permissions = [access["permission"] for access in response.data.get("data")]
        self.assertListEqual(permissions, ["test:assigned:permission1", "test:assigned:permission2"])

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_no_app_supplied(self, mock_request):
        """Test that we return all permissions when no app supplied."""

        role_name = "roleA"
        policy_name = "policyA"
        access_data = {
            "permission": "app:test_foo:test_bar",
            "resourceDefinitions": [{"attributeFilter": {"key": "keyA.id", "operation": "equal", "value": "valueA"}}],
        }
        response = self.create_role(role_name, self.test_headers, access_data)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.test_permission, tenant=self.test_tenant)
        self.create_policy(policy_name, self.test_group.uuid, [role_uuid], self.test_tenant)

        url = "{}?application=&username={}".format(reverse("v1_management:access"), self.test_principal.username)
        client = APIClient()
        response = client.get(url, **self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 2)
        self.assertEqual(response.data.get("meta").get("limit"), 2)

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_multiple_apps_supplied(self, mock_request):
        """Test that we return all permissions for multiple apps when supplied."""

        role_name = "roleA"
        policy_name = "policyA"
        access_data = {
            "permission": "app:test_foo:test_bar",
            "resourceDefinitions": [{"attributeFilter": {"key": "keyA.id", "operation": "equal", "value": "valueA"}}],
        }
        response = self.create_role(role_name, self.test_headers, access_data)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.test_permission, tenant=self.test_tenant)
        self.create_policy(policy_name, self.test_group.uuid, [role_uuid], self.test_tenant)

        url = "{}?application={}&username={}".format(reverse("v1_management:access"), "app,app2", "test_user")
        client = APIClient()
        response = client.get(url, **self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 2)

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_no_partial_match(self, mock_request):
        """Test that we can have a partial match on app/permission."""

        role_name = "roleA"
        policy_name = "policyA"
        access_data = {
            "permission": "app:test_foo:test_bar",
            "resourceDefinitions": [{"attributeFilter": {"key": "keyA.id", "operation": "equal", "value": "valueA"}}],
        }
        response = self.create_role(role_name, self.test_headers, access_data)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.test_permission, tenant=self.test_tenant)
        self.create_policy(policy_name, self.test_group.uuid, [role_uuid], self.test_tenant)

        url = "{}?application={}&username={}".format(
            reverse("v1_management:access"), "ap", self.test_principal.username
        )
        client = APIClient()
        response = client.get(url, **self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 0)
        self.assertEqual(response.data.get("meta").get("limit"), 0)

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_no_subset_match(self, mock_request):
        """Test that we cannot have a subset match on app/permission."""

        role_name = "roleA"
        policy_name = "policyA"
        access_data = {
            "permission": "app:test_foo:test_bar",
            "resourceDefinitions": [{"attributeFilter": {"key": "keyA.id", "operation": "equal", "value": "valueA"}}],
        }
        response = self.create_role(role_name, self.test_headers, access_data)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.test_permission, tenant=self.test_tenant)
        self.create_policy(policy_name, self.test_group.uuid, [role_uuid], self.test_tenant)

        url = "{}?application={}&username={}".format(
            reverse("v1_management:access"), "appfoo", self.test_principal.username
        )
        client = APIClient()
        response = client.get(url, **self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 0)
        self.assertEqual(response.data.get("meta").get("limit"), 0)

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch("management.cache.AccessCache.save_policy", return_value=None)
    @patch("management.cache.AccessCache.get_policy", return_value=None)
    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_with_ordering_and_cache(self, mock_request, get_policy, save_policy):
        """Test that we can obtain the expected access with ordering and cache."""

        role_name = "roleA"
        response = self.create_role(role_name, self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        test_role = self.create_role_and_permission("Test Role one", "test:assigned:permission1")
        policy_name = "policyA"
        response = self.create_policy(policy_name, self.test_group.uuid, [role_uuid, test_role.uuid], self.test_tenant)

        tenant_name = self.test_tenant.tenant_name
        principal_id = self.test_principal.uuid
        principal_username = self.test_principal.username
        key = f"rbac::policy::tenant={tenant_name}::user={principal_id}"
        client = APIClient()

        #### Sort by application ####
        url = "{}?application=&username={}&order_by={}".format(
            reverse("v1_management:access"), self.test_principal.username, "application"
        )
        response = client.get(url, **self.test_headers)

        # Cache is called saved with sub_key "&order:application&is_org_admin:True"
        get_policy.assert_called_with(principal_id, "&order:application&is_org_admin:True")
        called_with_para = save_policy.mock_calls[0][1]
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("&order:application&is_org_admin:True", called_with_para[1])
        self.assertEqual(2, len(called_with_para[2]))  # it catches all the policies
        self.assertEqual(response.data["meta"]["count"], 2)
        self.assertEqual(response.data["data"][0]["permission"], "app:*:*")  # check order

        url = "{}?application=&username={}&order_by={}".format(
            reverse("v1_management:access"), self.test_principal.username, "-application"
        )
        response = client.get(url, **self.test_headers)
        get_policy.assert_called_with(principal_id, "&order:-application&is_org_admin:True")
        called_with_para = save_policy.mock_calls[1][1]
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("&order:-application&is_org_admin:True", called_with_para[1])
        self.assertEqual(2, len(called_with_para[2]))  # it catches all the policies
        self.assertEqual(response.data["meta"]["count"], 2)
        # Response data is in reverse order
        self.assertEqual(response.data["data"][0]["permission"], "test:assigned:permission1")  # check order

        #### Sort by resource_type ####
        url = "{}?application=&username={}&order_by={}".format(
            reverse("v1_management:access"), self.test_principal.username, "resource_type"
        )
        response = client.get(url, **self.test_headers)

        # Cache is called saved with sub_key "&order:resource_type&is_org_admin:True"
        get_policy.assert_called_with(principal_id, "&order:resource_type&is_org_admin:True")
        called_with_para = save_policy.mock_calls[2][1]
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("&order:resource_type&is_org_admin:True", called_with_para[1])
        self.assertEqual(2, len(called_with_para[2]))  # it catches all the policies
        self.assertEqual(response.data["meta"]["count"], 2)
        self.assertEqual(response.data["data"][0]["permission"], "app:*:*")  # check order

        url = "{}?application=&username={}&order_by={}".format(
            reverse("v1_management:access"), self.test_principal.username, "-resource_type"
        )
        response = client.get(url, **self.test_headers)
        get_policy.assert_called_with(principal_id, "&order:-resource_type&is_org_admin:True")
        called_with_para = save_policy.mock_calls[3][1]
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("&order:-resource_type&is_org_admin:True", called_with_para[1])
        self.assertEqual(2, len(called_with_para[2]))  # it catches all the policies
        self.assertEqual(response.data["meta"]["count"], 2)
        # Response data is in reverse order
        self.assertEqual(response.data["data"][0]["permission"], "test:assigned:permission1")  # check order

        #### Sort by verb ####
        url = "{}?application=&username={}&order_by={}".format(
            reverse("v1_management:access"), self.test_principal.username, "verb"
        )
        response = client.get(url, **self.test_headers)

        # Cache is called saved with sub_key "&order:verb&is_org_admin:True"
        get_policy.assert_called_with(principal_id, "&order:verb&is_org_admin:True")
        called_with_para = save_policy.mock_calls[4][1]
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("&order:verb&is_org_admin:True", called_with_para[1])
        self.assertEqual(2, len(called_with_para[2]))  # it catches all the policies
        self.assertEqual(response.data["meta"]["count"], 2)
        self.assertEqual(response.data["data"][0]["permission"], "app:*:*")  # check order

        url = "{}?application=&username={}&order_by={}".format(
            reverse("v1_management:access"), self.test_principal.username, "-verb"
        )
        response = client.get(url, **self.test_headers)
        # Cache is called saved with sub_key "&order:-verb&is_org_admin:True"
        get_policy.assert_called_with(principal_id, "&order:-verb&is_org_admin:True")
        called_with_para = save_policy.mock_calls[5][1]
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("&order:-verb&is_org_admin:True", called_with_para[1])
        self.assertEqual(2, len(called_with_para[2]))  # it catches all the policies
        self.assertEqual(response.data["meta"]["count"], 2)
        # Response data is in reverse order
        self.assertEqual(response.data["data"][0]["permission"], "test:assigned:permission1")  # check order

        #### Sort by nothing still works ####
        url = "{}?application=&username={}&order_by=".format(
            reverse("v1_management:access"), self.test_principal.username
        )
        response = client.get(url, **self.test_headers)
        # Cache is called saved with sub_key "&is_org_admin:True"
        get_policy.assert_called_with(principal_id, "&is_org_admin:True")
        called_with_para = save_policy.mock_calls[6][1]
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("&is_org_admin:True", called_with_para[1])
        self.assertEqual(2, len(called_with_para[2]))  # it catches all the policies
        self.assertEqual(response.data["meta"]["count"], 2)

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_no_match(self, mock_request):
        """Test that we only match on the application name of the permission data."""

        role_name = "roleA"
        policy_name = "policyA"
        access_data = {
            "permission": "app:test_foo:test_bar",
            "resourceDefinitions": [{"attributeFilter": {"key": "keyA.id", "operation": "equal", "value": "valueA"}}],
        }
        response = self.create_role(role_name, self.test_headers, access_data)
        role_uuid = response.data.get("uuid")
        role = Role.objects.get(uuid=role_uuid)
        access = Access.objects.create(role=role, permission=self.test_permission, tenant=self.test_tenant)
        self.create_policy(policy_name, self.test_group.uuid, [role_uuid], self.test_tenant)

        url = "{}?application={}&username={}".format(
            reverse("v1_management:access"), "test_foo", self.test_principal.username
        )
        client = APIClient()
        response = client.get(url, **self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 0)
        self.assertEqual(response.data.get("meta").get("limit"), 0)

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_with_limit(self, mock_request):
        """Test that we can obtain the expected access with pagination."""

        role_name = "roleA"
        response = self.create_role(role_name, self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        policy_name = "policyA"
        response = self.create_policy(policy_name, self.test_group.uuid, [role_uuid], self.test_tenant)

        # test that we can retrieve the principal access
        url = "{}?application={}&username={}&limit=1".format(
            reverse("v1_management:access"), "app", self.test_principal.username
        )
        client = APIClient()
        response = client.get(url, **self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data.get("data"))
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 1)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        self.assertEqual(response.data.get("meta").get("limit"), 1)
        self.assertEqual(self.access_data, response.data.get("data")[0])

    def test_missing_query_params(self):
        """Test that we get expected failure when missing required query params."""
        url = "{}?page={}".format(reverse("v1_management:access"), "3")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    def test_missing_invalid_username(self, mock_request):
        """Test that we get expected failure when missing required query params."""
        url = "{}?application={}&username={}".format(reverse("v1_management:access"), "app", "test_user")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(ROLE_CREATE_ALLOW_LIST="app")
    @patch("management.cache.AccessCache.get_policy", return_value=None)
    @patch("management.cache.AccessCache.save_policy", return_value=None)
    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_with_pagination_and_cache(self, mock_request, save_policy, get_policy):
        """Test that we can obtain the expected access with pagination and cache."""

        role_name = "roleA"
        response = self.create_role(role_name, self.test_headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        test_role = self.create_role_and_permission("Test Role one", "test:assigned:permission1")
        policy_name = "policyA"
        response = self.create_policy(policy_name, self.test_group.uuid, [role_uuid, test_role.uuid], self.test_tenant)

        tenant_name = self.test_tenant.tenant_name
        principal_id = self.test_principal.uuid
        key = f"rbac::policy::tenant={tenant_name}::user={principal_id}"
        client = APIClient()

        ######## access_policy are cached with desired sub_key ############
        url = "{}?application={}&username={}&offset=1&limit=1".format(
            reverse("v1_management:access"), "app", self.test_principal.username
        )
        response = client.get(url, **self.test_headers)

        get_policy.assert_called_with(principal_id, "app&is_org_admin:True")
        called_with_para = save_policy.mock_calls[0][1]  # save_policy params
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("app&is_org_admin:True", called_with_para[1])
        self.assertEqual([self.access_data], called_with_para[2])  # it catches all the policies for app
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(
            response.data["data"], []
        )  # after pagination, it is empty because total is one, and offset is one
        ###################################################################

        #### access_policy are cached properly when application is empty ####
        url = "{}?application=&username={}&limit=1".format(
            reverse("v1_management:access"), self.test_principal.username
        )
        response = client.get(url, **self.test_headers)

        # Cache is called saved with sub_key "&is_org_admin:True"
        get_policy.assert_called_with(principal_id, "&is_org_admin:True")
        called_with_para = save_policy.mock_calls[1][1]
        self.assertEqual(principal_id, called_with_para[0])
        self.assertEqual("&is_org_admin:True", called_with_para[1])
        self.assertEqual(2, len(called_with_para[2]))  # it catches all the policies for app
        self.assertEqual(response.data["meta"]["count"], 2)
        self.assertEqual(len(response.data["data"]), 1)  # returns one policy because limit is 1

    def test_get_access_with_invalid_ordering_value(self):
        """Test that get access with invalid ordering value raises 401."""

        client = APIClient()
        url = "{}?application={}&username={}&order_by={}".format(
            reverse("v1_management:access"), "app", self.test_principal.username, "invalid_value"
        )
        response = client.get(url, **self.test_headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_access_with_invalid_limit(self):
        """Test that we get expected value of limit when invalid limit value is provided in the request."""
        # Create platform default group with default roles
        self.create_platform_default_resource()

        client = APIClient()
        default_limit = 10
        expected_count = 1

        # Request query without 'limit' query param => default value 'limit=10' in the response
        url = f"{reverse('v1_management:access')}?application="
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("limit"), expected_count)
        self.assertEqual(response.data.get("meta").get("count"), expected_count)

        # Request query with invalid value of 'limit' query param => default value 'limit=10 'in the response
        for limit in ["", 0, -10, "xxxxx"]:
            url = f"{reverse('v1_management:access')}?application=&limit={limit}"
            response = client.get(url, **self.headers)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data.get("meta").get("limit"), default_limit)
            self.assertEqual(response.data.get("meta").get("count"), expected_count)

    @override_settings(ROLE_CREATE_ALLOW_LIST="test_app")
    @override_settings(V1_ROLE_PERMISSION_BLOCK_LIST=["test_app:resource:blocked_action"])
    def test_get_access_filters_blocked_permissions_for_v1(self):
        """Test that blocked permissions are filtered out in v1 access endpoint."""
        # Create a role with both blocked and non-blocked permissions
        blocked_permission = Permission.objects.create(
            permission="test_app:resource:blocked_action", tenant=self.tenant
        )
        allowed_permission = Permission.objects.create(
            permission="test_app:resource:allowed_action", tenant=self.tenant
        )

        role = Role.objects.create(name="test_role_with_blocked", tenant=self.tenant)
        Access.objects.create(role=role, permission=blocked_permission, tenant=self.tenant)
        Access.objects.create(role=role, permission=allowed_permission, tenant=self.tenant)

        # Create policy linking role to group
        policy = Policy.objects.create(name="test_policy_blocked", tenant=self.tenant)
        policy.roles.add(role)
        policy.group = self.group
        policy.save()

        # Make request to v1 access endpoint
        url = f"{reverse('v1_management:access')}?application=test_app"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permissions = [item["permission"] for item in response.data.get("data", [])]

        # Blocked permission should NOT be in the response
        self.assertNotIn("test_app:resource:blocked_action", permissions)
        # Allowed permission should be in the response
        self.assertIn("test_app:resource:allowed_action", permissions)

    @override_settings(ROLE_CREATE_ALLOW_LIST="test_app")
    @override_settings(V1_ROLE_PERMISSION_BLOCK_LIST=["test_app:resource:blocked1", "test_app:resource:blocked2"])
    def test_get_access_filters_multiple_blocked_permissions(self):
        """Test that multiple blocked permissions are all filtered out."""
        # Create permissions
        blocked1 = Permission.objects.create(permission="test_app:resource:blocked1", tenant=self.tenant)
        blocked2 = Permission.objects.create(permission="test_app:resource:blocked2", tenant=self.tenant)
        allowed = Permission.objects.create(permission="test_app:resource:allowed", tenant=self.tenant)

        role = Role.objects.create(name="test_role_multi_blocked", tenant=self.tenant)
        Access.objects.create(role=role, permission=blocked1, tenant=self.tenant)
        Access.objects.create(role=role, permission=blocked2, tenant=self.tenant)
        Access.objects.create(role=role, permission=allowed, tenant=self.tenant)

        policy = Policy.objects.create(name="test_policy_multi_blocked", tenant=self.tenant)
        policy.roles.add(role)
        policy.group = self.group
        policy.save()

        url = f"{reverse('v1_management:access')}?application=test_app"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permissions = [item["permission"] for item in response.data.get("data", [])]

        # Both blocked permissions should NOT be in the response
        self.assertNotIn("test_app:resource:blocked1", permissions)
        self.assertNotIn("test_app:resource:blocked2", permissions)
        # Allowed permission should be in the response
        self.assertIn("test_app:resource:allowed", permissions)

    @override_settings(ROLE_CREATE_ALLOW_LIST="test_app")
    @override_settings(V1_ROLE_PERMISSION_BLOCK_LIST=[])
    def test_get_access_returns_all_permissions_when_block_list_empty(self):
        """Test that all permissions are returned when block list is empty."""
        permission1 = Permission.objects.create(permission="test_app:resource:action1", tenant=self.tenant)
        permission2 = Permission.objects.create(permission="test_app:resource:action2", tenant=self.tenant)

        role = Role.objects.create(name="test_role_no_block", tenant=self.tenant)
        Access.objects.create(role=role, permission=permission1, tenant=self.tenant)
        Access.objects.create(role=role, permission=permission2, tenant=self.tenant)

        policy = Policy.objects.create(name="test_policy_no_block", tenant=self.tenant)
        policy.roles.add(role)
        policy.group = self.group
        policy.save()

        url = f"{reverse('v1_management:access')}?application=test_app"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permissions = [item["permission"] for item in response.data.get("data", [])]

        # All permissions should be in the response
        self.assertIn("test_app:resource:action1", permissions)
        self.assertIn("test_app:resource:action2", permissions)

    @override_settings(ROLE_CREATE_ALLOW_LIST="test_app")
    @override_settings(V1_ROLE_PERMISSION_BLOCK_LIST=["test_app:resource:blocked_action"])
    @patch("management.cache.AccessCache.get_policy", return_value=None)
    @patch("management.cache.AccessCache.save_policy", return_value=None)
    def test_get_access_blocked_permissions_not_cached(self, mock_save_policy, mock_get_policy):
        """Test that blocked permissions are filtered before caching."""
        blocked_permission = Permission.objects.create(
            permission="test_app:resource:blocked_action", tenant=self.tenant
        )
        allowed_permission = Permission.objects.create(
            permission="test_app:resource:allowed_action", tenant=self.tenant
        )

        role = Role.objects.create(name="test_role_cache_block", tenant=self.tenant)
        Access.objects.create(role=role, permission=blocked_permission, tenant=self.tenant)
        Access.objects.create(role=role, permission=allowed_permission, tenant=self.tenant)

        policy = Policy.objects.create(name="test_policy_cache_block", tenant=self.tenant)
        policy.roles.add(role)
        policy.group = self.group
        policy.save()

        url = f"{reverse('v1_management:access')}?application=test_app"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify save_policy was called and check what was cached
        self.assertTrue(mock_save_policy.called)
        cached_data = mock_save_policy.call_args[0][2]
        cached_permissions = [item["permission"] for item in cached_data]

        # Blocked permission should NOT be in the cached data
        self.assertNotIn("test_app:resource:blocked_action", cached_permissions)
        # Allowed permission should be in the cached data
        self.assertIn("test_app:resource:allowed_action", cached_permissions)

    @patch("management.cache.AccessCache.get_policy", return_value=None)
    @patch("management.cache.AccessCache.save_policy", return_value=None)
    def test_is_org_admin_included_in_cache_sub_key(self, mock_save_policy, mock_get_policy):
        """Test that is_org_admin is included in the cache sub_key so flag changes trigger cache misses."""
        client = APIClient()
        url = "{}?application=app".format(reverse("v1_management:access"))

        # Request as org admin (self.test_headers uses is_org_admin=True)
        client.get(url, **self.test_headers)
        _, admin_sub_key = mock_get_policy.call_args[0]
        self.assertIn("is_org_admin:True", admin_sub_key)

        mock_get_policy.reset_mock()
        mock_save_policy.reset_mock()

        # Build non-admin headers
        user_data = {"username": "test_user", "email": "test@gmail.com"}
        non_admin_request_context = self._create_request_context(
            {"account_id": "1111111", "tenant_name": "acct1111111", "org_id": "100001"},
            user_data,
            is_org_admin=False,
        )
        non_admin_headers = non_admin_request_context["request"].META

        client.get(url, **non_admin_headers)
        _, non_admin_sub_key = mock_get_policy.call_args[0]
        self.assertIn("is_org_admin:False", non_admin_sub_key)

        # The two sub_keys must differ so the cache returns separate entries
        self.assertNotEqual(admin_sub_key, non_admin_sub_key)

    @override_settings(ROLE_CREATE_ALLOW_LIST="legacy_app,other_app")
    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["legacy_app"])
    @patch("management.access.view.is_v2_edit_enabled_for_request", return_value=True)
    def test_access_v2_tenant_allowed_app_returns_data(self, _mock_v2):
        """V2 orgs can query /access for applications in V2_MIGRATION_APP_EXCLUDE_LIST."""
        perm_legacy = Permission.objects.create(permission="legacy_app:*:*", tenant=self.tenant)
        role = Role.objects.create(name="legacy_role", tenant=self.tenant)
        Access.objects.create(role=role, permission=perm_legacy, tenant=self.tenant)
        policy = Policy.objects.create(name="legacy_policy", tenant=self.tenant)
        policy.roles.add(role)
        policy.group = self.group
        policy.save()

        from management.access.view import v1_access_by_v2_org_total

        client = APIClient()
        url = f"{reverse('v1_management:access')}?application=legacy_app"
        before = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="legacy_app", caller_type="user"
        )._value.get()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permissions = [row["permission"] for row in response.data.get("data", [])]
        self.assertIn("legacy_app:*:*", permissions)
        after = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="legacy_app", caller_type="user"
        )._value.get()
        self.assertEqual(after, before)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["legacy_app"])
    @patch("management.access.view.logger")
    @patch("management.access.view.is_v2_edit_enabled_for_request", return_value=True)
    def test_access_v2_tenant_disallowed_app_rejected(self, _mock_v2, mock_log):
        """V2 orgs are rejected when querying an app not in V2_MIGRATION_APP_EXCLUDE_LIST."""
        from management.access.view import v1_access_by_v2_org_total

        client = APIClient()
        url = f"{reverse('v1_management:access')}?application=other_app"
        before = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="other_app", caller_type="user"
        )._value.get()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Disallowed", response.data.get("detail", ""))
        mock_log.info.assert_called_once()
        log_msg = mock_log.info.call_args[0][0] % mock_log.info.call_args[0][1:]
        self.assertIn("result=rejected", log_msg)
        self.assertIn(str(self.tenant.org_id), log_msg)
        self.assertIn("other_app", log_msg)
        after = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="other_app", caller_type="user"
        )._value.get()
        self.assertEqual(after, before + 1)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["legacy_app"])
    @patch("management.access.view.is_v2_edit_enabled_for_request", return_value=True)
    def test_access_v2_tenant_empty_application_rejected(self, _mock_v2):
        """V2 orgs are rejected when application= is empty."""
        from management.access.view import v1_access_by_v2_org_total

        client = APIClient()
        url = f"{reverse('v1_management:access')}?application="
        before = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="", caller_type="user"
        )._value.get()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must specify", response.data.get("detail", ""))
        after = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="", caller_type="user"
        )._value.get()
        self.assertEqual(after, before + 1)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["legacy_app"])
    @patch("management.access.view.is_v2_edit_enabled_for_request", return_value=True)
    def test_access_v2_tenant_mixed_apps_rejected(self, _mock_v2):
        """V2 orgs are rejected when application param contains any disallowed app."""
        client = APIClient()
        url = f"{reverse('v1_management:access')}?application=legacy_app,other_app"
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("other_app", response.data.get("detail", ""))

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["legacy_app"])
    @patch("management.access.view.logger")
    @patch("management.access.view.is_v2_edit_enabled_for_request", return_value=True)
    def test_access_v2_org_metric_logs_context(self, _mock_v2, mock_logger):
        """Test that a structured log line is emitted when a v2 org is rejected from v1 /access."""
        client = APIClient()
        url = f"{reverse('v1_management:access')}?application=other_app"
        response = client.get(url, **self.headers, HTTP_USER_AGENT="insights-chrome/1.0")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_logger.info.assert_called_once()
        log_msg = mock_logger.info.call_args[0][0] % mock_logger.info.call_args[0][1:]
        self.assertIn("result=rejected", log_msg)
        self.assertIn(self.customer_data["org_id"], log_msg)
        self.assertIn("other_app", log_msg)
        self.assertIn("caller_type=user", log_msg)
        self.assertIn("user_id=", log_msg)
        self.assertIn("request_id=", log_msg)
        self.assertIn("user_agent=insights-chrome/1.0", log_msg)

    @override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["legacy_app"])
    @patch("management.access.view.is_v2_edit_enabled_for_request", return_value=True)
    def test_access_v2_org_metric_service_account_caller_type(self, _mock_v2):
        """Test that the metric labels caller_type as service_account for service account requests."""
        from management.access.view import v1_access_by_v2_org_total

        client = APIClient()
        url = f"{reverse('v1_management:access')}?application=other_app"
        before = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="other_app", caller_type="service_account"
        )._value.get()
        response = client.get(url, **self.headers_service_account)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        after = v1_access_by_v2_org_total.labels(
            org_id=self.customer_data["org_id"], application="other_app", caller_type="service_account"
        )._value.get()
        self.assertEqual(after, before + 1)
