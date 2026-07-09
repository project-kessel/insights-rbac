#
# Copyright 2025 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Helper for determining workspace/tenant binding levels for permissions."""

import dataclasses
from enum import IntEnum
from typing import Iterable, Self

from django.conf import settings
from django.db.models import QuerySet
from management.models import Role, Workspace
from management.permission.model import PermissionValue
from migration_tool.models import V2boundresource

from api.models import Tenant


class Scope(IntEnum):
    """
    Permission scope levels, ordered from lowest to highest.

    This represents the possible default scopes for a permission:
    * DEFAULT, for the default workspace of a tenant.
    * ROOT, for the root workspace of a tenant.
    * TENANT, for the tenant itself.

    Later scopes are said to be "higher" than earlier scopes, as they encompass more resources.
    """

    DEFAULT = 1
    ROOT = 2
    TENANT = 3


@dataclasses.dataclass(frozen=True)
class TenantScopeResources:
    """Contains the V2 resources to which default role bindings are bound for a given tenant."""

    tenant: V2boundresource
    root_workspace: V2boundresource
    default_workspace: V2boundresource

    @classmethod
    def for_models(cls, tenant: Tenant, root_workspace: Workspace, default_workspace: Workspace) -> Self:
        """Create a new instance with resources for the provided models."""
        return cls(
            tenant=V2boundresource.for_model(tenant),
            root_workspace=V2boundresource.for_model(root_workspace),
            default_workspace=V2boundresource.for_model(default_workspace),
        )

    @classmethod
    def for_tenant(cls, tenant: Tenant) -> Self:
        """Create a new instance with resources for the provided tenant (fetching models as needed)."""
        return cls.for_models(
            tenant=tenant,
            root_workspace=Workspace.objects.root(tenant=tenant),
            default_workspace=Workspace.objects.default(tenant=tenant),
        )

    def resource_for(self, scope: Scope) -> V2boundresource:
        """Return the resource to which role bindings in the given scope should be bound."""
        if scope == Scope.TENANT:
            return self.tenant

        if scope == Scope.ROOT:
            return self.root_workspace

        if scope == Scope.DEFAULT:
            return self.default_workspace

        raise ValueError(f"Unexpected scope: {scope}")


class TenantScopeResourcesCache:
    """A cache of TenantScopeResources instances for multiple tenants."""

    _workspaces: dict[Tenant, dict[Workspace.Types, Workspace]]

    def __init__(self, workspaces: dict[Tenant, dict[Workspace.Types, Workspace]]):
        """Create an instance from the internal representation."""
        self._workspaces = workspaces

    @classmethod
    def for_tenants(cls, tenants: Iterable[Tenant]) -> "TenantScopeResourcesCache":
        """Create an instance for the provided tenants."""
        tenants = list(tenants)
        raw_workspaces: QuerySet = Workspace.objects.filter(
            tenant__in=tenants, type__in=[Workspace.Types.DEFAULT, Workspace.Types.ROOT]
        )

        grouped_workspaces = {}

        # No reason to batch them, since we're keeping them all in memory anyway.
        for workspace in raw_workspaces:
            tenant_dict = grouped_workspaces.setdefault(workspace.tenant, {})

            # Default and root workspaces should be unique
            assert workspace.type not in tenant_dict

            tenant_dict[workspace.type] = workspace

        return TenantScopeResourcesCache(grouped_workspaces)

    def resources_for(self, tenant: Tenant) -> TenantScopeResources:
        """
        Return the resources for the specified tenant.

        Raises KeyError if the tenant is not known.
        """
        tenant_dict = self._workspaces[tenant]

        return TenantScopeResources.for_models(
            tenant=tenant,
            root_workspace=tenant_dict[Workspace.Types.ROOT],
            default_workspace=tenant_dict[Workspace.Types.DEFAULT],
        )


