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
"""Tests for workspace access utilities (is_user_allowed_v1)."""

from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import TestCase

from management.workspace.utils.access import is_user_allowed_v1


class IsUserAllowedV1Tests(TestCase):
    """Tests for is_user_allowed_v1 permission checking."""

    def setUp(self):
        """Set up common test data."""
        self.root_workspace_id = str(uuid4())
        self.target_workspace_id = str(uuid4())

    def _make_request(self, tenant=None):
        """Create a mock request with a tenant that has a root workspace."""
        request = Mock()
        request.tenant = tenant or Mock()
        return request

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_denied_with_only_write_permission(self, mock_workspace_model, mock_tuple_set):
        """User with only inventory:groups:write should NOT be able to read workspaces.

        Regression test for RHCLOUD-52309: in ephemeral with RBAC v2, a user
        with only inventory:groups:write could view workspaces because the V1
        code path treated write as implying read.
        """
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        # User has only write permission on the target workspace
        mock_tuple_set.return_value = {
            (f"inventory:groups:write", self.target_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "read", self.target_workspace_id)

        self.assertFalse(result, "inventory:groups:write should NOT grant read access")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_allowed_with_read_permission(self, mock_workspace_model, mock_tuple_set):
        """User with inventory:groups:read should be able to read workspaces."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        mock_tuple_set.return_value = {
            (f"inventory:groups:read", self.target_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "read", self.target_workspace_id)

        self.assertTrue(result, "inventory:groups:read should grant read access")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_allowed_with_wildcard_permission(self, mock_workspace_model, mock_tuple_set):
        """User with inventory:groups:* should be able to read workspaces."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        mock_tuple_set.return_value = {
            (f"inventory:groups:*", self.target_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "read", self.target_workspace_id)

        self.assertTrue(result, "inventory:groups:* should grant read access")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_allowed_with_wildcard_resource(self, mock_workspace_model, mock_tuple_set):
        """User with inventory:*:read should be able to read workspaces."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        mock_tuple_set.return_value = {
            (f"inventory:*:read", self.target_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "read", self.target_workspace_id)

        self.assertTrue(result, "inventory:*:read should grant read access")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_write_allowed_with_write_permission(self, mock_workspace_model, mock_tuple_set):
        """User with inventory:groups:write should be able to write to workspaces."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        mock_tuple_set.return_value = {
            (f"inventory:groups:write", self.target_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "write", self.target_workspace_id)

        self.assertTrue(result, "inventory:groups:write should grant write access")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_write_denied_with_only_read_permission(self, mock_workspace_model, mock_tuple_set):
        """User with only inventory:groups:read should NOT be able to write to workspaces."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        mock_tuple_set.return_value = {
            (f"inventory:groups:read", self.target_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "write", self.target_workspace_id)

        self.assertFalse(result, "inventory:groups:read should NOT grant write access")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_denied_with_write_on_different_workspace(self, mock_workspace_model, mock_tuple_set):
        """Write permission on different workspace should not grant read on target."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        other_workspace_id = str(uuid4())
        mock_tuple_set.return_value = {
            (f"inventory:groups:write", other_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "read", self.target_workspace_id)

        self.assertFalse(result, "write on different workspace should NOT grant read on target")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_with_both_read_and_write(self, mock_workspace_model, mock_tuple_set):
        """User with both read and write should be able to read."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        mock_tuple_set.return_value = {
            (f"inventory:groups:read", self.target_workspace_id),
            (f"inventory:groups:write", self.target_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "read", self.target_workspace_id)

        self.assertTrue(result, "User with both read and write should be able to read")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_sets_permission_tuples_on_request(self, mock_workspace_model, mock_tuple_set):
        """Read operation should set permission_tuples on request for FilterBackend."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        tuples = {
            (f"inventory:groups:read", self.target_workspace_id),
        }
        mock_tuple_set.return_value = tuples

        request = self._make_request()
        is_user_allowed_v1(request, "read", self.target_workspace_id)

        self.assertEqual(request.permission_tuples, tuples)

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_uses_root_workspace_when_target_is_none(self, mock_workspace_model, mock_tuple_set):
        """When target_workspace is None (list), should check root workspace."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        mock_tuple_set.return_value = {
            (f"inventory:groups:read", self.root_workspace_id),
        }

        request = self._make_request()
        result = is_user_allowed_v1(request, "read", None)

        self.assertTrue(result, "Should check root workspace when target is None")

    @patch("management.workspace.utils.access.workspace_permission_tuple_set")
    @patch("management.workspace.utils.access.Workspace")
    def test_read_denied_with_no_permissions(self, mock_workspace_model, mock_tuple_set):
        """User with no permissions should be denied read access."""
        mock_workspace_model.objects.root.return_value = Mock(id=self.root_workspace_id)

        mock_tuple_set.return_value = set()

        request = self._make_request()
        result = is_user_allowed_v1(request, "read", self.target_workspace_id)

        self.assertFalse(result, "User with no permissions should be denied read access")
