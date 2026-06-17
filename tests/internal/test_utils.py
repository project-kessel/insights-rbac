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
"""Test the internal utils module."""

import uuid
from datetime import timedelta
from typing import Optional
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from api.cross_access.model import CrossAccountRequest
from api.cross_access.relation_api_dual_write_cross_access_handler import RelationApiDualWriteCrossAccessHandler
from management.group.definer import seed_group
from management.group.model import Group
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.models import BindingMapping, Workspace, Access, Permission
from management.permission.scope_service import Scope
from management.policy.model import Policy
from management.principal.model import Principal
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.definer import seed_roles
from management.role.model import Role, ResourceDefinition
from management.role.v2_model import RoleV2
from management.role.v2_service import RoleV2Service
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from management.role_binding.service import RoleBindingService, CreateBindingRequest
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from management.tenant_service import V2TenantBootstrapService
from migration_tool.in_memory_tuples import (
    InMemoryTuples,
    InMemoryRelationReplicator,
    all_of,
    resource,
    relation,
    subject,
    resource_type,
)
from api.models import Tenant
from internal.utils import (
    replicate_missing_binding_tuples,
    clean_invalid_workspace_resource_definitions,
    remove_unassigned_system_binding_mappings,
    expire_orphaned_cross_account_requests,
)
from migration_tool.models import V2role, V2rolebinding, V2boundresource
from migration_tool.utils import create_relationship
from rbac.settings import ROOT_SCOPE_PERMISSIONS, TENANT_SCOPE_PERMISSIONS
from tests.management.role.test_dual_write import DualWriteTestCase
from tests.util import assert_v2_tuples_consistent, assert_v1_v2_tuples_fully_consistent
from tests.v2_util import seed_v2_role_from_v1, bootstrap_tenant_for_v2_test