def bound_model_for_scope(
    scope: Scope,
    tenant: Tenant,
    root_workspace: Workspace,
    default_workspace: Workspace,
) -> Tenant | Workspace:
    """Get the model corresponding the provided scope."""
    # TODO: we could retrieve the default and root workspaces from the tenant here (or only do so if None is passed).
    # This is not done here because no current use case requires it.
    assert root_workspace.tenant == tenant
    assert default_workspace.tenant == tenant

    if scope == Scope.TENANT:
        return tenant

    if scope == Scope.ROOT:
        return root_workspace

    if scope == Scope.DEFAULT:
        return default_workspace

    raise ValueError(f"Unexpected scope: {scope}")


class ImplicitResourceService:
    """Classifies permissions based on their default scope."""

    _permissions_map: dict[PermissionValue, Scope]

    def __init__(
        self,
        root_scope_permissions: list[str],
        tenant_scope_permissions: list[str],
        default_scope_permissions: list[str] | None = None,
    ):
        """
        Create an ImplicitResourceService with specific root, tenant, and default workspace scope permissions.

        root_scope_permissions is a set of permissions assigned to the root workspace scope.
        tenant_scope_permissions is a set of permissions assigned to tenant scope.
        default_scope_permissions is a set of permissions assigned to the default workspace scope.

        All sets are represented as V1 permission strings (valid for _PermissionDescriptor.parse_v1).
        All sets may contain wildcards.

        For a given permission, scope_for_permission checks more specific wildcard candidates before broader
        ones (see scope_for_permission). Listing both ``rbac:*:*`` in tenant_scope_permissions and
        ``rbac:role_binding:*`` in default_scope_permissions therefore binds role_binding at default workspace
        and other rbac permissions at tenant scope.
        """
        if default_scope_permissions is None:
            default_scope_permissions = []

        self._permissions_map = {}

        def add_permission(permission: PermissionValue, scope: Scope):
            previous_scope = self._permissions_map.get(permission)

            if previous_scope is not None and previous_scope != scope:
                raise ValueError(
                    f"Duplicate permission found: {permission.v1_string()} is in multiple scopes: "
                    f"{previous_scope} and {scope}"
                )

            self._permissions_map[permission] = scope

        for permission_str in root_scope_permissions:
            add_permission(PermissionValue.parse_v1(permission_str), Scope.ROOT)

        for permission_str in tenant_scope_permissions:
            add_permission(PermissionValue.parse_v1(permission_str), Scope.TENANT)

        for permission_str in default_scope_permissions:
            add_permission(PermissionValue.parse_v1(permission_str), Scope.DEFAULT)

    @classmethod
    def from_settings(cls) -> "ImplicitResourceService":
        """
        Create an ImplicitResourceService from the configuration in settings.

        Root workspace permissions are determined from the ROOT_SCOPE_PERMISSIONS setting. Tenant permissions are
        determined from the TENANT_SCOPE_PERMISSIONS setting. Default workspace permissions are determined from the
        DEFAULT_SCOPE_PERMISSIONS setting.

        Each setting must be a comma-separated list of V1 permissions strings (as if for
        _PermissionDescriptor.parse_v1); spaces are trimmed from the start and each of each permission. An empty (or
        blank) string is acceptable and will be parsed to the empty list.
        """

        def parse_setting(value: str) -> list[str]:
            if value.strip() == "":
                return []

            return [p.strip() for p in value.split(",")]

        return cls(
            root_scope_permissions=parse_setting(settings.ROOT_SCOPE_PERMISSIONS),
            tenant_scope_permissions=parse_setting(settings.TENANT_SCOPE_PERMISSIONS),
            default_scope_permissions=parse_setting(settings.DEFAULT_SCOPE_PERMISSIONS),
        )

    @staticmethod
    def _wildcard_candidates(parsed: PermissionValue) -> list[PermissionValue]:
        """Return the parsed value plus progressively broader wildcard variants.

        Precedence order: exact, unconstrained verb, unconstrained resource type,
        application-only.  Duplicates (when the input is already a wildcard) are
        excluded.
        """
        return [parsed] + [
            wildcard
            for wildcard in [
                parsed.with_unconstrained_verb(),
                parsed.with_unconstrained_resource_type(),
                parsed.with_application_only(),
            ]
            if wildcard != parsed
        ]

    def is_explicitly_scoped(self, permission: str) -> bool:
        """Return True if the permission matches any configured scope entry.

        A permission is explicitly scoped when it (or a wildcard that covers it)
        appears in root_scope_permissions, tenant_scope_permissions, or
        default_scope_permissions.  Permissions that fall through to the
        ``Scope.DEFAULT`` fallback in ``scope_for_permission`` are NOT
        explicitly scoped -- they are "workspace-granular".
        """
        candidates = self._wildcard_candidates(PermissionValue.parse_v1(permission))
        return any(candidate in self._permissions_map for candidate in candidates)

    def scope_for_permission(self, permission: str) -> Scope:
        """
        Return the scope that a permission binds to using this object's configured permissions.

        The argument shall be a V1 permission string (as if for _PermissionDescriptor.parse_v1).
        The permission may be a wildcard.

        Matching precedence (highest to lowest):
        1. Exact app:resource_type:verb match.
        2. Wildcard app:resource_type:* match.
        3. Wildcard app:*:verb match.
        4. Wildcard app:*:* match.
        5. Finally, if no match exists, the DEFAULT scope (including explicit patterns from default_scope_permissions
        / DEFAULT_SCOPE_PERMISSIONS when they match a candidate).

        Note that, if the permission is a wildcard, some of these steps will be redundant. For instance,
        if the permission is app:*:verb, there are only two possible matches: app:*:verb and app:*:*.
        """
        candidates = self._wildcard_candidates(PermissionValue.parse_v1(permission))

        for candidate in candidates:
            scope = self._permissions_map.get(candidate)

            if scope is not None:
                return scope

        return Scope.DEFAULT

    def all_scopes_for_permission(self, permission: str) -> set["Scope"]:
        """Return every scope that a permission covers, including scopes of narrower patterns it subsumes.

        For a concrete permission like ``subscriptions:organization:read``, this returns the
        same single scope as ``scope_for_permission``.

        For a wildcard like ``subscriptions:*:*``, this additionally checks whether any
        more-specific patterns in the scope map (e.g. ``subscriptions:reports:*``) belong to
        a different scope. If so, the wildcard effectively spans both scopes.
        """
        parsed = PermissionValue.parse_v1(permission)
        primary_scope = self.scope_for_permission(permission)
        scopes = {primary_scope}

        if not parsed.is_wildcard:
            return scopes

        for configured_perm, configured_scope in self._permissions_map.items():
            if configured_scope == primary_scope:
                continue
            if parsed.subsumes(configured_perm):
                scopes.add(configured_scope)

        return scopes

    def highest_scope_for_permissions(self, permissions: Iterable[str]) -> Scope:
        """
        Return the highest scope to which any permission in permissions is assigned.

        Permission scopes are determined as if by using scope_for_permission.
        """
        return max(
            (self.scope_for_permission(permission) for permission in permissions),
            default=Scope.DEFAULT,
        )

    def binding_scopes_for_role(self, role: Role) -> list["Scope"]:
        """Return the scopes at which bindings should be created for this role.

        If the role has mixed scopes including TENANT, TENANT is split out and the
        remaining workspace scopes are merged to the highest among them.
        ROOT+DEFAULT without TENANT collapses to ROOT (workspace parent inheritance).
        """
        # TODO: this may need to eventually take into account resource definitions for custom roles.
        return binding_scopes_for_permissions((a.permission.permission for a in role.access.all()), self)

    def v2_bound_resource_for_permission(
        self,
        permissions: Iterable[str],
        tenant_org_id: str,
        root_workspace_id: str,
        default_workspace_id: str,
    ) -> V2boundresource:
        """
        Return a V2boundresource corresponding the highest scope for any permission in permissions.

        The appropriate scope is determined as if by highest_scope_for_permissions. A V2boundresource is then
        returned, bound to the appropriate provided resource.
        """
        scope = self.highest_scope_for_permissions(permissions)

        if scope == Scope.TENANT:
            tenant_resource_id = Tenant.org_id_to_tenant_resource_id(tenant_org_id)
            return V2boundresource(resource_type=("rbac", "tenant"), resource_id=tenant_resource_id)
        elif scope == Scope.ROOT:
            return V2boundresource(resource_type=("rbac", "workspace"), resource_id=root_workspace_id)
        elif scope == Scope.DEFAULT:
            return V2boundresource(resource_type=("rbac", "workspace"), resource_id=default_workspace_id)
        else:
            raise AssertionError(f"Unexpected scope: {scope}")


