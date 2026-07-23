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
"""Tests for parity access checker."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from management.group.model import Group
from management.parity_check.checker import (
    ParityAccessChecker,
    ParityCheckResult,
    ParityJobResult,
    run_parity_checks,
)
from management.principal.model import Principal
from management.role.v2_model import CustomRoleV2
from management.role_binding.model import (
    RoleBinding,
    RoleBindingGroup,
    RoleBindingPrincipal,
)
from management.tenant_mapping.model import TenantMapping
from management.workspace.model import Workspace
from tests.identity_request import IdentityRequest

from api.models import Tenant


class ParityCheckResultTests(TestCase):
    """Tests for ParityCheckResult dataclass."""

    def test_no_discrepancy_when_sets_match(self):
        """Test that has_discrepancy returns False when sets match."""
        result = ParityCheckResult(
            org_id="org1",
            principal_id="principal1",
            user_id="user1",
            rbac_workspaces={"ws1", "ws2"},
            pdp_workspaces={"ws1", "ws2"},
            only_in_rbac=set(),
            only_in_pdp=set(),
        )
        self.assertFalse(result.has_discrepancy())

    def test_discrepancy_when_only_in_rbac(self):
        """Test that has_discrepancy returns True when RBAC has extra workspaces."""
        result = ParityCheckResult(
            org_id="org1",
            principal_id="principal1",
            user_id="user1",
            only_in_rbac={"ws1"},
            only_in_pdp=set(),
        )
        self.assertTrue(result.has_discrepancy())

    def test_discrepancy_when_only_in_pdp(self):
        """Test that has_discrepancy returns True when PDP has extra workspaces."""
        result = ParityCheckResult(
            org_id="org1",
            principal_id="principal1",
            user_id="user1",
            only_in_rbac=set(),
            only_in_pdp={"ws1"},
        )
        self.assertTrue(result.has_discrepancy())


class ParityAccessCheckerTests(IdentityRequest):
    """Tests for ParityAccessChecker."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.tenant.org_id = "test-org-12345"
        self.tenant.save()

        # Create tenant mapping with v2 activated
        self.tenant_mapping = TenantMapping.objects.create(
            tenant=self.tenant,
            v2_write_activated_at=timezone.now(),
        )

        # Create root and default workspaces
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=self.tenant,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            parent=self.root_workspace,
            tenant=self.tenant,
        )

        # Create a standard workspace
        self.standard_workspace = Workspace.objects.create(
            name="Test Workspace",
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
            tenant=self.tenant,
        )

        # Create a principal with user_id
        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            user_id="user-12345",
            type=Principal.Types.USER,
        )

        # Create a group and add the principal
        self.group = Group.objects.create(
            name="Test Group",
            tenant=self.tenant,
        )
        self.group.principals.add(self.principal)

    def tearDown(self):
        """Clean up test data."""
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        CustomRoleV2.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()
        TenantMapping.objects.all().delete()
        super().tearDown()

    def test_get_bootstrapped_tenants(self):
        """Test getting bootstrapped tenants."""
        checker = ParityAccessChecker(tenant_sample_size=100)
        tenants = checker.get_bootstrapped_tenants()

        self.assertIn(self.tenant, tenants)

    def test_get_bootstrapped_tenants_includes_non_activated(self):
        """Test that non-activated but bootstrapped tenants are included."""
        # Create another tenant without v2 activation but with TenantMapping
        other_tenant = Tenant.objects.create(
            tenant_name="other-tenant",
            org_id="other-org-12345",
        )
        TenantMapping.objects.create(
            tenant=other_tenant,
            v2_write_activated_at=None,
        )

        checker = ParityAccessChecker(tenant_sample_size=100)
        tenants = checker.get_bootstrapped_tenants()

        # Both tenants should be included since both have TenantMapping
        self.assertIn(self.tenant, tenants)
        self.assertIn(other_tenant, tenants)

        TenantMapping.objects.filter(tenant=other_tenant).delete()
        other_tenant.delete()

    def test_get_principals_for_tenant(self):
        """Test getting principals for a tenant."""
        checker = ParityAccessChecker(principal_sample_size=100)
        principals = checker.get_principals_for_tenant(self.tenant)

        self.assertIn(self.principal, principals)

    def test_get_principals_excludes_without_user_id(self):
        """Test that principals without user_id are excluded."""
        principal_no_user_id = Principal.objects.create(
            username="nouserid",
            tenant=self.tenant,
            user_id=None,
        )

        checker = ParityAccessChecker(principal_sample_size=100)
        principals = checker.get_principals_for_tenant(self.tenant)

        self.assertIn(self.principal, principals)
        self.assertNotIn(principal_no_user_id, principals)

        principal_no_user_id.delete()

    def test_get_rbac_accessible_workspaces_via_direct_binding(self):
        """Test getting accessible workspaces via direct principal binding."""
        # Create a role and role binding
        role = CustomRoleV2.objects.create(
            name="Test Role",
            tenant=self.tenant,
        )
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="workspace",
            resource_id=str(self.standard_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(
            binding=binding,
            principal=self.principal,
            source="test",
        )

        checker = ParityAccessChecker()
        workspaces = checker.get_rbac_accessible_workspaces(self.principal, self.tenant)

        self.assertIn(str(self.standard_workspace.id), workspaces)

    def test_get_rbac_accessible_workspaces_via_group_binding(self):
        """Test getting accessible workspaces via group membership."""
        # Create a role and role binding for the group
        role = CustomRoleV2.objects.create(
            name="Test Role",
            tenant=self.tenant,
        )
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            binding=binding,
            group=self.group,
        )

        checker = ParityAccessChecker()
        workspaces = checker.get_rbac_accessible_workspaces(self.principal, self.tenant)

        self.assertIn(str(self.default_workspace.id), workspaces)

    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_get_pdp_accessible_workspaces(self, mock_checker_class):
        """Test getting accessible workspaces from PDP."""
        mock_checker = MagicMock()
        mock_checker.lookup_accessible_workspaces.return_value = {
            str(self.standard_workspace.id),
            str(self.default_workspace.id),
        }
        mock_checker_class.return_value = mock_checker

        checker = ParityAccessChecker()
        checker.inventory_checker = mock_checker

        workspaces = checker.get_pdp_accessible_workspaces(self.principal)

        self.assertEqual(
            workspaces,
            {str(self.standard_workspace.id), str(self.default_workspace.id)},
        )
        mock_checker.lookup_accessible_workspaces.assert_called_once()

    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_check_principal_parity_match(self, mock_checker_class):
        """Test parity check when RBAC and PDP match."""
        # Create a role binding
        role = CustomRoleV2.objects.create(
            name="Test Role",
            tenant=self.tenant,
        )
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="workspace",
            resource_id=str(self.standard_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(
            binding=binding,
            principal=self.principal,
            source="test",
        )

        # Mock PDP to return the same workspace
        mock_checker = MagicMock()
        mock_checker.lookup_accessible_workspaces.return_value = {str(self.standard_workspace.id)}
        mock_checker_class.return_value = mock_checker

        checker = ParityAccessChecker()
        checker.inventory_checker = mock_checker

        result = checker.check_principal_parity(self.principal, self.tenant)

        self.assertTrue(result.match)
        self.assertFalse(result.has_discrepancy())
        self.assertEqual(result.only_in_rbac, set())
        self.assertEqual(result.only_in_pdp, set())

    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_check_principal_parity_discrepancy(self, mock_checker_class):
        """Test parity check when RBAC and PDP differ."""
        # Create a role binding
        role = CustomRoleV2.objects.create(
            name="Test Role",
            tenant=self.tenant,
        )
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="workspace",
            resource_id=str(self.standard_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(
            binding=binding,
            principal=self.principal,
            source="test",
        )

        # Mock PDP to return a different workspace
        mock_checker = MagicMock()
        mock_checker.lookup_accessible_workspaces.return_value = {str(self.default_workspace.id)}
        mock_checker_class.return_value = mock_checker

        checker = ParityAccessChecker()
        checker.inventory_checker = mock_checker

        result = checker.check_principal_parity(self.principal, self.tenant)

        self.assertFalse(result.match)
        self.assertTrue(result.has_discrepancy())
        self.assertIn(str(self.standard_workspace.id), result.only_in_rbac)
        self.assertIn(str(self.default_workspace.id), result.only_in_pdp)


@override_settings(
    PARITY_CHECK_TENANT_SAMPLE_SIZE=10,
    PARITY_CHECK_PRINCIPAL_SAMPLE_SIZE=50,
)
class RunParityChecksTests(IdentityRequest):
    """Tests for run_parity_checks function."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.tenant.org_id = "test-org-67890"
        self.tenant.save()

        # Create tenant mapping with v2 activated
        self.tenant_mapping = TenantMapping.objects.create(
            tenant=self.tenant,
            v2_write_activated_at=timezone.now(),
        )

        # Create workspaces
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=self.tenant,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            parent=self.root_workspace,
            tenant=self.tenant,
        )

        # Create a principal
        self.principal = Principal.objects.create(
            username="testuser2",
            tenant=self.tenant,
            user_id="user-67890",
            type=Principal.Types.USER,
        )

    def tearDown(self):
        """Clean up test data."""
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        CustomRoleV2.objects.all().delete()
        Principal.objects.all().delete()
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()
        TenantMapping.objects.all().delete()
        super().tearDown()

    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_run_parity_checks_no_discrepancies(self, mock_checker_class):
        """Test running parity checks with no discrepancies."""
        mock_checker = MagicMock()
        mock_checker.lookup_accessible_workspaces.return_value = set()
        mock_checker_class.return_value = mock_checker

        result = run_parity_checks(tenant_sample_size=1, principal_sample_size=1)

        self.assertIsInstance(result, ParityJobResult)
        self.assertGreaterEqual(result.tenants_checked, 1)
        self.assertEqual(len(result.discrepancies), 0)

    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_run_parity_checks_with_discrepancies(self, mock_checker_class):
        """Test running parity checks that finds discrepancies."""
        # Create a role binding that PDP won't know about
        role = CustomRoleV2.objects.create(
            name="Test Role",
            tenant=self.tenant,
        )
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(
            binding=binding,
            principal=self.principal,
            source="test",
        )

        # Mock PDP to return empty set
        mock_checker = MagicMock()
        mock_checker.lookup_accessible_workspaces.return_value = set()
        mock_checker_class.return_value = mock_checker

        result = run_parity_checks(tenant_sample_size=1, principal_sample_size=1)

        self.assertIsInstance(result, ParityJobResult)
        self.assertGreater(len(result.discrepancies), 0)
        self.assertGreater(result.checks_failed, 0)

    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_run_parity_checks_records_metrics(self, mock_checker_class):
        """Test that parity checks record metrics."""
        mock_checker = MagicMock()
        mock_checker.lookup_accessible_workspaces.return_value = set()
        mock_checker_class.return_value = mock_checker

        result = run_parity_checks(tenant_sample_size=1, principal_sample_size=1)

        # Verify metrics were recorded by checking result fields
        self.assertGreater(result.duration_seconds, 0)
        self.assertGreaterEqual(result.tenants_checked, 0)
        self.assertGreaterEqual(result.principals_checked, 0)


class ParityCheckerEdgeCasesTests(IdentityRequest):
    """Tests for edge cases in parity checker."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.tenant.org_id = "edge-case-org"
        self.tenant.save()

    def tearDown(self):
        """Clean up test data."""
        TenantMapping.objects.all().delete()
        Principal.objects.all().delete()
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()
        super().tearDown()

    def test_no_bootstrapped_tenants(self):
        """Test running checks when no bootstrapped tenants exist."""
        # Ensure no tenant mappings exist
        TenantMapping.objects.all().delete()

        result = run_parity_checks()

        self.assertEqual(result.tenants_checked, 0)
        self.assertEqual(result.principals_checked, 0)

    def test_no_principals_with_user_id(self):
        """Test running checks when no principals have user_id."""
        TenantMapping.objects.create(
            tenant=self.tenant,
            v2_write_activated_at=timezone.now(),
        )

        # Create principal without user_id
        Principal.objects.create(
            username="no_user_id",
            tenant=self.tenant,
            user_id=None,
        )

        result = run_parity_checks(tenant_sample_size=1)

        self.assertEqual(result.principals_checked, 0)

    @patch("management.parity_check.checker.WorkspaceInventoryAccessChecker")
    def test_pdp_error_handling(self, mock_checker_class):
        """Test that PDP errors are handled gracefully."""
        TenantMapping.objects.create(
            tenant=self.tenant,
            v2_write_activated_at=timezone.now(),
        )

        # Create workspaces
        root_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=self.tenant,
        )
        default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            parent=root_ws,
            tenant=self.tenant,
        )

        # Create principal
        principal = Principal.objects.create(
            username="error_test",
            tenant=self.tenant,
            user_id="error-user",
        )

        # Mock PDP to raise an exception
        mock_checker = MagicMock()
        mock_checker.lookup_accessible_workspaces.side_effect = Exception("PDP Error")
        mock_checker_class.return_value = mock_checker

        result = run_parity_checks(tenant_sample_size=1, principal_sample_size=1)

        # Should have recorded an error but not crashed
        self.assertGreater(len(result.errors), 0)

        # Clean up
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()
        principal.delete()

    def test_principal_without_principal_resource_id(self):
        """Test that principal without resource_id returns empty set from PDP."""
        TenantMapping.objects.create(
            tenant=self.tenant,
            v2_write_activated_at=timezone.now(),
        )

        # Create workspaces
        root_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=self.tenant,
        )
        default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            parent=root_ws,
            tenant=self.tenant,
        )

        # Create principal with empty user_id (but not None)
        principal = Principal.objects.create(
            username="empty_user_id",
            tenant=self.tenant,
            user_id="",  # Empty string passes the filter but has no resource_id
        )

        checker = ParityAccessChecker()

        # Should return empty set when principal_resource_id is None
        workspaces = checker.get_pdp_accessible_workspaces(principal)

        self.assertEqual(workspaces, set())

        # Clean up
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()
        principal.delete()
