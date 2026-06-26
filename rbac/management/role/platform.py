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
"""Contains utilities for handling platform roles."""

from typing import Callable, Iterable
from uuid import UUID

from django.conf import settings
from management.group.platform import GlobalPolicyIdService
from management.permission.scope_service import Scope
from management.tenant_mapping.model import DefaultAccessType

# admin_default seeded system roles that must nest under the admin platform role at ROOT (root workspace),
# not under the scope implied by their inventory (or other default-scope) permissions.
ADMIN_DEFAULT_SEEDED_ROLES_FORCE_ROOT_SCOPE = frozenset(
    (
        "Inventory Groups Administrator",
        "Inventory Hosts Administrator",
    )
)


def admin_platform_parent_scopes_for_seeded_system_role(
    role_name: str, permission_derived_scopes: Iterable[Scope]
) -> set[Scope]:
    """
    Return the scope of the admin platform role that should be the parent of this admin_default seeded role.

    Call only for roles with ``admin_default=True``. When apply_override is False (e.g. when generating
    tuples to strip all historical variants), the caller's scope is used as-is.
    """
    if role_name in ADMIN_DEFAULT_SEEDED_ROLES_FORCE_ROOT_SCOPE:
        return {Scope.ROOT}

    return set(permission_derived_scopes)


_uuid_fns: dict[DefaultAccessType, dict[Scope, Callable[[GlobalPolicyIdService], UUID]]] = {
    DefaultAccessType.USER: {
        Scope.DEFAULT: lambda ps: ps.platform_default_policy_uuid(),
        Scope.ROOT: lambda _: UUID(settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID),
        Scope.TENANT: lambda _: UUID(settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID),
    },
    DefaultAccessType.ADMIN: {
        Scope.DEFAULT: lambda ps: ps.admin_default_policy_uuid(),
        Scope.ROOT: lambda _: UUID(settings.SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID),
        Scope.TENANT: lambda _: UUID(settings.SYSTEM_ADMIN_TENANT_ROLE_UUID),
    },
}


def platform_v2_role_uuid_for(
    access_type: DefaultAccessType, scope: Scope, policy_service: GlobalPolicyIdService
) -> UUID:
    """
    Get the UUID for the platform default role for the provided access type and scope.

    This UUID is intended for use with Relations (e.g. as a parent role for platform default roles).
    """
    return _uuid_fns[access_type][scope](policy_service)
