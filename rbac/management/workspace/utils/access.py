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
"""Workspace access checking utilities."""

import logging
import time
from contextlib import contextmanager
from uuid import UUID

from django.db.models.expressions import RawSQL
from feature_flags import FEATURE_FLAGS
from management.models import Access, Workspace
from management.permissions.system_user_utils import SystemUserAccessResult, check_system_user_access
from management.permissions.workspace_inventory_access import (
    WorkspaceInventoryAccessChecker,
)
from management.principal.model import Principal
from management.principal.proxy import PrincipalProxy
from management.utils import get_principal_for_auth, get_principal_from_request, roles_for_principal
from rest_framework.serializers import ValidationError

from rbac import settings

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@contextmanager
def record_timing(timings: dict, key: str):
    """
    Context manager to record elapsed time for a code block.

    When timing is disabled (WORKSPACE_ACCESS_TIMING_ENABLED=False), this is a no-op
    to avoid perf_counter() and dict update overhead on hot paths.

    Args:
        timings: Dictionary to store timing measurements
        key: The key under which to store the elapsed time
    """
    if not settings.WORKSPACE_ACCESS_TIMING_ENABLED:
        yield
        return

    start = time.perf_counter()
    try:
        yield
    finally:
        timings[key] = time.perf_counter() - start


def _log_v2_timing(timings, total_start, extra_fields, reason=None):
    """
    Log timing breakdown for is_user_allowed_v2 if timing logging is enabled.

    Args:
        timings: Dictionary of timing measurements
        total_start: Start time from time.perf_counter()
        extra_fields: Additional fields to include in log (e.g., workspace count, path)
        reason: Optional reason for early return (e.g., "it_service_failure")
    """
    if not settings.WORKSPACE_ACCESS_TIMING_ENABLED:
        return

    timings["total"] = time.perf_counter() - total_start
    log_extra = {
        "timings_ms": {k: round(v * 1000, 2) for k, v in timings.items()},
        **extra_fields,
    }
    if reason:
        log_extra["early_return_reason"] = reason

    logger.info("is_user_allowed_v2 timing breakdown: %s", log_extra)


def get_fallback_workspace_ids(tenant):
    """
    Get the IDs of fallback workspaces (root, default, ungrouped) for a tenant.

    When a user has no accessible workspaces, these workspaces are returned
    to ensure they can still see the basic workspace structure.

    Uses a single database query to fetch all three workspace types.

    Args:
        tenant: The tenant to get fallback workspaces for

    Returns:
        set[str]: Set of workspace IDs for root, default, and ungrouped workspaces
    """
    workspace_ids = set()
    workspaces = Workspace.objects.filter(
        tenant=tenant,
        type__in=[
            Workspace.Types.ROOT,
            Workspace.Types.DEFAULT,
            Workspace.Types.UNGROUPED_HOSTS,
        ],
    )
    for workspace in workspaces:
        workspace_ids.add(str(workspace.id))
    return workspace_ids


def filter_top_level_workspaces(queryset):
    """
    Filter workspaces to return only top-level ones using a single CTE query.

    A workspace is top-level if none of its ancestors are in the queryset.
    Uses RawSQL with a recursive CTE (similar to Workspace.ancestors() in model.py)
    to compute top-level workspaces in a single database round-trip.

    Args:
        queryset: QuerySet of workspaces to filter

    Returns:
        QuerySet: Filtered queryset containing only top-level workspaces
    """
    accessible_ids_list = list(queryset.values_list("id", flat=True))
    if not accessible_ids_list:
        return queryset.none()

    # Single CTE query to find top-level workspaces:
    # A workspace is top-level if none of its ancestors are in the accessible set
    sql = """
        WITH RECURSIVE workspace_ancestors AS (
            -- Base case: start with all specified workspaces and their direct parents
            SELECT id AS workspace_id, parent_id AS ancestor_id
            FROM management_workspace
            WHERE id = ANY(%s) AND parent_id IS NOT NULL

            UNION

            -- Recursive case: get ancestors of ancestors
            SELECT wa.workspace_id, w.parent_id AS ancestor_id
            FROM workspace_ancestors wa
            JOIN management_workspace w ON w.id = wa.ancestor_id
            WHERE w.parent_id IS NOT NULL
        ),
        -- Find workspaces that have an ancestor in the accessible set (not top-level)
        has_ancestor_in_set AS (
            SELECT DISTINCT workspace_id
            FROM workspace_ancestors
            WHERE ancestor_id = ANY(%s)
        )
        -- Return workspaces that don't have any ancestor in the accessible set
        SELECT unnest(%s::uuid[])
        EXCEPT
        SELECT workspace_id FROM has_ancestor_in_set
    """

    return queryset.filter(id__in=RawSQL(sql, [accessible_ids_list, accessible_ids_list, accessible_ids_list]))


