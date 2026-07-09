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
"""QuerySet for RoleBinding lookups."""

import uuid as uuid_mod

from django.db.models import Case, Count, DateTimeField, F, OuterRef, Q, QuerySet, Subquery, Value, When
from django.db.models.fields import CharField, UUIDField
from django.db.models.functions import Cast
from management.subject import SubjectType


def _get_platform_type() -> str:
    """Lazily resolve the canonical platform role type to avoid a circular import."""
    from management.role.v2_model import RoleV2

    return RoleV2.Types.PLATFORM


class RoleBindingQuerySet(QuerySet):
    """Custom QuerySet for RoleBinding with domain-aware query methods."""

    def _clone(self):
        c = super()._clone()
        c._tenant = getattr(self, "_tenant", None)
        return c

    def for_tenant(self, tenant):
        """Return role bindings for a tenant with related data eagerly loaded.

        Eagerly loads related role, group/principal entries, and role children
        via ``select_related`` / ``prefetch_related``.

        Note: The ``role_created``, ``role_name``, ``role_uuid``, and
        ``role_modified`` annotations below are **not** consumed by the list
        endpoint's pagination (which uses the ``effective_role_*`` annotations
        from ``with_expanded_platform_roles()`` instead).  They are retained
        for the by-subject service path and any callers that access role
        attributes via ``getattr(instance, field)`` without going through
        the platform-role expansion layer.
        """
        qs = (
            self.filter(tenant=tenant)
            .select_related("role")
            .prefetch_related("group_entries__group", "principal_entries__principal", "role__children")
            .annotate(
                role_created=F("role__created"),
                role_name=F("role__name"),
                role_uuid=F("role__uuid"),
                role_modified=F("role__modified"),
            )
        )
        qs._tenant = tenant
        return qs

    def for_role(self, role_id):
        """Filter by role UUID."""
        return self.filter(role__uuid=role_id)

    def for_resource_filter(self, resource_type=None, resource_id=None):
        """Apply optional resource_type and resource_id filters."""
        qs = self
        if resource_id:
            qs = qs.filter(resource_id=resource_id)
        if resource_type:
            qs = qs.filter(resource_type=resource_type)
        return qs

    def for_subject(self, subject_type=None, subject_id=None):
        """Filter by subject type and/or subject ID.

        When subject_type is 'group', filters by group; when 'user', by principal.
        An unsupported subject_type returns an empty queryset.
        When subject_id is provided without subject_type, searches both groups and principals.
        """
        if subject_type == SubjectType.GROUP:
            if subject_id:
                return self.filter(group_entries__group__uuid=subject_id)
            return self.filter(group_entries__isnull=False).distinct()
        elif subject_type == SubjectType.USER:
            if subject_id:
                return self.filter(principal_entries__principal__uuid=subject_id)
            return self.filter(principal_entries__isnull=False).distinct()
        elif subject_type:
            return self.none()
        elif subject_id:
            return self.filter(
                Q(group_entries__group__uuid=subject_id) | Q(principal_entries__principal__uuid=subject_id)
            ).distinct()
        return self

    def for_granted_subject(
        self, granted_subject_type, granted_subject_id=None, granted_subject_principal_user_id=None
    ):
        """Filter by effective access grant, including transitive group membership.

        Requires for_tenant() to have been called first.

        For groups: returns bindings where the group is a direct subject.
        For users: looks up the principal (by UUID or user_id) and resolves
        their group memberships, then returns bindings where the user is a
        direct subject OR any of their groups is a subject.
        For principals: looks up by external user_id only (no UUID fallback),
        with the same membership resolution as users.
        Returns an empty queryset if the principal is not found.
        """
        tenant = getattr(self, "_tenant", None)
        if tenant is None:
            raise ValueError("for_granted_subject() requires for_tenant() to be called first")

        if granted_subject_type == SubjectType.GROUP:
            if not granted_subject_id:
                return self.none()
            return self.filter(group_entries__group__uuid=granted_subject_id)
        elif granted_subject_type == SubjectType.USER:
            if not granted_subject_id:
                return self.none()
            principal = _resolve_principal(granted_subject_id, tenant)
            if not principal:
                return self.none()
            group_uuids = _group_uuids_for_principal(principal, tenant)
            return self.filter(
                Q(principal_entries__principal__uuid=principal.uuid) | Q(group_entries__group__uuid__in=group_uuids)
            ).distinct()
        elif granted_subject_type == SubjectType.PRINCIPAL:
            if not granted_subject_principal_user_id:
                return self.none()
            principal = _resolve_principal_by_user_id(granted_subject_principal_user_id, tenant)
            if not principal:
                return self.none()
            group_uuids = _group_uuids_for_principal(principal, tenant)
            return self.filter(
                Q(principal_entries__principal__uuid=principal.uuid) | Q(group_entries__group__uuid__in=group_uuids)
            ).distinct()
        return self.none()

    def with_expanded_platform_roles(self):
        """Expand platform-role bindings into per-child-role rows at the DB level.

        For non-platform bindings: one row per binding, effective_role_* = binding's own role.
        For platform bindings: one row per child role, effective_role_* = child role's fields.
        Platform bindings with no children are excluded (zero rows).

        This must be called BEFORE pagination so that limit/offset operate on the
        expanded row count rather than the raw binding count.

        The ``F("role__children__*")`` references cause an implicit LEFT JOIN on
        the children M2M table.  Non-platform roles are short-circuited by an
        explicit ``When(~Q(...))`` clause evaluated first, so the JOIN result is
        unused for those rows.  However, if a non-platform role ever acquires
        M2M children entries (the DB schema does not prevent this), the JOIN
        could still produce duplicate rows.  ``.distinct()`` is applied as a
        safety net against that edge case.
        """
        platform_type = _get_platform_type()
        qs = (
            self.annotate(
                effective_role_uuid=Case(
                    When(~Q(role__type=platform_type), then=F("role__uuid")),
                    When(role__type=platform_type, then=F("role__children__uuid")),
                    default=F("role__uuid"),
                ),
                effective_role_name=Case(
                    When(~Q(role__type=platform_type), then=F("role__name")),
                    When(role__type=platform_type, then=F("role__children__name")),
                    default=F("role__name"),
                    output_field=CharField(),
                ),
                effective_role_created=Case(
                    When(~Q(role__type=platform_type), then=F("role__created")),
                    When(role__type=platform_type, then=F("role__children__created")),
                    default=F("role__created"),
                    output_field=DateTimeField(),
                ),
                effective_role_modified=Case(
                    When(~Q(role__type=platform_type), then=F("role__modified")),
                    When(role__type=platform_type, then=F("role__children__modified")),
                    default=F("role__modified"),
                    output_field=DateTimeField(),
                ),
            )
            .exclude(
                # Platform bindings with no children expand to a single NULL row — remove it.
                Q(role__type=platform_type)
                & Q(effective_role_uuid__isnull=True)
            )
            .distinct()
        )
        return qs

    def with_resource_names(self):
        """Annotate each binding with its resource's display name.

        Resolves workspace names via a correlated subquery so the name is
        available as ``obj.resource_name`` without per-row queries.
        Non-workspace resource types will get ``None``.

        Only applies the UUID cast for workspace resources to avoid errors
        with non-UUID resource IDs (e.g., tenant resources like "redhat/12345").
        """
        from management.workspace.model import Workspace

        return self.annotate(
            resource_name=Case(
                When(
                    resource_type="workspace",
                    then=Subquery(
                        Workspace.objects.filter(
                            id=Cast(OuterRef("resource_id"), UUIDField()),
                            tenant=OuterRef("tenant"),
                        ).values("name")[:1]
                    ),
                ),
                default=Value(None),
                output_field=CharField(),
            )
        )

    def for_resource(self, resource_type, resource_id, tenant):
        """Filter to bindings on a specific resource for a tenant."""
        return self.filter(resource_type=resource_type, resource_id=resource_id, tenant=tenant)

    def orphaned(self):
        """Filter to bindings that have no subjects (groups or principals) attached."""
        return self.annotate(
            group_count=Count("group_entries"),
            principal_count=Count("principal_entries"),
        ).filter(group_count=0, principal_count=0)


def _resolve_principal(granted_subject_id, tenant):
    """Look up a Principal by UUID, falling back to user_id."""
    from management.principal.model import Principal

    try:
        principal = Principal.objects.filter(uuid=uuid_mod.UUID(str(granted_subject_id)), tenant=tenant).first()
    except ValueError:
        principal = None
    if not principal:
        principal = Principal.objects.filter(user_id=granted_subject_id, tenant=tenant).first()
    return principal


def _resolve_principal_by_user_id(user_id, tenant):
    """Look up a Principal by external user_id only (no UUID fallback)."""
    from management.principal.model import Principal

    return Principal.objects.filter(user_id=user_id, tenant=tenant).first()


def _group_uuids_for_principal(principal, tenant):
    """Return a lazy queryset of group UUIDs for a principal (assigned + platform default)."""
    from management.group.model import Group

    return Group.objects.filter(
        Q(principals=principal) | Q(platform_default=True),
        Q(tenant=tenant) | Q(tenant__tenant_name="public"),
    ).values_list("uuid", flat=True)
