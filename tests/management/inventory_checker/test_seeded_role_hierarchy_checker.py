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
"""Tests for SeededRoleHierarchyChecker and generate_seeded_role_hierarchy_tuples."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from kessel.inventory.v1beta2.check_response_pb2 import CheckResponse
from management.group.platform import DefaultGroupNotAvailableError
from management.inventory_checker.inventory_api_check import (
    SeededRoleHierarchyChecker,
    generate_seeded_role_hierarchy_tuples,
)
from management.models import Access, Permission, Role
from management.role.v2_model import SeededRoleV2
from tests.identity_request import IdentityRequest

from api.models import Tenant

INVENTORY_STUB_PATH = "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub"  # noqa: E501


class GenerateSeededRoleHierarchyTuplesTest(IdentityRequest):
    """Tests for generate_seeded_role_hierarchy_tuples()."""

    @classmethod
    def setUpClass(cls):
        """Set up the public tenant and test data."""
        super().setUpClass()
        cls.public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")

    def _create_system_role(
        self, name, admin_default=False, platform_default=False, permission_str="app:resource:read"
    ):
        """Helper to create a V1 system role with a seeded V2 equivalent."""
        role = Role.objects.create(
            name=name,
            system=True,
            admin_default=admin_default,
            platform_default=platform_default,
            tenant=self.public_tenant,
        )
        perm = Permission.objects.create(permission=permission_str, tenant=self.public_tenant)
        Access.objects.create(permission=perm, role=role, tenant=self.public_tenant)
        seeded = SeededRoleV2.objects.create(
            name=name,
            tenant=self.public_tenant,
            v1_source=role,
        )
        seeded.permissions.add(perm)
        return seeded

    def test_no_tuples_for_non_default_role(self):
        """A role that is neither admin_default nor platform_default produces no tuples."""
        seeded = self._create_system_role("Plain Role", admin_default=False, platform_default=False)
        tuples = generate_seeded_role_hierarchy_tuples(seeded)
        self.assertEqual(tuples, [])

    def test_no_tuples_for_missing_v1_source(self):
        """A seeded role with no v1_source produces no tuples."""
        seeded = SeededRoleV2.objects.create(
            name="Orphan Seeded Role",
            tenant=self.public_tenant,
            v1_source=None,
        )
        tuples = generate_seeded_role_hierarchy_tuples(seeded)
        self.assertEqual(tuples, [])

    @patch("management.inventory_checker.inventory_api_check.GlobalPolicyIdService.shared")
    def test_admin_default_produces_one_tuple(self, mock_policy_service):
        """An admin_default role produces one parent-child tuple."""
        parent_uuid = uuid4()
        mock_service = MagicMock()
        mock_policy_service.return_value = mock_service
        mock_service.admin_default_policy_uuid.return_value = parent_uuid

        seeded = self._create_system_role("Admin Role", admin_default=True, platform_default=False)
        tuples = generate_seeded_role_hierarchy_tuples(seeded)

        self.assertEqual(len(tuples), 1)
        self.assertEqual(tuples[0].resource.id, str(parent_uuid))
        self.assertEqual(tuples[0].subject.subject.id, str(seeded.uuid))
        self.assertEqual(tuples[0].relation, "child")

    @patch("management.inventory_checker.inventory_api_check.GlobalPolicyIdService.shared")
    def test_platform_default_produces_one_tuple(self, mock_policy_service):
        """A platform_default role produces one parent-child tuple."""
        parent_uuid = uuid4()
        mock_service = MagicMock()
        mock_policy_service.return_value = mock_service
        mock_service.platform_default_policy_uuid.return_value = parent_uuid

        seeded = self._create_system_role("Platform Role", admin_default=False, platform_default=True)
        tuples = generate_seeded_role_hierarchy_tuples(seeded)

        self.assertEqual(len(tuples), 1)
        self.assertEqual(tuples[0].resource.id, str(parent_uuid))
        self.assertEqual(tuples[0].subject.subject.id, str(seeded.uuid))
        self.assertEqual(tuples[0].relation, "child")

    @patch("management.inventory_checker.inventory_api_check.GlobalPolicyIdService.shared")
    def test_both_defaults_produce_two_tuples(self, mock_policy_service):
        """A role that is both admin_default and platform_default produces two tuples."""
        admin_parent_uuid = uuid4()
        platform_parent_uuid = uuid4()
        mock_service = MagicMock()
        mock_policy_service.return_value = mock_service
        mock_service.admin_default_policy_uuid.return_value = admin_parent_uuid
        mock_service.platform_default_policy_uuid.return_value = platform_parent_uuid

        seeded = self._create_system_role("Both Default Role", admin_default=True, platform_default=True)
        tuples = generate_seeded_role_hierarchy_tuples(seeded)

        self.assertEqual(len(tuples), 2)
        parent_ids = {t.resource.id for t in tuples}
        self.assertEqual(parent_ids, {str(admin_parent_uuid), str(platform_parent_uuid)})
        for t in tuples:
            self.assertEqual(t.subject.subject.id, str(seeded.uuid))
            self.assertEqual(t.relation, "child")

    @patch("management.inventory_checker.inventory_api_check.GlobalPolicyIdService.shared")
    def test_default_group_not_available_returns_partial(self, mock_policy_service):
        """DefaultGroupNotAvailableError is caught and partial results are returned."""
        platform_parent_uuid = uuid4()
        mock_service = MagicMock()
        mock_policy_service.return_value = mock_service
        mock_service.admin_default_policy_uuid.side_effect = DefaultGroupNotAvailableError()
        mock_service.platform_default_policy_uuid.return_value = platform_parent_uuid

        seeded = self._create_system_role("Partial Default Role", admin_default=True, platform_default=True)
        tuples = generate_seeded_role_hierarchy_tuples(seeded)

        self.assertEqual(len(tuples), 1)
        self.assertEqual(tuples[0].resource.id, str(platform_parent_uuid))
        self.assertEqual(tuples[0].subject.subject.id, str(seeded.uuid))


class SeededRoleHierarchyCheckerTest(IdentityRequest):
    """Tests for the SeededRoleHierarchyChecker class."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        self.checker = SeededRoleHierarchyChecker()

    def _create_seeded_role_with_tuples(self, name, admin_default=False, platform_default=False):
        """Helper to create a seeded role and its expected hierarchy tuples."""
        role = Role.objects.create(
            name=name,
            system=True,
            admin_default=admin_default,
            platform_default=platform_default,
            tenant=self.public_tenant,
        )
        perm = Permission.objects.create(
            permission=f"app:{name.lower().replace(' ', '_')}:read",
            tenant=self.public_tenant,
        )
        Access.objects.create(permission=perm, role=role, tenant=self.public_tenant)
        seeded = SeededRoleV2.objects.create(name=name, tenant=self.public_tenant, v1_source=role)
        seeded.permissions.add(perm)
        return seeded

    def _setup_inventory_mocks(self, mock_create_channel, mock_stub_responses):
        """Helper to set up inventory API mocks."""
        mock_stub = MagicMock()
        if isinstance(mock_stub_responses, list):
            mock_stub.Check.side_effect = mock_stub_responses
        else:
            mock_stub.Check.return_value = mock_stub_responses

        mock_channel = MagicMock()
        mock_channel.__enter__.return_value = mock_channel
        mock_channel.__exit__.return_value = None
        mock_create_channel.return_value = mock_channel

        return mock_stub

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @patch("management.inventory_checker.inventory_api_check.GlobalPolicyIdService.shared")
    def test_check_hierarchy_all_exist(self, mock_policy_service, mock_message_to_dict, mock_create_channel):
        """Test that check returns True when all hierarchy relations exist in inventory."""
        parent_uuid = uuid4()
        mock_service = MagicMock()
        mock_policy_service.return_value = mock_service
        mock_service.admin_default_policy_uuid.return_value = parent_uuid

        seeded = self._create_seeded_role_with_tuples("Admin Check Role", admin_default=True)
        hierarchy_tuples = generate_seeded_role_hierarchy_tuples(seeded)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            result = self.checker.check_seeded_role_hierarchy(hierarchy_tuples, str(seeded.uuid))
            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 1)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @patch("management.inventory_checker.inventory_api_check.GlobalPolicyIdService.shared")
    def test_check_hierarchy_missing_relation(self, mock_policy_service, mock_message_to_dict, mock_create_channel):
        """Test that check returns False when a hierarchy relation is missing in inventory."""
        admin_parent = uuid4()
        platform_parent = uuid4()
        mock_service = MagicMock()
        mock_policy_service.return_value = mock_service
        mock_service.admin_default_policy_uuid.return_value = admin_parent
        mock_service.platform_default_policy_uuid.return_value = platform_parent

        seeded = self._create_seeded_role_with_tuples("Both Check Role", admin_default=True, platform_default=True)
        hierarchy_tuples = generate_seeded_role_hierarchy_tuples(seeded)
        self.assertEqual(len(hierarchy_tuples), 2)

        mock_responses = [MagicMock(spec=CheckResponse), MagicMock(spec=CheckResponse)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_responses)
        mock_message_to_dict.side_effect = [
            {"allowed": "ALLOWED_TRUE"},
            {"allowed": "ALLOWED_FALSE"},
        ]

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            result = self.checker.check_seeded_role_hierarchy(hierarchy_tuples, str(seeded.uuid))
            self.assertFalse(result)

    def test_check_hierarchy_empty_tuples(self):
        """Test that check returns True when there are no hierarchy tuples to verify."""
        result = self.checker.check_seeded_role_hierarchy([], str(uuid4()))
        self.assertTrue(result)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @patch("management.inventory_checker.inventory_api_check.GlobalPolicyIdService.shared")
    def test_check_hierarchy_all_missing(self, mock_policy_service, mock_message_to_dict, mock_create_channel):
        """Test that check returns False when all hierarchy relations are missing."""
        parent_uuid = uuid4()
        mock_service = MagicMock()
        mock_policy_service.return_value = mock_service
        mock_service.platform_default_policy_uuid.return_value = parent_uuid

        seeded = self._create_seeded_role_with_tuples("Platform Missing Role", platform_default=True)
        hierarchy_tuples = generate_seeded_role_hierarchy_tuples(seeded)

        mock_responses = [MagicMock(spec=CheckResponse)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_responses)
        mock_message_to_dict.side_effect = [{"allowed": "ALLOWED_FALSE"}]

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            result = self.checker.check_seeded_role_hierarchy(hierarchy_tuples, str(seeded.uuid))
            self.assertFalse(result)
