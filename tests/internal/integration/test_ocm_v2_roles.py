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
"""Contract tests for the OCM V2 roles-for-group integration."""

import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from django.db import connection
from django.test.utils import CaptureQueriesContext
from management.models import Group, Permission, Policy, Principal, Role
from management.permission.scope_service import ImplicitResourceService
from management.role.definer import _seed_v2_role_from_v1
from management.role.model import ExtRoleRelation, ExtTenant
from management.role.v2_model import RoleV2
from management.role_binding.model import RoleBinding, RoleBindingGroup
from management.tenant_mapping.model import TenantMapping
from rest_framework.test import APIClient
from tests.identity_request import IdentityRequest

from api.models import Tenant


class OCMV2RolesTests(IdentityRequest):
    """Exercise both implementations through the real integration URL."""

    def setUp(self):
        """Create an internal caller and a group with independent V1 and V2 roles."""
        super().setUp()
        self.client = APIClient()
        self.meta = self._create_request_context(self.customer_data, self.user_data, is_internal=True)["request"].META
        self.group = Group.objects.create(name="OCM group", tenant=self.tenant)
        self.v1 = Role.objects.create(name="OCM Cluster Owner", tenant=self.tenant, description="Cluster ownership")
        self.policy = Policy.objects.create(name="OCM policy", tenant=self.tenant, group=self.group)
        self.policy.roles.add(self.v1)
        self.role = RoleV2.objects.create(
            name=self.v1.name, description=self.v1.description, tenant=self.tenant, v1_source=self.v1
        )
        self.bind(self.role)
        self.public = Tenant.objects.get(tenant_name="public")
        self.url = f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{self.group.uuid}/roles/"

    def bind(self, role, group=None, tenant=None, **kwargs):
        """Persist a real binding and group entry."""
        binding = RoleBinding.objects.create(
            role=role, tenant=tenant or self.tenant, resource_type="workspace", resource_id=str(uuid4()), **kwargs
        )
        RoleBindingGroup.objects.create(binding=binding, group=group or self.group)
        return binding

    def get_roles(self, enabled=True, query="", url=None):
        """Call the endpoint with only the external flag evaluation mocked."""
        with patch("internal.integration.views.FEATURE_FLAGS.is_ocm_v2_enabled_global", return_value=enabled) as flag:
            response = self.client.get((url or self.url) + query, **self.meta)
        flag.assert_called_once_with()
        return response

    def test_disabled_preserves_v1_response(self):
        """V1 assignments remain authoritative when the OCM rollout is disabled."""
        self.role.name = "V2-only name"
        self.role.save()
        response = self.get_roles(enabled=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"][0]["name"], self.v1.name)
        self.assertEqual(response.data["data"][0]["uuid"], str(self.v1.uuid))

    def test_global_flag_selects_the_same_source_for_different_tenants(self):
        """Every target organization uses the same context-free OCM rollout decision."""
        other = Tenant.objects.create(tenant_name="global rollout", org_id="47378999")
        group = Group.objects.create(name="other group", tenant=other)
        v1 = Role.objects.create(name="other V1 role", tenant=other)
        policy = Policy.objects.create(name="other policy", tenant=other, group=group)
        policy.roles.add(v1)
        v2 = RoleV2.objects.create(name="other V2 role", tenant=other)
        self.bind(v2, group=group, tenant=other)
        other_url = f"/_private/api/v1/integrations/tenant/{other.org_id}/groups/{group.uuid}/roles/"
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                for url, expected in (
                    (self.url, self.role if enabled else self.v1),
                    (other_url, v2 if enabled else v1),
                ):
                    response = self.get_roles(enabled=enabled, url=url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual([row["uuid"] for row in response.data["data"]], [str(expected.uuid)])

    def test_v2_only_role_and_complete_schema(self):
        """No V1 policy or role is needed for a V2-native assignment."""
        self.role.v1_source = None
        self.role.save()
        self.policy.delete()
        self.v1.delete()
        permission = Permission.objects.create(permission="ocm:cluster:read", tenant=self.public)
        self.role.permissions.add(permission)
        response = self.get_roles()
        self.assertEqual(response.status_code, 200)
        row = response.data["data"][0]
        expected = {
            "uuid": str,
            "name": str,
            "display_name": str,
            "description": str,
            "created": str,
            "modified": str,
            "policyCount": int,
            "accessCount": int,
            "applications": list,
            "system": bool,
            "platform_default": bool,
            "admin_default": bool,
            "external_role_id": type(None),
            "external_tenant": type(None),
        }
        self.assertEqual(set(row), set(expected))
        for field, field_type in expected.items():
            self.assertIs(type(row[field]), field_type, field)
        self.assertEqual(row["uuid"], str(self.role.uuid))
        self.assertEqual(UUID(row["uuid"]).version, 7)
        self.assertEqual(row["policyCount"], 0)
        self.assertEqual(row["accessCount"], 1)
        self.assertEqual(row["applications"], ["ocm"])
        self.assertFalse(row["system"])
        self.assertFalse(row["platform_default"])
        self.assertFalse(row["admin_default"])

    def test_response_parity(self):
        """Compare compatibility fields, allowing only explicitly changed metadata."""
        v1 = self.get_roles(enabled=False).data
        v2 = self.get_roles().data
        self.assertEqual(v1["meta"], v2["meta"])
        self.assertEqual(v1["links"], v2["links"])
        # The legacy group serializer omits accessCount because that query does not annotate it.
        # The V2 contract explicitly requires the field, and uses V2 IDs/timestamps with no policies.
        intentional_differences = {"uuid", "created", "modified", "policyCount", "accessCount"}
        self.assertEqual(
            {k: v for k, v in v1["data"][0].items() if k not in intentional_differences},
            {k: v for k, v in v2["data"][0].items() if k not in intentional_differences},
        )

    def test_duplicate_bindings_and_other_tenants(self):
        """Deduplicate roles and exclude assignments belonging to another organization."""
        self.bind(self.role)
        other = Tenant.objects.create(tenant_name="other", org_id="other")
        foreign_role = RoleV2.objects.create(name="foreign", tenant=other)
        self.bind(foreign_role, tenant=other)
        # Even a malformed local binding must not disclose another tenant's custom role.
        self.bind(foreign_role)
        response = self.get_roles()
        self.assertEqual(response.data["meta"]["count"], 1)
        self.assertEqual(response.data["data"][0]["accessCount"], 0)

    def test_public_seeded_role_and_external_metadata(self):
        """Keep external OCM metadata on migrated roles and include public seeded roles."""
        external = ExtTenant.objects.create(name="ocm")
        ExtRoleRelation.objects.create(role=self.v1, ext_tenant=external, ext_id="ClusterOwner")
        self.role.tenant = self.public
        self.role.type = RoleV2.Types.SEEDED
        self.role.save()
        response = self.get_roles(query="?role_external_tenant=OCM")
        row = response.data["data"][0]
        self.assertTrue(row["system"])
        self.assertEqual(row["external_role_id"], "ClusterOwner")
        self.assertEqual(row["external_tenant"], "ocm")

    def test_ocm_seed_names_survive_real_seeding(self):
        """Exercise the repository's OCM seed definitions and the actual V2 seeder."""
        definitions = (
            Path(__file__).resolve().parents[3] / "rbac/management/role/definitions/ocm_cluster_local_test.json"
        )
        for definition in json.loads(definitions.read_text())["roles"]:
            with self.subTest(name=definition["name"]):
                v1 = Role.objects.create(name=definition["name"], tenant=self.public, system=True)
                role = _seed_v2_role_from_v1(
                    v1, v1.display_name, definition["description"], self.public, {}, ImplicitResourceService([], [])
                )
                self.bind(role)
                response = self.get_roles(query="?role_name=" + definition["name"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["data"][0]["name"], v1.name)

    def test_default_groups_expand_platform_children(self):
        """Default groups return seeded role names rather than internal aggregate names."""
        mapping = TenantMapping.objects.create(tenant=self.tenant)
        for flag, binding_id in (
            ("platform_default", mapping.default_role_binding_uuid),
            ("admin_default", mapping.default_admin_role_binding_uuid),
        ):
            with self.subTest(flag=flag):
                group = Group.objects.create(name=flag, tenant=self.public, system=True, **{flag: True})
                platform = RoleV2.objects.create(name=flag, tenant=self.public, type=RoleV2.Types.PLATFORM)
                seeded = RoleV2.objects.create(name=flag + " child", tenant=self.public, type=RoleV2.Types.SEEDED)
                platform.children.add(seeded)
                self.bind(platform, group=group, uuid=binding_id)
                url = f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group.uuid}/roles/"
                response = self.get_roles(url=url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row["name"] for row in response.data["data"]], [seeded.name])
                self.assertTrue(response.data["data"][0][flag])
                self.assertTrue(response.data["data"][0]["system"])

    def test_default_flag_on_seeded_role_in_ordinary_group(self):
        """Default role metadata describes the role even in a different group."""
        mapping = TenantMapping.objects.create(tenant=self.tenant)
        platform = RoleV2.objects.create(name="default parent", tenant=self.public, type=RoleV2.Types.PLATFORM)
        self.role.type = RoleV2.Types.SEEDED
        self.role.save()
        platform.children.add(self.role)
        RoleBinding.objects.create(
            tenant=self.tenant,
            role=platform,
            uuid=mapping.root_scope_default_admin_role_binding_uuid,
            resource_type="workspace",
            resource_id=str(uuid4()),
        )
        row = self.get_roles().data["data"][0]
        self.assertTrue(row["admin_default"])
        self.assertFalse(row["platform_default"])

    def test_pagination_and_ordering_parity(self):
        """Pages retain V1 counts, offsets, links and requested ordering."""
        for name in ("Alpha", "Zulu"):
            v1 = Role.objects.create(name=name, tenant=self.tenant)
            self.policy.roles.add(v1)
            self.bind(RoleV2.objects.create(name=name, tenant=self.tenant, v1_source=v1))
        for query in ("?limit=1&offset=1", "?limit=1&offset=0&order_by=-name", "?limit=1&offset=50"):
            with self.subTest(query=query):
                v1, v2 = self.get_roles(False, query).data, self.get_roles(True, query).data
                self.assertEqual(v1["meta"], v2["meta"])
                self.assertEqual(v1["links"], v2["links"])
                self.assertEqual([r["name"] for r in v1["data"]], [r["name"] for r in v2["data"]])

    def test_filters_and_errors(self):
        """Keep supported role filters, validation and empty responses."""
        for query in (
            "?role_name=Owner",
            "?role_description=ownership",
            "?role_display_name=Owner",
            "?role_name=missing",
        ):
            with self.subTest(query=query):
                v1, v2 = self.get_roles(False, query), self.get_roles(True, query)
                self.assertEqual(v1.status_code, v2.status_code)
                self.assertEqual(v1.data["meta"], v2.data["meta"])
        for query in ("?order_by=invalid", "?exclude=invalid"):
            with self.subTest(query=query):
                self.assertEqual(self.get_roles(query=query).status_code, 400)

    def test_exclude(self):
        """Exclusion returns unassigned V2 roles, including V2-native roles."""
        unassigned = RoleV2.objects.create(name="unassigned", tenant=self.tenant)
        response = self.get_roles(query="?exclude=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["name"] for r in response.data["data"]], [unassigned.name])

    def test_empty_group(self):
        """Do not silently fall back to V1 when no V2 assignments exist."""
        self.group.role_binding_entries.all().delete()
        self.assertEqual(self.get_roles().data["data"], [])
        self.assertEqual(len(self.get_roles(enabled=False).data["data"]), 1)

    def test_invalid_missing_and_foreign_group(self):
        """Keep invalid UUID errors and prevent cross-tenant group disclosure."""
        other = Tenant.objects.create(tenant_name="foreign group owner", org_id="foreign-group-owner")
        foreign = Group.objects.create(name="foreign group", tenant=other)
        for group_id, expected in (("invalid", 400), (str(uuid4()), 404), (str(foreign.uuid), 404)):
            with self.subTest(group_id=group_id):
                url = f"/_private/api/v1/integrations/tenant/{self.tenant.org_id}/groups/{group_id}/roles/"
                self.assertEqual(self.get_roles(url=url).status_code, expected)

    def test_external_identity_denied(self):
        """The V2 data path does not relax the integration authentication boundary."""
        self.meta = self._create_request_context(self.customer_data, self.user_data, is_internal=False)["request"].META
        with patch("internal.integration.views.FEATURE_FLAGS.is_ocm_v2_enabled_global", return_value=True):
            self.assertEqual(self.client.get(self.url, **self.meta).status_code, 403)

    def test_regular_v1_endpoint_unchanged(self):
        """The independent flag cannot switch the public V1 management endpoint."""
        with patch("internal.integration.views.FEATURE_FLAGS.is_ocm_v2_enabled_global", return_value=True) as flag:
            response = self.client.get(f"/api/rbac/v1/groups/{self.group.uuid}/roles/", **self.headers)
        flag.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"][0]["uuid"], str(self.v1.uuid))

    def test_role_system_filter(self):
        """V1 Boolean text filtering also works with the derived V2 system flag."""
        for value in ("true", "false"):
            v1 = self.get_roles(False, "?role_system=" + value)
            v2 = self.get_roles(True, "?role_system=" + value)
            self.assertEqual(v1.status_code, v2.status_code)
            self.assertEqual(v1.data["meta"], v2.data["meta"])

    def test_empty_and_nul_filters(self):
        """Preserve empty text filters and reject invalid database text safely."""
        for query in ("?role_name=", "?role_description=", "?role_external_tenant=%20"):
            v1, v2 = self.get_roles(False, query), self.get_roles(True, query)
            self.assertEqual(v1.status_code, v2.status_code)
            self.assertEqual(v1.data["meta"], v2.data["meta"])
        self.assertEqual(self.get_roles(query="?role_name=%00").status_code, 400)

    def test_exclude_with_username(self):
        """Principal-scoped exclusion uses the principal's other V2 group assignments."""
        principal = Principal.objects.create(username="ocm-user", tenant=self.tenant)
        self.group.principals.add(principal)
        second = Group.objects.create(name="second", tenant=self.tenant)
        second.principals.add(principal)
        assigned = RoleV2.objects.create(name="assigned elsewhere", tenant=self.tenant)
        self.bind(assigned, group=second)
        RoleV2.objects.create(name="not assigned", tenant=self.tenant)
        with patch(
            "management.principal.proxy.PrincipalProxy.request_filtered_principals",
            return_value={
                "status_code": 200,
                "data": [
                    {
                        "username": principal.username,
                        "org_id": self.tenant.org_id,
                        "is_org_admin": False,
                        "is_active": True,
                    }
                ],
            },
        ):
            response = self.get_roles(query="?exclude=true&username=ocm-user")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["name"] for row in response.data["data"]], [assigned.name])

    def test_role_metadata_queries_do_not_grow_with_page_size(self):
        """Permission counts, default flags, and external metadata are eagerly loaded."""
        # Warm public-tenant caching before comparing query counts.
        self.get_roles()
        with CaptureQueriesContext(connection) as small:
            self.get_roles()
        for i in range(5):
            self.bind(RoleV2.objects.create(name=f"extra {i}", tenant=self.tenant))
        with CaptureQueriesContext(connection) as large:
            response = self.get_roles()
        self.assertEqual(len(response.data["data"]), 6)
        self.assertEqual(len(small), len(large))
