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
"""Test the parity check tasks module."""

from unittest import mock
from unittest.mock import patch

from django.test import override_settings
from management.group.model import Group
from management.models import CustomRoleV2, Permission
from management.principal.model import Principal
from management.tasks import run_kessel_parity_checks_in_worker
from management.tenant_mapping.model import TenantMapping
from management.workspace.model import Workspace
from tests.identity_request import IdentityRequest

BOOTSTRAP_CHECKER_PATH = (
    "management.inventory_checker.inventory_api_check.BootstrappedTenantInventoryChecker.check_bootstrapped_tenant"
)


class ParityCheckTasksTest(IdentityRequest):
    """Test the Kessel parity check tasks."""

    def setUp(self):
        """Set up the parity check task tests."""
        super().setUp()

        # Create workspace hierarchy: root -> default -> child1
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=self.tenant,
            parent=None,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            tenant=self.tenant,
            parent=self.root_workspace,
        )
        self.child_workspace = Workspace.objects.create(
            name="Child Workspace",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
            parent=self.default_workspace,
        )

        # Create tenant mapping so bootstrap checker path is reachable
        self.tenant_mapping = TenantMapping.objects.create(tenant=self.tenant)

        # Mock bootstrap checker by default so existing tests aren't affected
        self._bootstrap_patcher = patch(BOOTSTRAP_CHECKER_PATH, return_value=(True, []))
        self.mock_bootstrap_checker = self._bootstrap_patcher.start()

    def tearDown(self):
        """Clean up mocks."""
        self._bootstrap_patcher.stop()
        super().tearDown()

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="")
    def test_parity_check_task_no_org_ids_configured(self):
        """Test parity check task when no org_ids are configured."""
        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result, {"message": "No org_ids configured"})

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="   ,  ,  ")
    def test_parity_check_task_empty_org_ids_list(self):
        """Test parity check task when org_ids list is empty after stripping."""
        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result, {"message": "No org_ids configured"})

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="999999999")
    def test_parity_check_task_tenant_not_found(self):
        """Test parity check task when tenant is not found."""
        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result["total_tenants"], 0)
        self.assertEqual(result["tenants_not_found"], 1)
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 0)

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_task_no_workspaces_to_check(self, mock_check_workspace):
        """Test parity check task when there are no workspace pairs to check."""
        # Delete child workspace so only root exists (which is excluded from checks)
        self.child_workspace.delete()
        self.default_workspace.delete()

        # Update tenant org_id to match settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        result = run_kessel_parity_checks_in_worker()

        # Should not call checker since no pairs
        mock_check_workspace.assert_not_called()

        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["total_workspace_pairs_checked"], 0)
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)
        self.assertEqual(len(result["tenants_checked"]), 1)
        self.assertEqual(result["tenants_checked"][0]["org_id"], "test_org_id")
        self.assertEqual(result["tenants_checked"][0]["workspace_pairs_checked"], 0)
        self.assertFalse(result["tenants_checked"][0]["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_task_all_checks_pass(self, mock_check_workspace):
        """Test parity check task when all workspace checks pass."""
        # Update tenant org_id to match settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Mock the checker to return True (all checks pass)
        mock_check_workspace.return_value = True

        result = run_kessel_parity_checks_in_worker()

        # Should call checker with 2 workspace pairs (default and child, excluding root)
        mock_check_workspace.assert_called_once()
        called_pairs = mock_check_workspace.call_args[0][0]
        self.assertEqual(len(called_pairs), 2)

        # Verify result
        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["total_workspace_pairs_checked"], 2)
        self.assertEqual(result["passed_tenants"], 1)
        self.assertEqual(result["failed_tenants"], 0)
        self.assertEqual(result["tenants_not_found"], 0)
        self.assertEqual(len(result["tenants_checked"]), 1)
        self.assertEqual(result["tenants_checked"][0]["org_id"], "test_org_id")
        self.assertEqual(result["tenants_checked"][0]["workspace_pairs_checked"], 2)
        self.assertTrue(result["tenants_checked"][0]["passed"])

        # Verify timing data is present
        self.assertIn("duration_seconds", result["tenants_checked"][0])
        self.assertIn("timing", result)
        self.assertIn("avg_seconds", result["timing"])
        self.assertIn("p95_seconds", result["timing"])
        self.assertIn("p99_seconds", result["timing"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_task_checks_fail(self, mock_check_workspace):
        """Test parity check task when workspace checks fail."""
        # Update tenant org_id to match settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Mock the checker to return False (checks failed)
        mock_check_workspace.return_value = False

        result = run_kessel_parity_checks_in_worker()

        # Should call checker
        mock_check_workspace.assert_called_once()

        # Verify result
        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["total_workspace_pairs_checked"], 2)
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)
        self.assertEqual(result["tenants_not_found"], 0)
        self.assertEqual(len(result["tenants_checked"]), 1)
        self.assertEqual(result["tenants_checked"][0]["org_id"], "test_org_id")
        self.assertEqual(result["tenants_checked"][0]["workspace_pairs_checked"], 2)
        self.assertFalse(result["tenants_checked"][0]["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id, test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_task_deduplicates_org_ids(self, mock_check_workspace):
        """Test parity check task deduplicates org_ids and only processes a tenant once."""
        # Update tenant org_id to match the duplicated org_id in settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Mock the checker to return True
        mock_check_workspace.return_value = True

        result = run_kessel_parity_checks_in_worker()

        # Only one tenant should be processed despite duplicate org_id
        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(len(result["tenants_checked"]), 1)
        self.assertEqual(result["tenants_checked"][0]["org_id"], "test_org_id")

        # Ensure the workspace checker is only called once (not twice)
        mock_check_workspace.assert_called_once()

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_1, test_org_2, 999999")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_task_multiple_orgs(self, mock_check_workspace):
        """Test parity check task with multiple org_ids (some valid, some not)."""
        # Update tenant org_id to match first in list
        self.tenant.org_id = "test_org_1"
        self.tenant.save()

        # Create second tenant
        from api.models import Tenant

        tenant2 = Tenant.objects.create(
            tenant_name="acct2",
            account_id="54321",
            org_id="test_org_2",
            ready=True,
        )
        root2 = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=tenant2,
            parent=None,
        )
        default2 = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            tenant=tenant2,
            parent=root2,
        )

        # Mock the checker to return True for first, False for second
        mock_check_workspace.side_effect = [True, False]

        result = run_kessel_parity_checks_in_worker()

        # Should call checker twice (once for each valid tenant)
        self.assertEqual(mock_check_workspace.call_count, 2)

        # Verify result
        self.assertEqual(result["total_tenants"], 2)
        self.assertEqual(result["total_workspace_pairs_checked"], 3)  # 2 from tenant1, 1 from tenant2
        self.assertEqual(result["passed_tenants"], 1)
        self.assertEqual(result["failed_tenants"], 1)
        self.assertEqual(result["tenants_not_found"], 1)  # Third org_id
        self.assertEqual(len(result["tenants_checked"]), 2)

        # Clean up - delete workspaces in correct order (child before parent), then tenant
        default2.delete()
        root2.delete()
        tenant2.delete()

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_task_workspace_pairs_format(self, mock_check_workspace):
        """Test that workspace pairs are formatted correctly as (child_id, parent_id) tuples."""
        # Update tenant org_id
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Mock the checker
        mock_check_workspace.return_value = True

        run_kessel_parity_checks_in_worker()

        # Get the workspace pairs passed to the checker
        called_pairs = mock_check_workspace.call_args[0][0]

        # Verify pairs are tuples of string UUIDs
        for pair in called_pairs:
            self.assertIsInstance(pair, tuple)
            self.assertEqual(len(pair), 2)
            workspace_id, parent_id = pair
            self.assertIsInstance(workspace_id, str)
            self.assertIsInstance(parent_id, str)

        # Verify expected pairs exist (default->root, child->default)
        workspace_ids = [str(self.default_workspace.id), str(self.child_workspace.id)]
        parent_ids = [str(self.root_workspace.id), str(self.default_workspace.id)]

        for workspace_id, parent_id in zip(workspace_ids, parent_ids):
            self.assertIn((workspace_id, parent_id), called_pairs)

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_task_handles_checker_exception(self, mock_check_workspace):
        """Test that the task handles exceptions from the checker gracefully."""
        # Update tenant org_id
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Mock the checker to raise an exception (e.g., gRPC network error)
        mock_check_workspace.side_effect = Exception("gRPC connection timeout")

        result = run_kessel_parity_checks_in_worker()

        # Should have attempted to call checker
        mock_check_workspace.assert_called_once()

        # Verify result shows failure but task completed
        self.assertEqual(result["total_tenants"], 1)  # Counted before exception
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)
        self.assertEqual(len(result["tenants_checked"]), 1)
        self.assertEqual(result["tenants_checked"][0]["org_id"], "test_org_id")
        self.assertEqual(result["tenants_checked"][0]["workspace_pairs_checked"], 2)
        self.assertFalse(result["tenants_checked"][0]["workspace_check_passed"])
        self.assertFalse(result["tenants_checked"][0]["passed"])
        self.assertIn("error", result["tenants_checked"][0])
        self.assertIn("gRPC connection timeout", result["tenants_checked"][0]["error"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.CustomRolePermissionChecker.check_custom_role_permissions"
    )
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_with_custom_roles_all_pass(self, mock_check_workspace, mock_check_role_perms):
        """Test parity check when both workspace and custom role checks pass."""
        # Update tenant org_id to match settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Create custom roles with permissions
        role1 = CustomRoleV2.objects.create(name="role1", tenant=self.tenant)
        perm1 = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        role1.permissions.add(perm1)

        role2 = CustomRoleV2.objects.create(name="role2", tenant=self.tenant)
        perm2 = Permission.objects.create(permission="inventory:hosts:write", tenant=self.tenant)
        role2.permissions.add(perm2)

        # Mock both checkers to return True
        mock_check_workspace.return_value = True
        mock_check_role_perms.return_value = True

        result = run_kessel_parity_checks_in_worker()

        # Verify workspace check was called
        mock_check_workspace.assert_called_once()

        # Verify custom role checks were called (once per role)
        self.assertEqual(mock_check_role_perms.call_count, 2)

        # Verify result
        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["total_workspace_pairs_checked"], 2)
        self.assertEqual(result["total_custom_roles_checked"], 2)
        self.assertEqual(result["passed_tenants"], 1)
        self.assertEqual(result["failed_tenants"], 0)
        self.assertEqual(len(result["tenants_checked"]), 1)

        tenant_result = result["tenants_checked"][0]
        self.assertEqual(tenant_result["org_id"], "test_org_id")
        self.assertTrue(tenant_result["workspace_check_passed"])
        self.assertTrue(tenant_result["custom_role_check_passed"])
        self.assertTrue(tenant_result["passed"])
        self.assertEqual(tenant_result["custom_roles_checked"], 2)
        self.assertEqual(len(tenant_result["role_results"]), 2)

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.CustomRolePermissionChecker.check_custom_role_permissions"
    )
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_custom_role_fails_workspace_passes(self, mock_check_workspace, mock_check_role_perms):
        """Test parity check when workspace passes but custom role check fails."""
        # Update tenant org_id to match settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Create a custom role
        role1 = CustomRoleV2.objects.create(name="role1", tenant=self.tenant)
        perm1 = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        role1.permissions.add(perm1)

        # Mock workspace check to pass, custom role check to fail
        mock_check_workspace.return_value = True
        mock_check_role_perms.return_value = False

        result = run_kessel_parity_checks_in_worker()

        # Verify result - tenant fails because custom role check failed
        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertTrue(tenant_result["workspace_check_passed"])
        self.assertFalse(tenant_result["custom_role_check_passed"])
        self.assertFalse(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.CustomRolePermissionChecker.check_custom_role_permissions"
    )
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_workspace_fails_custom_role_passes(self, mock_check_workspace, mock_check_role_perms):
        """Test parity check when custom role passes but workspace check fails."""
        # Update tenant org_id to match settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Create a custom role
        role1 = CustomRoleV2.objects.create(name="role1", tenant=self.tenant)
        perm1 = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        role1.permissions.add(perm1)

        # Mock workspace check to fail, custom role check to pass
        mock_check_workspace.return_value = False
        mock_check_role_perms.return_value = True

        result = run_kessel_parity_checks_in_worker()

        # Verify result - tenant fails because workspace check failed
        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertFalse(tenant_result["workspace_check_passed"])
        self.assertTrue(tenant_result["custom_role_check_passed"])
        self.assertFalse(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.CustomRolePermissionChecker.check_custom_role_permissions"
    )
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_no_custom_roles(self, mock_check_workspace, mock_check_role_perms):
        """Test parity check when tenant has no custom roles."""
        # Update tenant org_id to match settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Mock workspace check to pass
        mock_check_workspace.return_value = True

        result = run_kessel_parity_checks_in_worker()

        # Verify custom role checker was never called
        mock_check_role_perms.assert_not_called()

        # Verify result - tenant passes based on workspace check only
        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["total_custom_roles_checked"], 0)
        self.assertEqual(result["passed_tenants"], 1)
        self.assertEqual(result["failed_tenants"], 0)

        tenant_result = result["tenants_checked"][0]
        self.assertTrue(tenant_result["workspace_check_passed"])
        self.assertTrue(tenant_result["custom_role_check_passed"])
        self.assertTrue(tenant_result["passed"])
        self.assertEqual(tenant_result["custom_roles_checked"], 0)
        self.assertEqual(len(tenant_result["role_results"]), 0)

    @override_settings(PARITY_CHECK_ENABLED=False, PARITY_CHECK_ORG_IDS="test_org_id")
    @mock.patch("management.tasks.GroupPrincipalInventoryChecker")
    @mock.patch("management.tasks.CustomRolePermissionChecker")
    @mock.patch("management.tasks.WorkspaceRelationInventoryChecker")
    def test_parity_check_task_disabled(
        self, mock_workspace_checker_cls, mock_role_checker_cls, mock_group_checker_cls
    ):
        """Test parity check task returns early when disabled."""
        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result, {"message": "Parity checks disabled"})
        mock_workspace_checker_cls.assert_not_called()
        mock_role_checker_cls.assert_not_called()
        mock_group_checker_cls.assert_not_called()

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.CustomRolePermissionChecker.check_custom_role_permissions"
    )
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_task_handles_custom_role_checker_exception(
        self, mock_check_workspace, mock_check_role_perms
    ):
        """Test that the task handles exceptions from the custom role checker gracefully."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        CustomRoleV2.objects.create(name="role1", tenant=self.tenant)

        mock_check_workspace.return_value = True
        mock_check_role_perms.side_effect = RuntimeError("gRPC connection timeout")

        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)
        self.assertEqual(len(result["tenants_checked"]), 1)
        tenant_result = result["tenants_checked"][0]
        self.assertFalse(tenant_result["passed"])
        self.assertEqual(tenant_result["workspace_pairs_checked"], 2)
        self.assertTrue(tenant_result["workspace_check_passed"])
        self.assertIn("error", tenant_result)
        self.assertIn("gRPC connection timeout", tenant_result["error"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.CustomRolePermissionChecker.check_custom_role_permissions"
    )
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_custom_role_with_no_permissions(self, mock_check_workspace, mock_check_role_perms):
        """Test parity check when custom role has no permissions."""
        # Update tenant org_id to match settings
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        # Create a custom role with no permissions
        CustomRoleV2.objects.create(name="empty-role", tenant=self.tenant)

        # Mock workspace check to pass
        mock_check_workspace.return_value = True
        # Empty permission list returns True
        mock_check_role_perms.return_value = True

        result = run_kessel_parity_checks_in_worker()

        # Verify custom role checker was called once (for the role with no permissions)
        self.assertEqual(mock_check_role_perms.call_count, 1)
        # Verify the call was with empty tuple list
        call_args = mock_check_role_perms.call_args[0]
        self.assertEqual(len(call_args[0]), 0)

        # Verify result
        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["total_custom_roles_checked"], 1)
        self.assertEqual(result["passed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertTrue(tenant_result["passed"])
        self.assertEqual(tenant_result["custom_roles_checked"], 1)
        self.assertEqual(tenant_result["role_results"][0]["permission_count"], 0)

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_bootstrap_parity_success_updates_stats(self, mock_check_workspace):
        """Bootstrap parity success should increment counters and mark tenant as passed."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()
        mock_check_workspace.return_value = True

        bootstrap_details = [
            {"name": "default_workspace_parent", "exists": True},
            {"name": "root_workspace_parent", "exists": True},
        ]
        self.mock_bootstrap_checker.return_value = (True, bootstrap_details)

        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result["total_bootstrap_checks"], 2)
        self.assertEqual(result["passed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertEqual(tenant_result["bootstrap_checks"], 2)
        self.assertTrue(tenant_result["bootstrap_check_passed"])
        self.assertEqual(tenant_result["bootstrap_details"], bootstrap_details)
        self.assertTrue(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_bootstrap_parity_failure_marks_tenant_failed(self, mock_check_workspace):
        """Bootstrap parity failure should mark tenant as failed even when other checks pass."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()
        mock_check_workspace.return_value = True

        bootstrap_details = [
            {"name": "default_workspace_parent", "exists": True},
            {"name": "root_workspace_parent", "exists": False},
        ]
        self.mock_bootstrap_checker.return_value = (False, bootstrap_details)

        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result["total_bootstrap_checks"], 2)
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertEqual(tenant_result["bootstrap_checks"], 2)
        self.assertFalse(tenant_result["bootstrap_check_passed"])
        self.assertFalse(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_bootstrap_parity_no_tenant_mapping(self, mock_check_workspace):
        """Missing TenantMapping should skip bootstrap check and fail the tenant."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()
        mock_check_workspace.return_value = True
        self.tenant_mapping.delete()

        result = run_kessel_parity_checks_in_worker()

        self.mock_bootstrap_checker.assert_not_called()
        self.assertEqual(result["total_bootstrap_checks"], 0)
        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertFalse(tenant_result["bootstrap_check_passed"])
        self.assertFalse(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_bootstrap_parity_missing_root_workspace(self, mock_check_workspace):
        """Missing root workspace should skip bootstrap check and fail the tenant."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()
        mock_check_workspace.return_value = True

        self.child_workspace.delete()
        self.default_workspace.delete()
        self.root_workspace.delete()

        result = run_kessel_parity_checks_in_worker()

        self.mock_bootstrap_checker.assert_not_called()
        self.assertEqual(result["total_bootstrap_checks"], 0)

        tenant_result = result["tenants_checked"][0]
        self.assertFalse(tenant_result["bootstrap_check_passed"])
        self.assertFalse(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_bootstrap_parity_exception_still_counts_tenant(self, mock_check_workspace):
        """When the bootstrap checker raises, the tenant is still counted and marked failed."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()
        mock_check_workspace.return_value = True
        self.mock_bootstrap_checker.side_effect = Exception("gRPC unavailable")

        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result["total_tenants"], 1)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertFalse(tenant_result["passed"])
        self.assertIn("error", tenant_result)
        self.assertIn("gRPC unavailable", tenant_result["error"])
        self.assertEqual(tenant_result["bootstrap_checks"], 0)
        self.assertEqual(tenant_result["bootstrap_details"], [])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch("management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_with_groups_all_pass(self, mock_check_workspace, mock_check_group):
        """Test parity check when group-principal checks pass."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        group1 = Group.objects.create(name="group1", tenant=self.tenant)
        p1 = Principal.objects.create(username="user1", tenant=self.tenant, user_id="uid1")
        p2 = Principal.objects.create(username="user2", tenant=self.tenant, user_id="uid2")
        group1.principals.add(p1, p2)

        mock_check_workspace.return_value = True
        mock_check_group.return_value = {
            "group_uuid": str(group1.uuid),
            "principal_relations": [
                {"id": "localhost/uid1", "relation_exists": True},
                {"id": "localhost/uid2", "relation_exists": True},
            ],
        }

        result = run_kessel_parity_checks_in_worker()

        mock_check_group.assert_called_once()
        self.assertEqual(result["total_groups_checked"], 1)
        self.assertEqual(result["total_group_principal_relations_checked"], 2)
        self.assertEqual(result["passed_tenants"], 1)
        self.assertEqual(result["failed_tenants"], 0)

        tenant_result = result["tenants_checked"][0]
        self.assertTrue(tenant_result["group_principal_check_passed"])
        self.assertEqual(tenant_result["groups_checked"], 1)
        self.assertEqual(len(tenant_result["group_results"]), 1)
        self.assertEqual(tenant_result["group_results"][0]["principal_count"], 2)
        self.assertTrue(tenant_result["group_results"][0]["passed"])
        self.assertTrue(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch("management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_group_principal_fails(self, mock_check_workspace, mock_check_group):
        """Test parity check fails when group-principal relation is missing."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        group1 = Group.objects.create(name="group1", tenant=self.tenant)
        p1 = Principal.objects.create(username="user1", tenant=self.tenant, user_id="uid1")
        group1.principals.add(p1)

        mock_check_workspace.return_value = True
        mock_check_group.return_value = {
            "group_uuid": str(group1.uuid),
            "principal_relations": [
                {"id": "localhost/uid1", "relation_exists": False},
            ],
        }

        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertFalse(tenant_result["group_principal_check_passed"])
        self.assertFalse(tenant_result["group_results"][0]["passed"])
        self.assertFalse(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch("management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_group_no_principals(self, mock_check_workspace, mock_check_group):
        """Test parity check passes for groups with no principals."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        Group.objects.create(name="empty-group", tenant=self.tenant)

        mock_check_workspace.return_value = True

        result = run_kessel_parity_checks_in_worker()

        mock_check_group.assert_not_called()

        self.assertEqual(result["total_groups_checked"], 1)
        self.assertEqual(result["total_group_principal_relations_checked"], 0)
        self.assertEqual(result["passed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertTrue(tenant_result["group_principal_check_passed"])
        self.assertEqual(tenant_result["group_results"][0]["principal_count"], 0)
        self.assertTrue(tenant_result["group_results"][0]["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch("management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_principal_without_user_id(self, mock_check_workspace, mock_check_group):
        """Test that principals without user_id are filtered out gracefully."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        group1 = Group.objects.create(name="group1", tenant=self.tenant)
        p_no_uid = Principal.objects.create(username="no-uid", tenant=self.tenant, user_id=None)
        group1.principals.add(p_no_uid)

        mock_check_workspace.return_value = True

        result = run_kessel_parity_checks_in_worker()

        mock_check_group.assert_not_called()

        tenant_result = result["tenants_checked"][0]
        self.assertTrue(tenant_result["group_principal_check_passed"])
        self.assertEqual(tenant_result["group_results"][0]["principal_count"], 0)
        self.assertTrue(tenant_result["group_results"][0]["passed"])
        self.assertTrue(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch("management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_multiple_groups(self, mock_check_workspace, mock_check_group):
        """Test parity check with multiple groups per tenant."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        group1 = Group.objects.create(name="group1", tenant=self.tenant)
        group2 = Group.objects.create(name="group2", tenant=self.tenant)
        p1 = Principal.objects.create(username="user1", tenant=self.tenant, user_id="uid1")
        p2 = Principal.objects.create(username="user2", tenant=self.tenant, user_id="uid2")
        group1.principals.add(p1)
        group2.principals.add(p2)

        mock_check_workspace.return_value = True
        mock_check_group.side_effect = [
            {
                "group_uuid": str(group1.uuid),
                "principal_relations": [{"id": "localhost/uid1", "relation_exists": True}],
            },
            {
                "group_uuid": str(group2.uuid),
                "principal_relations": [{"id": "localhost/uid2", "relation_exists": False}],
            },
        ]

        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(mock_check_group.call_count, 2)
        self.assertEqual(result["total_groups_checked"], 2)
        self.assertEqual(result["total_group_principal_relations_checked"], 2)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertFalse(tenant_result["group_principal_check_passed"])
        self.assertTrue(tenant_result["group_results"][0]["passed"])
        self.assertFalse(tenant_result["group_results"][1]["passed"])
        self.assertFalse(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch("management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships")
    @patch(
        "management.inventory_checker.inventory_api_check.CustomRolePermissionChecker.check_custom_role_permissions"
    )
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_group_fails_others_pass(self, mock_check_workspace, mock_check_role_perms, mock_check_group):
        """Test that tenant fails when only group-principal check fails."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        role1 = CustomRoleV2.objects.create(name="role1", tenant=self.tenant)
        perm1 = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        role1.permissions.add(perm1)

        group1 = Group.objects.create(name="group1", tenant=self.tenant)
        p1 = Principal.objects.create(username="user1", tenant=self.tenant, user_id="uid1")
        group1.principals.add(p1)

        mock_check_workspace.return_value = True
        mock_check_role_perms.return_value = True
        mock_check_group.return_value = {
            "group_uuid": str(group1.uuid),
            "principal_relations": [{"id": "localhost/uid1", "relation_exists": False}],
        }

        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertTrue(tenant_result["workspace_check_passed"])
        self.assertTrue(tenant_result["custom_role_check_passed"])
        self.assertFalse(tenant_result["group_principal_check_passed"])
        self.assertFalse(tenant_result["passed"])

    @override_settings(PARITY_CHECK_ENABLED=True, PARITY_CHECK_ORG_IDS="test_org_id")
    @patch("management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    def test_parity_check_group_checker_exception(self, mock_check_workspace, mock_check_group):
        """Test that the task handles exceptions from the group-principal checker gracefully."""
        self.tenant.org_id = "test_org_id"
        self.tenant.save()

        group1 = Group.objects.create(name="group1", tenant=self.tenant)
        p1 = Principal.objects.create(username="user1", tenant=self.tenant, user_id="uid1")
        group1.principals.add(p1)

        mock_check_workspace.return_value = True
        mock_check_group.side_effect = RuntimeError("gRPC connection timeout")

        result = run_kessel_parity_checks_in_worker()

        self.assertEqual(result["passed_tenants"], 0)
        self.assertEqual(result["failed_tenants"], 1)

        tenant_result = result["tenants_checked"][0]
        self.assertFalse(tenant_result["passed"])
        self.assertIn("error", tenant_result)
        self.assertIn("gRPC connection timeout", tenant_result["error"])
