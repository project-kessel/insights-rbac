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

"""MCP endpoint for RBAC using Anthropic MCP Python SDK for tool registration and schema generation."""

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
from typing import Any, Callable

import jsonschema
from django.conf import settings
from django.core.cache import cache as django_cache
from django.db.models import Count, Prefetch
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.test import RequestFactory
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from management.access.view import AccessView
from management.audit_log.view import AuditLogViewSet
from management.cache import _get_connection_pool, _is_mock_redis
from management.group.view import GroupViewSet
from management.models import Access, AuditLog, Group, Permission
from management.permission.view import PermissionViewSet
from management.permissions.workspace_inventory_access import WorkspaceInventoryAccessChecker
from management.principal.model import Principal
from management.principal.proxy import PrincipalProxy, get_kessel_principal_id
from management.principal.view import PrincipalView
from management.role.model import Role
from management.role.v2_model import RoleV2
from management.role.v2_view import RoleV2ViewSet
from management.role.view import RoleViewSet
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from management.role_binding.view import RoleBindingViewSet
from management.tenant_mapping.v2_activation import is_v2_write_activated
from management.workspace.view import WorkspaceViewSet
from mcp.server.fastmcp import FastMCP
from prometheus_client import Counter, Histogram
from redis import Redis, exceptions as redis_exceptions

from api.common import RH_IDENTITY_HEADER
from api.cross_access.view import CrossAccountRequestViewSet
from api.models import CrossAccountRequest, Tenant
from api.status.view import status as status_view_fn

# Cache view functions — .as_view() returns a new callable each time,
# but the result is stateless and reusable.
_principal_view = PrincipalView.as_view()
_status_view = status_view_fn
_access_view = AccessView.as_view()
_permission_list_view = PermissionViewSet.as_view({"get": "list"})
_permission_options_view = PermissionViewSet.as_view({"get": "options"})
_auditlog_list_view = AuditLogViewSet.as_view({"get": "list"})
_role_access_view = RoleViewSet.as_view({"get": "access"})
_role_v1_list_view = RoleViewSet.as_view({"get": "list"})
_role_v1_detail_view = RoleViewSet.as_view({"get": "retrieve"})
_role_v2_list_view = RoleV2ViewSet.as_view({"get": "list"})
_role_v2_detail_view = RoleV2ViewSet.as_view({"get": "retrieve"})
_group_list_view = GroupViewSet.as_view({"get": "list"})
_group_detail_view = GroupViewSet.as_view({"get": "retrieve"})
_group_principals_view = GroupViewSet.as_view({"get": "principals"})
_group_roles_view = GroupViewSet.as_view({"get": "roles"})
_cross_account_list_view = CrossAccountRequestViewSet.as_view({"get": "list"})
_cross_account_detail_view = CrossAccountRequestViewSet.as_view({"get": "retrieve"})
_workspace_list_view = WorkspaceViewSet.as_view({"get": "list"})
_workspace_detail_view = WorkspaceViewSet.as_view({"get": "retrieve"})
_role_binding_list_view = RoleBindingViewSet.as_view({"get": "list"})
_role_binding_by_subject_view = RoleBindingViewSet.as_view({"get": "by_subject"})

# Write views (POST)
_group_create_view = GroupViewSet.as_view({"post": "create"})
_group_principals_write_view = GroupViewSet.as_view({"post": "principals"})
_group_roles_write_view = GroupViewSet.as_view({"post": "roles"})
_role_v1_create_view = RoleViewSet.as_view({"post": "create"})
_role_v2_create_view = RoleV2ViewSet.as_view({"post": "create"})
_role_binding_batch_create_view = RoleBindingViewSet.as_view({"post": "batch_create"})
_workspace_create_view = WorkspaceViewSet.as_view({"post": "create"})
_cross_account_create_view = CrossAccountRequestViewSet.as_view({"post": "create"})

# Write views (PUT/PATCH)
_group_update_view = GroupViewSet.as_view({"put": "update"})
_role_v1_update_view = RoleViewSet.as_view({"put": "update"})
_role_v1_patch_view = RoleViewSet.as_view({"patch": "partial_update"})
_role_v2_update_view = RoleV2ViewSet.as_view({"put": "update"})
_role_binding_update_view = RoleBindingViewSet.as_view({"put": "by_subject"})
_workspace_update_view = WorkspaceViewSet.as_view({"put": "update"})
_workspace_move_view = WorkspaceViewSet.as_view({"post": "move"})
_cross_account_update_view = CrossAccountRequestViewSet.as_view({"put": "update"})
_cross_account_patch_view = CrossAccountRequestViewSet.as_view({"patch": "partial_update"})

# Write views (DELETE)
_group_delete_view = GroupViewSet.as_view({"delete": "destroy"})
_group_principals_delete_view = GroupViewSet.as_view({"delete": "principals"})
_group_roles_delete_view = GroupViewSet.as_view({"delete": "roles"})
_role_v1_delete_view = RoleViewSet.as_view({"delete": "destroy"})
_role_v2_bulk_delete_view = RoleV2ViewSet.as_view({"post": "bulk_destroy"})
_workspace_delete_view = WorkspaceViewSet.as_view({"delete": "destroy"})

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"

# --- Prometheus metrics ---

mcp_tool_call_total = Counter(
    "rbac_mcp_tool_call_total",
    "Total MCP tool calls (excludes hello)",
    ["tool", "status"],
)
mcp_tool_call_duration_seconds = Histogram(
    "rbac_mcp_tool_call_duration_seconds",
    "Duration of MCP tool calls in seconds (excludes hello)",
    ["tool"],
)

# Factory for building internal Django requests to delegate to views.
_request_factory = RequestFactory()

# Headers forwarded from the original request to internal view requests,
# beyond the identity header (which is always forwarded).
_FORWARDED_HEADERS = ("HTTP_X_REQUEST_ID", "HTTP_X_RH_INSIGHTS_REQUEST_ID")

# --- Tool description overrides (Redis-backed, no restart needed) ---

_REDIS_DESC_PREFIX = "mcp:desc:"


def _get_redis():
    """Get a Redis connection using the shared connection pool from management.cache."""
    if _is_mock_redis():
        return None
    return Redis(connection_pool=_get_connection_pool(), ssl=settings.REDIS_SSL)


def _get_description_override(tool_name: str) -> str | None:
    try:
        conn = _get_redis()
        if conn is None:
            return None
        value = conn.get(f"{_REDIS_DESC_PREFIX}{tool_name}")
        return value.decode() if value else None
    except redis_exceptions.RedisError:
        logger.debug("mcp: redis unavailable for description override, tool=%s", tool_name)
        return None


def _set_description_override(tool_name: str, description: str) -> None:
    conn = _get_redis()
    if conn is not None:
        conn.set(f"{_REDIS_DESC_PREFIX}{tool_name}", description)


def _delete_description_override(tool_name: str) -> None:
    conn = _get_redis()
    if conn is not None:
        conn.delete(f"{_REDIS_DESC_PREFIX}{tool_name}")


def _get_all_description_overrides() -> dict[str, str]:
    try:
        r = _get_redis()
        if r is None:
            return {}
        keys = r.keys(f"{_REDIS_DESC_PREFIX}*")
        if not keys:
            return {}
        values = r.mget(keys)
        prefix_len = len(_REDIS_DESC_PREFIX)
        return {k.decode()[prefix_len:]: v.decode() for k, v in zip(keys, values) if v is not None}
    except redis_exceptions.RedisError:
        logger.debug("mcp: redis unavailable for description overrides")
        return {}


# --- MCP Server setup using the Anthropic MCP Python SDK ---

mcp = FastMCP("RBAC")


# --- Tool configuration ---
#
# @register_tool registers each tool with both FastMCP (for schema generation)
# and _TOOL_CONFIG (for sync execution). This eliminates the need for separate
# stub functions and a manual config dict.


class ApiVersion:
    """API version classification for MCP tools."""

    UNIFIED = "unified"
    COMMON = "common"
    V1 = "v1"
    V2 = "v2"
    UNVERSIONED = "unversioned"


@dataclass(frozen=True)
class ToolConfig:
    """Configuration for an MCP tool."""

    fn: Callable[..., str]
    requires_auth: bool = False
    passes_request: bool = False
    api_version: str = ApiVersion.COMMON
    write: bool = False
    required_relation: str | None = None
    required_resource_type: str = "tenant"
    v1_permission: tuple[str, str] | None = None
    caveats: str = ""
    redacted_fields: tuple[str, ...] = ()


_TOOL_CONFIG: dict[str, ToolConfig] = {}


def register_tool(
    *,
    description: str,
    requires_auth: bool = False,
    api_version: str = ApiVersion.COMMON,
    write: bool = False,
    required_relation: str | None = None,
    required_resource_type: str = "tenant",
    v1_permission: tuple[str, str] | None = None,
    caveats: str = "",
    redacted_fields: tuple[str, ...] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool with both FastMCP and _TOOL_CONFIG.

    If the function's first parameter is named ``request``, a schema-only
    wrapper (without the ``request`` param) is registered with FastMCP so
    the generated JSON schema matches what MCP clients send. The real
    implementation receives the Django request at call time via
    ``_handle_tools_call``.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = fn.__name__
        sig = inspect.signature(fn)
        first_param = next(iter(sig.parameters), None)
        passes_request = first_param == "request"

        resolved_caveats = caveats
        if not resolved_caveats and "Caveats:\n" in description:
            resolved_caveats = description.split("Caveats:\n", 1)[1].strip()

        _TOOL_CONFIG[tool_name] = ToolConfig(
            fn=fn,
            requires_auth=requires_auth,
            passes_request=passes_request,
            api_version=api_version,
            write=write,
            required_relation=required_relation,
            required_resource_type=required_resource_type,
            v1_permission=v1_permission,
            caveats=resolved_caveats,
            redacted_fields=redacted_fields,
        )

        if passes_request:
            # Build a wrapper whose signature omits `request` so FastMCP
            # generates a schema without it.
            remaining = [p for name, p in sig.parameters.items() if name != "request"]
            wrapper_sig = sig.replace(parameters=remaining)

            @wraps(fn)
            def _schema_stub(**kwargs: Any) -> str:
                raise RuntimeError(f"{tool_name} should not be called via FastMCP dispatch")

            _schema_stub.__signature__ = wrapper_sig  # type: ignore[attr-defined]
            mcp.tool(name=tool_name, description=description)(_schema_stub)
        else:
            mcp.tool(name=tool_name, description=description)(fn)

        return fn

    return decorator


_PRINCIPAL_REDACTED_FIELDS: tuple[str, ...] = ("email", "last_name")


def _clone_request(
    source: HttpRequest, path: str, *, method: str = "GET", body: Any = None, **kwargs: Any
) -> HttpRequest:
    """Clone a Django request for internal view delegation.

    Copies authentication context (user, tenant, identity header) and
    selected tracing headers from the source request so that the target
    view applies the same permission checks and is observable in traces.
    """
    method_upper = method.upper()
    body_data = json.dumps(body) if body is not None else ""
    if method_upper == "POST":
        view_request = _request_factory.post(path, data=body_data, content_type="application/json")
    elif method_upper == "PUT":
        view_request = _request_factory.put(path, data=body_data, content_type="application/json")
    elif method_upper == "PATCH":
        view_request = _request_factory.patch(path, data=body_data, content_type="application/json")
    elif method_upper == "DELETE":
        view_request = _request_factory.delete(path, **kwargs)
    else:
        view_request = _request_factory.get(path, **kwargs)

    view_request._dont_enforce_csrf_checks = True
    view_request.user = source.user
    view_request.tenant = getattr(source, "tenant", None)
    view_request.req_id = getattr(source, "req_id", None)

    if getattr(source, "mcp_source", False):
        view_request.mcp_source = True

    identity = source.META.get(RH_IDENTITY_HEADER)
    if identity:
        view_request.META[RH_IDENTITY_HEADER] = identity

    for header in _FORWARDED_HEADERS:
        value = source.META.get(header)
        if value:
            view_request.META[header] = value

    return view_request


def _call_view_json(
    request: HttpRequest,
    view: Callable[..., Any],
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    query_params: dict[str, str] | None = None,
    **view_kwargs: str,
) -> str:
    """Call a Django view and return the response body as a string.

    Handles response rendering, 204 No Content, and 4xx/5xx error wrapping
    so MCP clients can distinguish success from failure without parsing
    the view's raw output.
    """
    if query_params:
        from urllib.parse import urlencode

        path = f"{path}?{urlencode(query_params)}"
    view_request = _clone_request(request, path, method=method, body=body)
    response = view(view_request, **view_kwargs)
    if hasattr(response, "render"):
        response = response.render()
    if response.status_code == 204:
        return json.dumps({"status": "no_content"})
    content = response.content.decode()
    if response.status_code >= 400:
        return json.dumps({"error": f"HTTP {response.status_code}", "detail": content})
    return content


def _call_view_write(
    request: HttpRequest,
    view: Callable[..., Any],
    path: str,
    body: dict[str, Any],
    *,
    method: str = "POST",
    **view_kwargs: Any,
) -> str:
    """Call a Django view with a write request (POST/PUT/PATCH)."""
    return _call_view_json(request, view, path, method=method, body=body, **view_kwargs)


def _call_view_delete(
    request: HttpRequest,
    view: Callable[..., Any],
    path: str,
    query_params: dict[str, str] | None = None,
    **view_kwargs: Any,
) -> str:
    """Call a Django view with a DELETE request."""
    return _call_view_json(request, view, path, method="DELETE", query_params=query_params, **view_kwargs)


def _resolve_group_uuid(group_uuid: str, group_name: str, tenant) -> tuple[str | None, str | None]:
    """Resolve a group to its UUID from either group_uuid or group_name.

    Returns (resolved_uuid, error_json). On success error_json is None;
    on failure resolved_uuid is None and error_json is a JSON error string.
    """
    if group_uuid:
        return group_uuid, None
    if group_name:
        group = Group.objects.filter(name__iexact=group_name, tenant=tenant).only("uuid").first()
        if not group:
            return None, json.dumps({"error": f"Group '{group_name}' not found"})
        return str(group.uuid), None
    return None, json.dumps({"error": "Either group_uuid or group_name is required"})


def _resolve_group_for_tool(request: HttpRequest, group_uuid: str, group_name: str) -> tuple[str | None, str | None]:
    """Resolve a group UUID from a tool request, checking tenant context first."""
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return None, json.dumps({"error": "No tenant context available"})
    return _resolve_group_uuid(group_uuid, group_name, tenant)


_CROSS_ACCOUNT_LIFECYCLE = (
    "Cross-account request lifecycle: "
    "create → pending (awaits org admin approval) → approved (active access) → expired. "
    "A request can also be denied or cancelled at any stage."
)


def _cross_account_status_counts(org_id: str) -> dict[str, int]:
    """Return pending and expired cross-account request counts in a single query."""
    rows = (
        CrossAccountRequest.objects.filter(target_org=org_id, status__in=["pending", "expired"])
        .values("status")
        .annotate(count=Count("pk"))
    )
    counts = {r["status"]: r["count"] for r in rows}
    return {"pending": counts.get("pending", 0), "expired": counts.get("expired", 0)}


def _format_candidate_name(user: dict) -> str:
    """Build a display name from BOP user data, falling back to username."""
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return name or user.get("username", "")


def _lookup_principal(
    request: HttpRequest, username_or_name: str, tenant
) -> tuple[Principal | None, str | None, list[dict] | None]:
    """Resolve a principal by username or display name.

    Returns (principal, resolved_username, candidates).
    - On exact or unique match: principal and resolved_username are set, candidates is None.
    - On multiple matches: principal/resolved_username are None, candidates is a list of user dicts.
    - On no matches: everything is None.
    """
    principal = Principal.objects.filter(username=username_or_name, tenant=tenant).first()
    if principal:
        return principal, username_or_name, None

    # Username not found -- try display name search via BOP
    result_json = _list_principals_by_name(
        request, username_or_name, limit=5, offset=0, sort_order="asc", status="enabled"
    )
    try:
        result = json.loads(result_json)
        matches = result.get("data", [])
    except (json.JSONDecodeError, AttributeError):
        matches = []

    if len(matches) == 1:
        resolved_username = matches[0].get("username")
        if resolved_username:
            principal = Principal.objects.filter(username=resolved_username, tenant=tenant).first()
            if principal:
                return principal, resolved_username, None

    if len(matches) > 1:
        candidates = [
            {
                "username": u.get("username"),
                "name": _format_candidate_name(u),
            }
            for u in matches
        ]
        return None, None, candidates

    return None, None, None


# --- Tool implementations ---


@register_tool(
    description=(
        "Say hello to the RBAC service. Returns your message echoed back along with "
        "the current server date in UTC. No authentication required. "
        "Use this to verify MCP connectivity."
    ),
    api_version=ApiVersion.UNVERSIONED,
)
def hello(message: str = "Hello, World!") -> str:
    """Respond to a greeting — no authentication required."""
    now: str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return json.dumps({"response": f"RBAC received your message: '{message}'", "date": now})


@register_tool(
    description=(
        "List principals (users) for the authenticated organization. "
        "Supports pagination (limit/offset), sorting (asc/desc), and filtering by status "
        "(enabled/disabled/all). Set 'usernames' (comma-separated) to look up specific users. "
        "Set 'match_criteria' to 'exact' (default) or 'partial' for username matching. "
        "Set username_only='true' to return only usernames. "
        "Set 'name' to search by display name (e.g., 'RBAC Normal' matches 'RBAC Normal For V2'); "
        "cannot be used with 'usernames'. "
        "TROUBLESHOOTING: To find a user by display name, call "
        "list_principals(name='John Smith'). "
        "To confirm a user exists and check their org admin status, call "
        "list_principals(usernames='<user>', match_criteria='exact'). "
        "Returns: {meta: {count}, links, data: [{uuid, username, first_name, is_org_admin, ...}]}. "
        "Sensitive fields (email, last_name) are redacted by default. "
        "Calls: GET /api/v1/principals/\n"
        "Caveats:\n"
        "- Shows only users provisioned in this org -- does not include cross-account (TAM) users or "
        "service accounts from other identity providers.\n"
        "- 'is_org_admin' reflects the current state from the identity provider, not a historical snapshot.\n"
        "- Sensitive fields (email, last_name) are redacted from responses."
    ),
    requires_auth=True,
    redacted_fields=_PRINCIPAL_REDACTED_FIELDS,
)
def list_principals(
    request: HttpRequest,
    *,
    limit: int = 10,
    offset: int = 0,
    sort_order: str = "asc",
    status: str = "enabled",
    username_only: str = "false",
    usernames: str = "",
    match_criteria: str = "",
    name: str = "",
) -> str:
    """List principals by delegating to PrincipalView, with optional name filtering."""
    name = name.strip()

    # Reject conflicting inputs
    if name and usernames:
        return json.dumps({"errors": [{"detail": "Cannot use both 'name' and 'usernames' parameters"}]})

    # If name filter provided, fetch and filter directly via proxy
    if name:
        return _list_principals_by_name(request, name, limit, offset, sort_order, status, username_only)

    # Otherwise, delegate to PrincipalView
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
        "sort_order": sort_order,
        "status": status,
        "username_only": username_only,
    }
    if usernames:
        query_params["usernames"] = usernames
    if match_criteria:
        query_params["match_criteria"] = match_criteria

    path = reverse("v1_management:principals")
    result = _call_view(request, _principal_view, path, query_params)

    if usernames:
        try:
            parsed = json.loads(result)
            if parsed.get("meta", {}).get("count", 1) == 0:
                parsed["hint"] = (
                    f"No users matched the username '{usernames}'. "
                    f"If this is a display name (e.g. first/last name), "
                    f"retry with list_principals(name='{usernames}') to search by display name."
                )
                return json.dumps(parsed, default=str)
        except (json.JSONDecodeError, AttributeError):
            pass

    return _strip_pii_from_principal_list(result, tenant=getattr(request, "tenant", None))


def _list_principals_by_name(
    request: HttpRequest,
    name_filter: str,
    limit: int,
    offset: int,
    sort_order: str,
    status: str,
    username_only: str = "false",
) -> str:
    """Fetch principals from BOP and filter by name (case-insensitive substring match)."""
    proxy = PrincipalProxy()
    org_id = request.user.org_id
    name_lower = name_filter.lower()
    return_username_only = username_only.lower() == "true"

    options = {
        "sort_order": sort_order,
        "status": status,
    }

    filtered_users = []
    bop_offset = 0
    bop_limit = 500
    max_users = 2000

    while bop_offset < max_users:
        resp = proxy.request_principals(org_id=org_id, limit=bop_limit, offset=bop_offset, options=options)
        if resp.get("status_code") != 200:
            return json.dumps({"errors": resp.get("errors", [{"detail": "Failed to fetch principals"}])})

        data = resp.get("data", [])
        if isinstance(data, dict):
            users = data.get("users", [])
            total_count = int(data.get("userCount", 0))
        else:
            users = data
            total_count = int(resp.get("userCount", len(users)))

        if not users:
            break

        for user in users:
            first = (user.get("first_name") or "").lower()
            last = (user.get("last_name") or "").lower()
            full_name = f"{first} {last}".strip()
            if name_lower in full_name:
                filtered_users.append(user)

        bop_offset += bop_limit
        if bop_offset >= total_count:
            break

    paginated = filtered_users[offset : offset + limit]  # noqa: E203
    path = request.path

    if return_username_only:
        paginated = [{"username": u.get("username")} for u in paginated]

    base_params = f"name={name_filter}&sort_order={sort_order}&status={status}&username_only={username_only}"

    raw_json = json.dumps(
        {
            "meta": {"count": len(filtered_users), "limit": limit, "offset": offset},
            "links": {
                "first": f"{path}?{base_params}&limit={limit}&offset=0",
                "next": (
                    f"{path}?{base_params}&limit={limit}&offset={offset + limit}"
                    if offset + limit < len(filtered_users)
                    else None
                ),
                "previous": (
                    f"{path}?{base_params}&limit={limit}&offset={max(0, offset - limit)}" if offset > 0 else None
                ),
                "last": f"{path}?{base_params}&limit={limit}&offset={max(0, len(filtered_users) - limit)}",
            },
            "data": paginated,
        },
        default=str,
    )
    return _strip_pii_from_principal_list(raw_json, tenant=getattr(request, "tenant", None))


def _strip_pii_from_principal_list(raw_json: str, tenant: Any = None, redacted_fields: tuple[str, ...] = ()) -> str:
    """Strip redacted fields from a principal-list API response, enriching with UUIDs."""
    fields = redacted_fields or _PRINCIPAL_REDACTED_FIELDS
    if not _is_pii_redaction_enabled():
        return raw_json
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return raw_json

    data = parsed.get("data", [])
    if tenant and data:
        usernames = [item.get("username") for item in data if item.get("username")]
        if usernames:
            uuid_map = dict(
                Principal.objects.filter(username__in=usernames, tenant=tenant).values_list("username", "uuid")
            )
            for item in data:
                uname = item.get("username")
                if uname and uname in uuid_map:
                    item["uuid"] = str(uuid_map[uname])

    for item in data:
        for field in fields:
            item.pop(field, None)
    return json.dumps(parsed, default=str)


def _call_view(
    request: HttpRequest,
    view: Callable[..., Any],
    path: str,
    query_params: dict[str, str],
    **view_kwargs: str,
) -> str:
    """Call a Django view with cloned request and return the response body.

    For detail views, pass the lookup kwarg as a keyword argument,
    e.g. ``_call_view(request, view, path, {}, pk=value)``.

    DRF responses are rendered via their configured renderer so the
    output matches exactly what the API would return.
    """
    view_request = _clone_request(request, path, data=query_params)
    response = view(view_request, **view_kwargs)
    if hasattr(response, "render"):
        response = response.render()
    return response.content.decode()


# ┌──────────────────────────────────────┬───────────┬────────────────────────────────────────────────────┐
# │ MCP Tool                             │ Version   │ API Endpoint                                       │
# ├──────────────────────────────────────┼───────────┼────────────────────────────────────────────────────┤
# │ hello                                │ unver.    │ (none -- in-process greeting)                      │
# │ get_status                           │ unver.    │ GET /api/v1/status/                                │
# │ list_principals                      │ common    │ GET /api/v1/principals/                            │
# │ list_permissions                     │ common    │ GET /api/v1/permissions/                           │
# │ list_permission_options              │ common    │ GET /api/v1/permissions/options/                   │
# │ list_audit_logs                      │ common    │ GET /api/v1/auditlogs/                             │
# │ get_rbac_recent_changes              │ common    │ (in-process audit log analysis)                    │
# │ investigate_group_changes            │ common    │ (orchestrates audit log + authorization)           │
# │ investigate_user_access              │ common    │ (groups + roles + permissions analysis)            │
# │ list_groups                          │ common    │ GET /api/v1/groups/                                │
# │ get_group                            │ common    │ GET /api/v1/groups/{uuid}/                         │
# │ list_group_principals                │ common    │ GET /api/v1/groups/{uuid}/principals/              │
# │ list_cross_account_requests          │ common    │ GET /api/v1/cross-account-requests/                │
# │ get_cross_account_request            │ common    │ GET /api/v1/cross-account-requests/{id}/           │
# │ investigate_tam_access               │ common    │ (orchestrates cross-account + roles)               │
# │ audit_redhat_access                  │ common    │ (cross-account + roles + audit logs)               │
# │ list_workspaces                      │ common    │ GET /api/v2/workspaces/                            │
# │ get_workspace                        │ common    │ GET /api/v2/workspaces/{uuid}/                     │
# │ search_roles                         │ unified   │ V1: GET /api/v1/roles/                             │
# │                                      │           │ V2: GET /api/v2/roles/                             │
# │ get_role                             │ unified   │ V1: GET /api/v1/roles/{uuid}/ + /access/           │
# │                                      │           │ V2: GET /api/v2/roles/{uuid}/                      │
# │ check_user_permission                │ unified   │ V1: GET /api/v1/access/                            │
# │                                      │           │ V2: role-bindings -> roles                         │
# │ list_access                          │ v1        │ GET /api/v1/access/                                │
# │ list_group_roles                     │ v1        │ GET /api/v1/groups/{uuid}/roles/                   │
# │ list_role_access                     │ v1        │ GET /api/v1/roles/{uuid}/access/                   │
# │ list_role_bindings                   │ v2        │ GET /api/v2/role-bindings/                         │
# │ list_role_bindings_by_subject        │ v2        │ GET /api/v2/role-bindings/by-subject/              │
# ├──────────────────────────────────────┼───────────┼────────────────────────────────────────────────────┤
# │ create_group                         │ common W  │ POST /api/v1/groups/                              │
# │ add_principals_to_group              │ common W  │ POST /api/v1/groups/{uuid}/principals/            │
# │ add_roles_to_group                   │ v1 W      │ POST /api/v1/groups/{uuid}/roles/                 │
# │ create_role_v1                       │ v1 W      │ POST /api/v1/roles/                               │
# │ create_role                          │ v2 W      │ POST /api/v2/roles/                               │
# │ create_role_bindings                 │ v2 W      │ POST /api/v2/role-bindings/:batchCreate           │
# │ create_workspace                     │ v2 W      │ POST /api/v2/workspaces/                          │
# │ create_cross_account_request         │ common W  │ POST /api/v1/cross-account-requests/              │
# │ update_group                         │ common W  │ PUT /api/v1/groups/{uuid}/                        │
# │ update_role_v1                       │ v1 W      │ PUT /api/v1/roles/{uuid}/                         │
# │ patch_role_v1                        │ v1 W      │ PATCH /api/v1/roles/{uuid}/                       │
# │ update_role                          │ v2 W      │ PUT /api/v2/roles/{uuid}/                         │
# │ update_role_binding                  │ v2 W      │ PUT /api/v2/role-bindings/by-subject/              │
# │ update_workspace                     │ v2 W      │ PUT /api/v2/workspaces/{uuid}/                    │
# │ move_workspace                       │ v2 W      │ POST /api/v2/workspaces/{uuid}/move/              │
# │ update_cross_account_request         │ common W  │ PUT /api/v1/cross-account-requests/{id}/          │
# │ patch_cross_account_request          │ common W  │ PATCH /api/v1/cross-account-requests/{id}/        │
# │ delete_group                         │ common W  │ DELETE /api/v1/groups/{uuid}/                     │
# │ remove_principals_from_group         │ common W  │ DELETE /api/v1/groups/{uuid}/principals/          │
# │ remove_roles_from_group              │ v1 W      │ DELETE /api/v1/groups/{uuid}/roles/               │
# │ delete_role_v1                       │ v1 W      │ DELETE /api/v1/roles/{uuid}/                      │
# │ bulk_delete_roles                    │ v2 W      │ POST /api/v2/roles/:batchDelete                   │
# │ delete_workspace                     │ v2 W      │ DELETE /api/v2/workspaces/{uuid}/                 │
# └──────────────────────────────────────┴───────────┴────────────────────────────────────────────────────┘


