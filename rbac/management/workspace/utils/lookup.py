#
# Copyright 2019 Red Hat, Inc.
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
"""Workspace lookup utilities."""

import logging
from typing import Optional

from management.models import Workspace

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def get_default_workspace_id(request) -> Optional[str]:
    """
    Get the default workspace ID for a tenant.

    Args:
        request: The HTTP request object containing tenant information

    Returns:
        Optional[str]: The default workspace ID, or None if it doesn't exist
    """
    try:
        return str(Workspace.objects.default(tenant_id=request.tenant).id)
    except Workspace.DoesNotExist:
        logger.warning(f"No default workspace for tenant {request.tenant}")
        return None


def _ws_or_default(request, view, default):
    """
    Get workspace ID from view kwargs or return default.

    Args:
        request: The HTTP request object
        view: The view object containing kwargs
        default: The default workspace ID to return if not found

    Returns:
        Optional[str]: Workspace ID from view kwargs or default
    """
    lookup = getattr(view, "lookup_url_kwarg", None) or "pk"
    ws_id = (getattr(view, "kwargs", {}) or {}).get(lookup)
    return ws_id or default


def workspace_from_request(request, view=None) -> Optional[str]:
    """
    Determine target workspace for permission checks.

    Uses DRF's view.action when available, falling back to request.method otherwise.
    Determines the target workspace ID based on the action/method:
    - For create: checks parent_id in request.data, falls back to default workspace
    - For retrieve (detail GET): uses pk from view.kwargs
    - For update/partial_update/destroy: uses pk from view.kwargs or defaults to default workspace
    - For list: returns None

    Args:
        request: The HTTP request object
        view: The view object (optional, contains kwargs and action)

    Returns:
        Optional[str]: The workspace ID to check permissions against, or None for list operations
    """
    default = get_default_workspace_id(request)
    action = getattr(view, "action", None) if view else None

    # If we have a DRF action, use it for cleaner logic
    if action:
        if action == "create":
            return request.data.get("parent_id") or default
        if action in ("update", "partial_update", "destroy", "move"):
            return _ws_or_default(request, view, default)
        if action == "retrieve":
            # Detail GET - return workspace ID without fallback
            lookup = getattr(view, "lookup_url_kwarg", None) or "pk"
            return getattr(view, "kwargs", {}).get(lookup)
        # list or other actions
        return None

    # Fallback to request.method for non-DRF views or when action is not set
    if request.method == "POST":
        if parent := getattr(request, "data", {}).get("parent_id"):
            return parent
        return default

    # Compute ws_id from view.kwargs (if any)
    lookup = getattr(view, "lookup_url_kwarg", None) or "pk" if view else "pk"
    ws_id = getattr(view, "kwargs", {}).get(lookup)

    # PUT/PATCH/DELETE: ws_id or default
    if request.method in ("PUT", "PATCH", "DELETE"):
        return ws_id or default

    # GET: detail returns ws_id, list returns None
    return ws_id
