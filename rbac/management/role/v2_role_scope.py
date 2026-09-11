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
"""V2 role list and binding eligibility rules."""

from __future__ import annotations

from django.conf import settings
from django.db.models import Q
from management.role.v2_model import RoleV2
from management.workspace.model import Workspace

from api.models import Tenant


def v2_role_excluded_applications() -> frozenset[str]:
    """Return permission ``application`` values in ``V2_MIGRATION_APP_EXCLUDE_LIST``.

    Roles that include any permission in these applications are omitted from v2 role
    list and retrieve, cannot be assigned on role bindings, and cannot be created or
    updated as custom v2 roles with such permissions.
    """
    return frozenset(app.strip() for app in settings.V2_MIGRATION_APP_EXCLUDE_LIST if app and str(app).strip())


class V2RoleExcludedApplicationPermissionIdsCache:
    """In-process cache: Permission PKs whose ``application`` is in the migration exclude list.

    Rebuilt when the cached application set no longer matches settings, or after
    ``invalidate()`` (e.g. permission seeding).
    """

    def __init__(self):
        """Initialize the cache."""
        self._apps: frozenset[str] | None = None
        self._ids: frozenset[int] | None = None

    def permission_ids(self) -> frozenset[int]:
        """Return IDs of all permissions in excluded applications (possibly empty)."""
        apps = v2_role_excluded_applications()
        if not apps:
            self._apps = apps
            self._ids = frozenset()
            return self._ids

        if self._ids is not None and self._apps == apps:
            return self._ids

        from management.permission.model import Permission

        self._apps = apps
        self._ids = frozenset(Permission.objects.filter(application__in=list(apps)).values_list("id", flat=True))
        return self._ids

    def invalidate(self) -> None:
        """Clear the cache so the next access reloads from the database."""
        self._apps = None
        self._ids = None


v2_role_excluded_application_permission_ids_cache = V2RoleExcludedApplicationPermissionIdsCache()


# OCM external roles
OCM_EXTERNAL_TENANT = "ocm"

OCM_V2_ROLE_Q = Q(v1_source__ext_relation__ext_tenant__name__iexact=OCM_EXTERNAL_TENANT)


def is_ocm_v2_role(role: RoleV2) -> bool:
    """Return True when the V2 role is a seeded OCM external role."""
    v1_source = getattr(role, "v1_source", None)
    if v1_source is None:
        return False
    ext_relation = getattr(v1_source, "ext_relation", None)
    if ext_relation is None:
        return False
    ext_tenant = getattr(ext_relation, "ext_tenant", None)
    if ext_tenant is None:
        return False
    return ext_tenant.name.lower() == OCM_EXTERNAL_TENANT


def ocm_roles_allowed_for_workspace_binding(resource_type: str, resource_id: str | None, tenant: Tenant) -> bool:
    """Return True when OCM roles may appear in list or be bound for the given resource."""
    if resource_type != "workspace":
        return False
    if resource_id is None:
        return True
    return str(Workspace.objects.default(tenant=tenant).id) == str(resource_id)
