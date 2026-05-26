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
"""Tests for WorkspaceService notify wait logic."""

from collections import deque
from dataclasses import dataclass
from unittest.mock import Mock
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.test.utils import override_settings
from rest_framework import serializers

from api.models import Tenant
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.models import Access, BindingMapping, Group, Permission, Policy, ResourceDefinition, Role, Workspace
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType, WorkspaceEventStream, PartitionKey
from management.role.relation_api_dual_write_handler import RelationApiDualWriteHandler
from management.role.v2_model import RoleV2, CustomRoleV2
from management.role_binding.model import RoleBinding
from management.role_binding.service import RoleBindingService
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from management.workspace.service import WorkspaceService
from migration_tool.in_memory_tuples import (
    InMemoryTuples,
    InMemoryRelationReplicator,
    all_of,
    relation,
    subject_type,
    resource,
)
from tests.management.role.test_dual_write import RbacFixture
from tests.util import assert_v2_tuples_consistent
from tests.v2_util import bootstrap_tenant_for_v2_test, seed_v2_role_from_v1, WorkspaceCacheReplicator


@dataclass
class FakeNotify:
    channel: str
    payload: str


class WorkspaceServiceTest(TestCase):
    """Tests for WorkspaceService._wait_for_notify_post_commit."""

    @patch("internal.pg_notify_wait.select.select")
    @patch("internal.pg_notify_wait.connection")
    def test_wait_for_notify_post_commit_listen_unlisten(self, mock_connection, mock_select):
        # Arrange
        mock_conn = Mock()
        # Start with no notifications; they'll arrive after LISTEN
        mock_conn.notifies = deque()
        mock_conn.poll.side_effect = lambda: None

        mock_connection.ensure_connection = lambda: None
        mock_connection.connection = mock_conn

        mock_cursor = Mock()
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor

        # Make select.select indicate readability and inject a notification just-in-time
        def select_side_effect(*args, **kwargs):
            mock_conn.notifies = deque([FakeNotify("READ_YOUR_WRITES_CHANNEL", "42")])
            return ([mock_conn], [], [])

        mock_select.side_effect = select_side_effect

        service = WorkspaceService()

        # Act
        service._wait_for_notify_post_commit(workspace_id="42")

        # Assert LISTEN/UNLISTEN executed
        executed_sql_calls = [args[0] for args, _ in mock_cursor.execute.call_args_list]
        self.assertTrue(any("LISTEN" in str(c) for c in executed_sql_calls))
        self.assertTrue(any("UNLISTEN" in str(c) for c in executed_sql_calls))

    @patch("internal.pg_notify_wait.select.select")
    @patch("internal.pg_notify_wait.connection")
    def test_wait_for_notify_post_commit_payload_trimmed(self, mock_connection, mock_select):
        # Arrange: notification payload contains extra spaces; should still match
        mock_conn = Mock()
        mock_conn.notifies = deque()
        mock_conn.poll.side_effect = lambda: None

        mock_connection.ensure_connection = lambda: None
        mock_connection.connection = mock_conn

        mock_cursor = Mock()
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor

        def select_side_effect(*args, **kwargs):
            mock_conn.notifies = deque([FakeNotify("READ_YOUR_WRITES_CHANNEL", "  42  ")])
            return ([mock_conn], [], [])

        mock_select.side_effect = select_side_effect

        service = WorkspaceService()

        # Act
        service._wait_for_notify_post_commit(workspace_id="42")

        # Assert LISTEN/UNLISTEN executed
        executed_sql_calls = [args[0] for args, _ in mock_cursor.execute.call_args_list]
        self.assertTrue(any("LISTEN" in str(c) for c in executed_sql_calls))
        self.assertTrue(any("UNLISTEN" in str(c) for c in executed_sql_calls))

    @patch("internal.pg_notify_wait.select.select")
    @patch("internal.pg_notify_wait.connection")
    def test_wait_for_notify_post_commit_timeout(self, mock_connection, mock_select):
        # Arrange: no readability, so we loop until timeout and then exit
        mock_conn = Mock()
        mock_conn.notifies = deque()
        mock_conn.poll.side_effect = lambda: None

        mock_connection.ensure_connection = lambda: None
        mock_connection.connection = mock_conn

        mock_cursor = Mock()
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor

        # select.select returns no readable until the loop times out
        mock_select.return_value = ([], [], [])

        service = WorkspaceService()

        with patch("management.workspace.service.settings.READ_YOUR_WRITES_TIMEOUT_SECONDS", 0.01):
            # Act & Assert - should raise TimeoutError
            with self.assertRaises(TimeoutError) as context:
                service._wait_for_notify_post_commit(workspace_id="999")

            self.assertIn("Read-your-writes consistency check timed out", str(context.exception))

        # Assert LISTEN/UNLISTEN executed despite timeout
        executed_sql_calls = [args[0] for args, _ in mock_cursor.execute.call_args_list]
        self.assertTrue(any("LISTEN" in str(c) for c in executed_sql_calls))
        self.assertTrue(any("UNLISTEN" in str(c) for c in executed_sql_calls))


