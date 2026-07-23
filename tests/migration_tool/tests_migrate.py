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
"""Test the utils module."""

from datetime import timedelta

from platform import system
from unittest.mock import Mock, call, patch

from uuid import uuid4

from django.test import TestCase, override_settings

from django.utils import timezone

from api.models import CrossAccountRequest, Tenant

from management.models import *

from management.tenant_service.tenant_service import BootstrappedTenant
from management.tenant_service.v2 import V2TenantBootstrapService
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    all_of,
    none_of,
    one_of,
    relation,
    resource,
    resource_type,
    subject,
    subject_type,
)

from migration_tool.migrate import migrate_data, migrate_groups_for_tenant

from management.group.definer import seed_group, clone_default_group_in_public_schema
from tests.management.role.test_dual_write import RbacFixture
from tests.v2_util import seed_v2_role_from_v1, bootstrap_tenant_for_v2_test


@override_settings(ATOMIC_RETRY_DISABLED=True)
class MigrateTests(TestCase):
    """Test the utils module."""

    def setUp(self):
        """Set up the utils tests."""
        super().setUp()
        # public tenant
        public_tenant = Tenant.objects.get(tenant_name="public")

        # system roles
        self.system_role_1 = Role.objects.create(
            name="System Role 1", platform_default=True, tenant=public_tenant, system=True
        )
        self.system_role_2 = Role.objects.create(
            name="System Role 2", platform_default=True, system=True, tenant=public_tenant
        )

        seed_v2_role_from_v1(self.system_role_1)
        seed_v2_role_from_v1(self.system_role_2)

        # default group
        default_group, _ = seed_group()
        # permissions
        # This would be skipped
        permission1 = Permission.objects.create(permission="app1:hosts:read", tenant=public_tenant)
        permission2 = Permission.objects.create(permission="inventory:hosts:write", tenant=public_tenant)
        Access.objects.bulk_create(
            [
                Access(permission=permission1, role=self.system_role_2, tenant=public_tenant),
                Access(permission=permission2, role=self.system_role_2, tenant=public_tenant),
            ]
        )

        # two organizations
        # tenant 1 - org_id=1234567
        self.tenant = Tenant.objects.create(org_id="1234567", tenant_name="tenant", ready=True)

        tenant_bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = tenant_bootstrap_result.default_workspace
        self.root_workspace = tenant_bootstrap_result.root_workspace

        # setup data for organization 1234567
        # Create actual workspaces for the test
        workspace_1 = Workspace.objects.create(
            type=Workspace.Types.STANDARD, tenant=self.tenant, name="Test Workspace 1", parent=self.default_workspace
        )
        workspace_2 = Workspace.objects.create(
            type=Workspace.Types.STANDARD, tenant=self.tenant, name="Test Workspace 2", parent=self.default_workspace
        )
        self.workspace_id_1 = str(workspace_1.id)
        self.workspace_id_2 = str(workspace_2.id)

        # This role will be skipped because it contains permission with skipping application
        self.role_a1 = Role.objects.create(name="role_a1", tenant=self.tenant)
        self.access_a11 = Access.objects.create(permission=permission1, role=self.role_a1, tenant=self.tenant)
        self.access_a12 = Access.objects.create(permission=permission2, role=self.role_a1, tenant=self.tenant)

        self.role_a2 = Role.objects.create(name="role_a2", tenant=self.tenant)
        self.access_a2 = Access.objects.create(permission=permission2, role=self.role_a2, tenant=self.tenant)
        self.resourceDef_a2 = ResourceDefinition.objects.create(
            attributeFilter={
                "key": "group.id",
                "operation": "equal",
                "value": self.workspace_id_1,
            },
            access=self.access_a2,
            tenant=self.tenant,
        )
        self.role_a3 = Role.objects.create(name="role_a3", tenant=self.tenant)
        self.access_a3 = Access.objects.create(permission=permission2, role=self.role_a3, tenant=self.tenant)
        self.resourceDef_a3 = ResourceDefinition.objects.create(
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [self.workspace_id_1, self.workspace_id_2],
            },
            access=self.access_a3,
            tenant=self.tenant,
        )
        self.group_a2 = Group.objects.create(name="group_a2", tenant=self.tenant)
        self.principal1 = Principal.objects.create(username="principal1", tenant=self.tenant, user_id="user_id_1")
        self.principal2 = Principal.objects.create(username="principal2", tenant=self.tenant, user_id="user_id_2")
        self.group_a2.principals.add(self.principal1, self.principal2)
        self.policy_a2 = Policy.objects.create(name="System Policy_a2", group=self.group_a2, tenant=self.tenant)
        self.policy_a2.roles.add(self.role_a2)

        # tenant 2 - org_id=7654321
        another_tenant = Tenant.objects.create(org_id="7654321", ready=True)
        another_tenant_bootstrap_result = bootstrap_tenant_for_v2_test(another_tenant)

        # Create the principal that the cross-account request below will use.
        Principal.objects.create(tenant=self.tenant, username="1111111", user_id="1111111")

        self.default_workspace_for_another_tenant = another_tenant_bootstrap_result.default_workspace
        self.another_tenant = another_tenant

        Group.objects.create(name="another_group", tenant=another_tenant)

        # setup data for another tenant 7654321
        self.role_b = Role.objects.create(name="role_b", tenant=another_tenant)
        self.access_b = Access.objects.create(permission=permission2, role=self.role_b, tenant=another_tenant)

        self.policy_a2.roles.add(self.system_role_1)
        self.policy_a2.save()

        # create custom default group
        self.custom_default_group = clone_default_group_in_public_schema(default_group, self.tenant)

        # Setup cross account request to migrate
        self.ref_time = timezone.now()
        self.cross_account_request = CrossAccountRequest.objects.create(
            target_org="7654321",
            user_id="1111111",
            end_date=self.ref_time + timedelta(10),
            status="approved",
        )
        self.cross_account_request.roles.add(self.system_role_2)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat", READ_ONLY_API_MODE=True)
    @patch("migration_tool.migrate.RelationApiDualWriteGroupHandler.replicate")
    def test_migration_of_data_no_replication_event_to_migrate_groups(self, replicate_method):
        """Test that we get the correct access for a principal."""
        kwargs = {"exclude_apps": ["app1"], "orgs": ["7654321"]}
        migrate_data(**kwargs)
        replicate_method.assert_not_called()

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat", READ_ONLY_API_MODE=True)
    @patch("management.relation_replicator.logging_replicator.logger")
    def test_migration_of_data(self, logger_mock):
        """Test that we get the correct access for a principal."""
        kwargs = {"exclude_apps": ["app1"], "orgs": ["1234567"]}
        migrate_data(**kwargs)

        # This role should be skipped in its entirety due to having a permission from an excluded app.
        self.assertFalse(RoleV2.objects.filter(v1_source=self.role_a1).exists())

        # The V1 role has permissions only in a single workspace, so there should be a single V2 role and a single
        # binding.
        role_a2_v2 = RoleV2.objects.get(v1_source=self.role_a2)

        self.assertEqual(RoleV2.Types.CUSTOM, role_a2_v2.type)
        self.assertEqual(self.role_a2, role_a2_v2.v1_source)
        self.assertEqual(0, role_a2_v2.children.count())
        self.assertEqual({"inventory:hosts:write"}, {p.permission for p in role_a2_v2.permissions.all()})

        role_a2_binding = role_a2_v2.bindings.get()
        self.assertCountEqual([self.group_a2], role_a2_binding.bound_groups())
        self.assertEqual("workspace", role_a2_binding.resource_type)
        self.assertEqual(self.workspace_id_1, role_a2_binding.resource_id)

        # The V1 role has permissions in two workspaces, so there should be a single V2 role and two role bindings.
        role_a3_v2 = RoleV2.objects.get(v1_source=self.role_a3)

        self.assertEqual(RoleV2.Types.CUSTOM, role_a3_v2.type)
        self.assertEqual(self.role_a3, role_a3_v2.v1_source)
        self.assertEqual(0, role_a3_v2.children.count())
        self.assertEqual({"inventory:hosts:write"}, {p.permission for p in role_a3_v2.permissions.all()})

        role_a3_bindings = role_a3_v2.bindings.all()
        self.assertEqual(2, len(role_a3_bindings))
        self.assertCountEqual([self.workspace_id_1, self.workspace_id_2], [b.resource_id for b in role_a3_bindings])

        for binding in role_a3_v2.bindings.all():
            self.assertEqual("workspace", binding.resource_type)

            # No groups are assigned this role.
            self.assertFalse(binding.bound_groups().exists())

        org_id = self.tenant.org_id
        root_workspace_id = str(self.root_workspace.id)
        default_workspace_id = str(self.default_workspace.id)

        role_binding = BindingMapping.objects.filter(role=self.role_a2).get().get_role_binding()

        rolebinding_a2 = role_binding.id
        v2_role_a2 = role_binding.role.id

        role_binding_a3_1 = (
            BindingMapping.objects.filter(role=self.role_a3, resource_id=self.workspace_id_1).get().get_role_binding()
        )
        role_binding_a3_2 = (
            BindingMapping.objects.filter(role=self.role_a3, resource_id=self.workspace_id_2).get().get_role_binding()
        )
        v2_role_a31 = role_binding_a3_1.role.id
        v2_role_a32 = role_binding_a3_2.role.id

        rolebinding_a31 = role_binding_a3_1.id
        rolebinding_a32 = role_binding_a3_2.id

        workspace_1 = self.workspace_id_1
        workspace_2 = self.workspace_id_2
        # Switch these two if rolebinding order is not the same as v2 roles
        if call(f"role_binding:{rolebinding_a31}#role@role:{v2_role_a31}") not in logger_mock.info.call_args_list:
            rolebinding_a31, rolebinding_a32 = rolebinding_a32, rolebinding_a31
        # Switch these two if binding is not in correct order
        if (
            call(f"workspace:{self.workspace_id_1}#binding@role_binding:{rolebinding_a31}")
            not in logger_mock.info.call_args_list
        ):
            workspace_1, workspace_2 = workspace_2, workspace_1

        binding_mapping_system_role_1 = BindingMapping.objects.filter(
            role=self.system_role_1,
            resource_type_name="workspace",
            resource_type_namespace="rbac",
            resource_id=self.default_workspace.id,
        ).get()

        self.assertEqual(
            binding_mapping_system_role_1.mappings["groups"],
            [str(self.custom_default_group.uuid), str(self.group_a2.uuid)],
        )

        role_binding_system_role_1_uuid = binding_mapping_system_role_1.mappings["id"]

        binding_mapping_system_role_2 = BindingMapping.objects.filter(
            role=self.system_role_2,
            resource_type_name="workspace",
            resource_type_namespace="rbac",
            resource_id=self.default_workspace.id,
        ).get()

        self.assertEqual(binding_mapping_system_role_2.mappings["groups"][0], str(self.custom_default_group.uuid))

        role_binding_system_role_2_uuid = binding_mapping_system_role_2.mappings["id"]
        tuples = [
            # Org relationships of self.tenant
            # the other org is not included since it is not specified in the orgs parameter
            ## Group member
            call(f"group:{self.group_a2.uuid}#member@principal:{self.principal1.principal_resource_id()}"),
            call(f"group:{self.group_a2.uuid}#member@principal:{self.principal2.principal_resource_id()}"),
            ## Role binding to role_a2
            call(f"role_binding:{rolebinding_a2}#role@role:{v2_role_a2}"),
            call(f"role:{v2_role_a2}#inventory_hosts_write@principal:*"),
            call(f"role_binding:{rolebinding_a2}#subject@group:{self.group_a2.uuid}"),
            call(f"workspace:{self.workspace_id_1}#binding@role_binding:{rolebinding_a2}"),
            ## Role binding to role_a3
            call(f"role_binding:{rolebinding_a31}#role@role:{v2_role_a31}"),
            # NOTE: Duplicate permission tuples are deduplicated, so we only expect it once
            # call(f"role:{v2_role_a31}#inventory_hosts_write@principal:*"),  # Deduplicated!
            call(f"workspace:{workspace_1}#binding@role_binding:{rolebinding_a31}"),
            call(f"role_binding:{rolebinding_a32}#role@role:{v2_role_a32}"),
            # call(f"role:{v2_role_a32}#inventory_hosts_write@principal:*"),  # Deduplicated!
            call(f"workspace:{workspace_2}#binding@role_binding:{rolebinding_a32}"),
            ## System role 1 assigment to custom group
            call(f"workspace:{self.default_workspace.id}#binding@role_binding:{role_binding_system_role_1_uuid}"),
            call(f"role_binding:{role_binding_system_role_1_uuid}#subject@group:{self.group_a2.uuid}"),
            call(f"role_binding:{role_binding_system_role_1_uuid}#role@role:{self.system_role_1.uuid}"),
            ## System role 2 assigment to custom default group
            call(f"workspace:{self.default_workspace.id}#binding@role_binding:{role_binding_system_role_2_uuid}"),
            call(f"role_binding:{role_binding_system_role_2_uuid}#subject@group:{self.custom_default_group.uuid}"),
            call(f"role_binding:{role_binding_system_role_2_uuid}#role@role:{self.system_role_2.uuid}"),
        ]

        # With deduplication, we should see the permission tuple for the V2 role only once
        # Add it separately since v2_role_a31 and v2_role_a32 might be the same (shared V2 role)
        if v2_role_a31 == v2_role_a32:
            # Both bindings share same V2 role - permission tuple appears once
            tuples.append(call(f"role:{v2_role_a31}#inventory_hosts_write@principal:*"))
        else:
            # Different V2 roles - each has its own permission tuple
            tuples.append(call(f"role:{v2_role_a31}#inventory_hosts_write@principal:*"))
            tuples.append(call(f"role:{v2_role_a32}#inventory_hosts_write@principal:*"))

        logger_mock.info.assert_has_calls(tuples, any_order=True)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat", READ_ONLY_API_MODE=True)
    @patch("management.relation_replicator.logging_replicator.logger")
    def test_migration_of_cross_account_requests(self, logger_mock):
        """Test that we get the correct access for a principal."""
        kwargs = {"exclude_apps": ["app1"], "orgs": ["7654321"]}
        migrate_data(**kwargs)

        binding_mapping_system_role_2 = BindingMapping.objects.filter(
            role=self.system_role_2,
            resource_type_name="workspace",
            resource_type_namespace="rbac",
            resource_id=self.default_workspace_for_another_tenant.id,
        ).get()

        role_binding_system_role_2_uuid = binding_mapping_system_role_2.mappings["id"]

        tuples = [
            ## Cross account request for system role 2
            call(
                f"role_binding:{role_binding_system_role_2_uuid}#subject@principal:redhat/{self.cross_account_request.user_id}"
            ),
            call(f"role_binding:{role_binding_system_role_2_uuid}#role@role:{self.system_role_2.uuid}"),
            call(
                f"workspace:{self.default_workspace_for_another_tenant.id}#binding@role_binding:{role_binding_system_role_2_uuid}"
            ),
        ]
        logger_mock.info.assert_has_calls(tuples, any_order=True)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat", READ_ONLY_API_MODE=True)
    def test_skips_orgs_without_org_ids(self):
        # Create a tenant without an org id
        Tenant.objects.create(tenant_name="tenant", ready=True)

        # Migrate without limiting org
        kwargs = {"exclude_apps": ["app1"]}

        try:
            migrate_data(**kwargs)
        except Exception:
            self.fail("migrate_data raised an exception when migrating tenant without org_id")

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat", READ_ONLY_API_MODE=True)
    @patch("migration_tool.migrate.migrate_groups_for_tenant")
    @patch("migration_tool.migrate.migrate_roles_for_tenant")
    @patch("migration_tool.migrate.migrate_cross_account_requests")
    def test_skips_roles_migration(self, group_migrator, role_migrator, car_migrator):
        kwargs = {"orgs": ["1234567"], "skip_roles": True}

        migrate_data(**kwargs)

        group_migrator.assert_called_once()
        role_migrator.assert_not_called()
        car_migrator.assert_called_once()


