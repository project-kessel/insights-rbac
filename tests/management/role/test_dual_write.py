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
"""Test tuple changes for RBAC operations."""

from datetime import timedelta

from django.utils import timezone
from typing import Callable, Optional, Tuple, Iterable
from django.test import TestCase, override_settings
from django.db.models import Q
from django.conf import settings

from management.group.definer import seed_group, set_system_flag_before_update
from management.group.model import Group
from management.group.platform import GlobalPolicyIdService
from management.group.relation_api_dual_write_group_handler import (
    RelationApiDualWriteGroupHandler,
)
from management.models import Workspace
from management.permission.model import Permission
from management.permission.scope_service import Scope
from management.policy.model import Policy
from management.principal.model import Principal
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.relation_replicator import (
    DualWriteException,
    RelationReplicator,
    ReplicationEventType,
)
from management.role.model import (
    Access,
    BindingMapping,
    ResourceDefinition,
    Role,
    SourceKey,
)
from management.role.platform import platform_v2_role_uuid_for
from management.role.relation_api_dual_write_handler import (
    RelationApiDualWriteHandler,
    SeedingRelationApiDualWriteHandler,
)
from management.role.v2_model import CustomRoleV2, RoleV2, SeededRoleV2
from management.role_binding.model import RoleBinding, RoleBindingPrincipal
from management.role_binding.service import RoleBindingService
from management.tenant_mapping.model import TenantMapping, DefaultAccessType
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from management.tenant_service.tenant_service import BootstrappedTenant
from management.tenant_service.v2 import V2TenantBootstrapService
from management.tenant_service.tenant_service import TenantBootstrapService
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    all_of,
    one_of,
    relation,
    resource,
    resource_id,
    resource_type,
    subject,
    subject_type,
)
from migration_tool.utils import create_relationship

from api.cross_access.model import CrossAccountRequest
from api.cross_access.relation_api_dual_write_cross_access_handler import (
    RelationApiDualWriteCrossAccessHandler,
)
from api.cross_access.util import create_cross_principal
from api.models import Tenant, User
from unittest.mock import patch

from migration_tool.models import V2boundresource
from tests.util import assert_v1_v2_locally_consistent, assert_v1_v2_tuples_fully_consistent
from tests.v2_util import seed_v2_role_from_v1, bootstrap_tenant_for_v2_test


@override_settings(REPLICATION_TO_RELATION_ENABLED=True)
class DualWriteTestCase(TestCase):
    """
    Base TestCase for testing dual write logic.

    Use "given" methods to set up state like users would. Use "expect" methods to assert the state of the system.

    "Given" methods are treated like distinct transactions, which each replicate tuples via dual write.
    """

    def setUp(self):
        """Set up the dual write tests."""
        super().setUp()
        self.tuples = InMemoryTuples()
        self.fixture = RbacFixture()

        bootstrapped_tenant = self.fixture.new_tenant(org_id="1234567")
        self.tenant = bootstrapped_tenant.tenant
        self.test_tenant = self.tenant

        ws_1 = Workspace.objects.create(
            tenant=self.tenant, parent=bootstrapped_tenant.default_workspace, name="DualWriteTestCase Workspace 1"
        )

        ws_2 = Workspace.objects.create(
            tenant=self.tenant, parent=bootstrapped_tenant.default_workspace, name="DualWriteTestCase Workspace 2"
        )

        ws_3 = Workspace.objects.create(
            tenant=self.tenant, parent=bootstrapped_tenant.default_workspace, name="DualWriteTestCase Workspace 3"
        )

        self.ws_1_id = str(ws_1.id)
        self.ws_2_id = str(ws_2.id)
        self.ws_3_id = str(ws_3.id)

    def switch_to_new_tenant(self, name: str, org_id: str) -> Tenant:
        """Switch to a new tenant with the given name and org_id."""
        tenant = self.fixture.new_tenant(org_id=org_id).tenant
        self.tenant = tenant
        return tenant

    def switch_tenant(self, tenant: Tenant):
        self.tenant = tenant

    def restore_test_tenant(self):
        self.tenant = self.test_tenant

    def default_workspace(self, tenant: Optional[Tenant] = None) -> str:
        """Return the default workspace ID."""
        tenant = tenant if tenant is not None else self.tenant
        default = Workspace.objects.default(tenant=tenant)
        return str(default.id)

    def root_workspace(self, tenant: Optional[Tenant] = None) -> str:
        """Return the root workspace ID."""
        tenant = tenant if tenant is not None else self.tenant
        root = Workspace.objects.root(tenant=tenant)
        return str(root.id)

    def default_workspace_resource(self, tenant: Optional[Tenant] = None) -> V2boundresource:
        return V2boundresource(("rbac", "workspace"), self.default_workspace(tenant))

    def root_workspace_resource(self, tenant: Optional[Tenant] = None) -> V2boundresource:
        return V2boundresource(("rbac", "workspace"), self.root_workspace(tenant))

    def tenant_resource(self, tenant: Optional[Tenant] = None) -> V2boundresource:
        if tenant is None:
            tenant = self.tenant

        return V2boundresource.for_model(tenant)

    def dual_write_handler(
        self, role: Role, event_type: ReplicationEventType, replicator: Optional[RelationReplicator] = None
    ) -> RelationApiDualWriteHandler:
        """Create a RelationApiDualWriteHandler for the given role and event type."""
        return RelationApiDualWriteHandler(role, event_type, replicator=self._get_replicator(replicator))

    def _get_replicator(self, replicator: Optional[RelationReplicator]) -> RelationReplicator:
        return replicator if replicator is not None else InMemoryRelationReplicator(self.tuples)

    def given_v1_system_role(
        self,
        name: str,
        permissions: list[str],
        platform_default=False,
        admin_default=False,
    ) -> Role:
        """Create a new system role with the given ID and permissions."""
        role = self.fixture.new_system_role(
            name=name,
            permissions=permissions,
            platform_default=platform_default,
            admin_default=admin_default,
        )
        dual_write_handler = SeedingRelationApiDualWriteHandler(
            role=role, replicator=InMemoryRelationReplicator(self.tuples)
        )
        dual_write_handler.replicate_new_system_role()
        return role

    def given_v1_role(self, name: str, default: list[str], **kwargs: list[str]) -> Role:
        """Create a new custom role with the given ID and workspace permissions."""
        role = self.fixture.new_custom_role(
            name=name,
            tenant=self.tenant,
            resource_access=self.fixture.workspace_access(default, **kwargs),
        )
        dual_write = self.dual_write_handler(role, ReplicationEventType.CREATE_CUSTOM_ROLE)
        dual_write.replicate_new_or_updated_role(role)
        return role

    def _test_workspace_perms(self, ws_1: Optional[list[str]], ws_2: Optional[list[str]], ws_3: Optional[list[str]]):
        workspace_perms = {}

        for ws_id, requested_perms in [(self.ws_1_id, ws_1), (self.ws_2_id, ws_2), (self.ws_3_id, ws_3)]:
            if requested_perms is not None:
                workspace_perms[ws_id] = requested_perms

        return workspace_perms

    def given_v1_role_on_test_workspaces(
        self,
        name: str,
        default: list[str] = [],
        ws_1: Optional[list[str]] = None,
        ws_2: Optional[list[str]] = None,
        ws_3: Optional[list[str]] = None,
    ) -> Role:
        """Create a new custom role with the given ID and permissions for the pre-created test workspaces."""

        return self.given_v1_role(
            name=name, default=default, **self._test_workspace_perms(ws_1=ws_1, ws_2=ws_2, ws_3=ws_3)
        )

    def given_update_to_v1_role(
        self, role: Role, default: list[str] = [], replicator: Optional[RelationReplicator] = None, **kwargs: list[str]
    ):
        """Update the given role with the given workspace permissions."""
        dual_write = self.dual_write_handler(role, ReplicationEventType.UPDATE_CUSTOM_ROLE, replicator=replicator)
        dual_write.prepare_for_update()
        role = self.fixture.update_custom_role(
            role,
            resource_access=self.fixture.workspace_access(default, **kwargs),
        )
        dual_write.replicate_new_or_updated_role(role)
        return role

    def given_update_to_v1_role_on_test_workspaces(
        self,
        role: Role,
        default: list[str] = [],
        ws_1: Optional[list[str]] = None,
        ws_2: Optional[list[str]] = None,
        ws_3: Optional[list[str]] = None,
    ) -> Role:
        """Create a new custom role with the given ID and permissions for the pre-created test workspaces."""
        return self.given_update_to_v1_role(
            role=role, default=default, **self._test_workspace_perms(ws_1=ws_1, ws_2=ws_2, ws_3=ws_3)
        )

    def given_v1_role_removed(self, role: Role, replicator: Optional[RelationReplicator] = None):
        """Remove the given custom role."""
        dual_write = self.dual_write_handler(role, ReplicationEventType.DELETE_CUSTOM_ROLE, replicator=replicator)
        dual_write.prepare_for_update()
        role.delete()
        dual_write.replicate_deleted_role()

    def given_group(
        self, name: str, users: list[str] = [], service_accounts: list[str] = []
    ) -> Tuple[Group, list[Principal]]:
        """Create a new group with the given name and users."""
        group, principals = self.fixture.new_group(
            name=name,
            users=users,
            service_accounts=service_accounts,
            tenant=self.tenant,
        )
        dual_write = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.CREATE_GROUP,
            replicator=InMemoryRelationReplicator(self.tuples),
        )
        dual_write.replicate_new_principals(principals)
        return group, principals

    def given_custom_default_group(self, replicator: Optional[RelationReplicator] = None) -> Group:
        with patch("management.role.relation_api_dual_write_handler.OutboxReplicator.replicate") as replicate:
            replicate.side_effect = self._get_replicator(replicator).replicate
            return self.fixture.custom_default_group(self.tenant)

    def given_car(self, user_id: str, roles: list[Role]):
        create_cross_principal(user_id, target_org=self.tenant.org_id)
        car = self.fixture.new_car(self.tenant, user_id)
        car.roles.add(*roles)
        dual_write_handler = RelationApiDualWriteCrossAccessHandler(
            car,
            ReplicationEventType.APPROVE_CROSS_ACCOUNT_REQUEST,
            replicator=InMemoryRelationReplicator(self.tuples),
        )
        dual_write_handler.generate_relations_to_add_roles(car.roles.all())
        dual_write_handler.replicate()
        return car

    def given_car_expired(self, car: CrossAccountRequest, replicator: Optional[RelationReplicator] = None):
        dual_write_handler = RelationApiDualWriteCrossAccessHandler(
            car,
            ReplicationEventType.EXPIRE_CROSS_ACCOUNT_REQUEST,
            replicator=self._get_replicator(replicator),
        )
        dual_write_handler.generate_relations_to_remove_roles(car.roles.all())
        dual_write_handler.replicate()

    def given_additional_group_members(
        self, group: Group, users: list[str] = [], service_accounts: list[str] = []
    ) -> list[Principal]:
        """Add users to the given group."""
        principals = self.fixture.add_members_to_group(group, users, service_accounts, group.tenant)
        dual_write = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.ADD_PRINCIPALS_TO_GROUP,
            replicator=InMemoryRelationReplicator(self.tuples),
        )
        dual_write.replicate_new_principals(principals)
        return principals

    def given_removed_group_members(
        self, group: Group, users: list[str] = [], service_accounts: list[str] = []
    ) -> list[Principal]:
        """Remove users from the given group."""
        principals = self.fixture.remove_members_from_group(group, users, service_accounts, group.tenant)
        dual_write = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.REMOVE_PRINCIPALS_FROM_GROUP,
            replicator=InMemoryRelationReplicator(self.tuples),
        )
        dual_write.replicate_removed_principals(principals)
        return principals

    def given_roles_assigned_to_group(
        self, group: Group, roles: list[Role], replicator: Optional[RelationReplicator] = None
    ) -> Policy:
        """Assign the [roles] to the [group]."""
        assert roles, "Roles must not be empty"
        dual_write_handler = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.ASSIGN_ROLE,
            replicator=self._get_replicator(replicator),
        )
        policy: Policy
        for role in roles:
            policy = self.fixture.add_role_to_group(role, group)
            dual_write_handler.generate_relations_reset_roles([role])
        dual_write_handler.replicate()
        return policy

    def given_roles_unassigned_from_group(
        self, group: Group, roles: list[Role], replicator: Optional[RelationReplicator] = None
    ) -> Policy:
        """Unassign the [roles] to the [group]."""
        assert roles, "Roles must not be empty"
        policy = self.fixture.remove_role_from_group(roles[0], group)
        dual_write_handler = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.UNASSIGN_ROLE,
            replicator=self._get_replicator(replicator),
        )
        policy: Policy
        for role in roles:
            policy = self.fixture.remove_role_from_group(role, group)
            dual_write_handler.generate_relations_to_remove_roles([role])
        dual_write_handler.replicate()
        return policy

    def given_group_removed(self, group: Group, replicator: Optional[RelationReplicator] = None):
        """Remove the given group."""
        dual_write_handler = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.DELETE_GROUP,
            replicator=self._get_replicator(replicator),
        )
        roles = Role.objects.filter(policies__group=group)
        dual_write_handler.prepare_to_delete_group(roles)
        group.delete()
        dual_write_handler.replicate()

    def expect_1_v2_role_with_permissions(self, permissions: list[str]) -> str:
        """Assert there is a role matching the given permissions and return its ID."""
        return self.expect_v2_roles_with_permissions(1, permissions)[0]

    def expect_v2_roles_with_permissions(self, count: int, permissions: list[str]) -> list[str]:
        """Assert there is a role matching the given permissions and return its ID."""
        permission_tuples = self.tuples.find_tuples(lambda t: t.relation != "owner")
        roles, unmatched = permission_tuples.find_group_with_tuples(
            [
                all_of(
                    resource_type("rbac", "role"),
                    relation(permission.replace(":", "_")),
                )
                for permission in permissions
            ],
            group_by=lambda t: (
                t.resource.type.namespace,
                t.resource.type.name,
                t.resource.id,
            ),
            group_filter=lambda group: group[0] == "rbac" and group[1] == "role",
            require_full_match=True,
        )

        num_roles = len(roles)
        self.assertEqual(
            num_roles,
            count,
            f"Expected exactly {count} role(s) with permissions {permissions}, but got {num_roles}.\n"
            f"Matched roles: {roles}.\n"
            f"Unmatched roles: {unmatched}",
        )
        return [role[2] for role in roles.keys()]

    def expect_num_role_bindings(self, num: int):
        """Assert there are [num] role bindings."""
        role_bindings = self.tuples.find_tuples_grouped(
            subject_type("rbac", "role_binding"),
            group_by=lambda t: (
                t.resource.type.namespace,
                t.resource.type.name,
                t.resource.id,
            ),
        )
        num_role_bindings = len(role_bindings)
        self.assertEqual(
            num_role_bindings,
            num,
            f"Expected exactly {num} role bindings, but got {num_role_bindings}.\n" f"Role bindings: {role_bindings}",
        )

    def expect_role_bindings_to_resource(
        self,
        num: int,
        target: V2boundresource,
        for_v2_roles: list[str],
        for_groups: list[str],
        for_principals: list[str] = [],
    ):
        """Assert there are [num] role bindings with the given roles, groups, principals."""
        # Find all bindings for the given workspace
        resources = self.tuples.find_tuples_grouped(
            all_of(
                resource(target.resource_type[0], target.resource_type[1], target.resource_id),
                relation("binding"),
            ),
            group_by=lambda t: (
                t.resource.type.namespace,
                t.resource.type.name,
                t.resource.id,
            ),
        )

        # Now of those bound to the workspace, find bindings that bind the given roles and groups
        # (we expect only 1)
        role_bindings, unmatched = self.tuples.find_group_with_tuples(
            [
                all_of(
                    resource_type("rbac", "role_binding"),
                    one_of(*[resource_id(t.subject.subject.id) for _, tuples in resources.items() for t in tuples]),
                    relation("role"),
                    subject("rbac", "role", role_id),
                )
                for role_id in for_v2_roles
            ]
            + [
                all_of(
                    resource_type("rbac", "role_binding"),
                    relation("subject"),
                    subject("rbac", "group", group_id, "member"),
                )
                for group_id in for_groups
            ]
            + [
                all_of(
                    resource_type("rbac", "role_binding"),
                    relation("subject"),
                    subject("rbac", "principal", Principal.user_id_to_principal_resource_id(user_id)),
                )
                for user_id in for_principals
            ],
            group_by=lambda t: (
                t.resource.type.namespace,
                t.resource.type.name,
                t.resource.id,
            ),
            group_filter=lambda group: group[0] == "rbac" and group[1] == "role_binding",
            require_full_match=True,
        )

        num_role_bindings = len(role_bindings)
        self.assertEqual(
            num_role_bindings,
            num,
            f"Expected exactly {num} role binding{"s" if num != 1 else ""} against resource {target} "
            f"with roles {for_v2_roles} and groups {for_groups}, "
            f"but got {len(role_bindings)}.\n"
            f"Matched role bindings: {role_bindings}.\n"
            f"Unmatched role bindings: {unmatched}",
        )

    def expect_1_role_binding_to_workspace(
        self, workspace: str, for_v2_roles: list[str], for_groups: list[str], for_principals: list[str] = []
    ):
        """Assert there is a role binding with the given roles and groups."""
        self.expect_role_bindings_to_workspace(1, workspace, for_v2_roles, for_groups, for_principals)

    def expect_role_bindings_to_workspace(
        self, num: int, workspace: str, for_v2_roles: list[str], for_groups: list[str], for_principals: list[str] = []
    ):
        """Assert there are [num] role bindings for the given workspace with the given roles and groups."""
        self.expect_role_bindings_to_resource(
            num=num,
            target=V2boundresource(("rbac", "workspace"), workspace),
            for_v2_roles=for_v2_roles,
            for_groups=for_groups,
            for_principals=for_principals,
        )

    def expect_1_role_binding_to_tenant(
        self, org_id: str, for_v2_roles: list[str], for_groups: list[str], for_principals: list[str] = []
    ):
        """Assert there is a role binding for the given workspace with the given roles and groups."""
        self.expect_role_bindings_to_tenant(
            num=1, org_id=org_id, for_v2_roles=for_v2_roles, for_groups=for_groups, for_principals=for_principals
        )

    def expect_role_bindings_to_tenant(
        self, num: int, org_id: str, for_v2_roles: list[str], for_groups: list[str], for_principals: list[str] = []
    ):
        """Assert there is a role binding for the given tenant with the given roles and groups."""
        self.expect_role_bindings_to_resource(
            num=num,
            target=V2boundresource(("rbac", "tenant"), Tenant.org_id_to_tenant_resource_id(org_id)),
            for_v2_roles=for_v2_roles,
            for_groups=for_groups,
            for_principals=for_principals,
        )

    def expect_binding_present(self, target: V2boundresource, v2_role_id: str, group_id: str):
        """Assert that a role binding (and associated BindingMapping) exist for the given resource, role, and group."""
        self.expect_role_bindings_to_resource(
            num=1,
            target=target,
            for_v2_roles=[v2_role_id],
            for_groups=[group_id],
        )

        mapping = BindingMapping.objects.get(
            resource_type_namespace=target.resource_type[0],
            resource_type_name=target.resource_type[1],
            resource_id=target.resource_id,
            mappings__role__id=v2_role_id,
        )

        self.assertIn(group_id, mapping.mappings["groups"])
        self.assertIsNotNone(mapping.v2_role, "BindingMapping should have v2_role set during dual-write")
        self.assertEqual(str(mapping.v2_role.uuid), v2_role_id)

        binding = RoleBinding.objects.get(
            resource_type=target.resource_type[1],
            resource_id=target.resource_id,
            role__uuid=v2_role_id,
        )

        self.assertTrue(binding.bound_groups().filter(uuid=group_id).exists())

    def expect_binding_absent(self, target: V2boundresource, v2_role_id: str, group_id: str):
        """Assert that a role binding (and BindingMapping) do not exist for the given resource, role, and group."""
        self.expect_role_bindings_to_resource(
            num=0,
            target=target,
            for_v2_roles=[v2_role_id],
            for_groups=[group_id],
        )

        mappings = list(
            BindingMapping.objects.filter(
                resource_type_namespace=target.resource_type[0],
                resource_type_name=target.resource_type[1],
                resource_id=target.resource_id,
                mappings__role__id=v2_role_id,
            )
        )

        self.assertLessEqual(len(mappings), 1)

        if len(mappings) == 0:
            return

        mapping = mappings[0]
        self.assertNotIn(group_id, mapping.mappings["groups"])

    def expect_role_binding_groups(self, binding: RoleBinding, groups: Iterable[Group]):
        groups = set(groups)

        self.assertEqual(len(groups), binding.group_entries.count())
        self.assertCountEqual(groups, binding.bound_groups())

    def expect_role_binding_principals(self, binding: RoleBinding, entries: list[tuple[str, Principal]]):
        actual = [(e.source, e.principal) for e in binding.principal_entries.all()]
        self.assertCountEqual(entries, actual)


