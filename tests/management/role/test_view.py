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
"""Test the role viewset."""

import json
from typing import Optional
from uuid import uuid4
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.test.utils import override_settings
from django.urls import reverse, resolve
from rest_framework import status
from rest_framework.test import APIClient
from api.models import Tenant
from management.cache import TenantCache
from management.models import (
    Group,
    Permission,
    Principal,
    Role,
    Access,
    Policy,
    ResourceDefinition,
    ExtRoleRelation,
    ExtTenant,
    Workspace,
    BindingMapping,
)
from management.relation_replicator.noop_replicator import NoopReplicator
from management.role.v2_model import CustomRoleV2
from management.role_binding.model import RoleBinding
from management.tenant_service.v2 import V2TenantBootstrapService
from migration_tool.in_memory_tuples import (
    all_of,
    InMemoryRelationReplicator,
    InMemoryTuples,
    relation,
    resource,
    subject,
    resource_type,
)

from tests.core.test_kafka import copy_call_args
from tests.identity_request import IdentityRequest
from tests.util import assert_v1_v2_tuples_fully_consistent, assert_v1_v2_locally_consistent
from tests.v2_util import bootstrap_tenant_for_v2_test
from unittest.mock import ANY, patch, call, Mock

URL = reverse("v1_management:role-list")


def normalize_and_sort(json_obj):
    for key, value in json_obj.items():
        if isinstance(value, list):
            sorted_list = sorted([json.dumps(item, sort_keys=True, cls=DjangoJSONEncoder) for item in value])

            json_obj[key] = [json.loads(item) for item in sorted_list]
    return json_obj


def replication_event_for_v1_role(v1_role_uuid, bound_workspace_id, org_id=None, event_type="create_custom_role"):
    """Create a replication event for a v1 role."""
    event = {
        "relations_to_add": relation_api_tuples_for_v1_role(
            v1_role_uuid=v1_role_uuid,
            bound_workspace_id=bound_workspace_id,
        ),
        "relations_to_remove": [],
        "resource_context": {
            "org_id": org_id or "",
            "event_type": event_type,
            "created_at": ANY,
        },
    }
    return event


def relation_api_tuples_for_v1_role(v1_role_uuid, bound_workspace_id):
    """Create a relation API tuple for a v1 role."""
    v1_role = Role.objects.get(uuid=v1_role_uuid)
    mappings = BindingMapping.objects.filter(role=v1_role.id).all()
    relations = []
    seen_v2_role_ids = set()
    for role_binding in [m.get_role_binding() for m in mappings]:
        relation_tuple = relation_api_tuple("role_binding", role_binding.id, "role", "role", role_binding.role.id)
        relations.append(relation_tuple)

        for permission in role_binding.role.permissions:
            relation_tuple = relation_api_tuple("role", role_binding.role.id, permission, "principal", "*")
            relations.append(relation_tuple)
        if "app_all_read" in role_binding.role.permissions:
            relation_tuple = relation_api_tuple(
                "workspace",
                bound_workspace_id,
                "binding",
                "role_binding",
                role_binding.id,
            )
            relations.append(relation_tuple)
        else:
            relation_tuple = relation_api_tuple("keya/id", "valueA", "binding", "role_binding", role_binding.id)
            relations.append(relation_tuple)

        if role_binding.role.id not in seen_v2_role_ids:
            seen_v2_role_ids.add(role_binding.role.id)
            tenant_resource_id = v1_role.tenant.tenant_resource_id()
            if tenant_resource_id:
                relations.append(
                    relation_api_tuple("role", role_binding.role.id, "owner", "tenant", tenant_resource_id)
                )
    return relations


def relation_api_tuple(
    resource_type,
    resource_id,
    relation,
    subject_type,
    subject_id,
    subject_relation=None,
):
    """Helper function for creating a relation tuple in json."""
    return {
        "resource": relation_api_resource(resource_type, resource_id),
        "relation": relation,
        "subject": relation_api_subject(subject_type, subject_id, subject_relation),
    }


def relation_api_subject(subject_type, subject_id, subject_relation=None):
    subject = {"subject": relation_api_resource(subject_type, subject_id)}
    if subject_relation is not None:
        subject["relation"] = subject_relation
    return subject


def relation_api_resource(type_resource, id_resource):
    """Helper function for creating a relation resource in json."""
    return {"type": {"namespace": "rbac", "name": type_resource}, "id": id_resource}


def find_in_list(list, predicate):
    """Find an item in a list."""
    for item in list:
        if predicate(item):
            return item
    return None


