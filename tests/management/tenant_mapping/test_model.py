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
import uuid

from django.test import TestCase

from management.permission.scope_service import Scope
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from tests.management.role.test_dual_write import RbacFixture


class TenantMappingTests(TestCase):
    fixture: RbacFixture
    tenant_mapping: TenantMapping

    def setUp(self):
        self.fixture = RbacFixture()
        self.tenant_mapping = self.fixture.new_tenant(org_id="test-tenant").mapping

    def test_group_uuid_for(self):
        self.assertEqual(
            self.tenant_mapping.default_group_uuid,
            self.tenant_mapping.group_uuid_for(DefaultAccessType.USER),
        )

        self.assertEqual(
            self.tenant_mapping.default_admin_group_uuid,
            self.tenant_mapping.group_uuid_for(DefaultAccessType.ADMIN),
        )

    def test_default_role_binding_uuid_for_complete(self):
        binding_uuids = set()

        for access_type in DefaultAccessType:
            for scope in Scope:
                try:
                    binding_uuid = self.tenant_mapping.default_role_binding_uuid_for(access_type, scope)
                except Exception as e:
                    self.fail(
                        f"Unable to get binding UUID for access type {access_type.name} and scope {scope.name}: {e}"
                    )

                self.assertIsInstance(binding_uuid, uuid.UUID)

                if binding_uuid in binding_uuids:
                    self.fail(f"Got duplicate binding UUID: {binding_uuid}")

                binding_uuids.add(binding_uuid)

    def test_default_role_binding_uuid_for(self):
        self.assertEqual(
            self.tenant_mapping.default_role_binding_uuid,
            self.tenant_mapping.default_role_binding_uuid_for(DefaultAccessType.USER, Scope.DEFAULT),
        )

        self.assertEqual(
            self.tenant_mapping.root_scope_default_role_binding_uuid,
            self.tenant_mapping.default_role_binding_uuid_for(DefaultAccessType.USER, Scope.ROOT),
        )

        self.assertEqual(
            self.tenant_mapping.tenant_scope_default_role_binding_uuid,
            self.tenant_mapping.default_role_binding_uuid_for(DefaultAccessType.USER, Scope.TENANT),
        )

        self.assertEqual(
            self.tenant_mapping.default_admin_role_binding_uuid,
            self.tenant_mapping.default_role_binding_uuid_for(DefaultAccessType.ADMIN, Scope.DEFAULT),
        )

        self.assertEqual(
            self.tenant_mapping.root_scope_default_admin_role_binding_uuid,
            self.tenant_mapping.default_role_binding_uuid_for(DefaultAccessType.ADMIN, Scope.ROOT),
        )

        self.assertEqual(
            self.tenant_mapping.tenant_scope_default_admin_role_binding_uuid,
            self.tenant_mapping.default_role_binding_uuid_for(DefaultAccessType.ADMIN, Scope.TENANT),
        )

    def test_source_for_role_binding_id(self):
        for default_access_type in DefaultAccessType:
            for scope in Scope:
                self.assertEqual(
                    (default_access_type, scope),
                    self.tenant_mapping.source_for_role_binding_id(
                        self.tenant_mapping.default_role_binding_uuid_for(default_access_type, scope)
                    ),
                )

                self.assertEqual(
                    (default_access_type, scope),
                    self.tenant_mapping.source_for_role_binding_id(
                        str(self.tenant_mapping.default_role_binding_uuid_for(default_access_type, scope))
                    ),
                )

        self.assertIsNone(self.tenant_mapping.source_for_role_binding_id(uuid.uuid4()))
