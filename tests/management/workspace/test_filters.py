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
"""Tests for WorkspaceAccessFilterBackend."""

from importlib import reload
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from django.test import TransactionTestCase
from django.test.utils import override_settings
from django.urls import clear_url_caches, reverse
from kessel.inventory.v1beta2 import allowed_pb2
from rest_framework import status
from rest_framework.test import APIClient

from api.models import Tenant
from management.models import Workspace
from management.permissions.system_user_utils import SystemUserAccessResult
from management.workspace.filters import WorkspaceAccessFilterBackend
from management.workspace.service import WorkspaceService
from management.workspace.utils.access import filter_top_level_workspaces
from rbac import urls
from tests.identity_request import BaseIdentityRequest


class WorkspaceAccessFilterBackendUnitTests(TransactionTestCase):
    """Unit tests for WorkspaceAccessFilterBackend."""

    def setUp(self):
        """Set up test fixtures."""
        self.filter_backend = WorkspaceAccessFilterBackend()

    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_filter_disabled_when_v2_flag_off(self, mock_flags):
        """FilterBackend passes through when V2 flag is disabled."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = False

        request = Mock()
        request.permission_tuples = None
        queryset = Mock()
        view = Mock(action="list")

        result = self.filter_backend.filter_queryset(request, queryset, view)

        # Should return unfiltered queryset (v1 behavior with no permission_tuples)
        self.assertEqual(result, queryset)
        queryset.filter.assert_not_called()

    @patch("management.workspace.filters.is_user_allowed_v2")
    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_filters_queryset_by_accessible_workspaces(self, mock_flags, mock_is_user_allowed_v2):
        """FilterBackend filters queryset to accessible workspaces for list action."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = True

        # Mock is_user_allowed_v2 to set permission_tuples on request
        def set_permission_tuples(req, relation, target_workspace):
            req.permission_tuples = [(None, "ws-1"), (None, "ws-2")]
            return True

        mock_is_user_allowed_v2.side_effect = set_permission_tuples

        request = Mock(tenant=Mock())
        request.method = "GET"
        queryset = Mock()
        view = Mock(action="list")

        self.filter_backend.filter_queryset(request, queryset, view)

        # Should call is_user_allowed_v2 with None workspace_id for list
        mock_is_user_allowed_v2.assert_called_once()
        call_args = mock_is_user_allowed_v2.call_args
        self.assertIsNone(call_args[0][2])  # target_workspace should be None

        # Should filter queryset by accessible IDs
        queryset.filter.assert_called()

    @patch("management.workspace.filters.is_user_allowed_v2")
    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_detail_action_uses_workspace_id(self, mock_flags, mock_is_user_allowed_v2):
        """FilterBackend uses workspace_id for detail actions (CheckForUpdate)."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = True
        mock_is_user_allowed_v2.return_value = True

        request = Mock(tenant=Mock())
        request.method = "GET"
        queryset = Mock()
        view = Mock(action="retrieve", kwargs={"pk": "ws-123"})

        self.filter_backend.filter_queryset(request, queryset, view)

        # Should call is_user_allowed_v2 with specific workspace_id
        mock_is_user_allowed_v2.assert_called_once()
        call_args = mock_is_user_allowed_v2.call_args
        self.assertEqual(call_args[0][2], "ws-123")  # target_workspace should be ws-123

        # Should filter queryset to just that workspace
        queryset.filter.assert_called_once()

    @patch("management.workspace.filters.is_user_allowed_v2")
    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_detail_action_returns_empty_when_access_denied(self, mock_flags, mock_is_user_allowed_v2):
        """Detail action returns empty queryset when access is denied."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = True
        mock_is_user_allowed_v2.return_value = False

        request = Mock(tenant=Mock())
        request.method = "GET"
        queryset = Mock()
        view = Mock(action="retrieve", kwargs={"pk": "ws-123"})

        self.filter_backend.filter_queryset(request, queryset, view)

        # Should return empty queryset
        queryset.none.assert_called_once()

    @patch("management.workspace.filters.is_user_allowed_v2")
    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_returns_empty_when_is_user_allowed_raises_exception(self, mock_flags, mock_is_user_allowed_v2):
        """Returns empty queryset when is_user_allowed_v2 raises an exception."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = True
        mock_is_user_allowed_v2.side_effect = Exception("Inventory API connection failed")

        request = Mock()
        request.method = "GET"
        request.user.username = "test"
        request.user.org_id = "org123"
        queryset = Mock()
        view = Mock(action="list")

        self.filter_backend.filter_queryset(request, queryset, view)

        # Should return empty queryset on exception
        queryset.none.assert_called_once()

    @patch("management.workspace.filters.is_user_allowed_v2")
    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_returns_empty_when_access_denied_and_no_permission_tuples(self, mock_flags, mock_is_user_allowed_v2):
        """Returns empty queryset when access denied and no permission_tuples."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = True
        mock_is_user_allowed_v2.return_value = False  # Access denied

        request = Mock(tenant=Mock())
        request.method = "GET"
        # Ensure permission_tuples is not set
        del request.permission_tuples
        queryset = Mock()
        view = Mock(action="list")

        self.filter_backend.filter_queryset(request, queryset, view)

        # Should return empty queryset when access denied
        queryset.none.assert_called_once()

    @patch("management.workspace.filters.is_user_allowed_v2")
    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_returns_all_workspaces_for_system_user(self, mock_flags, mock_is_user_allowed_v2):
        """Returns all workspaces when system user has access but no permission_tuples set."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = True
        # System user: access granted but permission_tuples not set
        mock_is_user_allowed_v2.return_value = True

        request = Mock(tenant=Mock())
        request.method = "GET"
        # System users don't have permission_tuples set
        del request.permission_tuples
        queryset = Mock()
        view = Mock(action="list")

        result = self.filter_backend.filter_queryset(request, queryset, view)

        # Should return full queryset for system users (access granted, no filtering)
        self.assertEqual(result, queryset)
        queryset.none.assert_not_called()
        queryset.filter.assert_not_called()

    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_v1_filter_uses_permission_tuples(self, mock_flags):
        """V1 filtering uses permission_tuples from request."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = False

        request = Mock()
        request.permission_tuples = [("perm1", "ws-1"), ("perm2", "ws-2")]
        queryset = Mock()
        view = Mock(action="list")

        self.filter_backend.filter_queryset(request, queryset, view)

        # Should filter by permission tuples
        queryset.filter.assert_called_once()
        call_args = queryset.filter.call_args
        self.assertIn("id__in", call_args.kwargs)
        self.assertEqual(set(call_args.kwargs["id__in"]), {"ws-1", "ws-2"})

    @patch("management.workspace.filters.is_user_allowed_v2")
    @patch("management.workspace.filters.FEATURE_FLAGS")
    def test_detail_actions_include_all_detail_types(self, mock_flags, mock_is_user_allowed_v2):
        """All detail action types use CheckForUpdate via is_user_allowed_v2 with workspace_id."""
        mock_flags.is_workspace_access_check_v2_enabled.return_value = True
        mock_is_user_allowed_v2.return_value = True

        detail_actions = ["retrieve", "update", "partial_update", "destroy", "move"]

        for action in detail_actions:
            mock_is_user_allowed_v2.reset_mock()

            request = Mock(tenant=Mock())
            request.method = "GET" if action == "retrieve" else "POST"
            queryset = Mock()
            view = Mock(action=action, kwargs={"pk": "ws-456"})

            self.filter_backend.filter_queryset(request, queryset, view)

            # Should call is_user_allowed_v2 with workspace_id for all detail actions
            mock_is_user_allowed_v2.assert_called_once()
            call_args = mock_is_user_allowed_v2.call_args
            self.assertEqual(
                call_args[0][2],
                "ws-456",
                f"Action {action} should pass workspace_id to is_user_allowed_v2",
            )


