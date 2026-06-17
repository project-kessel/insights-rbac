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
"""Tests for BootstrappedTenantInventoryChecker."""

import uuid
from unittest.mock import MagicMock, patch

from django.test import override_settings
from kessel.inventory.v1beta2.check_response_pb2 import CheckResponse
from management.inventory_checker.inventory_api_check import BootstrappedTenantInventoryChecker
from management.permission.scope_service import Scope
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from tests.identity_request import IdentityRequest

INVENTORY_STUB_PATH = "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub"  # noqa: E501
PLATFORM_ROLE_UUID_PATH = "management.inventory_checker.inventory_api_check.platform_v2_role_uuid_for"

# 3 hierarchy + 6 binding combos x 3 tuples each = 21 base checks
EXPECTED_BASE_CHECK_COUNT = 21
EXPECTED_WITH_UNGROUPED = 22


class BootstrappedTenantCheckerTest(IdentityRequest):
    """Tests for BootstrappedTenantInventoryChecker.check_bootstrapped_tenant."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.tenant_mapping = TenantMapping.objects.create(tenant=self.tenant)
        self.root_workspace_id = str(uuid.uuid4())
        self.default_workspace_id = str(uuid.uuid4())
        self.ungrouped_workspace_id = str(uuid.uuid4())

        self.role_uuids: dict[tuple[DefaultAccessType, Scope], uuid.UUID] = {}
        for access_type in DefaultAccessType:
            for scope in Scope:
                self.role_uuids[(access_type, scope)] = uuid.uuid4()

        self.checker = BootstrappedTenantInventoryChecker()

    def _mock_platform_role_uuid(self, mock_fn: MagicMock) -> None:
        mock_fn.side_effect = lambda access_type, scope, policy_service: self.role_uuids[(access_type, scope)]

    def _setup_inventory_mocks(
        self, mock_create_channel: MagicMock, mock_stub_responses: MagicMock | list[MagicMock]
    ) -> MagicMock:
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

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_all_checks_pass(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """All bootstrap relations exist in inventory."""
        self._mock_platform_role_uuid(mock_role_uuid)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertTrue(passed)
        self.assertEqual(len(checks), EXPECTED_BASE_CHECK_COUNT)
        self.assertTrue(all(c["exists"] for c in checks))

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_all_checks_pass_with_ungrouped(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """All bootstrap relations including ungrouped workspace exist."""
        self._mock_platform_role_uuid(mock_role_uuid)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
                ungrouped_workspace_id=self.ungrouped_workspace_id,
            )

        self.assertTrue(passed)
        self.assertEqual(len(checks), EXPECTED_WITH_UNGROUPED)

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_missing_hierarchy_tuple(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Fails when the default_workspace_parent hierarchy tuple is missing."""
        self._mock_platform_role_uuid(mock_role_uuid)

        responses = [MagicMock(spec=CheckResponse) for _ in range(EXPECTED_BASE_CHECK_COUNT)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, responses)
        allowed_results = [{"allowed": "ALLOWED_TRUE"}] * EXPECTED_BASE_CHECK_COUNT
        # default_workspace_parent is index 0
        allowed_results[0] = {"allowed": "ALLOWED_FALSE"}
        mock_message_to_dict.side_effect = allowed_results

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertFalse(passed)
        failed = [c for c in checks if not c["exists"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "default_workspace_parent")

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_missing_root_scope_binding(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Fails when a root-scope binding tuple is missing."""
        self._mock_platform_role_uuid(mock_role_uuid)

        responses = [MagicMock(spec=CheckResponse) for _ in range(EXPECTED_BASE_CHECK_COUNT)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, responses)
        allowed_results = [{"allowed": "ALLOWED_TRUE"}] * EXPECTED_BASE_CHECK_COUNT
        # user_root_binding: 2 hierarchy + 3 user_default = index 5
        allowed_results[6] = {"allowed": "ALLOWED_FALSE"}
        mock_message_to_dict.side_effect = allowed_results

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertFalse(passed)
        failed = [c for c in checks if not c["exists"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "user_root_binding")

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_missing_tenant_scope_subject(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Fails when the admin tenant-scope subject is missing."""
        self._mock_platform_role_uuid(mock_role_uuid)

        responses = [MagicMock(spec=CheckResponse) for _ in range(EXPECTED_BASE_CHECK_COUNT)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, responses)
        allowed_results = [{"allowed": "ALLOWED_TRUE"}] * EXPECTED_BASE_CHECK_COUNT
        # admin_tenant_subject: 2 hierarchy + 15 (user 9 + admin default 3 + admin root 3) + 2 = index 19
        allowed_results[20] = {"allowed": "ALLOWED_FALSE"}
        mock_message_to_dict.side_effect = allowed_results

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertFalse(passed)
        failed = [c for c in checks if not c["exists"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "admin_tenant_subject")

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_missing_ungrouped_workspace(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Fails when the ungrouped workspace parent relation is missing."""
        self._mock_platform_role_uuid(mock_role_uuid)

        responses = [MagicMock(spec=CheckResponse) for _ in range(EXPECTED_WITH_UNGROUPED)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, responses)
        allowed_results = [{"allowed": "ALLOWED_TRUE"}] * EXPECTED_WITH_UNGROUPED
        # ungrouped is at the end (index 21)
        allowed_results[-1] = {"allowed": "ALLOWED_FALSE"}
        mock_message_to_dict.side_effect = allowed_results

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
                ungrouped_workspace_id=self.ungrouped_workspace_id,
            )

        self.assertFalse(passed)
        failed = [c for c in checks if not c["exists"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "ungrouped_workspace_parent")

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_multiple_failures(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Reports all failures when multiple checks fail."""
        self._mock_platform_role_uuid(mock_role_uuid)

        responses = [MagicMock(spec=CheckResponse) for _ in range(EXPECTED_BASE_CHECK_COUNT)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, responses)
        mock_message_to_dict.side_effect = [{"allowed": "ALLOWED_FALSE"}] * EXPECTED_BASE_CHECK_COUNT

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertFalse(passed)
        failed = [c for c in checks if not c["exists"]]
        self.assertEqual(len(failed), EXPECTED_BASE_CHECK_COUNT)

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_check_names_cover_all_scopes(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Verify check names cover all expected access_type/scope combinations."""
        self._mock_platform_role_uuid(mock_role_uuid)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            _, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        check_names = {c["name"] for c in checks}
        expected_names = {"default_workspace_parent", "root_workspace_tenant", "tenant_platform"}
        for access_type in DefaultAccessType:
            for scope in Scope:
                prefix = f"{access_type.value}_{scope.name.lower()}"
                expected_names.update({f"{prefix}_binding", f"{prefix}_role", f"{prefix}_subject"})

        self.assertEqual(check_names, expected_names)

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_uses_correct_tenant_resource_id(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Verify the tenant resource ID uses PRINCIPAL_USER_DOMAIN/org_id format."""
        self._mock_platform_role_uuid(mock_role_uuid)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, _ = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertTrue(passed)
        calls = mock_stub.Check.call_args_list
        tenant_calls = [
            c[0][0]
            for c in calls
            if getattr(getattr(c[0][0], "object", None), "resource_type", None) == "tenant"
            and c[0][0].object.resource_id == f"localhost/{self.tenant.org_id}"
        ]
        self.assertTrue(len(tenant_calls) > 0, "Expected a Check call for the tenant resource")
        check_request = tenant_calls[0]
        self.assertEqual(check_request.object.resource_type, "tenant")
        self.assertEqual(check_request.subject.resource.resource_id, "stage")
        self.assertEqual(check_request.subject.resource.resource_type, "platform")

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_binding_uses_correct_role_binding_uuid(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Verify binding checks use the correct UUIDs from TenantMapping."""
        self._mock_platform_role_uuid(mock_role_uuid)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, _ = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertTrue(passed)
        calls = mock_stub.Check.call_args_list
        expected_rb_uuid = str(self.tenant_mapping.default_role_binding_uuid)
        rb_calls = [
            c[0][0]
            for c in calls
            if getattr(getattr(c[0][0].subject, "resource", None), "resource_type", None) == "role_binding"
            and c[0][0].subject.resource.resource_id == expected_rb_uuid
        ]
        self.assertTrue(len(rb_calls) > 0, "Expected a Check call for the user's default role binding UUID")

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_role_check_uses_platform_role_uuid(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Verify role checks use the UUID from platform_v2_role_uuid_for."""
        self._mock_platform_role_uuid(mock_role_uuid)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, _ = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertTrue(passed)
        calls = mock_stub.Check.call_args_list
        expected_role_uuid = str(self.role_uuids[(DefaultAccessType.USER, Scope.DEFAULT)])
        role_calls = [
            c[0][0]
            for c in calls
            if getattr(getattr(c[0][0].subject, "resource", None), "resource_type", None) == "role"
            and c[0][0].subject.resource.resource_id == expected_role_uuid
        ]
        self.assertTrue(len(role_calls) > 0, "Expected a Check call for the platform role UUID")

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_subject_check_uses_group_uuid(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Verify subject checks use the correct group UUID from TenantMapping."""
        self._mock_platform_role_uuid(mock_role_uuid)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            passed, _ = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        self.assertTrue(passed)
        calls = mock_stub.Check.call_args_list
        expected_group_uuid = str(self.tenant_mapping.default_group_uuid)
        group_calls = [
            c[0][0]
            for c in calls
            if getattr(getattr(c[0][0].subject, "resource", None), "resource_type", None) == "group"
            and c[0][0].subject.resource.resource_id == expected_group_uuid
            and c[0][0].subject.relation == "member"
        ]
        self.assertTrue(len(group_calls) > 0, "Expected a Check call for the group UUID with member relation")

    @patch(PLATFORM_ROLE_UUID_PATH)
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_readable_check_string_format(self, mock_message_to_dict, mock_create_channel, mock_role_uuid):
        """Verify the check string uses namespace/type:id#relation@namespace/type:id format."""
        self._mock_platform_role_uuid(mock_role_uuid)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            _, checks = self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_workspace_id,
                default_workspace_id=self.default_workspace_id,
            )

        # default_workspace_parent check string
        ws_parent = next(c for c in checks if c["name"] == "default_workspace_parent")
        expected = f"rbac/workspace:{self.default_workspace_id}" f"#parent" f"@rbac/workspace:{self.root_workspace_id}"
        self.assertEqual(ws_parent["check"], expected)

        # subject check includes #member suffix
        subject = next(c for c in checks if c["name"] == "user_default_subject")
        self.assertIn("#member", subject["check"])