@override_settings(ATOMIC_RETRY_DISABLED=True)
class WorkspaceServiceTestBase(TestCase):
    """Base test class"""

    @classmethod
    def setUpTestData(cls):
        """Set up workspace service tests."""
        cls.tenant = Tenant.objects.create(tenant_name="Foo Tenant", org_id="1234567", account_id="7654321")

        bootstrap_result = bootstrap_tenant_for_v2_test(cls.tenant)
        cls.default_workspace = bootstrap_result.default_workspace
        cls.root_workspace = bootstrap_result.root_workspace

        cls.standard_workspace = Workspace.objects.create(
            name="Standard", type=Workspace.Types.STANDARD, tenant=cls.tenant, parent=cls.default_workspace
        )
        cls.standard_child_workspace = Workspace.objects.create(
            name="Standard Child", type=Workspace.Types.STANDARD, tenant=cls.tenant, parent=cls.standard_workspace
        )
        cls.ungrouped_workspace = Workspace.objects.create(
            name="Ungrouped", type=Workspace.Types.UNGROUPED_HOSTS, tenant=cls.tenant, parent=cls.default_workspace
        )

        cls.tuples = InMemoryTuples()
        cls.in_memory_replicator = InMemoryRelationReplicator(cls.tuples)

    def setUp(self):
        super().setUp()

        self.replicator = WorkspaceCacheReplicator(OutboxReplicator())
        self.service = WorkspaceService(replicator=self.replicator)

    def tearDown(self):
        with self.subTest(msg="tuple consistency"):
            assert_v2_tuples_consistent(test=self, tuples=self.tuples)

        super().tearDown()


class WorkspaceServiceCreateTests(WorkspaceServiceTestBase):
    """Tests for the create method"""

    def test_create_unique_per_parent(self):
        """Test the create method enforces name uniqueness per tenant"""
        validated_data = {"name": "Standard", "parent_id": self.default_workspace.id}
        with self.assertRaises(serializers.ValidationError) as context:
            self.service.create(validated_data, self.tenant)
        self.assertIn("Can't create workspace with same name within same parent workspace", str(context.exception))

    def test_create_validation_error(self):
        """Test the create method handles other validation errors"""
        with self.assertRaises(serializers.ValidationError) as context:
            self.service.create({}, self.tenant)
        self.assertIn("This field cannot be blank.", str(context.exception))

    def test_create_success_with_parent_id(self):
        """Test the create method successfully with a parent"""
        validated_data = {"name": "Unique Standard Child", "parent_id": self.default_workspace.id}
        workspace = self.service.create(validated_data, self.tenant)
        self.assertEqual(workspace.tenant, self.tenant)

    def test_create_success_without_parent_id(self):
        """Test the create method successfully without a parent"""
        validated_data = {"name": "Unique Standard Child"}
        workspace = self.service.create(validated_data, self.tenant)
        self.assertEqual(workspace.tenant, self.tenant)