@register_tool(
    description=(
        "Get RBAC server status including API version, commit hash, server address, "
        "platform info, Python version and loaded modules. No authentication required. "
        "Calls: GET /api/v1/status/"
    ),
    api_version=ApiVersion.UNVERSIONED,
)
def get_status(request: HttpRequest) -> str:
    """Return server status by delegating to the status view."""
    path = reverse("v1_api:server-status")
    return _call_view(request, _status_view, path, {})


@register_tool(
    description=(
        "Check MCP server health: tool registry integrity, database connectivity, "
        "and Redis availability. No authentication required — designed for "
        "infrastructure probing and readiness checks. "
        "Returns status 'ok' when all checks pass, 'degraded' with per-check "
        "details when any check fails."
    ),
    requires_auth=False,
    api_version=ApiVersion.UNVERSIONED,
)
def health_check() -> str:
    """Verify MCP-specific health: tool registry, database, and Redis."""
    checks: dict[str, str] = {}
    details: dict[str, str] = {}

    # 1. Tool registry check
    try:
        config_count = len(_TOOL_CONFIG)
        tools = _get_tools()
        tools_count = len(tools)
        if config_count > 0 and tools_count > 0:
            checks["tools"] = "ok"
        else:
            checks["tools"] = "error"
            details["tools"] = "registry empty"
    except Exception:
        logger.exception("mcp: health_check tools registry probe failed")
        checks["tools"] = "error"
        details["tools"] = "unavailable"

    # 2. Database check
    try:
        _check_database()
        checks["database"] = "ok"
    except Exception:
        logger.exception("mcp: health_check database probe failed")
        checks["database"] = "error"
        details["database"] = "unavailable"

    # 3. Redis check
    try:
        _check_redis()
        checks["redis"] = "ok"
    except Exception:
        logger.exception("mcp: health_check redis probe failed")
        checks["redis"] = "error"
        details["redis"] = "unavailable"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    result: dict[str, Any] = {"status": overall, "checks": checks}
    if details:
        result["details"] = details
    return json.dumps(result)


def _check_database() -> None:
    """Probe database connectivity via a lightweight ORM query."""
    Tenant.objects.exists()


def _check_redis() -> None:
    """Probe Redis connectivity via PING. No-op when Redis is mocked."""
    conn = _get_redis()
    if conn is None:
        return
    conn.ping()


@register_tool(
    description=(
        "List permissions available in RBAC. Each permission has the format 'application:resource_type:verb'. "
        "Filter by application, resource_type, or verb. Supports pagination and ordering by 'permission' or "
        "'-permission'. "
        "Returns: {meta: {count}, links, data: [{application, resource_type, verb, permission}]}. "
        "Calls: GET /api/v1/permissions/\n"
        "Caveats:\n"
        "- Permissions exist independently of roles. A permission appearing here does not mean any role "
        "grants it -- use search_roles(permission='...') to find roles that include a specific permission.\n"
        "- Wildcard permissions ('*') are expanded at access-check time, not in this listing."
    ),
    requires_auth=True,
)
def list_permissions(
    request: HttpRequest,
    *,
    application: str = "",
    resource_type: str = "",
    verb: str = "",
    limit: int = 10,
    offset: int = 0,
    order_by: str = "",
) -> str:
    """List permissions by delegating to PermissionViewSet."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if application:
        query_params["application"] = application
    if resource_type:
        query_params["resource_type"] = resource_type
    if verb:
        query_params["verb"] = verb
    if order_by:
        query_params["order_by"] = order_by

    path = reverse("v1_management:permission-list")
    return _call_view(request, _permission_list_view, path, query_params)


@register_tool(
    description=(
        "List audit log entries recording RBAC changes for the authenticated organization. "
        "Each entry records who changed what (principal, resource_type, action). "
        "Filter by principal_username (who made the change), resource_type "
        "(group/role/role_v2/user/permission/workspace/role_binding), action (add/edit/delete/create/remove), "
        "group_name (filter by group), or role_name (filter by role). "
        "IMPORTANT: Always set group_name and/or role_name to narrow results (avoids scanning 100+ entries). "
        "Example: list_audit_logs(resource_type='group', action='add', group_name='Contractors', "
        "role_name='Vulnerability administrator') "
        "Set include_authorization=true to see the role/permission that authorized the action. "
        "Order by: 'created', 'principal_username', 'resource_type', 'action' (prefix with '-' to reverse). "
        "NOTE: Authorization shows actor's CURRENT role/permission — may differ from time of change. "
        "RESPONSE FORMAT: When summarizing audit log entries, include the action, resource type, who performed it "
        "(principal_username), the date and time (formatted as '14 April at 9:32 AM', not numeric like "
        "'2025-04-14T09:32:15'), and the description field. When include_authorization=true, also state the "
        "role name (authorized_by.role), "
        "group name (authorized_by.via_group), and specific permission (authorized_by.permission) that "
        "authorized the action.\n"
        "Caveats:\n"
        "- Does NOT capture: IP addresses, session IDs, login/logout events, geographic location, or "
        "before/after state diffs. This is an activity log of RBAC changes, not a full security audit trail.\n"
        "- No server-side date range filter. Time-bounded queries require client-side pagination through "
        "results ordered by '-created' until entries fall outside the desired window.\n"
        "- Cross-account request approval/denial events may not appear here -- those are tracked in the "
        "cross-account-requests API, not the audit log."
    ),
    requires_auth=True,
    required_relation="rbac_roles_read",
    v1_permission=("admin", "only"),
)
def list_audit_logs(
    request: HttpRequest,
    *,
    limit: int = 10,
    offset: int = 0,
    order_by: str = "-created",
    principal_username: str = "",
    resource_type: str = "",
    action: str = "",
    group_name: str = "",
    role_name: str = "",
    include_authorization: bool = False,
) -> str:
    """List audit logs with optional authorization context."""
    valid_order_fields = {"created", "principal_username", "resource_type", "action"}
    order_field = order_by.lstrip("-")
    if order_field not in valid_order_fields:
        return json.dumps(
            {"error": f"Invalid order_by field '{order_by}'. Valid fields: {', '.join(sorted(valid_order_fields))}"}
        )

    tenant = getattr(request, "tenant", None)
    if not tenant:
        return json.dumps({"error": "No tenant context available"})

    queryset = AuditLog.objects.filter(tenant=tenant).order_by(order_by)

    if principal_username:
        queryset = queryset.filter(principal_username__icontains=principal_username)
    if resource_type:
        queryset = queryset.filter(resource_type=resource_type)
    if action:
        queryset = queryset.filter(action=action)
    if group_name:
        queryset = queryset.filter(description__icontains=group_name)
    if role_name:
        queryset = queryset.filter(description__icontains=role_name)

    total_count = queryset.count()
    entries = list(queryset[offset : offset + limit])  # noqa: E203

    if not entries:
        return json.dumps({"meta": {"count": total_count}, "data": []})

    if not include_authorization:
        # Return basic audit log data
        data = [
            {
                "principal_username": e.principal_username,
                "action": e.action,
                "resource_type": e.resource_type,
                "description": e.description,
                "created": e.created.isoformat() if e.created else None,
                "source": e.source,
            }
            for e in entries
        ]
        return json.dumps({"meta": {"count": total_count}, "data": data}, default=str)

    # Enrich with authorization context
    auth_cache: dict[str, dict[str, dict[str, Any] | None]] = {}
    org_admin_cache: dict[str, bool] = {}
    org_admin_auth: dict[str, Any] = {
        "role": "Org Admin",
        "via_group": None,
        "permission": "(bypasses all RBAC checks)",
    }
    results = []

    for entry in entries:
        actor = entry.principal_username
        entry_action = entry.action
        entry_resource = entry.resource_type
        required_perms = _get_required_permissions(entry_resource, entry_action)
        cache_key = f"{entry_resource}:{entry_action}"

        if actor not in auth_cache:
            auth_cache[actor] = {}

        if actor not in org_admin_cache:
            org_admin_cache[actor] = _is_org_admin(actor, tenant.org_id)

        auth_info: dict[str, Any] | None
        if org_admin_cache[actor]:
            auth_info = org_admin_auth
        elif cache_key not in auth_cache[actor]:
            auth_cache[actor][cache_key] = _find_authorizing_role(actor, tenant, required_perms)
            auth_info = auth_cache[actor][cache_key]
        else:
            auth_info = auth_cache[actor][cache_key]

        result: dict[str, Any] = {
            "principal_username": actor,
            "action": entry_action,
            "resource_type": entry_resource,
            "description": entry.description,
            "created": entry.created.isoformat() if entry.created else None,
            "source": entry.source,
            "authorized_by": auth_info,
        }

        if not auth_info:
            result["note"] = f"User '{actor}' not found or no longer has permissions. Required: {required_perms}"

        results.append(result)

    return json.dumps({"meta": {"count": total_count}, "data": results}, default=str)


# Permission requirements by resource_type and action
_REQUIRED_PERMISSIONS: dict[str, dict[str, list[str]]] = {
    "group": {
        "create": ["rbac:group:write"],
        "delete": ["rbac:group:write"],
        "edit": ["rbac:group:write"],
        "add": ["rbac:group:write", "rbac:principal:write"],
        "remove": ["rbac:group:write", "rbac:principal:write"],
    },
    "role": {
        "create": ["rbac:role:write"],
        "delete": ["rbac:role:write"],
        "edit": ["rbac:role:write"],
    },
    "role_v2": {
        "create": ["rbac:role:write"],
        "delete": ["rbac:role:write"],
        "edit": ["rbac:role:write"],
    },
    "workspace": {
        "create": ["rbac:workspace:write"],
        "delete": ["rbac:workspace:write"],
        "edit": ["rbac:workspace:write"],
    },
    "role_binding": {
        "create": ["rbac:role_binding:write"],
        "delete": ["rbac:role_binding:write"],
    },
    "user": {
        "add": ["rbac:principal:write"],
        "remove": ["rbac:principal:write"],
    },
    "permission": {
        "create": ["rbac:role:write"],
        "edit": ["rbac:role:write"],
        "delete": ["rbac:role:write"],
    },
}


def _get_required_permissions(resource_type: str, action: str) -> list[str]:
    """Get the permissions required for a given resource_type and action."""
    resource_perms = _REQUIRED_PERMISSIONS.get(resource_type, {})
    return resource_perms.get(action, [f"rbac:{resource_type}:write"])


def _is_org_admin(username: str, org_id: str) -> bool:
    """Check if a user is an org admin by querying BOP."""
    try:
        proxy = PrincipalProxy()
        resp = proxy.request_filtered_principals([username], org_id=org_id, limit=1)
        if resp.get("status_code") == 200:
            data = resp.get("data", [])
            if data and len(data) > 0:
                return data[0].get("is_org_admin", False)
    except Exception:
        logger.debug("mcp: failed to check org admin status for user=%s", username)
    return False


def _find_authorizing_role(username: str, tenant: Any, required_perms: list[str]) -> dict[str, Any] | None:
    """Find the role and permission that authorized an action."""
    principal = Principal.objects.filter(username=username, tenant=tenant).first()
    if not principal:
        return None

    # Single query: get all permissions the user has with their role/group context
    access_entries = (
        Access.objects.filter(
            role__policies__group__principals=principal,
            role__policies__group__tenant=tenant,
        )
        .select_related("permission", "role")
        .values(
            "permission__permission",
            "role__name",
            "role__display_name",
            "role__policies__group__name",
        )
    )

    # Build required permission set for fast lookup
    required_set = set(required_perms)

    for entry in access_entries:
        granted_perm = entry["permission__permission"]
        # Check exact match first (fast path)
        if granted_perm in required_set:
            return {
                "role": entry["role__display_name"] or entry["role__name"],
                "via_group": entry["role__policies__group__name"],
                "permission": granted_perm,
            }
        # Check wildcard match
        for required in required_perms:
            if _permission_matches(granted_perm, required):
                return {
                    "role": entry["role__display_name"] or entry["role__name"],
                    "via_group": entry["role__policies__group__name"],
                    "permission": granted_perm,
                }

    return None


@register_tool(
    description=(
        "List access permissions for a principal (V1 API only). By default shows access for the "
        "currently authenticated user. Set 'username' to query another user's access (requires "
        "org admin or rbac:principal:read permission). Each entry is a permission string "
        "with optional resource definitions that further constrain it. "
        "IMPORTANT: This is a V1-only endpoint. For V2 organizations, this returns only legacy "
        "V1 access and does NOT include V2 role binding permissions. For V2 orgs, use "
        "check_user_permission (which auto-detects V1/V2) or list_role_bindings with "
        "granted_subject_type='principal' and granted_subject_principal_user_id='<username>'. "
        "Order by: 'application', 'resource_type', 'verb' (prefix with '-' to reverse). "
        "Returns: {meta: {count}, links, data: [{permission, resourceDefinitions: [...]}]}. "
        "Calls: GET /api/v1/access/\n"
        "Caveats:\n"
        "- This shows the flattened effective permissions but not the path: you cannot see which "
        "role or group granted each permission. Use list_group_roles + list_role_access to trace "
        "the full chain."
    ),
    requires_auth=True,
    api_version=ApiVersion.V1,
)
def list_access(
    request: HttpRequest,
    *,
    application: str,
    username: str = "",
    limit: int = 10,
    offset: int = 0,
    order_by: str = "",
    status: str = "enabled",
) -> str:
    """List principal access by delegating to AccessView."""
    query_params: dict[str, str] = {
        "application": application,
        "limit": str(limit),
        "offset": str(offset),
        "status": status,
    }
    if username:
        query_params["username"] = username
    if order_by:
        query_params["order_by"] = order_by

    path = reverse("v1_management:access")
    return _call_view(request, _access_view, path, query_params)


@register_tool(
    description=(
        "Get distinct values for a permission field. Use this to discover what applications, "
        "resource_types, or verbs exist. The 'field' parameter is required and must be one of: "
        "'application', 'resource_type', 'verb'. Optionally filter by application, resource_type, "
        "or verb (comma-separated for multiple). "
        "Returns: {meta: {count}, links, data: ['value1', 'value2', ...]}. "
        "Calls: GET /api/v1/permissions/options/\n"
        "Caveats:\n"
        "- Returns values from the permission registry, not from what is actively assigned. An "
        "application or verb may appear here even if no role in this org currently uses it.\n"
        "- Application names are identifiers (e.g., 'cost-management', 'advisor'), not display "
        "names. There is no mapping from these identifiers to user-facing product names."
    ),
    requires_auth=True,
)
def list_permission_options(
    request: HttpRequest,
    *,
    field: str,
    application: str = "",
    resource_type: str = "",
    verb: str = "",
    limit: int = 10,
    offset: int = 0,
) -> str:
    """List distinct permission field values by delegating to PermissionViewSet.options."""
    query_params: dict[str, str] = {
        "field": field,
        "limit": str(limit),
        "offset": str(offset),
    }
    if application:
        query_params["application"] = application
    if resource_type:
        query_params["resource_type"] = resource_type
    if verb:
        query_params["verb"] = verb

    path = reverse("v1_management:permission-options")
    return _call_view(request, _permission_options_view, path, query_params)


@register_tool(
    description=(
        "List roles assigned to a specific group. Provide either group_uuid OR group_name "
        "(if both are provided, group_uuid takes precedence). "
        "Use group_name for convenience (case-insensitive lookup). "
        "Example: list_group_roles(group_name='<group-name>') to see roles assigned to that group. "
        "Filter results by role_name, role_description, role_display_name, or role_system. "
        "Set exclude='true' to list roles NOT in the group. "
        "Order by: 'name', 'display_name', 'modified', 'policyCount' (prefix with '-' to reverse). "
        "Returns: {meta: {count}, links, data: [{uuid, name, description, system, ...}]}.\n"
        "Caveats:\n"
        "- Non-org-admin users cannot modify groups that contain roles with RBAC write permissions "
        "(e.g., 'User Access administrator'). The Default admin access group cannot be modified at all."
    ),
    requires_auth=True,
    api_version=ApiVersion.V1,
)
def list_group_roles(
    request: HttpRequest,
    *,
    group_uuid: str = "",
    group_name: str = "",
    limit: int = 10,
    offset: int = 0,
    order_by: str = "",
    role_name: str = "",
    role_description: str = "",
    role_display_name: str = "",
    role_system: str = "",
    exclude: str = "false",
) -> str:
    """List roles for a group by delegating to GroupViewSet.roles."""
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return json.dumps({"error": "No tenant context available"})

    resolved_uuid, error = _resolve_group_uuid(group_uuid, group_name, tenant)
    if error:
        return error
    assert resolved_uuid is not None

    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if order_by:
        query_params["order_by"] = order_by
    if role_name:
        query_params["role_name"] = role_name
    if role_description:
        query_params["role_description"] = role_description
    if role_display_name:
        query_params["role_display_name"] = role_display_name
    if role_system:
        query_params["role_system"] = role_system
    if exclude != "false":
        query_params["exclude"] = exclude

    path = reverse("v1_management:group-roles", kwargs={"uuid": resolved_uuid})
    return _call_view(request, _group_roles_view, path, query_params, uuid=resolved_uuid)


@register_tool(
    description=(
        "List access permissions granted by a specific role (V1 API). Each access entry is a "
        "permission string with optional resource definitions. "
        "Returns: {meta: {count}, links, data: [{permission, resourceDefinitions: [...]}]}. "
        "Calls: GET /api/v1/roles/{uuid}/access/\n"
        "Caveats:\n"
        "- This shows what the role grants in isolation. A user's effective access is the union of "
        "all roles across all their groups -- a missing permission here may be covered by another role.\n"
        "- No workspace or scope concept in V1. Permissions attach to roles, roles to groups, groups "
        "to principals -- there is no way to limit a role to a specific workspace or resource subset "
        "beyond ResourceDefinitions."
    ),
    requires_auth=True,
    api_version=ApiVersion.V1,
)
def list_role_access(
    request: HttpRequest,
    *,
    role_uuid: str,
    limit: int = 10,
    offset: int = 0,
) -> str:
    """List access for a role by delegating to RoleViewSet.access."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    path = reverse("v1_management:role-access", kwargs={"uuid": role_uuid})
    return _call_view(request, _role_access_view, path, query_params, uuid=role_uuid)


@register_tool(
    description=(
        "Search and filter roles by name, permission, application, or other criteria. "
        "Automatically detects whether the organization uses V1 or V2 and routes accordingly. "
        "Best tool for answering 'which role grants permission X?' or 'find role named Y'. "
        "TROUBLESHOOTING: To find a role by name, call search_roles(name='<role_name>'). "
        "To find which roles grant a specific permission, call "
        "search_roles(permission='<app>:<resource>:<verb>'). Accepts comma-separated permissions. "
        "To see all roles for an application, call search_roles(application='<app>'). "
        "To find roles held by a specific user (V1 orgs only), call search_roles(username='<user>'). "
        "V2 orgs support name with '*' wildcards (e.g., name='Cost*') and resource_type filter. "
        "V1 orgs additionally support display_name, system flag, and username filters. "
        "Returns: {meta: {count}, links, data: [{uuid, name, description, ...}], org_version: 'v1'|'v2'}.\n"
        "Caveats:\n"
        "- The permission filter finds roles containing that permission, but does not account for "
        "ResourceDefinition filters that may narrow the permission's effective scope.\n"
        "- Role names are not unique across V1 and V2. The org_version field indicates which API "
        "version the result came from.\n"
        "- The username filter is V1-only. For V2 orgs, it is ignored and returned in ignored_filters "
        "with guidance to use list_role_bindings instead."
    ),
    requires_auth=True,
    api_version=ApiVersion.UNIFIED,
    required_relation="rbac_roles_read",
    v1_permission=("role", "read"),
)
def search_roles(
    request: HttpRequest,
    *,
    limit: int = 10,
    offset: int = 0,
    name: str = "",
    display_name: str = "",
    permission: str = "",
    application: str = "",
    system: str = "",
    resource_type: str = "",
    order_by: str = "",
    username: str = "",
) -> str:
    """Search roles, auto-detecting V1/V2 and delegating to the appropriate view."""
    tenant = getattr(request, "tenant", None)
    if tenant and is_v2_write_activated(tenant):
        return _search_roles_v2(
            request,
            limit,
            offset,
            name,
            resource_type,
            permission,
            order_by,
            username,
            display_name,
            application,
            system,
        )
    return _search_roles_v1(
        request, limit, offset, name, display_name, permission, application, system, order_by, username
    )


def _search_roles_v1(
    request: HttpRequest,
    limit: int,
    offset: int,
    name: str,
    display_name: str,
    permission: str,
    application: str,
    system: str,
    order_by: str,
    username: str,
) -> str:
    """Search roles using V1 API."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if name:
        query_params["name"] = name
    if display_name:
        query_params["display_name"] = display_name
    if permission:
        query_params["permission"] = permission
    if application:
        query_params["application"] = application
    if system:
        query_params["system"] = system
    if order_by:
        query_params["order_by"] = order_by
    if username:
        query_params["username"] = username

    path = reverse("v1_management:role-list")
    raw = _call_view(request, _role_v1_list_view, path, query_params)
    result = json.loads(raw)
    result["org_version"] = "v1"
    return json.dumps(result)


def _search_roles_v2(
    request: HttpRequest,
    limit: int,
    offset: int,
    name: str,
    resource_type: str,
    permission: str,
    order_by: str,
    username: str,
    display_name: str,
    application: str,
    system: str,
) -> str:
    """Search roles using V2 API."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if name:
        query_params["name"] = name
    if resource_type:
        query_params["resource_type"] = resource_type
    if permission:
        query_params["permission"] = permission
    if order_by:
        query_params["order_by"] = order_by

    path = reverse("v2_management:roles-list")
    raw = _call_view(request, _role_v2_list_view, path, query_params)
    result = json.loads(raw)
    result["org_version"] = "v2"

    ignored_filters: dict[str, dict[str, str]] = {}

    if username:
        ignored_filters["username"] = {
            "value": username,
            "reason": "V2 orgs use role bindings instead of group-based role assignment.",
            "alternative": (
                "Use list_role_bindings(granted_subject_type='principal', "
                f"granted_subject_principal_user_id='{username}') to find roles bound to this user."
            ),
        }

    if display_name:
        ignored_filters["display_name"] = {
            "value": display_name,
            "reason": "V2 roles do not have a separate display_name field.",
            "alternative": "Use the 'name' parameter instead, which supports wildcard matching (e.g., name='Cost*').",
        }

    if application:
        ignored_filters["application"] = {
            "value": application,
            "reason": "V2 roles do not support filtering by application.",
            "alternative": (
                "Use the 'permission' parameter to filter by specific permissions "
                "(e.g., permission='cost-management:*:*')."
            ),
        }

    if system:
        ignored_filters["system"] = {
            "value": system,
            "reason": "V2 roles use 'type' (custom/system/seeded) instead of a boolean 'system' flag.",
            "alternative": "V2 role results include a 'type' field you can filter client-side.",
        }

    if ignored_filters:
        result["ignored_filters"] = ignored_filters

    return json.dumps(result)


@register_tool(
    description=(
        "Get details of a specific role by UUID, including its permissions. "
        "Automatically detects whether the organization uses V1 or V2 and routes accordingly. "
        "Returns: {uuid, name, description, permissions: [...], org_version: 'v1'|'v2'}."
    ),
    requires_auth=True,
    api_version=ApiVersion.UNIFIED,
    required_relation="rbac_roles_read",
    v1_permission=("role", "read"),
)
def get_role(
    request: HttpRequest,
    *,
    role_uuid: str,
) -> str:
    """Get a single role, auto-detecting V1/V2 and delegating to the appropriate view."""
    tenant = getattr(request, "tenant", None)
    if tenant and is_v2_write_activated(tenant):
        return _get_role_v2(request, role_uuid)
    return _get_role_v1(request, role_uuid)


def _get_role_v1(request: HttpRequest, role_uuid: str) -> str:
    """Get role details using V1 API (retrieve + access)."""
    detail_path = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
    detail_raw = _call_view(request, _role_v1_detail_view, detail_path, {}, uuid=role_uuid)
    result = json.loads(detail_raw)

    access_path = reverse("v1_management:role-access", kwargs={"uuid": role_uuid})
    access_raw = _call_view(request, _role_access_view, access_path, {"limit": "1000"}, uuid=role_uuid)
    access_data = json.loads(access_raw)

    result["permissions"] = access_data.get("data", [])
    result["org_version"] = "v1"
    return json.dumps(result)


def _get_role_v2(request: HttpRequest, role_uuid: str) -> str:
    """Get role details using V2 API."""
    path = reverse("v2_management:roles-detail", kwargs={"uuid": role_uuid})
    raw = _call_view(request, _role_v2_detail_view, path, {}, uuid=role_uuid)
    result = json.loads(raw)
    result["org_version"] = "v2"
    return json.dumps(result)


