#
# Copyright 2024 Red Hat, Inc.
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
"""Test cases for Tenant bootstrapping logic."""

from typing import Optional, Tuple
import uuid

from django.conf import settings
from unittest.mock import patch
from django.test import TestCase
from management.group.definer import seed_group
from management.group.model import Group
from management.policy.model import Policy
from management.principal.model import Principal
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    WorkspaceEvent,
    WorkspaceEventStream,
    ReplicationEvent,
    ReplicationEventType,
    PartitionKey,
)
from management.tenant_mapping.model import TenantMapping
from management.tenant_service.v2 import V2TenantBootstrapService
from management.workspace.model import Workspace
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    all_of,
    relation,
    resource,
    subject,
)
from migration_tool.models import V2boundresource
from tests.management.role.test_dual_write import RbacFixture
from tests.v2_util import WorkspaceCacheReplicator

from api.models import Tenant, User


class V2TenantBootstrapServiceTest(TestCase):
    """Test cases for Tenant bootstrapping logic."""

    service: V2TenantBootstrapService
    tuples: InMemoryTuples
    replicator: WorkspaceCacheReplicator
    fixture: RbacFixture

    def setUp(self):
        # Clear any existing state first
        self.tuples = InMemoryTuples()
        self.replicator = WorkspaceCacheReplicator(InMemoryRelationReplicator(self.tuples))
        self.service = V2TenantBootstrapService(self.replicator)
        self.fixture = RbacFixture(self.service)
        self.fixture.new_system_role("System Role", ["app1:foo:read"], platform_default=True)
        self.default_group, self.admin_group = seed_group()

    def test_prevents_bootstrapping_public_tenant(self):
        with self.assertRaises(ValueError):
            self.service.bootstrap_tenant(self.fixture.public_tenant)

    def test_relates_workspace_tenant_platform_hierarchy(self):
        bootstrapped = self.fixture.new_tenant(org_id="o1")
        root = self.fixture.root_workspace(bootstrapped.tenant)
        default = self.fixture.default_workspace(bootstrapped.tenant)

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", default.id),
                    relation("parent"),
                    subject("rbac", "workspace", root.id),
                )
            ),
        )
        self.assertEqual(
            0,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", root.id),
                    relation("parent"),
                    subject("rbac", "tenant", "localhost/o1"),
                )
            ),
        )
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "tenant", "localhost/o1"),
                    relation("platform"),
                    subject("rbac", "platform", "stage"),
                )
            ),
        )

    def test_removes_user_from_admin_group_when_no_longer_admin(self):
        bootstrapped = self.fixture.new_tenant(org_id="o1")

        user = User()
        user.user_id = "u1"
        user.org_id = "o1"
        user.admin = True
        user.is_active = True

        self.service.update_user(user)
        user.admin = False
        self.service.update_user(user)

        self.assertEqual(
            0,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", bootstrapped.mapping.default_admin_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", "localhost/u1"),
                )
            ),
        )

        # But is still a regular user
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", bootstrapped.mapping.default_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", "localhost/u1"),
                )
            ),
        )

    def test_adds_user_to_admin_group_when_becoming_admin(self):
        bootstrapped = self.fixture.new_tenant(org_id="o1")

        user = User()
        user.user_id = "u1"
        user.org_id = "o1"
        user.admin = False
        user.is_active = True

        self.service.update_user(user)

        self.assertEqual(
            0,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", bootstrapped.mapping.default_admin_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", "localhost/u1"),
                )
            ),
        )

        # But is a regular user
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", bootstrapped.mapping.default_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", "localhost/u1"),
                )
            ),
        )

        user.admin = True
        self.service.update_user(user)

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", bootstrapped.mapping.default_admin_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", "localhost/u1"),
                )
            ),
        )

        # And is still a regular user
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", bootstrapped.mapping.default_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", "localhost/u1"),
                )
            ),
        )

    def test_will_reuse_default_access_when_already_customized(self):
        """Test that bootstrapping a single tenant with a custom default group will use that group."""
        tenant = self.fixture.new_unbootstrapped_tenant(org_id="o1")
        self.fixture.custom_default_group(tenant)

        user = User()
        user.user_id = "u1"
        user.org_id = "o1"
        user.admin = False
        user.is_active = True

        bootstrapped = self.service.update_user(user)
        default_ws = self.fixture.default_workspace(tenant)

        self.assertEqual(
            0,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", default_ws.id),
                    relation("binding"),
                    subject("rbac", "role_binding", bootstrapped.mapping.default_role_binding_uuid),
                )
            ),
        )
        self.assertEqual(
            0,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "role_binding", bootstrapped.mapping.default_role_binding_uuid),
                    relation("subject"),
                    subject("rbac", "group", bootstrapped.mapping.default_group_uuid, "member"),
                )
            ),
        )
        self.assertEqual(
            tenant.tenant_mapping.default_group_uuid, Group.objects.get(tenant=tenant, platform_default=True).uuid
        )

    def test_adds_default_access_when_not_customized(self):
        tenant = self.fixture.new_unbootstrapped_tenant(org_id="o1")

        user = User()
        user.user_id = "u1"
        user.org_id = "o1"
        user.admin = False
        user.is_active = True

        bootstrapped = self.service.update_user(user)
        default_ws = self.fixture.default_workspace(tenant)

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", default_ws.id),
                    relation("binding"),
                    subject("rbac", "role_binding", bootstrapped.mapping.default_role_binding_uuid),
                )
            ),
        )

    def test_bulk_adding_updating_users(self):
        bootstrapped = self.fixture.new_tenant(org_id="o1")
        self._clear_state()

        # Set up another org with custom default group but not bootstrapped
        # Create the custom default group directly to avoid triggering auto-bootstrap
        # that happens through clone_default_group_in_public_schema with V2_BOOTSTRAP_TENANT=True
        o3_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o3")
        o3_custom_group = Group.objects.create(
            tenant=o3_tenant,
            name="Custom default access",
            platform_default=True,
            system=False,
        )

        self.fixture.new_unbootstrapped_tenant(org_id="o4")

        users = []
        for user_id, username, org_id, admin in [
            ("u1", "username1", "o1", True),
            ("u2", "username2", "o2", False),
            ("u3", "username3", "o2", True),
            ("u4", "username4", "o1", False),
            ("u5", "username5", "o3", False),
            ("u6", "username6", "o4", False),
        ]:
            user = User()
            user.user_id = user_id
            user.username = username
            user.org_id = org_id
            user.admin = admin
            user.is_active = True
            users.append(user)

        self.service.import_bulk_users(users)

        # Admins get 2, otherwise 1
        num_group_membership_tuples = 2 + 1 + 2 + 1 + 1 + 1
        # o1 is already bootstrapped, should get 0
        # existing unbootstrapped custom group tenants get 12
        # new or otherwise unbootstrapped tenants get 21
        num_tenant_bootstrapping_tuples = 0 + 21 + 12 + 21

        self.assertEqual(num_group_membership_tuples + num_tenant_bootstrapping_tuples, self.tuples.count_tuples())

        # Assert user updated for first user with existing tenant
        self.assertAddedToDefaultGroup("localhost/u1", bootstrapped.mapping, and_admin_group=True)  # 2
        # Assert user updated for other user with existing tenant who is not an admin
        self.assertAddedToDefaultGroup("localhost/u4", bootstrapped.mapping)  # 1

        # And also bootstraps the second tenant only once
        # And adds users to default group
        _, mapping, _, _ = self.assertTenantBootstrapped("o2", existing=False)  # 9
        self.assertAddedToDefaultGroup("localhost/u2", mapping)  # 1
        self.assertAddedToDefaultGroup("localhost/u3", mapping, and_admin_group=True)  # 2

        # Bootstraps third tenant but uses existing custom group
        _, mapping, _, _ = self.assertTenantBootstrapped("o3", with_custom_default_group=o3_custom_group)  # 6
        self.assertAddedToDefaultGroup("localhost/u5", mapping)  # 1

        # Bootstraps fourth tenant with new default group
        _, mapping, _, _ = self.assertTenantBootstrapped("o4", existing=True)  # 9
        self.assertAddedToDefaultGroup("localhost/u6", mapping)  # 1

    def test_bulk_import_updates_user_ids_on_principals_but_does_not_add_principals(self):
        bootstrapped = self.fixture.new_tenant(org_id="o1")
        self._clear_state()

        # Set up another org with custom default group but not bootstrapped
        o3_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o3")
        self.fixture.custom_default_group(o3_tenant)

        o4_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o4")

        # Existing bootstrapped tenant with existing user with id
        Principal.objects.create(username="username1", tenant=bootstrapped.tenant, user_id="u1")
        # Another user without an id
        Principal.objects.create(username="username4", tenant=bootstrapped.tenant)

        Principal.objects.create(username="username5", tenant=o3_tenant)
        Principal.objects.create(username="username6", tenant=o4_tenant, user_id="u6")

        users = []
        for user_id, username, org_id, admin in [
            ("u1", "username1", "o1", True),  # Already bootstrapped tenant, existing user with ID
            ("u2", "username2", "o2", False),  # New tenant, so new user, shouldn't get created
            ("u3", "username3", "o2", True),  # New tenant, so new user, shouldn't get created
            ("u4", "username4", "o1", False),  # Already bootstrapped tenant, existing user without ID
            ("u5", "username5", "o3", False),  # Unbootstrapped tenant, existing user without ID
            ("u6", "username6", "o4", False),  # Unbootstrapped tenant, existing user with ID
        ]:
            user = User()
            user.user_id = user_id
            user.username = username
            user.org_id = org_id
            user.admin = admin
            user.is_active = True
            users.append(user)

        self.service.import_bulk_users(users)

        # Assert each principal has the right user ID
        self.assertEqual("u1", Principal.objects.get(username="username1").user_id)
        self.assertEqual("u4", Principal.objects.get(username="username4").user_id)
        self.assertEqual("u5", Principal.objects.get(username="username5").user_id)
        self.assertEqual("u6", Principal.objects.get(username="username6").user_id)

        # Assert no extra principals created
        self.assertEqual(4, Principal.objects.count())

    def test_bulk_import_does_not_update_usernames_in_the_wrong_tenant(self):
        """Principals are only unique by Tenant and username. We cannot look up by just username."""
        # Set up another org with custom default group but not bootstrapped
        o3_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o3")
        o4_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o4")
        o5_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o5")

        # Another principal with the same username but different tenant
        Principal.objects.create(username="username5", tenant=o5_tenant)

        # Existing bootstrapped tenant with existing user with id
        Principal.objects.create(username="username5", tenant=o3_tenant)
        Principal.objects.create(username="username6", tenant=o4_tenant, user_id="u6")

        users = []
        for user_id, username, org_id, admin in [
            ("u5", "username5", "o3", False),  # Unbootstrapped tenant, existing user without ID
            ("u6", "username6", "o4", False),  # Unbootstrapped tenant, existing user with ID
            # o5 tenant should be in the batch, but with a different user
            ("u7", "username6", "o5", False),
        ]:
            user = User()
            user.user_id = user_id
            user.username = username
            user.org_id = org_id
            user.admin = admin
            user.is_active = True
            users.append(user)

        self.service.import_bulk_users(users)

        # Assert username5 in o3 gets the user ID, not the one in o5
        self.assertEqual("u5", Principal.objects.get(username="username5", tenant=o3_tenant).user_id)
        self.assertIsNone(Principal.objects.get(username="username5", tenant=o5_tenant).user_id)
        self.assertEqual("u6", Principal.objects.get(username="username6").user_id)

        # Assert no extra principals created
        self.assertEqual(3, Principal.objects.count())

    def test_force_bootstrap_replicates_already_bootstrapped_unready_tenants(self):
        bootstrapped = self.fixture.new_tenant(org_id="o1")
        self._clear_state()

        original_mapping = TenantMapping.objects.get(tenant=bootstrapped.tenant)
        original_workspaces = list(Workspace.objects.filter(tenant=bootstrapped.tenant))

        self.service.bootstrap_tenant(bootstrapped.tenant, force=True)

        self.assertTenantBootstrapped("o1", existing=False)

        new_mapping = TenantMapping.objects.get(tenant=bootstrapped.tenant)
        new_workspaces = list(Workspace.objects.filter(tenant=bootstrapped.tenant))

        self.assertEqual(original_mapping, new_mapping)
        self.assertCountEqual(original_workspaces, new_workspaces)

    def test_force_bootstrap_replicates_already_bootstrapped_ready_tenants(self):
        bootstrapped = self.fixture.new_tenant(org_id="o1")
        bootstrapped.tenant.ready = True
        bootstrapped.tenant.save()
        self._clear_state()

        original_mapping = TenantMapping.objects.get(tenant=bootstrapped.tenant)
        original_workspaces = list(Workspace.objects.filter(tenant=bootstrapped.tenant))

        self.service.bootstrap_tenant(bootstrapped.tenant, force=True)

        self.assertTenantBootstrapped("o1", existing=True)

        new_mapping = TenantMapping.objects.get(tenant=bootstrapped.tenant)
        new_workspaces = list(Workspace.objects.filter(tenant=bootstrapped.tenant))

        self.assertEqual(original_mapping, new_mapping)
        self.assertCountEqual(original_workspaces, new_workspaces)

    def test_inject_get_user_id_method(self):
        bootstrapped = self.fixture.new_tenant(org_id="o1")

        user = User()
        user.org_id = "o1"
        user.admin = False
        user.is_active = True
        user.user_name = "user_1"

        get_user_id = lambda user: "u1"
        service = V2TenantBootstrapService(InMemoryRelationReplicator(self.tuples), get_user_id=get_user_id)
        bootstrapped = service.update_user(user)

        # But is a regular user
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", bootstrapped.mapping.default_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", "localhost/u1"),
                )
            ),
        )

    def test_bulk_import_workspace(self):
        ws_id_1 = uuid.uuid4()
        ws_id_2 = uuid.uuid4()
        root_id = uuid.uuid4()
        default_id = uuid.uuid4()
        self._clear_state()
        pairs = [
            (ws_id_1, root_id),
            (ws_id_2, default_id),
        ]
        self.service.create_workspace_relationships(pairs)
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", ws_id_1),
                    relation("parent"),
                    subject("rbac", "workspace", root_id),
                )
            ),
        )
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", ws_id_2),
                    relation("parent"),
                    subject("rbac", "workspace", default_id),
                )
            ),
        )

    def test_bulk_bootstrap_simple(self):
        o1_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o1")
        o2_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o2")
        o3_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o3")

        self.service.bootstrap_tenants([o1_tenant, o2_tenant, o3_tenant])

        self.assertTenantBootstrapped(org_id="o1", existing=True)
        self.assertTenantBootstrapped(org_id="o2", existing=True)
        self.assertTenantBootstrapped(org_id="o3", existing=True)

    def test_bulk_bootstrap_force(self):
        o1_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o1")
        o2_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o2")
        o3_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o3")

        self.service.bootstrap_tenants([o1_tenant, o2_tenant, o3_tenant])
        self._clear_state()

        # Re-bootstrapping without forcing should have no effect.
        self.service.bootstrap_tenants([o1_tenant, o2_tenant, o3_tenant])
        self.assertEqual(len(self.tuples), 0)

        # Re-bootstrapping with force should re-replicate relations.
        self.service.bootstrap_tenants([o1_tenant, o2_tenant, o3_tenant], force=True)
        self.assertGreater(len(self.tuples), 0)

        self.assertTenantBootstrapped(org_id="o1", existing=True)
        self.assertTenantBootstrapped(org_id="o2", existing=True)
        self.assertTenantBootstrapped(org_id="o3", existing=True)

    def test_bulk_bootstrap_mixed_state(self):
        o1_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o1")
        o2_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o2")
        o3_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o3")

        self.service.bootstrap_tenant(o1_tenant)
        self._clear_state()

        self.service.bootstrap_tenants([o1_tenant, o2_tenant, o3_tenant])

        # o2 and o3 should have been newly bootstrapped.
        self.assertTenantBootstrapped("o2", existing=True)
        self.assertTenantBootstrapped("o3", existing=True)

        # o1 should not have been re-replicated.
        self.assertEqual(0, self.tuples.count_tuples(resource("rbac", "tenant", o1_tenant.tenant_resource_id())))
        self.assertEqual(0, self.tuples.count_tuples(subject("rbac", "tenant", o1_tenant.tenant_resource_id())))

    def test_bulk_bootstrap_custom_default_group(self):
        o1_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o1")
        o2_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o2")
        o3_tenant = self.fixture.new_unbootstrapped_tenant(org_id="o3")

        # Create custom default group directly to avoid triggering auto-bootstrap
        # that happens through clone_default_group_in_public_schema with V2_BOOTSTRAP_TENANT=True
        o1_group = Group.objects.create(
            tenant=o1_tenant,
            name="Custom default access",
            platform_default=True,
            system=False,
        )

        self.service.bootstrap_tenants([o1_tenant, o2_tenant, o3_tenant])

        self.assertTenantBootstrapped("o1", existing=True, with_custom_default_group=o1_group)
        self.assertTenantBootstrapped("o2", existing=True)
        self.assertTenantBootstrapped("o3", existing=True)

    def assertAddedToDefaultGroup(self, user_id: str, tenant_mapping: TenantMapping, and_admin_group: bool = False):
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", tenant_mapping.default_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", user_id),
                )
            ),
        )
        self.assertEqual(
            1 if and_admin_group else 0,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "group", tenant_mapping.default_admin_group_uuid),
                    relation("member"),
                    subject("rbac", "principal", user_id),
                )
            ),
        )

    def _clear_state(self):
        self.tuples.clear()
        self.replicator.clear_events()

    def assertTenantBootstrapped(
        self, org_id: str, with_custom_default_group: Optional[Group] = None, existing: bool = False
    ) -> Tuple[Tenant, TenantMapping, Workspace, Workspace]:
        tenant: Tenant = Tenant.objects.get(org_id=org_id)
        mapping: TenantMapping = TenantMapping.objects.get(tenant=tenant)
        workspaces: list[Workspace] = list(Workspace.objects.filter(tenant=tenant))
        self.assertEqual(len(workspaces), 2)
        default = Workspace.objects.default(tenant=tenant)
        root = Workspace.objects.root(tenant=tenant)

        platform_default_policy = Policy.objects.get(
            group=Group.objects.get(platform_default=True, tenant=self.fixture.public_tenant)
        )
        admin_default_policy = Policy.objects.get(
            group=Group.objects.get(admin_default=True, tenant=self.fixture.public_tenant)
        )
        custom_default_group = with_custom_default_group

        # If custom default group, must be existing
        if existing or custom_default_group:
            self.assertTrue(tenant.ready, f"Expected existing tenant {org_id} to be ready")
        else:
            self.assertFalse(tenant.ready, f"Expected new tenant {org_id} to not be ready")

        self.assertEqual(
            default.parent_id,
            root.id,
            f"Expected default workspace to be child of root workspace for tenant {org_id}",
        )

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", default.id),
                    relation("parent"),
                    subject("rbac", "workspace", root.id),
                )
            ),
            f"Expected default workspace to be child of root workspace for tenant {org_id}",
        )
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", root.id),
                    relation("tenant"),
                    subject("rbac", "tenant", f"localhost/{org_id}"),
                )
            ),
            f"Expected root workspace to have workspace#tenant@tenant for tenant {org_id}",
        )
        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "tenant", f"localhost/{org_id}"),
                    relation("platform"),
                    subject("rbac", "platform", "stage"),
                )
            ),
            f"Expected tenant {org_id} to have platform",
        )

        def assert_default_role_binding(
            target: V2boundresource,
            group_uuid: uuid.UUID | str,
            role_uuid: uuid.UUID | str,
            role_binding_uuid: uuid.UUID | str,
            count: int = 0,
        ):
            verb = "have" if count != 0 else "not verb"

            self.assertEqual(
                count,
                self.tuples.count_tuples(
                    all_of(
                        resource(target.resource_type[0], target.resource_type[1], target.resource_id),
                        relation("binding"),
                        subject("rbac", "role_binding", str(role_binding_uuid)),
                    )
                ),
                f"Expected target resource to {verb} default role binding for tenant {org_id}",
            )

            self.assertEqual(
                count,
                self.tuples.count_tuples(
                    all_of(
                        resource("rbac", "role_binding", str(role_binding_uuid)),
                        relation("subject"),
                        subject("rbac", "group", str(group_uuid), "member"),
                    )
                ),
                f"Expected default role binding to {verb} default group as subject for tenant {org_id}",
            )

            self.assertEqual(
                count,
                self.tuples.count_tuples(
                    all_of(
                        resource("rbac", "role_binding", str(role_binding_uuid)),
                        relation("role"),
                        subject("rbac", "role", str(role_uuid)),
                    )
                ),
                f"Expected default role binding to {verb} platform default role as subject for tenant {org_id}",
            )

        assert_default_role_binding(
            count=1 if custom_default_group is None else 0,
            target=V2boundresource(("rbac", "workspace"), str(default.id)),
            group_uuid=mapping.default_group_uuid,
            role_uuid=platform_default_policy.uuid,
            role_binding_uuid=mapping.default_role_binding_uuid,
        )

        assert_default_role_binding(
            count=1 if custom_default_group is None else 0,
            target=V2boundresource(("rbac", "workspace"), str(root.id)),
            group_uuid=mapping.default_group_uuid,
            role_uuid=settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID,
            role_binding_uuid=mapping.root_scope_default_role_binding_uuid,
        )

        assert_default_role_binding(
            count=1 if custom_default_group is None else 0,
            target=V2boundresource(("rbac", "tenant"), tenant.tenant_resource_id()),
            group_uuid=mapping.default_group_uuid,
            role_uuid=settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID,
            role_binding_uuid=mapping.tenant_scope_default_role_binding_uuid,
        )

        if custom_default_group is not None:
            # We expect the migrator will take care of this.
            self.assertEqual(
                0,
                self.tuples.count_tuples(
                    all_of(
                        relation("subject"),
                        subject("rbac", "group", custom_default_group.uuid, "member"),
                    )
                ),
                f"Expected no relations to custom default group (leave to migrator) for tenant {org_id}",
            )

        assert_default_role_binding(
            count=1,
            target=V2boundresource(("rbac", "workspace"), str(default.id)),
            group_uuid=mapping.default_admin_group_uuid,
            role_uuid=admin_default_policy.uuid,
            role_binding_uuid=mapping.default_admin_role_binding_uuid,
        )

        assert_default_role_binding(
            count=1,
            target=V2boundresource(("rbac", "workspace"), str(root.id)),
            group_uuid=mapping.default_admin_group_uuid,
            role_uuid=settings.SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID,
            role_binding_uuid=mapping.root_scope_default_admin_role_binding_uuid,
        )

        assert_default_role_binding(
            count=1,
            target=V2boundresource(("rbac", "tenant"), tenant.tenant_resource_id()),
            group_uuid=mapping.default_admin_group_uuid,
            role_uuid=settings.SYSTEM_ADMIN_TENANT_ROLE_UUID,
            role_binding_uuid=mapping.tenant_scope_default_admin_role_binding_uuid,
        )

        default_workspace_events = [
            e
            for e in self.replicator.workspace_events_for(WorkspaceEventStream.STANDARD)
            if e.event_type == ReplicationEventType.CREATE_WORKSPACE and e.workspace["id"] == str(default.id)
        ]

        self.assertEqual(len(default_workspace_events), 1)
        default_workspace_event = default_workspace_events[0]

        self.assertEqual(default_workspace_event.org_id, org_id)
        self.assertEqual(default_workspace_event.account_number, tenant.account_id)
        self.assertEqual(str(default_workspace_event.partition_key), str(PartitionKey.byEnvironment()))
        self.assertEqual(default_workspace_event.workspace["name"], Workspace.SpecialNames.DEFAULT)
        self.assertEqual(default_workspace_event.workspace["type"], Workspace.Types.DEFAULT)

        return tenant, mapping, root, default