def is_user_allowed(request, required_operation, target_workspace):
    """
    Check if the user is allowed to perform the required permission on the target workspace.

    This function handles V1/V2 feature flag branching for compatibility with
    code that calls it directly (outside of WorkspaceAccessPermission).
    For new code, prefer using WorkspaceAccessPermission as the single entry point.

    Args:
        request: The HTTP request object
        required_operation: The operation to check
            - For V1: "read" or "write"
            - For V2: "view", "create", "edit", "move", "delete"
        target_workspace: The workspace ID to check, or None for list operations

    Returns:
        bool: True if the user has permission, False otherwise
    """
    if FEATURE_FLAGS.is_workspace_access_check_v2_enabled():
        return is_user_allowed_v2(request, required_operation, target_workspace)
    return is_user_allowed_v1(request, required_operation, target_workspace)


def is_user_allowed_v1(request, required_operation, target_workspace):
    """
    Check if the user is allowed using V1 workspace access logic.

    This is the legacy implementation using direct role/permission checks.
    Use WorkspaceAccessPermission for the main entry point which handles
    V1/V2 feature flag branching.

    Args:
        request: The HTTP request object
        required_operation: The operation to check ("read" or "write")
        target_workspace: The workspace ID to check, or None for list operations

    Returns:
        bool: True if the user has permission, False otherwise
    """
    root_workspace_id = str(Workspace.objects.root(tenant_id=request.tenant).id)
    is_get_action = request.method == "GET"
    if target_workspace is None:
        # If the target workspace is not provided, check if the user has the required permission
        # on any workspace.
        target_workspace = root_workspace_id
    if required_operation == "read":
        allowed_operations = ["read", "write", "*"]
    else:
        allowed_operations = ["write", "*"]
    valid_perm_tuples = set()
    for valid_resource in ["groups", "*"]:
        for valid_operation in allowed_operations:
            valid_perm_tuples.add((f"inventory:{valid_resource}:{valid_operation}", target_workspace))
    tuple_set = workspace_permission_tuple_set(request, root_workspace_id, is_get_action)

    if is_get_action:
        # Get the set of permission tuples for later filter
        request.permission_tuples = tuple_set
    return any(valid_perm_tuple in tuple_set for valid_perm_tuple in valid_perm_tuples)


