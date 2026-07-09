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
"""Workspace access control via FilterBackend for v2 APIs."""

import logging

from feature_flags import FEATURE_FLAGS
from management.workspace.utils import permission_from_request
from management.workspace.utils.access import is_user_allowed_v2
from rest_framework import filters
from rest_framework.exceptions import PermissionDenied

logger = logging.getLogger(__name__)


class WorkspaceAccessFilterBackend(filters.BaseFilterBackend):
    """
    FilterBackend that enforces workspace visibility via Kessel Inventory API.

    This filter:
    - Applies ONLY to v2 workspace endpoints
    - Uses is_user_allowed_v2 which internally selects the appropriate API:
      - List (workspace_id=None): StreamedListObjects, sets request.permission_tuples
      - Detail (workspace_id specified): CheckForUpdate (single resource check)
    - Filters queryset to only include workspaces user can access
    - Returns 404 for inaccessible workspaces on detail views (no existence leak)

    Why FilterBackend over Permission class:
    - Permission classes are for coarse-grained endpoint access (can user call this endpoint?)
    - FilterBackends are for data filtering (what data can user see?)
    - This separation follows DRF best practices and single responsibility principle
    """

    # Actions that operate on a single workspace (use CheckForUpdate via is_user_allowed_v2)
    DETAIL_ACTIONS = {"retrieve", "update", "partial_update", "destroy", "move"}

    def filter_queryset(self, request, queryset, view):
        """
        Filter workspaces to only those accessible by the current user.

        Uses is_user_allowed_v2 for all access checks which provides:
        - Consistent timing logs for performance monitoring
        - Proper error handling and logging
        - IT service fallback for user_id lookup
        - System user bypass
        - Automatically selects API method based on workspace_id:
          - None: StreamedListObjects, sets request.permission_tuples
          - Specified: CheckForUpdate

        This ensures consistent 404 behavior:
        - If workspace doesn't exist: 404 (standard DRF behavior)
        - If workspace exists but user can't access: 404 (prevents existence leakage)
        """
        # Skip filtering if V2 access check is disabled (fall back to v1 behavior)
        if not FEATURE_FLAGS.is_workspace_access_check_v2_enabled():
            return self._filter_v1(request, queryset, view)

        # Determine the relation/permission and workspace_id based on action
        relation = permission_from_request(request, view)
        action = getattr(view, "action", None)
        workspace_id = str(view.kwargs.get("pk")) if action in self.DETAIL_ACTIONS else None

        # Call is_user_allowed_v2 - handles both list and detail cases
        # Side effect: when workspace_id is None (list actions), is_user_allowed_v2 sets
        # request.permission_tuples with accessible workspace IDs, used for filtering below
        with_ancestry = getattr(request, "with_ancestry", False) if workspace_id is None else False
        try:
            has_access = is_user_allowed_v2(request, relation, workspace_id, with_ancestry=with_ancestry)
        except Exception as e:
            logger.exception(
                "Exception in is_user_allowed_v2: user=%s, org_id=%s, workspace_id=%s, relation=%s, error=%s",
                getattr(request.user, "username", "unknown"),
                getattr(request.user, "org_id", "unknown"),
                workspace_id,
                relation,
                str(e),
            )
            return queryset.none()

        # For detail actions: filter to specific workspace if access granted
        if workspace_id:
            return queryset.filter(id=workspace_id) if has_access else queryset.none()

        # For list actions: check access decision first, then filter by permission_tuples
        if not has_access:
            if not with_ancestry:
                raise PermissionDenied("You do not have permission to perform this action.")
            return queryset.none()

        # If permission_tuples is set, filter by those IDs
        if hasattr(request, "permission_tuples") and request.permission_tuples:
            accessible_ids = {ws_tuple[1] for ws_tuple in request.permission_tuples}
            return queryset.filter(id__in=accessible_ids)

        # has_access is True but no permission_tuples (system user bypass) - return all workspaces
        return queryset

    def _filter_v1(self, request, queryset, view):
        """
        V1 filtering behavior - uses permission tuples set by is_user_allowed_v1.

        This maintains backward compatibility with V1 access control.
        """
        # For V1, the permission check in is_user_allowed_v1 sets request.permission_tuples
        # We need to filter based on those tuples
        if hasattr(request, "permission_tuples") and request.permission_tuples:
            permitted_ws_ids = [tuple[1] for tuple in request.permission_tuples]
            return queryset.filter(id__in=permitted_ws_ids)
        return queryset


class WorkspaceObjectAccessMixin:
    """
    Mixin that ensures 404 for inaccessible workspaces on detail views.

    Add this to viewsets to prevent object existence leakage.
    The get_object() method will raise 404 if the workspace is not
    in the filtered queryset (i.e., user doesn't have access).
    """

    def get_object(self):
        """
        Get object with access-aware 404 behavior.

        If the workspace exists but user doesn't have access,
        returns 404 (not 403) to prevent existence leakage.
        """
        # get_queryset() already filtered by WorkspaceAccessFilterBackend
        # If object not in filtered queryset, DRF raises 404
        return super().get_object()