@override_settings(ROOT_SCOPE_PERMISSIONS="root:*:*", TENANT_SCOPE_PERMISSIONS="tenant:*:*")
class DualWriteGroupTestCase(DualWriteTestCase):
    """Test dual write logic for group modifications."""

    def tearDown(self):
        with self.subTest(msg="V2 consistency"):
            assert_v1_v2_locally_consistent(test=self)

        super().tearDown()

    def test_cannot_replicate_group_for_public_tenant(self):
        """Do not replicate group changes for the public tenant groups (system groups)."""
        platform_default, admin_default = seed_group()

        with self.assertRaises(DualWriteException):
            RelationApiDualWriteGroupHandler(platform_default, ReplicationEventType.CREATE_GROUP)

        with self.assertRaises(DualWriteException):
            RelationApiDualWriteGroupHandler(admin_default, ReplicationEventType.CREATE_GROUP)

    def test_create_group_tuples(self):
        """Create a group and add users to it."""
        group, principals = self.given_group("g1", ["u1", "u2"])
        tuples = self.tuples.find_tuples(all_of(resource("rbac", "group", group.uuid), relation("member")))
        self.assertEqual(len(tuples), 2)
        self.assertEqual(
            {t.subject.subject.id for t in tuples},
            {f"localhost/{p.user_id}" for p in principals},
        )

    def test_update_group_tuples(self):
        """Update a group by adding and removing users."""
        group, principals = self.given_group("g1", ["u1", "u2"])

        principals += self.given_additional_group_members(group, ["u3"])

        tuples = self.tuples.find_tuples(all_of(resource("rbac", "group", group.uuid), relation("member")))
        self.assertEqual(len(tuples), 3)
        self.assertEqual(
            {t.subject.subject.id for t in tuples},
            {f"localhost/{p.user_id}" for p in principals},
        )

        self.given_removed_group_members(group, ["u2"])
        principals = [p for p in principals if p.username != "u2"]

        tuples = self.tuples.find_tuples(all_of(resource("rbac", "group", group.uuid), relation("member")))
        self.assertEqual(len(tuples), 2)
        self.assertEqual(
            {t.subject.subject.id for t in tuples},
            {f"localhost/{p.user_id}" for p in principals},
        )

    def test_custom_roles_group_assignments_tuples(self):
        role_1 = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        role_2 = self.given_v1_role_on_test_workspaces(
            "r2",
            default=["app2:hosts:read", "inventory:systems:write"],
            ws_2=["app2:hosts:read", "inventory:systems:write"],
        )
        group, _ = self.given_group("g1", [])

        self.given_roles_assigned_to_group(group, roles=[role_1, role_2])

        mappings = BindingMapping.objects.filter(Q(role=role_1) | Q(role=role_2)).values_list("mappings", flat=True)

        tuples = self.tuples.find_tuples(
            all_of(
                resource_type("rbac", "role_binding"),
                relation("subject"),
                subject_type("rbac", "group", "member"),
            )
        )

        self.assertEqual(len(tuples), 4)
        for mapping in mappings:
            for group_from_mapping in mapping["groups"]:
                tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role_binding", mapping["id"]),
                        relation("subject"),
                        subject("rbac", "group", group_from_mapping, "member"),
                    )
                )
                self.assertEqual(len(tuples), 1)
                self.assertEqual(tuples.only.subject.subject.id, mapping["groups"][0])

        self.given_roles_unassigned_from_group(group, [role_1, role_2])

        mappings = BindingMapping.objects.filter(role=role_2).all()
        for m in mappings:
            self.assertEqual(m.mappings["groups"], [])

        tuples = self.tuples.find_tuples(
            all_of(
                resource_type("rbac", "role_binding"),
                relation("subject"),
                subject_type("rbac", "group"),
            )
        )

        self.assertEqual(len(tuples), 0)

    def test_system_role_mapping(self):
        role_test = self.given_v1_system_role(
            "rtest",
            permissions=["app1:hosts:read", "inventory:hosts:write"],
        )

        group, _ = self.given_group("g1", [])

        self.given_roles_assigned_to_group(group, roles=[role_test])

        # See the group bound.
        mappings = BindingMapping.objects.filter(role=role_test).get().mappings
        self.assertEqual(mappings["groups"], [str(group.uuid)])

    def test_adding_same_role_again_and_unassign_it_once(self):
        role_test = self.given_v1_role_on_test_workspaces(
            "rtest",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        group, _ = self.given_group("g1", [])

        self.given_roles_assigned_to_group(group, roles=[role_test])
        self.given_roles_assigned_to_group(group, roles=[role_test])

        # See the group bound.
        mappings = BindingMapping.objects.filter(role=role_test).first().mappings
        self.assertEqual(len(mappings["groups"]), 1)
        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", mappings["id"]),
                relation("subject"),
                subject("rbac", "group", str(group.uuid), "member"),
            )
        )
        self.assertEqual(len(tuples), 1)
        dual_write_handler = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.UNASSIGN_ROLE,
            replicator=InMemoryRelationReplicator(self.tuples),
        )
        dual_write_handler.generate_relations_to_remove_roles([role_test])
        dual_write_handler.replicate()

        mappings = BindingMapping.objects.filter(role=role_test).first().mappings
        self.assertEqual(len(mappings["groups"]), 0)
        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", mappings["id"]),
                relation("subject"),
                subject("rbac", "group", str(group.uuid), "member"),
            )
        )
        self.assertEqual(len(tuples), 0)

    def test_reset_called_multiple_times_when_role_added_multiple_times(self):
        role_test = self.given_v1_system_role(
            "rtest",
            permissions=["app1:hosts:read", "inventory:hosts:write"],
        )

        group, _ = self.given_group("g1", [])

        self.given_roles_assigned_to_group(group, roles=[role_test])
        self.given_roles_assigned_to_group(group, roles=[role_test])

        # Removing the RoleBinding for the system role should just result in it being recreated at the next assignment.
        self.assertEqual(1, RoleBinding.objects.filter(role__v1_source=role_test).count())
        RoleBinding.objects.filter(role__v1_source=role_test).delete()

        self.given_roles_assigned_to_group(group, roles=[role_test])

        # See the group bound.
        mappings = BindingMapping.objects.filter(role=role_test).first().mappings
        self.assertEqual(len(mappings["groups"]), 1)

        role_binding = RoleBinding.objects.filter(role__v1_source=role_test).get()
        self.expect_role_binding_groups(role_binding, {group})

        assert_v1_v2_locally_consistent(test=self)

        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", mappings["id"]),
                relation("subject"),
                subject("rbac", "group", str(group.uuid), "member"),
            )
        )
        self.assertEqual(len(tuples), 1)

        dual_write_handler = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.UNASSIGN_ROLE,
            replicator=InMemoryRelationReplicator(self.tuples),
        )
        dual_write_handler.generate_relations_reset_roles([role_test])
        dual_write_handler.generate_relations_reset_roles([role_test])
        dual_write_handler.generate_relations_reset_roles([role_test])
        dual_write_handler.replicate()

        mappings = BindingMapping.objects.filter(role=role_test).first().mappings
        self.assertEqual(len(mappings["groups"]), 1)

        role_binding = RoleBinding.objects.filter(role__v1_source=role_test).get()
        self.expect_role_binding_groups(role_binding, {group})

        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", mappings["id"]),
                relation("subject"),
                subject("rbac", "group", str(group.uuid), "member"),
            )
        )
        self.assertEqual(len(tuples), 1)

    def test_reset_when_role_added_multiple_times(self):
        role_test = self.given_v1_system_role(
            "rtest",
            permissions=["app1:hosts:read", "inventory:hosts:write"],
        )

        group, _ = self.given_group("g1", [])

        self.given_roles_assigned_to_group(group, roles=[role_test])

        binding_mapping: BindingMapping = BindingMapping.objects.filter(role=role_test).get()

        original_groups = binding_mapping.mappings["groups"]
        self.assertEqual(original_groups, [str(group.uuid)])

        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_mapping.mappings["id"]),
                relation("subject"),
                subject("rbac", "group", str(group.uuid), "member"),
            )
        )
        self.assertEqual(len(tuples), 1)

        # In previous versions of RBAC, a single group could have been stored twice in the same BindingMapping. We
        # need to ensure that calling generate_relations_reset_roles correctly handles this case and results in the
        # group being stored only once.
        binding_mapping.mappings["groups"] = original_groups + original_groups
        binding_mapping.save()

        dual_write_handler = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.UNASSIGN_ROLE,
            replicator=InMemoryRelationReplicator(self.tuples),
        )
        dual_write_handler.generate_relations_reset_roles([role_test])
        dual_write_handler.replicate()

        # Retrieve the updated mapping.
        binding_mapping = BindingMapping.objects.filter(role=role_test).get()
        self.assertEqual(binding_mapping.mappings["groups"], [str(group.uuid)])

        role_binding = RoleBinding.objects.filter(role__v1_source=role_test).get()
        self.expect_role_binding_groups(role_binding, {group})

        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_mapping.mappings["id"]),
                relation("subject"),
                subject("rbac", "group", str(group.uuid), "member"),
            )
        )
        self.assertEqual(len(tuples), 1)

    def test_delete_group_removes_group_from_role_bindings(self):
        # Add two groups to two roles
        role_1 = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        role_2 = self.given_v1_role_on_test_workspaces(
            "r2",
            default=["app2:hosts:read", "inventory:systems:write"],
            ws_2=["app2:hosts:read", "inventory:systems:write"],
        )

        group_1, _ = self.given_group("g1", ["u1"])
        group_2, _ = self.given_group("g2", ["u2"])

        self.given_roles_assigned_to_group(group_1, roles=[role_1, role_2])
        self.given_roles_assigned_to_group(group_2, roles=[role_1, role_2])

        # Delete the first group
        self.given_group_removed(group_1)

        # Assert that the group is removed from the role bindings by querying the role binding subject tuples
        tuples = self.tuples.find_tuples(
            all_of(
                resource_type("rbac", "role_binding"),
                relation("subject"),
                subject_type("rbac", "group", "member"),
            )
        )

        self.assertEqual({t.subject.subject.id for t in tuples}, {str(group_2.uuid)})
        # 2 resources * 2 roles * 1 group = 4 role bindings
        self.assertEqual(len(tuples), 4)

    def test_delete_group_removes_principals(self):
        group, _ = self.given_group("g1", ["u1", "u2"])

        self.given_group_removed(group)

        tuples = self.tuples.find_tuples(all_of(resource("rbac", "group", group.uuid)))
        self.assertEqual(len(tuples), 0)

    @override_settings(V2_BOOTSTRAP_TENANT=True)
    def test_custom_default_group_scopes(self):
        """Test that system roles assigned to a new custom default groups are bound in the appropriate scope."""
        for index, (permissions, target_for) in enumerate(
            [
                ([], lambda t: self.fixture.default_workspace(t)),
                (["default:resource:verb"], lambda t: self.fixture.default_workspace(t)),
                (["root:resource:verb"], lambda t: self.fixture.root_workspace(t)),
                (["default:resource:verb", "root:resource:verb"], lambda t: self.fixture.root_workspace(t)),
                (["tenant:resource:verb"], lambda t: t),
                (["default:resource:verb", "root:resource:verb", "tenant:resource:verb"], lambda t: t),
            ]
        ):
            with self.subTest(permissions=permissions):
                Role.objects.public_tenant_only().delete()
                platform_role = self.given_v1_system_role("platform", permissions, platform_default=True)

                seed_group()

                self.switch_to_new_tenant(name=f"test-{index}", org_id=f"test-{index}")
                custom_group = self.given_custom_default_group()

                target = V2boundresource.for_model(target_for(self.tenant))

                role_id = str(platform_role.uuid)
                group_id = str(custom_group.uuid)

                self.expect_binding_present(target, v2_role_id=role_id, group_id=group_id)

                self.given_roles_unassigned_from_group(custom_group, [platform_role])
                self.expect_binding_absent(target, v2_role_id=role_id, group_id=group_id)

                # Adding the role back later should still bind it in the target scope.
                self.given_roles_assigned_to_group(custom_group, [platform_role])
                self.expect_binding_present(target, v2_role_id=role_id, group_id=group_id)

    @override_settings(V2_BOOTSTRAP_TENANT=True)
    def test_remove_scope_changed(self):
        """Test that removing a role with changed scope from a group removes the old relations."""
        Role.objects.public_tenant_only().delete()
        platform_role = self.given_v1_system_role(
            "platform", ["app:resource:verb", "other_app:resource:verb"], platform_default=True
        )

        def make_platform_default_group() -> Group:
            # Assume that the custom default group was created while the role had root scope.
            with override_settings(ROOT_SCOPE_PERMISSIONS="app:*:*", TENANT_SCOPE_PERMISSIONS=""):
                seed_group()
                return self.given_custom_default_group()

        def make_ordinary_group() -> Group:
            with override_settings(ROOT_SCOPE_PERMISSIONS="app:*:*", TENANT_SCOPE_PERMISSIONS=""):
                group, _ = self.given_group(name="a group")
                self.given_roles_assigned_to_group(group, [platform_role])

                return group

        for kind, group_fn in [
            ("custom platform default", make_platform_default_group),
            ("ordinary", make_ordinary_group),
        ]:
            with self.subTest(kind=kind):
                group = group_fn()

                role_id = str(platform_role.uuid)
                group_id = str(group.uuid)

                default_workspace = self.default_workspace_resource()
                root_workspace = self.root_workspace_resource()
                tenant = self.tenant_resource()

                # A binding to the root workspace should have been created.
                self.expect_binding_present(root_workspace, v2_role_id=role_id, group_id=group_id)

                # No other bindings should have been created
                self.expect_binding_absent(default_workspace, v2_role_id=role_id, group_id=group_id)
                self.expect_binding_absent(tenant, v2_role_id=role_id, group_id=group_id)

                # Remove the role while it has tenant scope.
                with override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="app:*:*"):
                    self.given_roles_unassigned_from_group(group, [platform_role])

                # The root workspace binding should have been removed, and no other bindings should have been created.
                self.expect_binding_absent(default_workspace, v2_role_id=role_id, group_id=group_id)
                self.expect_binding_absent(root_workspace, v2_role_id=role_id, group_id=group_id)
                self.expect_binding_absent(tenant, v2_role_id=role_id, group_id=group_id)

    def test_custom_role_scope(self):
        """Test that custom roles with mixed TENANT+ROOT scope create per-scope bindings."""
        role = self.given_v1_role("a role", default=["root:resource:verb", "tenant:resource:verb"])

        group, _ = self.given_group(name="a group")
        self.given_roles_assigned_to_group(group, [role])

        # With mixed scopes, we now get two bindings: one at tenant, one at root workspace.
        mappings = BindingMapping.objects.filter(role=role)
        self.assertEqual(mappings.count(), 2)

        tenant_bm = mappings.get(resource_type_name="tenant")
        ws_bm = mappings.get(resource_type_name="workspace")
        group_id = str(group.uuid)

        tenant_v2_role_id = tenant_bm.mappings["role"]["id"]
        ws_v2_role_id = ws_bm.mappings["role"]["id"]

        self.expect_binding_present(
            self.tenant_resource(),
            v2_role_id=tenant_v2_role_id,
            group_id=group_id,
        )

        self.expect_binding_present(
            self.root_workspace_resource(),
            v2_role_id=ws_v2_role_id,
            group_id=group_id,
        )

        self.expect_binding_absent(
            self.default_workspace_resource(),
            v2_role_id=tenant_v2_role_id,
            group_id=group_id,
        )

    def test_delete_group_removes_role_binding_for_system_roles_if_last_group(self):
        role_1 = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        # Given system role
        role_2 = self.given_v1_system_role("r2", ["app2:hosts:read", "inventory:systems:write"])

        group_1, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(group_1, roles=[role_1, role_2])

        # Now remove the group
        self.given_group_removed(group_1)

        # Assert no role binding tuples exist for the system role
        tuples = self.tuples.find_tuples(
            all_of(
                resource_type("rbac", "role_binding"),
                relation("role"),
                subject("rbac", "role", str(role_2.uuid)),
            )
        )

        self.assertEqual(len(tuples), 0)

        # But the custom role remains
        tuples = self.tuples.find_tuples(
            all_of(
                resource_type("rbac", "role_binding"),
                relation("role"),
                subject_type("rbac", "role"),
            )
        )

        # 2 resources * 1 role
        self.assertEqual(len(tuples), 2)

    def test_delete_group_keeps_role_binding_for_system_roles_if_not_last_group(self):
        """Keep the role binding if it still has other groups assigned to it."""
        role_1 = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        # Given system role
        role_2 = self.given_v1_system_role("r2", ["app2:hosts:read", "inventory:systems:write"])

        group_1, _ = self.given_group("g1", ["u1"])
        group_2, _ = self.given_group("g2", ["u2"])

        self.given_roles_assigned_to_group(group_1, roles=[role_1, role_2])
        self.given_roles_assigned_to_group(group_2, roles=[role_1, role_2])

        # Delete the first group
        self.given_group_removed(group_1)

        # Check the system role binding remains
        tuples = self.tuples.find_tuples(
            all_of(
                resource_type("rbac", "role_binding"),
                relation("role"),
                subject("rbac", "role", str(role_2.uuid)),
            )
        )

        self.assertEqual(len(tuples), 1)

    def _assert_custom_default_group_before_bootstrap(
        self, do_boostrap: Callable[[V2TenantBootstrapService, Tenant], None]
    ):
        replicator = InMemoryRelationReplicator(self.tuples)
        bootstrap_service = V2TenantBootstrapService(replicator=replicator)

        self.switch_tenant(self.fixture.new_unbootstrapped_tenant(org_id="56789"))

        # Hypothesis: a custom default access group is created before the tenant is bootstrapped (e.g. before V2
        # existed).
        default_group = Group.objects.create(
            tenant=self.tenant,
            name="Custom default access",
            platform_default=True,
            system=False,
        )

        do_boostrap(bootstrap_service, self.tenant)

        mapping: TenantMapping = self.tenant.tenant_mapping
        policy_service = GlobalPolicyIdService()

        default_scope_role = str(
            platform_v2_role_uuid_for(DefaultAccessType.USER, Scope.DEFAULT, policy_service=policy_service)
        )
        root_scope_role = str(
            platform_v2_role_uuid_for(DefaultAccessType.USER, Scope.ROOT, policy_service=policy_service)
        )
        tenant_scope_role = str(
            platform_v2_role_uuid_for(DefaultAccessType.USER, Scope.TENANT, policy_service=policy_service)
        )

        def assert_default_bindings(num: int):
            self.expect_role_bindings_to_workspace(
                num=num,
                workspace=self.default_workspace(self.tenant),
                for_v2_roles=[default_scope_role],
                for_groups=[mapping.default_group_uuid],
            )

            self.expect_role_bindings_to_workspace(
                num=num,
                workspace=self.root_workspace(self.tenant),
                for_v2_roles=[root_scope_role],
                for_groups=[mapping.default_group_uuid],
            )

            self.expect_role_bindings_to_tenant(
                num=num,
                org_id=self.tenant.org_id,
                for_v2_roles=[tenant_scope_role],
                for_groups=[mapping.default_group_uuid],
            )

        # Ensure that we actually use the correct default group UUID.
        self.assertEqual(default_group.uuid, mapping.default_group_uuid)

        # After bootstrap, no default role binding should exist, since a custom default access group exists.
        assert_default_bindings(0)

        self.given_group_removed(default_group)

        # Once we have removed the default group, the default role binding should be restored.
        assert_default_bindings(1)

    def test_custom_default_group_before_single_bootstrap(self):
        def do_bootstrap(bootstrap_service: V2TenantBootstrapService, tenant: Tenant):
            bootstrap_service.bootstrap_tenant(tenant)

        self._assert_custom_default_group_before_bootstrap(do_bootstrap)

    def test_custom_default_group_before_bulk_bootstrap(self):
        def do_bootstrap(bootstrap_service: V2TenantBootstrapService, tenant: Tenant):
            # Import a single user in order to exercise the bulk import path.
            bootstrap_service.import_bulk_users(
                [
                    User(
                        username="test_user",
                        user_id=f"{self.tenant.org_id}-user",
                        org_id=self.tenant.org_id,
                        is_active=True,
                    )
                ]
            )

        self._assert_custom_default_group_before_bootstrap(do_bootstrap)

    def test_v2_group_members(self):
        ensure_v2_write_activated(self.tenant)

        group, principals = self.given_group("a group", ["p1"])

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", str(group.uuid)),
                    relation("member"),
                    subject("rbac", "principal", principals[0].principal_resource_id()),
                )
            ),
        )

    def test_v2_group_role_assignment(self):
        ensure_v2_write_activated(self.tenant)

        group, _ = self.given_group("a group", ["p1"])
        role = self.given_v1_system_role("a role", ["rbac:*:*"])

        # It is not meaningful to simply assign a role to a group in a V2 tenant.
        with self.assertRaises(RuntimeError):
            self.given_roles_assigned_to_group(group, [role])

    def test_v2_group_role_unassignment(self):
        group, _ = self.given_group("a group", ["p1"])
        role = self.given_v1_system_role("a role", ["rbac:*:*"])

        self.given_roles_assigned_to_group(group, [role])

        ensure_v2_write_activated(self.tenant)

        # It is not meaningful to simply assign a role to a group in a V2 tenant.
        with self.assertRaises(RuntimeError):
            self.given_roles_unassigned_from_group(group, [role])