class WorkspaceServiceUpdateTests(WorkspaceServiceTestBase):
    """Tests for the update method"""

    def test_update_success(self):
        """Test the update method succeeds"""
        validated_data = {"name": "Bar Name", "description": "Bar Desc"}
        updated_instance = self.service.update(self.standard_workspace, validated_data)
        self.assertEqual(updated_instance.name, validated_data["name"])
        self.assertEqual(updated_instance.description, validated_data["description"])

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate_workspace")
    def test_update_standard_workspace_event(self, replicate_workspace, replicate):
        """Test the standard workspace update will create a Workspace Event."""
        replicate.side_effect = self.in_memory_replicator.replicate

        validated_data = {"name": "Bar Name", "description": "Bar Desc"}
        updated_instance = self.service.update(self.standard_workspace, validated_data)
        self.assertEqual(updated_instance.name, validated_data["name"])
        self.assertEqual(updated_instance.description, validated_data["description"])

        workspace_event = replicate_workspace.call_args[0][0]
        self.assertEqual(workspace_event.workspace["name"], validated_data["name"])
        self.assertEqual(workspace_event.workspace["type"], Workspace.Types.STANDARD)
        self.assertEqual(workspace_event.event_type, ReplicationEventType.UPDATE_WORKSPACE)

        self.assertEqual(len(self.tuples), 0)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_update_default_workspace_event(self, replicate):
        """Test the default workspace update sends both creation and update Workspace Events."""
        replicate.side_effect = self.in_memory_replicator.replicate

        validated_data = {"name": "Bar Name", "description": "Bar Desc"}
        updated_instance = self.service.update(self.default_workspace, validated_data)
        self.assertEqual(updated_instance.name, validated_data["name"])
        self.assertEqual(updated_instance.description, validated_data["description"])

        events = self.replicator.workspace_events_for(WorkspaceEventStream.STANDARD)
        self.assertEqual(len(events), 2)

        self.assertEqual(events[0].event_type, ReplicationEventType.CREATE_WORKSPACE)
        self.assertEqual(events[1].event_type, ReplicationEventType.UPDATE_WORKSPACE)

        for event in events:
            self.assertEqual(event.org_id, self.tenant.org_id)
            self.assertEqual(event.account_number, self.tenant.account_id)
            self.assertEqual(str(event.partition_key), str(PartitionKey.byEnvironment()))
            self.assertEqual(event.workspace["id"], str(updated_instance.id))
            self.assertEqual(event.workspace["type"], Workspace.Types.DEFAULT.value)
            self.assertEqual(event.workspace["name"], updated_instance.name)

        self.assertEqual(len(self.replicator.workspace_events_for(WorkspaceEventStream.BULK)), 0)
        self.assertEqual(len(self.tuples), 0)

    def test_update_parent_id_same(self):
        """Test the update method when the parent is the same"""
        validated_data = {"parent_id": self.standard_workspace.parent_id}
        updated_instance = self.service.update(self.standard_workspace, validated_data)
        self.assertEqual(updated_instance.parent_id, self.standard_workspace.parent_id)

    def test_update_parent_id_different(self):
        """Test the update method when the parent is being changed"""
        validated_data = {"parent_id": self.default_workspace.parent_id}
        with self.assertRaises(serializers.ValidationError) as context:
            self.service.update(self.standard_workspace, validated_data)
        self.assertIn("Can't update the 'parent_id' on a workspace directly", str(context.exception))

    def test_update_root_workspace(self):
        """Test we cannot update the root workspace."""
        validated_data = {"name": "Bar Name", "description": "Bar Desc"}
        with self.assertRaises(serializers.ValidationError) as context:
            self.service.update(self.root_workspace, validated_data)
        self.assertIn("The root workspace cannot be updated.", str(context.exception))

    def test_update_default_workspace(self):
        """Test we can update the default workspace."""
        validated_data = {"name": "Default new name", "description": "Default new description"}
        updated_instance = self.service.update(self.default_workspace, validated_data)
        self.assertEqual(updated_instance.name, validated_data["name"])
        self.assertEqual(updated_instance.description, validated_data["description"])

    @override_settings(WORKSPACE_RESTRICT_DEFAULT_PEERS=True)
    def test_update_default_workspace_with_current_parent_id(self):
        """Test updating the default workspace while confirming its current parent_id (root workspace) succeeds.

        The default workspace's parent is the root workspace. When the caller includes parent_id
        equal to the root workspace's id (as required by the spec), the update must succeed.
        Previously _enforce_hierarchy_depth was called with the root workspace's id, which
        incorrectly raised 'Sub-workspaces may only be created under the default workspace.'
        """
        validated_data = {
            "name": "Updated Default",
            "description": "Updated description",
            "parent_id": self.root_workspace.id,
        }
        updated_instance = self.service.update(self.default_workspace, validated_data)
        self.assertEqual(updated_instance.name, validated_data["name"])
        self.assertEqual(updated_instance.description, validated_data["description"])
        self.assertEqual(updated_instance.parent_id, self.root_workspace.id)

    def test_update_ungrouped_workspace(self):
        """Test we cannot update the ungrouped workspace."""
        validated_data = {"name": "Bar Name", "description": "Bar Desc"}
        with self.assertRaises(serializers.ValidationError) as context:
            self.service.update(self.ungrouped_workspace, validated_data)
        self.assertIn("The ungrouped-hosts workspace cannot be updated.", str(context.exception))

    def test_update_name_unique_per_parent(self):
        """Test we cannot update the workspace name if the same name already exists under same parent."""
        wsA = Workspace.objects.create(
            name="Workspace A", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )
        wsB = Workspace.objects.create(
            name="Workspace B", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )

        # Try to update the Workspace A with name="Workspace B"
        validated_data = {"name": wsB.name}
        with self.assertRaises(serializers.ValidationError) as context:
            self.service.update(wsA, validated_data)
        self.assertIn(
            f"A workspace with the name '{wsB.name}' already exists under same parent.", str(context.exception)
        )