@register_tool(
    description=(
        "Pre-flight check for a custom role: analyze what permissions it grants before assigning it to users. "
        "Best tool for answering 'What will users with this custom role be able to do?' or validating custom roles. "
        "NOTE: This tool only checks custom roles (tenant-specific), not system/seeded roles. "
        "SCENARIO: Before assigning a new 'Patch Reviewer' custom role to 200 users, call "
        "check_role_permissions(role_name='Patch Reviewer') to see exactly what it grants. "
        "Takes a role name (required) and finds the custom role, lists all its permissions grouped by application, "
        "expands wildcards, identifies what verbs are NOT included (e.g., no :write or :create), "
        "and checks for potential cross-app permissions. "
        "Automatically detects whether the organization uses V1 or V2 and routes accordingly. "
        "Set include_available_permissions=true to also list what permissions exist in each application "
        "that the role does NOT include. "
        "Returns: {role: {uuid, name, description, system/type}, permissions: {summary, by_application, "
        "expanded_permissions, verbs_included, verbs_not_included}, coverage_analysis, recommendations, org_version}. "
        "AFTER ANALYSIS: Present numbered options: "
        "(1) create a new group and attach the role, ready for user assignment, "
        "(2) update the role's metadata (e.g. display_name) before assignment, "
        "(3) add additional permissions to the role. "
        "Format as 'Reply 1, 2, or 3 -- or no'. Do NOT execute write tools without explicit user selection.\n"
        "Caveats:\n"
        "- Permission names are naming conventions only -- RBAC cannot confirm what UI elements "
        "or API endpoints they control.\n"
        "- ResourceDefinition filters are stored but enforced by the consuming application, not RBAC."
    ),
    requires_auth=True,
    api_version=ApiVersion.UNIFIED,
    required_relation="rbac_roles_read",
    v1_permission=("role", "read"),
)
def check_role_permissions(
    request: HttpRequest,
    *,
    role_name: str,
    include_available_permissions: bool = False,
) -> str:
    """Pre-flight check for a role: analyze what permissions it grants."""
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return json.dumps({"error": "No tenant context available"})

    # Detect V1 vs V2
    is_v2 = is_v2_write_activated(tenant)

    if is_v2:
        return _check_role_permissions_v2(tenant, role_name, include_available_permissions)
    return _check_role_permissions_v1(tenant, role_name, include_available_permissions)


def _check_role_permissions_v1(
    tenant: Tenant,
    role_name: str,
    include_available_permissions: bool,
) -> str:
    """Pre-flight check for a V1 custom role."""
    # Step 1: Find the custom role by name (case-insensitive exact match first, then partial)
    role = Role.objects.filter(name__iexact=role_name, tenant=tenant, system=False).first()

    if not role:
        # Try partial match and suggest (custom roles only)
        roles = Role.objects.filter(name__icontains=role_name, tenant=tenant).values("uuid", "name", "system")[:5]
        if roles:
            suggestions = [{"uuid": str(r["uuid"]), "name": r["name"], "system": r["system"]} for r in roles]
            return json.dumps(
                {
                    "error": f"Role '{role_name}' not found (exact match)",
                    "did_you_mean": suggestions,
                    "hint": "Use the exact role name from the suggestions above.",
                }
            )
        return json.dumps(
            {
                "error": f"Role '{role_name}' not found",
                "hint": "Use search_roles(name='<partial>') to search for roles.",
            }
        )

    # Step 2: Get all access entries (permissions) for the role
    access_entries = Access.objects.filter(role=role).select_related("permission").all()
    permissions = [access.permission for access in access_entries if access.permission]

    # Build role info for V1
    role_info = {
        "uuid": str(role.uuid),
        "name": role.name,
        "display_name": role.display_name or role.name,
        "description": role.description or "",
        "system": getattr(role, "system", False),
        "platform_default": getattr(role, "platform_default", False),
    }

    return _build_role_permissions_result(role_info, permissions, include_available_permissions, "v1")


def _check_role_permissions_v2(
    tenant: Tenant,
    role_name: str,
    include_available_permissions: bool,
) -> str:
    """Pre-flight check for a V2 custom role."""
    # Step 1: Find the custom role by name (case-insensitive exact match first, then partial)
    role = RoleV2.objects.filter(name__iexact=role_name, tenant=tenant).first()

    if not role:
        # Try partial match and suggest (custom roles only)
        roles = RoleV2.objects.filter(name__icontains=role_name, tenant=tenant, type=RoleV2.Types.CUSTOM).values(
            "uuid", "name", "type"
        )[:5]
        if roles:
            suggestions = [
                {"uuid": str(r["uuid"]), "name": r["name"], "type": r["type"], "system": r["type"] != "custom"}
                for r in roles
            ]
            return json.dumps(
                {
                    "error": f"Role '{role_name}' not found (exact match)",
                    "did_you_mean": suggestions,
                    "hint": "Use the exact role name from the suggestions above.",
                }
            )
        return json.dumps(
            {
                "error": f"Role '{role_name}' not found",
                "hint": "Use search_roles(name='<partial>') to search for roles.",
            }
        )

    # Step 2: Get permissions directly from the role (V2 uses M2M)
    permissions = list(role.permissions.all())

    # Build role info for V2
    role_info = {
        "uuid": str(role.uuid),
        "name": role.name,
        "description": role.description or "",
        "type": role.type,
        "system": role.type != RoleV2.Types.CUSTOM,
    }

    return _build_role_permissions_result(role_info, permissions, include_available_permissions, "v2")


def _build_role_permissions_result(
    role_info: dict[str, Any],
    permissions: list[Permission],
    include_available_permissions: bool,
    org_version: str,
) -> str:
    """Build the analysis result for a role's permissions (shared by V1 and V2)."""
    permissions_list = []
    by_application: dict[str, list[str]] = {}
    verbs_included: set[str] = set()
    has_wildcard_resource = False
    has_wildcard_verb = False

    for perm in permissions:
        perm_str = f"{perm.application}:{perm.resource_type}:{perm.verb}"
        permissions_list.append(perm_str)

        if perm.application not in by_application:
            by_application[perm.application] = []
        by_application[perm.application].append(perm_str)

        verbs_included.add(perm.verb)

        if perm.resource_type == "*":
            has_wildcard_resource = True
        if perm.verb == "*":
            has_wildcard_verb = True

    # Analyze what verbs are NOT included
    common_verbs = {"read", "write", "create", "delete", "execute", "order", "link", "unlink"}
    verbs_not_included = common_verbs - verbs_included

    # Build expanded permissions explanation
    expanded_permissions = []
    for perm_str in permissions_list:
        parts = perm_str.split(":")
        if len(parts) == 3:
            app, resource, verb = parts
            if resource == "*" and verb == "*":
                expanded_permissions.append(f"{perm_str} → full access to all {app} resources")
            elif resource == "*":
                expanded_permissions.append(f"{perm_str} → {verb} access to all {app} resources")
            elif verb == "*":
                expanded_permissions.append(f"{perm_str} → full access to {app} {resource} resources")
            else:
                expanded_permissions.append(f"{perm_str} → {verb} access to {app} {resource} resources")

    # Check for available permissions in covered applications (if requested)
    available_but_not_granted: dict[str, list[str]] = {}
    if include_available_permissions and by_application:
        for app in by_application.keys():
            app_permissions = Permission.objects.filter(application=app).values_list(
                "application", "resource_type", "verb"
            )
            app_perm_strings = {f"{p[0]}:{p[1]}:{p[2]}" for p in app_permissions}
            not_included = app_perm_strings - set(permissions_list)
            if not_included:
                available_but_not_granted[app] = sorted(not_included)

    # Build coverage analysis
    coverage_analysis = {
        "applications_covered": list(by_application.keys()),
        "total_permissions": len(permissions_list),
        "has_wildcard_resource": has_wildcard_resource,
        "has_wildcard_verb": has_wildcard_verb,
        "is_read_only": verbs_included <= {"read"} and not has_wildcard_verb,
        "can_modify": bool(verbs_included & {"write", "create", "delete"}) or has_wildcard_verb,
    }

    # Generate recommendations
    recommendations = []
    if not permissions_list:
        recommendations.append("WARNING: This role has no permissions. Users with only this role cannot do anything.")
    if coverage_analysis["is_read_only"]:
        recommendations.append(
            "This is a read-only role. Users can view but not modify resources in: " + ", ".join(by_application.keys())
        )
    if has_wildcard_resource and has_wildcard_verb:
        recommendations.append(
            "CAUTION: This role grants full access (*:*) to some applications. "
            "Consider if narrower permissions would be more appropriate."
        )
    if len(by_application) > 3:
        recommendations.append(
            f"This role spans {len(by_application)} applications. "
            "Consider if users need access to all of them or if separate roles would be better."
        )
    if verbs_not_included and not has_wildcard_verb:
        recommendations.append(
            f"Verbs NOT granted by this role: {', '.join(sorted(verbs_not_included))}. "
            "Users will not be able to perform these actions."
        )

    # Build final result
    result: dict[str, Any] = {
        "role": role_info,
        "permissions": {
            "summary": f"This role contains {len(permissions_list)} permission(s) across "
            f"{len(by_application)} application(s).",
            "total_count": len(permissions_list),
            "by_application": {app: sorted(perms) for app, perms in sorted(by_application.items())},
            "expanded_permissions": expanded_permissions,
            "verbs_included": sorted(verbs_included),
            "verbs_not_included": sorted(verbs_not_included) if not has_wildcard_verb else [],
        },
        "coverage_analysis": coverage_analysis,
        "recommendations": recommendations,
        "org_version": org_version,
    }

    if include_available_permissions and available_but_not_granted:
        result["available_but_not_granted"] = available_but_not_granted
        result["permissions"]["note"] = (
            f"The applications field on the role lists: {', '.join(sorted(by_application.keys()))}. "
            "See 'available_but_not_granted' for permissions in these apps that this role does NOT include."
        )

    return json.dumps(result, default=str)


@register_tool(
    description=(
        "List groups for the authenticated organization. Groups are collections of principals "
        "that can be assigned roles via policies. Filter by name (partial match), username "
        "(groups a specific user belongs to), or role_names (groups that have a specific role assigned). "
        "TROUBLESHOOTING: To find which groups a user belongs to, call list_groups(username='<user>'). "
        "To find groups with a specific role, call list_groups(role_names='<role_name>'). "
        "Order by: 'name', '-name', 'modified', '-modified', 'principalCount', '-principalCount', "
        "'policyCount', '-policyCount'. "
        "Returns: {meta: {count}, links, data: [{uuid, name, description, principalCount, ...}]}. "
        "Calls: GET /api/v1/groups/"
    ),
    requires_auth=True,
)
def list_groups(
    request: HttpRequest,
    *,
    limit: int = 10,
    offset: int = 0,
    name: str = "",
    username: str = "",
    role_names: str = "",
    order_by: str = "",
) -> str:
    """List groups by delegating to GroupViewSet."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if name:
        query_params["name"] = name
    if username:
        query_params["username"] = username
    if role_names:
        query_params["role_names"] = role_names
    if order_by:
        query_params["order_by"] = order_by

    path = reverse("v1_management:group-list")
    return _call_view(request, _group_list_view, path, query_params)


@register_tool(
    description=(
        "Get details of a specific group by UUID, including its name, description, "
        "principal count, policy count, and role count. "
        "Returns: {uuid, name, description, principalCount, policyCount, roleCount, ...}. "
        "Calls: GET /api/v1/groups/{uuid}/\n"
        "Caveats:\n"
        "- The 'Default access' group is a system group that all users belong to implicitly. "
        "Its principalCount may not reflect the full org size, and its roles define the baseline "
        "permissions every user gets.\n"
        "- Group membership changes are not versioned. You see the current state, not a history "
        "of who was added or removed. Use list_audit_logs for membership change history."
    ),
    requires_auth=True,
)
def get_group(
    request: HttpRequest,
    *,
    group_uuid: str,
) -> str:
    """Get a single group by delegating to GroupViewSet."""
    path = reverse("v1_management:group-detail", kwargs={"uuid": group_uuid})
    return _call_view(request, _group_detail_view, path, {}, uuid=group_uuid)


@register_tool(
    description=(
        "List principals (users) that are members of a specific group. "
        "Optionally filter by principal_type. "
        "Returns: {meta: {count}, links, data: [{uuid, username, first_name, ...}]}. "
        "Sensitive fields (email, last_name) are redacted by default. "
        "Calls: GET /api/v1/groups/{uuid}/principals/"
    ),
    requires_auth=True,
    redacted_fields=_PRINCIPAL_REDACTED_FIELDS,
)
def list_group_principals(
    request: HttpRequest,
    *,
    group_uuid: str,
    limit: int = 10,
    offset: int = 0,
    principal_type: str = "",
) -> str:
    """List principals in a group by delegating to GroupViewSet.principals."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if principal_type:
        query_params["principal_type"] = principal_type

    path = reverse("v1_management:group-principals", kwargs={"uuid": group_uuid})
    result = _call_view(request, _group_principals_view, path, query_params, uuid=group_uuid)
    return _strip_pii_from_principal_list(result, tenant=getattr(request, "tenant", None))


