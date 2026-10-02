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
"""Service layer for GroupV2."""

import logging
from typing import List, Optional, Sequence

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, ProtectedError, Q, QuerySet
from management.group.model import Group
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.group.v2_exceptions import (
    GroupAlreadyExistsError,
    GroupHasRoleBindingsError,
    PrincipalNotFoundError,
    ProtectedGroupError,
)
from management.principal.model import Principal
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import Role
from management.v2_filters import v2_name_filter, v2_name_query

from api.models import Tenant

logger = logging.getLogger(__name__)


class GroupV2Service:
    """Service for V2 group operations."""

    UNIQUE_NAME_CONSTRAINT = "unique group name per tenant"
    DEFAULT_ORDER_BY = "name"
    ORDER_BY_FIELD_MAPPING = {
        "name": "name",
        "modified": "modified",
        "principal_count": "principal_count_annotation",
        "role_count": "role_count_annotation",
    }
    PROTECTED_FLAGS_FOR_UPDATE = ("system",)
    PROTECTED_FLAGS_FOR_DELETE = ("system", "platform_default", "admin_default")
    ORG_ID_SCOPE = "org_id"
    PRINCIPAL_SCOPE = "principal"
    SCOPES = (ORG_ID_SCOPE, PRINCIPAL_SCOPE)
    ROLE_DISCRIMINATOR_ANY = "any"
    ROLE_DISCRIMINATOR_ALL = "all"
    ROLE_DISCRIMINATORS = (ROLE_DISCRIMINATOR_ANY, ROLE_DISCRIMINATOR_ALL)

    def __init__(self, tenant: Tenant):
        """Initialize service with tenant context."""
        self.tenant = tenant

    def queryset(self, *, include_public_defaults: bool = False, is_org_admin: Optional[bool] = None) -> QuerySet:
        """Return the tenant's groups annotated with principal and role counts.

        include_public_defaults also returns the public tenant's default groups the tenant has no own copy of
        (V1 parity); the public admin default group is only included for org admins. is_org_admin must be
        passed explicitly whenever include_public_defaults=True, to avoid silently omitting the admin default
        group for an actual org admin.
        """
        groups = Q(tenant=self.tenant)
        if include_public_defaults:
            if is_org_admin is None:
                raise ValueError("is_org_admin must be provided when include_public_defaults=True")
            # A single OR-ed filter (not a queryset union) keeps the count annotations, filters, ordering and
            # pagination applying uniformly to tenant-owned and public groups. This avoids materializing a large
            # pk__in list; the tenant lookup and per-flag checks still use small, index-backed subqueries.
            groups |= self._public_default_groups_filter(is_org_admin)
        return Group.objects.filter(groups).annotate(
            principal_count_annotation=Count(
                "principals", filter=Q(principals__type=Principal.Types.USER), distinct=True
            ),
            role_count_annotation=Count(
                "role_binding_entries__binding__role",
                filter=Q(role_binding_entries__binding__tenant=F("tenant")),
                distinct=True,
            ),
        )

    def list(self, params: dict, requester_username: Optional[str] = None, is_org_admin: bool = False) -> QuerySet:
        """List groups with optional filtering and ordering.

        requester_username is required for scope=principal, which returns only the requester's groups.
        is_org_admin decides whether the public admin default group is included.
        """
        exclude_username = params.get("exclude_username")
        # exclude_username omits the public default groups, which can never gain members. V1's _filter_default_groups
        # also drops tenant-owned default groups; those stay listed here since V2 allows adding members to them.
        queryset = self.queryset(include_public_defaults=not exclude_username, is_org_admin=is_org_admin)

        name = params.get("name")
        if name:
            queryset = v2_name_filter(queryset, name, field="name")

        uuids = params.get("uuid")
        if uuids:
            queryset = queryset.filter(uuid__in=uuids)

        # Filters traversing principals or role bindings join multi-valued relations, so .distinct() prevents
        # duplicate groups. The count annotations use Count(distinct=True) and are unaffected by the extra joins.
        # All principal-based filters restrict to Principal.Types.USER to match principal_count_annotation --
        # service accounts share the username field but are excluded from that count. Each also pins
        # principals__tenant=F("tenant") so a cross-tenant Principal can never match even if the group's
        # principals M2M were ever mistakenly linked across tenants.
        username = params.get("username")
        if username:
            queryset = v2_name_filter(
                queryset,
                username,
                field="principals__username",
                extra_filters={"principals__type": Principal.Types.USER, "principals__tenant": F("tenant")},
            ).distinct()

        if exclude_username:
            # exclude() on a multi-valued relation ANDs conditions across independently-matched rows
            # rather than requiring a single row to satisfy both (unlike filter()), so the type and
            # username conditions are combined here via a Principal subquery instead.
            matching_principals = Principal.objects.filter(
                tenant=self.tenant, type=Principal.Types.USER, username__icontains=exclude_username
            ).values("pk")
            queryset = queryset.exclude(principals__in=matching_principals)

        role_names = params.get("role_names")
        if role_names:
            discriminator = params.get("role_discriminator", self.ROLE_DISCRIMINATOR_ANY)
            queryset = self._filter_by_role_names(queryset, role_names, discriminator)

        # Chain one filter per principal so a group must contain all of them.
        principals = params.get("principals") or ()
        for principal in principals:
            queryset = queryset.filter(
                principals__type=Principal.Types.USER,
                principals__username__iexact=principal,
                principals__tenant=F("tenant"),
            )
        if principals:
            queryset = queryset.distinct()

        if params.get("scope") == self.PRINCIPAL_SCOPE:
            if not requester_username:
                return queryset.none()
            queryset = queryset.filter(
                principals__type=Principal.Types.USER,
                principals__username__iexact=requester_username,
                principals__tenant=F("tenant"),
            ).distinct()

        for flag in ("system", "platform_default", "admin_default"):
            value = params.get(flag)
            if value is not None:
                queryset = queryset.filter(**{flag: value})

        return queryset.order_by(*self._ordering(params.get("order_by") or self.DEFAULT_ORDER_BY))

    def get(self, group: Group) -> Group:
        """Return the given group re-fetched with count annotations."""
        return self.queryset().get(pk=group.pk)

    def create(self, name: str, description: Optional[str] = None) -> Group:
        """Create a new group."""
        try:
            with transaction.atomic():
                group = Group.objects.create(name=name, description=description, tenant=self.tenant)
        except IntegrityError as e:
            if self._is_unique_name_violation(e):
                raise GroupAlreadyExistsError(name)
            raise
        return group

    def update(self, group: Group, name: str, description: Optional[str] = None) -> Group:
        """Update the name and description of a group."""
        self._check_not_protected(group, self.PROTECTED_FLAGS_FOR_UPDATE, "updated")

        group.name = name
        group.description = description
        try:
            with transaction.atomic():
                group.save()
        except IntegrityError as e:
            if self._is_unique_name_violation(e):
                raise GroupAlreadyExistsError(name)
            raise
        return group

    def delete(self, group: Group) -> None:
        """Delete a group and replicate the removal of its membership relations."""
        self._check_not_protected(group, self.PROTECTED_FLAGS_FOR_DELETE, "deleted")

        # Count both v2 role bindings (RoleBindingGroup) and legacy v1 role assignments
        # (Policy). role_binding_entries alone misses tenants still on v1-era Policy/
        # BindingMapping assignments, which would otherwise pass this guard and leave
        # orphaned SpiceDB tuples behind after the group is deleted.
        binding_count = group.role_binding_entries.count()
        legacy_role_count = Role.objects.filter(policies__group=group).count()
        if binding_count or legacy_role_count:
            raise GroupHasRoleBindingsError(binding_count + legacy_role_count)

        # Capture members before deletion; the M2M rows are removed along with the group.
        principals = list(group.principals.all())
        # Construct before delete(): Django clears the instance pk on delete(), and while
        # this handler currently only reads group.tenant_id/uuid (unaffected), constructing
        # it up front matches the V1 destroy() ordering and avoids relying on that detail.
        dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.DELETE_GROUP)
        try:
            group.delete()
        except ProtectedError as e:
            raise GroupHasRoleBindingsError(len(e.protected_objects))

        dual_write_handler.replicate_removed_principals(principals)

    def _public_default_groups_filter(self, is_org_admin: bool) -> Q:
        """Match the public default groups for each default flag the tenant has no own group for.

        A tenant-owned default group (e.g. the "Custom default group") fully replaces its public counterpart.
        """
        flags = ("platform_default", "admin_default") if is_org_admin else ("platform_default",)
        missing_defaults = Q()
        for flag in flags:
            tenant_has_own = Exists(Group.objects.filter(tenant=self.tenant, **{flag: True}))
            missing_defaults |= Q(**{flag: True}) & ~tenant_has_own
        return Q(tenant=Tenant._get_public_tenant(), system=True) & missing_defaults

    def _filter_by_role_names(self, queryset: QuerySet, role_names: Sequence[str], discriminator: str) -> QuerySet:
        """Filter groups bound to any (default) or all of the given role names, matched case-insensitively."""
        # Only count bindings in the group's own tenant, matching role_count_annotation.
        tenant_bindings = Q(role_binding_entries__binding__tenant=F("tenant"))
        if discriminator == self.ROLE_DISCRIMINATOR_ALL:
            # Each chained filter() joins the bindings anew, so every role name must match some binding.
            for role_name in role_names:
                queryset = queryset.filter(
                    tenant_bindings, role_binding_entries__binding__role__name__iexact=role_name
                )
            return queryset.distinct()

        any_role = Q()
        for role_name in role_names:
            any_role |= Q(role_binding_entries__binding__role__name__iexact=role_name)
        return queryset.filter(tenant_bindings, any_role).distinct()

    def list_principals(self, group: Group, params: dict) -> QuerySet:
        """List a group's member principals, annotated with group_count, filtered by the given params."""
        queryset = (
            Principal.objects.filter(tenant=self.tenant, pk__in=group.principals.values("pk"))
            .exclude(cross_account=True)
            .annotate(group_count=Count("group", filter=Q(group__tenant=F("tenant")), distinct=True))
        )

        service_account_client_ids = params.get("service_account_client_ids")
        if service_account_client_ids:
            return queryset.filter(
                type=Principal.Types.SERVICE_ACCOUNT, service_account_id__in=service_account_client_ids
            ).order_by("username", "uuid")

        principal_type = params.get("principal_type") or Principal.Types.USER
        if principal_type != "all":
            queryset = queryset.filter(type=principal_type)

        for field in ("username", "principal_username"):
            value = params.get(field)
            if value:
                queryset = v2_name_filter(queryset, value, field="username")

        # service_account_name/service_account_description only apply when principal_type is 'service-account'
        # or 'all' (per the TypeSpec contract); with principal_type='user' the queryset is already narrowed to
        # users, so applying a type=service-account filter on top would always yield zero rows. Skip them
        # instead, matching the documented no-op behavior for that case.
        if principal_type != Principal.Types.USER:
            # service_account_name/service_account_description have no local column to filter on (no display_name
            # or description stored for service accounts); degrade to matching on username, scoped to service
            # accounts only so these filters never match regular user principals. The two filters are independent
            # search criteria (per the TypeSpec contract), so they are OR-ed together rather than chained, which
            # would otherwise require a single username to match both substrings simultaneously.
            sa_values: list[str] = [
                params[field] for field in ("service_account_name", "service_account_description") if params.get(field)
            ]
            if sa_values:
                queryset = queryset.filter(type=Principal.Types.SERVICE_ACCOUNT)
                combined_query = None
                for value in sa_values:
                    query = v2_name_query(value, field="username")
                    if query is None:
                        # A bare '*' already matches everything; no further filtering is needed.
                        combined_query = None
                        break
                    combined_query = query if combined_query is None else combined_query | query
                if combined_query is not None:
                    queryset = queryset.filter(combined_query)

        # username_only and admin_only are accepted (see GroupV2ListPrincipalsInputSerializer help_text) but
        # intentionally not read here: this endpoint never enriches from external identity services, so
        # username_only is always satisfied by construction, and admin_only has no local Principal column to
        # filter on.
        order_by = params.get("order_by") or "username"
        return queryset.order_by(order_by, "uuid")

    def add_principals(self, group: Group, usernames: set, service_account_client_ids: set) -> List[Principal]:
        """Add principals to a group, resolved from RBAC's local Principal table only.

        Identifiers that resolve to already-existing members are silently skipped -- only newly added
        principals are replicated and returned, so re-adding an existing member is a no-op rather than
        producing duplicate dual-write replication and audit trail entries.
        """
        self._check_not_protected(group, self.PROTECTED_FLAGS_FOR_UPDATE, "modified")

        principals = self._resolve_principals(usernames, service_account_client_ids)
        existing_ids = set(group.principals.values_list("pk", flat=True))
        new_principals = [p for p in principals if p.pk not in existing_ids]

        if new_principals:
            group.principals.add(*new_principals)
            dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.ADD_PRINCIPALS_TO_GROUP)
            dual_write_handler.replicate_new_principals(new_principals)

        return new_principals

    def remove_principals(self, group: Group, usernames: set, service_account_client_ids: set) -> List[Principal]:
        """Remove principals from a group. All identifiers must currently be members, or nothing is removed."""
        self._check_not_protected(group, self.PROTECTED_FLAGS_FOR_UPDATE, "modified")

        principals = self._resolve_member_principals(group, usernames, service_account_client_ids)
        group.principals.remove(*principals)

        dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.REMOVE_PRINCIPALS_FROM_GROUP)
        dual_write_handler.replicate_removed_principals(principals)

        return principals

    def remove_principal(self, group: Group, principal_uuid) -> Principal:
        """Remove a single principal from a group by principal UUID."""
        self._check_not_protected(group, self.PROTECTED_FLAGS_FOR_UPDATE, "modified")

        try:
            principal = (
                group.principals.filter(tenant=self.tenant, uuid=principal_uuid).exclude(cross_account=True).first()
            )
        except (DjangoValidationError, ValueError):
            principal = None
        if principal is None:
            raise PrincipalNotFoundError([str(principal_uuid)])

        group.principals.remove(principal)

        dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.REMOVE_PRINCIPALS_FROM_GROUP)
        dual_write_handler.replicate_removed_principals([principal])

        return principal

    def _resolve_principals(self, usernames: set, service_account_client_ids: set) -> List[Principal]:
        """Resolve usernames/service account client IDs against all tenant principals."""
        return self._resolve(
            Principal.objects.filter(tenant=self.tenant).exclude(cross_account=True),
            usernames,
            service_account_client_ids,
        )

    def _resolve_member_principals(
        self, group: Group, usernames: set, service_account_client_ids: set
    ) -> List[Principal]:
        """Resolve usernames/service account client IDs against the group's current members only."""
        return self._resolve(
            group.principals.filter(tenant=self.tenant).exclude(cross_account=True),
            usernames,
            service_account_client_ids,
        )

    @staticmethod
    def _resolve(queryset: QuerySet, usernames: set, service_account_client_ids: set) -> List[Principal]:
        principals = []
        missing = []

        if usernames:
            found = list(queryset.filter(type=Principal.Types.USER, username__in=usernames))
            missing.extend(usernames - {p.username for p in found})
            principals.extend(found)

        if service_account_client_ids:
            found_sa = list(
                queryset.filter(
                    type=Principal.Types.SERVICE_ACCOUNT, service_account_id__in=service_account_client_ids
                )
            )
            missing.extend(service_account_client_ids - {p.service_account_id for p in found_sa})
            principals.extend(found_sa)

        if missing:
            raise PrincipalNotFoundError(missing)
        return principals

    def _ordering(self, order_by: str) -> tuple[str, ...]:
        """Translate an API order_by value into ORM ordering, with a stable name/uuid tiebreaker."""
        descending = order_by.startswith("-")
        field = self.ORDER_BY_FIELD_MAPPING[order_by.lstrip("-")]
        primary = f"-{field}" if descending else field
        if field == "name":
            return (primary, "uuid")
        return (primary, "name", "uuid")

    @staticmethod
    def _check_not_protected(group: Group, flags: tuple[str, ...], action: str) -> None:
        for flag in flags:
            if getattr(group, flag):
                raise ProtectedGroupError(action, flag)

    @classmethod
    def _is_unique_name_violation(cls, error: IntegrityError) -> bool:
        """Check whether an IntegrityError was raised by the unique group name constraint.

        Inspects the underlying psycopg2 diagnostics (constraint_name) instead of matching the
        free-text error message, which is brittle across driver/locale differences.
        """
        diag = getattr(getattr(error, "__cause__", None), "diag", None)
        constraint_name = getattr(diag, "constraint_name", None)
        if constraint_name is not None:
            return constraint_name == cls.UNIQUE_NAME_CONSTRAINT
        return cls.UNIQUE_NAME_CONSTRAINT in str(error)
