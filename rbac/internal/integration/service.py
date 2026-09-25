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

"""V2 role compatibility helpers for OCM integration reads."""

from django.db.models import BooleanField, Case, Exists, F, OuterRef, Q, Value, When
from django.db.models.functions import Coalesce
from management.permission.scope_service import CONCRETE_SCOPES
from management.role.v2_model import RoleV2
from management.role_binding.model import RoleBinding
from management.tenant_mapping.model import DefaultAccessType, TenantMapping


def expand_default_roles(roles, tenant):
    """Expose seeded children rather than internal platform aggregations for default groups."""
    platform_ids = roles.filter(type=RoleV2.Types.PLATFORM).values("pk")
    direct_ids = roles.exclude(type=RoleV2.Types.PLATFORM).values("pk")
    return RoleV2.objects.for_tenant(tenant).filter(Q(pk__in=direct_ids) | Q(parents__in=platform_ids)).distinct()


def roles_for_group(group, tenant, *, exclude=False, principal_groups=None):
    """Resolve group assignments or excluded roles from tenant-scoped V2 bindings."""
    roles = (
        RoleV2.objects.for_tenant(tenant)
        .filter(bindings__group_entries__group=group, bindings__tenant=tenant)
        .distinct()
    )
    if group.system and (group.platform_default or group.admin_default):
        roles = expand_default_roles(roles, tenant)
    if exclude:
        candidates = RoleV2.objects.for_tenant(tenant).assignable()
        if principal_groups is not None:
            bound_roles = RoleV2.objects.for_tenant(tenant).filter(
                bindings__tenant=tenant, bindings__group_entries__group__in=principal_groups
            )
            candidates = expand_default_roles(bound_roles, tenant)
        roles = candidates.exclude(pk__in=roles.values("pk")).distinct()
    return roles


def annotate_integration_roles(roles, tenant):
    """Derive V1 response metadata without resolving assignments through V1 policies."""
    mapping = TenantMapping.objects.filter(tenant=tenant).first()
    default_queries = {}
    for access_type, field in (
        (DefaultAccessType.USER, "platform_default"),
        (DefaultAccessType.ADMIN, "admin_default"),
    ):
        binding_ids = (
            [mapping.default_role_binding_uuid_for(access_type, scope) for scope in CONCRETE_SCOPES] if mapping else []
        )
        default_bindings = RoleBinding.objects.filter(tenant=tenant, uuid__in=binding_ids)
        default_queries[field] = Exists(
            default_bindings.filter(Q(role_id=OuterRef("pk")) | Q(role__children=OuterRef("pk")))
        )
    return (
        roles.annotate(
            display_name=Coalesce("v1_source__display_name", F("name")),
            policyCount=Value(0),
            system=Case(
                When(type__in=(RoleV2.Types.SEEDED, RoleV2.Types.PLATFORM), then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
            **default_queries,
        )
        .select_related("v1_source__ext_relation__ext_tenant")
        .prefetch_related("permissions")
    )