SCOPE_RESOURCE_TYPE: dict[Scope, str] = {
    Scope.TENANT: "tenant",
    Scope.ROOT: "workspace",
    Scope.DEFAULT: "workspace",
}
"""Maps each Scope to the resource_type string it binds to."""

SCOPE_DISPLAY_NAME: dict[Scope, str] = {
    Scope.DEFAULT: "Default Workspace",
    Scope.ROOT: "Root Workspace",
    Scope.TENANT: "Organization",
}
"""User-facing label for each Scope (avoids exposing internal 'tenant' terminology)."""


def scopes_for_resource_type(resource_type: str) -> set[Scope]:
    """Return all Scope values that map to the given resource_type."""
    return {scope for scope, rt in SCOPE_RESOURCE_TYPE.items() if rt == resource_type}


def scope_for_resource(resource_type: str, resource_id: str, tenant: Tenant) -> Scope | None:
    """Return the single expected Scope for a (resource_type, resource_id) pair, or None if unknown.

    * ``tenant`` -> ``Scope.TENANT``
    * ``workspace`` with the root workspace -> ``Scope.ROOT``
    * ``workspace`` with any other workspace -> ``Scope.DEFAULT``
    * anything else -> ``None``
    """
    if resource_type == "tenant":
        return Scope.TENANT
    if resource_type == "workspace":
        try:
            workspace = Workspace.objects.get(id=resource_id, tenant=tenant)
        except Workspace.DoesNotExist:
            return None
        if workspace.type == Workspace.Types.ROOT:
            return Scope.ROOT
        return Scope.DEFAULT
    return None