@override_settings(
    ATOMIC_RETRY_DISABLED=True,
    REPLICATION_TO_RELATION_ENABLED=True,
    ROOT_SCOPE_PERMISSIONS="",
    TENANT_SCOPE_PERMISSIONS="",
)
class WorkspaceServiceDestroyTests(WorkspaceServiceTestBase):
    """Tests for the destroy method"""

    def test_destroy_non_standard(self):
        """Test the destroy method on non-standard workspaces"""
        for workspace in (self.default_workspace, self.root_workspace, self.ungrouped_workspace):
            with self.assertRaises(serializers.ValidationError) as context:
                self.service.destroy(workspace)
            self.assertIn(f"Unable to delete {workspace.type} workspace", str(context.exception))

    def test_destroy_when_parent(self):
        """Test the destroy method on parent workspaces"""
        with self.assertRaises(serializers.ValidationError) as context:
            self.service.destroy(self.standard_workspace)
        self.assertIn("Unable to delete due to workspace dependencies", str(context.exception))

    def test_destroy_success(self):
        """Test the destroy method successfully"""
        self.service.destroy(self.standard_child_workspace)
        self.assertFalse(Workspace.objects.filter(id=self.standard_child_workspace.id).exists())

    @override_settings(
        REMOVE_NULL_VALUE=False,
    )
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_destroy_updates_roles_referencing_workspace(self, mock_replicate):
        """Test that destroying a workspace updates roles that reference it"""
        # Setup: Create additional workspaces for testing
        workspace_to_delete = Workspace.objects.create(
            name="Workspace to Delete",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
            parent=self.default_workspace,
        )
        workspace_to_keep = Workspace.objects.create(
            name="Workspace to Keep",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
            parent=self.default_workspace,
        )

        # Create a custom role with resource definitions targeting multiple workspaces
        role = Role.objects.create(
            name="Multi-Workspace Role",
            system=False,
            tenant=self.tenant,
        )

        # Create group and policy
        group = Group.objects.create(name="Test Group", tenant=self.tenant)
        policy = Policy.objects.create(name="Test Policy", group=group, tenant=self.tenant)
        policy.roles.add(role)

        # Create permission
        perm = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )

        # Create access with resource definition targeting both workspaces
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)
        rd = ResourceDefinition.objects.create(
            access=access,
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [str(workspace_to_delete.id), str(workspace_to_keep.id)],
            },
            tenant=self.tenant,
        )

        # Create bindings for the role using dual write handler
        mock_replicate.side_effect = self.in_memory_replicator.replicate
        role_for_binding = Role.objects.select_for_update().get(pk=role.pk)
        dual_write = RelationApiDualWriteHandler(role_for_binding, ReplicationEventType.CREATE_CUSTOM_ROLE)
        dual_write.replicate_new_or_updated_role(role_for_binding)

        # Verify initial state: 2 bindings (one per workspace)
        initial_bindings = BindingMapping.objects.filter(role=role)
        self.assertEqual(initial_bindings.count(), 2, "Should have 2 bindings initially")

        # Verify resource definition has 2 workspaces
        rd.refresh_from_db()
        self.assertEqual(len(rd.attributeFilter["value"]), 2, "Should have 2 workspace IDs")
        self.assertIn(str(workspace_to_delete.id), rd.attributeFilter["value"])
        self.assertIn(str(workspace_to_keep.id), rd.attributeFilter["value"])

        # ACTION: Delete the workspace
        self.service.destroy(workspace_to_delete)

        # VERIFY: Workspace was deleted
        self.assertFalse(
            Workspace.objects.filter(id=workspace_to_delete.id).exists(),
            "Workspace should be deleted",
        )

        # VERIFY: Resource definition was updated to remove deleted workspace
        rd.refresh_from_db()
        self.assertEqual(
            len(rd.attributeFilter["value"]),
            1,
            "Should have 1 workspace ID after deletion",
        )
        self.assertNotIn(
            str(workspace_to_delete.id),
            rd.attributeFilter["value"],
            "Deleted workspace should be removed",
        )
        self.assertIn(
            str(workspace_to_keep.id),
            rd.attributeFilter["value"],
            "Kept workspace should remain",
        )

        # VERIFY: Bindings were updated - should only have 1 binding now
        final_bindings = BindingMapping.objects.filter(role=role)
        self.assertEqual(
            final_bindings.count(),
            1,
            "Should have 1 binding after workspace deletion",
        )

        # VERIFY: The remaining binding is for the workspace we kept
        remaining_binding = final_bindings.first()
        self.assertEqual(
            remaining_binding.resource_id,
            str(workspace_to_keep.id),
            "Remaining binding should be for the kept workspace",
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_destroy_custom_role_v2(self, replicate):
        """Test that bindings from a V1 custom role are properly deleted in a tenant migrated to V2."""
        replicate.side_effect = self.in_memory_replicator.replicate

        workspace = Workspace.objects.create(name="a workspace", tenant=self.tenant)

        fixture = RbacFixture()
        custom_role = fixture.new_custom_role(
            "custom role", fixture.workspace_access(default=[], **{str(workspace.id): ["rbac:*:*"]}), self.tenant
        )
        group, _ = fixture.new_group("a group", self.tenant, ["p1"])

        RelationApiDualWriteHandler(
            role=custom_role, event_type=ReplicationEventType.CREATE_CUSTOM_ROLE
        ).replicate_new_or_updated_role(custom_role)

        group_handler = RelationApiDualWriteGroupHandler(group=group, event_type=ReplicationEventType.ASSIGN_ROLE)
        group_handler.generate_relations_reset_roles([custom_role])
        group_handler.replicate()

        ensure_v2_write_activated(self.tenant)

        def assert_binding_exists(value: bool):
            self.assertEqual(
                int(value),
                self.tuples.count_tuples(
                    all_of(resource("rbac", "workspace", str(workspace.id)), relation("binding"))
                ),
            )

            self.assertEqual(
                value,
                BindingMapping.objects.filter(
                    resource_type_namespace="rbac",
                    resource_type_name="workspace",
                    resource_id=str(workspace.id),
                    role=custom_role,
                ).exists(),
            )

            self.assertEqual(
                value,
                RoleBinding.objects.filter(
                    resource_type="workspace",
                    resource_id=str(workspace.id),
                    role__v1_source=custom_role,
                ).exists(),
            )

        assert_binding_exists(True)

        # This should remove the binding (including both the BindingMapping and RoleBinding models), despite the tenant
        # having been migrated to V2.
        self.service.destroy(workspace)

        assert_binding_exists(False)

    def _do_test_destroy_with_bindings_v2(self, role: RoleV2):
        replicator = InMemoryRelationReplicator(self.tuples)
        binding_service = RoleBindingService(tenant=self.tenant, replicator=replicator)
        workspace_service = WorkspaceService(replicator=replicator)

        group = Group.objects.create(tenant=self.tenant, name="a group")

        parent_uuid = str(self.standard_workspace.id)
        child_uuid = str(self.standard_child_workspace.id)

        def assert_workspace_binding_count(workspace_id: str, count: int):
            self.assertEqual(
                count, RoleBinding.objects.filter(resource_type="workspace", resource_id=workspace_id).count()
            )

            self.assertEqual(
                count,
                self.tuples.count_tuples(
                    all_of(
                        resource("rbac", "workspace", workspace_id),
                        relation("binding"),
                        subject_type("rbac", "role_binding"),
                    )
                ),
            )

        def assert_bindings(parent_count: int, child_count: int):
            # Check both parent and child workspaces to ensure that we don't accidentally delete anything from the
            # parent.
            assert_workspace_binding_count(parent_uuid, parent_count)
            assert_workspace_binding_count(child_uuid, child_count)

        binding_service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.standard_workspace.id),
            subject_type="group",
            subject_id=str(group.uuid),
            role_ids=[str(role.uuid)],
        )

        binding_service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.standard_child_workspace.id),
            subject_type="group",
            subject_id=str(group.uuid),
            role_ids=[str(role.uuid)],
        )

        assert_bindings(parent_count=1, child_count=1)

        workspace_service.destroy(self.standard_child_workspace)
        assert_bindings(1, 0)

    def test_destroy_with_seeded_bindings_v2(self):
        public_tenant = Tenant.objects.get(tenant_name="public")
        v1_role = Role.objects.create(tenant=public_tenant, name="system role", system=True)

        self._do_test_destroy_with_bindings_v2(seed_v2_role_from_v1(v1_role))

    def test_destroy_with_custom_bindings_v2(self):
        role = CustomRoleV2.objects.create(tenant=self.tenant, name="a role")
        self._do_test_destroy_with_bindings_v2(role)