@override_settings(ATOMIC_RETRY_DISABLED=True, REPLICATION_TO_RELATION_ENABLED=True)
class ReplicateMissingBindingTuplesTest(TestCase):
    """Test the replicate_missing_binding_tuples function."""

    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="12345")

        self.public_tenant = Tenant.objects.get(tenant_name="public")
        self.tuples = InMemoryTuples()

        # Bootstrap without replicating tuples because various tests do not expect them.
        bootstrap_result = bootstrap_tenant_for_v2_test(tenant=self.tenant)

        self.root_ws = bootstrap_result.root_workspace
        self.default_ws = bootstrap_result.default_workspace

    @override_settings(ROOT_SCOPE_PERMISSIONS="test:resource:read")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_replicate_missing_binding_tuples_for_specific_bindings(self, mock_replicate):
        """Test that function replicates all tuples for specific binding IDs."""
        # Redirect replication to in-memory tuples
        replicator = InMemoryRelationReplicator(self.tuples)
        mock_replicate.side_effect = replicator.replicate

        # Create a system role
        role = Role.objects.create(name="Test Role", system=True, tenant=self.public_tenant)
        perm = Permission.objects.create(permission="test:resource:read", tenant=self.public_tenant)
        Access.objects.create(role=role, permission=perm, tenant=self.public_tenant)

        seed_v2_role_from_v1(role)

        # Create a group
        group = Group.objects.create(name="Test Group", tenant=self.tenant)
        principal = Principal.objects.create(username="some_user", user_id="some_user", tenant=self.tenant)

        # Create binding WITHOUT replication (missing base tuples)
        RelationApiDualWriteGroupHandler(
            group=group, event_type=ReplicationEventType.ASSIGN_ROLE, replicator=NoopReplicator()
        ).generate_relations_reset_roles([role])

        def create_car():
            car = CrossAccountRequest.objects.create(
                target_org=self.tenant.org_id,
                user_id=principal.user_id,
                end_date=timezone.now() + timedelta(days=364),
            )

            car.roles.set([role])

            RelationApiDualWriteCrossAccessHandler(
                cross_account_request=car,
                replicator=NoopReplicator(),
                event_type=ReplicationEventType.APPROVE_CROSS_ACCOUNT_REQUEST,
            ).generate_relations_to_add_roles([role])

        # Test with multiple assignments of the same user.
        create_car()
        create_car()

        binding = BindingMapping.objects.get(role=role)
        binding_id = binding.mappings["id"]

        # Verify initial state: NO tuples exist
        self.assertEqual(len(self.tuples), 0, "Should have NO tuples before fix")

        # Call the function
        results = replicate_missing_binding_tuples(binding_uuids=[binding.mappings["id"]])

        # Verify results
        self.assertEqual(results["bindings_checked"], 1)
        self.assertEqual(results["bindings_fixed"], 1)
        self.assertEqual(results["tuples_added"], 4)  # t_role + t_binding + subject + subject

        # Verify t_role tuple was created
        t_role_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("role"),
            )
        )
        self.assertEqual(len(t_role_tuples), 1, "Should have t_role tuple")

        # Verify t_binding tuple was created
        t_binding_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(self.root_ws.id)),
                relation("binding"),
                subject("rbac", "role_binding", binding_id),
            )
        )
        self.assertEqual(len(t_binding_tuples), 1, "Should have t_binding tuple")

        # Verify group subject tuple was created
        group_subject_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("subject"),
                subject("rbac", "group", str(group.uuid), "member"),
            )
        )
        self.assertEqual(len(group_subject_tuples), 1, "Should have subject group tuple")

        # Verify principal subject tuple was created
        principal_subject_tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("subject"),
                subject("rbac", "principal", principal.principal_resource_id()),
            )
        )
        self.assertEqual(len(principal_subject_tuples), 1, "Should have principal subject tuple")

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_replicate_missing_binding_tuples_idempotent(self, mock_replicate):
        """Test that function is idempotent and safe to run multiple times."""
        # Redirect replication to in-memory tuples
        replicator = InMemoryRelationReplicator(self.tuples)
        mock_replicate.side_effect = replicator.replicate

        # Create a system role
        role = Role.objects.create(name="Test Role Idempotent", system=True, tenant=self.public_tenant)
        perm = Permission.objects.create(permission="test:idempotent:read", tenant=self.public_tenant)
        Access.objects.create(role=role, permission=perm, tenant=self.public_tenant)
        seed_v2_role_from_v1(role)

        group = Group.objects.create(name="a group", tenant=self.tenant)

        # Create binding
        RelationApiDualWriteGroupHandler(
            group=group, event_type=ReplicationEventType.ASSIGN_ROLE, replicator=NoopReplicator()
        ).generate_relations_reset_roles([role])

        binding = BindingMapping.objects.get(role=role)

        self.assertEqual(len(self.tuples), 0)

        # Run once
        results1 = replicate_missing_binding_tuples(binding_uuids=[binding.mappings["id"]])
        self.assertEqual(results1["bindings_fixed"], 1)
        after_first = set(self.tuples)

        # Run again - should be idempotent (duplicates handled by Kessel)
        results2 = replicate_missing_binding_tuples(binding_uuids=[binding.mappings["id"]])
        self.assertEqual(results2["bindings_fixed"], 1)
        after_second = set(self.tuples)

        self.assertEqual(after_first, after_second)

        # Tuples are added again but that's OK (Kessel handles duplicates)
        # The important thing is no error occurs
        self.assertGreater(len(self.tuples), 0, "Should have tuples after running twice")

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_replicate_v2_binding(self, mock_replicate):
        seed_roles()

        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        role_service = RoleV2Service(replicator=NoopReplicator())
        binding_service = RoleBindingService(replicator=NoopReplicator(), tenant=self.tenant)

        group = Group.objects.create(name="a group", tenant=self.tenant)

        role = role_service.create(
            name="a role",
            description="a description",
            permission_data=[{"application": "rbac", "resource_type": "*", "verb": "*"}],
            tenant=self.tenant,
        )

        binding_service.batch_create(
            [
                CreateBindingRequest(
                    role_id=str(role.uuid),
                    resource_type="workspace",
                    resource_id=str(self.default_ws.id),
                    subject_type="group",
                    subject_id=str(group.uuid),
                )
            ]
        )

        binding = RoleBinding.objects.get(role=role)
        binding_id = str(binding.uuid)

        self.assertEqual(len(self.tuples), 0)

        replicate_missing_binding_tuples()

        self.assertEqual(len(self.tuples), 5)

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(self.default_ws.id)),
                    relation("binding"),
                    subject("rbac", "role_binding", binding_id),
                )
            ),
        )

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "role_binding", binding_id),
                    relation("role"),
                    subject("rbac", "role", str(role.uuid)),
                )
            ),
        )

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role.uuid)),
                    relation("rbac_all_all"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role.uuid)),
                    relation("owner"),
                    subject("rbac", "tenant", self.tenant.tenant_resource_id()),
                )
            ),
        )

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "role_binding", binding_id),
                    relation("subject"),
                    subject("rbac", "group", str(group.uuid), "member"),
                )
            ),
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_no_replicate_default_access(self, mock_replicate):
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        # Ensure platform-default roles and groups exist, since we will not create default access RoleBindings if we
        # can't find them.
        seed_roles()
        seed_group()

        # Create tuples for default access role bindings (since we want to check how they are affected).
        bootstrap_tenant_for_v2_test(self.tenant, tuples=self.tuples)

        group = Group.objects.create(name="a group", tenant=self.tenant)

        role = Role.objects.create(name="test role", system=True, tenant=self.public_tenant)
        perm = Permission.objects.create(permission="app:resource:verb", tenant=self.public_tenant)
        Access.objects.create(role=role, permission=perm, tenant=self.public_tenant)

        seed_v2_role_from_v1(role)

        tenant_mapping: TenantMapping = TenantMapping.objects.get(tenant=self.tenant)

        dual_write_handler = RelationApiDualWriteGroupHandler(group=group, event_type=ReplicationEventType.ASSIGN_ROLE)
        dual_write_handler.generate_relations_reset_roles([role])
        dual_write_handler.replicate()

        binding_service = RoleBindingService(tenant=self.tenant)

        # This will create a physical RoleBinding for each of the default access role bindings for the tenant,
        # despite the tenant still being V1.
        binding_service.get_role_bindings_by_subject(
            {
                "subject_type": "group",
                "subject_id": str(group.uuid),
                "resource_type": "workspace",
                "resource_id": str(self.default_ws.id),
            }
        )

        self.assertEqual(BindingMapping.objects.filter(role=role).count(), 1)
        self.assertEqual(
            RoleBinding.objects.filter(tenant=self.tenant).count(), len(Scope) * len(DefaultAccessType) + 1
        )

        # Check that we actually created the default access RoleBinding.
        self.assertTrue(RoleBinding.objects.filter(uuid=tenant_mapping.default_role_binding_uuid).exists())

        def assert_group_binding_in_tuples():
            self.assertEqual(
                1,
                self.tuples.count_tuples(
                    all_of(
                        resource_type("rbac", "role_binding"),
                        relation("subject"),
                        subject("rbac", "group", str(group.uuid), "member"),
                    )
                ),
            )

        def assert_default_binding_in_tuples(value: bool = True):
            self.assertEqual(
                int(value),
                self.tuples.count_tuples(
                    all_of(
                        resource("rbac", "workspace", str(self.default_ws.id)),
                        relation("binding"),
                        subject("rbac", "role_binding", str(tenant_mapping.default_role_binding_uuid)),
                    )
                ),
            )

        assert_group_binding_in_tuples()
        assert_default_binding_in_tuples()

        # Clear tuples so that we can check what is actually re-replicated.
        self.tuples.clear()

        replicate_missing_binding_tuples(tenant=self.tenant)

        assert_group_binding_in_tuples()

        # We should not have re-replicated the default binding.
        assert_default_binding_in_tuples(False)

        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class CleanInvalidResourceDefinitionsTest(TestCase):
    def setUp(self):
        """Set up test data."""
        self.public_tenant = Tenant.objects.get(tenant_name="public")

        self.tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="12345")
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)

        self.default_ws = bootstrap_result.default_workspace
        self.root_ws = bootstrap_result.root_workspace

    @override_settings(REPLICATION_TO_RELATION_ENABLED=False)
    def test_clean_invalid_workspace_resource_definitions_handles_both_operations(self):
        """
        Test cleaning resource definitions with both operation types.

        Tests two scenarios:
        1. operation='in' with list of workspace IDs (all invalid)
        2. operation='equal' with single string workspace ID (invalid)

        Verifies:
        - Invalid IDs removed correctly
        - Value type preserved (list for 'in', string for 'equal')
        - Bindings updated via dual write handler
        """
        # Create a custom role
        role = Role.objects.create(name="Test Role Mixed Operations", system=False, tenant=self.tenant)

        # Permission 1: operation='in' (list value)
        perm_in = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )
        access_in = Access.objects.create(role=role, permission=perm_in, tenant=self.tenant)

        # Permission 2: operation='equal' (string value)
        perm_equal = Permission.objects.create(
            permission="inventory:groups:write",
            application="inventory",
            resource_type="groups",
            verb="write",
            tenant=self.tenant,
        )
        access_equal = Access.objects.create(role=role, permission=perm_equal, tenant=self.tenant)

        # Create RD with operation='in' and ONLY invalid workspace IDs (list)
        fake_ws_id_1 = "95473d62-56ea-4c0c-8945-4f3f6a620669"
        fake_ws_id_2 = "64f65afb-e6f7-4dbb-ba43-ffcdb4a7fb9b"
        rd_in = ResourceDefinition.objects.create(
            access=access_in,
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [fake_ws_id_1, fake_ws_id_2],
            },
            tenant=self.tenant,
        )

        # Create RD with operation='equal' and INVALID workspace ID (string)
        fake_ws_id_3 = "90bd58c4-0579-4cff-937b-a08f69292b29"
        rd_equal = ResourceDefinition.objects.create(
            access=access_equal,
            attributeFilter={
                "key": "group.id",
                "operation": "equal",
                "value": fake_ws_id_3,  # String, not list
            },
            tenant=self.tenant,
        )

        # BEFORE: Verify setup
        self.assertEqual(len(rd_in.attributeFilter["value"]), 2)
        self.assertIsInstance(rd_in.attributeFilter["value"], list)
        self.assertIsInstance(rd_equal.attributeFilter["value"], str)

        # Call the cleanup function
        results = clean_invalid_workspace_resource_definitions()

        # Verify results - both RDs should be fixed
        self.assertEqual(results["resource_definitions_fixed"], 2)
        self.assertEqual(len(results["changes"]), 2)

        # AFTER: Verify RD with operation='in' has empty list
        rd_in.refresh_from_db()
        self.assertEqual(rd_in.attributeFilter["value"], [])
        self.assertIsInstance(rd_in.attributeFilter["value"], list)

        # AFTER: Verify RD with operation='equal' has empty string
        rd_equal.refresh_from_db()
        self.assertEqual(rd_equal.attributeFilter["value"], "")
        self.assertIsInstance(rd_equal.attributeFilter["value"], str)

        # Note: Dual write handler manages binding updates automatically

    def test_clean_preserves_none_value_for_ungrouped_workspace(self):
        """
        Test that None values (representing ungrouped workspace) are preserved.

        Setup: Create RD with None value mixed with valid and invalid workspace IDs
        Action: Call cleanup function
        Verify: None is preserved, valid IDs kept, invalid IDs removed
        """
        # Create a custom role
        role = Role.objects.create(name="Test Role Ungrouped", system=False, tenant=self.tenant)

        perm = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)

        # Create a valid workspace (use STANDARD type to avoid unique constraint)
        valid_ws = Workspace.objects.create(
            tenant=self.tenant, type=Workspace.Types.STANDARD, name="Valid Workspace", parent=self.default_ws
        )

        # Create RD with None (ungrouped), valid UUID, and invalid UUID
        fake_ws_id = "95473d62-56ea-4c0c-8945-4f3f6a620669"
        rd = ResourceDefinition.objects.create(
            access=access,
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [None, str(valid_ws.id), fake_ws_id],
            },
            tenant=self.tenant,
        )

        # BEFORE: Verify setup
        self.assertEqual(len(rd.attributeFilter["value"]), 3)
        self.assertIn(None, rd.attributeFilter["value"])
        self.assertIn(str(valid_ws.id), rd.attributeFilter["value"])
        self.assertIn(fake_ws_id, rd.attributeFilter["value"])

        # Call the cleanup function
        results = clean_invalid_workspace_resource_definitions()

        # Verify results
        self.assertEqual(results["resource_definitions_fixed"], 1)
        self.assertEqual(len(results["changes"]), 1)

        # AFTER: Verify None is preserved, valid ID kept, invalid ID removed
        rd.refresh_from_db()
        self.assertIn(None, rd.attributeFilter["value"], "None value should be preserved for ungrouped workspace")
        self.assertIn(str(valid_ws.id), rd.attributeFilter["value"])
        self.assertNotIn(fake_ws_id, rd.attributeFilter["value"])

    def test_clean_preserves_none_when_all_other_ids_invalid(self):
        """
        Test that None value is preserved even when all other IDs are invalid.

        Setup: Create RD with only None and invalid workspace IDs
        Action: Call cleanup function
        Verify: None is preserved, invalid IDs removed
        """
        # Create a custom role
        role = Role.objects.create(name="Test Role Only Ungrouped", system=False, tenant=self.tenant)

        perm = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)

        # Create RD with only None and invalid UUIDs
        fake_ws_id_1 = "95473d62-56ea-4c0c-8945-4f3f6a620669"
        fake_ws_id_2 = "64f65afb-e6f7-4dbb-ba43-ffcdb4a7fb9b"
        rd = ResourceDefinition.objects.create(
            access=access,
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [None, fake_ws_id_1, fake_ws_id_2],
            },
            tenant=self.tenant,
        )

        # BEFORE: Verify setup
        self.assertEqual(len(rd.attributeFilter["value"]), 3)
        self.assertIn(None, rd.attributeFilter["value"])

        # Call the cleanup function
        results = clean_invalid_workspace_resource_definitions()

        # Verify results
        self.assertEqual(results["resource_definitions_fixed"], 1)

        # AFTER: Verify None is preserved, invalid IDs removed
        rd.refresh_from_db()
        self.assertIn(
            None, rd.attributeFilter["value"], "None value should be preserved even when all other IDs are invalid"
        )
        self.assertNotIn(fake_ws_id_1, rd.attributeFilter["value"])
        self.assertNotIn(fake_ws_id_2, rd.attributeFilter["value"])

    def test_clean_preserves_none_for_equal_operation_ungrouped(self):
        """
        Test that None value is preserved for operation='equal' (ungrouped workspace).

        Setup: Create RD with operation='equal' and value=None
        Action: Call cleanup function (should not process since workspace_ids is empty)
        Verify: None is preserved, RD is not modified

        Note: When value=None, workspace_ids is empty, so the function skips processing
        at line 241-242. This is correct behavior - None doesn't need cleaning.
        """
        # Create a custom role
        role = Role.objects.create(name="Test Role Equal None", system=False, tenant=self.tenant)

        perm = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)

        # Create RD with operation='equal' and value=None (ungrouped workspace)
        rd = ResourceDefinition.objects.create(
            access=access,
            attributeFilter={
                "key": "group.id",
                "operation": "equal",
                "value": None,
            },
            tenant=self.tenant,
        )

        # BEFORE: Verify setup
        self.assertIsNone(rd.attributeFilter["value"])

        # Call the cleanup function - should not process this RD since workspace_ids is empty
        results = clean_invalid_workspace_resource_definitions()

        # Verify no changes (continues at line 241-242 since workspace_ids is empty)
        self.assertEqual(results["resource_definitions_fixed"], 0)

        # AFTER: Verify None is preserved
        rd.refresh_from_db()
        self.assertIsNone(rd.attributeFilter["value"], "None value should be preserved for operation='equal'")

    def test_clean_fixes_int_id(self):
        role = Role.objects.create(name="Test Role Equal None", system=False, tenant=self.tenant)

        perm = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)

        # Create RD with operation='equal' and value=None (ungrouped workspace)
        rd = ResourceDefinition.objects.create(
            access=access,
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [1],
            },
            tenant=self.tenant,
        )

        results = clean_invalid_workspace_resource_definitions()
        self.assertEqual(results["resource_definitions_fixed"], 1)

        rd.refresh_from_db()
        self.assertEqual(rd.attributeFilter["value"], [])

    def test_clean_dry_run_mode(self):
        """
        Test that dry_run mode reports changes without making them.

        Setup: Create RD with invalid workspace IDs
        Action: Call cleanup function with dry_run=True
        Verify: Changes are reported but not applied to database
        """
        # Create a custom role
        role = Role.objects.create(name="Test Role Dry Run", system=False, tenant=self.tenant)

        perm = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)

        # Create RD with invalid workspace IDs
        fake_ws_id_1 = "95473d62-56ea-4c0c-8945-4f3f6a620669"
        fake_ws_id_2 = "64f65afb-e6f7-4dbb-ba43-ffcdb4a7fb9b"
        original_value = [None, fake_ws_id_1, fake_ws_id_2]
        rd = ResourceDefinition.objects.create(
            access=access,
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": original_value,
            },
            tenant=self.tenant,
        )

        # Call the cleanup function in DRY RUN mode
        results = clean_invalid_workspace_resource_definitions(dry_run=True)

        # Verify results show what WOULD be changed
        self.assertTrue(results["dry_run"])
        self.assertEqual(results["resource_definitions_fixed"], 0)  # Nothing actually fixed
        self.assertEqual(len(results["changes"]), 1)  # But one change was detected

        # Verify change info contains before/after details
        change = results["changes"][0]
        self.assertEqual(change["action"], "would_update")
        self.assertEqual(change["original_value"], original_value)
        self.assertEqual(change["new_value"], [None])  # Only None preserved
        self.assertEqual(len(change["invalid_workspaces"]), 2)
        self.assertTrue(change["preserved_none"])

        # CRITICAL: Verify database was NOT changed
        rd.refresh_from_db()
        self.assertEqual(rd.attributeFilter["value"], original_value)
        self.assertIn(fake_ws_id_1, rd.attributeFilter["value"])
        self.assertIn(fake_ws_id_2, rd.attributeFilter["value"])

        # Now run without dry_run to verify it actually makes changes
        results = clean_invalid_workspace_resource_definitions(dry_run=False)
        self.assertFalse(results["dry_run"])
        self.assertEqual(results["resource_definitions_fixed"], 1)
        self.assertEqual(results["changes"][0]["action"], "updated")

        # Verify database WAS changed this time
        rd.refresh_from_db()
        self.assertEqual(rd.attributeFilter["value"], [None])
        self.assertNotIn(fake_ws_id_1, rd.attributeFilter["value"])
        self.assertNotIn(fake_ws_id_2, rd.attributeFilter["value"])

    def test_ignore_v2_tenant(self):
        role = Role.objects.create(name="Test Role Equal None", system=False, tenant=self.tenant)

        perm = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)

        # Create RD with operation='equal' and value=None (ungrouped workspace)
        original_filter = {
            "key": "group.id",
            "operation": "in",
            "value": [1],
        }

        rd = ResourceDefinition.objects.create(
            access=access,
            attributeFilter=original_filter,
            tenant=self.tenant,
        )

        ensure_v2_write_activated(self.tenant)

        result = clean_invalid_workspace_resource_definitions()
        self.assertEqual(result["resource_definitions_fixed"], 0)

        rd.refresh_from_db()
        self.assertEqual(rd.attributeFilter, original_filter)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RemoveOrphanBindingMappingsTest(DualWriteTestCase):
    def _create_empty_binding_mapping(self, resource: Optional[V2boundresource] = None) -> tuple[Role, BindingMapping]:
        if resource is None:
            resource = self.default_workspace_resource()

        role = self.given_v1_system_role(name="system role", permissions=["rbac:*:*"])

        # There's no intended way to create a BindingMapping for a system role with no subjects, so we have to do it
        # manually.
        binding = BindingMapping.for_role_binding(
            V2rolebinding(
                id=str(uuid.uuid4()),
                role=V2role.for_system_role(str(role.uuid)),
                resource=resource,
                groups=[],
                users={},
            ),
            role,
            v2_role=None,
        )

        for tuple in binding.as_tuples():
            self.tuples.add(tuple)

        binding.save()

        return role, binding

    def _do_remove_empty(self):
        remove_unassigned_system_binding_mappings(InMemoryRelationReplicator(self.tuples))

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
    def test_remove_simple(self):
        system_role, system_binding = self._create_empty_binding_mapping()

        for tuple in system_binding.as_tuples():
            self.assertIn(tuple, self.tuples)

        custom_role = self.given_v1_role(name="custom role", default=["rbac:*:*"])

        self.assertEqual(BindingMapping.objects.filter(role=custom_role).count(), 1)
        self.assertEqual(RoleBinding.objects.filter(role__v1_source=custom_role).count(), 1)

        self._do_remove_empty()

        self.assertFalse(BindingMapping.objects.filter(pk=system_binding.pk).exists())

        for tuple in system_binding.as_tuples():
            self.assertNotIn(tuple, self.tuples)

        self.assertEqual(BindingMapping.objects.filter(role=custom_role).count(), 1)
        self.assertEqual(RoleBinding.objects.filter(role__v1_source=custom_role).count(), 1)

    def test_remove_tenant(self):
        role, binding_mapping = self._create_empty_binding_mapping(resource=self.tenant_resource())

        for tuple in binding_mapping.as_tuples():
            self.assertIn(tuple, self.tuples)

        self._do_remove_empty()

        for tuple in binding_mapping.as_tuples():
            self.assertNotIn(tuple, self.tuples)

        self.assertFalse(BindingMapping.objects.filter(pk=binding_mapping.pk).exists())

    def test_remove_with_role_binding(self):
        role, binding_mapping = self._create_empty_binding_mapping()

        role_binding = RoleBinding.objects.create(
            tenant=self.tenant,
            uuid=binding_mapping.mappings["id"],
            role=RoleV2.objects.filter(uuid=role.uuid).get(),
        )

        for tuple in binding_mapping.as_tuples():
            self.assertIn(tuple, self.tuples)

        self._do_remove_empty()

        for tuple in binding_mapping.as_tuples():
            self.assertNotIn(tuple, self.tuples)

        self.assertFalse(BindingMapping.objects.filter(pk=binding_mapping.pk).exists())
        self.assertFalse(RoleBinding.objects.filter(pk=role_binding.pk).exists())

    def test_preserve_assigned_role_binding(self):
        role = self.given_v1_system_role("system role", ["rbac:*:*"])
        group, _ = self.given_group("group", ["p1"])

        self.given_roles_assigned_to_group(group, [role])

        tuples_before = set(self.tuples)
        self.assertEqual(BindingMapping.objects.filter(role=role).count(), 1)
        self.assertEqual(RoleBinding.objects.filter(role__v1_source=role).count(), 1)

        self._do_remove_empty()

        self.assertEqual(set(self.tuples), tuples_before)
        self.assertEqual(BindingMapping.objects.filter(role=role).count(), 1)
        self.assertEqual(RoleBinding.objects.filter(role__v1_source=role).count(), 1)

    def test_fail_on_inconsistent_subjects(self):
        role, binding_mapping = self._create_empty_binding_mapping()

        role_binding = RoleBinding.objects.create(
            tenant=self.tenant,
            uuid=binding_mapping.mappings["id"],
            role=RoleV2.objects.filter(uuid=role.uuid).get(),
        )

        group, _ = self.given_group("group", ["p1"])
        RoleBindingGroup.objects.create(binding=role_binding, group=group)

        tuples_before = set(self.tuples)

        # The migration should fail if it finds an unassigned BindingMapping but an assigned RoleBinding.
        with self.assertRaises(AssertionError):
            self._do_remove_empty()

        self.assertEqual(set(self.tuples), tuples_before)

    def test_no_remove_unexpected_type(self):
        role, binding_mapping = self._create_empty_binding_mapping(V2boundresource(("rbac", "whatever"), "some_id"))
        tuples_before = set(self.tuples)

        self._do_remove_empty()

        self.assertTrue(BindingMapping.objects.filter(pk=binding_mapping.pk).exists())
        self.assertEqual(set(self.tuples), tuples_before)

    def test_no_remove_v2_tenant(self):
        role, binding_mapping = self._create_empty_binding_mapping()
        tuples_before = set(self.tuples)

        ensure_v2_write_activated(self.tenant)

        self._do_remove_empty()

        self.assertTrue(BindingMapping.objects.filter(pk=binding_mapping.pk).exists())
        self.assertEqual(set(self.tuples), tuples_before)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class ExpireOrphanCrossAccountRequests(DualWriteTestCase):
    def setUp(self):
        super().setUp()

        self.source_tenant = self.fixture.new_tenant(org_id="car_source").tenant
        self.source_user_id = "car_user"
        self.source_principal = self.fixture.new_principals_in_tenant(
            users=[self.source_user_id], tenant=self.source_tenant
        )[0]

        self.role = self.given_v1_system_role("a role", ["rbac:*:*"])
        self.group, _ = self.given_group("a group", ["p1"])

    def _do_expire(self):
        expire_orphaned_cross_account_requests(replicator=InMemoryRelationReplicator(self.tuples))

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
    def test_expire_orphans(self):
        self.given_roles_assigned_to_group(self.group, [self.role])

        car = self.given_car(self.source_user_id, [self.role])

        # Currently, we will create a RoleBinding for the CAR. (This was not always the case in the past.)
        self.assertEqual(RoleBinding.objects.filter(tenant=self.tenant).count(), 1)

        # Emulate an orphaned CAR created before the RoleBinding model existed (and orphaned before RoleBindings were
        # backfilled for all-cross account requests). The CAR will still be approved, and a BindingMapping will
        # exist, but no RoleBinding will exist.
        RoleBinding.objects.filter(tenant=self.tenant).delete()
        self.source_principal.delete()

        # We should also have created a BindingMapping.
        mapping = BindingMapping.objects.get(role=self.role)

        self.assertEqual([str(self.group.uuid)], mapping.mappings["groups"])
        self.assertEqual({str(car.source_key()): self.source_user_id}, mapping.mappings["users"])

        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[str(self.role.uuid)],
            for_groups=[str(self.group.uuid)],
            for_principals=[self.source_user_id],
        )

        self._do_expire()

        car.refresh_from_db()
        self.assertEqual(car.status, "expired")

        # The binding should still exist and have the group as a subject.
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[str(self.role.uuid)],
            for_groups=[str(self.group.uuid)],
            for_principals=[],
        )

        # The BindingMapping should have been updated.
        mapping = BindingMapping.objects.get(role=self.role)

        self.assertEqual([str(self.group.uuid)], mapping.mappings["groups"])
        self.assertEqual({}, mapping.mappings["users"])

        # We should not have created a RoleBinding.
        self.assertFalse(RoleBinding.objects.filter(tenant=self.tenant).exists())

    def test_expire_orphans_with_bad_role_binding(self):
        car_a = self.given_car(self.source_user_id, [self.role])
        car_b = self.given_car(self.source_user_id, [self.role])

        # Currently, we will create a RoleBinding for the CAR. (This was not always the case in the past.)
        self.assertEqual(RoleBinding.objects.filter(tenant=self.tenant).count(), 1)

        # Orphan the CAR, but leave an empty RoleBinding around. (This apparently happened in production, possibly as a
        # result of migrating the CAR and then deleting the principal?)
        RoleBindingGroup.objects.filter(binding__tenant=self.tenant).delete()
        RoleBindingPrincipal.objects.filter(binding__tenant=self.tenant).delete()
        self.source_principal.delete()

        # We should also have created a BindingMapping.
        mapping = BindingMapping.objects.get(role=self.role)

        # Ensure that this RoleBinding exists and fetch it so that we can ensure it's deleted later.
        role_binding = RoleBinding.objects.filter(uuid=mapping.mappings["id"]).get()

        def expect_binding_count(count: int):
            self.assertEqual(
                count,
                self.tuples.count_tuples(
                    all_of(resource("rbac", "workspace", self.default_workspace()), relation("binding"))
                ),
            )

        # The CARs should have created a binding in Kessel.
        expect_binding_count(1)

        self._do_expire()

        # The role binding should have been removed from Kessel.
        expect_binding_count(0)

        # Both the BindingMapping and RoleBinding should have been deleted (the former because it now has no subjects,
        # the latter because it was incorrect).

        self.assertFalse(BindingMapping.objects.filter(pk=mapping.pk).exists())
        self.assertFalse(RoleBinding.objects.filter(pk=role_binding.pk).exists())

        car_a.refresh_from_db()
        car_b.refresh_from_db()

        self.assertEqual(car_a.status, "expired")
        self.assertEqual(car_b.status, "expired")

    def test_preserve_not_orphaned(self):
        car = self.given_car(self.source_user_id, [self.role])

        def expect_exists():
            self.expect_1_role_binding_to_workspace(
                self.default_workspace(),
                for_v2_roles=[str(self.role.uuid)],
                for_groups=[],
                for_principals=[self.source_user_id],
            )

            self.assertEqual(
                BindingMapping.objects.get(role=self.role).mappings["users"],
                {str(car.source_key()): self.source_user_id},
            )
            self.assertTrue(
                RoleBindingPrincipal.objects.filter(
                    principal=self.source_principal, binding__role__v1_source=self.role
                ).exists()
            )

        expect_exists()

        self._do_expire()

        car.refresh_from_db()
        self.assertEqual(car.status, "approved")

        expect_exists()

    def test_preserve_v2_tenant(self):
        car = self.given_car(self.source_user_id, [self.role])

        ensure_v2_write_activated(self.tenant)

        def expect_exists():
            self.expect_1_role_binding_to_workspace(
                self.default_workspace(),
                for_v2_roles=[str(self.role.uuid)],
                for_groups=[],
                for_principals=[self.source_user_id],
            )

            self.assertEqual(
                BindingMapping.objects.get(role=self.role).mappings["users"],
                {str(car.source_key()): self.source_user_id},
            )

        expect_exists()

        RoleBinding.objects.filter(tenant=self.tenant).delete()
        self.source_principal.delete()

        self._do_expire()

        car.refresh_from_db()
        self.assertEqual(car.status, "approved")

        expect_exists()