def resolve_workspace_scope(resource_id: str, tenant: Tenant) -> tuple[Scope, bool] | None:
    """Resolve workspace scope and standard-workspace flag in a single query.

    Returns ``(scope, is_standard_workspace)`` or ``None`` if the workspace
    does not exist for the given tenant.
    """
    try:
        ws_type = Workspace.objects.values_list("type", flat=True).get(id=resource_id, tenant=tenant)
    except Workspace.DoesNotExist:
        return None
    if ws_type == Workspace.Types.ROOT:
        return Scope.ROOT, False
    return Scope.DEFAULT, ws_type == Workspace.Types.STANDARD


"""
A global ImplicitResourceService configured using Django Settings.

See ImplicitResourceService.from_settings for details on how this is configured.
"""
default_implicit_resource_service = ImplicitResourceService.from_settings()


class PermissionScopeCache:
    """In-process cache mapping Permission IDs to their computed Scope.

    The cache is populated lazily on first access and remains valid for the
    lifetime of the process.  Call ``invalidate()`` after any operation that
    mutates the Permission table (e.g. seeding) so the next access rebuilds
    the mapping from the database.
    """

    def __init__(self, scope_service: ImplicitResourceService):
        """Create a cache backed by the given scope service."""
        self._scope_service = scope_service
        self._ids_by_scope: dict[Scope, frozenset[int]] | None = None
        self._explicit_default_ids: frozenset[int] | None = None

    def _build(self) -> dict[Scope, frozenset[int]]:
        from management.permission.model import Permission

        result: dict[Scope, set[int]] = {scope: set() for scope in Scope}
        explicit_default: set[int] = set()
        for row in Permission.objects.values_list("id", "permission", named=True):
            scope = self._scope_service.scope_for_permission(row.permission)
            result[scope].add(row.id)
            if scope == Scope.DEFAULT and self._scope_service.is_explicitly_scoped(row.permission):
                explicit_default.add(row.id)
        self._explicit_default_ids = frozenset(explicit_default)
        return {s: frozenset(ids) for s, ids in result.items()}

    @property
    def ids_by_scope(self) -> dict[Scope, frozenset[int]]:
        """Return the cached mapping, building it on first access."""
        if self._ids_by_scope is None:
            self._ids_by_scope = self._build()
        return self._ids_by_scope

    def ids_for_scopes(self, scopes: set[Scope]) -> frozenset[int]:
        """Return the union of Permission IDs for the given scopes."""
        return frozenset().union(*(self.ids_by_scope.get(s, frozenset()) for s in scopes))

    @property
    def explicit_default_ids(self) -> frozenset[int]:
        """Return Permission IDs that are explicitly configured as DEFAULT scope.

        These are permissions that matched a DEFAULT_SCOPE_PERMISSIONS entry,
        as opposed to fallback-DEFAULT permissions (workspace-granular) that
        simply didn't match any configured scope.
        """
        _ = self.ids_by_scope  # Ensure cache is built
        assert self._explicit_default_ids is not None
        return self._explicit_default_ids

    def invalidate(self):
        """Clear the cached mapping so it is rebuilt on next access."""
        self._ids_by_scope = None
        self._explicit_default_ids = None


