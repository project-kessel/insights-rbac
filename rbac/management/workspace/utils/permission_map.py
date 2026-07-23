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
"""Permission mapping utilities for workspace operations."""

import logging
import uuid

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from management.models import Workspace
from rest_framework import permissions
from rest_framework.exceptions import MethodNotAllowed

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

# Map HTTP methods to workspace permissions
PERM_MAP = {
    "GET": "view",
    "POST": "create",
    "PUT": "edit",
    "PATCH": "edit",
    "DELETE": "delete",
}


def operation_from_request(request) -> str:
    """
    Map HTTP request method to legacy operation ('read' or 'write').

    Centralized to ensure consistency across V1 permission checks.

    Args:
        request: The HTTP request object

    Returns:
        str: 'read' for safe methods (GET, HEAD, OPTIONS), 'write' for others
    """
    return "read" if request.method in permissions.SAFE_METHODS else "write"


def permission_from_request(request, view=None) -> str:
    """
    Determine the permission/relation from the HTTP request method.

    Maps HTTP methods to workspace permissions:
    - GET -> view
    - POST -> create
    - PUT/PATCH -> edit (or 'move' if moving workspace to different parent)
    - DELETE -> delete

    Note: The 'move' permission is a special case of 'edit' that occurs when
    changing a workspace's parent. To detect this, we compare the new parent
    value with the existing one.

    Args:
        request: The HTTP request object
        view: The view object (optional, used to get the current workspace instance)

    Returns:
        str: The permission/relation name (view, create, edit, move, delete)

    Raises:
        MethodNotAllowed: If the HTTP method is not supported
    """
    method = request.method.upper()

    try:
        perm = PERM_MAP[method]
    except KeyError:
        logger.error(f"Unsupported HTTP method: {method}")
        raise MethodNotAllowed(method)

    # Detect "move" only on edit methods (PUT/PATCH)
    if perm == "edit" and view and method in ("PUT", "PATCH"):
        data = getattr(request, "data", {}) or {}
        new_pid = data.get("parent_id") or data.get("parent")

        if new_pid:
            # Get workspace ID from view kwargs and query directly to avoid
            # triggering select_for_update() from get_queryset() which would
            # fail outside of a transaction (permission check runs before
            # the view's @transaction.atomic decorator)
            lookup = getattr(view, "lookup_url_kwarg", None) or "pk"
            workspace_id = getattr(view, "kwargs", {}).get(lookup)

            # If workspace_id is missing from kwargs, we cannot determine if this is a move.
            # This should not happen for properly configured detail routes (PUT/PATCH /{id}/),
            # but if it does, we fall back to 'edit' permission which is the safe default
            # (move permission is more restrictive than edit).
            if not workspace_id:
                logger.warning(
                    "Cannot determine workspace_id from view kwargs for move detection; "
                    "falling back to 'edit' permission"
                )
            else:
                try:
                    # Validate UUID format before querying to avoid ValidationError
                    uuid.UUID(str(workspace_id))
                    current_pid = Workspace.objects.filter(pk=workspace_id).values_list("parent_id", flat=True).first()
                    # If current parent exists and differs from new parent, this is a move
                    if current_pid and str(new_pid) != str(current_pid):
                        return "move"
                except (ValueError, ValidationError):
                    # Invalid UUID format - fall back to edit permission
                    # The view will handle the proper 404/400 response later
                    logger.debug(
                        "Invalid workspace_id format for move detection; " "falling back to 'edit' permission"
                    )
                except DatabaseError:
                    # Database error during lookup - fall back to edit permission
                    logger.warning(
                        "DatabaseError while checking workspace parent_id for move detection; "
                        "falling back to 'edit' permission",
                        exc_info=True,
                    )

    return perm
