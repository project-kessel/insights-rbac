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
from unittest import TestCase

from management.role.model import BindingMapping, Role
from management.role.v2_model import CustomRoleV2, RoleV2
from management.role_binding.model import RoleBinding


def _assert_binding_mapping_consistent(test: TestCase, binding: RoleBinding, mapping: BindingMapping):
    test.assertEqual(str(binding.uuid), mapping.mappings["id"])
    test.assertEqual(binding.role.v1_source, mapping.role)
    test.assertEqual(binding.resource_type, mapping.resource_type_name)
    test.assertEqual(binding.resource_id, mapping.resource_id)
    test.assertEqual(str(binding.role.uuid), mapping.mappings["role"]["id"])

    test.assertIsNotNone(mapping.v2_role, f"BindingMapping {mapping.id} should have v2_role set")
    test.assertEqual(mapping.v2_role_id, binding.role_id, "v2_role FK should match RoleBinding's role")
    test.assertEqual(
        str(mapping.v2_role.uuid),
        mapping.mappings["role"]["id"],
        "v2_role UUID should match mappings JSON role id",
    )

    v1_groups = set(mapping.mappings["groups"])
    v2_groups = set(str(g.uuid) for g in binding.bound_groups())

    test.assertEqual(v1_groups, v2_groups)

    v1_entries = mapping.mappings["users"].items()
    v2_entries = [(e.source, e.principal.user_id) for e in binding.principal_entries.all()]

    test.assertCountEqual(v1_entries, v2_entries)


def _assert_v2_names(test: TestCase, v1_role: Role):
    v2_roles = list(CustomRoleV2.objects.filter(v1_source=v1_role))

    test.assertCountEqual(
        [r.name for r in v2_roles],
        [f"{v1_role.display_name} ({i + 1})" for i in range(len(v2_roles))],
        "Expected V2 role names to be based on V1 role's display name and numbered sequentially",
    )


def _assert_role_mappings_consistent(test: TestCase, role: CustomRoleV2, mappings: list[BindingMapping]):
    expected_permissions = role.v2_permissions()

    test.assertGreater(len(mappings), 0, "Expected V2 role to be used in at least one BindingMapping.")

    for binding_mapping in mappings:
        test.assertFalse(binding_mapping.mappings["role"]["is_system"])

        test.assertEqual(
            expected_permissions,
            set(binding_mapping.mappings["role"]["permissions"]),
            f"Mismatched permissions between RoleV2 {role.uuid} and BindingMapping {binding_mapping.id} "
            f"({binding_mapping.mappings["id"]})",
        )


def _assert_v1_v2_system_roles_locally_consistent(test: TestCase):
    binding_mappings_by_uuid = {
        str(m.mappings["id"]): m
        for m in BindingMapping.objects.filter(role__system=True).prefetch_related("role").select_related("v2_role")
    }

    role_bindings_by_uuid = {str(b.uuid): b for b in RoleBinding.objects.filter(role__type=RoleV2.Types.SEEDED)}

    # We should have the same UUIDs for both BindingMappings and RoleBindings.
    test.assertCountEqual(role_bindings_by_uuid.keys(), binding_mappings_by_uuid.keys())

    for binding_uuid, binding in role_bindings_by_uuid.items():
        _assert_binding_mapping_consistent(test, binding, binding_mappings_by_uuid[binding_uuid])


def _assert_v1_v2_custom_roles_locally_consistent(test: TestCase):
    binding_mappings_by_uuid = {
        str(m.mappings["id"]): m
        for m in BindingMapping.objects.filter(role__system=False).prefetch_related("role").select_related("v2_role")
    }

    role_bindings_by_uuid = {str(b.uuid): b for b in RoleBinding.objects.filter(role__type=RoleV2.Types.CUSTOM)}

    # We should have the same UUIDs for both BindingMappings and RoleBindings.
    test.assertCountEqual(role_bindings_by_uuid.keys(), binding_mappings_by_uuid.keys())

    for role in Role.objects.filter(system=False):
        _assert_v2_names(test, role)

    for role in CustomRoleV2.objects.all():
        test.assertIsNotNone(role.v1_source, "All custom roles created through dual-write should have a v1_source.")

        _assert_role_mappings_consistent(
            test,
            role,
            [m for m in binding_mappings_by_uuid.values() if m.mappings["role"]["id"] == str(role.uuid)],
        )

    for binding_uuid, binding in role_bindings_by_uuid.items():
        mapping = binding_mappings_by_uuid[binding_uuid]

        _assert_binding_mapping_consistent(test, binding, mapping)

        # Principals should not be bound to custom roles.
        test.assertEqual(0, len(mapping.mappings["users"]))


def assert_v1_v2_locally_consistent(test: TestCase):
    _assert_v1_v2_custom_roles_locally_consistent(test)
    _assert_v1_v2_system_roles_locally_consistent(test)