@register_tool(
    description=(
        "List cross-account access requests. These allow users from one org (e.g. Red Hat TAMs) "
        "to request temporary access to another org's resources. "
        "Set query_by='target_org' (default) to see requests INTO your org, or "
        "query_by='user_id' to see requests you made to other orgs. "
        "Filter by status (pending/approved/denied/expired/cancelled), org_id, or approved_only. "
        "TROUBLESHOOTING: To see who from Red Hat currently has access to your org, call "
        "list_cross_account_requests(query_by='target_org', status='approved'). "
        "Order by: 'request_id', 'start_date', 'end_date', 'created', 'modified', "
        "'status' (prefix with '-' to reverse). "
        "Returns: {meta: {count}, links, data: [{request_id, target_org, status, start_date, end_date, ...}]}. "
        "Calls: GET /api/v1/cross-account-requests/\n"
        "Caveats:\n"
        "- Cross-account requests are org-to-org, not user-to-user. A TAM requests access to your "
        "entire org's resources (scoped by the roles they are granted), not to a specific user's data.\n"
        "- Approved requests have a time window (start_date to end_date). An 'approved' request may "
        "have expired -- check end_date against the current date to confirm active access.\n"
        "- Cross-account activity is not tracked in RBAC audit logs.\n"
        "- Only org admins can approve or deny requests -- regular users can view but not act on them."
    ),
    requires_auth=True,
)
def list_cross_account_requests(
    request: HttpRequest,
    *,
    limit: int = 10,
    offset: int = 0,
    query_by: str = "",
    status: str = "",
    org_id: str = "",
    approved_only: str = "",
    order_by: str = "",
) -> str:
    """List cross-account requests by delegating to CrossAccountRequestViewSet."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if query_by:
        query_params["query_by"] = query_by
    if status:
        query_params["status"] = status
    if org_id:
        query_params["org_id"] = org_id
    if approved_only:
        query_params["approved_only"] = approved_only
    if order_by:
        query_params["order_by"] = order_by

    path = reverse("v1_api:cross-list")
    return _call_view(request, _cross_account_list_view, path, query_params)


@register_tool(
    description=(
        "Get details of a specific cross-account access request by its ID, including "
        "status, start/end dates, target org, and the requested roles. "
        "Returns: {request_id, target_org, status, start_date, end_date, created, roles, ...}. "
        "Calls: GET /api/v1/cross-account-requests/{request_id}/"
    ),
    requires_auth=True,
)
def get_cross_account_request(
    request: HttpRequest,
    *,
    request_id: str,
) -> str:
    """Get a single cross-account request by delegating to CrossAccountRequestViewSet."""
    path = reverse("v1_api:cross-detail", kwargs={"pk": request_id})
    return _call_view(request, _cross_account_detail_view, path, {}, pk=request_id)


@register_tool(
    description=(
        "Investigate TAM (Technical Account Manager) cross-account access. Use this when a TAM "
        "or external user reports they cannot access a feature in your organization's console. "
        "This tool fetches approved cross-account requests, shows what roles were granted, "
        "lists all permissions those roles provide, and identifies potential permission gaps. "
        "SCENARIO: 'TAM Rachel can't see the subscription watch dashboard' → call "
        "investigate_tam_access(requester_name='Rachel') to see what roles/permissions she has. "
        "Set 'required_permission' to check if a specific permission is granted (e.g., "
        "'subscriptions:watch:read'). The tool will report whether that permission is present. "
        "When no approved requests are found, the tool proactively checks for pending/expired "
        "requests and explains the cross-account request lifecycle. "
        "RESPONSE FORMAT: Present results as a table: "
        "requester name | roles granted | specific permissions | expiration | status. "
        "Explain the lifecycle: create → pending → approved → active → expired. "
        "Returns: {requests: [{request_id, status, end_date, days_remaining, requester_info: {user_id}, "
        "roles: [{name, permissions: [...]}], permission_summary}], analysis}. "
        "Sensitive requester fields (email, last_name) are redacted; requesters are identified by user_id. "
        "AFTER ANALYSIS: Present numbered options: "
        "(1) update the cross-account request with an expanded roles list, "
        "(2) deny the current request and ask the TAM to resubmit with the correct scope (cleaner audit trail). "
        "Format as 'Reply 1 or 2 -- or no'. Do NOT execute write tools without explicit user selection."
    ),
    requires_auth=True,
    api_version=ApiVersion.COMMON,
    required_relation="rbac_roles_read",
    v1_permission=("role", "read"),
    redacted_fields=_PRINCIPAL_REDACTED_FIELDS,
)
def investigate_tam_access(
    request: HttpRequest,
    *,
    requester_name: str = "",
    requester_email: str = "",
    status: str = "approved",
    required_permission: str = "",
    limit: int = 20,
) -> str:
    """Investigate TAM cross-account access by examining approved requests, roles, and permissions."""
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        return json.dumps({"error": "No organization context available"})

    now = datetime.now(timezone.utc)

    # Query cross-account requests targeting this org
    queryset = CrossAccountRequest.objects.filter(target_org=org_id).prefetch_related(
        "roles", "roles__access__permission"
    )

    if status:
        queryset = queryset.filter(status=status)

    # For approved requests, only show currently active ones
    if status == "approved":
        queryset = queryset.filter(start_date__lte=now, end_date__gte=now)

    queryset = queryset.order_by("-created")[:limit]
    requests_list = list(queryset)

    if not requests_list:
        status_counts = _cross_account_status_counts(org_id)
        pending_count = status_counts["pending"]
        expired_count = status_counts["expired"]

        hint: str
        if pending_count or expired_count:
            hint = (
                f"There are {pending_count} pending and {expired_count} expired request(s). "
                "Use investigate_tam_access(status='pending') or status='expired' to inspect them."
            )
        else:
            hint = "No requests of any status exist for this organization."

        return json.dumps(
            {
                "requests": [],
                "analysis": {
                    "total_active_requests": 0,
                    "message": f"No {status} cross-account requests found for this organization.",
                    "other_statuses": {
                        "pending": pending_count,
                        "expired": expired_count,
                    },
                    "lifecycle": _CROSS_ACCOUNT_LIFECYCLE,
                    "hint": hint,
                },
            }
        )

    # Get requester info from BOP for all user_ids
    user_ids = list({car.user_id for car in requests_list})
    user_info_map: dict[str, dict[str, Any]] = {}
    try:
        proxy = PrincipalProxy()
        bop_resp = proxy.request_filtered_principals(
            user_ids, org_id=None, options={"query_by": "user_id", "return_id": True}
        )
        if bop_resp.get("status_code") == 200:
            for principal in bop_resp.get("data", []):
                user_info_map[str(principal.get("user_id", ""))] = {
                    "first_name": principal.get("first_name", ""),
                    "last_name": principal.get("last_name", ""),
                    "email": principal.get("email", ""),
                    "username": principal.get("username", ""),
                    "user_id": str(principal.get("user_id", "")),
                }
    except Exception:
        logger.warning("mcp: failed to fetch requester info from BOP", exc_info=True)

    # Filter by requester name/email if provided (uses BOP data internally, not exposed)
    filtered_requests = requests_list
    if requester_name or requester_email:
        filtered_requests = []
        requester_name_lower = requester_name.lower() if requester_name else ""
        requester_email_lower = requester_email.lower() if requester_email else ""
        for car in requests_list:
            info = user_info_map.get(car.user_id, {})
            full_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".lower()
            email = info.get("email", "").lower()
            if requester_name_lower and requester_name_lower in full_name:
                filtered_requests.append(car)
            elif requester_email_lower and requester_email_lower in email:
                filtered_requests.append(car)

    if not filtered_requests:
        return json.dumps(
            {
                "requests": [],
                "analysis": {
                    "total_active_requests": len(requests_list),
                    "filtered_count": 0,
                    "message": f"No cross-account requests found matching name='{requester_name}' "
                    f"or email='{requester_email}'.",
                    "lifecycle": _CROSS_ACCOUNT_LIFECYCLE,
                    "hint": "Call investigate_tam_access() without filters to see all active requests, "
                    "or try investigate_tam_access(status='pending') for pending requests.",
                },
            }
        )

    # Build detailed response
    results: list[dict[str, Any]] = []
    all_permissions_granted: set[str] = set()
    required_perm_found = False
    required_perm_role: str | None = None

    for car in filtered_requests:
        days_remaining = (car.end_date - now).days if car.end_date else None
        raw_info = user_info_map.get(car.user_id, {})
        if _is_pii_redaction_enabled():
            requester_info = {"user_id": raw_info.get("user_id", car.user_id)}
        else:
            requester_info = raw_info if raw_info else {"user_id": car.user_id}

        roles_data: list[dict[str, Any]] = []
        for role in car.roles.all():
            permissions_list: list[str] = []
            for access in role.access.all():
                if access.permission:
                    perm_str = access.permission.permission
                    permissions_list.append(perm_str)
                    all_permissions_granted.add(perm_str)
                    # Check if this matches the required permission
                    if required_permission and _permission_matches(perm_str, required_permission):
                        required_perm_found = True
                        required_perm_role = role.display_name or role.name

            roles_data.append(
                {
                    "uuid": str(role.uuid),
                    "name": role.name,
                    "display_name": role.display_name or role.name,
                    "description": role.description or "",
                    "system": getattr(role, "system", False),
                    "permission_count": len(permissions_list),
                    "permissions": sorted(permissions_list),
                }
            )

        results.append(
            {
                "request_id": str(car.request_id),
                "status": car.status,
                "start_date": car.start_date.strftime("%Y-%m-%d") if car.start_date else None,
                "end_date": car.end_date.strftime("%Y-%m-%d") if car.end_date else None,
                "days_remaining": days_remaining,
                "created": car.created.strftime("%Y-%m-%d") if car.created else None,
                "requester_info": requester_info,
                "roles": roles_data,
                "role_count": len(roles_data),
                "total_permissions": sum(r["permission_count"] for r in roles_data),
            }
        )

    # Group permissions by application for summary
    perm_by_app: dict[str, list[str]] = {}
    for perm in all_permissions_granted:
        parts = perm.split(":")
        if len(parts) >= 1:
            app = parts[0]
            if app not in perm_by_app:
                perm_by_app[app] = []
            perm_by_app[app].append(perm)

    # Build analysis
    analysis: dict[str, Any] = {
        "total_requests_found": len(results),
        "unique_permissions_granted": len(all_permissions_granted),
        "permissions_by_application": {app: sorted(perms) for app, perms in sorted(perm_by_app.items())},
    }

    if required_permission:
        if required_perm_found:
            analysis["required_permission_check"] = {
                "permission": required_permission,
                "granted": True,
                "via_role": required_perm_role,
            }
        else:
            # Try to suggest what permission might be needed
            required_parts = required_permission.split(":")
            app = required_parts[0] if len(required_parts) >= 1 else ""

            similar_perms = [p for p in all_permissions_granted if p.startswith(f"{app}:")]
            analysis["required_permission_check"] = {
                "permission": required_permission,
                "granted": False,
                "similar_permissions_granted": sorted(similar_perms) if similar_perms else [],
                "hint": f"The permission '{required_permission}' is NOT granted. "
                + (
                    f"However, these {app} permissions are granted: {similar_perms}. "
                    if similar_perms
                    else f"No {app} permissions are currently granted. "
                )
                + "Check with the feature team to confirm what permission is actually required.",
            }

    return json.dumps({"requests": results, "analysis": analysis}, default=str)


@register_tool(
    description=(
        "Audit all Red Hat cross-account access into your organization. "
        "Returns a complete inventory of: (1) who from Red Hat has access, (2) what roles/permissions "
        "they have, (3) when their access expires, and (4) what RBAC changes they've made. "
        "SCENARIO: 'Who from Red Hat is in our org right now?' → call audit_redhat_access() to get "
        "a summary of all active approved cross-account requests with audit activity. "
        "Set include_inactive=true to also see expired or pending requests. "
        "Set audit_days to control how far back to look in audit logs (default 30 days). "
        "FORMAT FOR CISO BRIEFING: Present as a table with columns: "
        "Red Hat user | roles | access scope | expiration | approved by | recent activity. "
        "When no active access exists, still present a summary stating zero access, "
        "note any pending/expired requests, and provide a monitoring recommendation. "
        "Returns: {active_access: [{user_info: {user_id}, roles, permissions, expires, days_remaining, "
        "audit_activity: {total_actions, recent_actions, summary}}], summary: {total_users, "
        "expiring_soon, unused_access}}. "
        "Sensitive user fields (email, last_name) are redacted; users are identified by user_id. "
        "AFTER ANALYSIS: Present numbered options: "
        "(1) cancel unused cross-account access (requests with no audit log activity), "
        "(2) format the full report for a CISO briefing, "
        "(3) both. "
        "Format as 'Reply 1, 2, or 3 -- or no'. Do NOT execute write tools without explicit user selection."
    ),
    requires_auth=True,
    api_version=ApiVersion.COMMON,
    required_relation="rbac_roles_read",
    v1_permission=("role", "read"),
    redacted_fields=_PRINCIPAL_REDACTED_FIELDS,
)
def audit_redhat_access(
    request: HttpRequest,
    *,
    include_inactive: bool = False,
    audit_days: int = 30,
    limit: int = 50,
) -> str:
    """Audit Red Hat cross-account access into the organization."""
    org_id = getattr(request.user, "org_id", None)
    tenant = getattr(request, "tenant", None)
    if not org_id or not tenant:
        return json.dumps({"error": "No organization context available"})

    now = datetime.now(timezone.utc)
    audit_since = now - timedelta(days=audit_days)

    # Query cross-account requests targeting this org
    queryset = CrossAccountRequest.objects.filter(target_org=org_id).prefetch_related(
        "roles", "roles__access__permission"
    )

    if not include_inactive:
        queryset = queryset.filter(status="approved", start_date__lte=now, end_date__gte=now)
    else:
        queryset = queryset.filter(status__in=["approved", "pending", "expired"])

    queryset = queryset.order_by("-end_date")[:limit]
    requests_list = list(queryset)

    if not requests_list:
        status_counts = _cross_account_status_counts(org_id)
        pending_count = status_counts["pending"]
        expired_count = status_counts["expired"]

        ciso_parts = [
            "CISO Summary: Zero Red Hat personnel currently have active access to this "
            "organization's console.redhat.com environment."
        ]
        if pending_count:
            ciso_parts.append(f"There are {pending_count} pending request(s) awaiting approval.")
        if expired_count:
            ciso_parts.append(f"There are {expired_count} previously expired access grant(s) on record.")

        return json.dumps(
            {
                "active_access": [],
                "summary": {
                    "total_users": 0,
                    "expiring_soon": 0,
                    "unused_access": 0,
                    "pending_requests": pending_count,
                    "expired_requests": expired_count,
                    "message": "No active Red Hat cross-account access found for this organization.",
                    "ciso_briefing": " ".join(ciso_parts),
                    "briefing_template": (
                        "If active access existed, the report would include for each Red Hat user: "
                        "name, email, roles granted, permission scope, access start/end dates, "
                        "days remaining, and RBAC audit activity (actions performed during the access window)."
                    ),
                    "monitoring": (
                        "To monitor future cross-account requests, periodically call "
                        "audit_redhat_access() or list_cross_account_requests(query_by='target_org', "
                        "status='pending') to catch new requests before they are approved."
                    ),
                },
            }
        )

    # Get requester info from BOP for all user_ids
    user_ids = list({car.user_id for car in requests_list})
    user_info_map: dict[str, dict[str, Any]] = {}
    try:
        proxy = PrincipalProxy()
        bop_resp = proxy.request_filtered_principals(
            user_ids, org_id=None, options={"query_by": "user_id", "return_id": True}
        )
        if bop_resp.get("status_code") == 200:
            for principal in bop_resp.get("data", []):
                user_info_map[str(principal.get("user_id", ""))] = {
                    "first_name": principal.get("first_name", ""),
                    "last_name": principal.get("last_name", ""),
                    "email": principal.get("email", ""),
                    "username": principal.get("username", ""),
                    "user_id": str(principal.get("user_id", "")),
                }
    except Exception:
        logger.warning("mcp: failed to fetch requester info from BOP", exc_info=True)

    # Batch query audit logs for all usernames (uses username internally for audit log lookup)
    all_usernames = {info.get("username") for info in user_info_map.values() if info.get("username")}
    audit_by_user: dict[str, list[AuditLog]] = {u: [] for u in all_usernames}
    audit_counts_by_user: dict[str, int] = {}
    if all_usernames:
        audit_qs = AuditLog.objects.filter(
            tenant=tenant, principal_username__in=all_usernames, created__gte=audit_since
        ).order_by("-created")
        for entry in audit_qs:
            if entry.principal_username in audit_by_user and len(audit_by_user[entry.principal_username]) < 10:
                audit_by_user[entry.principal_username].append(entry)

        # Get true total counts per user
        counts_qs = (
            AuditLog.objects.filter(tenant=tenant, principal_username__in=all_usernames, created__gte=audit_since)
            .values("principal_username")
            .annotate(count=Count("id"))
        )
        audit_counts_by_user = {row["principal_username"]: row["count"] for row in counts_qs}

    # Build detailed response
    results: list[dict[str, Any]] = []
    expiring_soon_count = 0
    unused_access_count = 0
    all_permissions_granted: set[str] = set()

    for car in requests_list:
        days_remaining = (car.end_date - now).days if car.end_date else None
        is_expiring_soon = days_remaining is not None and 0 < days_remaining <= 7 and car.status == "approved"
        if is_expiring_soon:
            expiring_soon_count += 1

        requester_info = user_info_map.get(car.user_id, {"user_id": car.user_id})
        username = requester_info.get("username", "")

        # Collect roles and permissions
        roles_data: list[dict[str, Any]] = []
        permissions_list: list[str] = []
        for role in car.roles.all():
            role_perms: list[str] = []
            for access in role.access.all():
                if access.permission:
                    perm_str = access.permission.permission
                    role_perms.append(perm_str)
                    permissions_list.append(perm_str)
                    all_permissions_granted.add(perm_str)

            roles_data.append(
                {
                    "name": role.display_name or role.name,
                    "description": role.description or "",
                    "permissions": sorted(role_perms),
                }
            )

        # Get pre-fetched audit logs for this user
        audit_activity: dict[str, Any] = {"total_actions": 0, "recent_actions": [], "summary": ""}
        if username and car.status == "approved":
            audit_entries = audit_by_user.get(username, [])
            audit_activity["total_actions"] = audit_counts_by_user.get(username, 0)

            if audit_entries:
                for entry in audit_entries[:5]:
                    audit_activity["recent_actions"].append(
                        {
                            "action": entry.action,
                            "resource_type": entry.resource_type,
                            "description": entry.description,
                            "date": entry.created.strftime("%Y-%m-%d %H:%M") if entry.created else None,
                        }
                    )
                # Summarize activity types
                action_counts: dict[str, int] = {}
                for entry in audit_entries:
                    action_counts[entry.action] = action_counts.get(entry.action, 0) + 1
                summary_parts = [f"{count} {action}" for action, count in sorted(action_counts.items())]
                audit_activity["summary"] = ", ".join(summary_parts) + f" action(s) in last {audit_days} days"
            else:
                audit_activity["summary"] = f"No RBAC activity in last {audit_days} days"
                unused_access_count += 1

        # Determine status display
        status_display = car.status
        if car.status == "approved":
            if days_remaining is not None and days_remaining < 0:
                status_display = "expired (just now)"
            elif is_expiring_soon:
                status_display = f"approved (expires in {days_remaining} day{'s' if days_remaining != 1 else ''})"

        results.append(
            {
                "request_id": str(car.request_id),
                "user_info": (
                    {"user_id": requester_info.get("user_id", car.user_id)}
                    if _is_pii_redaction_enabled()
                    else {
                        "user_id": requester_info.get("user_id", car.user_id),
                        "name": (
                            f"{requester_info.get('first_name', '')} {requester_info.get('last_name', '')}".strip()
                            or car.user_id
                        ),
                        "email": requester_info.get("email", ""),
                        "username": username or f"user_{car.user_id}",
                    }
                ),
                "status": status_display,
                "start_date": car.start_date.strftime("%Y-%m-%d") if car.start_date else None,
                "end_date": car.end_date.strftime("%Y-%m-%d") if car.end_date else None,
                "days_remaining": days_remaining if car.status == "approved" else None,
                "roles": roles_data,
                "permissions_summary": f"{len(permissions_list)} permission(s) across {len(roles_data)} role(s)",
                "audit_activity": audit_activity,
            }
        )

    # Group permissions by application for summary
    perm_by_app: dict[str, int] = {}
    for perm in all_permissions_granted:
        parts = perm.split(":")
        if len(parts) >= 1:
            app = parts[0]
            perm_by_app[app] = perm_by_app.get(app, 0) + 1

    # Build summary
    summary: dict[str, Any] = {
        "total_users": len(results),
        "total_active": sum(1 for r in results if "approved" in r["status"]),
        "expiring_soon": expiring_soon_count,
        "unused_access": unused_access_count,
        "permissions_by_application": {app: count for app, count in sorted(perm_by_app.items())},
        "audit_period_days": audit_days,
    }

    if expiring_soon_count > 0:
        summary["warning"] = f"{expiring_soon_count} access grant(s) expiring within 7 days"

    if unused_access_count > 0:
        summary["note"] = f"{unused_access_count} user(s) with access but no RBAC activity in last {audit_days} days"

    return json.dumps({"active_access": results, "summary": summary}, default=str)


@register_tool(
    description=(
        "Audit a group before dissolving it. Shows all members (users + service accounts), "
        "roles/permissions they'd lose, and identifies who would be left stranded (losing all "
        "non-default access). Essential for org changes, contractor offboarding, or acquisitions. "
        "Provide either group_uuid OR group_name to identify the group. "
        "Returns: {group: {...}, members: [{uuid, type, other_groups, access_impact}], "
        "roles: [{name, permissions}], analysis: {stranded_users, stranded_service_accounts, ...}}. "
        "Members are identified by UUID. Sensitive fields (email, last_name) are redacted. "
        "STRANDED means the member is ONLY in this group plus platform_default — they'd be demoted "
        "to default access only. Service accounts with no other groups would start 403'ing. "
        "Queries: Group, Principal, Policy, Role, Access models directly (no per-member API calls). "
        "AFTER ANALYSIS: Present numbered options: "
        "(1) delete the group immediately (warn about stranded users/service accounts), "
        "(2) create a transition group with the same roles, move stranded members, then delete the original, "
        "(3) remove only covered users from the group, leave it intact for manual review. "
        "Format as 'Reply 1, 2, or 3 -- or no'. Do NOT execute write tools without explicit user selection."
    ),
    requires_auth=True,
    api_version=ApiVersion.COMMON,
    required_relation="rbac_roles_read",
    v1_permission=("group", "read"),
    redacted_fields=_PRINCIPAL_REDACTED_FIELDS,
)
def audit_group_for_dissolution(
    request: HttpRequest,
    *,
    group_uuid: str = "",
    group_name: str = "",
) -> str:
    """Audit a group before dissolving it to identify stranded members."""
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return json.dumps({"error": "No tenant context available"})

    # Resolve group by UUID or name
    resolved_uuid, error = _resolve_group_uuid(group_uuid, group_name, tenant)
    if error:
        return error
    assert resolved_uuid is not None

    # Get the group with prefetched data
    group = (
        Group.objects.filter(uuid=resolved_uuid, tenant=tenant)
        .prefetch_related(
            "principals",
            "policies__roles__access__permission",
        )
        .first()
    )

    if not group:
        return json.dumps({"error": f"Group with UUID '{resolved_uuid}' not found"})

    # Get all members (users and service accounts)
    all_principals = list(group.principals.all())
    users = [p for p in all_principals if p.type == Principal.Types.USER]
    service_accounts = [p for p in all_principals if p.type == Principal.Types.SERVICE_ACCOUNT]

    # Get roles assigned to this group with their permissions
    group_roles: list[dict[str, Any]] = []
    all_permissions_in_group: set[str] = set()

    for policy in group.policies.all():
        for role in policy.roles.all():
            role_perms: list[str] = []
            for access in role.access.all():
                if access.permission:
                    perm_str = access.permission.permission
                    role_perms.append(perm_str)
                    all_permissions_in_group.add(perm_str)

            group_roles.append(
                {
                    "uuid": str(role.uuid),
                    "name": role.display_name or role.name,
                    "description": role.description or "",
                    "system": getattr(role, "system", False),
                    "permissions": sorted(role_perms),
                    "permission_count": len(role_perms),
                }
            )

    # Precompute all group memberships for all principals
    principal_ids = set(p.id for p in all_principals)
    all_memberships = (
        Group.objects.filter(principals__id__in=principal_ids, tenant=tenant)
        .exclude(uuid=resolved_uuid)
        .prefetch_related(
            "policies__roles__access__permission",
            Prefetch("principals", queryset=Principal.objects.filter(id__in=principal_ids)),
        )
        .distinct()
    )
    # Build a mapping of principal_id -> list of other groups
    principal_to_groups: dict[int, list[Group]] = {pid: [] for pid in principal_ids}
    for grp in all_memberships:
        for p in grp.principals.all():
            if p.id in principal_to_groups:
                principal_to_groups[p.id].append(grp)

    # Analyze each member's other group memberships and access impact
    members_data: list[dict[str, Any]] = []
    stranded_users: list[str] = []
    stranded_service_accounts: list[str] = []
    members_with_overlap: list[str] = []

    for principal in all_principals:
        # Get precomputed groups for this principal
        other_groups_list = principal_to_groups.get(principal.id, [])

        # Check what permissions they'd retain from other groups
        retained_permissions: set[str] = set()
        other_groups_info: list[dict[str, Any]] = []

        for other_group in other_groups_list:
            group_perms: list[str] = []
            for policy in other_group.policies.all():
                for role in policy.roles.all():
                    for access in role.access.all():
                        if access.permission:
                            perm_str = access.permission.permission
                            group_perms.append(perm_str)
                            retained_permissions.add(perm_str)

            other_groups_info.append(
                {
                    "uuid": str(other_group.uuid),
                    "name": other_group.name,
                    "is_platform_default": other_group.platform_default,
                    "is_admin_default": other_group.admin_default,
                    "permission_count": len(group_perms),
                }
            )

        # Calculate lost permissions (permissions in target group but not in other groups)
        lost_permissions = all_permissions_in_group - retained_permissions

        # Determine if stranded (only in target group + platform_default, or no other groups at all)
        non_default_other_groups = [g for g in other_groups_info if not g["is_platform_default"]]
        is_stranded = len(non_default_other_groups) == 0

        # Categorize access impact
        if is_stranded:
            if lost_permissions:
                access_impact = "stranded - will lose all non-default access"
            else:
                access_impact = "stranded - no permissions to lose (target group grants none)"
        elif lost_permissions:
            overlap_perms = all_permissions_in_group & retained_permissions
            if overlap_perms:
                access_impact = (
                    f"partial - loses {len(lost_permissions)} perm(s), retains {len(overlap_perms)} overlapping"
                )
            else:
                access_impact = f"loses {len(lost_permissions)} permission(s), retains access via other groups"
        else:
            access_impact = "no impact - all permissions retained via other groups"

        redact = _is_pii_redaction_enabled()
        member_info: dict[str, Any] = {
            "uuid": str(principal.uuid),
            "type": principal.type,
            "other_groups": other_groups_info,
            "other_group_count": len(other_groups_info),
            "non_default_group_count": len(non_default_other_groups),
            "is_stranded": is_stranded,
            "access_impact": access_impact,
            "permissions_lost": sorted(lost_permissions) if lost_permissions else [],
            "permissions_retained": len(retained_permissions),
        }

        if not redact:
            member_info["username"] = principal.username
        if principal.type == Principal.Types.SERVICE_ACCOUNT:
            member_info["service_account_id"] = principal.service_account_id

        members_data.append(member_info)

        # Track stranded members by UUID (or username when redaction is off)
        member_id = str(principal.uuid) if redact else principal.username
        if is_stranded:
            if principal.type == Principal.Types.USER:
                stranded_users.append(member_id)
            else:
                stranded_service_accounts.append(member_id)
        elif lost_permissions and retained_permissions:
            members_with_overlap.append(member_id)

    # Group permissions by application for summary
    perm_by_app: dict[str, int] = {}
    for perm in all_permissions_in_group:
        app = perm.split(":")[0]
        perm_by_app[app] = perm_by_app.get(app, 0) + 1

    # Build analysis summary
    analysis: dict[str, Any] = {
        "total_members": len(all_principals),
        "total_users": len(users),
        "total_service_accounts": len(service_accounts),
        "total_roles": len(group_roles),
        "total_unique_permissions": len(all_permissions_in_group),
        "permissions_by_application": dict(sorted(perm_by_app.items())),
        "stranded_users": stranded_users,
        "stranded_user_count": len(stranded_users),
        "stranded_service_accounts": stranded_service_accounts,
        "stranded_service_account_count": len(stranded_service_accounts),
        "members_with_overlapping_access": members_with_overlap,
        "members_with_overlapping_count": len(members_with_overlap),
    }

    # Add warnings/recommendations (truncate long lists to avoid huge messages)
    warnings: list[str] = []
    if stranded_users:
        if len(stranded_users) <= 5:
            user_list = ", ".join(stranded_users)
        else:
            user_list = ", ".join(stranded_users[:5]) + f", +{len(stranded_users) - 5} more"
        warnings.append(f"{len(stranded_users)} user(s) ({user_list}) will be demoted to default access only")
    if stranded_service_accounts:
        if len(stranded_service_accounts) <= 5:
            svc_list = ", ".join(stranded_service_accounts)
        else:
            svc_list = ", ".join(stranded_service_accounts[:5]) + f", +{len(stranded_service_accounts) - 5} more"
        warnings.append(
            f"{len(stranded_service_accounts)} service account(s) ({svc_list}) "
            f"will lose all access — automated processes using them will start 403'ing"
        )
    if not stranded_users and not stranded_service_accounts:
        if len(all_principals) == 0:
            warnings.append("Group has no members — safe to delete")
        else:
            warnings.append(
                f"All {len(all_principals)} member(s) have other group memberships — "
                f"access impact is partial or none"
            )

    analysis["warnings"] = warnings

    return json.dumps(
        {
            "group": {
                "uuid": str(group.uuid),
                "name": group.name,
                "description": group.description or "",
                "platform_default": group.platform_default,
                "admin_default": group.admin_default,
                "system": group.system,
            },
            "members": members_data,
            "roles": group_roles,
            "analysis": analysis,
        },
        default=str,
    )


@register_tool(
    description=(
        "List workspaces for the authenticated organization (V2 API). Workspaces are hierarchical "
        "containers used to scope role bindings to specific resource boundaries. "
        "Order by: 'name', 'created', 'modified', 'type' (prefix with '-' to reverse). "
        "Returns: {meta: {count}, links, data: [{uuid, name, description, type, parent_id, created, ...}]}. "
        "Calls: GET /api/v2/workspaces/"
    ),
    requires_auth=True,
)
def list_workspaces(
    request: HttpRequest,
    *,
    limit: int = 10,
    offset: int = 0,
    order_by: str = "",
) -> str:
    """List workspaces by delegating to WorkspaceViewSet."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if order_by:
        query_params["order_by"] = order_by

    path = reverse("v2_management:workspace-list")
    return _call_view(request, _workspace_list_view, path, query_params)


@register_tool(
    description=(
        "Get details of a specific workspace by UUID (V2 API). Returns the workspace's name, "
        "type, parent, and optionally its full ancestry chain (set include_ancestry=true). "
        "Returns: {uuid, name, description, type, parent_id, created, modified, ...}. "
        "Calls: GET /api/v2/workspaces/{uuid}/"
    ),
    requires_auth=True,
)
def get_workspace(
    request: HttpRequest,
    *,
    workspace_uuid: str,
    include_ancestry: bool = False,
) -> str:
    """Get a single workspace by delegating to WorkspaceViewSet."""
    query_params: dict[str, str] = {}
    if include_ancestry:
        query_params["include_ancestry"] = "true"

    path = reverse("v2_management:workspace-detail", kwargs={"pk": workspace_uuid})
    return _call_view(request, _workspace_detail_view, path, query_params, pk=workspace_uuid)


@register_tool(
    description=(
        "List role bindings for the authenticated organization (V2 API). A role binding assigns "
        "a role to a subject (user/group) within a resource scope (e.g. workspace). "
        "Filter by role_id, resource_type, resource_id, subject_type, or subject_id. "
        "ACCESS RESOLUTION: To find what permissions a user has (or which are missing) in V2, "
        "(1) call list_role_bindings(granted_subject_type='principal', "
        "granted_subject_principal_user_id='<username>') to find all effective bindings "
        "including those inherited through group membership "
        "(this is the V2 equivalent of list_access(username=X) for V1), "
        "(2) for each binding extract the role UUID, "
        "(3) call get_role(role_uuid=...) or list_role_access(role_uuid=...) to inspect "
        "the role's permissions, (4) compare against the required permissions to identify "
        "what is granted and what is missing. "
        "TIP: For a quick yes/no check, use check_user_permission instead — it auto-detects "
        "V1/V2 and does the full resolution automatically. "
        "Returns: {meta: {count}, links, data: [{uuid, role, resource, subject, ...}]}. "
        "Calls: GET /api/v2/role-bindings/"
    ),
    requires_auth=True,
    api_version=ApiVersion.V2,
)
def list_role_bindings(
    request: HttpRequest,
    *,
    limit: int = 10,
    offset: int = 0,
    role_id: str = "",
    resource_type: str = "",
    resource_id: str = "",
    subject_type: str = "",
    subject_id: str = "",
    granted_subject_type: str = "",
    granted_subject_id: str = "",
    granted_subject_principal_user_id: str = "",
) -> str:
    """List role bindings by delegating to RoleBindingViewSet."""
    query_params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if role_id:
        query_params["role_id"] = role_id
    if resource_type:
        query_params["resource_type"] = resource_type
    if resource_id:
        query_params["resource_id"] = resource_id
    if subject_type:
        query_params["subject_type"] = subject_type
    if subject_id:
        query_params["subject_id"] = subject_id
    if granted_subject_type:
        query_params["granted_subject_type"] = granted_subject_type
    if granted_subject_id:
        query_params["granted_subject_id"] = granted_subject_id
    if granted_subject_principal_user_id:
        query_params["granted_subject.principal.user_id"] = granted_subject_principal_user_id

    path = reverse("v2_management:role-bindings-list")
    return _call_view(request, _role_binding_list_view, path, query_params)


@register_tool(
    description=(
        "List role bindings grouped by subject (V2 API). Shows which roles each subject "
        "(user/group) has within a specific resource. Requires resource_id and resource_type. "
        "Optionally filter by subject_type and subject_id. "
        "Returns: {meta: {count}, links, data: [{subject, roles: [...], ...}]}. "
        "Calls: GET /api/v2/role-bindings/by-subject/"
    ),
    requires_auth=True,
    api_version=ApiVersion.V2,
)
def list_role_bindings_by_subject(
    request: HttpRequest,
    *,
    resource_id: str,
    resource_type: str,
    subject_type: str = "",
    subject_id: str = "",
    limit: int = 10,
    offset: int = 0,
) -> str:
    """List role bindings by subject by delegating to RoleBindingViewSet.by_subject."""
    query_params: dict[str, str] = {
        "resource_id": resource_id,
        "resource_type": resource_type,
        "limit": str(limit),
        "offset": str(offset),
    }
    if subject_type:
        query_params["subject_type"] = subject_type
    if subject_id:
        query_params["subject_id"] = subject_id

    path = reverse("v2_management:role-bindings-by-subject")
    return _call_view(request, _role_binding_by_subject_view, path, query_params)


def _permission_matches(granted_permission: str, requested_permission: str) -> bool:
    """Check if a granted permission matches a requested permission, supporting wildcards.

    Supports exact match and wildcard patterns in the granted permission:
    - "app:resource:verb" matches "app:resource:verb" (exact)
    - "app:*:*" matches "app:resource:verb" (wildcard resource and verb)
    - "app:resource:*" matches "app:resource:verb" (wildcard verb)
    """
    granted_parts = granted_permission.split(":")
    requested_parts = requested_permission.split(":")
    if len(granted_parts) != 3 or len(requested_parts) != 3:
        return False
    return all(g == "*" or g == r for g, r in zip(granted_parts, requested_parts))


@register_tool(
    description=(
        "Check whether a specific user has a specific permission. Returns true/false "
        "with the matched permission details. Automatically detects whether the organization "
        "is V1 or V2 and uses the appropriate access resolution method: "
        "V1 uses the access endpoint; V2 resolves role bindings and inspects role permissions. "
        "The permission format is 'application:resource_type:verb' (e.g., "
        "'cost-management:cost_model:write'). Supports wildcard matching. "
        "Requires org admin or rbac:principal:read permission to check another user. "
        "ACCESS RESOLUTION: This is the fastest way to answer 'Can user X do Y?' — "
        "it works for both V1 and V2 organizations automatically. "
        "Returns: {allowed: bool, username, permission, matched_permission, ...} "
        "or {allowed: false, hint: str}. "
        "V1 calls: GET /api/v1/access/?username=X&application=Y (internally). "
        "V2 resolves: role bindings → roles → permissions (internally).\n"
        "Caveats:\n"
        "- Org admins bypass all RBAC checks; this tool returns only explicitly assigned permissions, "
        "not their effective unlimited access.\n"
        "- ResourceDefinition filters are stored but enforced by the consuming application, not RBAC.\n"
        "- Permission names are naming conventions only -- RBAC cannot confirm what UI elements "
        "or API endpoints they control."
    ),
    requires_auth=True,
    api_version=ApiVersion.UNIFIED,
    required_relation="rbac_roles_read",
    v1_permission=("principal", "read"),
)
def check_user_permission(
    request: HttpRequest,
    *,
    username: str,
    permission: str,
) -> str:
    """Check if a user has a specific permission by delegating to AccessView (V1) or role bindings (V2)."""
    parts = permission.split(":")
    if len(parts) != 3:
        return json.dumps(
            {
                "error": "Permission must be in format 'application:resource_type:verb'. "
                "Example: 'cost-management:cost_model:write'"
            }
        )

    tenant = getattr(request, "tenant", None)
    if not tenant or not is_v2_write_activated(tenant):
        # V1 path: resolve display name then delegate
        if tenant:
            _principal, resolved_username, candidates = _lookup_principal(request, username, tenant)
            if candidates:
                return json.dumps(
                    {
                        "error": f"Multiple users match '{username}'. Please specify the exact username.",
                        "candidates": candidates,
                    }
                )
            if resolved_username:
                username = resolved_username
            else:
                return json.dumps(
                    {
                        "allowed": False,
                        "username": username,
                        "permission": permission,
                        "hint": f"User '{username}' not found in this organization. "
                        "Use list_principals(usernames='<partial>', match_criteria='partial') to search.",
                    }
                )
        return _check_user_permission_v1(request, username, permission)

    # V2 path: resolve display name → principal
    resolved_principal, resolved_username, candidates = _lookup_principal(request, username, tenant)
    if candidates:
        return json.dumps(
            {
                "allowed": False,
                "username": username,
                "permission": permission,
                "org_version": "v2",
                "error": f"Multiple users match '{username}'. Please specify the exact username.",
                "candidates": candidates,
            }
        )
    if not resolved_principal or not resolved_username:
        return json.dumps(
            {
                "allowed": False,
                "username": username,
                "permission": permission,
                "org_version": "v2",
                "hint": f"User '{username}' not found in this organization. "
                "Use list_principals(usernames='<partial>', match_criteria='partial') to search.",
            }
        )
    principal: Principal = resolved_principal
    username = resolved_username

    bindings_path = reverse("v2_management:role-bindings-list")
    raw = _call_view(
        request,
        _role_binding_list_view,
        bindings_path,
        {
            "granted_subject_type": "user",
            "granted_subject_id": str(principal.uuid),
            "limit": "1000",
        },
    )
    bindings_data = json.loads(raw)
    bindings = bindings_data.get("data", [])

    if not bindings:
        return json.dumps(
            {
                "allowed": False,
                "username": username,
                "permission": permission,
                "org_version": "v2",
                "total_bindings_checked": 0,
                "hint": f"User '{username}' has no role bindings in this V2 organization. "
                f"Use list_role_bindings(granted_subject_type='user', "
                f"granted_subject_id='{principal.uuid}') to see all role bindings.",
            }
        )

    role_uuids = {b["role"]["id"] for b in bindings if b.get("role", {}).get("id")}

    for role_uuid in role_uuids:
        role_path = reverse("v2_management:roles-detail", kwargs={"uuid": role_uuid})
        role_raw = _call_view(request, _role_v2_detail_view, role_path, {}, uuid=role_uuid)
        role_data = json.loads(role_raw)

        for perm in role_data.get("permissions", []):
            perm_str = f"{perm['application']}:{perm['resource_type']}:{perm['operation']}"
            if _permission_matches(perm_str, permission):
                return json.dumps(
                    {
                        "allowed": True,
                        "username": username,
                        "permission": permission,
                        "matched_permission": perm_str,
                        "role_name": role_data.get("name"),
                        "role_uuid": str(role_uuid),
                        "org_version": "v2",
                    }
                )

    return json.dumps(
        {
            "allowed": False,
            "username": username,
            "permission": permission,
            "org_version": "v2",
            "total_roles_checked": len(role_uuids),
            "hint": f"User '{username}' does not have permission '{permission}' in this V2 organization. "
            f"Use list_role_bindings(granted_subject_type='user', "
            f"granted_subject_id='{principal.uuid}') to see all role bindings, "
            f"or search_roles(name='*') to browse available roles.",
        }
    )