class WorkspaceHierarchyTests(WorkspaceServiceTestBase):
    """Tests for hierarchy enforcement"""

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=2, WORKSPACE_RESTRICT_DEFAULT_PEERS=True)
    def test_enforce_hierarchy_depth_exceeded(self):
        """Test when hierarchy depth is exceeded"""
        with self.assertRaises(serializers.ValidationError) as context:
            self.service._enforce_hierarchy_depth(self.standard_workspace.id, self.tenant)
        self.assertIn(
            f"Workspaces may only nest {settings.WORKSPACE_HIERARCHY_DEPTH_LIMIT} levels deep.", str(context.exception)
        )

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=2, WORKSPACE_RESTRICT_DEFAULT_PEERS=True)
    def test_enforce_hierarchy_depth_is_within_range_but_peer_violation(self):
        """Test when hierarchy depth is within range but there are peer restrictions"""
        with self.assertRaises(serializers.ValidationError) as context:
            self.service._enforce_hierarchy_depth(self.root_workspace.id, self.tenant)
        self.assertIn("Sub-workspaces may only be created under the default workspace.", str(context.exception))

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=2, WORKSPACE_RESTRICT_DEFAULT_PEERS=True)
    def test_enforce_hierarchy_depth_is_within_range_and_no_peer_violation(self):
        """Test when hierarchy depth is within range and no peer restrictions"""
        self.assertEqual(self.service._enforce_hierarchy_depth(self.default_workspace.id, self.tenant), None)

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=100, WORKSPACE_RESTRICT_DEFAULT_PEERS=False)
    def test_enforce_hierarchy_depth_is_within_range_and_peer_validation_is_off(self):
        """Test when hierarchy depth is within range and no peer restrictions"""
        self.assertEqual(self.service._enforce_hierarchy_depth(self.root_workspace.id, self.tenant), None)