class DualWriteSystemRolesTestCase(DualWriteTestCase):
    """Test dual write logic for system roles."""

    def test_system_role_grants_access_to_default_workspace(self):
        """Create role binding only when system role is bound to group."""
        role = self.given_v1_system_role("r1", ["app1:hosts:read", "inventory:hosts:write"])
        group, _ = self.given_group("g1", ["u1", "u2"])

        self.expect_num_role_bindings(0)

        self.given_roles_assigned_to_group(group, roles=[role])

        id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(), for_v2_roles=[id], for_groups=[str(group.uuid)]
        )
        self.expect_num_role_bindings(1)

    def test_unassign_system_role_removes_role_binding_if_unassigned(self):
        """Remove role binding when system role is unbound from group."""
        role = self.given_v1_system_role("r1", ["app1:hosts:read", "inventory:hosts:write"])
        group, _ = self.given_group("g1", ["u1", "u2"])

        self.given_roles_assigned_to_group(group, roles=[role])

        id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(), for_v2_roles=[id], for_groups=[str(group.uuid)]
        )

        self.given_roles_unassigned_from_group(group, roles=[role])
        self.expect_num_role_bindings(0)

    def test_unassign_system_role_keeps_role_binding_if_still_assigned(self):
        """Keep the role binding if it still has other groups assigned to it."""
        role = self.given_v1_system_role("r1", ["app1:hosts:read", "inventory:hosts:write"])
        g1, _ = self.given_group("g1", ["u1", "u2"])
        g2, _ = self.given_group("g2", ["u1", "u2"])

        self.expect_num_role_bindings(0)

        self.given_roles_assigned_to_group(g1, roles=[role])
        self.given_roles_assigned_to_group(g2, roles=[role])

        id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[id],
            for_groups=[str(g1.uuid), str(g2.uuid)],
        )

        self.given_roles_unassigned_from_group(g1, roles=[role])

        self.expect_1_role_binding_to_workspace(self.default_workspace(), for_v2_roles=[id], for_groups=[str(g2.uuid)])

    def test_assignment_is_tenant_specific(self):
        """System role assignments are tenant-specific despite using the same role."""
        role = self.given_v1_system_role("r1", ["app1:hosts:read", "inventory:hosts:write"])
        g1, _ = self.given_group("g1", ["u1", "u2"])
        self.given_roles_assigned_to_group(g1, roles=[role])

        t2 = self.switch_to_new_tenant("tenant2", "7654321")
        g2, _ = self.given_group("g2", ["u3", "u4"])
        self.given_roles_assigned_to_group(g2, roles=[role])

        id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])

        self.expect_1_role_binding_to_workspace(
            self.default_workspace(self.test_tenant),
            for_v2_roles=[id],
            for_groups=[str(g1.uuid)],
        )
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(t2), for_v2_roles=[id], for_groups=[str(g2.uuid)]
        )

    def test_unassign_role_is_tenant_specific(self):
        """System role unassignments are tenant-specific despite using the same role."""
        role = self.given_v1_system_role("r1", ["app1:hosts:read", "inventory:hosts:write"])
        g1, _ = self.given_group("g1", ["u1", "u2"])
        self.given_roles_assigned_to_group(g1, roles=[role])

        t2 = self.switch_to_new_tenant("tenant2", "7654321")
        g2, _ = self.given_group("g2", ["u3", "u4"])
        self.given_roles_assigned_to_group(g2, roles=[role])

        self.given_roles_unassigned_from_group(g1, roles=[role])

        id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])

        self.expect_role_bindings_to_workspace(
            0,
            self.default_workspace(self.test_tenant),
            for_v2_roles=[id],
            for_groups=[str(g1.uuid)],
        )
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(t2), for_v2_roles=[id], for_groups=[str(g2.uuid)]
        )

    def test_updating_system_role(self):
        platform_default_group, admin_default_group = seed_group()
        platform_default = str(platform_default_group.policies.get().uuid)
        admin_default = str(admin_default_group.policies.get().uuid)

        role = self.given_v1_system_role(
            "r1",
            ["app1:hosts:read", "inventory:hosts:write"],
            platform_default=True,
            admin_default=True,
        )

        # check if relations exist in replicator.
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 4)

        parents = [
            rel.resource.id for rel in tuples if rel.relation == "child" and rel.subject.subject.id == str(role.uuid)
        ]
        self.assertSetEqual(set([admin_default, platform_default]), set(parents))

        dual_write_handler = SeedingRelationApiDualWriteHandler(
            role=role, replicator=InMemoryRelationReplicator(self.tuples)
        )
        dual_write_handler.prepare_for_update()
        role.admin_default = False
        role = self.fixture.update_custom_role(
            role,
            resource_access=self.fixture.workspace_access(default=["inventory:hosts:write"]),
        )
        dual_write_handler.replicate_update_system_role()

        # check if only 2 relations exists in replicator.
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 2)
        parents = [
            rel.resource.id for rel in tuples if rel.relation == "child" and rel.subject.subject.id == str(role.uuid)
        ]
        self.assertSetEqual(set([platform_default]), set(parents))

        # ensure no relations exist in replicator.
        dual_write_handler = SeedingRelationApiDualWriteHandler(
            role=role, replicator=InMemoryRelationReplicator(self.tuples)
        )
        dual_write_handler.prepare_for_update()
        role.platform_default = False
        role = self.fixture.update_custom_role(
            role,
            resource_access=self.fixture.workspace_access(default=[]),
        )
        dual_write_handler.replicate_update_system_role()

        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 0)

    def test_delete_system_role(self):
        platform_default_group, admin_default_group = seed_group()
        platform_default = str(platform_default_group.policies.get().uuid)
        admin_default = str(admin_default_group.policies.get().uuid)

        role = self.given_v1_system_role(
            "d_r1",
            ["app1:hosts:read", "inventory:hosts:write"],
            platform_default=True,
            admin_default=True,
        )

        # check if relations exist in replicator.
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 4)
        parents = [
            rel.resource.id for rel in tuples if rel.relation == "child" and rel.subject.subject.id == str(role.uuid)
        ]
        self.assertSetEqual(set([admin_default, platform_default]), set(parents))

        dual_write_handler = SeedingRelationApiDualWriteHandler(
            role, replicator=InMemoryRelationReplicator(self.tuples)
        )
        dual_write_handler.replicate_deleted_system_role()

        # check if relations do not exist in replicator.
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 0)

        role = self.given_v1_system_role("d_r2", [], platform_default=True)

        # Check that it was created as platform default
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 1)
        parents = [
            rel.resource.id for rel in tuples if rel.relation == "child" and rel.subject.subject.id == str(role.uuid)
        ]
        self.assertSetEqual(set([platform_default]), set(parents))

        # Delete system role
        dual_write_handler = SeedingRelationApiDualWriteHandler(
            role, replicator=InMemoryRelationReplicator(self.tuples)
        )
        dual_write_handler.replicate_deleted_system_role()

        # Check if relations do not exist in replicator.
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 0)

        role = self.given_v1_system_role("d_r3", [], admin_default=True)

        # Check that it was created as platform default
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 1)
        parents = [
            rel.resource.id for rel in tuples if rel.relation == "child" and rel.subject.subject.id == str(role.uuid)
        ]
        self.assertSetEqual(set([admin_default]), set(parents))

        # Delete system role
        dual_write_handler = SeedingRelationApiDualWriteHandler(
            role, replicator=InMemoryRelationReplicator(self.tuples)
        )
        dual_write_handler.replicate_deleted_system_role()

        # Check if relations do not exist in replicator.
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 0)

        # create role with no relations
        role = self.given_v1_system_role("d_r4", [])

        # ensure no relations exist in replicator.
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 0)

        # delete system role
        dual_write_handler = SeedingRelationApiDualWriteHandler(
            role, replicator=InMemoryRelationReplicator(self.tuples)
        )
        dual_write_handler.replicate_deleted_system_role()

        # check if relations do not exist in replicator.
        tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(tuples), 0)

    def test_updating_platform_role_scope_transitions(self):
        """Test platform role scope transitions: ROOT->TENANT, DEFAULT->TENANT, and DEFAULT->ROOT."""

        # Subtest 1: ROOT to TENANT transition
        with override_settings(
            ROOT_SCOPE_PERMISSIONS="inventory:*:*",
            TENANT_SCOPE_PERMISSIONS="app1:*:*",
            SYSTEM_DEFAULT_TENANT_ROLE_UUID="3c9e6f1a-8b2d-4e5c-9a7f-1d3b5c8e2a4f",
            SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID="5e8a2c4f-9d1b-4c7e-8f3a-6d2b9c1e5a7f",
        ):
            with self.subTest(transition="ROOT to TENANT"):
                platform_tenant_uuid = settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID
                platform_root_uuid = settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_role_root_to_tenant",
                    permissions=["inventory:*:*"],
                    platform_default=True,
                )

                # Verify initial ROOT parent relationship
                initial_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_root_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(initial_tuples), 1)

                # Update to TENANT scope
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role=role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.prepare_for_update()
                role.admin_default = False
                role = self.fixture.update_custom_role(
                    role,
                    resource_access=self.fixture.workspace_access(
                        tenant_scope_permissions=["app1:organization:admin"]
                    ),
                )
                dual_write_handler.replicate_update_system_role()

                # Verify TENANT parent relationship exists and ROOT is deleted
                updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_tenant_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(updated_tuples), 1)
                deleted_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_root_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(deleted_tuples), 0)

        # Subtest 2: DEFAULT to TENANT transition
        with override_settings(
            TENANT_SCOPE_PERMISSIONS="app1:*:*",
            SYSTEM_DEFAULT_TENANT_ROLE_UUID="3c9e6f1a-8b2d-4e5c-9a7f-1d3b5c8e2a4f",
        ):
            with self.subTest(transition="DEFAULT to TENANT"):
                platform_default_group, _ = seed_group()
                platform_default_uuid = str(platform_default_group.policies.get().uuid)
                platform_tenant_uuid = settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_role_default_to_tenant",
                    permissions=["inventory:hosts:read"],
                    platform_default=True,
                )

                # Verify initial DEFAULT parent relationship
                initial_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_default_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(initial_tuples), 1)

                # Update to TENANT scope
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role=role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.prepare_for_update()
                role = self.fixture.update_custom_role(
                    role,
                    resource_access=self.fixture.workspace_access(tenant_scope_permissions=["app1:*:*"]),
                )
                dual_write_handler.replicate_update_system_role()

                # Verify TENANT parent relationship exists and DEFAULT is deleted
                updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_tenant_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(updated_tuples), 1)
                deleted_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_default_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(deleted_tuples), 0)

        # Subtest 3: DEFAULT to ROOT transition
        with override_settings(
            ROOT_SCOPE_PERMISSIONS="inventory:*:*",
            SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID="5e8a2c4f-9d1b-4c7e-8f3a-6d2b9c1e5a7f",
        ):
            with self.subTest(transition="DEFAULT to ROOT"):
                platform_default_group, _ = seed_group()
                platform_default_uuid = str(platform_default_group.policies.get().uuid)
                platform_root_uuid = settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_role_default_to_root",
                    permissions=["app1:hosts:read"],
                    platform_default=True,
                )

                # Verify initial DEFAULT parent relationship
                initial_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_default_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(initial_tuples), 1)

                # Update to ROOT scope
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role=role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.prepare_for_update()
                role = self.fixture.update_custom_role(
                    role,
                    resource_access=self.fixture.workspace_access(root_scope_permission=["inventory:*:*"]),
                )
                dual_write_handler.replicate_update_system_role()

                # Verify ROOT parent relationship exists and DEFAULT is deleted
                updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_root_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(updated_tuples), 1)
                deleted_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_default_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(deleted_tuples), 0)

    def test_updating_admin_role_scope_transitions(self):
        """Test admin role scope transitions: ROOT->TENANT, DEFAULT->TENANT, and DEFAULT->ROOT."""

        # Subtest 1: ROOT to TENANT transition
        with override_settings(
            ROOT_SCOPE_PERMISSIONS="inventory:*:*",
            TENANT_SCOPE_PERMISSIONS="app1:*:*",
            SYSTEM_ADMIN_TENANT_ROLE_UUID="a7f3c8b2-1d4e-4f9a-8c6d-2b5e7a9f1c3d",
            SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID="9b4c7e1f-3a5d-4f8c-9e2a-7c1d5b8f3a6e",
        ):
            with self.subTest(transition="ROOT to TENANT"):
                admin_root_uuid = settings.SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID
                admin_tenant_uuid = settings.SYSTEM_ADMIN_TENANT_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_admin_role_root_to_tenant",
                    permissions=["inventory:*:*"],
                    admin_default=True,
                )

                # Verify initial ROOT parent relationship
                initial_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_root_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(initial_tuples), 1)

                # Update to TENANT scope
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role=role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.prepare_for_update()
                role = self.fixture.update_custom_role(
                    role,
                    resource_access=self.fixture.workspace_access(
                        tenant_scope_permissions=["app1:organization:admin"]
                    ),
                )
                dual_write_handler.replicate_update_system_role()

                # Verify TENANT parent relationship exists and ROOT is deleted
                updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_tenant_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(updated_tuples), 1)
                deleted_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_root_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(deleted_tuples), 0)

        # Subtest 2: DEFAULT to TENANT transition
        with override_settings(
            TENANT_SCOPE_PERMISSIONS="app1:*:*",
            SYSTEM_ADMIN_TENANT_ROLE_UUID="a7f3c8b2-1d4e-4f9a-8c6d-2b5e7a9f1c3d",
        ):
            with self.subTest(transition="DEFAULT to TENANT"):
                _, admin_default_group = seed_group()
                admin_default_uuid = str(admin_default_group.policies.get().uuid)
                admin_tenant_uuid = settings.SYSTEM_ADMIN_TENANT_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_admin_role_default_to_tenant",
                    permissions=["inventory:hosts:read"],
                    admin_default=True,
                )

                # Verify initial DEFAULT parent relationship
                initial_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_default_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(initial_tuples), 1)

                # Update to TENANT scope
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role=role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.prepare_for_update()
                role = self.fixture.update_custom_role(
                    role,
                    resource_access=self.fixture.workspace_access(tenant_scope_permissions=["app1:*:*"]),
                )
                dual_write_handler.replicate_update_system_role()

                # Verify TENANT parent relationship exists and DEFAULT is deleted
                updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_tenant_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(updated_tuples), 1)
                deleted_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_default_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(deleted_tuples), 0)

        # Subtest 3: DEFAULT to ROOT transition
        with override_settings(
            ROOT_SCOPE_PERMISSIONS="inventory:*:*",
            SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID="9b4c7e1f-3a5d-4f8c-9e2a-7c1d5b8f3a6e",
        ):
            with self.subTest(transition="DEFAULT to ROOT"):
                _, admin_default_group = seed_group()
                admin_default_uuid = str(admin_default_group.policies.get().uuid)
                admin_root_uuid = settings.SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_admin_role_default_to_root",
                    permissions=["app1:hosts:read"],
                    admin_default=True,
                )

                # Verify initial DEFAULT parent relationship
                initial_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_default_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(initial_tuples), 1)

                # Update to ROOT scope
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role=role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.prepare_for_update()
                role = self.fixture.update_custom_role(
                    role,
                    resource_access=self.fixture.workspace_access(root_scope_permission=["inventory:*:*"]),
                )
                dual_write_handler.replicate_update_system_role()

                # Verify ROOT parent relationship exists and DEFAULT is deleted
                updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_root_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(updated_tuples), 1)
                deleted_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_default_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(deleted_tuples), 0)

    def test_dual_platform_admin_role_operations(self):
        """Test operations on roles with both platform_default=True and admin_default=True."""

        # Subtest 1: DEFAULT to ROOT transition for dual role
        with override_settings(
            ROOT_SCOPE_PERMISSIONS="inventory:*:*",
            SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID="5e8a2c4f-9d1b-4c7e-8f3a-6d2b9c1e5a7f",
            SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID="9b4c7e1f-3a5d-4f8c-9e2a-7c1d5b8f3a6e",
        ):
            with self.subTest(operation="DEFAULT to ROOT"):
                platform_root_uuid = settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID
                admin_root_uuid = settings.SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_dual_role_default_to_root",
                    ["app1:hosts:read"],
                    platform_default=True,
                    admin_default=True,
                )

                # Verify initial state - check tuples specific to this role
                role_specific_tuples = self.tuples.find_tuples(
                    all_of(
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                # Should have 2 child relationships (platform default + admin default parents)
                self.assertEqual(len(role_specific_tuples), 2)

                # Update to ROOT scope
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role=role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.prepare_for_update()
                role = self.fixture.update_custom_role(
                    role,
                    resource_access=self.fixture.workspace_access(root_scope_permission=["inventory:*:*"]),
                )
                dual_write_handler.replicate_update_system_role()

                # Verify both platform and admin ROOT relationships exist
                platform_updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_root_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(platform_updated_tuples), 1)

                admin_updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_root_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(admin_updated_tuples), 1)

        # Subtest 2: DEFAULT to TENANT transition for dual role
        with override_settings(
            TENANT_SCOPE_PERMISSIONS="app1:*:*",
            SYSTEM_ADMIN_TENANT_ROLE_UUID="a7f3c8b2-1d4e-4f9a-8c6d-2b5e7a9f1c3d",
            SYSTEM_DEFAULT_TENANT_ROLE_UUID="3c9e6f1a-8b2d-4e5c-9a7f-1d3b5c8e2a4f",
        ):
            with self.subTest(operation="DEFAULT to TENANT"):
                platform_tenant_uuid = settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID
                admin_tenant_uuid = settings.SYSTEM_ADMIN_TENANT_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_dual_role_default_to_tenant",
                    ["app1:hosts:read"],
                    platform_default=True,
                    admin_default=True,
                )

                # Verify initial state - check tuples specific to this role
                role_specific_tuples = self.tuples.find_tuples(
                    all_of(
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                # Should have 2 child relationships (platform default + admin default parents)
                self.assertEqual(len(role_specific_tuples), 2)

                # Update to TENANT scope
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role=role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.prepare_for_update()
                role = self.fixture.update_custom_role(
                    role,
                    resource_access=self.fixture.workspace_access(root_scope_permission=["app1:*:*"]),
                )
                dual_write_handler.replicate_update_system_role()

                # Verify both platform and admin TENANT relationships exist
                platform_updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_tenant_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(platform_updated_tuples), 1)

                admin_updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_tenant_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(admin_updated_tuples), 1)

        # Subtest 3: Role deletion removes both relationships
        with override_settings(
            TENANT_SCOPE_PERMISSIONS="app1:*:*",
            SYSTEM_ADMIN_TENANT_ROLE_UUID="a7f3c8b2-1d4e-4f9a-8c6d-2b5e7a9f1c3d",
            SYSTEM_DEFAULT_TENANT_ROLE_UUID="3c9e6f1a-8b2d-4e5c-9a7f-1d3b5c8e2a4f",
        ):
            with self.subTest(operation="DELETE removes relationships"):
                platform_tenant_uuid = settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID
                admin_tenant_uuid = settings.SYSTEM_ADMIN_TENANT_ROLE_UUID

                role = self.given_v1_system_role(
                    "test_dual_role_deletion",
                    ["app1:hosts:read"],
                    platform_default=True,
                    admin_default=True,
                )

                # Verify initial state - check tuples specific to this role
                role_specific_tuples = self.tuples.find_tuples(
                    all_of(
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                # Should have 2 child relationships (platform default + admin default parents)
                self.assertEqual(len(role_specific_tuples), 2)

                # Delete system role
                dual_write_handler = SeedingRelationApiDualWriteHandler(
                    role, replicator=InMemoryRelationReplicator(self.tuples)
                )
                dual_write_handler.replicate_deleted_system_role()

                # Verify both platform and admin relationships are removed
                platform_updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", platform_tenant_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(platform_updated_tuples), 0)

                admin_updated_tuples = self.tuples.find_tuples(
                    all_of(
                        resource("rbac", "role", admin_tenant_uuid),
                        relation("child"),
                        subject("rbac", "role", str(role.uuid)),
                    )
                )
                self.assertEqual(len(admin_updated_tuples), 0)

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="inventory:*:*",
        TENANT_SCOPE_PERMISSIONS="app1:*:*",
        SYSTEM_ADMIN_TENANT_ROLE_UUID="a7f3c8b2-1d4e-4f9a-8c6d-2b5e7a9f1c3d",
        SYSTEM_DEFAULT_TENANT_ROLE_UUID="3c9e6f1a-8b2d-4e5c-9a7f-1d3b5c8e2a4f",
        SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID="5e8a2c4f-9d1b-4c7e-8f3a-6d2b9c1e5a7f",
        SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID="9b4c7e1f-3a5d-4f8c-9e2a-7c1d5b8f3a6e",
    )
    def test_system_role_incorrect_scope_removal(self):
        """Test that when a role has incorrect parent relationships (e.g., ROOT scope bound to TENANT parent), those incorrect relationships are removed."""
        platform_tenant_uuid = settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID
        admin_tenant_uuid = settings.SYSTEM_ADMIN_TENANT_ROLE_UUID
        platform_root_uuid = settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID
        admin_root_uuid = settings.SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID

        # Create a role with ROOT scope permissions
        role = self.given_v1_system_role("r1", ["inventory:*:*"], platform_default=True, admin_default=True)

        # Verify it's correctly bound to ROOT parent relationships
        initial_tuples = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(initial_tuples), 3)

        platform_root_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_root_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(platform_root_tuples), 1)

        admin_root_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role", admin_root_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(admin_root_tuples), 1)

        # Manually inject incorrect TENANT scope relationships (simulating a data inconsistency)
        # This simulates the scenario where the role's scope is ROOT but it's incorrectly bound to TENANT parents
        platform_incorrect_relationship = create_relationship(
            resource_name=("rbac", "role"),
            resource_id=platform_tenant_uuid,
            relation="child",
            subject_name=("rbac", "role"),
            subject_id=str(role.uuid),
            subject_relation=None,
        )
        self.tuples.add(platform_incorrect_relationship)
        admin_incorrect_relationship = create_relationship(
            resource_name=("rbac", "role"),
            resource_id=admin_tenant_uuid,
            relation="child",
            subject_name=("rbac", "role"),
            subject_id=str(role.uuid),
            subject_relation=None,
        )
        self.tuples.add(admin_incorrect_relationship)

        # Verify incorrect relationships now exist
        all_tuples_before_delete = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(all_tuples_before_delete), 5)  # 3 correct + 2 incorrect

        # Delete system role - this should remove ALL parent relationships including incorrect ones
        dual_write_handler = SeedingRelationApiDualWriteHandler(
            role, replicator=InMemoryRelationReplicator(self.tuples)
        )
        dual_write_handler.replicate_deleted_system_role()

        all_tuples_after_delete = self.tuples.find_tuples(predicate=resource_type("rbac", "role"))
        self.assertEqual(len(all_tuples_after_delete), 0)
        # Verify all relationships are removed (both correct ROOT and incorrect TENANT)

        platform_root_tuples_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_root_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(platform_root_tuples_after), 0)

        admin_root_tuples_after = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role", admin_root_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(admin_root_tuples_after), 0)

    @override_settings(
        ROOT_SCOPE_PERMISSIONS="inventory:*:*,app1:*:*",
        TENANT_SCOPE_PERMISSIONS="catalog:*:*",
        SYSTEM_DEFAULT_TENANT_ROLE_UUID="3c9e6f1a-8b2d-4e5c-9a7f-1d3b5c8e2a4f",
        SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID="5e8a2c4f-9d1b-4c7e-8f3a-6d2b9c1e5a7f",
    )
    def test_updating_platform_role_scope_changes_due_to_settings_change(self):
        """Test that updating a platform default role's parent relationship when scope changes due to settings change, not permission change."""
        platform_default_group, admin_default_group = seed_group()
        platform_default_uuid = str(platform_default_group.policies.get().uuid)
        platform_tenant_uuid = settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID
        platform_root_uuid = settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID

        # Create a platform default role with app1:*:* which initially is in ROOT scope
        role = self.given_v1_system_role(
            "test_scope_change_via_settings",
            permissions=["app1:*:*"],
            platform_default=True,
        )

        # Initially, parent should be the platform root role since app1:*:* is in ROOT_SCOPE_PERMISSIONS
        initial_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_root_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(initial_tuples), 1)

        # Now simulate settings change where ROOT_SCOPE_PERMISSIONS no longer includes app1:*:*
        # We override settings to remove app1:*:* from ROOT_SCOPE_PERMISSIONS
        with override_settings(
            ROOT_SCOPE_PERMISSIONS="inventory:*:*",  # app1:*:* removed
            TENANT_SCOPE_PERMISSIONS="catalog:*:*",  # app1:*:* not here either, so it falls back to DEFAULT
        ):
            # Prepare for update (but permissions stay the same, only settings changed)
            dual_write_handler = SeedingRelationApiDualWriteHandler(
                role=role, replicator=InMemoryRelationReplicator(self.tuples)
            )
            dual_write_handler.prepare_for_update()

            # Call replicate_update_system_role without changing permissions
            # Just refresh the role to simulate re-processing with new settings
            role = self.fixture.update_custom_role(
                role,
                resource_access=self.fixture.workspace_access(default=["app1:*:*"]),
            )
            dual_write_handler.replicate_update_system_role()

            # After update with new settings, parent should be the platform default role
            # because app1:*:* no longer matches ROOT or TENANT scope
            updated_tuples = self.tuples.find_tuples(
                all_of(
                    resource("rbac", "role", platform_default_uuid),
                    relation("child"),
                    subject("rbac", "role", str(role.uuid)),
                )
            )
            self.assertEqual(len(updated_tuples), 1)

            # Verify the old ROOT relationship is deleted
            deleted_tuple = self.tuples.find_tuples(
                all_of(
                    resource("rbac", "role", platform_root_uuid),
                    relation("child"),
                    subject("rbac", "role", str(role.uuid)),
                )
            )
            self.assertEqual(len(deleted_tuple), 0)