def _check_user_permission_v1(request: HttpRequest, username: str, permission: str) -> str:
    """Check user permission using V1 access endpoint."""
    application = permission.split(":")[0]
    query_params: dict[str, str] = {
        "application": application,
        "username": username,
        "limit": "1000",
    }
    path = reverse("v1_management:access")

    try:
        raw = _call_view(request, _access_view, path, query_params)
    except Exception as e:
        return json.dumps(
            {
                "error": f"Failed to check permissions: {e}",
                "hint": "Requires org admin or rbac:principal:read permission to check another user.",
            }
        )

    data = json.loads(raw)

    if "detail" in data:
        return json.dumps(
            {
                "allowed": False,
                "error": data["detail"],
                "hint": "Requires org admin or rbac:principal:read permission to check another user.",
            }
        )

    access_list = data.get("data", [])

    for entry in access_list:
        entry_permission = entry.get("permission", "")
        if _permission_matches(entry_permission, permission):
            return json.dumps(
                {
                    "allowed": True,
                    "username": username,
                    "permission": permission,
                    "matched_permission": entry_permission,
                    "resource_definitions": entry.get("resourceDefinitions", []),
                }
            )

    application = permission.split(":")[0]
    return json.dumps(
        {
            "allowed": False,
            "username": username,
            "permission": permission,
            "total_permissions_checked": len(access_list),
            "hint": f"User '{username}' does not have permission '{permission}'. "
            f"Use list_access(username='{username}', application='{application}') to see all "
            f"permissions, or list_groups(username='{username}') to trace the group/role chain.",
        }
    )


_DEFAULT_INCLUDE_GROUP_ROLES = True
_DEFAULT_INCLUDE_PERMISSIONS = True
_DEFAULT_AUDIT_LOG_LIMIT = 10


@register_tool(
    description=(
        "Get comprehensive state for a specific user, returning all RBAC information in one call. "
        "This is the best tool for understanding a user's complete RBAC picture. "
        "Returns: (1) groups the user belongs to with roles assigned to each, "
        "(2) all permissions/access the user has (auto-detects V1/V2), "
        "(3) recent audit log activity ON the groups (by anyone), "
        "(4) actions performed BY the user with breakdown by group and action type. "
        "Supports both V1 and V2 organizations automatically. "
        "Use this instead of calling list_groups + list_access + list_audit_logs separately. "
        "SCENARIO: 'Contractor X is being offboarded, I need their RBAC activity and current access' → "
        "call get_user_state(username='X') to get groups, permissions, and audit trail in one call. "
        "USE WHEN: 'offboard', 'offboarding', 'leaving the company', 'contractor leaving', "
        "'deprovisioning', 'compliance report', 'activity summary for records', 'what access does user have'. "
        "RESPONSE FORMAT: Summarize as 'Member of: <groups>. Effective access: <role names>. "
        "Audit log: <N> actions performed by user on <groups> (action types).'. "
        "AFTER ANALYSIS: Present numbered options: "
        "(1) remove the user from all their group(s), "
        "(2) generate a formatted compliance report for records, "
        "(3) both. "
        "Format as 'Reply 1, 2, or 3 -- or no'. "
        "Note: Deactivating the user account itself is handled in the IT/account portal, not the RBAC API. "
        "Returns: {uuid, org_version, groups: [{name, roles, recent_activity}], "
        "access: [{permission, role_name, ...}], user_actions: {total_count, by_group, by_type, recent}, "
        "summary: {group_count, permission_count, actions_by_user, recent_actions_on_groups}}. "
        "The user is identified by UUID in the response (email, last_name redacted).\n"
        "Caveats:\n"
        "- Org admins have implicit full access that bypasses all RBAC checks; "
        "this is not reflected in the returned data.\n"
        "- Permission names are naming conventions only -- RBAC cannot confirm what UI elements "
        "or API endpoints they control."
    ),
    requires_auth=True,
    api_version=ApiVersion.UNIFIED,
    required_relation="rbac_roles_read",
    v1_permission=("principal", "read"),
    redacted_fields=_PRINCIPAL_REDACTED_FIELDS,
)
def get_user_state(
    request: HttpRequest,
    *,
    username: str,
    include_group_roles: bool = _DEFAULT_INCLUDE_GROUP_ROLES,
    include_permissions: bool = _DEFAULT_INCLUDE_PERMISSIONS,
    audit_log_limit: int = _DEFAULT_AUDIT_LOG_LIMIT,
) -> str:
    """Get comprehensive RBAC state for a user including groups, access, and audit activity."""
    # Clamp audit_log_limit to prevent expensive queries
    audit_log_limit = min(max(audit_log_limit, 1), 100)

    tenant = getattr(request, "tenant", None)
    if not tenant:
        return json.dumps({"error": "No tenant context available"})

    # Determine V1/V2 mode
    is_v2 = tenant and is_v2_write_activated(tenant)
    org_version = "v2" if is_v2 else "v1"

    # Resolve username or display name
    resolved_principal, resolved_username, candidates = _lookup_principal(request, username, tenant)
    if candidates:
        return json.dumps(
            {
                "error": f"Multiple users match '{username}'. Please specify the exact username.",
                "candidates": candidates,
            }
        )
    if not resolved_principal or not resolved_username:
        return json.dumps(
            {
                "error": f"User '{username}' not found in this organization",
                "hint": "Use list_principals(usernames='<partial>', match_criteria='partial') to search.",
            }
        )
    principal: Principal = resolved_principal
    username = resolved_username

    result: dict[str, Any] = {
        **({"uuid": str(principal.uuid)} if _is_pii_redaction_enabled() else {"username": username}),
        "org_version": org_version,
        "groups": [],
        "access": [],
        "summary": {
            "group_count": 0,
            "permission_count": 0,
        },
    }

    # Get groups the user belongs to with prefetched policies and roles
    groups = (
        Group.objects.filter(principals=principal, tenant=tenant).prefetch_related("policies__roles").order_by("name")
    )
    group_list = list(groups)
    all_group_names = [g.name for g in group_list]

    # Batch fetch audit activity for all groups (avoids N+1 queries)
    # Use exact UUID matching via resource_uuid field for reliable group identification
    group_uuid_to_name: dict[str, str] = {str(g.uuid): g.name for g in group_list}
    group_activity_map: dict[str, list[dict[str, Any]]] = {name: [] for name in all_group_names}

    if group_list:
        group_uuids = [g.uuid for g in group_list]

        all_activity = AuditLog.objects.filter(
            tenant=tenant,
            resource_type=AuditLog.GROUP,
            resource_uuid__in=group_uuids,
        ).order_by("-created")[: audit_log_limit * len(group_list)]

        # Map entries to groups by exact UUID match
        for entry in all_activity:
            group_name = group_uuid_to_name.get(str(entry.resource_uuid))
            if group_name and len(group_activity_map[group_name]) < audit_log_limit:
                group_activity_map[group_name].append(
                    {
                        "action": entry.action,
                        "resource_type": entry.resource_type,
                        "principal_username": entry.principal_username,
                        "description": entry.description,
                        "created": entry.created.isoformat() if entry.created else None,
                    }
                )

    group_data = []
    for group in group_list:
        group_info: dict[str, Any] = {
            "uuid": str(group.uuid),
            "name": group.name,
            "description": group.description or "",
        }

        # Get roles for each group if requested (using prefetched data)
        if include_group_roles:
            roles_in_group = []
            for policy in group.policies.all():
                for role in policy.roles.all():
                    roles_in_group.append(
                        {
                            "uuid": str(role.uuid),
                            "name": role.name,
                            "display_name": role.display_name or role.name,
                            "system": getattr(role, "system", False),
                        }
                    )
            group_info["roles"] = roles_in_group
            group_info["role_count"] = len(roles_in_group)

        # Get recent audit activity for this group from pre-fetched map
        group_info["recent_activity"] = group_activity_map.get(group.name, [])
        group_info["recent_activity_count"] = len(group_info["recent_activity"])

        group_data.append(group_info)

    result["groups"] = group_data
    result["summary"]["group_count"] = len(group_data)

    # Get access/permissions for the user
    if include_permissions:
        if is_v2:
            result["access"] = _get_user_access_v2(request, principal, tenant)
        else:
            result["access"] = _get_user_access_v1(request, username)

    result["summary"]["permission_count"] = len(result["access"])

    # Get actions performed BY this user (across all groups they're in)
    user_performed_actions = AuditLog.objects.filter(
        tenant=tenant,
        principal_username=username,
    ).order_by(
        "-created"
    )[: audit_log_limit * 2]

    # Group user actions by target group using exact UUID matching
    user_actions_by_group: dict[str, list[dict[str, Any]]] = {}
    user_action_counts: dict[str, int] = {}

    for entry in user_performed_actions:
        action_info = {
            "action": entry.action,
            "resource_type": entry.resource_type,
            "description": entry.description,
            "created": entry.created.isoformat() if entry.created else None,
        }

        # Match group by exact UUID if this is a group action
        target_group = None
        if entry.resource_type == AuditLog.GROUP and entry.resource_uuid:
            target_group = group_uuid_to_name.get(str(entry.resource_uuid))

        if target_group:
            if target_group not in user_actions_by_group:
                user_actions_by_group[target_group] = []
            user_actions_by_group[target_group].append(action_info)

        # Count by action type
        action_key = f"{entry.resource_type}:{entry.action}"
        user_action_counts[action_key] = user_action_counts.get(action_key, 0) + 1

    result["user_actions"] = {
        "total_count": len(user_performed_actions),
        "by_group": user_actions_by_group,
        "by_type": user_action_counts,
        "recent": [
            {
                "action": entry.action,
                "resource_type": entry.resource_type,
                "description": entry.description,
                "created": entry.created.isoformat() if entry.created else None,
            }
            for entry in user_performed_actions[:audit_log_limit]
        ],
    }

    # Get total recent activity count across all groups (actions on the groups)
    total_activity = 0
    for g in result["groups"]:
        total_activity += g.get("recent_activity_count", 0)
    result["summary"]["recent_actions_on_groups"] = total_activity
    result["summary"]["actions_by_user"] = len(user_performed_actions)

    result["hints"] = {
        "check_specific_permission": (
            "Use check_user_permission(username=..., permission='app:resource:verb') to verify"
        ),
        "view_audit_details": "Use list_audit_logs(group_name='<group>', include_authorization=True) for details",
        "trace_role_permissions": "Use get_role(role_uuid='<uuid>') to see all permissions granted by a role",
    }

    return json.dumps(result, default=str)


@register_tool(
    description=(
        "Look up a contractor, vendor, consultant, temp, intern, or any non-employee by name. "
        "Returns their full RBAC state: group memberships, roles, permissions, and audit trail. "
        "Use for offboarding, compliance reviews, access audits, or deprovisioning. "
        "This is the same as get_user_state -- all employment types are regular RBAC users."
    ),
    requires_auth=True,
    api_version=ApiVersion.UNIFIED,
    required_relation="rbac_roles_read",
    v1_permission=("principal", "read"),
)
def lookup_person(
    request: HttpRequest,
    *,
    username: str,
    include_group_roles: bool = _DEFAULT_INCLUDE_GROUP_ROLES,
    include_permissions: bool = _DEFAULT_INCLUDE_PERMISSIONS,
    audit_log_limit: int = _DEFAULT_AUDIT_LOG_LIMIT,
) -> str:
    """Alias for get_user_state that surfaces contractor/vendor/consultant terminology."""
    return get_user_state(
        request,
        username=username,
        include_group_roles=include_group_roles,
        include_permissions=include_permissions,
        audit_log_limit=audit_log_limit,
    )


def _get_user_access_v1(request: HttpRequest, username: str) -> list[dict[str, Any]]:
    """Get user's access permissions using V1 API.

    The access endpoint requires an application filter, so we first get distinct
    applications the user has access to, then query each.
    """
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return []

    principal = Principal.objects.filter(username=username, tenant=tenant).first()
    if not principal:
        return []

    # Get distinct applications the user has access to
    applications = (
        Access.objects.filter(
            role__policies__group__principals=principal,
            role__policies__group__tenant=tenant,
        )
        .values_list("permission__application", flat=True)
        .distinct()
    )

    access_list: list[dict[str, Any]] = []
    seen_permissions: set[str] = set()
    path = reverse("v1_management:access")

    for app in applications:
        if not app:
            continue
        query_params: dict[str, str] = {
            "application": app,
            "username": username,
            "limit": "1000",
        }
        try:
            raw = _call_view(request, _access_view, path, query_params)
            data = json.loads(raw)
            for entry in data.get("data", []):
                perm = entry.get("permission", "")
                if perm and perm not in seen_permissions:
                    seen_permissions.add(perm)
                    access_list.append(entry)
        except Exception:
            logger.warning("mcp: failed to get V1 access for user=%s app=%s", username, app, exc_info=True)

    return access_list


@register_tool(
    description=(
        "Get a summary of recent RBAC changes. Returns changes grouped by resource type and action, "
        "with statistics. Set 'days' (1-30, default 7) to control how far back to look. "
        "Returns: total change count, changes by resource type (group/role/role_v2/workspace/role_binding), "
        "changes by action (create/delete/edit/add/remove), top actors with change counts, "
        "and the 100 most recent changes. "
        "Use this tool to get an overview of what changed, then use list_audit_logs for details. "
        "RESPONSE FORMAT: Consolidate by actor. For each actor: '<actor>: <count> <action> actions on "
        "<resource_type> (<day of week>). <actor> holds <role name if known>.' "
        "Note patterns like 'Matches expected automation pattern' for service accounts. "
        "Format dates as day of week (Monday, Tuesday, etc.), not ISO timestamps. "
        "AFTER ANALYSIS: This is a review-only tool. Present the summary and let the user ask follow-up questions. "
        "Do NOT offer write-action suggestions."
    ),
    requires_auth=True,
    required_relation="rbac_roles_read",
    v1_permission=("admin", "only"),
)
def get_rbac_recent_changes(
    request: HttpRequest,
    *,
    days: int = 7,
) -> str:
    """Get a summary of recent RBAC changes."""
    days = min(max(days, 1), 30)

    tenant = getattr(request, "tenant", None)
    if not tenant:
        return json.dumps({"error": "No tenant context available"})

    cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)

    entries = AuditLog.objects.filter(tenant=tenant, created__gte=cutoff).order_by("-created")

    total_count = entries.count()
    if total_count == 0:
        return json.dumps(
            {
                "summary": {
                    "days_reviewed": days,
                    "total_changes": 0,
                    "message": f"No RBAC changes in the last {days} days.",
                },
                "by_resource_type": {},
                "by_action": {},
                "by_actor": {},
                "recent_changes": [],
            }
        )

    by_resource: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_actor: dict[str, int] = {}

    entry_list = list(entries[:500])

    for entry in entry_list:
        rt = entry.resource_type
        by_resource[rt] = by_resource.get(rt, 0) + 1

        act = entry.action
        by_action[act] = by_action.get(act, 0) + 1

        actor = entry.principal_username
        by_actor[actor] = by_actor.get(actor, 0) + 1

    recent_changes = [
        {
            "actor": entry.principal_username,
            "action": entry.action,
            "resource_type": entry.resource_type,
            "description": entry.description[:200],
            "created": entry.created.isoformat() if entry.created else None,
        }
        for entry in entry_list[:100]
    ]

    sorted_actors = sorted(by_actor.items(), key=lambda x: x[1], reverse=True)[:10]
    by_resource_sorted = dict(sorted(by_resource.items(), key=lambda x: x[1], reverse=True))
    by_action_sorted = dict(sorted(by_action.items(), key=lambda x: x[1], reverse=True))

    result = {
        "summary": {
            "days_reviewed": days,
            "total_changes": total_count,
            "unique_actors": len(by_actor),
            "period_start": cutoff.isoformat(),
            "period_end": datetime.now(timezone.utc).isoformat(),
        },
        "by_resource_type": by_resource_sorted,
        "by_action": by_action_sorted,
        "by_actor": dict(sorted_actors),
        "recent_changes": recent_changes,
    }

    return json.dumps(result, default=str)


@register_tool(
    description=(
        "Investigate who changed a specific group. Returns audit log entries for the group with "
        "authorization context for each actor. Best tool for answering 'Who added role X to group Y?' "
        "or 'Who modified the Contractors group last week?'. "
        "USAGE: Provide group_name (required). Optionally filter by role_name to find changes "
        "involving a specific role (e.g., 'Vulnerability administrator'). Set include_authorization=true "
        "(default) to see what role/permission authorized each action. "
        "RETURNS: {group: {uuid, name, current_roles}, audit_entries: [{actor, action, description, "
        "created, authorized_by: {role, via_group, permission}}]}. "
        "RESPONSE FORMAT: State who performed the action, when (formatted as '14 April at 9:32 AM'), "
        "the description, and what authority they had. Example: 'The audit log shows jdoe added the "
        "Vulnerability administrator role to group Contractors on 14 April at 9:32 AM. jdoe currently "
        "holds the User Access administrator role via the Access Governance group, which grants "
        "rbac:group:write.'"
    ),
    requires_auth=True,
    required_relation="rbac_roles_read",
    v1_permission=("group", "read"),
)
def investigate_group_changes(
    request: HttpRequest,
    *,
    group_name: str,
    role_name: str = "",
    action: str = "",
    limit: int = 20,
    include_authorization: bool = True,
) -> str:
    """Investigate changes to a specific group with full authorization context."""
    limit = min(max(limit, 1), 100)

    tenant = getattr(request, "tenant", None)
    if not tenant:
        return json.dumps({"error": "No tenant context available"})

    # Step 1: Find the group by name
    group = Group.objects.filter(name__iexact=group_name, tenant=tenant).first()
    if not group:
        # Try partial match - first with full name, then with prefix segment
        groups = Group.objects.filter(name__icontains=group_name, tenant=tenant).values("uuid", "name")[:5]
        if not groups:
            # Try matching on prefix (first segment split by common delimiters)
            segments = re.split(r"[-_\s]", group_name)
            if segments and len(segments[0]) >= 3:
                groups = Group.objects.filter(name__icontains=segments[0], tenant=tenant).values("uuid", "name")[:5]
        if groups:
            suggestions = [{"uuid": str(g["uuid"]), "name": g["name"]} for g in groups]
            return json.dumps(
                {
                    "error": f"Group '{group_name}' not found (exact match)",
                    "did_you_mean": suggestions,
                    "hint": "Use the exact group name from the suggestions above.",
                }
            )
        return json.dumps(
            {
                "error": f"Group '{group_name}' not found",
                "hint": "Use list_groups(name='<partial>') to search for groups.",
            }
        )

    # Step 2: Get current roles assigned to the group
    current_roles = []
    for policy in group.policies.prefetch_related("roles").all():
        for role in policy.roles.all():
            current_roles.append(
                {
                    "uuid": str(role.uuid),
                    "name": role.name,
                    "display_name": role.display_name or role.name,
                    "system": getattr(role, "system", False),
                }
            )

    # Step 3: Query audit logs for this group using exact UUID match
    queryset = AuditLog.objects.filter(
        tenant=tenant,
        resource_type=AuditLog.GROUP,
        resource_uuid=group.uuid,
    ).order_by("-created")

    if action:
        queryset = queryset.filter(action=action)

    if role_name:
        queryset = queryset.filter(description__icontains=role_name)

    total_count = queryset.count()
    entries = list(queryset[:limit])

    if not entries:
        return json.dumps(
            {
                "group": {
                    "uuid": str(group.uuid),
                    "name": group.name,
                    "description": group.description or "",
                    "current_roles": current_roles,
                    "current_role_count": len(current_roles),
                },
                "audit_entries": [],
                "summary": {
                    "total_changes_found": 0,
                    "message": f"No audit log entries found for group '{group_name}'"
                    + (f" with role containing '{role_name}'" if role_name else "")
                    + (f" and action='{action}'" if action else ""),
                },
            }
        )

    # Step 4: Build results with optional authorization context
    auth_cache: dict[str, dict[str, dict[str, Any] | None]] = {}
    org_admin_cache: dict[str, bool] = {}
    org_admin_auth: dict[str, Any] = {
        "role": "Org Admin",
        "via_group": None,
        "permission": "(bypasses all RBAC checks)",
    }

    audit_entries = []
    actors_seen: set[str] = set()

    for entry in entries:
        actor = entry.principal_username
        entry_action = entry.action
        actors_seen.add(actor)

        result_entry: dict[str, Any] = {
            "actor": actor,
            "action": entry_action,
            "resource_type": entry.resource_type,
            "description": entry.description,
            "created": entry.created.isoformat() if entry.created else None,
        }

        if include_authorization:
            required_perms = _get_required_permissions(entry.resource_type, entry_action)
            cache_key = f"{entry.resource_type}:{entry_action}"

            if actor not in auth_cache:
                auth_cache[actor] = {}

            if actor not in org_admin_cache:
                org_admin_cache[actor] = _is_org_admin(actor, tenant.org_id)

            auth_info: dict[str, Any] | None
            if org_admin_cache[actor]:
                auth_info = org_admin_auth
            elif cache_key not in auth_cache[actor]:
                auth_cache[actor][cache_key] = _find_authorizing_role(actor, tenant, required_perms)
                auth_info = auth_cache[actor][cache_key]
            else:
                auth_info = auth_cache[actor][cache_key]

            result_entry["authorized_by"] = auth_info
            if not auth_info:
                result_entry["authorization_note"] = (
                    f"User '{actor}' not found or no longer has permissions. " f"Required: {required_perms}"
                )

        audit_entries.append(result_entry)

    # Step 5: Build summary statistics
    action_counts: dict[str, int] = {}
    for entry in audit_entries:
        act = entry["action"]
        action_counts[act] = action_counts.get(act, 0) + 1

    result: dict[str, Any] = {
        "group": {
            "uuid": str(group.uuid),
            "name": group.name,
            "description": group.description or "",
            "current_roles": current_roles,
            "current_role_count": len(current_roles),
        },
        "audit_entries": audit_entries,
        "summary": {
            "total_changes_found": total_count,
            "changes_returned": len(audit_entries),
            "unique_actors": len(actors_seen),
            "actors": list(actors_seen),
            "by_action": action_counts,
        },
        "hints": {
            "actor_details": "Use list_principals(usernames='<actor>', match_criteria='exact') to get actor details",
            "actor_permissions": "Use search_roles(username='<actor>') to see what roles they currently have",
            "role_details": "Use get_role(role_uuid='<uuid>') to see permissions granted by a role",
        },
    }

    # Check if the role mentioned is currently assigned
    if role_name:
        role_found = any(
            role_name.lower() in r["name"].lower() or role_name.lower() in r["display_name"].lower()
            for r in current_roles
        )
        result["role_currently_assigned"] = role_found
        if role_found:
            matching_roles = [
                r
                for r in current_roles
                if role_name.lower() in r["name"].lower() or role_name.lower() in r["display_name"].lower()
            ]
            result["matching_current_roles"] = matching_roles

    return json.dumps(result, default=str)


def _build_expected_perm_full(application: str, expected_permission: str, expected_verb: str) -> str:
    """Build the full permission string to search for."""
    if expected_permission and application:
        if ":" in expected_permission:
            return expected_permission
        return f"{application}:*:{expected_permission}"
    elif expected_verb and application:
        return f"{application}:*:{expected_verb}"
    return ""