def is_user_allowed_v2(request, required_operation, target_workspace):
    """
    Check if the user is allowed to perform the required permission on the target workspace using Inventory API.

    This is the v2 implementation using Inventory API for permission checks.

    Args:
        request: The HTTP request object
        required_operation: The operation/relation to check (view, create, edit, move, delete)
        target_workspace: The workspace ID to check, or None for list operations

    Returns:
        bool: True if the user has permission, False otherwise
    """
    # Only initialize timing infrastructure when timing is enabled to avoid overhead
    total_start = time.perf_counter() if settings.WORKSPACE_ACCESS_TIMING_ENABLED else None
    timings: dict[str, float] = {} if settings.WORKSPACE_ACCESS_TIMING_ENABLED else {}
    base_extra = {
        "org_id": getattr(request.user, "org_id", None),
        "request_path": request.path,
        "request_id": getattr(request, "req_id", None),
    }
    early_reason = None
    result = False
    principal_id = None
    accessible_workspace_ids = None

    try:
        # For system users (s2s communication), bypass v2 access checks and rely on user.admin
        # Uses unified check_system_user_access to prevent behavior drift
        # Note: action is not passed here since is_user_allowed_v2 doesn't have view context;
        # the caller (WorkspaceAccessPermission) handles move-specific logic with action parameter
        with record_timing(timings, "system_user_check"):
            system_check = check_system_user_access(request.user)

        if system_check.is_system:
            # For system users, ALLOWED and CHECK_MOVE_TARGET both return True here
            # (move-specific checks are handled by the caller with full view context)
            # DENIED returns False
            early_reason = "system_user"
            result = system_check.result != SystemUserAccessResult.DENIED
            return result
        # NOT_SYSTEM_USER - continue with normal checks

        # Try to get user_id from principal, request.user, or IT service API
        with record_timing(timings, "get_principal_id"):
            principal = get_principal_for_auth(request)
            if principal is not None and principal.user_id is not None:
                user_id = principal.user_id
            elif (user_id := getattr(request.user, "user_id", None)) is not None:
                # user_id available from request identity header
                pass
            elif username := getattr(request.user, "username", None):
                # Fallback: query IT service via PrincipalProxy to get user_id
                org_id = getattr(request.user, "org_id", None)
                if not org_id:
                    logger.warning("No org_id available from request.user, denying access")
                    early_reason = "no_org_id"
                    return False

                proxy = PrincipalProxy()
                resp = proxy.request_filtered_principals([username], org_id=org_id, options={"return_id": True})

                if resp.get("status_code") != 200 or not resp.get("data"):
                    logger.warning("Failed to retrieve user_id from IT service for username: %s", username)
                    early_reason = "it_service_failure"
                    return False

                user_id = resp["data"][0].get("user_id")
                if not user_id:
                    logger.warning("IT service response missing user_id for username: %s", username)
                    early_reason = "missing_user_id"
                    return False

                logger.debug("Retrieved user_id from IT service via PrincipalProxy")
            else:
                logger.warning("No username available from request.user, denying access")
                early_reason = "no_username"
                return False

        # Log warning if user_id is None after all lookup attempts
        if user_id is None:
            org_id = getattr(request.user, "org_id", None)
            username = getattr(request.user, "username", None)
            is_system = getattr(request.user, "system", False)
            # Log a minimal, structured subset of context to avoid exposing PII
            logger.warning(
                "user_id is None after all lookup attempts",
                extra={
                    "org_id": org_id,
                    "has_username": bool(username),
                    "principal_type": type(principal).__name__ if principal is not None else None,
                    "is_system": is_system,
                    "request_path": request.path,
                    "request_method": request.method,
                },
            )

        # Format principal ID as required by Inventory API (e.g., "localhost/username")
        principal_id = Principal.user_id_to_principal_resource_id(user_id)

        # Create the Inventory API checker
        checker = WorkspaceInventoryAccessChecker()

        # Use the required_operation directly (already determined by permission_from_request)
        relation = required_operation

        # For list operations (None workspace_id), get all accessible workspaces
        if target_workspace is None:
            # Lookup accessible workspaces using StreamedListObjects
            with record_timing(timings, "inventory_api_lookup"):
                org_id = getattr(request.tenant, "org_id", None)
                # Reload tenant from DB to get the latest consistency token,
                # since the cached tenant (from Redis TenantCache) may have a stale value.
                # The Kafka consumer updates this field in the DB when relations change.
                request.tenant.refresh_from_db(fields=["relations_consistency_token"])
                consistency_token = request.tenant.relations_consistency_token
                logger.info(
                    "lookup_accessible_workspaces: org_id=%s, consistency_token=%s",
                    org_id,
                    consistency_token,
                )
                accessible_workspace_ids = checker.lookup_accessible_workspaces(
                    principal_id=principal_id,
                    relation=relation,
                    request_id=getattr(request, "req_id", None),
                    consistency_token=consistency_token,
                )

            # Convert to set of UUIDs for proper filtering
            accessible_workspace_ids = set(accessible_workspace_ids)

            if accessible_workspace_ids:
                # User has actual workspace permissions (not just fallbacks)
                request.has_real_workspace_access = True

                # Add ancestors only from the top-level workspace(s) in accessible workspaces (for ancestry needs)
                # Get workspace objects for accessible IDs
                with record_timing(timings, "db_filter_accessible_workspaces"):
                    accessible_workspaces = Workspace.objects.filter(
                        id__in=accessible_workspace_ids, tenant=request.tenant
                    )

                if not accessible_workspaces.exists():
                    # Inventory can return workspace ids that are not present in RBAC for this tenant
                    # (replication lag, stale tuples, cross-env mismatch). Treat like no workspace access
                    # so the list API still returns root/default/ungrouped like users with no permissions.
                    logger.info(
                        "StreamedListObjects returned %s workspace id(s) but none exist for this tenant; "
                        "using fallback workspaces",
                        len(accessible_workspace_ids),
                    )
                    request.has_real_workspace_access = False
                    with record_timing(timings, "get_fallback_workspace_ids"):
                        accessible_workspace_ids = get_fallback_workspace_ids(request.tenant)
                else:
                    # Keep only ids that exist for this tenant; drop stale Inventory-only ids
                    accessible_workspace_ids = {str(wid) for wid in accessible_workspaces.values_list("id", flat=True)}

                    # Find the top-level workspace(s) - those that are not children of any other accessible workspace
                    with record_timing(timings, "filter_top_level_workspaces"):
                        top_level_workspaces = filter_top_level_workspaces(accessible_workspaces)

                    with record_timing(timings, "add_ancestor_ids"):
                        for workspace in top_level_workspaces:
                            # Add ancestors directly for this top-level workspace
                            ancestor_ids = {str(ancestor.id) for ancestor in workspace.ancestors()}
                            accessible_workspace_ids.update(ancestor_ids)
            else:
                # User has no actual workspace permissions, only fallback access
                request.has_real_workspace_access = False

                # If no accessible workspaces, attach at least root, default, and ungrouped workspaces
                with record_timing(timings, "get_fallback_workspace_ids"):
                    accessible_workspace_ids = get_fallback_workspace_ids(request.tenant)

            # Store permission tuples for later filtering
            request.permission_tuples = [(None, ws_id) for ws_id in accessible_workspace_ids]

            result = bool(accessible_workspace_ids)
            return result

        # For specific workspace operations, check access for that workspace
        with record_timing(timings, "inventory_api_check_access"):
            result = checker.check_workspace_access(
                workspace_id=target_workspace, principal_id=principal_id, relation=relation
            )

        # If Kessel denied access for a 'view' operation, check if it's a fallback workspace
        # (root, default, ungrouped). These workspaces should be accessible to all users for
        # basic workspace structure visibility, but only for read operations.
        # Write operations (create, edit, move, delete) still require explicit permissions.
        if not result and required_operation == "view":
            with record_timing(timings, "check_fallback_workspace"):
                fallback_workspace_ids = get_fallback_workspace_ids(request.tenant)
                if target_workspace in fallback_workspace_ids:
                    result = True

        return result

    finally:
        # Only build timing metadata and log when timing logs are enabled to reduce overhead
        if settings.WORKSPACE_ACCESS_TIMING_ENABLED:
            extra = {
                **base_extra,
                "required_operation": required_operation,
                "access_decision": "allowed" if result else "denied",
            }
            if principal_id is not None:
                extra["principal_id"] = principal_id
            if target_workspace is None and accessible_workspace_ids is not None:
                extra["accessible_workspace_count"] = len(accessible_workspace_ids)
            if target_workspace is not None:
                extra["target_workspace"] = target_workspace

            _log_v2_timing(timings, total_start, extra, reason=early_reason)