class RoleViewsetTests(IdentityRequest):
    """Test the role viewset."""

    def setUp(self):
        """Set up the role viewset tests."""
        super().setUp()
        bootstrap_tenant_for_v2_test(self.tenant)
        sys_role_config = {
            "name": "system_role",
            "display_name": "system_display",
            "system": True,
        }

        sys_pub_role_config = {
            "name": "system_public_role",
            "display_name": "system_public_display",
            "system": True,
        }

        def_role_config = {
            "name": "default_role",
            "display_name": "default_display",
            "platform_default": True,
        }

        admin_def_role_config = {
            "name": "admin_default_role",
            "display_name": "admin_default_display",
            "system": True,
            "admin_default": True,
        }

        platform_admin_def_role_config = {
            "name": "platform_admin_default_role",
            "display_name": "platform_admin_default_display",
            "system": True,
            "platform_default": True,
            "admin_default": True,
        }

        self.display_fields = {
            "applications",
            "description",
            "uuid",
            "name",
            "display_name",
            "system",
            "created",
            "policyCount",
            "accessCount",
            "modified",
            "platform_default",
            "admin_default",
            "external_role_id",
            "external_tenant",
        }

        self.principal = Principal(username=self.user_data["username"], tenant=self.tenant)
        self.principal.save()
        self.policy = Policy.objects.create(name="policyA", tenant=self.tenant)
        self.group = Group(name="groupA", description="groupA description", tenant=self.tenant)
        self.group.save()
        self.group.principals.add(self.principal)
        self.group.policies.add(self.policy)
        self.group.save()

        self.policyTwo = Policy.objects.create(name="policyB", tenant=self.tenant)
        self.groupTwo = Group(name="groupB", description="groupB description", tenant=self.tenant)
        self.groupTwo.save()
        self.groupTwo.principals.add(self.principal)
        self.groupTwo.policies.add(self.policyTwo)
        self.groupTwo.save()

        self.public_tenant = Tenant.objects.get(tenant_name="public")
        self.sysPubRole = Role(**sys_pub_role_config, tenant=self.public_tenant)
        self.sysPubRole.save()

        self.adminRole = Role(**admin_def_role_config, tenant=self.public_tenant)
        self.adminRole.save()

        self.platformAdminRole = Role(**platform_admin_def_role_config, tenant=self.public_tenant)
        self.platformAdminRole.save()

        self.sysRole = Role(**sys_role_config, tenant=self.public_tenant)
        self.sysRole.save()

        self.defRole = Role(**def_role_config, tenant=self.public_tenant)
        self.defRole.save()

        self.ext_tenant = ExtTenant.objects.create(name="foo")
        self.ext_role_relation = ExtRoleRelation.objects.create(role=self.defRole, ext_tenant=self.ext_tenant)

        self.policy.roles.add(
            self.defRole,
            self.sysRole,
            self.adminRole,
            self.platformAdminRole,
            self.sysPubRole,
        )
        self.policy.save()

        self.policyTwo.roles.add(self.platformAdminRole)
        self.policyTwo.save()

        self.permission = Permission.objects.create(permission="app:*:*", tenant=self.tenant)
        self.permission2 = Permission.objects.create(permission="app2:*:*", tenant=self.tenant)
        self.permission3 = Permission.objects.create(permission="app:*:read", tenant=self.tenant)
        self.permission.permissions.add(self.permission3)
        self.access = Access.objects.create(permission=self.permission, role=self.defRole, tenant=self.tenant)
        self.access2 = Access.objects.create(permission=self.permission2, role=self.defRole, tenant=self.tenant)

        self.access3 = Access.objects.create(permission=self.permission2, role=self.sysRole, tenant=self.tenant)
        Permission.objects.create(permission="cost-management:*:*", tenant=self.tenant)
        self.root_workspace, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
            defaults={"name": "root", "description": "Root workspace"},
        )
        self.default_workspace, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            defaults={"name": "default", "description": "Default workspace", "parent": self.root_workspace},
        )
        self.child_workspace = Workspace.objects.create(
            name="child",
            description="Child workspace",
            tenant=self.tenant,
            parent=self.default_workspace,
            type="standard",
        )

    def tearDown(self):
        """Tear down role viewset tests."""
        with self.subTest(msg="V2 consistency"):
            assert_v1_v2_locally_consistent(test=self)

        Group.objects.all().delete()
        Principal.objects.all().delete()
        Role.objects.all().delete()
        Policy.objects.all().delete()
        Permission.objects.all().delete()
        Access.objects.all().delete()
        ExtTenant.objects.all().delete()
        ExtRoleRelation.objects.all().delete()
        Workspace.objects.filter(type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(type=Workspace.Types.UNGROUPED_HOSTS).delete()
        Workspace.objects.filter(type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(parent__isnull=True).delete()
        # we need to delete old test_tenant's that may exist in cache
        test_tenant_org_id = "100001"
        cached_tenants = TenantCache()
        cached_tenants.delete_tenant(test_tenant_org_id)
        # Clear principal cache to avoid stale data between tests
        from management.utils import PRINCIPAL_CACHE

        PRINCIPAL_CACHE.delete_all_principals_for_tenant("100001")
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)

    def create_role(self, role_name, role_display="", in_access_data=None):
        """Create a role."""
        access_data = [
            {
                "permission": "app:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "key1.id",
                            "operation": "equal",
                            "value": "value1",
                        }
                    }
                ],
            },
            {"permission": "app:*:read", "resourceDefinitions": []},
        ]
        if in_access_data is not None:
            access_data = in_access_data
        test_data = {
            "name": role_name,
            "display_name": role_display,
            "access": access_data,
        }

        # create a role
        client = APIClient()
        response = client.post(URL, test_data, format="json", **self.headers)
        return response

    def create_group(self, group_name):
        """Create a group."""
        test_data = {"name": group_name, "description": "a group!"}
        url = reverse("v1_management:group-list")
        client = APIClient()
        response = client.post(url, test_data, format="json", **self.headers)
        return response

    def create_policy(self, policy_name, group, roles):
        """Create a policy."""
        # create a policy
        policy = Policy.objects.create(name=policy_name, tenant=self.tenant, system=True)
        for role in Role.objects.filter(uuid__in=roles):
            policy.roles.add(role)
        policy.group = Group.objects.get(uuid=group)
        policy.save()

    def add_principal_to_group(self, group_uuid, username):
        """Add principal to existing group."""
        url = reverse("v1_management:group-principals", kwargs={"uuid": group_uuid})
        client = APIClient()
        test_data = {"principals": [{"username": username}]}
        response = client.post(url, test_data, format="json", **self.headers)
        return response

    @patch("management.permissions.v2_edit_api_access.FEATURE_FLAGS.is_v2_edit_api_enabled", return_value=True)
    def test_create_role_blocked_when_workspaces_enabled(self, mock_is_v2_edit_enabled):
        """Test that V1 role create returns 403 when workspaces feature flag is enabled for the org."""
        role_name = "BlockedV1Role"
        access_data = [{"permission": "app:*:read", "resourceDefinitions": []}]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("workspaces", str(response.data).lower())
        mock_is_v2_edit_enabled.assert_called_once_with(self.customer_data["org_id"])

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_create_role_success(self, send_kafka_message):
        """Test that we can create a role."""
        with self.settings(NOTIFICATIONS_ENABLED=True):
            role_name = "roleA"
            access_data = [
                {
                    "permission": "app:*:*",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "keyA.id",
                                "operation": "equal",
                                "value": "valueA",
                            }
                        }
                    ],
                },
                {"permission": "app:*:read", "resourceDefinitions": []},
            ]
            response = self.create_role(role_name, in_access_data=access_data)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # test whether newly created role is added correctly within audit log database
            al_url = "/api/rbac/v1/auditlogs/"
            al_client = APIClient()
            al_response = al_client.get(al_url, **self.headers)
            retrieve_data = al_response.data.get("data")
            al_list = retrieve_data
            al_dict = al_list[0]

            al_dict_principal_username = al_dict["principal_username"]
            al_dict_description = al_dict["description"]
            al_dict_resource = al_dict["resource_type"]
            al_dict_action = al_dict["action"]

            self.assertEqual(self.user_data["username"], al_dict_principal_username)
            self.assertIsNotNone(al_dict_description)
            self.assertEqual(al_dict_resource, "role")
            self.assertEqual(al_dict_action, "create")

            # test that we can retrieve the role
            url = reverse("v1_management:role-detail", kwargs={"uuid": response.data.get("uuid")})
            client = APIClient()
            response = client.get(url, **self.headers)
            uuid = response.data.get("uuid")
            role = Role.objects.get(uuid=uuid)

            org_id = self.customer_data["org_id"]

            self.assertIsNotNone(uuid)
            self.assertIsNotNone(response.data.get("name"))
            self.assertEqual(role_name, response.data.get("name"))
            self.assertIsNotNone(response.data.get("display_name"))
            self.assertEqual(role_name, response.data.get("display_name"))
            self.assertIsInstance(response.data.get("access"), list)
            self.assertEqual(access_data, response.data.get("access"))
            self.assertEqual(role.tenant, self.tenant)
            for access in role.access.all():
                self.assertEqual(access.tenant, self.tenant)
                for rd in ResourceDefinition.objects.filter(access=access):
                    self.assertEqual(rd.tenant, self.tenant)
            send_kafka_message.assert_called_with(
                settings.NOTIFICATIONS_TOPIC,
                {
                    "bundle": "console",
                    "application": "rbac",
                    "event_type": "custom-role-created",
                    "timestamp": ANY,
                    "events": [
                        {
                            "metadata": {},
                            "payload": {
                                "name": role.name,
                                "username": self.user_data["username"],
                                "uuid": str(role.uuid),
                            },
                        }
                    ],
                    "org_id": org_id,
                },
                ANY,
            )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_create_role_with_display_success(self, mock_method):
        """Test that we can create a role."""
        role_name = "roleD"
        role_display = "display name for roleD"
        access_data = [
            {
                "permission": "app:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            },
            {"permission": "app:*:read", "resourceDefinitions": []},
        ]
        response = self.create_role(role_name, role_display=role_display, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        replication_event = replication_event_for_v1_role(
            response.data.get("uuid"),
            str(self.default_workspace.id),
            org_id=self.customer_data["org_id"],
            event_type="create_custom_role",
        )

        mock_method.assert_called_once()
        actual_call_arg = mock_method.call_args[0][0]
        expected_sorted = normalize_and_sort(replication_event)
        actual_sorted = normalize_and_sort(actual_call_arg)
        self.assertEqual(expected_sorted, actual_sorted)

        # test that we can retrieve the role
        url = reverse("v1_management:role-detail", kwargs={"uuid": response.data.get("uuid")})
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertIsNotNone(response.data.get("uuid"))
        self.assertIsNotNone(response.data.get("name"))
        self.assertEqual(role_name, response.data.get("name"))
        self.assertIsNotNone(response.data.get("display_name"))
        self.assertEqual(role_display, response.data.get("display_name"))
        self.assertIsInstance(response.data.get("access"), list)
        self.assertEqual(access_data, response.data.get("access"))

    def test_create_role_without_required_permission(self):
        """Test that creating a role with dependent permissions not supplied, fails."""
        role_name = "roleWithDependentPermissions"
        access_data = [
            {
                "permission": self.permission.permission,
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            f"Permission '{self.permission.permission}' requires: '['{self.permission3.permission}']'",
        )

    def test_create_role_invalid(self):
        """Test that creating an invalid role returns an error."""
        test_data = {}
        client = APIClient()
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_role_invalid_permission(self):
        """Test that creating a role with invalid access permission returns an error."""
        test_data = {
            "name": "role1",
            "access": [{"permission": "foo:bar", "resourceDefinitions": []}],
        }
        client = APIClient()
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_role_empty_application_in_permission(self):
        """Test that creating a role with empty application in access permission returns an error."""
        test_data = {
            "name": "role1",
            "access": [{"permission": ":foo:bar", "resourceDefinitions": []}],
        }
        client = APIClient()
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_role_allow_list(self):
        """Test that we can create a role in an allow_listed application via API."""
        role_name = "C-MRole"
        access_data = [
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # test that we can retrieve the role
        url = reverse("v1_management:role-detail", kwargs={"uuid": response.data.get("uuid")})
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertIsNotNone(response.data.get("uuid"))
        self.assertIsNotNone(response.data.get("name"))
        self.assertEqual(role_name, response.data.get("name"))
        self.assertIsInstance(response.data.get("access"), list)
        self.assertEqual(access_data, response.data.get("access"))

    def test_create_role_allow_list_fail(self):
        """Test that we cannot create a role for a non-allow_listed app."""
        role_name = "roleFail"
        access_data = [
            {
                "permission": "someApp:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_role_appfilter_structure_fail(self):
        """Test that we cannot create a role with invalid structure of resource definition."""
        role_name = "operationFail"
        access_data = [
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": {
                    "attributeFilter": {
                        "key": "keyA.id",
                        "operation": "in",
                        "foo": "valueA",
                    }
                },
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["errors"][0]["detail"].code, "not_a_list")

    def test_create_role_appfilter_fields_fail(self):
        """Test that we cannot create a role with an invalid key in the attributeFilter object."""
        role_name = "operationFail"
        access_data = [
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "in",
                            "foo": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_role_appfilter_operation_fail(self):
        """Test that we cannot create a role with an invalid operation."""
        role_name = "operationFail"
        access_data = [
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "boop",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_role_permission_does_not_exist_fail(self):
        """Test that we cannot create a role with a permission that doesn't exist."""
        role_name = "roleFailPermission"
        permission = "cost-management:foo:bar"
        access_data = [
            {
                "permission": permission,
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            f"Permission does not exist: {permission}",
        )

    def test_create_role_fail_with_access_not_list(self):
        """Test that we cannot create a role for a non-allow_listed app."""
        role_name = "AccessNotList"
        access_data = "some data"
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_role_fail_with_invalid_access(self):
        """Test that we cannot create a role for invalid access data."""
        role_name = "AccessInvalid"
        access_data = [{"per": "some data"}]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_read_role_invalid(self):
        """Test that reading an invalid role returns an error."""
        url = reverse("v1_management:role-detail", kwargs={"uuid": uuid4()})
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_read_role_valid(self):
        """Test that reading a valid role returns expected fields/values."""
        url = reverse("v1_management:role-detail", kwargs={"uuid": self.defRole.uuid})
        client = APIClient()
        response = client.get(url, **self.headers)
        response_data = response.data
        expected_fields = self.display_fields
        expected_fields.add("access")
        self.assertEqual(expected_fields, set(response_data.keys()))
        self.assertEqual(response_data.get("uuid"), str(self.defRole.uuid))
        self.assertEqual(response_data.get("name"), self.defRole.name)
        self.assertEqual(response_data.get("display_name"), self.defRole.display_name)
        self.assertEqual(response_data.get("description"), self.defRole.description)
        self.assertCountEqual(response_data.get("applications"), ["app", "app2"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_read_role_access_success(self):
        """Test that reading a valid role returns access."""
        url = reverse("v1_management:role-access", kwargs={"uuid": self.defRole.uuid})
        client = APIClient()
        response = client.get(url, **self.headers)

        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 2)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_read_role_access_invalid_uuid(self):
        """Test that reading a non-existent role uuid returns an error."""
        url = reverse("v1_management:role-access", kwargs={"uuid": "abc-123"})
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_read_role_access_not_found_uuid(self):
        """Test that reading an invalid role uuid returns an error."""
        url = reverse("v1_management:role-access", kwargs={"uuid": uuid4()})
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(V1_ROLE_PERMISSION_BLOCK_LIST=["test_app:resource:blocked_action"])
    def test_read_role_access_filters_blocked_permissions(self):
        """Test that blocked permissions are filtered from role access endpoint."""
        blocked_permission = Permission.objects.create(
            permission="test_app:resource:blocked_action", tenant=self.tenant
        )
        allowed_permission = Permission.objects.create(
            permission="test_app:resource:allowed_action", tenant=self.tenant
        )

        role = Role.objects.create(name="test_role_block_list", tenant=self.tenant)
        Access.objects.create(role=role, permission=blocked_permission, tenant=self.tenant)
        Access.objects.create(role=role, permission=allowed_permission, tenant=self.tenant)

        url = reverse("v1_management:role-access", kwargs={"uuid": role.uuid})
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permissions = [item["permission"] for item in response.data.get("data", [])]

        self.assertNotIn("test_app:resource:blocked_action", permissions)
        self.assertIn("test_app:resource:allowed_action", permissions)

    def test_read_role_list_success(self):
        """Test that we can read a list of roles."""
        role_name = "roleA"
        role_display = "Display name for roleA"
        response = self.create_role(role_name, role_display=role_display)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # list a role
        client = APIClient()
        response = client.get(URL, **self.headers)

        # three parts in response: meta, links and data
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)
        self.assertEqual(len(response.data.get("data")), 6)

        role = None

        for iterRole in response.data.get("data"):
            self.assertIsNotNone(iterRole.get("name"))
            # fields displayed are same as defined
            self.assertEqual(self.display_fields, set(iterRole.keys()))
            if iterRole.get("name") == role_name:
                self.assertEqual(iterRole.get("accessCount"), 2)
                role = iterRole
        self.assertEqual(role.get("name"), role_name)
        self.assertEqual(role.get("display_name"), role_display)

    def test_get_role_by_application_single(self):
        """Test that getting roles by application returns roles based on permissions."""
        url = "{}?application={}".format(URL, "app")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        self.assertEqual(response.data.get("data")[0].get("name"), self.defRole.name)

    def test_get_role_by_application_using_ext_tenant(self):
        """Test that getting roles by application returns roles based on external tenant name."""
        url = "{}?application={}".format(URL, "foo")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        self.assertEqual(response.data.get("data")[0].get("name"), self.defRole.name)

    def test_get_role_by_application_multiple(self):
        """Test that getting roles by multiple applications returns roles based on permissions."""
        url = "{}?application={}".format(URL, "app2")
        client = APIClient()
        response = client.get(url, **self.headers)
        role_names = [role.get("name") for role in response.data.get("data")]
        self.assertEqual(response.data.get("meta").get("count"), 2)
        self.assertCountEqual(role_names, [self.defRole.name, self.sysRole.name])

    def test_get_role_by_application_duplicate_role(self):
        """Test that getting roles by application with permissions in the same role only returns the roles once."""
        url = "{}?application={}".format(URL, "app,app2")
        client = APIClient()
        response = client.get(url, **self.headers)
        role_names = [role.get("name") for role in response.data.get("data")]
        self.assertEqual(response.data.get("meta").get("count"), 2)
        self.assertCountEqual(role_names, [self.defRole.name, self.sysRole.name])

    def test_get_role_by_application_does_not_exist(self):
        """Test that getting roles by application returns nothing when there is no match."""
        url = "{}?application={}".format(URL, "foobar")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 0)

    def test_get_role_by_permission_single(self):
        """Test that getting roles by permission returns roles based on permissions."""
        url = "{}?permission={}".format(URL, "app:*:*")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        self.assertEqual(response.data.get("data")[0].get("name"), self.defRole.name)

    def test_get_role_by_duplicate_permission(self):
        """Test that getting roles by duplicate permissions in the same role only returns the roles once."""
        url = "{}?permission={}".format(URL, "app2:*:*")
        client = APIClient()
        response = client.get(url, **self.headers)
        role_names = [role.get("name") for role in response.data.get("data")]
        self.assertEqual(response.data.get("meta").get("count"), 2)
        self.assertCountEqual(role_names, [self.defRole.name, self.sysRole.name])

    def test_get_role_by_permission_multiple(self):
        """Test that getting roles by permissions ."""
        url = "{}?permission={}".format(URL, "app:*:*,app2:*:*")
        client = APIClient()
        response = client.get(url, **self.headers)
        role_names = [role.get("name") for role in response.data.get("data")]
        self.assertEqual(response.data.get("meta").get("count"), 2)
        self.assertCountEqual(role_names, [self.defRole.name, self.sysRole.name])

    def test_get_role_by_permission_does_not_exist(self):
        """Test that getting roles by permission returns nothing when there is no match."""
        url = "{}?permission={}".format(URL, "foo:foo:foo")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 0)

    def test_get_role_by_partial_name_by_default(self):
        """Test that getting roles by name returns partial match by default."""
        url = "{}?name={}".format(URL, "role")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 5)

    def test_get_role_by_partial_name_explicit(self):
        """Test that getting roles by name returns partial match when specified."""
        url = "{}?name={}&name_match={}".format(URL, "role", "partial")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 5)

    def test_get_role_by_name_invalid_criteria(self):
        """Test that getting roles by name fails with invalid name_match."""
        url = "{}?name={}&name_match={}".format(URL, "role", "bad_criteria")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_role_by_exact_name_match(self):
        """Test that getting roles by name returns exact match."""
        url = "{}?name={}&name_match={}".format(URL, self.sysRole.name, "exact")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        role = response.data.get("data")[0]
        self.assertEqual(role.get("name"), self.sysRole.name)

    def test_get_role_by_exact_name_no_match(self):
        """Test that getting roles by name returns no results with exact match."""
        url = "{}?name={}&name_match={}".format(URL, "role", "exact")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 0)

    def test_get_role_by_partial_display_name_by_default(self):
        """Test that getting roles by display_name returns partial match by default."""
        url = "{}?display_name={}".format(URL, "display")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 5)

    def test_get_role_by_partial_display_name_explicit(self):
        """Test that getting roles by display_name returns partial match when specified."""
        url = "{}?display_name={}&name_match={}".format(URL, "display", "partial")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 5)

    def test_get_role_by_display_name_invalid_criteria(self):
        """Test that getting roles by display_name fails with invalid name_match."""
        url = "{}?display_name={}&name_match={}".format(URL, "display", "bad_criteria")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_role_by_exact_display_name_match(self):
        """Test that getting roles by display_name returns exact match."""
        url = "{}?display_name={}&name_match={}".format(URL, self.sysRole.display_name, "exact")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        role = response.data.get("data")[0]
        self.assertEqual(role.get("display_name"), self.sysRole.display_name)

    def test_get_role_by_exact_display_name_no_match(self):
        """Test that getting roles by display_name returns no results with exact match."""
        url = "{}?display_name={}&name_match={}".format(URL, "display", "exact")
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.data.get("meta").get("count"), 0)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_list_role_with_groups_in_fields_with_username_param_for_non_org_admin(self, mock_request):
        """
        Test that we can read a list of roles and the groups_in fields is set correctly
        for a request with 'username' param for non org admin principal.
        """
        # Set existing groups as system groups
        default_access_group_name = "Default access"
        self.group.name = default_access_group_name
        self.group.system = self.group.platform_default = True
        self.group.save()

        default_admin_access_group_name = "Default admin access"
        self.groupTwo.name = default_admin_access_group_name
        self.groupTwo.system = self.groupTwo.admin_default = True
        self.groupTwo.save()

        # create a custom role
        custom_role_name = "NewRoleForJohn"
        custom_role = self.create_role(custom_role_name)
        self.assertEqual(custom_role.status_code, status.HTTP_201_CREATED)
        custom_role_uuid = custom_role.data.get("uuid")

        # create a custom group
        custom_group_name = "NewGroupForJohn"
        custom_group = self.create_group(custom_group_name)
        self.assertEqual(custom_group.status_code, status.HTTP_201_CREATED)
        custom_group_uuid = custom_group.data.get("uuid")

        # create a policy to link the role and group
        custom_policy_name = "NewPolicyForJohn"
        self.create_policy(custom_policy_name, custom_group_uuid, [custom_role_uuid])

        # create a principal
        john = Principal(username="john", tenant=self.tenant)

        # Mock return value for request_filtered_principals() -> user is NOT org admin
        mock_request.return_value = {
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "user_id": 52567473,
                    "username": john.username,
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        }

        # add principal to the created group
        principal_response = self.add_principal_to_group(custom_group_uuid, john.username)
        self.assertEqual(principal_response.status_code, status.HTTP_200_OK, principal_response)

        # add groups_in and groups_in_count fields into display fields
        groups_in_count = "groups_in_count"
        groups_in = "groups_in"
        new_display_fields = self.display_fields
        new_display_fields.add(groups_in_count)
        new_display_fields.add(groups_in)

        url = f"{URL}?add_fields={groups_in_count},{groups_in}&username={john.username}"
        client = APIClient()
        response = client.get(url, **self.headers)

        # three parts in response: meta, links and data
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)

        response_data = response.data.get("data")

        for iterRole in response_data:
            # fields displayed are same as defined incl. groups_in and groups_in_count
            self.assertEqual(new_display_fields, set(iterRole.keys()))
            self.assertIsNotNone(iterRole.get(groups_in)[0]["name"])
            self.assertIsNotNone(iterRole.get(groups_in)[0]["uuid"])
            self.assertIsNotNone(iterRole.get(groups_in)[0]["description"])

        # make sure created role exists in result set and has correct values
        created_role = next(
            (iterRole for iterRole in response_data if iterRole["name"] == custom_role_name),
            None,
        )
        self.assertIsNotNone(created_role)
        self.assertEqual(created_role[groups_in_count], 1)
        self.assertEqual(created_role[groups_in][0]["name"], custom_group_name)

        # make sure all roles are from:
        #       * custom group 'NewGroupForJohn' or
        #       * 'Default access' group
        groups = [default_access_group_name, custom_group_name]
        for role in response_data:
            for group in role[groups_in]:
                self.assertIn(group["name"], groups)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_list_role_with_groups_in_fields_with_username_param_for_org_admin(self, mock_request):
        """
        Test that we can read a list of roles and the groups_in fields is set correctly
        for a request with 'username' param for org admin principal.
        """
        # Set existing groups as system groups
        default_access_group_name = "Default access"
        self.group.name = default_access_group_name
        self.group.system = self.group.platform_default = True
        self.group.save()

        default_admin_access_group_name = "Default admin access"
        self.groupTwo.name = default_admin_access_group_name
        self.groupTwo.system = self.groupTwo.admin_default = True
        self.groupTwo.save()

        # create a custom role
        custom_role_name = "NewRoleForMary"
        custom_role = self.create_role(custom_role_name)
        self.assertEqual(custom_role.status_code, status.HTTP_201_CREATED)
        custom_role_uuid = custom_role.data.get("uuid")

        # create a custom group
        custom_group_name = "NewGroupForMary"
        custom_group = self.create_group(custom_group_name)
        self.assertEqual(custom_group.status_code, status.HTTP_201_CREATED)
        custom_group_uuid = custom_group.data.get("uuid")

        # create a policy to link the role and group
        custom_policy_name = "NewPolicyForMary"
        self.create_policy(custom_policy_name, custom_group_uuid, [custom_role_uuid])

        # create a principal
        mary = Principal(username="mary", tenant=self.tenant)

        # Mock return value for request_filtered_principals() -> user is NOT org admin
        mock_request.return_value = {
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "user_id": 52567473,
                    "username": mary.username,
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        }

        # add principal to the created group
        principal_response = self.add_principal_to_group(custom_group_uuid, mary.username)
        self.assertEqual(principal_response.status_code, status.HTTP_200_OK, principal_response)

        # add groups_in and groups_in_count fields into display fields
        groups_in_count = "groups_in_count"
        groups_in = "groups_in"
        new_display_fields = self.display_fields
        new_display_fields.add(groups_in_count)
        new_display_fields.add(groups_in)

        url = f"{URL}?add_fields={groups_in_count},{groups_in}&username={mary.username}"
        client = APIClient()
        response = client.get(url, **self.headers)

        # three parts in response: meta, links and data
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)

        response_data = response.data.get("data")

        for iterRole in response_data:
            # fields displayed are same as defined incl. groups_in and groups_in_count
            self.assertEqual(new_display_fields, set(iterRole.keys()))
            self.assertIsNotNone(iterRole.get(groups_in)[0]["name"])
            self.assertIsNotNone(iterRole.get(groups_in)[0]["uuid"])
            self.assertIsNotNone(iterRole.get(groups_in)[0]["description"])

        # make sure created role exists in result set and has correct values
        created_role = next(
            (iterRole for iterRole in response_data if iterRole["name"] == custom_role_name),
            None,
        )
        self.assertIsNotNone(created_role)
        self.assertEqual(created_role[groups_in_count], 1)
        self.assertEqual(created_role[groups_in][0]["name"], custom_group_name)

        # make sure all roles are from:
        #       * custom group 'NewGroupForJohn' or
        #       * 'Default access' group
        #       * 'Default admin access' group
        groups = [
            default_access_group_name,
            default_admin_access_group_name,
            custom_group_name,
        ]
        for role in response_data:
            for group in role[groups_in]:
                self.assertIn(group["name"], groups)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_list_role_with_groups_in_fields_for_principal_scope_success(self, mock_request):
        """
        Test that we can read a list of roles and the groups_in fields is set correctly
        for a principal scoped request.
        """
        mock_request.return_value = {
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "user_id": 52567473,
                    "username": self.principal.username,
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        }

        # create a role
        role_name = "groupsInRole"
        created_role = self.create_role("groupsInRole")
        self.assertEqual(created_role.status_code, status.HTTP_201_CREATED)
        role_uuid = created_role.data.get("uuid")

        # create a group
        group_name = "groupsInGroup"
        created_group = self.create_group(group_name)
        self.assertEqual(created_group.status_code, status.HTTP_201_CREATED)
        group_uuid = created_group.data.get("uuid")

        # create a policy to link the 2
        policy_name = "groupsInPolicy"
        self.create_policy(policy_name, group_uuid, [role_uuid])

        # add user principal to the created group
        principal_response = self.add_principal_to_group(group_uuid, self.principal.username)
        self.assertEqual(principal_response.status_code, status.HTTP_200_OK, principal_response)

        # hit /roles?groups_in, group should appear in groups_in
        field_1 = "groups_in_count"
        field_2 = "groups_in"
        new_display_fields = self.display_fields
        new_display_fields.add(field_1)
        new_display_fields.add(field_2)

        url = "{}?add_fields={},{}&username={}".format(URL, field_1, field_2, self.principal.username)
        client = APIClient()
        response = client.get(url, **self.headers)

        # three parts in response: meta, links and data
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)

        response_data = response.data.get("data")

        for iterRole in response_data:
            # fields displayed are same as defined, groupsInCount is added
            self.assertEqual(new_display_fields, set(iterRole.keys()))
            self.assertIsNotNone(iterRole.get("groups_in")[0]["name"])
            self.assertIsNotNone(iterRole.get("groups_in")[0]["uuid"])
            self.assertIsNotNone(iterRole.get("groups_in")[0]["description"])

        # make sure created role exists in result set and has correct values
        created_role = next(
            (iterRole for iterRole in response_data if iterRole["name"] == role_name),
            None,
        )
        self.assertIsNotNone(created_role)
        self.assertEqual(created_role["groups_in_count"], 1)
        self.assertEqual(created_role["groups_in"][0]["name"], group_name)

        # make sure a default role exists in result set and has correct values
        default_role = next(
            (iterRole for iterRole in response_data if iterRole["name"] == self.defRole.name),
            None,
        )
        self.assertIsNotNone(default_role)
        self.assertEqual(default_role["groups_in_count"], 1)
        self.assertEqual(default_role["groups_in"][0]["name"], self.group.name)

    def test_list_role_with_groups_in_fields_for_admin_scope_success(self):
        """
        Test that we can read a list of roles and the groups_in fields is set correctly
        for an admin scoped request.
        """
        field_1 = "groups_in_count"
        field_2 = "groups_in"
        new_display_fields = self.display_fields
        new_display_fields.add(field_1)
        new_display_fields.add(field_2)

        url = "{}?add_fields={},{}".format(URL, field_1, field_2)
        client = APIClient()
        response = client.get(url, **self.headers)

        # three parts in response: meta, links and data
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)
        self.assertIsInstance(response.data.get("data"), list)

        response_data = response.data.get("data")
        for iterRole in response_data:
            # fields displayed are same as defined, groupsInCount is added
            self.assertEqual(new_display_fields, set(iterRole.keys()))
            self.assertIsNotNone(iterRole.get("groups_in")[0]["name"])
            self.assertIsNotNone(iterRole.get("groups_in")[0]["uuid"])
            self.assertIsNotNone(iterRole.get("groups_in")[0]["description"])

        # make sure a default role exists in result set and has correct values
        default_role = next(
            (iterRole for iterRole in response_data if iterRole["name"] == self.defRole.name),
            None,
        )
        self.assertIsNotNone(default_role)
        self.assertEqual(default_role["groups_in_count"], 1)
        self.assertEqual(default_role["groups_in"][0]["name"], self.group.name)

        # make sure an admin role exists in result set and has correct values
        admin_role = next(
            (iterRole for iterRole in response_data if iterRole["name"] == self.adminRole.name),
            None,
        )
        self.assertIsNotNone(admin_role)
        self.assertEqual(admin_role["groups_in_count"], 1)
        self.assertEqual(admin_role["groups_in"][0]["name"], self.group.name)

    def test_list_role_with_username_forbidden_to_nonadmin(self):
        """Test that non admin can not read a list of roles for username."""
        # Setup non admin request
        non_admin_request_context = self._create_request_context(
            self.customer_data, self.user_data, is_org_admin=False
        )
        non_admin_request = non_admin_request_context["request"]

        url = "{}?username={}".format(URL, self.user_data["username"])
        client = APIClient()
        response = client.get(url, **non_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    def test_list_role_fail_with_invalid_username(self, mock_request):
        """Test that non admin can not read a list of roles for username."""
        url = "{}?username={}".format(URL, "foo")
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_list_role_with_additional_fields_username_success(self, mock_request):
        """Test that we can read a list of roles and add fields for username."""
        field_1 = "groups_in_count"
        field_2 = "groups_in"
        new_display_fields = self.display_fields
        new_display_fields.add(field_1)
        new_display_fields.add(field_2)

        mock_request.return_value = {
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "user_id": 52567473,
                    "username": self.principal.username,
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        }

        url = "{}?add_fields={},{}&username={}".format(URL, field_1, field_2, self.principal.username)
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(len(response.data.get("data")), 5)

        role = response.data.get("data")[0]
        self.assertEqual(new_display_fields, set(role.keys()))
        self.assertEqual(role["groups_in_count"], 1)

    def test_list_role_with_additional_fields_principal_success(self):
        """Test that we can read a list of roles and add fields for principal."""
        field_1 = "groups_in_count"
        field_2 = "groups_in"
        new_display_fields = self.display_fields
        new_display_fields.add(field_1)
        new_display_fields.add(field_2)

        url = "{}?add_fields={},{}&scope=principal".format(URL, field_1, field_2)
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(len(response.data.get("data")), 5)

        role = response.data.get("data")[0]
        self.assertEqual(new_display_fields, set(role.keys()))
        self.assertEqual(role["groups_in_count"], 1)

    def test_list_role_with_additional_fields_access(self):
        """Test that we can read a list of roles and add field access."""
        field = "access"
        expected_fields = self.display_fields
        expected_fields.add(field)

        # list a role
        url = f"{URL}?add_fields={field}"
        client = APIClient()
        response = client.get(url, **self.headers)

        # three parts in response: meta, links and data
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, response.data)

        self.assertIsInstance(response.data.get("data"), list)

        for iterRole in response.data.get("data"):
            # fields displayed are same as defined, field "access" is added
            self.assertEqual(expected_fields, set(iterRole.keys()))
            # if the role contains permissions, then check structure of access field
            if iterRole.get("accessCount") > 0:
                self.assertIsNotNone(iterRole.get("access"))
                for item in iterRole.get("access"):
                    for key in ["resourceDefinitions", "permission"]:
                        self.assertIn(key, item)

    def test_list_role_with_invalid_additional_fields(self):
        """Test that invalid additional fields will raise exception."""
        add_field = "invalid_field"

        # list a role
        url = "{}?add_fields={}".format(URL, add_field)
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_role_with_invalid_sort_order(self):
        """Test that an invalid sort order is ignored."""
        url = "{}?sort_field=zombie".format(URL)
        client = APIClient()
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_patch_role_success(self):
        """Test that we can patch an existing role."""
        role_name = "role"
        response = self.create_role(role_name)
        updated_name = role_name + "_update"
        updated_description = role_name + "This is a test"
        role_uuid = response.data.get("uuid")
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        response = client.patch(
            url,
            {
                "name": updated_name,
                "display_name": updated_name,
                "description": updated_description,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIsNotNone(response.data.get("uuid"))
        self.assertEqual(updated_name, response.data.get("name"))
        self.assertEqual(updated_name, response.data.get("display_name"))
        self.assertEqual(updated_description, response.data.get("description"))

        # test whether newly edited (PATCH) role is added correctly within audit log database
        al_url = "/api/rbac/v1/auditlogs/"
        al_client = APIClient()
        al_response = al_client.get(al_url, **self.headers)
        retrieve_data = al_response.data.get("data")
        al_list = retrieve_data
        # Audit logs are now ordered by -created (newest first), so edit action is at index 0
        al_dict = al_list[0]

        al_dict_principal_username = al_dict["principal_username"]
        al_dict_description = al_dict["description"]
        al_dict_resource = al_dict["resource_type"]
        al_dict_action = al_dict["action"]

        self.assertEqual(self.user_data["username"], al_dict_principal_username)
        self.assertIsNotNone(al_dict_description)
        self.assertEqual(al_dict_resource, "role")
        self.assertEqual(al_dict_action, "edit")

    def test_patch_role_failure(self):
        """Test that we return a 400 with invalid fields in the patch."""
        role_name = "role"
        response = self.create_role(role_name)
        updated_name = role_name + "_update"
        updated_description = role_name + "This is a test"
        role_uuid = response.data.get("uuid")
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        response = client.patch(
            url,
            {
                "name": updated_name,
                "display_name": updated_name,
                "description": updated_description,
                "foo": "bar",
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_role_without_payload(self):
        """Test that we no-op when no payload supplied."""
        role_name = "role"
        response = self.create_role(role_name)
        updated_name = role_name + "_update"
        updated_description = role_name + "This is a test"
        role_uuid = response.data.get("uuid")
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        response = client.patch(
            url,
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_update_role_success(self, send_kafka_message):
        """Test that we can update an existing role."""
        kafka_mock = copy_call_args(send_kafka_message)
        with self.settings(NOTIFICATIONS_ENABLED=True):
            role_name = "roleA"
            response = self.create_role(role_name)
            updated_name = role_name + "_update"
            role_uuid = response.data.get("uuid")
            test_data = response.data
            test_data["name"] = updated_name
            test_data["access"][0]["permission"] = "cost-management:*:*"
            del test_data["uuid"]
            url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
            client = APIClient()
            response = client.put(url, test_data, format="json", **self.headers)

            org_id = self.customer_data["org_id"]

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            self.assertIsNotNone(response.data.get("uuid"))
            self.assertEqual(updated_name, response.data.get("name"))
            self.assertEqual("cost-management:*:*", response.data.get("access")[0]["permission"])

            # test whether newly updated (post) role is added correctly within audit log database
            al_url = "/api/rbac/v1/auditlogs/"
            al_client = APIClient()
            al_response = al_client.get(al_url, **self.headers)
            retrieve_data = al_response.data.get("data")
            al_list = retrieve_data
            # Audit logs are now ordered by -created (newest first), so edit action is at index 0
            al_dict = al_list[0]

            al_dict_principal_username = al_dict["principal_username"]
            al_dict_description = al_dict["description"]
            al_dict_resource = al_dict["resource_type"]
            al_dict_action = al_dict["action"]

            self.assertEqual(self.user_data["username"], al_dict_principal_username)
            self.assertIsNotNone(al_dict_description)
            self.assertEqual(al_dict_resource, "role")
            self.assertEqual(al_dict_action, "edit")

            kafka_mock.assert_called_with(
                settings.NOTIFICATIONS_TOPIC,
                {
                    "bundle": "console",
                    "application": "rbac",
                    "event_type": "custom-role-updated",
                    "timestamp": ANY,
                    "events": [
                        {
                            "metadata": {},
                            "payload": {
                                "name": updated_name,
                                "username": self.user_data["username"],
                                "uuid": response.data.get("uuid"),
                            },
                        }
                    ],
                    "org_id": org_id,
                },
                ANY,
            )

    def test_update_role_invalid(self):
        """Test that updating an invalid role returns an error."""
        url = reverse("v1_management:role-detail", kwargs={"uuid": uuid4()})
        client = APIClient()
        response = client.put(
            url,
            {
                "name": "updated_name",
                "display_name": "updated_name",
                "description": "updated_description",
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_role_invalid_permission(self):
        """Test that updating a role with an invalid permission returns an error."""
        # Set up
        role_name = "permRole"
        access_data = [
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        test_data = response.data
        test_data.get("access")[0]["permission"] = "foo:*:read"
        test_data["applications"] = ["foo"]

        # Test update failure
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        response = client.put(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_update_role(self, mock_method):
        """Test that updating a role with an invalid permission returns an error."""
        # Set up
        role_name = "test_update_role"
        access_data = [
            {
                "permission": "app:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            },
            {"permission": "app:*:read", "resourceDefinitions": []},
        ]

        new_access_data = [
            {
                "permission": "app:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            },
            {"permission": "app:*:read", "resourceDefinitions": []},
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        test_data = response.data
        test_data["access"] = new_access_data
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        current_relations = relation_api_tuples_for_v1_role(role_uuid, str(self.default_workspace.id))

        response = client.put(url, test_data, format="json", **self.headers)
        replication_event = replication_event_for_v1_role(
            response.data.get("uuid"),
            str(self.default_workspace.id),
            org_id=self.customer_data["org_id"],
            event_type="update_custom_role",
        )
        replication_event["relations_to_remove"] = current_relations
        actual_call_arg = mock_method.call_args[0][0]
        expected_sorted = normalize_and_sort(replication_event)
        actual_sorted = normalize_and_sort(actual_call_arg)
        self.assertEqual(expected_sorted, actual_sorted)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_settings(
        ROLE_CREATE_ALLOW_LIST="compliance,inventory",
        REPLICATION_TO_RELATION_ENABLED=True,
        ROOT_SCOPE_PERMISSIONS="inventory:*:*",
        TENANT_SCOPE_PERMISSIONS="",
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_update_role_scoped(self, replicate):
        """Test that updating a role properly updates its scope."""
        tuples = InMemoryTuples()
        replicate.side_effect = InMemoryRelationReplicator(tuples).replicate

        # Set up
        Permission.objects.create(permission="compliance:policy:read", tenant=self.public_tenant)
        Permission.objects.create(permission="inventory:groups:read", tenant=self.public_tenant)

        role_name = "test_update_role"
        access_data = [{"permission": "compliance:policy:read", "resourceDefinitions": []}]
        new_access_data = [{"permission": "inventory:groups:read", "resourceDefinitions": []}]

        default_workspace: Workspace = Workspace.objects.default(tenant=self.tenant)
        root_workspace: Workspace = Workspace.objects.root(tenant=self.tenant)

        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        v1_uuid = response.data["uuid"]

        default_binding = BindingMapping.objects.filter(role__uuid=v1_uuid).get()

        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(default_workspace.id)),
                    relation("binding"),
                    subject("rbac", "role_binding", default_binding.mappings["id"]),
                )
            ),
        )

        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "role_binding", default_binding.mappings["id"]),
                    relation("role"),
                    subject("rbac", "role", str(default_binding.mappings["role"]["id"])),
                )
            ),
        )

        # Update the role with new access data
        test_data = dict(response.data)
        test_data["access"] = new_access_data
        url = reverse("v1_management:role-detail", kwargs={"uuid": v1_uuid})
        client = APIClient()

        response = client.put(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        root_binding = BindingMapping.objects.filter(role__uuid=v1_uuid).get()

        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(root_workspace.id)),
                    relation("binding"),
                    subject("rbac", "role_binding", root_binding.mappings["id"]),
                )
            ),
        )

        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "role_binding", root_binding.mappings["id"]),
                    relation("role"),
                    subject("rbac", "role", str(root_binding.mappings["role"]["id"])),
                )
            ),
        )

    @override_settings(
        ROLE_CREATE_ALLOW_LIST="inventory", REPLICATION_TO_RELATION_ENABLED=True, REMOVE_NULL_VALUE=True
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_update_role_with_null_value(self, replicate):
        """Test that updating a role with a null value success."""
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate
        # Set up
        Permission.objects.create(permission="inventory:groups:*", tenant=self.public_tenant)
        Permission.objects.create(permission="inventory:groups:read", tenant=self.public_tenant)
        standard_ws = Workspace.objects.create(
            name="Test Workspace", tenant=self.tenant, parent=self.default_workspace
        )
        role_name = "test_update_role"
        access_data = [
            {
                "permission": "inventory:groups:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "group.id",
                            "operation": "in",
                            "value": [None],
                        }
                    }
                ],
            }
        ]

        new_access_data = [
            {
                "permission": "inventory:groups:read",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "group.id",
                            "operation": "in",
                            "value": [str(standard_ws.id), None],
                        }
                    }
                ],
            },
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        first_binding = BindingMapping.objects.filter(role=Role.objects.get(uuid=response.data["uuid"])).first()

        # Update the role with new access data
        role_uuid = response.data.get("uuid")
        test_data = response.data
        test_data["access"] = new_access_data
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()

        response = client.put(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        second_binding = (
            BindingMapping.objects.filter(role=Role.objects.get(uuid=role_uuid)).exclude(id=first_binding.id).first()
        )

        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(standard_ws.id)),
                    relation("binding"),
                    subject("rbac", "role_binding", second_binding.mappings["id"]),
                )
            ),
        )
        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "role_binding", second_binding.mappings["id"]),
                    relation("role"),
                    subject("rbac", "role", second_binding.mappings["role"]["id"]),
                )
            ),
        )
        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "role", second_binding.mappings["role"]["id"]),
                    relation("inventory_groups_read"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )

    def test_update_role_invalid_resource_defs_structure(self):
        """Test that updating a role with an invalid resource definitions returns an error."""
        # Set up
        role_name = "permRole"
        access_data = [
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        test_data = response.data
        test_data.get("access")[0]["resourceDefinitions"] = {
            "attributeFilter": {
                "key": "keyA.id",
                "operation": "equal",
                "value": "valueA",
            }
        }

        # Test update failure
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        response = client.put(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["errors"][0]["detail"].code, "not_a_list")

    def test_update_role_appfilter_operation_fail(self):
        # Set up
        role_name = "permRole"
        access_data = [
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        test_data = response.data
        test_data.get("access")[0]["resourceDefinitions"][0].get("attributeFilter")["operation"] = "foo"

        # Test update failure
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        response = client.put(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.data["errors"][0]["detail"]),
            "attributeFilter operation must be one of ['in', 'equal']",
        )

    def test_update_role_permission_does_not_exist_fail(self):
        """Test that we cannot update a role with a permission that doesn't exist."""
        # Set up
        role_name = "permRole"
        permission = "cost-management:foo:bar"
        access_data = [
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")
        test_data = response.data
        test_data.get("access")[0]["permission"] = permission
        test_data["applications"] = ["foo"]

        # Test update failure
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        response = client.put(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            f"Permission does not exist: {permission}",
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_delete_role(self, mock_method):
        """Test that we can delete an existing role."""
        role_name = "roleA"
        access_data = [
            {
                "permission": "app:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": "valueA",
                        }
                    }
                ],
            },
            {"permission": "app:*:read", "resourceDefinitions": []},
        ]
        response = self.create_role(role_name, in_access_data=access_data)

        role_uuid = response.data.get("uuid")
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        replication_event = {
            "relations_to_add": [],
            "relations_to_remove": [],
            "resource_context": {
                "org_id": str(self.tenant.org_id),
                "event_type": "delete_custom_role",
                "created_at": ANY,
            },
        }
        current_relations = relation_api_tuples_for_v1_role(role_uuid, str(self.default_workspace.id))
        replication_event["relations_to_remove"] = current_relations
        response = client.delete(url, **self.headers)
        actual_call_arg = mock_method.call_args[0][0]
        expected_sorted = normalize_and_sort(replication_event)
        actual_sorted = normalize_and_sort(actual_call_arg)
        self.assertEqual(expected_sorted, actual_sorted)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_delete_role_success(self, send_kafka_message):
        """Test that we can delete an existing role."""
        with self.settings(NOTIFICATIONS_ENABLED=True):
            role_name = "roleA"
            response = self.create_role(role_name)

            role_uuid = response.data.get("uuid")
            url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
            client = APIClient()
            response = client.delete(url, **self.headers)

            org_id = self.customer_data["org_id"]

            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
            # test whether correctly added to audit logs
            al_url = "/api/rbac/v1/auditlogs/"
            al_client = APIClient()
            al_response = al_client.get(al_url, **self.headers)
            retrieve_data = al_response.data.get("data")
            al_list = retrieve_data
            for al_record in al_list:
                if al_record["action"] == "delete":
                    al_dict = al_record
                    break

            al_dict_principal_username = al_dict["principal_username"]
            al_dict_description = al_dict["description"]
            al_dict_resource = al_dict["resource_type"]
            al_dict_action = al_dict["action"]

            self.assertEqual(self.user_data["username"], al_dict_principal_username)
            self.assertIsNotNone(al_dict_description)
            self.assertEqual(al_dict_resource, "role")
            self.assertEqual(al_dict_action, "delete")

            send_kafka_message.assert_called_with(
                settings.NOTIFICATIONS_TOPIC,
                {
                    "bundle": "console",
                    "application": "rbac",
                    "event_type": "custom-role-deleted",
                    "timestamp": ANY,
                    "events": [
                        {
                            "metadata": {},
                            "payload": {
                                "name": role_name,
                                "username": self.user_data["username"],
                                "uuid": role_uuid,
                            },
                        }
                    ],
                    "org_id": org_id,
                },
                ANY,
            )

            # verify the role no longer exists
            response = client.get(url, **self.headers)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_system_role(self):
        """Test that system roles are protected from deletion"""
        url = reverse("v1_management:role-detail", kwargs={"uuid": self.sysRole.uuid})
        client = APIClient()
        response = client.delete(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # verify the role still exists
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_delete_system_role_in_public_tenant(self):
        """Test that system roles in public tenant are protected from deletion"""
        url = reverse("v1_management:role-detail", kwargs={"uuid": self.sysPubRole.uuid})
        client = APIClient()

        existing_role = Role.objects.filter(uuid=self.sysPubRole.uuid, tenant=self.public_tenant).first()
        self.assertIsNotNone(existing_role, "Public system role should exist before deletion test.")

        response = client.delete(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # verify the role still exists
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("management.role.relation_api_dual_write_handler.OutboxReplicator.replicate")
    def test_delete_custom_role_without_bindingmappins(self, replicate_mock):
        role_name = "role_without_bindingmapping"
        access_data = []
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        replicate_mock.reset_mock()
        role_uuid = response.data.get("uuid")
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        response = client.delete(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        replicate_mock.assert_not_called()

    def test_update_admin_default_role(self):
        """Test that admin default roles are protected from deletion"""

        url = reverse("v1_management:role-detail", kwargs={"uuid": self.adminRole.uuid})
        client = APIClient()
        access_data = [
            {
                "admin_default": True,
                "permission": "app:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "key1.id",
                            "operation": "equal",
                            "value": "value1",
                        }
                    }
                ],
            },
            {"permission": "app:*:read", "resourceDefinitions": []},
        ]

        test_data = {
            "name": "role_name",
            "display_name": "role_display",
            "access": access_data,
        }
        response = client.put(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["errors"][0]["detail"], "System roles may not be updated.")

    def test_delete_default_role(self):
        """Test that default roles are protected from deletion"""
        url = reverse("v1_management:role-detail", kwargs={"uuid": self.defRole.uuid})
        client = APIClient()
        response = client.delete(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # verify the role still exists
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_delete_role_invalid(self):
        """Test that deleting an invalid role returns an error."""
        url = reverse("v1_management:role-detail", kwargs={"uuid": uuid4()})
        client = APIClient()
        response = client.delete(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_delete_role_child_workspace(self, mock_method):
        """Test that we can delete an existing role bound to a child workspace.

        This is a regression test: a previous version of the dual-write code attempted to remove the parent-child
        relationship between the workspace a role is bound to (if not the default workspace) and the default
        workspace."""

        workspace_definition = {
            "attributeFilter": {
                "key": "group.id",
                "operation": "equal",
                "value": str(self.child_workspace.id),
            }
        }

        role_name = "roleA"
        access_data = [
            {
                "permission": "app:*:read",
                "resourceDefinitions": [workspace_definition],
            },
            {
                "permission": "cost-management:*:*",
                "resourceDefinitions": [workspace_definition],
            },
        ]
        response = self.create_role(role_name, in_access_data=access_data)

        role_uuid = response.data.get("uuid")
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        client = APIClient()
        replication_event = {
            "relations_to_add": [],
            "relations_to_remove": [],
            "resource_context": {
                "org_id": str(self.tenant.org_id),
                "event_type": "delete_custom_role",
                "created_at": ANY,
            },
        }
        current_relations = relation_api_tuples_for_v1_role(role_uuid, bound_workspace_id=str(self.child_workspace.id))
        replication_event["relations_to_remove"] = current_relations
        response = client.delete(url, **self.headers)
        actual_call_arg = mock_method.call_args[0][0]
        expected_sorted = normalize_and_sort(replication_event)
        actual_sorted = normalize_and_sort(actual_call_arg)
        self.assertEqual(expected_sorted, actual_sorted)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_system_flag_filter(self):
        """Test that we can filter roles based on system flag."""
        client = APIClient()
        response = client.get(URL, **self.headers)

        self.assertEqual(len(response.data.get("data")), 5)

        url = f"{URL}?system=true"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(len(response.data.get("data")), 4)
        role = response.data.get("data")[0]
        self.assertEqual(role.get("system"), True)

        url = f"{URL}?system=false"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(len(response.data.get("data")), 1)
        role = response.data.get("data")[0]
        self.assertEqual(role.get("system"), False)

    def test_external_tenant_filter(self):
        """Test that we can filter roles based on external_tenant."""
        client = APIClient()
        response = client.get(URL, **self.headers)

        self.assertEqual(len(response.data.get("data")), 5)

        url = f"{URL}?external_tenant=foo"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(len(response.data.get("data")), 1)
        role = response.data.get("data")[0]
        self.assertEqual(role.get("external_tenant"), "foo")

    def test_list_role_admin_platform_default_groups(self):
        """Test roles with both admin and platform default groups."""
        client = APIClient()
        response = client.get(URL, **self.headers)

        self.assertEqual(len(response.data.get("data")), 5)

        url = f"{URL}?display_name=platform_admin_default_display&add_fields=groups_in_count%2Cgroups_in"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(len(response.data.get("data")), 1)
        role = response.data.get("data")[0]
        self.assertEqual(role.get("groups_in_count"), 2)

    def test_create_duplicate_role_fail(self):
        """
        Test that it is not possible to create a custom role with the same name for a tenant.
        """
        client = APIClient()
        name = "Duplicate role name"
        test_data = {"name": name, "access": []}

        # Create new role
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Try to create the same role again
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            f"Role '{name}' already exists for a tenant.",
        )

    def test_create_role_with_system_role_name_fail(self):
        """Test that creating a custom role with the same name as a system role is rejected."""
        client = APIClient()
        # Use the system role name from setUp (case-insensitive match)
        test_data = {"name": "SYSTEM_ROLE", "access": []}

        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conflicts with an existing system role", str(response.data))

    def test_create_role_with_system_role_display_name_fail(self):
        """Test that creating a custom role with the same display_name as a system role is rejected."""
        client = APIClient()
        # Use the system role display_name from setUp (case-insensitive match)
        test_data = {"name": "unique_custom_name", "display_name": "System_Display", "access": []}

        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conflicts with an existing system role", str(response.data))

    def test_update_role_to_system_role_name_fail(self):
        """Test that updating a custom role to have the same name as a system role is rejected."""
        client = APIClient()
        # Create a custom role first
        test_data = {"name": "my_custom_role", "access": []}
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")

        # Try to update name to match system role (case-insensitive)
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        update_data = {"name": "System_Role", "display_name": "my_custom_role", "access": []}
        response = client.put(url, update_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conflicts with an existing system role", str(response.data))

    def test_patch_role_to_system_role_name_fail(self):
        """Test that patching a custom role to have the same name as a system role is rejected."""
        client = APIClient()
        # Create a custom role first
        test_data = {"name": "my_patchable_role", "access": []}
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")

        # Try to patch name to match system role (case-insensitive)
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        patch_data = {"name": "SYSTEM_ROLE"}
        response = client.patch(url, patch_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conflicts with an existing system role", str(response.data))

    def test_update_role_to_system_role_display_name_fail(self):
        """Test that updating a custom role to have the same display_name as a system role is rejected."""
        client = APIClient()
        # Create a custom role first
        test_data = {"name": "my_updatable_dn_role", "access": []}
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")

        # Try to update display_name to match system role (case-insensitive)
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        update_data = {"name": "my_updatable_dn_role", "display_name": "SYSTEM_DISPLAY", "access": []}
        response = client.put(url, update_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conflicts with an existing system role", str(response.data))

    def test_patch_role_to_system_role_display_name_fail(self):
        """Test that patching a custom role to have the same display_name as a system role is rejected."""
        client = APIClient()
        # Create a custom role first
        test_data = {"name": "my_patchable_dn_role", "access": []}
        response = client.post(URL, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        role_uuid = response.data.get("uuid")

        # Try to patch display_name to match system role (case-insensitive)
        url = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
        patch_data = {"display_name": "System_Display"}
        response = client.patch(url, patch_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conflicts with an existing system role", str(response.data))

    @override_settings(ROLE_CREATE_ALLOW_LIST="someApp")
    def test_create_role_with_invalid_equals_operation(self):
        """Test that we cannot create a role when a List value is paired with the 'equal' operation."""
        role_name = "roleFail"
        Permission.objects.create(
            application="someApp",
            resource_type="*",
            verb="*",
            permission="someApp:*:*",
            tenant=self.tenant,
        )
        access_data = [
            {
                "permission": "someApp:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "equal",
                            "value": ["value1", "value2"],
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.data["errors"][0]["source"], "resourceDefinitions.attributeFilter.format")
        self.assertEqual(
            response.data["errors"][0]["detail"], "attributeFilter operation 'equal' expects a String value or None"
        )

    def test_create_role_with_invalid_in_operation(self):
        """Test that we cannot create a role when a String value is paired with the 'in' operation."""
        role_name = "roleFail"
        access_data = [
            {
                "permission": "someApp:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "keyA.id",
                            "operation": "in",
                            "value": "123456",
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.data["errors"][0]["source"], "resourceDefinitions.attributeFilter.format")
        self.assertEqual(response.data["errors"][0]["detail"], "attributeFilter operation 'in' expects a List value")

    @override_settings(
        ROLE_CREATE_ALLOW_LIST="inventory", REPLICATION_TO_RELATION_ENABLED=True, REMOVE_NULL_VALUE=True
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_create_role_with_null_value(self, replicate):
        """Test that create a role with a null value success."""
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate
        # Set up
        Permission.objects.create(permission="inventory:groups:*", tenant=self.public_tenant)
        Permission.objects.create(permission="inventory:groups:read", tenant=self.public_tenant)
        standard_ws = Workspace.objects.create(
            name="Test Workspace", tenant=self.tenant, parent=self.default_workspace
        )
        role_name = "test_create_role"
        access_data = [
            {
                "permission": "inventory:groups:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "group.id",
                            "operation": "in",
                            "value": [None],
                        }
                    }
                ],
            }
        ]

        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        bindingmappings = BindingMapping.objects.filter(role=Role.objects.get(uuid=response.data["uuid"]))
        self.assertEqual(bindingmappings.count(), 1)
        bindingmapping = bindingmappings.first()
        ungrouped_ws = Workspace.objects.get(tenant=self.tenant, type=Workspace.Types.UNGROUPED_HOSTS)
        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(ungrouped_ws.id)),
                    relation("binding"),
                    subject("rbac", "role_binding", bindingmapping.mappings["id"]),
                )
            ),
        )
        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "role_binding", bindingmapping.mappings["id"]),
                    relation("role"),
                    subject("rbac", "role", bindingmapping.mappings["role"]["id"]),
                )
            ),
        )
        self.assertEqual(
            1,
            tuples.count_tuples(
                all_of(
                    resource("rbac", "role", bindingmapping.mappings["role"]["id"]),
                    relation("inventory_groups_all"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )

    @override_settings(ROLE_CREATE_ALLOW_LIST="inventory")
    @patch("rbac.middleware.FEATURE_FLAGS.is_remove_null_value_enabled", return_value=False)
    def test_retrieving_role_replaces_null_value(self, ff_is_remove_null_value_enabled: Mock):
        """Test that we can replace the null value in resource definition when retrieving role."""
        role_name = "test-role"
        Permission.objects.create(permission="inventory:*:*", tenant=self.tenant)
        access_data = [
            {
                "permission": "inventory:*:*",
                "resourceDefinitions": [
                    {
                        "attributeFilter": {
                            "key": "group.id",
                            "operation": "in",
                            "value": [None],
                        }
                    }
                ],
            }
        ]
        response = self.create_role(role_name, in_access_data=access_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # test that we can retrieve the role
        url = reverse("v1_management:role-detail", kwargs={"uuid": response.data.get("uuid")})
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertIsNotNone(response.data.get("uuid"))
        self.assertIsNotNone(response.data.get("name"))
        self.assertEqual(role_name, response.data.get("name"))
        self.assertIsInstance(response.data.get("access"), list)
        self.assertEqual(access_data, response.data.get("access"))

        # test that null value will be replaced
        ff_is_remove_null_value_enabled.return_value = True
        response = client.get(url, **self.headers)

        ungrouped_hosts_id = Workspace.objects.get(tenant=self.tenant, type=Workspace.Types.UNGROUPED_HOSTS)
        access_data[0]["resourceDefinitions"][0]["attributeFilter"]["value"] = [str(ungrouped_hosts_id.id)]
        self.assertEqual(response.data.get("access"), access_data)

    def test_groups_in_only_from_custom_default_group(self):
        """
        Test roles being in default groups won't show up
        when organization has custom default group that doesn't include the role.
        """
        # Create a role that will be in public default group but NOT in custom default group
        test_role_name = "test_role_not_in_custom_default"
        test_role = Role.objects.create(
            name=test_role_name,
            platform_default=True,  # This makes it part of public default group
            system=True,
            tenant=self.public_tenant,
        )

        # Create permission and access for the test role
        test_permission = Permission.objects.create(permission="test:*:*", tenant=self.public_tenant)
        Access.objects.create(permission=test_permission, role=test_role, tenant=self.public_tenant)

        # Ensure public default group exists, or create it
        public_default_group, created = Group.objects.get_or_create(
            platform_default=True,
            tenant=self.public_tenant,
            defaults={"name": "Default access", "description": "Default access group", "system": True},
        )

        # Create a policy for the public default group if it doesn't have one
        public_default_policy, policy_created = Policy.objects.get_or_create(
            group=public_default_group,
            defaults={
                "name": f"System Policy for Group {public_default_group.uuid}",
                "system": True,
                "tenant": self.public_tenant,
            },
        )

        # Add our test role to the public default group
        public_default_policy.roles.add(test_role)

        # Create a CUSTOM default group for our tenant that explicitly EXCLUDES the test role
        custom_default_group = Group.objects.create(
            name="Custom default access",
            platform_default=True,
            system=False,  # Custom groups are not system groups
            tenant=self.tenant,
        )

        # Create a policy for the custom default group that does NOT include our test role
        custom_default_policy = Policy.objects.create(
            name="Custom default policy", system=True, tenant=self.tenant, group=custom_default_group
        )
        # Intentionally NOT adding test_role to this policy
        # Only add existing roles that should be in custom default
        custom_default_policy.roles.add(self.defRole)  # Add a different role

        # Now test the groups_in API
        url = f"{URL}?add_fields=groups_in,groups_in_count"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.data.get("data")

        # Find our test role in the response
        test_role_response = next(
            (role for role in response_data if role["name"] == test_role_name),
            None,
        )

        # Assert that our test role is in the response
        self.assertIsNotNone(test_role_response, f"Test role '{test_role_name}' not found in API response")

        groups_in = test_role_response.get("groups_in", [])
        group_names = [group["name"] for group in groups_in]

        # CORRECT BEHAVIOR: groups_in should be empty since:
        # - Role is NOT in the custom default group for this tenant
        # - Should not fall back to public default group when custom exists
        self.assertEqual(
            len(groups_in),
            0,
            f"Role '{test_role_name}' incorrectly shows as being in groups: {group_names}. "
            f"Expected empty list since role is not in custom default group and should not fall back to public default.",
        )


class RoleViewNonAdminTests(IdentityRequest):
    """Test the role view for nonadmin user."""

    def setUp(self):
        """Set up the role viewset nonadmin tests."""
        super().setUp()
        bootstrap_tenant_for_v2_test(self.tenant)

        platform_default_role_config = {
            "name": "platform_default_role",
            "display_name": "Platform Default Role",
            "system": True,
            "platform_default": True,
            "admin_default": False,
        }
        self.platform_default_role = Role.objects.create(**platform_default_role_config, tenant=self.tenant)

        admin_default_role_config = {
            "name": "admin_default_role",
            "display_name": "Admin Default Role",
            "system": True,
            "platform_default": False,
            "admin_default": True,
        }
        self.admin_default_role = Role.objects.create(**admin_default_role_config, tenant=self.tenant)

        not_system_role_config = {
            "name": "not_system_role",
            "display_name": "Not System Role",
            "system": False,
            "platform_default": False,
            "admin_default": False,
        }

        self.not_system_role = Role.objects.create(**not_system_role_config, tenant=self.tenant)

        self.system_roles_count = 2
        self.non_system_roles_count = 1

        self.display_fields = {
            "applications",
            "description",
            "uuid",
            "name",
            "display_name",
            "system",
            "created",
            "policyCount",
            "accessCount",
            "modified",
            "platform_default",
            "admin_default",
            "external_role_id",
            "external_tenant",
        }

        # Create 2 non org admin principals and 1 org admin
        # 1. user based principal
        self.user_based_principal = Principal(username="user_based_principal", tenant=self.tenant)
        self.user_based_principal.save()

        customer_data = {
            "account_id": self.tenant.account_id,
            "tenant_name": self.tenant.tenant_name,
            "org_id": self.tenant.org_id,
        }

        request_context_user_based_principal = self._create_request_context(
            customer_data=customer_data,
            user_data={
                "username": self.user_based_principal.username,
                "email": "test@email.com",
            },
            is_org_admin=False,
        )
        self.headers_user_based_principal = request_context_user_based_principal["request"].META

        # 2. service account based principal
        service_account_data = self._create_service_account_data()
        self.service_account_principal = Principal(
            username=service_account_data["username"],
            tenant=self.tenant,
            type="service-account",
            service_account_id=service_account_data["client_id"],
        )
        self.service_account_principal.save()

        request_context_service_account_principal = self._create_request_context(
            customer_data=customer_data,
            service_account_data=service_account_data,
            is_org_admin=False,
        )
        self.headers_service_account_principal = request_context_service_account_principal["request"].META

        # 3 org admin principal in the tenant
        self.org_admin = Principal(username="org_admin", tenant=self.tenant)
        self.org_admin.save()

        request_context_org_admin = self._create_request_context(
            customer_data=customer_data,
            user_data={"username": self.org_admin.username, "email": "test@email.com"},
            is_org_admin=True,
        )
        self.headers_org_admin = request_context_org_admin["request"].META

        # Error messages
        self.no_permission_err_message = "You do not have permission to perform this action."

    def tearDown(self):
        """Tear down role viewset nonadmin tests."""
        with self.subTest(msg="V2 consistency"):
            self._assert_v2_consistent()

        Group.objects.all().delete()
        Principal.objects.all().delete()
        Role.objects.all().delete()

        # we need to delete old test_tenant's that may exist in cache
        test_tenant_org_id = "100001"
        cached_tenants = TenantCache()
        cached_tenants.delete_tenant(test_tenant_org_id)
        # Clear principal cache to avoid stale data between tests
        from management.utils import PRINCIPAL_CACHE

        PRINCIPAL_CACHE.delete_all_principals_for_tenant("100001")
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)

    def _assert_v2_consistent(self):
        assert_v1_v2_locally_consistent(test=self)

    @staticmethod
    def _create_group_with_user_access_admin_role(tenant):
        """Create a group with a 'User Access administrator' role."""
        # Create a group with 'User Access administrator' role
        rbac_admin_permission = Permission.objects.create(
            application="rbac",
            permission="rbac:*:*",
            resource_type="*",
            verb="*",
            tenant=tenant,
        )
        user_access_administrator_role = Role.objects.create(
            admin_default=True,
            description="User Access administrator role description",
            display_name="User Access administrator",
            platform_default=False,
            system=True,
            tenant=tenant,
        )
        Access.objects.create(
            permission=rbac_admin_permission,
            role=user_access_administrator_role,
            tenant=tenant,
        )
        rbac_admin_group = Group.objects.create(
            admin_default=False,
            description="A group with the 'User Access administrator' role",
            name="rbac_admin_group",
            platform_default=False,
            system=False,
            tenant=tenant,
        )
        policy_for_rbac_admin_group = Policy.objects.create(
            group=rbac_admin_group,
            name="Policy for rbac_admin_group",
            system=True,
            tenant=tenant,
        )
        policy_for_rbac_admin_group.roles.add(user_access_administrator_role)
        return rbac_admin_group

    def test_list_roles_without_User_Access_Admin_fail(self):
        """
        Test that principal without 'User Access administrator' role cannot read a list of roles.
        """
        client = APIClient()
        url = reverse("v1_management:role-list")

        response = client.get(url, **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("errors")[0].get("detail"), self.no_permission_err_message)

        response = client.get(url, **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("errors")[0].get("detail"), self.no_permission_err_message)

        # Org Admin can list the roles
        response = client.get(url, **self.headers_org_admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected_count = self.system_roles_count + self.non_system_roles_count
        self.assertEqual(len(response.data.get("data")), expected_count)

    def test_list_roles_with_User_Access_Admin_success(self):
        """
        Test that principal with 'User Access administrator' role can read a list of roles.
        """
        # Create a group with 'User Access administrator' role and add principals we use in headers
        group_with_UA_admin = self._create_group_with_user_access_admin_role(self.tenant)
        group_with_UA_admin.principals.add(self.user_based_principal, self.service_account_principal)

        client = APIClient()
        url = reverse("v1_management:role-list")

        response = client.get(url, **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected_count = self.system_roles_count + self.non_system_roles_count + 1
        self.assertEqual(len(response.data.get("data")), expected_count)

        response = client.get(url, **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected_count = self.system_roles_count + self.non_system_roles_count + 1
        self.assertEqual(len(response.data.get("data")), expected_count)

    def test_list_roles_without_User_Access_Admin_system_true_success(self):
        """
        Test that principal without 'User Access administrator' role can read a list of roles
        with '?system=true' in the request.
        """
        client = APIClient()
        url = reverse("v1_management:role-list") + "?system=true"

        response = client.get(url, **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), self.system_roles_count)

        response = client.get(url, **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), self.system_roles_count)

    def test_list_roles_with_User_Access_Admin_system_true_success(self):
        """
        Test that principal with 'User Access administrator' role can read a list of roles
        with '?system=true' in the request.
        """
        # Create a group with 'User Access administrator' role and add principals we use in headers
        group_with_UA_admin = self._create_group_with_user_access_admin_role(self.tenant)
        group_with_UA_admin.principals.add(self.user_based_principal, self.service_account_principal)
        client = APIClient()
        url = reverse("v1_management:role-list") + "?system=true"

        response = client.get(url, **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected_count = self.system_roles_count + 1
        self.assertEqual(len(response.data.get("data")), expected_count)

        response = client.get(url, **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected_count = self.system_roles_count + 1
        self.assertEqual(len(response.data.get("data")), expected_count)

    def test_list_roles_without_User_Access_Admin_system_false_fail(self):
        """
        Test that principal without 'User Access administrator' role cannot read a list of roles
        with '?system=false' in the request.
        """
        client = APIClient()
        url = reverse("v1_management:role-list") + "?system=false"

        response = client.get(url, **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("errors")[0].get("detail"), self.no_permission_err_message)

        response = client.get(url, **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("errors")[0].get("detail"), self.no_permission_err_message)

        # Org Admin can list the roles
        response = client.get(url, **self.headers_org_admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), self.non_system_roles_count)

    def test_list_roles_with_User_Access_Admin_system_false_success(self):
        """
        Test that principal with 'User Access administrator' role can read a list of roles
        with '?system=false' in the request.
        """
        # Create a group with 'User Access administrator' role and add principals we use in headers
        group_with_UA_admin = self._create_group_with_user_access_admin_role(self.tenant)
        group_with_UA_admin.principals.add(self.user_based_principal, self.service_account_principal)
        client = APIClient()
        url = reverse("v1_management:role-list") + "?system=false"

        response = client.get(url, **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), self.non_system_roles_count)

        response = client.get(url, **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), self.non_system_roles_count)

    def test_list_roles_without_User_Access_Admin_system_foo_fail(self):
        """
        Test that principal without 'User Access administrator' role cannot read a list of roles
        with '?system=foo' in the request (where 'foo' is not supported value for the 'system' param
        so the 'system' param is ignored in this case).
        """
        client = APIClient()
        url = reverse("v1_management:role-list") + "?system=foo"

        response = client.get(url, **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("errors")[0].get("detail"), self.no_permission_err_message)

        response = client.get(url, **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("errors")[0].get("detail"), self.no_permission_err_message)

        # Org Admin can list the roles
        response = client.get(url, **self.headers_org_admin)
        expected_count = self.system_roles_count + self.non_system_roles_count
        self.assertEqual(len(response.data.get("data")), expected_count)

    def test_list_roles_with_User_Access_Admin_system_foo_success(self):
        """
        Test that principal with 'User Access administrator' role can read a list of roles
        with '?system=foo' in the request (where 'foo' is not supported value for the 'system' param
        so the 'system' param is ignored in this case).
        """
        # Create a group with 'User Access administrator' role and add principals we use in headers
        group_with_UA_admin = self._create_group_with_user_access_admin_role(self.tenant)
        group_with_UA_admin.principals.add(self.user_based_principal, self.service_account_principal)

        client = APIClient()
        url = reverse("v1_management:role-list")

        response = client.get(url, **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected_count = self.system_roles_count + self.non_system_roles_count + 1
        self.assertEqual(len(response.data.get("data")), expected_count)

        response = client.get(url, **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected_count = self.system_roles_count + self.non_system_roles_count + 1
        self.assertEqual(len(response.data.get("data")), expected_count)

    @override_settings(
        ROLE_CREATE_ALLOW_LIST="cost-management,remediations,inventory,drift,policies,advisor,catalog,approval,"
        "vulnerability,compliance,automation-analytics,notifications,patch,integrations,ros,"
        "staleness,config-manager"
    )
    def test_create_role_with_rbac_permission_fail(self):
        """
        Test that it is not possible to create a custom role with RBAC permission.
        """
        # Create a group with 'User Access administrator' role and add principals we use in headers
        group_with_UA_admin = self._create_group_with_user_access_admin_role(self.tenant)
        group_with_UA_admin.principals.add(self.user_based_principal, self.service_account_principal)

        Permission.objects.create(permission="rbac:principal:read", tenant=self.tenant)
        permissions_count = len(Permission.objects.all())

        client = APIClient()

        # Test that permissions are present in the RBAC db
        url = reverse("v1_management:permission-list")
        response = client.get(url, **self.headers_org_admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get("data")), permissions_count)

        # It is not possible to create custom role with RBAC permission
        url = reverse("v1_management:role-list")
        role_name = "My custom role"
        access_data = [{"permission": "rbac:*:*", "resourceDefinitions": []}]
        test_data = {"name": role_name, "access": access_data}

        response = client.post(url, test_data, format="json", **self.headers_user_based_principal)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            "Custom roles cannot be created for rbac",
        )

        response = client.post(url, test_data, format="json", **self.headers_service_account_principal)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            "Custom roles cannot be created for rbac",
        )

        response = client.post(url, test_data, format="json", **self.headers_org_admin)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            "Custom roles cannot be created for rbac",
        )


class RoleWorkspaceValidationTests(IdentityRequest):
    """Test the v1 role workspace validation logic"""

    def setUp(self):
        """Set up the workspace validation tests."""
        super().setUp()

        # Create 2 separate tenants to test with
        self.tenant1 = Tenant.objects.create(
            tenant_name="tenant1", account_id="tenant1_acc_id", org_id="tenant1_org_id"
        )

        self.tenant2 = Tenant.objects.create(
            tenant_name="tenant2", account_id="tenant2_acc_id", org_id="tenant2_org_id"
        )

        # Create workspaces in different tenants
        self.root_workspace_t1 = Workspace.objects.create(
            name="root",
            description="Root workspace",
            tenant=self.tenant1,
            type="root",
        )
        self.default_workspace_t1 = Workspace.objects.create(
            name="default",
            description="Default workspace",
            tenant=self.tenant1,
            parent=self.root_workspace_t1,
            type="default",
        )
        self.tenant1_workspace = Workspace.objects.create(
            name="Tenant 1 Workspace", description="Workspace in same tenant", tenant=self.tenant1
        )

        self.root_workspace_t2 = Workspace.objects.create(
            name="root",
            description="Root workspace",
            tenant=self.tenant2,
            type="root",
        )
        self.default_workspace_t2 = Workspace.objects.create(
            name="default",
            description="Default workspace",
            tenant=self.tenant2,
            parent=self.root_workspace_t2,
            type="default",
        )
        self.tenant2_workspace = Workspace.objects.create(
            name="Tenant 2 Workspace", description="Workspace in different tenant", tenant=self.tenant2
        )

        # Create a permission for testing
        self.test_permission = Permission.objects.create(
            application="inventory",
            resource_type="groups",
            verb="read",
            permission="inventory:groups:read",
            tenant=self.tenant1,
        )

        # Mock user with org_id
        self.request_user_mock = Mock()
        self.request_user_mock.org_id = "tenant1_org_id"

        # Mock request object
        self.request_mock = Mock()
        self.request_mock.tenant = self.tenant1
        self.request_mock.user = self.request_user_mock

    def test_workspace_validation_same_tenant_passes_with_equal_operation(self):
        """Test that workspace validation passes when workspace belongs to same tenant."""
        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.tenant1_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        # Create viewset instance and call validate_role
        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        # Should not raise any exception
        try:
            viewset.validate_role(self.request_mock)
        except Exception as e:
            self.fail(f"validate_role raised an unexpected exception: {e}")

    def test_workspace_validation_same_tenant_passes_with_in_operation(self):
        """Test that workspace validation passes when workspace belongs to same tenant."""
        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "in",
                                "value": [str(self.tenant1_workspace.id), str(self.default_workspace_t1.id)],
                            }
                        }
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        # Create viewset instance and call validate_role
        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        # Should not raise any exception
        try:
            viewset.validate_role(self.request_mock)
        except Exception as e:
            self.fail(f"validate_role raised an unexpected exception: {e}")

    def test_workspace_validation_different_tenant_fails_equal_operation(self):
        """Test that workspace validation fails when workspace belongs to different tenant using the equal operation."""
        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.tenant2_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        from rest_framework import serializers
        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        with self.assertRaises(serializers.ValidationError) as context:
            viewset.validate_role(self.request_mock)

        error_dict = context.exception.detail
        self.assertIn("role", error_dict)
        error_msg = str(error_dict["role"][0])
        self.assertIn(
            "user from org 'tenant1_org_id' cannot add permission 'inventory:groups:read' to workspace outside their org",
            error_msg,
        )
        self.assertIn(f"Invalid workspace IDs: {self.tenant2_workspace.id}", error_msg)

    def test_workspace_validation_different_tenant_fails_in_operation(self):
        """Test that workspace validation fails when workspace belongs to different tenant using the in operation."""
        random_uuid = str(uuid4())
        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "in",
                                "value": [
                                    str(self.tenant1_workspace.id),  # Valid
                                    str(self.tenant2_workspace.id),  # Invalid (different tenant)
                                    random_uuid,  # Invalid (random)
                                ],
                            }
                        }
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        from rest_framework import serializers
        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        with self.assertRaises(serializers.ValidationError) as context:
            viewset.validate_role(self.request_mock)

        error_dict = context.exception.detail
        self.assertIn("role", error_dict)
        error_msg = str(error_dict["role"][0])
        self.assertIn("cannot add permission", error_msg)
        self.assertIn(str(self.tenant2_workspace.id), error_msg)
        self.assertIn(random_uuid, error_msg)
        # Ensure valid workspace is NOT in the error message
        self.assertNotIn(str(self.tenant1_workspace.id), error_msg)

    def test_workspace_validation_non_workspace_resource_skipped(self):
        """Test that non-workspace resources skip validation."""
        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "other.id",  # Not group.id
                                "operation": "equal",
                                "value": str(self.tenant2_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        # Should not raise any exception since it's not a workspace resource
        try:
            viewset.validate_role(self.request_mock)
        except Exception as e:
            self.fail(f"validate_role raised an unexpected exception: {e}")

    def test_workspace_validation_non_inventory_app_fails(self):
        """Test that non-inventory applications skip workspace validation."""
        # Create permission for different app
        Permission.objects.create(
            application="app", resource_type="groups", verb="read", permission="app:groups:read", tenant=self.tenant1
        )

        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "app:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.tenant2_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        from rest_framework import serializers
        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        with self.assertRaises(serializers.ValidationError) as context:
            viewset.validate_role(self.request_mock)

        error_dict = context.exception.detail
        self.assertIn("role", error_dict)
        error_msg = str(error_dict["role"][0])
        self.assertIn(
            "user from org 'tenant1_org_id' cannot add permission 'app:groups:read' to workspace outside their org",
            error_msg,
        )
        self.assertIn(f"Invalid workspace IDs: {self.tenant2_workspace.id}", error_msg)

    def test_workspace_validation_multiple_resource_definitions(self):
        """Test validation with multiple resourceDefinitions, some valid, some invalid."""
        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.tenant1_workspace.id),  # Valid
                            }
                        },
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.tenant2_workspace.id),  # Invalid
                            }
                        },
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        from rest_framework import serializers
        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        # Should fail because one of the resourceDefinitions is invalid
        with self.assertRaises(serializers.ValidationError) as context:
            viewset.validate_role(self.request_mock)

        error_dict = context.exception.detail
        self.assertIn("role", error_dict)
        self.assertIn("cannot add permission", str(error_dict["role"][0]))

    def test_workspace_validation_empty_resource_definitions(self):
        """Test validation with empty resourceDefinitions."""
        request_data = {
            "name": "test_role",
            "access": [{"permission": "inventory:groups:read", "resourceDefinitions": []}],
        }

        self.request_mock.data = request_data

        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        # Should not raise exception with empty resourceDefinitions
        try:
            viewset.validate_role(self.request_mock)
        except Exception as e:
            self.fail(f"validate_role raised an unexpected exception: {e}")

    def test_workspace_validation_missing_attribute_filter(self):
        """Test validation when resourceDefinition lacks attributeFilter."""
        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            # Missing attributeFilter
                        }
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        from rest_framework import serializers
        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        # Should raise exception when attributeFilter is missing
        with self.assertRaises(serializers.ValidationError) as context:
            viewset.validate_role(self.request_mock)

        error_dict = context.exception.detail
        self.assertIn("resourceDefinitions", error_dict)
        self.assertIn("attributeFilter", error_dict["resourceDefinitions"][0])
        self.assertIn("This field is required.", error_dict["resourceDefinitions"][0]["attributeFilter"][0])

    @patch("management.role.view.get_workspace_ids_from_resource_definition")
    def test_workspace_validation_get_workspace_id_called(self, mock_get_workspace_ids):
        """Test that get_workspace_ids_from_resource_definition is called with correct parameters."""
        mock_get_workspace_ids.return_value = [self.tenant1_workspace.id]

        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.tenant1_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        from management.role.view import RoleViewSet

        viewset = RoleViewSet()
        viewset.validate_role(self.request_mock)

        # Verify get_workspace_ids_from_resource_definition was called
        mock_get_workspace_ids.assert_called_once_with(
            {"key": "group.id", "operation": "equal", "value": str(self.tenant1_workspace.id)}
        )

    def test_workspace_validation_nonexistent_workspace_id(self):
        """Test validation when workspace ID doesn't exist in any tenant."""
        fake_workspace_id = str(uuid4())

        request_data = {
            "name": "test_role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {"attributeFilter": {"key": "group.id", "operation": "equal", "value": fake_workspace_id}}
                    ],
                }
            ],
        }

        self.request_mock.data = request_data

        from rest_framework import serializers
        from management.role.view import RoleViewSet

        viewset = RoleViewSet()

        # Should fail because workspace doesn't exist in any tenant
        with self.assertRaises(serializers.ValidationError) as context:
            viewset.validate_role(self.request_mock)

        error_dict = context.exception.detail
        self.assertIn("role", error_dict)
        self.assertIn("cannot add permission", str(error_dict["role"][0]))