permission_scope_cache = PermissionScopeCache(default_implicit_resource_service)


def split_permissions_by_binding_scope(
    permissions: Iterable[str],
    resource_service: ImplicitResourceService | None = None,
) -> dict[Scope, list[str]]:
    """Group permissions by their binding scope.

    If all permissions share one scope, returns ``{scope: [all_perms]}``.
    If mixed with TENANT, returns ``{TENANT: [...], max(others): [...]}``.
    If only ROOT+DEFAULT mixed, returns ``{ROOT: [all_perms]}`` because ROOT
    inherits to DEFAULT via workspace parent chain.

    Wildcard permissions that span both TENANT and workspace scopes are placed
    in **both** groups so that the permission is effective at every level.
    """
    if resource_service is None:
        resource_service = default_implicit_resource_service

    permissions = list(permissions)
    if not permissions:
        return {}

    permissions_by_scope: dict[Scope, set[str]] = {}
    for perm in permissions:
        for scope in resource_service.all_scopes_for_permission(perm):
            permissions_by_scope.setdefault(scope, set()).add(perm)

    if not permissions_by_scope:
        return {Scope.DEFAULT: permissions}

    if Scope.DEFAULT in permissions_by_scope and Scope.ROOT in permissions_by_scope:
        permissions_by_scope[Scope.ROOT].update(permissions_by_scope[Scope.DEFAULT])
        del permissions_by_scope[Scope.DEFAULT]

    return {scope: list(scoped_perms) for scope, scoped_perms in permissions_by_scope.items()}


def binding_scopes_for_permissions(
    permissions: Iterable[str],
    resource_service: ImplicitResourceService | None = None,
) -> list[Scope]:
    """Return the distinct binding scopes needed for a set of permissions.

    If the permissions span multiple scopes and one of them is TENANT,
    TENANT is kept separate and the remaining workspace-level scopes are
    merged to the highest among them (ROOT > DEFAULT).

    If only ROOT and DEFAULT are mixed, a single ROOT scope is returned
    because workspace parent inheritance covers DEFAULT.

    Wildcard permissions (e.g. ``subscriptions:*:*``) that subsume patterns
    configured in other scopes are treated as spanning those scopes too.
    """
    split = split_permissions_by_binding_scope(permissions, resource_service)
    if not split:
        return [Scope.DEFAULT]
    return sorted(split.keys(), key=lambda s: s.value)