class TransactionIdentityRequest(BaseIdentityRequest, TransactionTestCase):
    """Identity request test base class for FilterBackend integration tests."""

    pass


@override_settings(V2_APIS_ENABLED=True, WORKSPACE_HIERARCHY_DEPTH_LIMIT=10)
@override_settings(WORKSPACE_ACCESS_CHECK_V2_ENABLED=True)
class WorkspaceFilterBackendIntegrationTests(TransactionIdentityRequest):
    """Integration tests for WorkspaceAccessFilterBackend."""

    def setUp(self):
        """Set up the workspace filter backend tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        self.service = WorkspaceService()
        self.root_workspace = Workspace.objects.create(
            name="Root Workspace",
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        self.default_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            name="Default Workspace",
            description="Default Description",
            parent_id=self.root_workspace.id,
        )
        self.ungrouped_workspace = Workspace.objects.create(
            name="Ungrouped Hosts Workspace",
            description="Ungrouped Hosts Workspace - description",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.UNGROUPED_HOSTS,
        )
        validated_data_standard_ws = {
            "name": "Standard Workspace",
            "description": "Standard Workspace - description",
            "parent_id": self.default_workspace.id,
        }
        self.standard_workspace = self.service.create(validated_data_standard_ws, self.tenant)

    def tearDown(self):
        """Tear down workspace tests."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    def _create_mock_workspace_responses(self, workspace_ids, continuation_token=None):
        """Create mock response objects for StreamedListObjects."""
        responses = []
        for i, ws_id in enumerate(workspace_ids):
            mock_response = MagicMock(object=MagicMock(resource_id=str(ws_id)))
            if continuation_token is not None and i == len(workspace_ids) - 1:
                mock_response.pagination = MagicMock(continuation_token=continuation_token)
            else:
                mock_response.pagination = None
            responses.append(mock_response)
        return responses

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_list_returns_only_accessible_workspaces(self, mock_flag, mock_channel):
        """Test that list endpoint returns only accessible workspaces via FilterBackend."""
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # User can only access standard_workspace
        # Use side_effect to create fresh mock responses each time StreamedListObjects is called
        # This ensures permission check and FilterBackend both get valid responses
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(
            self._create_mock_workspace_responses([self.standard_workspace.id])
        )

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            # Verify only accessible workspaces and their ancestors are returned
            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.standard_workspace.id), returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_detail_returns_404_for_inaccessible_workspace(self, mock_flag, mock_channel):
        """Test that detail endpoint returns 404 for inaccessible workspace (no existence leak)."""
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Inventory API returns access denied via CheckForUpdate
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_FALSE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": self.standard_workspace.id},
            )
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should return 404 (not 403) to prevent existence leakage
            # User cannot distinguish between non-existing and inaccessible workspaces
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

            # Verify CheckForUpdate was called (not StreamedListObjects)
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_list_returns_fallback_workspaces_when_no_real_access(self, mock_flag, mock_channel):
        """Test that list returns 200 with fallback workspaces when user has no real workspace access in V2 mode."""
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # User has no accessible workspaces - FilterBackend will use fallback workspaces
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should return 200 with fallback workspaces (root, default, ungrouped)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.root_workspace.id), returned_ids)
            self.assertIn(str(self.default_workspace.id), returned_ids)
            self.assertIn(str(self.ungrouped_workspace.id), returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_v2_list_non_admin_without_inventory_access_includes_root_and_default(self, mock_flag, mock_channel):
        """Users with no workspace relations in Inventory still list root and default (and ungrouped) workspaces."""
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META
            response = APIClient().get(reverse("v2_management:workspace-list"), format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        types_by_id = {str(row["id"]): row["type"] for row in response.data["data"]}
        self.assertEqual(types_by_id[str(self.root_workspace.id)], Workspace.Types.ROOT)
        self.assertEqual(types_by_id[str(self.default_workspace.id)], Workspace.Types.DEFAULT)
        self.assertEqual(types_by_id[str(self.ungrouped_workspace.id)], Workspace.Types.UNGROUPED_HOSTS)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_list_returns_fallback_when_inventory_ids_missing_in_rbac_db(self, mock_flag, mock_channel):
        """Inventory IDs with no matching Workspace row for this tenant fall back to root/default/ungrouped."""
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()
        orphan_id = uuid4()
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(
            self._create_mock_workspace_responses([orphan_id])
        )

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META
            response = APIClient().get(reverse("v2_management:workspace-list"), format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_ids = {str(ws["id"]) for ws in response.data["data"]}
        self.assertIn(str(self.root_workspace.id), returned_ids)
        self.assertIn(str(self.default_workspace.id), returned_ids)
        self.assertIn(str(self.ungrouped_workspace.id), returned_ids)
        self.assertNotIn(str(orphan_id), returned_ids)

    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=False,
    )
    def test_v1_mode_uses_permission_tuples_filtering(self, mock_flag):
        """Test that V1 mode still works with permission_tuples filtering."""
        # In V1 mode, the permission check sets permission_tuples
        # The FilterBackend should use these for filtering
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=True)
        headers = request_context["request"].META

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(url, format="json", **headers)

        # Admin in V1 should have access to all workspaces
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class WorkspacePermissionAndFilterBackendIntegrationTests(TransactionTestCase):
    """Test that permission class and FilterBackend work together correctly."""

    def test_permission_class_does_not_have_filter_methods(self):
        """Permission class should not contain queryset filtering logic."""
        from management.permissions.workspace_access import WorkspaceAccessPermission

        permission = WorkspaceAccessPermission()
        # Permission class delegates to FilterBackend for queryset operations
        self.assertFalse(hasattr(permission, "filter_queryset"))
        self.assertFalse(hasattr(permission, "get_accessible_workspaces"))

    @patch("management.permissions.workspace_access.permission_from_request")
    @patch("management.permissions.workspace_access.workspace_from_request")
    @patch("management.permissions.workspace_access.check_system_user_access")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_has_permission_allows_list_for_non_system_users(
        self, mock_flag, mock_system, mock_ws_from_req, mock_perm_from_req
    ):
        """Permission class should allow list requests - FilterBackend handles filtering."""
        from management.permissions.workspace_access import WorkspaceAccessPermission

        mock_system.return_value = Mock(result=SystemUserAccessResult.NOT_SYSTEM_USER)
        mock_ws_from_req.return_value = None  # List action has no workspace ID
        mock_perm_from_req.return_value = "view"

        permission = WorkspaceAccessPermission()
        request = Mock()
        request.method = "GET"
        view = Mock(action="list", kwargs={})

        # Permission should allow - filtering happens in FilterBackend
        result = permission.has_permission(request, view)
        self.assertTrue(result)


class FilterTopLevelWorkspacesTests(TransactionTestCase):
    """Unit tests for filter_top_level_workspaces optimization."""

    def setUp(self):
        """Set up test fixtures with a workspace hierarchy."""
        self.tenant = Tenant.objects.create(
            tenant_name="test_tenant",
            org_id="test_org_id",
            ready=True,
        )

        # Create hierarchy:
        #   root
        #   └── default
        #       ├── ws_a
        #       │   └── ws_a1
        #       │       └── ws_a1a
        #       └── ws_b
        self.root = Workspace.objects.create(
            name="Root",
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        self.default = Workspace.objects.create(
            name="Default",
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=self.root,
        )
        self.ws_a = Workspace.objects.create(
            name="Workspace A",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default,
        )
        self.ws_a1 = Workspace.objects.create(
            name="Workspace A1",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.ws_a,
        )
        self.ws_a1a = Workspace.objects.create(
            name="Workspace A1A",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.ws_a1,
        )
        self.ws_b = Workspace.objects.create(
            name="Workspace B",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default,
        )

    def tearDown(self):
        """Clean up test fixtures."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()
        Tenant.objects.all().delete()

    def test_single_workspace_is_top_level(self):
        """Single workspace with no ancestors in set is top-level."""
        queryset = Workspace.objects.filter(id=self.ws_a1a.id)
        result = filter_top_level_workspaces(queryset)

        self.assertEqual(list(result), [self.ws_a1a])

    def test_parent_and_child_only_parent_is_top_level(self):
        """When parent and child are in set, only parent is top-level."""
        queryset = Workspace.objects.filter(id__in=[self.ws_a.id, self.ws_a1.id])
        result = filter_top_level_workspaces(queryset)

        result_ids = set(result.values_list("id", flat=True))
        self.assertEqual(result_ids, {self.ws_a.id})

    def test_grandparent_and_grandchild_only_grandparent_is_top_level(self):
        """When grandparent and grandchild are in set (parent not), only grandparent is top-level."""
        # ws_a is grandparent of ws_a1a, ws_a1 (parent) is not in set
        queryset = Workspace.objects.filter(id__in=[self.ws_a.id, self.ws_a1a.id])
        result = filter_top_level_workspaces(queryset)

        result_ids = set(result.values_list("id", flat=True))
        self.assertEqual(result_ids, {self.ws_a.id})

    def test_siblings_both_are_top_level(self):
        """Sibling workspaces (no ancestor relationship) are both top-level."""
        queryset = Workspace.objects.filter(id__in=[self.ws_a.id, self.ws_b.id])
        result = filter_top_level_workspaces(queryset)

        result_ids = set(result.values_list("id", flat=True))
        self.assertEqual(result_ids, {self.ws_a.id, self.ws_b.id})

    def test_full_hierarchy_only_root_is_top_level(self):
        """When full hierarchy is in set, only root is top-level."""
        all_ids = [
            self.root.id,
            self.default.id,
            self.ws_a.id,
            self.ws_a1.id,
            self.ws_a1a.id,
            self.ws_b.id,
        ]
        queryset = Workspace.objects.filter(id__in=all_ids)
        result = filter_top_level_workspaces(queryset)

        result_ids = set(result.values_list("id", flat=True))
        self.assertEqual(result_ids, {self.root.id})

    def test_empty_queryset_returns_empty(self):
        """Empty queryset returns empty result."""
        queryset = Workspace.objects.none()
        result = filter_top_level_workspaces(queryset)

        self.assertEqual(list(result), [])

    def test_multiple_branches_multiple_top_levels(self):
        """Workspaces from different branches are all top-level."""
        # ws_a1 and ws_b are from different branches
        queryset = Workspace.objects.filter(id__in=[self.ws_a1.id, self.ws_b.id])
        result = filter_top_level_workspaces(queryset)

        result_ids = set(result.values_list("id", flat=True))
        self.assertEqual(result_ids, {self.ws_a1.id, self.ws_b.id})

    def test_deep_hierarchy_with_gaps(self):
        """Workspace with ancestor in set is not top-level, even with gaps."""
        # default -> ws_a -> ws_a1 -> ws_a1a
        # Include default and ws_a1a (skip ws_a and ws_a1)
        queryset = Workspace.objects.filter(id__in=[self.default.id, self.ws_a1a.id])
        result = filter_top_level_workspaces(queryset)

        result_ids = set(result.values_list("id", flat=True))
        # Only default is top-level because ws_a1a has default as ancestor
        self.assertEqual(result_ids, {self.default.id})