@override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat", READ_ONLY_API_MODE=True)
class MigrateTestTupleStore(TestCase):
    """Test migrator."""

    relations: InMemoryTuples
    fixture: RbacFixture

    o1: BootstrappedTenant
    o2: BootstrappedTenant

    def setUp(self):
        """Set up the migrator tests."""
        super().setUp()

        self.relations = InMemoryTuples()
        self.fixture = RbacFixture(V2TenantBootstrapService(InMemoryRelationReplicator(self.relations)))

        # Create two platform default system roles, and one not platform default
        self.sr1 = self.fixture.new_system_role("sr1", ["app1:res1:verb1", "app2:res2:verb2"], platform_default=True)
        self.sr2 = self.fixture.new_system_role("sr2", ["app3:res3:verb3", "app4:res4:verb4"], platform_default=True)
        self.sr3 = self.fixture.new_system_role("sr3", ["app5:res5:verb5", "app6:res6:verb6"])

        self.o1 = self.fixture.new_tenant("o1")
        self.o2 = self.fixture.new_tenant("o2")

        o1_w1 = Workspace.objects.create(
            tenant=self.o1.tenant, parent=Workspace.objects.default(tenant=self.o1.tenant), name="o1_w1"
        )

        o1_w2 = Workspace.objects.create(
            tenant=self.o1.tenant, parent=Workspace.objects.default(tenant=self.o1.tenant), name="o1_w2"
        )

        self.o1_w1_id = str(o1_w1.id)
        self.o1_w2_id = str(o1_w2.id)

        # Tenanted objects for o1
        self.o1_r1 = self.fixture.new_custom_role(
            "o1_r1", self.fixture.workspace_access(default=["app5:res5:verb5"]), self.o1.tenant
        )
        self.o1_r2 = self.fixture.new_custom_role(
            "o1_r2", self.fixture.workspace_access(**{self.o1_w1_id: ["app1:res1:verb1"]}), self.o1.tenant
        )
        self.o1_r3 = self.fixture.new_custom_role(
            "o1_r3",
            self.fixture.workspace_access(
                **{
                    self.o1_w1_id: ["app2:res2:verb2"],
                    self.o1_w2_id: ["app2:res2:verb2", "app3:res3:verb3"],
                }
            ),
            self.o1.tenant,
        )

        self.o1_default_group = self.fixture.custom_default_group(self.o1.tenant)
        self.fixture.add_role_to_group(self.o1_r1, self.o1_default_group)
        self.fixture.remove_role_from_group(self.sr2, self.o1_default_group)

        self.o1_g1, _ = self.fixture.new_group("o1_g1", self.o1.tenant, ["o1_u1", "o1_u2"])
        self.fixture.add_role_to_group(self.sr3, self.o1_g1)
        self.fixture.add_role_to_group(self.o1_r2, self.o1_g1)

        # Tenanted objects for o2
        self.o2_r1 = self.fixture.new_custom_role(
            "o2_r1", self.fixture.workspace_access(default=["app5:res5:verb5"]), self.o2.tenant
        )
        self.o2_g1, _ = self.fixture.new_group("o2_g1", self.o2.tenant, ["o2_u1", "o2_u2"])
        self.fixture.add_role_to_group(self.o2_r1, self.o2_g1)

        self.maxDiff = None

    def test_migrate_no_exclusions(self):
        self.relations.clear()
        self.o1.tenant.ready = True
        self.o1.tenant.save()
        self.o2.tenant.ready = True
        self.o2.tenant.save()

        migrate_data(write_relationships=InMemoryRelationReplicator(self.relations))

        # Check tenanted objects for o1 are migrated

        # Group members...
        # 2
        self.assertCountEqual(
            ["redhat/o1_u1", "redhat/o1_u2"],
            [
                t.subject.subject.id
                for t in self.relations.find_tuples(
                    all_of(resource("rbac", "group", self.o1_g1.uuid), relation("member")),
                )
            ],
        )

        # Bindings...

        # o1_default_group should be bound default workspace with sr1 and o1_r1, but not sr2
        # Start with the default workspace bindings...
        default_bindings = self.relations.find_tuples(
            all_of(resource("rbac", "workspace", self.o1.default_workspace.id), relation("binding")),
        )

        # 4
        self.assertTrue(
            # ...then traverse that role binding
            default_bindings.traverse_subject(
                # and find which ones have
                [
                    # a role relation, to o1_r1 (app5 perm); only=False because role also has #owner@tenant
                    all_of(
                        relation("role"),
                        self.relations.subject_is_resource_of(relation("app5_res5_verb5"), only=False),
                    ),
                    # and a subject relation to the custom default group's members
                    all_of(relation("subject"), subject("rbac", "group", self.o1_default_group.uuid, "member")),
                ]
            ),
            "missing o1_r1 binding",
        )

        # 3
        self.assertTrue(
            default_bindings.traverse_subject(
                # from default find bindings which have
                [
                    # a role relation, to the sr1 role
                    all_of(
                        relation("role"),
                        subject("rbac", "role", self.sr1.uuid),
                    ),
                    # and a subject relation to the custom default group's members
                    all_of(relation("subject"), subject("rbac", "group", self.o1_default_group.uuid, "member")),
                ]
            ),
            "missing sr1 binding",
        )

        self.assertFalse(
            default_bindings.traverse_subject(
                # from default find bindings which have
                [
                    # a role relation, to the sr2 role
                    all_of(
                        relation("role"),
                        subject("rbac", "role", self.sr2.uuid),
                    ),
                    # and a subject relation to the custom default group's members
                    all_of(relation("subject"), subject("rbac", "group", self.o1_default_group.uuid, "member")),
                ]
            ),
            "unexpected sr2 binding",
        )

        # o1_1 should be bound to sr3 and to o1_r2, which grants permissions to workspace o1_w1
        # 3
        self.assertTrue(
            default_bindings.traverse_subject(
                [
                    all_of(
                        relation("role"),
                        subject("rbac", "role", self.sr3.uuid),
                    ),
                    all_of(relation("subject"), subject("rbac", "group", self.o1_g1.uuid, "member")),
                ]
            )
        )

        # 4
        self.assertTrue(
            self.relations.find_tuples(
                all_of(resource("rbac", "workspace", self.o1_w1_id), relation("binding"))
            ).traverse_subject(
                [
                    all_of(
                        relation("role"),
                        self.relations.subject_is_resource_of(relation("app1_res1_verb1"), only=False),
                    ),
                    all_of(relation("subject"), subject("rbac", "group", self.o1_g1.uuid, "member")),
                ]
            )
        )

        # o1_r3 custom role should have bindings without subjects (none bound yet)
        # 3
        self.assertTrue(
            self.relations.find_tuples(
                all_of(resource("rbac", "workspace", self.o1_w1_id), relation("binding"))
            ).traverse_subject(
                [
                    all_of(
                        relation("role"),
                        self.relations.subject_is_resource_of(relation("app2_res2_verb2"), only=False),
                    ),
                ]
            ),
            "missing o1_r3 binding for o1_w1",
        )

        # 4
        self.assertTrue(
            self.relations.find_tuples(
                all_of(resource("rbac", "workspace", self.o1_w2_id), relation("binding"))
            ).traverse_subject(
                [
                    all_of(
                        relation("role"),
                        self.relations.subject_is_resource_of(
                            [relation("app2_res2_verb2"), relation("app3_res3_verb3")], only=False
                        ),
                    ),
                ]
            ),
            "missing o1_r3 binding for o1_w2",
        )

        # Check tenanted objects for o2 are migrated

        # Group members...
        # 2
        self.assertCountEqual(
            ["redhat/o2_u1", "redhat/o2_u2"],
            [
                t.subject.subject.id
                for t in self.relations.find_tuples(
                    all_of(resource("rbac", "group", self.o2_g1.uuid), relation("member")),
                )
            ],
        )

        # Bindings...
        # 4
        self.assertTrue(
            self.relations.find_tuples(
                all_of(resource("rbac", "workspace", self.o2.default_workspace.id), relation("binding"))
            ).traverse_subject(
                [
                    all_of(
                        relation("role"),
                        self.relations.subject_is_resource_of(relation("app5_res5_verb5"), only=False),
                    ),
                    all_of(relation("subject"), subject("rbac", "group", self.o2_g1.uuid, "member")),
                ]
            ),
            "missing o2_r1 binding",
        )

        # Includes rbac/role#owner@rbac/tenant for each migrated custom role (29 baseline + 5 owner edges).
        self.assertEqual(34, len(self.relations))

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat", READ_ONLY_API_MODE=True)
    def test_empty_resource_id_filtered_out(self):
        """Test that resource definitions with empty string values are filtered out and no tuples are created."""
        # Create a permission for inventory
        permission = Permission.objects.create(permission="inventory:hosts:read", tenant=self.o1.tenant)

        # Create a custom role with a resource definition that has an empty string value
        role_with_empty_resource = Role.objects.create(name="role_with_empty", tenant=self.o1.tenant)
        access = Access.objects.create(permission=permission, role=role_with_empty_resource, tenant=self.o1.tenant)
        ResourceDefinition.objects.create(
            attributeFilter={
                "key": "group.id",
                "operation": "equal",
                "value": "",  # Empty string - should be filtered out
            },
            access=access,
            tenant=self.o1.tenant,
        )

        # Create a group and assign the role to it
        group = Group.objects.create(name="test_group", tenant=self.o1.tenant)
        principal = Principal.objects.create(username="test_user", tenant=self.o1.tenant, user_id="test_user_id")
        group.principals.add(principal)
        policy = Policy.objects.create(name="test_policy", group=group, tenant=self.o1.tenant)
        policy.roles.add(role_with_empty_resource)

        # Clear relations and migrate
        self.relations.clear()
        self.o1.tenant.ready = True
        self.o1.tenant.save()

        migrate_data(write_relationships=InMemoryRelationReplicator(self.relations), orgs=[self.o1.tenant.org_id])

        # Verify no BindingMapping was created for the empty resource_id
        binding_mappings = BindingMapping.objects.filter(role=role_with_empty_resource, resource_id="")
        self.assertEqual(0, binding_mappings.count(), "No binding mapping should be created with empty resource_id")

        # Verify no tuples were created with empty resource_id
        # Search for any tuple with an empty ID (this should not exist)
        tuples_with_empty_id = [t for t in self.relations._tuples if t.resource.id == "" or t.subject.subject.id == ""]
        self.assertEqual(0, len(tuples_with_empty_id), "No tuples should have empty resource_id or subject_id")

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat", READ_ONLY_API_MODE=True)
    def test_empty_resource_id_in_list_filtered_out(self):
        """Test that empty strings in a list of resource IDs are filtered out."""
        # Create a permission for inventory
        permission = Permission.objects.create(permission="inventory:hosts:write", tenant=self.o1.tenant)

        # Create workspaces to use as valid resource IDs
        workspace1 = Workspace.objects.create(
            type=Workspace.Types.STANDARD,
            tenant=self.o1.tenant,
            name="Test Workspace 1",
            parent=self.o1.default_workspace,
        )
        workspace2 = Workspace.objects.create(
            type=Workspace.Types.STANDARD,
            tenant=self.o1.tenant,
            name="Test Workspace 2",
            parent=self.o1.default_workspace,
        )

        # Create a custom role with a resource definition that has empty strings mixed with valid IDs
        role_with_mixed_resources = Role.objects.create(name="role_with_mixed", tenant=self.o1.tenant)
        access = Access.objects.create(permission=permission, role=role_with_mixed_resources, tenant=self.o1.tenant)
        ResourceDefinition.objects.create(
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [str(workspace1.id), "", str(workspace2.id), ""],  # Mix of valid and empty strings
            },
            access=access,
            tenant=self.o1.tenant,
        )

        # Create a group and assign the role to it
        group = Group.objects.create(name="test_group_mixed", tenant=self.o1.tenant)
        principal = Principal.objects.create(
            username="test_user_mixed", tenant=self.o1.tenant, user_id="test_user_mixed_id"
        )
        group.principals.add(principal)
        policy = Policy.objects.create(name="test_policy_mixed", group=group, tenant=self.o1.tenant)
        policy.roles.add(role_with_mixed_resources)

        # Clear relations and migrate
        self.relations.clear()
        self.o1.tenant.ready = True
        self.o1.tenant.save()

        migrate_data(write_relationships=InMemoryRelationReplicator(self.relations), orgs=[self.o1.tenant.org_id])

        # Verify no BindingMapping was created for empty resource_id
        binding_mappings_empty = BindingMapping.objects.filter(role=role_with_mixed_resources, resource_id="")
        self.assertEqual(
            0, binding_mappings_empty.count(), "No binding mapping should be created with empty resource_id"
        )

        # Verify BindingMappings were created for valid workspace IDs only
        binding_mappings_ws1 = BindingMapping.objects.filter(
            role=role_with_mixed_resources, resource_id=str(workspace1.id)
        )
        binding_mappings_ws2 = BindingMapping.objects.filter(
            role=role_with_mixed_resources, resource_id=str(workspace2.id)
        )
        self.assertEqual(1, binding_mappings_ws1.count(), "Binding mapping should be created for workspace1")
        self.assertEqual(1, binding_mappings_ws2.count(), "Binding mapping should be created for workspace2")

        # Verify tuples were created for valid workspaces
        tuples_ws1 = self.relations.find_tuples(
            all_of(resource("rbac", "workspace", str(workspace1.id)), relation("binding"))
        )
        tuples_ws2 = self.relations.find_tuples(
            all_of(resource("rbac", "workspace", str(workspace2.id)), relation("binding"))
        )
        self.assertGreater(len(tuples_ws1), 0, "Tuples should be created for workspace1")
        self.assertGreater(len(tuples_ws2), 0, "Tuples should be created for workspace2")

        # Verify no tuples with empty resource_id
        tuples_with_empty_id = [t for t in self.relations._tuples if t.resource.id == "" or t.subject.subject.id == ""]
        self.assertEqual(0, len(tuples_with_empty_id), "No tuples should have empty resource_id or subject_id")
