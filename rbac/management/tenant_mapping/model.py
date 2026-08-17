#
# Copyright 2024 Red Hat, Inc.
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
"""TenantMapping model."""

import enum
import logging
import uuid
from typing import Callable, ClassVar, Optional

from django.db import models
from management.permission.scope_service import Scope
from management.utils import as_uuid

from api.models import Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class DefaultAccessType(enum.StrEnum):
    """Represents the two types of default access resources. This mirrors the split in TenantMapping."""

    USER = "user"
    ADMIN = "admin"


class TenantMapping(models.Model):
    """Tenant mappings to V2 domain concepts."""

    @staticmethod
    def _role_binding_field():
        return models.UUIDField(default=uuid.uuid4, editable=False, null=False, unique=True)

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="tenant_mapping")

    # Once set, this tenant has performed V2 write operations and must never
    # be allowed to write via V1 again, regardless of feature flag state.
    v2_write_activated_at = models.DateTimeField(null=True, blank=True, default=None)

    # Default group UUID specific to a Tenant. This is used for adding members of the Tenant to the access graph.
    # It is also used for custom default group UUID, so that the custom roles get bound to the default group
    # members.
    default_group_uuid = models.UUIDField(default=uuid.uuid4, editable=False, null=False)
    # The admin default group UUID, for the same purpose as above, except for admin users.
    default_admin_group_uuid = models.UUIDField(default=uuid.uuid4, editable=False, null=False)

    # UUIDs for the role bindings that bind the user default and admin default groups to the appropriate platform
    # groups. One such role binding exists for each of the scopes that has default roles: the tenant itself, the
    # tenant's root workspace, and the tenant's default workspace.
    default_role_binding_uuid = _role_binding_field()
    default_admin_role_binding_uuid = _role_binding_field()
    root_scope_default_role_binding_uuid = _role_binding_field()
    root_scope_default_admin_role_binding_uuid = _role_binding_field()
    tenant_scope_default_role_binding_uuid = _role_binding_field()
    tenant_scope_default_admin_role_binding_uuid = _role_binding_field()

    _role_binding_uuid_fns: ClassVar[dict[DefaultAccessType, dict[Scope, Callable[["TenantMapping"], uuid.UUID]]]] = {
        DefaultAccessType.USER: {
            Scope.DEFAULT: lambda m: m.default_role_binding_uuid,
            Scope.ROOT: lambda m: m.root_scope_default_role_binding_uuid,
            Scope.TENANT: lambda m: m.tenant_scope_default_role_binding_uuid,
        },
        DefaultAccessType.ADMIN: {
            Scope.DEFAULT: lambda m: m.default_admin_role_binding_uuid,
            Scope.ROOT: lambda m: m.root_scope_default_admin_role_binding_uuid,
            Scope.TENANT: lambda m: m.tenant_scope_default_admin_role_binding_uuid,
        },
    }

    def group_uuid_for(self, access_type: DefaultAccessType) -> uuid.UUID:
        """Get the UUID for the tenant's default group for the appropriate access type."""
        if access_type == DefaultAccessType.USER:
            return self.default_group_uuid
        elif access_type == DefaultAccessType.ADMIN:
            return self.default_admin_group_uuid
        else:
            raise ValueError(f"Unexpected access type: {access_type}")

    def default_role_binding_uuid_for(self, access_type: DefaultAccessType, scope: Scope) -> uuid.UUID:
        """Get the UUID for the tenant's default role binding (in the provided scope) of the provided access type."""
        return TenantMapping._role_binding_uuid_fns[access_type][scope](self)

    def role_binding_ids(self) -> set[str]:
        """Return the set of all role binding UUIDs (as strings) that this TenantMapping defines."""
        return {
            str(self.default_role_binding_uuid),
            str(self.default_admin_role_binding_uuid),
            str(self.root_scope_default_role_binding_uuid),
            str(self.root_scope_default_admin_role_binding_uuid),
            str(self.tenant_scope_default_role_binding_uuid),
            str(self.tenant_scope_default_admin_role_binding_uuid),
        }

    def source_for_role_binding_id(
        self, role_binding_id: str | uuid.UUID
    ) -> Optional[tuple[DefaultAccessType, Scope]]:
        """Return the DefaultAccessType and Scope corresponding to the provided UUID, or None if there is none."""
        role_binding_id = as_uuid(role_binding_id)

        if role_binding_id == self.default_role_binding_uuid:
            return DefaultAccessType.USER, Scope.DEFAULT

        if role_binding_id == self.default_admin_role_binding_uuid:
            return DefaultAccessType.ADMIN, Scope.DEFAULT

        if role_binding_id == self.root_scope_default_role_binding_uuid:
            return DefaultAccessType.USER, Scope.ROOT

        if role_binding_id == self.root_scope_default_admin_role_binding_uuid:
            return DefaultAccessType.ADMIN, Scope.ROOT

        if role_binding_id == self.tenant_scope_default_role_binding_uuid:
            return DefaultAccessType.USER, Scope.TENANT

        if role_binding_id == self.tenant_scope_default_admin_role_binding_uuid:
            return DefaultAccessType.ADMIN, Scope.TENANT

        return None