def _check_permission_match(
    expected_perm_full: str,
    expected_verb: str,
    application: str,
    all_permissions: set[str],
    permission_sources: dict[str, list[dict[str, Any]]],
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    """Check if expected permission is granted. Returns (found, matched_perm, sources)."""
    for perm in all_permissions:
        if (
            _permission_matches(perm, expected_perm_full)
            or _permission_matches(expected_perm_full, perm)
            or (expected_verb and perm.endswith(f":{expected_verb}") and perm.startswith(f"{application}:"))
        ):
            return True, perm, permission_sources.get(perm, [])
    return False, None, []


def _get_available_verbs(application: str, all_permissions: set[str]) -> list[str]:
    """Extract available verbs for an application from permissions."""
    available_verbs: set[str] = set()
    for perm in all_permissions:
        if perm.startswith(f"{application}:"):
            parts = perm.split(":")
            if len(parts) >= 3:
                available_verbs.add(parts[2])
    return sorted(available_verbs)


def _analyze_expected_permission(
    application: str,
    expected_permission: str,
    expected_verb: str,
    all_permissions: set[str],
    permission_sources: dict[str, list[dict[str, Any]]],
    is_org_admin: bool,
    analysis: dict[str, Any],
) -> None:
    """Analyze whether expected permission is granted and update analysis dict in place."""
    expected_perm_full = _build_expected_perm_full(application, expected_permission, expected_verb)
    if not expected_perm_full:
        return

    has_permission, matched_permission, matched_sources = _check_permission_match(
        expected_perm_full, expected_verb, application, all_permissions, permission_sources
    )

    if has_permission or is_org_admin:
        analysis["has_expected_permission"] = True
        analysis["expected_permission_check"] = {
            "looking_for": expected_perm_full,
            "found": True,
            "matched_permission": matched_permission if not is_org_admin else "(org admin bypass)",
            "granted_via": matched_sources if not is_org_admin else [{"role": "Org Admin"}],
        }
        if is_org_admin:
            analysis["note"] = "User is org admin and has implicit access to everything"
    else:
        analysis["has_expected_permission"] = False
        analysis["expected_permission_check"] = {
            "looking_for": expected_perm_full,
            "found": False,
            "available_verbs_for_app": _get_available_verbs(application, all_permissions),
        }


@register_tool(
    description=(
        "Investigate why a user has or lacks expected permissions, especially when they belong to "
        "multiple groups. Supports both V1 and V2 organizations (auto-detected). "
        "Use this when a user reports they can't do something despite being in a group that should grant access. "
        "SCENARIO: 'User is in Compliance Auditors AND Compliance Admins but can't edit compliance policies' "
        "→ call investigate_user_access(username='user', application='compliance', expected_permission='write'). "
        "The tool will: (1) confirm the user exists and check org admin status, (2) list ALL groups/role bindings, "
        "(3) expand each role to show actual permissions, (4) check effective access for the application, "
        "(5) identify if the expected permission is missing and explain why. "
        "RBAC is additive, so users get the most permissive access from all their memberships. "
        "Common causes: role doesn't contain the assumed permission, group doesn't have the expected role. "
        "V1 RETURNS: {user, org_version, groups: [{roles: [{permissions}]}], effective_access, analysis}. "
        "V2 RETURNS: {user, org_version, groups, role_bindings: [{role: {permissions}}], effective_access, analysis}. "
        "AFTER ANALYSIS: Present numbered remediation options: "
        "(1) add a role containing the missing permission to the user's group, "
        "(2) create a new custom role with the missing permission and add it to the group, "
        "(3) do nothing -- audit the role definition first. "
        "Format as 'Reply 1, 2, or 3 -- or no'. Do NOT execute write tools without explicit user selection.\n"
        "Caveats:\n"
        "- Org admins bypass all RBAC checks; this tool returns only explicitly assigned permissions, "
        "not their effective unlimited access.\n"
        "- In V1, roles are assigned to groups -- roles cannot be assigned directly to users. "
        "In V2, roles are bound to subjects via role bindings, which can target individual users.\n"
        "- Permission names are naming conventions only -- RBAC cannot confirm what UI elements "
        "or API endpoints they control."
    ),
    requires_auth=True,
    api_version=ApiVersion.COMMON,
    required_relation="rbac_roles_read",
    v1_permission=("principal", "read"),
)
def investigate_user_access(
    request: HttpRequest,
    *,
    username: str,
    application: str = "",
    expected_permission: str = "",
    expected_verb: str = "",
) -> str:
    """Investigate why a user has or lacks expected permissions across multiple groups."""
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return json.dumps({"error": "No tenant context available"})

    org_id = getattr(request.user, "org_id", None)
    is_v2 = is_v2_write_activated(tenant)
    org_version = "v2" if is_v2 else "v1"

    # Step 1: Resolve username or display name and get org admin status
    resolved_principal, resolved_username, candidates = _lookup_principal(request, username, tenant)
    if candidates:
        return json.dumps(
            {
                "error": f"Multiple users match '{username}'. Please specify the exact username.",
                "candidates": candidates,
            }
        )
    if not resolved_principal or not resolved_username:
        return json.dumps(
            {
                "error": f"User '{username}' not found in this organization",
                "hint": "Use list_principals(usernames='<partial>', match_criteria='partial') to search.",
            }
        )
    principal: Principal = resolved_principal
    username = resolved_username
    is_org_admin = False

    # Check org admin status via BOP
    if org_id:
        is_org_admin = _is_org_admin(username, org_id)

    user_info: dict[str, Any] = {
        "username": username,
        "uuid": str(principal.uuid),
        "exists": True,
        "is_org_admin": is_org_admin,
    }

    if is_org_admin:
        user_info["note"] = "User is an org admin and bypasses all RBAC checks"

    # Branch based on V1 or V2
    if is_v2:
        return _investigate_user_access_v2(
            request,
            principal,
            tenant,
            username,
            application,
            expected_permission,
            expected_verb,
            user_info,
            is_org_admin,
            org_version,
        )

    # V1 path: Get all groups the user belongs to
    groups = (
        Group.objects.filter(principals=principal, tenant=tenant)
        .prefetch_related("policies__roles__access__permission")
        .order_by("name")
    )
    groups_list = list(groups)

    if not groups_list and not is_org_admin:
        return json.dumps(
            {
                "user": user_info,
                "org_version": org_version,
                "groups": [],
                "effective_access": [],
                "analysis": {
                    "has_expected_permission": False,
                    "message": f"User '{username}' is not a member of any groups. No permissions are granted.",
                    "hint": "Use list_groups() to see available groups, then add the user to appropriate groups.",
                },
            }
        )

    # Step 3: For each group, get roles and expand to permissions (V1)
    groups_data: list[dict[str, Any]] = []
    all_permissions: set[str] = set()
    permission_sources: dict[str, list[dict[str, str]]] = {}

    for group in groups_list:
        group_info: dict[str, Any] = {
            "uuid": str(group.uuid),
            "name": group.name,
            "description": group.description or "",
            "roles": [],
        }

        for policy in group.policies.all():
            for role in policy.roles.all():
                role_info: dict[str, Any] = {
                    "uuid": str(role.uuid),
                    "name": role.name,
                    "display_name": role.display_name or role.name,
                    "system": getattr(role, "system", False),
                    "permissions": [],
                }

                for access in role.access.all():
                    if access.permission:
                        perm_str = access.permission.permission
                        role_info["permissions"].append(perm_str)
                        all_permissions.add(perm_str)

                        if perm_str not in permission_sources:
                            permission_sources[perm_str] = []
                        permission_sources[perm_str].append(
                            {
                                "group": group.name,
                                "role": role.display_name or role.name,
                            }
                        )

                role_info["permission_count"] = len(role_info["permissions"])
                group_info["roles"].append(role_info)

        group_info["role_count"] = len(group_info["roles"])
        groups_data.append(group_info)

    # Step 4: Get effective access for the user (filtered by application if provided) - V1
    effective_access: list[dict[str, Any]] = []
    effective_access_error: str | None = None
    if application:
        path = reverse("v1_management:access")
        query_params: dict[str, str] = {
            "application": application,
            "username": username,
            "limit": "1000",
        }
        try:
            raw = _call_view(request, _access_view, path, query_params)
            data = json.loads(raw)
            effective_access = data.get("data", [])
        except Exception as e:
            effective_access_error = str(e)
            logger.warning("mcp: failed to get effective access for user=%s app=%s: %s", username, application, e)

    # Step 5: Analyze the expected permission
    analysis: dict[str, Any] = {
        "total_groups": len(groups_data),
        "total_roles": sum(g["role_count"] for g in groups_data),
        "total_unique_permissions": len(all_permissions),
    }

    if effective_access_error:
        analysis["effective_access_error"] = effective_access_error

    if application:
        app_permissions = [p for p in all_permissions if p.startswith(f"{application}:")]
        analysis["permissions_for_application"] = sorted(app_permissions)
        analysis["application_permission_count"] = len(app_permissions)

    # Use shared helper for permission analysis
    _analyze_expected_permission(
        application, expected_permission, expected_verb, all_permissions, permission_sources, is_org_admin, analysis
    )

    # Add V1-specific gap analysis if permission not found
    expected_perm_full = _build_expected_perm_full(application, expected_permission, expected_verb)
    if expected_perm_full and analysis.get("has_expected_permission") is False:
        gaps: list[str] = []
        for group_data in groups_data:
            group_name = group_data["name"]
            has_any_app_access = False
            for role_data in group_data["roles"]:
                for perm in role_data["permissions"]:
                    if perm.startswith(f"{application}:"):
                        has_any_app_access = True
                        break
            if not has_any_app_access and application:
                gaps.append(
                    f"Group '{group_name}' has {len(group_data['roles'])} role(s) but none grant "
                    f"any {application} permissions"
                )
            elif has_any_app_access:
                app_perms_in_group = []
                for role_data in group_data["roles"]:
                    for perm in role_data["permissions"]:
                        if perm.startswith(f"{application}:"):
                            app_perms_in_group.append(f"{role_data['display_name']}: {perm}")
                missing = expected_verb or expected_permission
                avail = ", ".join(app_perms_in_group[:5])
                suffix = f" (+{len(app_perms_in_group) - 5} more)" if len(app_perms_in_group) > 5 else ""
                gaps.append(
                    f"Group '{group_name}' grants {application} access but NOT '{missing}'. "
                    f"Available: {avail}{suffix}"
                )

        analysis["gaps"] = gaps
        analysis["diagnosis"] = (
            f"User '{username}' does not have '{expected_perm_full}' permission. "
            f"Neither of their {len(groups_data)} group memberships grants this specific access."
        )

    # Build final result (V1)
    result: dict[str, Any] = {
        "user": user_info,
        "org_version": org_version,
        "groups": groups_data,
        "effective_access": effective_access if application else [],
        "analysis": analysis,
        "permission_sources": permission_sources,
    }

    # Add hints
    result["hints"] = {
        "verify_specific_permission": (
            f"Use check_user_permission(username='{username}', permission='app:resource:verb')"
        ),
        "find_role_with_permission": "Use search_roles(permission='...') to find roles granting a permission",
        "check_role_contents": "Use get_role(role_uuid='...') to see all permissions in a role",
        "add_user_to_group": "Use list_groups(role_names='...') to find groups with a specific role",
    }

    return json.dumps(result, default=str)


def _investigate_user_access_v2(
    request: HttpRequest,
    principal: Principal,
    tenant: Any,
    username: str,
    application: str,
    expected_permission: str,
    expected_verb: str,
    user_info: dict[str, Any],
    is_org_admin: bool,
    org_version: str,
) -> str:
    """Investigate user access for V2 organizations using role bindings."""
    # Get groups the user belongs to
    groups = Group.objects.filter(principals=principal, tenant=tenant).order_by("name")
    groups_list = list(groups)

    # Get direct role bindings for the principal
    direct_binding_ids = set(
        RoleBindingPrincipal.objects.filter(principal=principal).values_list("binding_id", flat=True)
    )

    # Get group-based role bindings with group info
    group_bindings_qs = RoleBindingGroup.objects.filter(group__in=groups_list).select_related("group")
    group_binding_ids: set[int] = set()
    binding_to_group: dict[int, str] = {}
    for rbg in group_bindings_qs:
        group_binding_ids.add(rbg.binding_id)
        binding_to_group[rbg.binding_id] = rbg.group.name

    # Combine all binding IDs
    all_binding_ids = direct_binding_ids | group_binding_ids

    if not all_binding_ids and not groups_list and not is_org_admin:
        return json.dumps(
            {
                "user": user_info,
                "org_version": org_version,
                "groups": [],
                "role_bindings": [],
                "effective_access": [],
                "analysis": {
                    "has_expected_permission": False,
                    "message": f"User '{username}' has no role bindings or group memberships.",
                    "hint": "Use list_role_bindings() to see available bindings.",
                },
            }
        )

    # Get all bindings with their roles and permissions
    bindings = (
        RoleBinding.objects.filter(id__in=all_binding_ids, tenant=tenant)
        .select_related("role")
        .prefetch_related("role__permissions")
    )

    # Build groups data
    groups_data: list[dict[str, Any]] = []
    for group in groups_list:
        groups_data.append(
            {
                "uuid": str(group.uuid),
                "name": group.name,
                "description": group.description or "",
            }
        )

    # Build role bindings data and collect permissions
    bindings_data: list[dict[str, Any]] = []
    all_permissions: set[str] = set()
    permission_sources: dict[str, list[dict[str, str | None]]] = {}

    for binding in bindings:
        role = binding.role
        if not role:
            continue

        # Determine binding source (direct or via group)
        if binding.id in binding_to_group:
            binding_source = "group"
            source_group = binding_to_group[binding.id]
        else:
            binding_source = "direct"
            source_group = None

        permissions_list: list[str] = []
        for perm in role.permissions.all():
            perm_str = f"{perm.application}:{perm.resource_type}:{perm.verb}"
            permissions_list.append(perm_str)
            all_permissions.add(perm_str)

            if perm_str not in permission_sources:
                permission_sources[perm_str] = []
            permission_sources[perm_str].append(
                {
                    "role": role.name,
                    "binding_source": binding_source,
                    "group": source_group,
                    "resource_scope": f"{binding.resource_type}:{binding.resource_id}",
                }
            )

        bindings_data.append(
            {
                "uuid": str(binding.uuid),
                "role": {
                    "uuid": str(role.uuid),
                    "name": role.name,
                    "permissions": permissions_list,
                    "permission_count": len(permissions_list),
                },
                "binding_source": binding_source,
                "source_group": source_group,
                "resource_type": binding.resource_type,
                "resource_id": binding.resource_id,
            }
        )

    # Get effective access
    effective_access = _get_user_access_v2(request, principal, tenant)
    if application:
        effective_access = [a for a in effective_access if a.get("application") == application]

    # Build analysis
    analysis: dict[str, Any] = {
        "total_groups": len(groups_data),
        "total_role_bindings": len(bindings_data),
        "total_unique_permissions": len(all_permissions),
    }

    if application:
        app_permissions = [p for p in all_permissions if p.startswith(f"{application}:")]
        analysis["permissions_for_application"] = sorted(app_permissions)
        analysis["application_permission_count"] = len(app_permissions)

    # Use shared helper for permission analysis
    _analyze_expected_permission(
        application, expected_permission, expected_verb, all_permissions, permission_sources, is_org_admin, analysis
    )

    # Add V2-specific gap analysis if permission not found
    expected_perm_full = _build_expected_perm_full(application, expected_permission, expected_verb)
    if expected_perm_full and analysis.get("has_expected_permission") is False:
        gaps: list[str] = []
        for binding_data in bindings_data:
            role_perms = binding_data["role"]["permissions"]
            has_any_app_access = any(p.startswith(f"{application}:") for p in role_perms)
            role_name = binding_data["role"]["name"]
            source = binding_data["source_group"] or "direct binding"

            if not has_any_app_access and application:
                gaps.append(f"Role '{role_name}' (via {source}) has no {application} permissions")
            elif has_any_app_access:
                app_perms = [p for p in role_perms if p.startswith(f"{application}:")]
                missing = expected_verb or expected_permission
                gaps.append(
                    f"Role '{role_name}' (via {source}) grants {application} access but NOT '{missing}'. "
                    f"Has: {', '.join(app_perms[:3])}"
                    + (f" (+{len(app_perms) - 3} more)" if len(app_perms) > 3 else "")
                )

        analysis["gaps"] = gaps
        analysis["diagnosis"] = (
            f"User '{username}' does not have '{expected_perm_full}' permission. "
            f"None of their {len(bindings_data)} role binding(s) grants this access."
        )

    # Build result
    result: dict[str, Any] = {
        "user": user_info,
        "org_version": org_version,
        "groups": groups_data,
        "role_bindings": bindings_data,
        "effective_access": effective_access,
        "analysis": analysis,
        "permission_sources": permission_sources,
    }

    # Add hints
    result["hints"] = {
        "verify_specific_permission": (
            f"Use check_user_permission(username='{username}', permission='app:resource:verb')"
        ),
        "list_user_bindings": (
            f"Use list_role_bindings(granted_subject_type='principal', "
            f"granted_subject_principal_user_id='{username}')"
        ),
        "find_role_with_permission": "Use search_roles(permission='...') to find roles granting a permission",
        "check_role_contents": "Use get_role(role_uuid='...') to see all permissions in a role",
    }

    return json.dumps(result, default=str)


def _get_user_access_v2(request: HttpRequest, principal: Principal, tenant: Any) -> list[dict[str, Any]]:
    """Get user's access permissions using V2 role bindings."""
    access_list: list[dict[str, Any]] = []
    seen_permissions: set[tuple[str, str, str]] = set()

    # Get direct role bindings for the principal
    direct_binding_ids = RoleBindingPrincipal.objects.filter(principal=principal).values_list("binding_id", flat=True)

    # Get group-based role bindings
    user_groups = principal.group.filter(tenant=tenant)
    group_binding_ids = RoleBindingGroup.objects.filter(group__in=user_groups).values_list("binding_id", flat=True)

    # Combine all binding IDs
    all_binding_ids = set(direct_binding_ids) | set(group_binding_ids)

    # Get all bindings with their roles and permissions
    bindings = (
        RoleBinding.objects.filter(id__in=all_binding_ids, tenant=tenant)
        .select_related("role")
        .prefetch_related("role__permissions")
    )

    for binding in bindings:
        role = binding.role
        if not role:
            continue

        for perm in role.permissions.all():
            perm_str = f"{perm.application}:{perm.resource_type}:{perm.verb}"
            dedup_key = (perm_str, binding.resource_type, binding.resource_id)
            if dedup_key not in seen_permissions:
                seen_permissions.add(dedup_key)
                access_list.append(
                    {
                        "permission": perm_str,
                        "application": perm.application,
                        "resource_type": perm.resource_type,
                        "verb": perm.verb,
                        "role_name": role.name,
                        "role_uuid": str(role.uuid),
                        "resource_scope": {
                            "type": binding.resource_type,
                            "id": binding.resource_id,
                        },
                    }
                )

    return access_list


# --- Write tool implementations (gated by MCP_WRITE_ENABLED) ---

# ┌──────────────────────────────────────┬───────────┬─────────────────────────────────────────────────────┐
# │ MCP Tool                             │ Gating    │ API Endpoint                                        │
# ├──────────────────────────────────────┼───────────┼─────────────────────────────────────────────────────┤
# │ create_group                         │ both      │ POST /api/v1/groups/                                │
# │ add_principals_to_group              │ both      │ POST /api/v1/groups/{uuid}/principals/              │
# │ add_roles_to_group                   │ v1        │ POST /api/v1/groups/{uuid}/roles/                   │
# │ create_role_v1                       │ v1        │ POST /api/v1/roles/                                 │
# │ create_role                          │ v2        │ POST /api/v2/roles/                                 │
# │ create_role_bindings                 │ v2        │ POST /api/v2/role-bindings/:batchCreate              │
# │ create_workspace                     │ v2        │ POST /api/v2/workspaces/                            │
# │ create_cross_account_request         │ both      │ POST /api/v1/cross-account-requests/                │
# └──────────────────────────────────────┴───────────┴─────────────────────────────────────────────────────┘


@register_tool(
    description=(
        "Create a new custom group. Groups are collections of principals (users) that can be "
        "assigned roles. Works for both V1 and V2 organizations. "
        "Required: name (string). Optional: description (string). "
        "Example: create_group(name='Engineering Team', description='Backend engineers') "
        "Returns: the created group object with uuid, name, description. "
        "Calls: POST /api/v1/groups/"
    ),
    requires_auth=True,
    write=True,
)
def create_group(
    request: HttpRequest,
    *,
    name: str,
    description: str = "",
) -> str:
    """Create a group by delegating to GroupViewSet."""
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description

    path = reverse("v1_management:group-list")
    return _call_view_write(request, _group_create_view, path, body)


@register_tool(
    description=(
        "Add one or more principals (users) to a group. Provide either group_uuid OR group_name "
        "(group_uuid takes precedence). Works for both V1 and V2 organizations. "
        "Required: principals (list of usernames to add). "
        "Example: add_principals_to_group(group_name='Engineering', principals=['jdoe', 'jsmith']) "
        "Returns: {principals: [{username}], ...}. "
        "Calls: POST /api/v1/groups/{uuid}/principals/"
    ),
    requires_auth=True,
    write=True,
)
def add_principals_to_group(
    request: HttpRequest,
    *,
    group_uuid: str = "",
    group_name: str = "",
    principals: list[str],
) -> str:
    """Add principals to a group by delegating to GroupViewSet.principals."""
    resolved_uuid, error = _resolve_group_for_tool(request, group_uuid, group_name)
    if error:
        return error
    assert resolved_uuid is not None

    body = {"principals": [{"username": u} for u in principals]}
    path = reverse("v1_management:group-principals", kwargs={"uuid": resolved_uuid})
    return _call_view_write(request, _group_principals_write_view, path, body, uuid=resolved_uuid)


@register_tool(
    description=(
        "Assign one or more roles to a group. Provide either group_uuid OR group_name "
        "(group_uuid takes precedence). V1 only -- blocked for V2 organizations "
        "(use create_role_bindings instead). "
        "Required: roles (list of role UUIDs to assign). "
        "Example: add_roles_to_group(group_name='Engineering', roles=['uuid-1', 'uuid-2']) "
        "Returns: the updated group-roles mapping. "
        "Calls: POST /api/v1/groups/{uuid}/roles/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V1,
)
def add_roles_to_group(
    request: HttpRequest,
    *,
    group_uuid: str = "",
    group_name: str = "",
    roles: list[str],
) -> str:
    """Add roles to a group by delegating to GroupViewSet.roles."""
    resolved_uuid, error = _resolve_group_for_tool(request, group_uuid, group_name)
    if error:
        return error
    assert resolved_uuid is not None

    body = {"roles": [{"uuid": r} for r in roles]}
    path = reverse("v1_management:group-roles", kwargs={"uuid": resolved_uuid})
    return _call_view_write(request, _group_roles_write_view, path, body, uuid=resolved_uuid)


@register_tool(
    description=(
        "Create a custom role (V1 API). V1 only -- blocked for V2 organizations "
        "(use create_role for V2). "
        "Required: name, access (list of permission objects). "
        "Each access entry needs: permission (string 'app:resource:verb') and optionally "
        "resourceDefinitions (list of resource definition filters). "
        "Example: create_role_v1(name='Cost Reader', access=[{'permission': 'cost-management:cost_model:read'}]) "
        "Returns: the created role object with uuid, name, access list. "
        "Calls: POST /api/v1/roles/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V1,
)
def create_role_v1(
    request: HttpRequest,
    *,
    name: str,
    display_name: str = "",
    description: str = "",
    access: list[dict[str, Any]],
) -> str:
    """Create a V1 role by delegating to RoleViewSet."""
    body: dict[str, Any] = {"name": name, "access": access}
    if display_name:
        body["display_name"] = display_name
    if description:
        body["description"] = description

    path = reverse("v1_management:role-list")
    return _call_view_write(request, _role_v1_create_view, path, body)


@register_tool(
    description=(
        "Create a custom role (V2 API). V2 only -- requires workspace-enabled organization. "
        "Required: name, permissions (list of permission objects). "
        "Each permission needs: application, resource_type, operation. "
        "Example: create_role(name='Cost Reader', permissions=[{'application': 'cost-management', "
        "'resource_type': 'cost_model', 'operation': 'read'}]) "
        "Returns: the created role object with uuid, name, permissions list. "
        "Calls: POST /api/v2/roles/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def create_role(
    request: HttpRequest,
    *,
    name: str,
    description: str = "",
    permissions: list[dict[str, str]],
) -> str:
    """Create a V2 role by delegating to RoleV2ViewSet."""
    body: dict[str, Any] = {"name": name, "permissions": permissions}
    if description:
        body["description"] = description

    path = reverse("v2_management:roles-list")
    return _call_view_write(request, _role_v2_create_view, path, body)


@register_tool(
    description=(
        "Create role bindings (V2 API). Assigns roles to subjects (users/groups) "
        "within resource scopes (workspaces). V2 only. Can create one or many bindings. "
        "Required: bindings (list of binding objects, each with role, resource, subject). "
        "Each binding needs: role (UUID string), resource (object with type and id), "
        "subject (object with type and id -- type is 'principal' or 'group'). "
        "Example: create_role_bindings(bindings=[{"
        "'role': '<role-uuid>', 'resource': {'type': 'workspace', 'id': '<ws-uuid>'}, "
        "'subject': {'type': 'principal', 'id': '<user-uuid>'}}]) "
        "Returns: list of created role binding objects. "
        "Calls: POST /api/v2/role-bindings/:batchCreate"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def create_role_bindings(
    request: HttpRequest,
    *,
    bindings: list[dict[str, Any]],
) -> str:
    """Create role bindings by delegating to RoleBindingViewSet.batch_create."""
    body: dict[str, Any] = {"requests": bindings}
    path = reverse("v2_management:role-bindings-batch-create")
    return _call_view_write(request, _role_binding_batch_create_view, path, body)


@register_tool(
    description=(
        "Create a workspace (V2 API). Workspaces are hierarchical containers used to scope "
        "role bindings. V2 only. "
        "Required: name (string). Optional: description (string), parent_id (UUID of parent workspace). "
        "Example: create_workspace(name='EMEA Engineering', parent_id='<root-workspace-uuid>') "
        "Returns: the created workspace object with uuid, name, type, parent_id. "
        "Calls: POST /api/v2/workspaces/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def create_workspace(
    request: HttpRequest,
    *,
    name: str,
    description: str = "",
    parent_id: str = "",
) -> str:
    """Create a workspace by delegating to WorkspaceViewSet."""
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if parent_id:
        body["parent_id"] = parent_id

    path = reverse("v2_management:workspace-list")
    return _call_view_write(request, _workspace_create_view, path, body)


@register_tool(
    description=(
        "Create a cross-account access request. Allows users from one org (e.g. TAMs) "
        "to request temporary access to another org's resources. "
        "Required: target_org (the org ID to request access to), "
        "start_date (YYYY-MM-DD), end_date (YYYY-MM-DD), roles (list of role UUIDs). "
        "Example: create_cross_account_request(target_org='12345', "
        "start_date='2026-06-01', end_date='2026-06-30', roles=['<role-uuid>']) "
        "Returns: the created request with request_id, status, dates. "
        "Calls: POST /api/v1/cross-account-requests/"
    ),
    requires_auth=True,
    write=True,
)
def create_cross_account_request(
    request: HttpRequest,
    *,
    target_org: str,
    start_date: str,
    end_date: str,
    roles: list[str],
) -> str:
    """Create a cross-account request by delegating to CrossAccountRequestViewSet."""
    body: dict[str, Any] = {
        "target_org": target_org,
        "start_date": start_date,
        "end_date": end_date,
        "roles": roles,
    }

    path = reverse("v1_api:cross-list")
    return _call_view_write(request, _cross_account_create_view, path, body)


@register_tool(
    description=(
        "Check if a user can be delegated user access management without Org Admin privileges. "
        "Accepts username (e.g., 'doejoe') or display name (e.g., 'Joe Doe'). "
        "If a display name is given, the tool resolves it automatically. "
        "If multiple users match, candidates are returned for disambiguation.\n\n"
        "USE WHEN: 'delegate user access', 'let someone manage users without Org Admin', "
        "'give RBAC permissions', 'User Access administrator role'.\n\n"
        "BACKGROUND: 'User Access administrator' is a system role with rbac:* permissions. "
        "CAN: create/delete groups, assign roles, add/remove users, invite users, create custom roles. "
        "CANNOT: grant Org Admin flag, manage groups containing this role (escalation guard), "
        "access cost management/subscriptions.\n\n"
        "DECISION TREE:\n"
        "1. role_info.error → Role missing, contact Red Hat support.\n"
        "2. user_already_has_role=true → No action needed.\n"
        "3. user_info.is_org_admin=true → Redundant (Org Admin has full access).\n"
        "4. org_version='v1' → Call add_principals_to_group(group_uuid=existing_assignments[].uuid, "
        "principals=[username]), OR create_group() then add_role_to_group(role_uuid=role_info.uuid) "
        "then add_principals_to_group().\n"
        "5. org_version='v2' → Call create_role_bindings(role_id=role_info.uuid, subjects=[username]).\n\n"
        "Returns: {org_version, user_info, role_info, user_already_has_role, existing_assignments}."
    ),
    requires_auth=True,
    api_version=ApiVersion.UNIFIED,
    required_relation="rbac_roles_read",
    v1_permission=("role", "read"),
)
def guide_user_access_delegation(
    request: HttpRequest,
    *,
    username: str,
) -> str:
    """Check if a user can be delegated user access management without Org Admin privileges."""
    try:
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return json.dumps({"error": "No tenant context available"})

        is_v2 = is_v2_write_activated(tenant)
        user_access_admin_role_name = "User Access administrator"

        result: dict[str, Any] = {
            "org_version": "v2" if is_v2 else "v1",
            "user_info": None,
            "role_info": None,
            "user_already_has_role": False,
            "existing_assignments": [],
        }

        # Check if user exists (reuse shared lookup helper)
        original_input = username
        try:
            _principal, resolved_username, candidates = _lookup_principal(request, username, tenant)
            if candidates:
                result["user_info"] = {
                    "error": f"Multiple users match '{original_input}'. Please specify the exact username.",
                    "candidates": candidates,
                    "hint": "Re-call with the exact username from the candidates list.",
                }
            elif resolved_username:
                username = resolved_username
                user_info: dict = {"username": username}
                if resolved_username != original_input:
                    user_info["resolved_from"] = f"Display name '{original_input}' matched to '{resolved_username}'"
                # Fetch user details (org admin status, active status) via BOP
                principals_raw = list_principals(request, usernames=username, match_criteria="exact", limit=1)
                principals_data = json.loads(principals_raw)
                if principals_data.get("data"):
                    user_data = principals_data["data"][0]
                    user_info["is_org_admin"] = user_data.get("is_org_admin", False)
                    user_info["is_active"] = user_data.get("is_active", True)
                result["user_info"] = user_info
            else:
                # Fuzzy fallback: try display name search
                original_input = username
                name_raw = _list_principals_by_name(
                    request, username, limit=5, offset=0, sort_order="asc", status="enabled"
                )
                name_data = json.loads(name_raw)
                candidates = name_data.get("data", [])
                if len(candidates) == 1:
                    user_data = candidates[0]
                    username = user_data.get("username", username)
                    result["user_info"] = {
                        "username": username,
                        "is_org_admin": user_data.get("is_org_admin", False),
                        "is_active": user_data.get("is_active", True),
                        "resolved_from": f"Display name '{original_input}' matched to '{username}'",
                    }
                elif len(candidates) > 1:
                    result["user_info"] = {
                        "error": f"Multiple users match '{original_input}'",
                        "candidates": [
                            {
                                "username": c.get("username"),
                                "name": _format_candidate_name(c),
                            }
                            for c in candidates
                        ],
                        "hint": "Re-call with the exact username from the candidates list.",
                    }
                else:
                    result["user_info"] = {"error": f"User '{original_input}' not found"}
        except Exception as e:
            logger.warning("guide_user_access_delegation: Failed to verify user %s: %s", username, e)
            result["user_info"] = {"error": f"Could not verify user '{username}'"}

        # Find the 'User Access administrator' role
        role_uuid = None
        try:
            if is_v2:
                roles_raw = search_roles(request, name=user_access_admin_role_name, limit=10)
            else:
                roles_raw = search_roles(request, name=user_access_admin_role_name, system="true", limit=1)
            roles_data = json.loads(roles_raw)
            if roles_data.get("data"):
                role_data = next(
                    (
                        r
                        for r in roles_data["data"]
                        if r.get("name", "").lower() == user_access_admin_role_name.lower()
                    ),
                    roles_data["data"][0],
                )
                role_uuid = role_data.get("uuid") or role_data.get("id")
                result["role_info"] = {"uuid": role_uuid, "name": role_data.get("name")}
            else:
                result["role_info"] = {"error": "Role not found - contact Red Hat support"}
                return json.dumps(result)
        except Exception as e:
            logger.warning("guide_user_access_delegation: Failed to find role: %s", e)
            result["role_info"] = {"error": "Role not found"}
            return json.dumps(result)

        # Check current assignments and if user already has the role
        if is_v2:
            v2_role = RoleV2.objects.filter(uuid=role_uuid, tenant=tenant).first()
            if v2_role:
                # Get existing bindings
                bindings = RoleBinding.objects.filter(role=v2_role, tenant=tenant).annotate(
                    principal_count=Count("principal_entries", distinct=True),
                    group_count=Count("group_entries", distinct=True),
                )
                for b in bindings:
                    result["existing_assignments"].append(
                        {
                            "type": "role_binding",
                            "id": str(b.id),
                            "principals": b.principal_count,
                            "groups": b.group_count,
                        }
                    )

                # Check if user already has role (direct or via group)
                has_direct = RoleBindingPrincipal.objects.filter(
                    principal__username__iexact=username, principal__tenant=tenant, binding__role=v2_role
                ).exists()
                user_groups = Group.objects.filter(principals__username__iexact=username, tenant=tenant)
                has_via_group = RoleBindingGroup.objects.filter(
                    group__in=user_groups, binding__role=v2_role, binding__tenant=tenant
                ).exists()
                result["user_already_has_role"] = has_direct or has_via_group
        else:
            # V1: Check groups with this role
            try:
                groups_raw = list_groups(request, role_names=user_access_admin_role_name, limit=100)
                groups_data = json.loads(groups_raw)
                groups_with_role = {g.get("uuid"): g.get("name") for g in groups_data.get("data", [])}
                for group_uuid, name in groups_with_role.items():
                    result["existing_assignments"].append({"type": "group", "uuid": group_uuid, "name": name})

                # Check if user is in any of these groups
                user_groups_raw = list_groups(request, username=username, limit=100)
                user_groups_data = json.loads(user_groups_raw)
                user_group_uuids = {g.get("uuid") for g in user_groups_data.get("data", [])}
                result["user_already_has_role"] = bool(user_group_uuids & set(groups_with_role.keys()))
            except Exception as e:
                logger.warning("guide_user_access_delegation: Failed to check groups: %s", e)

        return json.dumps(result)
    except Exception:
        logger.exception("guide_user_access_delegation failed")
        return json.dumps({"error": "An internal error occurred. Please try again or contact support."})


# --- UPDATE tool implementations ---

# ┌──────────────────────────────────────┬───────────┬─────────────────────────────────────────────────────┐
# │ MCP Tool                             │ Gating    │ API Endpoint                                        │
# ├──────────────────────────────────────┼───────────┼─────────────────────────────────────────────────────┤
# │ update_group                         │ both      │ PUT /api/v1/groups/{uuid}/                          │
# │ update_role_v1                       │ v1        │ PUT /api/v1/roles/{uuid}/                           │
# │ patch_role_v1                        │ v1        │ PATCH /api/v1/roles/{uuid}/                         │
# │ update_role                          │ v2        │ PUT /api/v2/roles/{uuid}/                           │
# │ update_role_binding                  │ v2        │ PUT /api/v2/role-bindings/by-subject/                │
# │ update_workspace                     │ v2        │ PUT /api/v2/workspaces/{uuid}/                      │
# │ move_workspace                       │ v2        │ POST /api/v2/workspaces/{uuid}/move/                │
# │ update_cross_account_request         │ both      │ PUT /api/v1/cross-account-requests/{id}/            │
# │ patch_cross_account_request          │ both      │ PATCH /api/v1/cross-account-requests/{id}/          │
# └──────────────────────────────────────┴───────────┴─────────────────────────────────────────────────────┘


@register_tool(
    description=(
        "Update a custom group (full replacement). Provide either group_uuid OR group_name "
        "(group_uuid takes precedence). Works for both V1 and V2 organizations. "
        "System groups (platform_default, admin_default) cannot be modified. "
        "Required: name. Optional: description. "
        "Example: update_group(group_name='Engineering', name='Engineering Team', description='Updated desc') "
        "Returns: the updated group object. "
        "Calls: PUT /api/v1/groups/{uuid}/"
    ),
    requires_auth=True,
    write=True,
)
def update_group(
    request: HttpRequest,
    *,
    group_uuid: str = "",
    group_name: str = "",
    name: str,
    description: str = "",
) -> str:
    """Update a group by delegating to GroupViewSet."""
    resolved_uuid, error = _resolve_group_for_tool(request, group_uuid, group_name)
    if error:
        return error
    assert resolved_uuid is not None

    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description

    path = reverse("v1_management:group-detail", kwargs={"uuid": resolved_uuid})
    return _call_view_write(request, _group_update_view, path, body, method="PUT", uuid=resolved_uuid)


@register_tool(
    description=(
        "Update a custom role (V1 API, full replacement). V1 only -- blocked for V2 organizations "
        "(use update_role for V2). Replaces the entire role including permissions. "
        "Required: role_uuid, name, access (list of permission objects). "
        "Each access entry needs: permission (string 'app:resource:verb') and optionally "
        "resourceDefinitions. System roles cannot be modified. "
        "Example: update_role_v1(role_uuid='<uuid>', name='Cost Reader', "
        "access=[{'permission': 'cost-management:cost_model:read'}]) "
        "Returns: the updated role object. "
        "Calls: PUT /api/v1/roles/{uuid}/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V1,
)
def update_role_v1(
    request: HttpRequest,
    *,
    role_uuid: str,
    name: str,
    display_name: str = "",
    description: str = "",
    access: list[dict[str, Any]],
) -> str:
    """Update a V1 role by delegating to RoleViewSet."""
    body: dict[str, Any] = {"name": name, "access": access}
    if display_name:
        body["display_name"] = display_name
    if description:
        body["description"] = description

    path = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
    return _call_view_write(request, _role_v1_update_view, path, body, method="PUT", uuid=role_uuid)


@register_tool(
    description=(
        "Partially update a custom role (V1 API). V1 only. Updates only the fields provided "
        "(name, display_name, description). Does NOT update permissions -- use update_role_v1 "
        "for full replacement including permissions. System roles cannot be modified. "
        "Required: role_uuid. At least one of: name, display_name, description. "
        "Example: patch_role_v1(role_uuid='<uuid>', display_name='New Display Name') "
        "Returns: the updated role object. "
        "Calls: PATCH /api/v1/roles/{uuid}/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V1,
)
def patch_role_v1(
    request: HttpRequest,
    *,
    role_uuid: str,
    name: str = "",
    display_name: str = "",
    description: str = "",
) -> str:
    """Patch a V1 role by delegating to RoleViewSet."""
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if display_name:
        body["display_name"] = display_name
    if description:
        body["description"] = description

    if not body:
        return json.dumps({"error": "At least one of name, display_name, or description is required"})

    path = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
    return _call_view_write(request, _role_v1_patch_view, path, body, method="PATCH", uuid=role_uuid)


@register_tool(
    description=(
        "Update a custom role (V2 API, full replacement). V2 only -- requires workspace-enabled organization. "
        "Replaces the entire role including permissions. "
        "Required: role_uuid, name, permissions (list of permission objects). "
        "Each permission needs: application, resource_type, operation. "
        "Example: update_role(role_uuid='<uuid>', name='Cost Reader', "
        "permissions=[{'application': 'cost-management', 'resource_type': 'cost_model', 'operation': 'read'}]) "
        "Returns: the updated role object. "
        "Calls: PUT /api/v2/roles/{uuid}/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def update_role(
    request: HttpRequest,
    *,
    role_uuid: str,
    name: str,
    description: str = "",
    permissions: list[dict[str, str]],
) -> str:
    """Update a V2 role by delegating to RoleV2ViewSet."""
    body: dict[str, Any] = {"name": name, "permissions": permissions}
    if description:
        body["description"] = description

    path = reverse("v2_management:roles-detail", kwargs={"uuid": role_uuid})
    return _call_view_write(request, _role_v2_update_view, path, body, method="PUT", uuid=role_uuid)


@register_tool(
    description=(
        "Update role bindings for a specific subject on a resource (V2 API). V2 only. "
        "Sets the exact list of roles for the given subject on the resource -- any existing "
        "bindings not in the list are removed. "
        "Required: resource_id, subject_id, subject_type ('principal' or 'group'), "
        "roles (list of objects with 'id' key). "
        "Optional: resource_type (default 'workspace'). "
        "Example: update_role_binding(resource_id='<ws-uuid>', subject_id='<user-uuid>', "
        "subject_type='principal', roles=[{'id': '<role-uuid>'}]) "
        "Returns: the updated role binding state. "
        "Calls: PUT /api/v2/role-bindings/by-subject/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def update_role_binding(
    request: HttpRequest,
    *,
    resource_id: str,
    resource_type: str = "workspace",
    subject_id: str,
    subject_type: str,
    roles: list[dict[str, str]],
) -> str:
    """Update role bindings by subject by delegating to RoleBindingViewSet.by_subject."""
    path = reverse("v2_management:role-bindings-by-subject")
    query_params = {
        "resource_id": resource_id,
        "resource_type": resource_type,
        "subject_id": subject_id,
        "subject_type": subject_type,
    }
    body: dict[str, Any] = {"roles": roles}
    return _call_view_json(
        request, _role_binding_update_view, path, method="PUT", body=body, query_params=query_params
    )


@register_tool(
    description=(
        "Update a workspace (V2 API, full replacement). V2 only. "
        "Required: workspace_id, name. Optional: description, parent_id (required for "
        "standard workspaces). Root and ungrouped-hosts workspaces cannot be modified. "
        "Example: update_workspace(workspace_id='<uuid>', name='EMEA Engineering', "
        "description='Updated', parent_id='<parent-uuid>') "
        "Returns: the updated workspace object. "
        "Calls: PUT /api/v2/workspaces/{uuid}/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def update_workspace(
    request: HttpRequest,
    *,
    workspace_id: str,
    name: str,
    description: str = "",
    parent_id: str = "",
) -> str:
    """Update a workspace by delegating to WorkspaceViewSet."""
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if parent_id:
        body["parent_id"] = parent_id

    path = reverse("v2_management:workspace-detail", kwargs={"pk": workspace_id})
    return _call_view_write(request, _workspace_update_view, path, body, method="PUT", pk=workspace_id)


@register_tool(
    description=(
        "Move a workspace to a new parent (V2 API). V2 only. Changes the parent of a workspace "
        "in the hierarchy. Root and ungrouped-hosts workspaces cannot be moved. "
        "Required: workspace_id, parent_id (UUID of the new parent workspace). "
        "Example: move_workspace(workspace_id='<uuid>', parent_id='<new-parent-uuid>') "
        "Returns: the moved workspace object. "
        "Calls: POST /api/v2/workspaces/{uuid}/move/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def move_workspace(
    request: HttpRequest,
    *,
    workspace_id: str,
    parent_id: str,
) -> str:
    """Move a workspace by delegating to WorkspaceViewSet.move."""
    body: dict[str, Any] = {"parent_id": parent_id}
    path = reverse("v2_management:workspace-move", kwargs={"pk": workspace_id})
    return _call_view_write(request, _workspace_move_view, path, body, pk=workspace_id)


@register_tool(
    description=(
        "Update a cross-account access request (full replacement). "
        "Required: request_id, target_org (org ID), start_date (MM/DD/YYYY), "
        "end_date (MM/DD/YYYY), roles (list of role display name strings, NOT UUIDs). "
        "Example: update_cross_account_request(request_id='<uuid>', target_org='12345', "
        "start_date='06/01/2026', end_date='06/30/2026', roles=['Vulnerability administrator']) "
        "Returns: the updated request object. "
        "Calls: PUT /api/v1/cross-account-requests/{id}/"
    ),
    requires_auth=True,
    write=True,
)
def update_cross_account_request(
    request: HttpRequest,
    *,
    request_id: str,
    target_org: str,
    start_date: str,
    end_date: str,
    roles: list[str],
) -> str:
    """Update a cross-account request by delegating to CrossAccountRequestViewSet."""
    body: dict[str, Any] = {
        "target_org": target_org,
        "start_date": start_date,
        "end_date": end_date,
        "roles": roles,
    }

    path = reverse("v1_api:cross-detail", kwargs={"pk": request_id})
    return _call_view_write(request, _cross_account_update_view, path, body, method="PUT", pk=request_id)


@register_tool(
    description=(
        "Partially update a cross-account access request (status change). "
        "Used to approve, deny, or cancel a request. "
        "Required: request_id, status (one of: pending, approved, denied, cancelled, expired). "
        "Example: patch_cross_account_request(request_id='<uuid>', status='approved') "
        "Returns: the updated request object. "
        "Calls: PATCH /api/v1/cross-account-requests/{id}/"
    ),
    requires_auth=True,
    write=True,
)
def patch_cross_account_request(
    request: HttpRequest,
    *,
    request_id: str,
    status: str,
) -> str:
    """Patch a cross-account request status."""
    allowed_statuses = {"pending", "approved", "denied", "cancelled", "expired"}
    if status not in allowed_statuses:
        return json.dumps({"error": f"Invalid status '{status}'. Must be one of: {sorted(allowed_statuses)}"})

    body: dict[str, Any] = {"status": status}

    path = reverse("v1_api:cross-detail", kwargs={"pk": request_id})
    return _call_view_write(request, _cross_account_patch_view, path, body, method="PATCH", pk=request_id)


# --- DELETE tool implementations ---

# ┌──────────────────────────────────────┬───────────┬─────────────────────────────────────────────────────┐
# │ MCP Tool                             │ Gating    │ API Endpoint                                        │
# ├──────────────────────────────────────┼───────────┼─────────────────────────────────────────────────────┤
# │ delete_group                         │ both      │ DELETE /api/v1/groups/{uuid}/                       │
# │ remove_principals_from_group         │ both      │ DELETE /api/v1/groups/{uuid}/principals/            │
# │ remove_roles_from_group              │ v1        │ DELETE /api/v1/groups/{uuid}/roles/                 │
# │ delete_role_v1                       │ v1        │ DELETE /api/v1/roles/{uuid}/                        │
# │ bulk_delete_roles                    │ v2        │ POST /api/v2/roles/:batchDelete                    │
# │ delete_workspace                     │ v2        │ DELETE /api/v2/workspaces/{uuid}/                   │
# └──────────────────────────────────────┴───────────┴─────────────────────────────────────────────────────┘


@register_tool(
    description=(
        "DESTRUCTIVE: Permanently delete a custom group. This operation is IRREVERSIBLE. "
        "All role assignments and principal memberships in the group are removed. "
        "Provide either group_uuid OR group_name (group_uuid takes precedence). "
        "System groups (platform_default, admin_default) cannot be deleted. "
        "Works for both V1 and V2 organizations. "
        "Example: delete_group(group_name='Old Team') "
        "Returns: {status: 'deleted'}. "
        "Calls: DELETE /api/v1/groups/{uuid}/"
    ),
    requires_auth=True,
    write=True,
)
def delete_group(
    request: HttpRequest,
    *,
    group_uuid: str = "",
    group_name: str = "",
) -> str:
    """Delete a group by delegating to GroupViewSet."""
    resolved_uuid, error = _resolve_group_for_tool(request, group_uuid, group_name)
    if error:
        return error
    assert resolved_uuid is not None

    path = reverse("v1_management:group-detail", kwargs={"uuid": resolved_uuid})
    return _call_view_delete(request, _group_delete_view, path, uuid=resolved_uuid)


@register_tool(
    description=(
        "DESTRUCTIVE: Remove one or more principals (users) from a group. This is IRREVERSIBLE -- "
        "re-adding requires a separate call. Provide either group_uuid OR group_name "
        "(group_uuid takes precedence). At least one of usernames or service_accounts is required. "
        "usernames: comma-separated list of usernames. "
        "service_accounts: comma-separated list of service account client IDs. "
        "Works for both V1 and V2 organizations. "
        "Example: remove_principals_from_group(group_name='Engineering', usernames='jdoe,jsmith') "
        "Returns: {status: 'deleted'}. "
        "Calls: DELETE /api/v1/groups/{uuid}/principals/"
    ),
    requires_auth=True,
    write=True,
)
def remove_principals_from_group(
    request: HttpRequest,
    *,
    group_uuid: str = "",
    group_name: str = "",
    usernames: str = "",
    service_accounts: str = "",
) -> str:
    """Remove principals from a group by delegating to GroupViewSet.principals."""
    if not usernames and not service_accounts:
        return json.dumps({"error": "At least one of usernames or service_accounts is required"})

    resolved_uuid, error = _resolve_group_for_tool(request, group_uuid, group_name)
    if error:
        return error
    assert resolved_uuid is not None

    query_params: dict[str, str] = {}
    if usernames:
        query_params["usernames"] = usernames
    if service_accounts:
        query_params["service-accounts"] = service_accounts

    path = reverse("v1_management:group-principals", kwargs={"uuid": resolved_uuid})
    return _call_view_delete(request, _group_principals_delete_view, path, query_params, uuid=resolved_uuid)


@register_tool(
    description=(
        "DESTRUCTIVE: Remove one or more roles from a group. This is IRREVERSIBLE -- "
        "the role-group association is deleted (the role itself is NOT deleted). "
        "V1 only -- blocked for V2 organizations (use update_role_binding instead). "
        "Provide either group_uuid OR group_name (group_uuid takes precedence). "
        "Required: roles (comma-separated string of role UUIDs). "
        "Example: remove_roles_from_group(group_name='Engineering', roles='uuid-1,uuid-2') "
        "Returns: {status: 'deleted'}. "
        "Calls: DELETE /api/v1/groups/{uuid}/roles/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V1,
)
def remove_roles_from_group(
    request: HttpRequest,
    *,
    group_uuid: str = "",
    group_name: str = "",
    roles: str,
) -> str:
    """Remove roles from a group by delegating to GroupViewSet.roles."""
    resolved_uuid, error = _resolve_group_for_tool(request, group_uuid, group_name)
    if error:
        return error
    assert resolved_uuid is not None

    query_params: dict[str, str] = {"roles": roles}
    path = reverse("v1_management:group-roles", kwargs={"uuid": resolved_uuid})
    return _call_view_delete(request, _group_roles_delete_view, path, query_params, uuid=resolved_uuid)


@register_tool(
    description=(
        "DESTRUCTIVE: Permanently delete a custom role (V1 API). This operation is IRREVERSIBLE. "
        "All permissions and group assignments for this role are removed. "
        "V1 only -- blocked for V2 organizations (use bulk_delete_roles for V2). "
        "System roles cannot be deleted. "
        "Required: role_uuid. "
        "Example: delete_role_v1(role_uuid='<uuid>') "
        "Returns: {status: 'deleted'}. "
        "Calls: DELETE /api/v1/roles/{uuid}/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V1,
)
def delete_role_v1(
    request: HttpRequest,
    *,
    role_uuid: str,
) -> str:
    """Delete a V1 role by delegating to RoleViewSet."""
    path = reverse("v1_management:role-detail", kwargs={"uuid": role_uuid})
    return _call_view_delete(request, _role_v1_delete_view, path, uuid=role_uuid)


@register_tool(
    description=(
        "DESTRUCTIVE: Permanently delete one or more roles in a single atomic operation (V2 API). "
        "This operation is IRREVERSIBLE. All role bindings referencing the deleted roles are removed. "
        "V2 only -- requires workspace-enabled organization. "
        "Atomic: if any UUID is not found, the entire operation fails and no roles are deleted. "
        "Required: ids (list of role UUID strings). "
        "Example: bulk_delete_roles(ids=['<uuid-1>', '<uuid-2>']) "
        "Returns: {status: 'deleted'}. "
        "Calls: POST /api/v2/roles/:batchDelete"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def bulk_delete_roles(
    request: HttpRequest,
    *,
    ids: list[str],
) -> str:
    """Bulk-delete V2 roles by delegating to RoleV2ViewSet.bulk_destroy."""
    body: dict[str, Any] = {"ids": ids}
    path = reverse("v2_management:roles-bulk-destroy")
    return _call_view_write(request, _role_v2_bulk_delete_view, path, body)


@register_tool(
    description=(
        "DESTRUCTIVE: Permanently delete a workspace (V2 API). This operation is IRREVERSIBLE. "
        "All role bindings scoped to this workspace are removed. "
        "V2 only -- requires workspace-enabled organization. "
        "Only STANDARD workspaces can be deleted -- root and ungrouped-hosts workspaces are protected. "
        "Cannot delete a workspace that has children -- move or delete children first. "
        "Required: workspace_uuid. "
        "Example: delete_workspace(workspace_uuid='<uuid>') "
        "Returns: {status: 'deleted'}. "
        "Calls: DELETE /api/v2/workspaces/{uuid}/"
    ),
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V2,
)
def delete_workspace(
    request: HttpRequest,
    *,
    workspace_uuid: str,
) -> str:
    """Delete a workspace by delegating to WorkspaceViewSet."""
    path = reverse("v2_management:workspace-detail", kwargs={"pk": workspace_uuid})
    return _call_view_delete(request, _workspace_delete_view, path, pk=workspace_uuid)


# --- JSON-RPC parsing ---


class JsonRpcError(Exception):
    """JSON-RPC validation error raised during request parsing."""

    def __init__(self, request_id: Any | None, code: int, message: str) -> None:
        """Initialize with JSON-RPC error fields."""
        self.request_id = request_id
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class JsonRpcRequest:
    """Parsed JSON-RPC 2.0 request."""

    request_id: Any | None
    method: str
    params: dict[str, Any]


def _parse_jsonrpc(body: bytes) -> JsonRpcRequest:
    """Parse and validate a JSON-RPC 2.0 request body.

    Returns a JsonRpcRequest on success. Raises JsonRpcError on validation failure.
    Notifications (no id) return a JsonRpcRequest with request_id=None.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise JsonRpcError(None, -32700, "Parse error")

    if isinstance(payload, list):
        raise JsonRpcError(None, -32600, "Invalid Request: batch requests are not supported")

    if not isinstance(payload, dict):
        raise JsonRpcError(None, -32600, "Invalid Request: expected a JSON object")

    req_id = payload.get("id")

    if payload.get("jsonrpc") != "2.0":
        raise JsonRpcError(req_id, -32600, "Invalid Request: jsonrpc must be '2.0'")

    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(req_id, -32600, "Invalid Request: method must be a non-empty string")

    raw_params = payload.get("params") or {}
    if req_id is not None and not isinstance(raw_params, dict):
        raise JsonRpcError(req_id, -32600, "Invalid Request: params must be an object")

    return JsonRpcRequest(
        request_id=req_id,
        method=method,
        params=raw_params if isinstance(raw_params, dict) else {},
    )


# --- Django View implementing MCP StreamableHTTP transport ---


@method_decorator(csrf_exempt, name="dispatch")
class MCPView(View):
    """WSGI-compatible MCP endpoint implementing StreamableHTTP transport.

    Handles the MCP JSON-RPC protocol over HTTP POST, using the Anthropic MCP
    Python SDK (mcp) for tool registration and schema generation.

    Authentication is handled by Django's IdentityHeaderMiddleware, the same
    way as the regular /api/v1/principals/ endpoint.
    """

    http_method_names = ["post", "get", "delete"]

    def post(self, request: HttpRequest) -> HttpResponse:
        """Handle MCP JSON-RPC requests via HTTP POST."""
        org_id = getattr(getattr(request, "user", None), "org_id", None)
        req_id = getattr(request, "req_id", "unknown")

        try:
            rpc_req = _parse_jsonrpc(request.body)
        except JsonRpcError as exc:
            logger.warning(
                "mcp: parse error, org_id=%s, req_id=%s, code=%s, msg='%s'",
                org_id,
                req_id,
                exc.code,
                exc.message,
            )
            return _error_response(exc.request_id, exc.code, exc.message)

        logger.info("mcp: method=%s, org_id=%s, req_id=%s", rpc_req.method, org_id, req_id)

        if rpc_req.request_id is None:
            return HttpResponse(status=202, content_type="application/json")

        if rpc_req.method == "initialize":
            return _handle_initialize(request, rpc_req.request_id, rpc_req.params)
        if rpc_req.method == "tools/list":
            return _handle_tools_list(request, rpc_req.request_id, rpc_req.params)
        if rpc_req.method == "tools/call":
            return _handle_tools_call(request, rpc_req.request_id, rpc_req.params)

        logger.warning("mcp: unknown method=%s, org_id=%s, req_id=%s", rpc_req.method, org_id, req_id)
        return _error_response(rpc_req.request_id, -32601, f"Method not found: {rpc_req.method}")

    def get(self, request: HttpRequest) -> HttpResponse:
        """SSE streaming is not supported in WSGI mode."""
        return HttpResponse("SSE streaming not supported in WSGI mode", status=405, content_type="text/plain")

    def delete(self, request: HttpRequest) -> HttpResponse:
        """Handle MCP session termination."""
        return HttpResponse(status=200, content_type="application/json")


# --- JSON-RPC method handlers ---


_MCP_INSTRUCTIONS_BASE = (
    "You are an RBAC (Role-Based Access Control) assistant for console.redhat.com. "
    "Use the available tools to investigate user permissions, group memberships, "
    "roles, audit logs, and cross-account access."
)

_MCP_INSTRUCTIONS_HONEST_CAVEATS = (
    "\n\n## Honest Caveats\n\n"
    "After answering any question, proactively surface what the API response does NOT cover. "
    "If the user's question requires data from multiple tools, explain which additional calls "
    "are needed rather than presenting partial data as complete.\n\n"
    "Cross-cutting limitations to keep in mind:\n\n"
    "1. **Permission-to-UI mapping does not exist.** RBAC defines permission strings "
    "(e.g., 'cost-management:cost_model:write') but the consuming application determines "
    "what UI elements or API endpoints each permission unlocks. Permission names are naming "
    "conventions only -- RBAC cannot confirm what they control.\n\n"
    "2. **ResourceDefinition filters are opaque.** The 'resourceDefinitions' array on access "
    "entries scopes permissions to specific resources, but RBAC only stores the filter -- the "
    "consuming application enforces it. RBAC cannot tell you which concrete resources a filter "
    "matches.\n\n"
    "3. **Org admins have implicit full access.** Org admins bypass all RBAC checks. Tools "
    "like list_access return only explicitly assigned permissions, not the effective (unlimited) "
    "access org admins actually have. When a user is an org admin, clarify that their effective "
    "access is unrestricted regardless of what the API returns.\n\n"
    "4. **V1 vs V2 role assignment model.** In V1, roles are assigned to groups and users are "
    "added to those groups -- roles cannot be assigned directly to users. In V2, roles are "
    "bound to subjects via role bindings, which can target individual users directly. When "
    "advising on role assignment, check the org's API version (org_version field) to give "
    "accurate guidance."
)

_MCP_INSTRUCTIONS_REMEDIATION = (
    "\n\n## Remediation Guidance\n\n"
    "After completing a readonly analysis that reveals a problem (permission gap, stale membership, "
    "unauthorized change, etc.), always suggest concrete next steps the user or an admin can take "
    "to resolve it. Present remediation as numbered options:\n\n"
    '"To fix this you could: (1) <action>, (2) <action>, or (3) <action>."\n\n'
    "Scenario-specific guidance:\n"
    "- **Permission gaps**: suggest (a) adding the user to an existing group that already has "
    "the right role, (b) creating a narrow custom role with just the needed permissions, or "
    "(c) adding the missing role to the user's current group.\n"
    "- **Group dissolution**: suggest immediate deletion, a transition group for stranded members, "
    "or partial cleanup.\n"
    "- **Audit investigations**: suggest removing unauthorized changes, revoking the actor's access, "
    "or both.\n"
    "- **Offboarding**: suggest removing the user from groups, generating a report, or both.\n"
    "- **Cross-account access**: suggest updating or cancelling requests, or generating a briefing report.\n"
    "- **Review-only scenarios** (e.g., summarizing recent changes): do NOT suggest write actions. "
    "Present the summary and let the user ask follow-up questions.\n\n"
    "Always include a 'do nothing / audit first' option when the action is irreversible."
)

_MCP_INSTRUCTIONS_WRITE_ACTIONS = (
    "\n\n## Write Actions\n\n"
    "Write tools are enabled. When presenting remediation options, offer to execute them directly. "
    "Format as: \"Want me to do this now? Reply 1, 2, or 3 -- or 'no'.\"\n\n"
    "NEVER execute a write tool without the user explicitly selecting an option."
)

_MCP_INSTRUCTIONS_CONFIRMATION_PROTOCOL = (
    "\n\n## Write Confirmation Protocol\n\n"
    "All write operations (create, update, delete) require two-phase confirmation:\n\n"
    "1. **First call**: Call the write tool with the desired arguments. The server returns a "
    "confirmation message describing the action and a confirmation_token.\n"
    "2. **Present to user**: Show the confirmation message to the user and ask for approval.\n"
    "3. **Second call**: If the user approves, call the same tool again with the exact same "
    'arguments plus confirmation_token="<token>".\n\n'
    "Important:\n"
    "- NEVER skip the confirmation step or auto-confirm on behalf of the user.\n"
    "- If the user declines, do NOT call the tool again.\n"
    "- Tokens expire after {ttl_display} -- if expired, repeat the first call to get a new token.\n"
    "- The arguments in the second call must exactly match the first call."
)

_TOOL_DESCRIPTION_CONFIRMATION_SUFFIX = (
    "\n\nWrite Confirmation: This tool requires user approval. "
    "The first call returns a preview and confirmation_token. "
    "Present the preview to the user. If approved, call again with the same arguments "
    "plus confirmation_token. NEVER auto-confirm."
)


def _build_mcp_instructions() -> str:
    """Build MCP server instructions based on current feature flags."""
    parts = [_MCP_INSTRUCTIONS_BASE, _MCP_INSTRUCTIONS_HONEST_CAVEATS, _MCP_INSTRUCTIONS_REMEDIATION]
    if _is_write_enabled():
        parts.append(_MCP_INSTRUCTIONS_WRITE_ACTIONS)
        if _is_write_confirmation_enabled():
            ttl = getattr(settings, "MCP_WRITE_CONFIRMATION_TTL", _CONFIRM_TTL_DEFAULT)
            ttl_minutes = max(1, ttl // 60)
            ttl_display = f"{ttl_minutes} minute{'s' if ttl_minutes != 1 else ''}"
            parts.append(_MCP_INSTRUCTIONS_CONFIRMATION_PROTOCOL.format(ttl_display=ttl_display))
    return "".join(parts)


def _handle_initialize(request: HttpRequest, request_id: Any, params: dict[str, Any]) -> JsonResponse:
    """Handle MCP initialize request."""
    client_info = params.get("clientInfo", {})
    logger.info("mcp: initialize, client=%s/%s", client_info.get("name", "unknown"), client_info.get("version", "?"))
    result = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name": mcp.name,
            "version": "1.0.0",
        },
        "instructions": _build_mcp_instructions(),
    }
    response = _success_response(request_id, result)
    response["Mcp-Session-Id"] = str(uuid.uuid4())
    return response


@lru_cache(maxsize=1)
def _get_tools() -> list[Any]:
    """Resolve and cache tool metadata from FastMCP on first call.

    Tools are static (listChanged: False). Lazy initialization avoids
    import-time side effects. Runs in WSGI context where no event loop
    is active; ASGI would require a different approach.
    """
    return asyncio.run(mcp.list_tools())


@lru_cache(maxsize=1)
def _get_tool_schemas() -> dict[str, dict[str, Any]]:
    """Build a {tool_name: inputSchema} lookup from the cached tool list."""
    return {tool.name: tool.inputSchema for tool in _get_tools()}


def _sanitize_validation_error(error: jsonschema.ValidationError) -> str:
    """Build a human-readable validation message without exposing internals."""
    path = ".".join(str(p) for p in error.absolute_path) if error.absolute_path else ""
    if error.validator == "type":
        expected = error.schema.get("type", "unknown")
        actual = type(error.instance).__name__
        field = f"'{path}'" if path else "input"
        return f"Expected {field} to be {expected}, got {actual}"
    if error.validator == "required":
        if isinstance(error.instance, dict) and error.validator_value:
            missing = [p for p in error.validator_value if p not in error.instance]
            if missing:
                return f"Missing required argument: {missing[0]}"
        match = re.match(r"'(.+)' is a required property", error.message)
        field_name = match.group(1) if match else error.message
        return f"Missing required argument: {field_name}"
    if error.validator == "additionalProperties":
        names = re.findall(r"'([^']+)'", error.message)
        if names:
            if len(names) == 1:
                return f"Unknown argument: {names[0]}"
            return f"Unknown arguments: {', '.join(names)}"
        return "Unknown argument provided"
    if path:
        return f"Validation failed for '{path}': {error.message}"
    return f"Validation failed: {error.message}"


def _validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Validate arguments against the tool's JSON schema.

    Injects ``additionalProperties: false`` so unknown arguments are
    rejected even though FastMCP-generated schemas omit that keyword.

    Returns None on success, or a sanitized error message on failure.
    """
    try:
        schemas = _get_tool_schemas()
    except Exception:
        return None
    schema = schemas.get(tool_name)
    if schema is None:
        return None
    strict_schema = dict(schema)
    strict_schema.setdefault("additionalProperties", False)
    try:
        jsonschema.validate(instance=arguments, schema=strict_schema)
    except jsonschema.ValidationError as exc:
        return _sanitize_validation_error(exc)
    return None


def _is_v2_available() -> bool:
    """Check whether V2 API routes are mounted."""
    return getattr(settings, "V2_APIS_ENABLED", False)


def _is_write_enabled() -> bool:
    """Check whether MCP write tools are enabled."""
    return getattr(settings, "MCP_WRITE_ENABLED", False)


def _is_write_confirmation_enabled() -> bool:
    """Check whether write tools require user confirmation before executing."""
    return getattr(settings, "MCP_WRITE_CONFIRMATION", True)


def _is_pii_redaction_enabled() -> bool:
    """Check whether PII is redacted from MCP tool responses."""
    return getattr(settings, "MCP_PII_REDACTION_ENABLED", True)


# --- Write confirmation token system ---

_CONFIRM_CACHE_PREFIX = "mcp:confirm:"
_CONFIRM_TTL_DEFAULT = 300


def _hash_arguments(tool_name: str, arguments: dict[str, Any]) -> str:
    """Create a stable hash of tool name + arguments for token validation."""
    payload = json.dumps({"tool": tool_name, "args": arguments}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _generate_confirmation_token(tool_name: str, arguments: dict[str, Any], org_id: str | None) -> str:
    """Generate a single-use confirmation token and store it in the Django cache."""
    token = uuid.uuid4().hex
    args_hash = _hash_arguments(tool_name, arguments)
    ttl = getattr(settings, "MCP_WRITE_CONFIRMATION_TTL", _CONFIRM_TTL_DEFAULT)
    value = {"tool": tool_name, "args_hash": args_hash, "org_id": org_id or ""}
    django_cache.set(f"{_CONFIRM_CACHE_PREFIX}{token}", value, timeout=ttl)
    return token


def _validate_confirmation_token(
    token: str, tool_name: str, arguments: dict[str, Any], org_id: str | None
) -> tuple[bool, str]:
    """Validate and consume a confirmation token. Returns (valid, error_message)."""
    key = f"{_CONFIRM_CACHE_PREFIX}{token}"
    stored = django_cache.get(key)
    if not stored:
        return False, "Confirmation token is invalid or expired. Please retry the operation."

    if stored["tool"] != tool_name:
        return False, f"Token was issued for '{stored['tool']}', not '{tool_name}'."
    expected_hash = _hash_arguments(tool_name, arguments)
    if stored["args_hash"] != expected_hash:
        return False, "Arguments have changed since the confirmation was issued. Please retry."
    if stored["org_id"] != (org_id or ""):
        return False, "Token was issued for a different organization."

    django_cache.delete(key)
    return True, ""


def _generate_write_preview(tool_name: str, arguments: dict[str, Any]) -> str:
    """Generate a human-readable one-liner summary of a write operation."""
    name = arguments.get("name", "")
    group_name = arguments.get("group_name", "")
    group_uuid = arguments.get("group_uuid", "")
    group_ref = group_name or group_uuid or ""
    role_uuid = arguments.get("role_uuid", "")
    workspace_id = arguments.get("workspace_id", arguments.get("workspace_uuid", ""))

    if tool_name == "create_group":
        return f"This will create a new group '{name}'."
    if tool_name == "add_principals_to_group":
        principals = arguments.get("principals") or []
        return f"This will add {len(principals)} principal(s) to group '{group_ref}'."
    if tool_name == "add_roles_to_group":
        roles = arguments.get("roles") or []
        return f"This will assign {len(roles)} role(s) to group '{group_ref}'."
    if tool_name in ("create_role_v1", "create_role"):
        return f"This will create a new role '{name}'."
    if tool_name == "create_role_bindings":
        bindings = arguments.get("bindings") or []
        return f"This will create {len(bindings)} role binding(s)."
    if tool_name == "create_workspace":
        return f"This will create a new workspace '{name}'."
    if tool_name == "create_cross_account_request":
        target = arguments.get("target_org", "")
        return f"This will create a cross-account access request to org '{target}'."
    if tool_name == "update_group":
        return f"This will update group '{group_ref}' (name -> '{name}')."
    if tool_name in ("update_role_v1", "update_role"):
        return f"This will replace role '{role_uuid}' with new definition (name: '{name}')."
    if tool_name == "patch_role_v1":
        return f"This will partially update role '{role_uuid}'."
    if tool_name == "update_role_binding":
        subject = arguments.get("subject_id", "")
        return f"This will replace role bindings for subject '{subject}'."
    if tool_name in ("update_workspace", "move_workspace"):
        action = "update" if tool_name == "update_workspace" else "move"
        return f"This will {action} workspace '{workspace_id}'."
    if tool_name in ("update_cross_account_request", "patch_cross_account_request"):
        req_id = arguments.get("request_id", "")
        return f"This will update cross-account request '{req_id}'."
    if tool_name == "delete_group":
        return f"DESTRUCTIVE: This will permanently delete group '{group_ref}'. This is irreversible."
    if tool_name == "remove_principals_from_group":
        users = arguments.get("usernames", "")
        svc = arguments.get("service_accounts", "")
        who = users or svc or "specified principals"
        return f"DESTRUCTIVE: This will remove {who} from group '{group_ref}'. This is irreversible."
    if tool_name == "remove_roles_from_group":
        return f"DESTRUCTIVE: This will remove role(s) from group '{group_ref}'. This is irreversible."
    if tool_name == "delete_role_v1":
        return f"DESTRUCTIVE: This will permanently delete role '{role_uuid}'. This is irreversible."
    if tool_name == "bulk_delete_roles":
        ids = arguments.get("ids") or []
        return f"DESTRUCTIVE: This will permanently delete {len(ids)} role(s). This is irreversible."
    if tool_name == "delete_workspace":
        ws = arguments.get("workspace_uuid", "")
        return f"DESTRUCTIVE: This will permanently delete workspace '{ws}'. This is irreversible."

    return f"This will execute write operation '{tool_name}'."


def _handle_tools_list(request: HttpRequest, request_id: Any, params: dict[str, Any]) -> JsonResponse:
    """Handle MCP tools/list request using FastMCP's registered tools."""
    v2_available = _is_v2_available()
    write_enabled = _is_write_enabled()
    overrides = _get_all_description_overrides()
    tools_data = []
    for tool in _get_tools():
        config = _TOOL_CONFIG.get(tool.name, ToolConfig(fn=lambda: ""))
        if not v2_available and config.api_version == ApiVersion.V2:
            continue
        description = overrides.get(tool.name, tool.description or "")
        if config.write and not write_enabled:
            description = f"[DISABLED -- write mode off] {description}"
        elif config.write and write_enabled and _is_write_confirmation_enabled():
            description = f"{description}{_TOOL_DESCRIPTION_CONFIRMATION_SUFFIX}"
        tools_data.append(
            {
                "name": tool.name,
                "description": description,
                "inputSchema": tool.inputSchema,
            }
        )
    return _success_response(request_id, {"tools": tools_data})


def _normalize_tool_result(result: Any) -> str:
    """Normalize tool return values into a string suitable for MCP text content."""
    if isinstance(result, str):
        return result
    return json.dumps(result, default=str)


class ToolTimeoutError(Exception):
    """Raised when a tool exceeds its execution timeout.

    Distinct from the built-in TimeoutError so that _handle_tools_call can
    distinguish infrastructure timeouts from TimeoutError raised by the
    tool itself (e.g. from ``requests`` or other libraries).
    """


_MCP_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=settings.MCP_TOOL_MAX_WORKERS,
    thread_name_prefix="mcp-tool",
)


def _execute_with_timeout(fn: Callable[..., Any], timeout: int, *args: Any, **kwargs: Any) -> Any:
    """Execute a tool function with a timeout.

    Uses the module-level ThreadPoolExecutor so threads are reused across
    calls instead of creating a new executor per request.

    Raises ToolTimeoutError if the function does not complete within the
    given timeout (seconds).
    """
    future = _MCP_TOOL_EXECUTOR.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise ToolTimeoutError(f"Tool execution exceeded {timeout}s timeout")


def _check_v1_access(request: HttpRequest, v1_permission: tuple[str, str]) -> bool:
    """Check v1 RBAC permission for the requesting user.

    Args:
        request: The Django HTTP request with user context populated by middleware.
        v1_permission: A (resource_type, verb) tuple, e.g. ("role", "read").
            Use ("admin", "only") to require org admin with no access-dict fallback.

    Returns True if access is granted, False otherwise.
    """
    user = getattr(request, "user", None)
    if not user:
        return False

    if getattr(user, "admin", False):
        return True

    resource_type, verb = v1_permission
    if resource_type == "admin":
        return False

    access = getattr(user, "access", {})
    return bool(access.get(resource_type, {}).get(verb, []))


def _check_kessel_access(request: HttpRequest, resource_type: str, relation: str) -> bool:
    """Check Kessel Inventory API permission for the requesting user.

    Returns True if access is granted, False otherwise. Fails closed on
    missing tenant, missing principal ID, or Inventory API errors.
    """
    try:
        tenant = getattr(request, "tenant", None)
        if not tenant:
            return False

        resource_id = tenant.tenant_resource_id()
        if not resource_id:
            return False

        principal_id = get_kessel_principal_id(request)
        if not principal_id:
            return False

        return WorkspaceInventoryAccessChecker().check_resource_access(
            resource_type=resource_type,
            resource_id=resource_id,
            principal_id=principal_id,
            relation=relation,
        )
    except Exception:
        logger.exception("mcp: Kessel access check failed unexpectedly")
        return False


def _handle_tools_call(request: HttpRequest, request_id: Any, params: dict[str, Any]) -> JsonResponse:
    """Handle MCP tools/call request.

    Calls tool functions directly in the sync WSGI context (not through
    FastMCP's async call_tool) to avoid Django's SynchronousOnlyOperation
    error when tools access the ORM.

    Tools that need auth context receive the Django request as the first
    argument, so no thread-local state is needed.

    Tool execution is wrapped in a configurable timeout (MCP_TOOL_TIMEOUT_SECONDS,
    default 30s) using a ThreadPoolExecutor. On timeout, a JSON-RPC internal
    error (-32603) is returned and a Prometheus metric with status="timeout"
    is recorded.
    """
    tool_name: str = params.get("name", "")
    if "arguments" not in params:
        return _error_response(request_id, -32602, "Missing required field: arguments")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return _error_response(request_id, -32602, "Invalid params: arguments must be an object")

    config = _TOOL_CONFIG.get(tool_name)
    if config is None:
        logger.warning("mcp: tools/call unknown tool='%s'", tool_name)
        return _error_response(request_id, -32602, f"Unknown tool: {tool_name}")

    if config.api_version == ApiVersion.V2 and not _is_v2_available():
        logger.warning("mcp: tools/call tool='%s' rejected, V2 APIs not enabled", tool_name)
        return _error_response(
            request_id,
            -32602,
            f"Tool '{tool_name}' requires V2 APIs, which are not enabled in this deployment.",
        )

    if config.write and not _is_write_enabled():
        logger.warning("mcp: tools/call tool='%s' rejected, write mode disabled", tool_name)
        return _error_response(
            request_id,
            -32602,
            f"Tool '{tool_name}' is a write operation. Write mode is disabled (MCP_WRITE_ENABLED=False).",
        )

    org_id = getattr(getattr(request, "user", None), "org_id", None)
    logger.info(
        "mcp: tools/call tool='%s', org_id=%s, args=%s",
        tool_name,
        org_id,
        list(arguments.keys()),
    )

    if config.requires_auth:
        if not org_id:
            logger.warning("mcp: tools/call tool='%s' rejected, no auth", tool_name)
            _record_metric(tool_name, "auth_error")
            return _error_response(request_id, -32000, "Authentication required")

    if config.write:
        request.mcp_source = True

    if config.required_relation:
        tenant = getattr(request, "tenant", None)
        if tenant and is_v2_write_activated(tenant):
            access_granted = _check_kessel_access(request, config.required_resource_type, config.required_relation)
        elif config.v1_permission:
            access_granted = _check_v1_access(request, config.v1_permission)
        else:
            access_granted = False
        if not access_granted:
            logger.warning(
                "mcp: tools/call tool='%s' rejected, permission denied (relation=%s, resource_type=%s)",
                tool_name,
                config.required_relation,
                config.required_resource_type,
            )
            _record_metric(tool_name, "permission_denied")
            return _error_response(request_id, -32003, "Permission denied")

    confirmation_token = arguments.pop("confirmation_token", None)

    validation_error = _validate_tool_arguments(tool_name, arguments)
    if validation_error:
        logger.warning("mcp: tools/call tool='%s' schema validation failed: %s", tool_name, validation_error)
        return _error_response(request_id, -32602, f"Invalid params for tool '{tool_name}': {validation_error}")

    if config.write and _is_write_confirmation_enabled():
        if confirmation_token:
            valid, error_msg = _validate_confirmation_token(confirmation_token, tool_name, arguments, org_id)
            if not valid:
                logger.warning("mcp: tools/call tool='%s' confirmation failed: %s", tool_name, error_msg)
                _record_metric(tool_name, "confirmation_failed")
                content = [{"type": "text", "text": json.dumps({"error": error_msg})}]
                return _success_response(request_id, {"content": content, "isError": True})
            logger.info("mcp: tools/call tool='%s' confirmed, executing", tool_name)
        else:
            token = _generate_confirmation_token(tool_name, arguments, org_id)
            preview = _generate_write_preview(tool_name, arguments)
            logger.info("mcp: tools/call tool='%s' awaiting confirmation, token issued", tool_name)
            _record_metric(tool_name, "confirmation_pending")
            content = [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"confirmation_required": True, "message": preview, "confirmation_token": token}
                    ),
                }
            ]
            return _success_response(request_id, {"content": content, "isError": False})

    track = tool_name != "hello"
    start = time.monotonic() if track else 0
    timeout = getattr(settings, "MCP_TOOL_TIMEOUT_SECONDS", 30)

    try:
        if timeout > 0:
            if config.passes_request:
                result = _execute_with_timeout(config.fn, timeout, request, **arguments)
            else:
                result = _execute_with_timeout(config.fn, timeout, **arguments)
        else:
            if config.passes_request:
                result = config.fn(request, **arguments)
            else:
                result = config.fn(**arguments)

        if track:
            duration = time.monotonic() - start
            _record_metric(tool_name, "success", duration)
            logger.info("mcp: tools/call tool='%s' completed in %.3fs", tool_name, duration)

        result_text = _normalize_tool_result(result)
        content = [{"type": "text", "text": result_text}]
        if config.caveats:
            content.append(
                {"type": "text", "text": f"IMPORTANT — INCLUDE THESE CAVEATS IN YOUR ANSWER:\n{config.caveats}"}
            )

        return _success_response(request_id, {"content": content, "isError": False})
    except ToolTimeoutError:
        duration = time.monotonic() - start if track else timeout
        if track:
            _record_metric(tool_name, "timeout", duration)
        logger.error("mcp: tools/call tool='%s' timed out after %ds", tool_name, timeout)
        return _error_response(request_id, -32603, f"Tool execution timed out after {timeout}s")
    except TypeError as exc:
        if track:
            _record_metric(tool_name, "invalid_params", time.monotonic() - start)
        return _error_response(request_id, -32602, f"Invalid params for tool '{tool_name}': {exc}")
    except Exception:
        if track:
            _record_metric(tool_name, "error", time.monotonic() - start)
        logger.exception("mcp: tools/call tool='%s' failed", tool_name)
        return _error_response(request_id, -32603, "Internal error executing tool")


def _record_metric(tool_name: str, status: str, duration: float | None = None) -> None:
    """Record prometheus metrics for a tool call."""
    mcp_tool_call_total.labels(tool=tool_name, status=status).inc()
    if duration is not None:
        mcp_tool_call_duration_seconds.labels(tool=tool_name).observe(duration)


# --- JSON-RPC response helpers ---


def _success_response(request_id: Any, result: dict[str, Any]) -> JsonResponse:
    """Create a JSON-RPC success response."""
    return JsonResponse({"jsonrpc": "2.0", "result": result, "id": request_id})


def _error_response(request_id: Any, code: int, message: str) -> JsonResponse:
    """Create a JSON-RPC error response."""
    return JsonResponse({"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": request_id})
