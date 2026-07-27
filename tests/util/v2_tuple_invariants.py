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
from typing import Optional
from unittest import TestCase

from management.group.platform import GlobalPolicyIdService, DefaultGroupNotAvailableError
from management.permission.scope_service import Scope
from management.role.platform import platform_v2_role_uuid_for
from management.role.v2_model import CustomRoleV2, RoleV2, SeededRoleV2
from management.role_binding.model import RoleBinding
from management.tenant_mapping.model import DefaultAccessType
from migration_tool.in_memory_tuples import (
    InMemoryTuples,
    all_of,
    resource,
    subject,
    relation,
    subject_type,
    resource_type,
)


def _assert_role_tuples_consistent(test: TestCase, tuples: InMemoryTuples, role: RoleV2):
    expected_permissions = role.v2_permissions()

    tuple_permissions = set(
        t.relation
        for t in tuples.find_tuples(
            all_of(
                resource("rbac", "role", str(role.uuid)),
                subject("rbac", "principal", "*"),
            )
        )
    )

    test.assertEqual(
        expected_permissions,
        tuple_permissions,
        f"Database and relations permissions do not match for role {role.uuid}",
    )


def _assert_binding_tuples_consistent(test: TestCase, tuples: InMemoryTuples, binding: RoleBinding):
    resource_predicate = resource("rbac", "role_binding", str(binding.uuid))

    role_tuples = tuples.find_tuples(
        all_of(
            resource_predicate,
            relation("role"),
            subject_type("rbac", "role"),
        )
    )

    test.assertEqual(1, len(role_tuples), f"Missing role relation for binding: {str(binding.uuid)}")
    test.assertEqual(str(binding.role.uuid), role_tuples.only.subject.subject.id)

    resource_tuples = tuples.find_tuples(
        all_of(
            relation("binding"),
            subject(
                "rbac",
                "role_binding",
                str(binding.uuid),
            ),
        )
    )

    test.assertEqual(1, len(resource_tuples))
    test.assertEqual(binding.resource_type, resource_tuples.only.resource.type.name)
    test.assertEqual(binding.resource_id, resource_tuples.only.resource.id)

    db_groups = set(str(g.uuid) for g in binding.bound_groups())

    tuple_groups = set(
        t.subject.subject.id
        for t in tuples.find_tuples(
            all_of(
                resource_predicate,
                relation("subject"),
                subject_type("rbac", "group", "member"),
            )
        )
    )

    test.assertEqual(
        db_groups,
        tuple_groups,
        f"Database and relations groups do not match for role binding {binding.uuid} "
        f"for role ({binding.role.uuid})",
    )

    db_principals = set(p.principal_resource_id() for p in binding.bound_principals())
    test.assertNotIn(None, db_principals)

    tuple_principals = set(
        t.subject.subject.id
        for t in tuples.find_tuples(
            all_of(
                resource_predicate,
                relation("subject"),
                subject_type("rbac", "principal"),
            )
        )
    )

    test.assertCountEqual(
        db_principals,
        tuple_principals,
        f"Database and relations principals do not match for role binding {binding.uuid} "
        f"for role ({binding.role.uuid})",
    )


def _assert_no_phantom_roles(test: TestCase, tuples: InMemoryTuples):
    """Check that there are no roles referenced in tuples that do not exist in the database."""
    # We are not consistent about actually creating PlatformRoleV2 models, so use the pre-configured values without
    # consulting the database.
    policy_service = GlobalPolicyIdService()

    def _uuid_for(access_type: DefaultAccessType, scope: Scope) -> Optional[str]:
        try:
            return str(platform_v2_role_uuid_for(access_type, scope, policy_service=policy_service))
        except DefaultGroupNotAvailableError:
            return None

    platform_role_uuids = {
        r
        for r in (_uuid_for(access_type, scope) for access_type in DefaultAccessType for scope in Scope)
        if r is not None
    }

    for v2_role_uuid in {
        *(t.resource.id for t in tuples.find_tuples(resource_type("rbac", "role"))),
        *(t.subject.subject.id for t in tuples.find_tuples(subject_type("rbac", "role"))),
    }:
        if SeededRoleV2.objects.filter(uuid=v2_role_uuid).exists():
            # We are not interested in system roles here.
            continue

        if v2_role_uuid in platform_role_uuids:
            # We are not interested in platform roles here.
            continue

        v2_role = CustomRoleV2.objects.filter(uuid=v2_role_uuid).first()
        test.assertIsNotNone(v2_role, f"V2 Role with UUID {v2_role_uuid} exists in tuples but not in the database.")


def _assert_v2_custom_role_tuples_consistent(test: TestCase, tuples: InMemoryTuples):
    for role in CustomRoleV2.objects.all():
        _assert_role_tuples_consistent(test, tuples, role)

    for binding in RoleBinding.objects.filter(role__type=RoleV2.Types.CUSTOM):
        _assert_binding_tuples_consistent(test, tuples, binding)

    _assert_no_phantom_roles(test, tuples)


def _assert_v2_system_role_tuples_consistent(test: TestCase, tuples: InMemoryTuples):
    for binding in RoleBinding.objects.filter(role__type=RoleV2.Types.SEEDED):
        _assert_binding_tuples_consistent(test, tuples, binding)


def assert_v2_tuples_consistent(test: TestCase, tuples: InMemoryTuples):
    _assert_v2_custom_role_tuples_consistent(test, tuples)
    _assert_v2_system_role_tuples_consistent(test, tuples)