def get_access_permission_tuples(access, tenant, root_workspace_id, is_get_action):
    """Get the set of permission tuples for the given access."""
    group_list = _get_group_list_from_resource_definitions(access.resourceDefinitions.all()) or [root_workspace_id]
    workspaces = Workspace.objects.filter(tenant=tenant, id__in=group_list)
    tuple_set = set()
    for workspace in workspaces:
        for descendant in Workspace.objects.descendant_ids_with_parents([str(workspace.id)], workspace.tenant_id):
            tuple_set.add((access.permission.permission, descendant))
        if is_get_action:
            # Allow getting ancestors for a workspace they have access to
            for ancestor in workspace.ancestors():
                tuple_set.add((access.permission.permission, str(ancestor.id)))
    return tuple_set


def workspace_permission_tuple_set(request, root_workspace_id, is_get_action):
    """Get the set of permission tuples for the user's roles on the workspace."""
    principal = get_principal_from_request(request)
    roles = roles_for_principal(
        principal,
        request.tenant,
        **{
            "prefetch_lookups_for_ids": "resourceDefinitions",
            "prefetch_lookups_for_groups": "policies__roles__access",
            "is_org_admin": request.user.admin,
        },
    )
    accesses = Access.objects.filter(
        role__in=roles,
        permission__application="inventory",
        permission__resource_type__in=["groups", "*"],
    )
    tuple_set = set()
    for access in accesses:
        tuple_set |= get_access_permission_tuples(access, request.tenant, root_workspace_id, is_get_action)
    return tuple_set


def _get_group_list_from_resource_definitions(resource_definitions: dict) -> list[str]:
    """Get the list of group IDs from the resource definition."""
    group_list = []
    for resource_definition in resource_definitions:
        attribute_filter = resource_definition.attributeFilter
        if not attribute_filter:
            continue
        if attribute_filter.get("key") != "group.id":
            raise ValidationError("Invalid value for attributeFilter.key.")
        if attribute_filter.get("operation") not in ["in", "equal"]:
            raise ValidationError("Invalid value for attributeFilter.operation.")

        value = attribute_filter.get("value")
        if isinstance(value, list):
            group_list.extend(value)
        else:
            group_list.append(value)

    # Build new list of valid UUIDs instead of modifying during iteration
    valid_group_list = []
    for gid in group_list:
        if gid is None:
            continue
        try:
            UUID(gid)
            valid_group_list.append(gid)
        except (ValueError, TypeError):
            # Skip invalid UUIDs
            pass

    return valid_group_list