class DualWriteCustomRolesTestCase(DualWriteTestCase):
    """Test dual write logic when we are working with custom roles."""

    def tearDown(self):
        # Run this in a subtest so as to not cover up any earlier failures.
        with self.subTest(msg="V2 consistency"):
            self._expect_v2_consistent()

        super().tearDown()

    def _expect_v2_consistent(self):
        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

    def test_simple_role(self):
        """Test the simplest meaningful role: a single permission bound to the default resource."""
        role = self.given_v1_role("r1", default=["inventory:hosts:read"])

        group, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(group, roles=[role])

        v2_role_uuid = self.expect_1_v2_role_with_permissions(["inventory:hosts:read"])

        self.expect_1_role_binding_to_workspace(
            self.default_workspace(), for_v2_roles=[v2_role_uuid], for_groups=[str(group.uuid)]
        )

    def _test_non_default_resources(self, workspaces: list[str], permissions: list[str]):
        role = self.given_v1_role("r1", default=[], **dict.fromkeys(workspaces, permissions))

        group, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(group, roles=[role])

        v2_role_uuid = self.expect_1_v2_role_with_permissions(permissions)

        for workspace_id in workspaces:
            self.expect_1_role_binding_to_workspace(
                workspace_id, for_v2_roles=[v2_role_uuid], for_groups=[str(group.uuid)]
            )

        # The default workspace should have no role binding, since it has no permissions.
        self.assertEqual(
            0,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", self.default_workspace()),
                    relation("binding"),
                )
            ),
        )

        self._expect_v2_consistent()

        self.given_v1_role_removed(role)
        self.given_group_removed(group)
        self._expect_v2_consistent()

    def test_non_default_resource_only(self):
        """Test a role with access to a specific resource only."""
        for workspaces in [[self.ws_1_id], [self.ws_1_id, self.ws_2_id, self.ws_3_id]]:
            for permissions in [
                ["inventory:hosts:read"],
                ["inventory:hosts:read", "inventory:hosts:write", "inventory:hosts:delete"],
            ]:
                with self.subTest(workspaces=workspaces, permissions=permissions):
                    self._test_non_default_resources(workspaces=workspaces, permissions=permissions)

    def test_many_different_permissions(self):
        """Test creating a role with many different sets of permissions."""
        role = self.given_v1_role_on_test_workspaces(
            "r1", default=["inventory:hosts:read"], ws_1=["inventory:hosts:write"], ws_2=["inventory:hosts:delete"]
        )

        group, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(group, roles=[role])

        for workspace, permissions in [
            (self.default_workspace(), ["inventory:hosts:read"]),
            (self.ws_1_id, ["inventory:hosts:write"]),
            (self.ws_2_id, ["inventory:hosts:delete"]),
        ]:
            role = self.expect_1_v2_role_with_permissions(permissions)
            self.expect_1_role_binding_to_workspace(workspace, for_v2_roles=[role], for_groups=[str(group.uuid)])

    def test_partial_overlap(self):
        """Test creating a role where some, but not all, workspaces share the same permissions."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["inventory:hosts:read"],
            ws_1=["inventory:hosts:read"],
            ws_2=["inventory:hosts:write"],
            ws_3=["inventory:hosts:write"],
        )

        group, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(group, roles=[role])

        read_role = self.expect_1_v2_role_with_permissions(["inventory:hosts:read"])
        write_role = self.expect_1_v2_role_with_permissions(["inventory:hosts:write"])

        self.expect_1_role_binding_to_workspace(
            self.default_workspace(), for_v2_roles=[read_role], for_groups=[str(group.uuid)]
        )

        self.expect_1_role_binding_to_workspace(self.ws_1_id, for_v2_roles=[read_role], for_groups=[str(group.uuid)])

        self.expect_1_role_binding_to_workspace(self.ws_2_id, for_v2_roles=[write_role], for_groups=[str(group.uuid)])
        self.expect_1_role_binding_to_workspace(self.ws_3_id, for_v2_roles=[write_role], for_groups=[str(group.uuid)])

    def test_role_with_same_default_and_resource_permission_reuses_same_v2_role(self):
        """With same resource permissions (when one of those is the default workspace), reuse the same v2 role."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        group, _ = self.given_group("g1", ["u1", "u2"])
        self.given_roles_assigned_to_group(group, roles=[role])

        id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(), for_v2_roles=[id], for_groups=[str(group.uuid)]
        )
        self.expect_1_role_binding_to_workspace(self.ws_2_id, for_v2_roles=[id], for_groups=[str(group.uuid)])

    def test_add_permissions_to_role(self):
        """Modify the role in place when adding permissions."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        self.given_update_to_v1_role_on_test_workspaces(
            role,
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write", "app2:hosts:read"],
        )

        group, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(group, [role])

        role_for_default = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])
        role_for_ws_2 = self.expect_1_v2_role_with_permissions(
            ["app1:hosts:read", "inventory:hosts:write", "app2:hosts:read"]
        )

        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[role_for_default],
            for_groups=[str(group.uuid)],
        )
        self.expect_1_role_binding_to_workspace(
            self.ws_2_id, for_v2_roles=[role_for_ws_2], for_groups=[str(group.uuid)]
        )

    def test_remove_permissions_from_role(self):
        """Modify the role in place when removing permissions."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        self.given_update_to_v1_role_on_test_workspaces(
            role,
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read"],
        )

        group, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(group, [role])

        role_for_default = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])
        role_for_ws_2 = self.expect_1_v2_role_with_permissions(["app1:hosts:read"])

        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[role_for_default],
            for_groups=[str(group.uuid)],
        )
        self.expect_1_role_binding_to_workspace(
            self.ws_2_id, for_v2_roles=[role_for_ws_2], for_groups=[str(group.uuid)]
        )

    def test_remove_permissions_from_role_back_to_original(self):
        """Modify the role in place when removing permissions, consolidating roles."""
        """Modify the role in place when adding permissions."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        self.given_update_to_v1_role_on_test_workspaces(
            role,
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write", "app2:hosts:read"],
        )

        self.given_update_to_v1_role_on_test_workspaces(
            role,
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])
        self.expect_1_role_binding_to_workspace(self.default_workspace(), for_v2_roles=[id], for_groups=[])
        self.expect_1_role_binding_to_workspace(self.ws_2_id, for_v2_roles=[id], for_groups=[])

    def test_add_resource_uses_existing_groups(self):
        """New bindings get existing groups."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        g1, _ = self.given_group("g2", ["u2"])
        g2, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(g1, roles=[role])
        self.given_roles_assigned_to_group(g2, roles=[role])

        self.given_update_to_v1_role_on_test_workspaces(
            role,
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
            ws_3=["app1:hosts:read", "inventory:hosts:write"],
        )

        role = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])

        self.expect_1_role_binding_to_workspace(
            self.ws_3_id, for_v2_roles=[role], for_groups=[str(g1.uuid), str(g2.uuid)]
        )

    def test_delete_role(self):
        """Delete the role and its bindings when deleting a custom role."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        v2_role_uuid = self.expect_1_v2_role_with_permissions(permissions=["app1:hosts:read", "inventory:hosts:write"])

        role_binding_uuids = {
            t.resource.id
            for t in self.tuples.find_tuples(
                all_of(
                    resource_type("rbac", "role_binding"),
                    relation("role"),
                    subject("rbac", "role", v2_role_uuid),
                )
            )
        }

        # The specifics of these bindings are tested elsewhere; we just want to check that there's actually something
        # to remove.
        self.assertGreater(len(role_binding_uuids), 0)

        self.given_v1_role_removed(role)

        self.assertEqual(0, self.tuples.count_tuples(resource("rbac", "role", v2_role_uuid)))
        self.assertEqual(0, self.tuples.count_tuples(subject("rbac", "role", v2_role_uuid)))

        for role_binding_uuid in role_binding_uuids:
            self.assertEqual(0, self.tuples.count_tuples(resource("rbac", "role_binding", role_binding_uuid)))
            self.assertEqual(0, self.tuples.count_tuples(subject("rbac", "role_binding", role_binding_uuid)))

    @patch("management.role.relation_api_dual_write_handler.OutboxReplicator.replicate")
    def test_create_role_with_empty_access(self, replicate_mock):
        """Create a role and its bindings when creating a custom role."""
        self.given_v1_role("role_without_access", [])
        replicate_mock.assert_not_called()

    def test_remove_resource_removes_role_binding(self):
        """Remove the role binding when removing the resource from attribute filter."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        self.given_update_to_v1_role_on_test_workspaces(
            role,
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        role = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])

        self.expect_num_role_bindings(1)
        self.expect_1_role_binding_to_workspace(self.ws_2_id, for_v2_roles=[role], for_groups=[])

    def test_two_roles_with_same_resource_permissions_create_two_v2_roles(self):
        """Create two v2 roles when two roles have the same resource permissions across different resources."""
        self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        self.given_v1_role_on_test_workspaces(
            "r2",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        roles = self.expect_v2_roles_with_permissions(2, ["app1:hosts:read", "inventory:hosts:write"])

        self.expect_1_role_binding_to_workspace(self.default_workspace(), for_v2_roles=[roles[0]], for_groups=[])
        self.expect_1_role_binding_to_workspace(self.default_workspace(), for_v2_roles=[roles[1]], for_groups=[])
        self.expect_1_role_binding_to_workspace(self.ws_2_id, for_v2_roles=[roles[0]], for_groups=[])
        self.expect_1_role_binding_to_workspace(self.ws_2_id, for_v2_roles=[roles[1]], for_groups=[])

    def test_unassigned_role_keeps_role_binding(self):
        """Unassigning a role from a group does not remove the role binding."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
            ws_2=["app1:hosts:read", "inventory:hosts:write"],
        )

        group, _ = self.given_group("g1", ["u1"])
        self.given_roles_assigned_to_group(group, roles=[role])

        id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])

        self.expect_1_role_binding_to_workspace(
            self.default_workspace(), for_v2_roles=[id], for_groups=[str(group.uuid)]
        )

        self.given_roles_unassigned_from_group(group, roles=[role])

        self.expect_1_role_binding_to_workspace(self.default_workspace(), for_v2_roles=[id], for_groups=[])

    def test_assign_group_no_rolebindngs(self):
        """Test assigning a group to a custom role before RoleBindings have been created for it."""
        role = self.given_v1_role_on_test_workspaces(
            "r1",
            default=["app1:hosts:read", "inventory:hosts:write"],
        )

        group, _ = self.given_group("a group")

        # Emulate RoleBindings not having been created for the role.
        RoleBinding.objects.all().delete()

        self.given_roles_assigned_to_group(group, [role])

        v2_id = self.expect_1_v2_role_with_permissions(["app1:hosts:read", "inventory:hosts:write"])

        self.expect_1_role_binding_to_workspace(
            self.default_workspace(), for_v2_roles=[v2_id], for_groups=[str(group.uuid)]
        )

        # The role should have been automatically migrated when assigning the group.
        self.assertTrue(RoleBinding.objects.filter(role__v1_source=role).exists())
        self._expect_v2_consistent()

    def test_change_role_scope(self):
        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            role = self.given_v1_role(name="test role", default=["app:resource:verb"])

        group, _ = self.given_group("a group")
        group_id = str(group.uuid)

        # This can change, so we need to recompute it after updating the role.
        def get_v2_role_id() -> str:
            return BindingMapping.objects.get(role=role).mappings["role"]["id"]

        default_workspace = self.default_workspace_resource()
        root_workspace = self.root_workspace_resource()
        tenant = self.tenant_resource()

        # Scope at the time of assignment should not matter.
        with self.settings(ROOT_SCOPE_PERMISSIONS="app:*:*", TENANT_SCOPE_PERMISSIONS=""):
            self.given_roles_assigned_to_group(group, [role])
            v2_role_id = get_v2_role_id()

        self.expect_binding_present(default_workspace, v2_role_id=v2_role_id, group_id=group_id)
        self.expect_binding_absent(root_workspace, v2_role_id=v2_role_id, group_id=group_id)
        self.expect_binding_absent(tenant, v2_role_id=v2_role_id, group_id=group_id)
        self._expect_v2_consistent()

        # Put the existing permission into root scope, then try removing it.
        with self.settings(ROOT_SCOPE_PERMISSIONS="app:*:*", TENANT_SCOPE_PERMISSIONS=""):
            self.given_update_to_v1_role(role, default=[])

        # At this point, we expect no role bindings to exist (and thus there is no V2 role ID to check).
        # We expect to have successfully removed the existing role binding despite the scope of the role having
        # changed since it was created.
        self.assertFalse(BindingMapping.objects.filter(role=role).exists())
        self.assertEqual(0, len(self.tuples.find_tuples(relation("binding"))))
        self._expect_v2_consistent()

        # Re-adding the permission (but now in root scope) should bind the role in the root workspace.
        with self.settings(ROOT_SCOPE_PERMISSIONS="app:*:*", TENANT_SCOPE_PERMISSIONS=""):
            self.given_update_to_v1_role(role, default=["app:resource:verb"])
            v2_role_id = get_v2_role_id()

        self.expect_binding_absent(default_workspace, v2_role_id=v2_role_id, group_id=group_id)
        self.expect_binding_present(root_workspace, v2_role_id=v2_role_id, group_id=group_id)
        self.expect_binding_absent(tenant, v2_role_id=v2_role_id, group_id=group_id)
        self._expect_v2_consistent()

        # Adding a new permission in tenant scope should split the role: tenant + root bindings.
        with self.settings(ROOT_SCOPE_PERMISSIONS="app:*:*", TENANT_SCOPE_PERMISSIONS="other_app:*:*"):
            self.given_update_to_v1_role(role, default=["app:resource:verb", "other_app:resource:verb"])

        # With the split, we now have two V2 roles (one per scope group).
        mappings = BindingMapping.objects.filter(role=role)
        tenant_bm = mappings.filter(resource_type_name="tenant")
        ws_bm = mappings.filter(resource_type_name="workspace")
        self.assertEqual(tenant_bm.count(), 1, "Should have tenant binding for other_app")
        self.assertEqual(ws_bm.count(), 1, "Should have workspace binding for app")

        tenant_v2_role_id = tenant_bm.first().mappings["role"]["id"]
        ws_v2_role_id = ws_bm.first().mappings["role"]["id"]

        self.expect_binding_present(tenant, v2_role_id=tenant_v2_role_id, group_id=group_id)
        self.expect_binding_present(root_workspace, v2_role_id=ws_v2_role_id, group_id=group_id)
        self.expect_binding_absent(default_workspace, v2_role_id=tenant_v2_role_id, group_id=group_id)
        self._expect_v2_consistent()

    def test_role_with_mixed_resource_definitions_creates_multiple_bindings(self):
        """
        Test that a role with both scope-based permissions and workspace resource definitions creates multiple bindings.

        Scenario:
        - ROOT scope permission (advisor:recommendation:read) without resource definition → binds to root workspace
        - DEFAULT scope permission (inventory:groups:write) without resource definition → binds to root workspace (merged)
        - DEFAULT scope permission (inventory:groups:read) with specific workspace resource definition → binds to that workspace

        Expected: TWO separate bindings (one for root, one for specific workspace)
        """
        # Create a specific workspace for the resource definition
        specific_workspace = Workspace.objects.create(
            name="Specific Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.fixture.root_workspace(self.tenant),
        )
        specific_ws_id = str(specific_workspace.id)

        # Create role with mixed permissions
        with self.settings(ROOT_SCOPE_PERMISSIONS="advisor:*:*", TENANT_SCOPE_PERMISSIONS=""):
            role = self.given_v1_role(
                "mixed_resource_role",
                default=["advisor:recommendation:read", "inventory:groups:write"],
                **{specific_ws_id: ["inventory:groups:read"]},
            )

        # Verify 2 BindingMappings created (one per resource)
        mappings = BindingMapping.objects.filter(role=role)
        self.assertEqual(mappings.count(), 2, "Should have 2 BindingMapping records")

        # Get the V2 role IDs for each binding
        root_binding = mappings.get(resource_id=self.root_workspace())
        specific_binding = mappings.get(resource_id=specific_ws_id)

        root_role_id = root_binding.mappings["role"]["id"]
        specific_role_id = specific_binding.mappings["role"]["id"]

        # Verify root workspace binding exists with correct permissions
        self.expect_1_role_binding_to_workspace(
            self.root_workspace(),
            for_v2_roles=[root_role_id],
            for_groups=[],
        )
        root_role_binding = root_binding.get_role_binding()
        self.assertIn("advisor_recommendation_read", root_role_binding.role.permissions)
        self.assertIn("inventory_groups_write", root_role_binding.role.permissions)

        # Verify specific workspace binding exists with correct permissions
        self.expect_1_role_binding_to_workspace(
            specific_ws_id,
            for_v2_roles=[specific_role_id],
            for_groups=[],
        )
        specific_role_binding = specific_binding.get_role_binding()
        self.assertIn("inventory_groups_read", specific_role_binding.role.permissions)

        # Verify the two bindings have different V2 roles (different permission sets)
        self.assertNotEqual(
            root_role_id, specific_role_id, "Different permission sets should create different V2 roles"
        )

    def test_create_v2_tenant(self):
        ensure_v2_write_activated(self.tenant)

        with self.assertRaises(DualWriteException):
            self.given_v1_role("a role", default=["rbac:*:*"])

    def test_update_v2_tenant(self):
        role = self.given_v1_role("a role", default=["rbac:*:*"])

        ensure_v2_write_activated(self.tenant)

        with self.assertRaises(DualWriteException):
            self.given_update_to_v1_role(role, default=["inventory:*:*"])

    def test_ignore_out_of_tenant_workspace(self):
        another_tenant = Tenant.objects.create(tenant_name="another tenant", org_id="23456")
        bootstrap_result = bootstrap_tenant_for_v2_test(another_tenant)

        role = self.given_v1_role("a role", default=[], **{str(bootstrap_result.root_workspace.id): ["inventory:*:*"]})

        # There should be no binding in the default workspace because there are no permissions there.
        # There should be no binding in the other tenant's workspace because it should be ignored.
        self.assertFalse(BindingMapping.objects.filter(role=role).exists())
        self.assertFalse(RoleBinding.objects.filter(role__v1_source=role).exists())


@override_settings(ROOT_SCOPE_PERMISSIONS="advisor:*:*", TENANT_SCOPE_PERMISSIONS="subscriptions:*:*")
class DualWriteMixedScopeTestCase(DualWriteTestCase):
    """Test that mixed TENANT + workspace scope roles create per-scope bindings."""

    def tearDown(self):
        with self.subTest(msg="V2 consistency"):
            assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)
        super().tearDown()

    def test_custom_role_mixed_tenant_default_creates_two_bindings(self):
        """A custom role with TENANT + DEFAULT perms creates bindings at both levels."""
        role = self.given_v1_role(
            "mixed_td",
            default=["subscriptions:organization:read", "inventory:hosts:read"],
        )

        mappings = BindingMapping.objects.filter(role=role)
        self.assertEqual(mappings.count(), 2, "Should have 2 BindingMapping records (tenant + workspace)")

        tenant_bm = mappings.filter(resource_type_name="tenant")
        ws_bm = mappings.filter(resource_type_name="workspace")
        self.assertEqual(tenant_bm.count(), 1)
        self.assertEqual(ws_bm.count(), 1)

        # Tenant binding should have the subscriptions permission
        tenant_binding = tenant_bm.first().get_role_binding()
        self.assertIn("subscriptions_organization_read", tenant_binding.role.permissions)

        # Workspace binding should have the inventory permission
        ws_binding = ws_bm.first().get_role_binding()
        self.assertIn("inventory_hosts_read", ws_binding.role.permissions)

    def test_custom_role_mixed_tenant_root_default_splits_to_tenant_and_root(self):
        """TENANT + ROOT + DEFAULT: TENANT perms at tenant, rest at root workspace."""
        role = self.given_v1_role(
            "mixed_trd",
            default=["subscriptions:organization:read", "advisor:recommendation:read", "inventory:hosts:read"],
        )

        mappings = BindingMapping.objects.filter(role=role)
        self.assertEqual(mappings.count(), 2, "Should have 2 BindingMapping records (tenant + root ws)")

        tenant_bm = mappings.filter(resource_type_name="tenant")
        ws_bm = mappings.filter(resource_type_name="workspace")
        self.assertEqual(tenant_bm.count(), 1)
        self.assertEqual(ws_bm.count(), 1)

        # Workspace binding should be at root workspace (ROOT > DEFAULT)
        ws_mapping = ws_bm.first()
        root_ws = Workspace.objects.root(tenant=self.tenant)
        self.assertEqual(ws_mapping.resource_id, str(root_ws.id))

        # Verify permissions are split correctly
        tenant_binding = tenant_bm.first().get_role_binding()
        self.assertIn("subscriptions_organization_read", tenant_binding.role.permissions)

        ws_binding = ws_mapping.get_role_binding()
        self.assertIn("advisor_recommendation_read", ws_binding.role.permissions)
        self.assertIn("inventory_hosts_read", ws_binding.role.permissions)

    def test_custom_role_single_tenant_scope_no_split(self):
        """A role with only TENANT-scoped permissions gets a single tenant binding."""
        role = self.given_v1_role(
            "tenant_only",
            default=["subscriptions:organization:read", "subscriptions:products:write"],
        )

        mappings = BindingMapping.objects.filter(role=role)
        self.assertEqual(mappings.count(), 1)
        self.assertEqual(mappings.first().resource_type_name, "tenant")

    def test_system_role_mixed_scope_creates_per_scope_bindings(self):
        """A system role with mixed TENANT + workspace perms creates bindings at each scope when assigned to a group."""
        seed_group()
        role = self.given_v1_system_role(
            "mixed_system",
            permissions=["subscriptions:organization:read", "inventory:hosts:read"],
            platform_default=True,
        )

        group, _ = self.given_group("test_group")
        self.given_roles_assigned_to_group(group, [role])

        # Should have bindings at both tenant and workspace scopes
        group_uuid = str(group.uuid)
        mappings = BindingMapping.objects.filter(role=role)

        tenant_mappings = [m for m in mappings if m.resource_type_name == "tenant"]
        ws_mappings = [m for m in mappings if m.resource_type_name == "workspace"]

        self.assertGreaterEqual(len(tenant_mappings), 1, "Should have tenant-scope binding")
        self.assertGreaterEqual(len(ws_mappings), 1, "Should have workspace-scope binding")

        # Verify group is assigned in both
        for m in tenant_mappings:
            self.assertIn(group_uuid, m.mappings.get("groups", []))
        for m in ws_mappings:
            self.assertIn(group_uuid, m.mappings.get("groups", []))


@override_settings(ATOMIC_RETRY_DISABLED=True)
class DualWriteCrossAccountReqeustTestCase(DualWriteTestCase):
    user_id: str
    user: Principal

    def setUp(self):
        super().setUp()

        self.user_id = "user_id"
        self.user = self.fixture.new_principals_in_tenant(
            [self.user_id], self.fixture.new_tenant("car_source").tenant
        )[0]

    def test_adding_same_principal_to_two_cars_and_expire_one(self):
        system_role = self.given_v1_system_role("rtest", permissions=["app1:hosts:read", "inventory:hosts:write"])
        car_1 = self.given_car(self.user_id, [system_role])
        car_2 = self.given_car(self.user_id, [system_role])

        # See the user bound multiple times
        mappings = BindingMapping.objects.filter(role=system_role).first().mappings

        self.assertEqual(
            mappings["users"],
            {
                str(car_1.source_key()): self.user_id,
                str(car_2.source_key()): self.user_id,
            },
        )

        role_binding: RoleBinding = RoleBinding.objects.filter(role__v1_source=system_role).get()
        self.expect_role_binding_principals(
            role_binding,
            [
                (str(car_1.source_key()), self.user),
                (str(car_2.source_key()), self.user),
            ],
        )

        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", mappings["id"]),
                relation("subject"),
                subject("rbac", "principal", f"localhost/{self.user_id}"),
            )
        )
        self.assertEqual(len(tuples), 1)
        dual_write_handler = RelationApiDualWriteCrossAccessHandler(
            car_1,
            ReplicationEventType.EXPIRE_CROSS_ACCOUNT_REQUEST,
            replicator=InMemoryRelationReplicator(self.tuples),
        )
        dual_write_handler.generate_relations_to_remove_roles(car_1.roles.all())
        dual_write_handler.replicate()

        mappings = BindingMapping.objects.filter(role=system_role).first().mappings
        self.assertEqual(len(mappings["users"]), 1)

        role_binding: RoleBinding = RoleBinding.objects.filter(role__v1_source=system_role).get()
        self.expect_role_binding_principals(
            role_binding,
            [
                (str(car_2.source_key()), self.user),
            ],
        )

        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", mappings["id"]),
                relation("subject"),
                subject("rbac", "principal", f"localhost/{self.user_id}"),
            )
        )
        self.assertEqual(len(tuples), 1)

    def _expect_user_default_count(self, count: int, role: Role):
        self.expect_role_bindings_to_workspace(
            count,
            self.default_workspace(),
            for_v2_roles=[str(role.uuid)],
            for_groups=[],
            for_principals=[self.user_id],
        )

    def _expect_user_root_count(self, count: int, role: Role):
        self.expect_role_bindings_to_workspace(
            count,
            self.root_workspace(),
            for_v2_roles=[str(role.uuid)],
            for_groups=[],
            for_principals=[self.user_id],
        )

    def _expect_user_tenant_count(self, count: int, role: Role):
        self.expect_role_bindings_to_tenant(
            count,
            org_id=self.tenant.org_id,
            for_v2_roles=[str(role.uuid)],
            for_groups=[],
            for_principals=[self.user_id],
        )

    def _do_test_scope_assignment(self):
        system_role = self.given_v1_system_role("test", permissions=["app:resource:verb"])

        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            car = self.given_car(self.user_id, [system_role])

            self._expect_user_default_count(1, system_role)
            self._expect_user_root_count(0, system_role)
            self._expect_user_tenant_count(0, system_role)

            self.given_car_expired(car)

        with self.settings(ROOT_SCOPE_PERMISSIONS="app:resource:verb", TENANT_SCOPE_PERMISSIONS=""):
            car = self.given_car(self.user_id, [system_role])

            self._expect_user_default_count(0, system_role)
            self._expect_user_root_count(1, system_role)
            self._expect_user_tenant_count(0, system_role)

            self.given_car_expired(car)

        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="app:resource:verb"):
            car = self.given_car(self.user_id, [system_role])

            self._expect_user_default_count(0, system_role)
            self._expect_user_root_count(0, system_role)
            self._expect_user_tenant_count(1, system_role)

            self.given_car_expired(car)

    def test_scope_assignment(self):
        self._do_test_scope_assignment()

    def test_v2_scope_assignment(self):
        ensure_v2_write_activated(self.tenant)
        self._do_test_scope_assignment()

    def _do_test_scope_removal(self):
        system_role = self.given_v1_system_role("test", permissions=["app:resource:verb"])

        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="app:resource:verb"):
            car = self.given_car(self.user_id, [system_role])
            self._expect_user_tenant_count(1, system_role)

        # Expiring the CAR should remove the role binding even if the role's scope has changed in the interim.
        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            self.given_car_expired(car)
            self._expect_user_root_count(0, system_role)

    def test_scope_removal(self):
        self._do_test_scope_removal()

    def test_v2_scope_removal(self):
        ensure_v2_write_activated(self.tenant)
        self._do_test_scope_removal()

    def test_v1_assignment_with_v2_removal(self):
        """Test that a CAR created in a V1 tenant can be expired after that tenant migrates to V2."""
        system_role = self.given_v1_system_role("test", permissions=["app:resource:verb"])

        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            car = self.given_car(self.user_id, [system_role])
            self._expect_user_default_count(1, system_role)

        ensure_v2_write_activated(self.tenant)

        # We deliberately test removal in a different scope in order to ensure that doesn't break.
        with self.settings(ROOT_SCOPE_PERMISSIONS="app:resource:verb", TENANT_SCOPE_PERMISSIONS=""):
            self.given_car_expired(car)
            self._expect_user_default_count(0, system_role)

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
    def test_v2_existing_binding_unaffected(self):
        """Test that adding a CAR to a V2 tenant does not affect existing groups/principals on a role binding."""
        system_role = self.given_v1_system_role("test", permissions=["app:resource:verb"])
        group, _ = self.given_group("a group", ["p1"])

        service = RoleBindingService(
            tenant=self.tenant,
            replicator=InMemoryRelationReplicator(self.tuples),
            principal_source="another source",
            allow_external_subjects=True,
        )

        ensure_v2_write_activated(self.tenant)

        # Add a group to check that nothing accidentally removes the group later.
        service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=self.default_workspace(),
            subject_type="group",
            subject_id=str(group.uuid),
            role_ids=[str(system_role.uuid)],
        )

        service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=self.default_workspace(),
            subject_type="user",
            subject_id=str(self.user.uuid),
            role_ids=[str(system_role.uuid)],
        )

        self.expect_1_role_binding_to_workspace(
            workspace=self.default_workspace(),
            for_v2_roles=[str(system_role.uuid)],
            for_groups=[str(group.uuid)],
            for_principals=[self.user_id],
        )

        def _assert_source(exists: bool, source: str):
            self.assertEqual(
                exists,
                RoleBindingPrincipal.objects.filter(binding__role__v1_source=system_role, source=source).exists(),
            )

        car = self.given_car(self.user_id, [system_role])

        # The principal should still be bound (it just now has two sources).
        self.assertEqual(2, RoleBindingPrincipal.objects.filter(principal=self.user).count())

        _assert_source(True, "another source")
        _assert_source(True, str(car.source_key()))

        self.expect_1_role_binding_to_workspace(
            workspace=self.default_workspace(),
            for_v2_roles=[str(system_role.uuid)],
            for_groups=[str(group.uuid)],
            for_principals=[self.user_id],
        )

        self.given_car_expired(car)

        _assert_source(True, "another source")
        _assert_source(False, str(car.source_key()))

        # The principal should still be bound in relations (since there is still a source for it).
        self.expect_1_role_binding_to_workspace(
            workspace=self.default_workspace(),
            for_v2_roles=[str(system_role.uuid)],
            for_groups=[str(group.uuid)],
            for_principals=[self.user_id],
        )


class RbacFixture:
    """RBAC Fixture."""

    def __init__(
        self,
        bootstrap_service: TenantBootstrapService = V2TenantBootstrapService(NoopReplicator()),
    ):
        """Initialize the RBAC fixture."""
        self.public_tenant = Tenant.objects.get(tenant_name="public")
        self.bootstrap_service = bootstrap_service
        self.default_group, self.admin_group = seed_group()

    def new_tenant(self, org_id: str) -> BootstrappedTenant:
        """Create a new tenant with the given name and organization ID."""
        return self.bootstrap_service.new_bootstrapped_tenant(org_id)

    def new_not_ready_tenant(self, org_id: str) -> Tenant:
        """Create a new tenant with ready=false flag."""
        return Tenant.objects.create(tenant_name=f"org{org_id}", org_id=org_id, ready=False)

    def new_unbootstrapped_tenant(self, org_id: str) -> Tenant:
        """Create a new tenant with the given name and organization ID."""
        # A new unbootstrapped tenant would be ready, because this must've been created prior to bootstrapping
        return Tenant.objects.create(tenant_name=f"org{org_id}", org_id=org_id, ready=True)

    def bootstrap_tenant(self, tenant: Tenant) -> Optional[BootstrappedTenant]:
        """Bootstrap the tenant."""
        if isinstance(self.bootstrap_service, V2TenantBootstrapService):
            return self.bootstrap_service.bootstrap_tenant(tenant)
        else:
            # Nothing to do if not using V2 bootstrapping
            return None

    def new_system_role(
        self,
        name: str,
        permissions: Optional[list[str]] = None,
        platform_default=False,
        admin_default=False,
    ) -> Role:
        """Create a new system role with the given name and permissions."""
        if permissions is None:
            permissions = []

        role = Role.objects.create(
            name=name,
            system=True,
            platform_default=platform_default,
            admin_default=admin_default,
            tenant=self.public_tenant,
        )

        access_list = [
            Access(
                permission=Permission.objects.get_or_create(permission=permission, tenant=self.public_tenant)[0],
                role=role,
                tenant=self.public_tenant,
            )
            for permission in permissions
        ]

        Access.objects.bulk_create(access_list)

        seed_v2_role_from_v1(role)

        return role

    def new_custom_role(self, name: str, resource_access: list[tuple[list[str], dict]], tenant: Tenant) -> Role:
        """
        Create a new custom role.

        [resource_access] is a list of tuples of the form (permissions, attribute filter).
        """
        role = Role.objects.create(name=name, system=False, tenant=tenant)
        return self.update_custom_role(role, resource_access)

    def update_custom_role(self, role: Role, resource_access: list[tuple[list[str], dict]]) -> Role:
        """
        Update a custom role.

        [resource_access] is a list of tuples of the form (permissions, attribute filter).
        """
        role.access.all().delete()

        for permissions, attribute_filter in resource_access:
            access_list = [
                Access(
                    permission=Permission.objects.get_or_create(permission=permission, tenant=self.public_tenant)[0],
                    role=role,
                    tenant=role.tenant,
                )
                for permission in permissions
            ]

            Access.objects.bulk_create(access_list)

            if attribute_filter:
                for access in access_list:
                    ResourceDefinition.objects.create(
                        attributeFilter=attribute_filter,
                        access=access,
                        tenant=role.tenant,
                    )

        return role

    def new_principals_in_tenant(self, users: list[str], tenant: Tenant) -> list[Principal]:
        """Create new principals in the tenant."""
        return [
            Principal.objects.get_or_create(username=user_id, tenant=tenant, user_id=user_id)[0] for user_id in users
        ]

    def workspace_access(self, default: list[str] = [], **kwargs: list[str]):
        """
        Generate a list of tuples representing workspace access permissions.

        Args:
            default (list[str]): A list of default permissions.
            **kwargs (list[str]): Additional keyword arguments where the key is the workspace
                                  and the value is a list of permissions for that workspace.

        Returns:
            list[tuple]: A list of tuples where each tuple contains a list of permissions and
                         a dictionary describing an attribute filter.
        """
        return [
            (default, {}),
            *[
                (
                    permissions,
                    {"key": "group.id", "operation": "equal", "value": workspace},
                )
                for workspace, permissions in kwargs.items()
            ],
        ]

    def new_group(
        self,
        name: str,
        tenant: Tenant,
        users: list[str] = [],
        service_accounts: list[str] = [],
    ) -> Tuple[Group, list[Principal]]:
        """Create a new group with the given name, users, and tenant."""
        group = Group.objects.create(name=name, tenant=tenant)
        principals = self.add_members_to_group(group, users, service_accounts, tenant)
        return group, principals

    def custom_default_group(self, tenant: Tenant) -> Group:
        return set_system_flag_before_update(Group.objects.get(pk=self.default_group.pk), tenant, None)  # type: ignore

    def root_workspace(self, tenant: Tenant) -> Workspace:
        return Workspace.objects.root(tenant=tenant)

    def default_workspace(self, tenant: Tenant) -> Workspace:
        return Workspace.objects.default(tenant=tenant)

    def add_role_to_group(self, role: Role, group: Group) -> Policy:
        """Add a role to a group for a given tenant and return the policy."""
        policy, _ = Policy.objects.get_or_create(
            name=f"System Policy for Group {group.uuid}",
            system=True,
            group=group,
            tenant=group.tenant,
        )
        policy.roles.add(role)
        policy.save()
        return policy

    def remove_role_from_group(self, role: Role, group: Group) -> Policy:
        """Remove a role to a group for a given tenant and return the policy."""
        policy, _ = Policy.objects.get_or_create(
            name=f"System Policy for Group {group.uuid}",
            system=True,
            group=group,
            tenant=group.tenant,
        )
        policy.roles.remove(role)
        policy.save()
        return policy

    def add_members_to_group(
        self,
        group: Group,
        users: list[str],
        service_accounts: list[str],
        principal_tenant: Tenant,
    ) -> list[Principal]:
        """Add members to the group."""
        principals = [
            *[
                Principal.objects.get_or_create(username=user_id, tenant=principal_tenant, user_id=user_id)[0]
                for user_id in users
            ],
            *[
                Principal.objects.get_or_create(
                    username="service-account-" + user_id,
                    tenant=principal_tenant,
                    type="service-account",
                    user_id=user_id,
                )[0]
                for user_id in service_accounts
            ],
        ]

        group.principals.add(*principals)

        return principals

    def remove_members_from_group(
        self,
        group: Group,
        users: list[str],
        service_accounts: list[str],
        principal_tenant: Tenant,
    ):
        """Remove members from the group."""
        principals = [
            *[Principal.objects.get_or_create(username=username, tenant=principal_tenant)[0] for username in users],
            *[
                Principal.objects.get_or_create(username=username, tenant=principal_tenant, type="service-account")[0]
                for username in service_accounts
            ],
        ]

        group.principals.remove(*principals)

        return principals

    def new_car(self, tenant: Tenant, user_id: str) -> CrossAccountRequest:
        """Create a new CAR."""
        return CrossAccountRequest.objects.create(
            target_org=tenant.org_id,
            user_id=user_id,
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(days=1),
            status="approved",
        )