@override_settings(REPLICATION_TO_RELATION_ENABLED=True)
class RemoveLegacyRootWorkspaceTenantParentRelationsTest(TestCase):
    """Tests for remove_legacy_root_workspace_tenant_parent_relations."""

    def setUp(self):
        wait_patcher = patch("internal.pg_notify_wait.wait_for_pg_notify")
        self.mock_wait_for_pg_notify = wait_patcher.start()
        self.addCleanup(wait_patcher.stop)
        self.tenant = Tenant.objects.create(tenant_name="legacy_root_cleanup", org_id="9918877")
        self.tuples = InMemoryTuples()
        bootstrap_tenant_for_v2_test(self.tenant, tuples=self.tuples)

    def test_skipped_when_replication_disabled(self):
        from internal.utils import remove_legacy_root_workspace_tenant_parent_relations

        with override_settings(REPLICATION_TO_RELATION_ENABLED=False):
            result = remove_legacy_root_workspace_tenant_parent_relations()
        self.assertTrue(result["skipped"])

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_enqueues_delete_for_legacy_tuple(self, mock_replicate):
        from internal.utils import remove_legacy_root_workspace_tenant_parent_relations

        root = Workspace.objects.root(tenant=self.tenant)
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        tenant_rid = self.tenant.tenant_resource_id()
        self.tuples.add(
            create_relationship(
                ("rbac", "workspace"),
                str(root.id),
                ("rbac", "tenant"),
                tenant_rid,
                "parent",
            )
        )

        result = remove_legacy_root_workspace_tenant_parent_relations()
        self.assertFalse(result["skipped"])
        self.assertGreaterEqual(result["tenants_processed"], 1)

        remaining = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(root.id)),
                relation("parent"),
                subject("rbac", "tenant", tenant_rid),
            )
        )
        self.assertEqual(len(remaining), 0)
