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

"""Models for role binding management."""

from typing import Iterable, Optional

from django.db import models, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from management.group.model import Group
from management.principal.model import Principal
from management.relation_replicator.types import ObjectReference, ObjectType, RelationTuple, SubjectReference
from management.role_binding.queryset import RoleBindingQuerySet
from migration_tool.models import V2boundresource, V2rolebinding
from uuid_utils.compat import UUID, uuid7

from api.models import TenantAwareModel


class RoleBinding(TenantAwareModel):
    """A role binding."""

    objects = RoleBindingQuerySet.as_manager()

    uuid = models.UUIDField(default=uuid7, editable=False, unique=True, null=False)
    role = models.ForeignKey("management.RoleV2", on_delete=models.CASCADE, related_name="bindings")

    resource_type = models.CharField(max_length=256, null=False)
    resource_id = models.CharField(max_length=256, null=False)

    # ── Relation tuple generation ────────────────────────────────────
    #
    # These methods produce ``RelationTuple`` objects that mirror the
    # SpiceDB relationships for this binding.  They are **pure data
    # transformations** — no DB writes.  The service layer collects
    # these tuples and sends them via the ``OutboxReplicator``.

    def _resource_type_pair(self) -> tuple[str, str]:
        """Return the (namespace, name) pair for this binding's resource type.

        Convention: resource_type stored as ``"workspace"`` maps to
        ``("rbac", "workspace")`` in the relations graph.
        """
        return ("rbac", self.resource_type)

    def _role_relation_tuple(self) -> RelationTuple:
        """``rbac/role_binding:<uuid>#role@rbac/role:<role_uuid>``."""
        return RelationTuple(
            resource=ObjectReference(type=ObjectType(namespace="rbac", name="role_binding"), id=str(self.uuid)),
            relation="role",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="role"), id=str(self.role.uuid)),
            ),
        )

    def _resource_binding_tuple(self) -> RelationTuple:
        """``rbac/<resource_type>:<resource_id>#binding@rbac/role_binding:<uuid>``."""
        ns, name = self._resource_type_pair()
        return RelationTuple(
            resource=ObjectReference(type=ObjectType(namespace=ns, name=name), id=self.resource_id),
            relation="binding",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="role_binding"), id=str(self.uuid)),
            ),
        )

    def _group_subject_tuple(self, group: "Group") -> RelationTuple:
        """``rbac/role_binding:<uuid>#subject@rbac/group:<group_uuid>[#member]``."""
        return RelationTuple(
            resource=ObjectReference(type=ObjectType(namespace="rbac", name="role_binding"), id=str(self.uuid)),
            relation="subject",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="group"), id=str(group.uuid)),
                relation="member",
            ),
        )

    def _user_subject_tuple(self, principal: "Principal") -> RelationTuple:
        """``rbac/role_binding:<uuid>#subject@rbac/principal:<principal_resource_id>``."""
        principal_resource_id = Principal.user_id_to_principal_resource_id(principal.user_id)
        return RelationTuple(
            resource=ObjectReference(type=ObjectType(namespace="rbac", name="role_binding"), id=str(self.uuid)),
            relation="subject",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="principal"), id=principal_resource_id),
            ),
        )

    def binding_tuples(self) -> list[RelationTuple]:
        """Return the two binding-level tuples (role + resource).

        These are the tuples that should be added when a binding is created
        and removed when a binding is deleted.
        """
        return [self._role_relation_tuple(), self._resource_binding_tuple()]

    def all_tuples(self) -> list[RelationTuple]:
        """Return the complete set of SpiceDB tuples for this binding.

        This is a full snapshot (role, resource, and all subject tuples)
        derived from the current DB state -- not a diff.  Used as a
        building block by ``CustomRoleV2.replication_tuples`` when entire
        bindings are being torn down.

        Expects ``group_entries`` and ``principal_entries`` to be prefetched
        (or accepts the extra queries if they are not).
        """
        tuples = self.binding_tuples()
        for entry in self.group_entries.all():
            tuples.append(self._group_subject_tuple(entry.group))
        for principal in set(e.principal for e in self.principal_entries.all()):
            tuples.append(self._user_subject_tuple(principal))
        return tuples

    def subject_tuple(self, subject: "Group | Principal") -> RelationTuple:
        """Return the subject tuple for this binding and the given subject.

        Dispatches to ``group_subject_tuple`` or ``user_subject_tuple``
        based on the subject's type.
        """
        if isinstance(subject, Group):
            return self._group_subject_tuple(subject)
        return self._user_subject_tuple(subject)

    @staticmethod
    def replication_tuples(
        subject: "Group | Principal",
        bindings_created: Iterable["RoleBinding"] = (),
        bindings_deleted: Iterable["RoleBinding"] = (),
        subject_linked_to: Iterable["RoleBinding"] = (),
        subject_unlinked_from: Iterable["RoleBinding"] = (),
    ) -> tuple[list[RelationTuple], list[RelationTuple]]:
        """Compute the delta (tuples to add vs. remove) for a role-binding changeset.

        Unlike ``all_tuples``, which returns a full snapshot for a single
        binding, this method computes the minimal diff across multiple
        bindings for a given subject.

        Pure data transformation — no DB writes.  The service calls this
        once after performing the DB mutations and passes the result to the
        outbox replicator.

        Args:
            subject: The group or principal being updated.
            bindings_created: Newly created RoleBinding instances.
            bindings_deleted: Orphaned RoleBinding instances that were deleted.
            subject_linked_to: Bindings the subject was linked to (added).
            subject_unlinked_from: Bindings the subject was unlinked from (removed).

        Returns:
            ``(tuples_to_add, tuples_to_remove)`` ready for replication.
        """
        tuples_to_add: list[RelationTuple] = []
        tuples_to_remove: list[RelationTuple] = []

        # New bindings: role + resource binding tuples
        for binding in bindings_created:
            tuples_to_add.extend(binding.binding_tuples())

        # Deleted (orphaned) bindings: role + resource binding tuples
        for binding in bindings_deleted:
            tuples_to_remove.extend(binding.binding_tuples())

        # Subject linked: subject tuple per binding
        for binding in subject_linked_to:
            tuples_to_add.append(binding.subject_tuple(subject))

        # Subject unlinked: subject tuple per binding
        for binding in subject_unlinked_from:
            tuples_to_remove.append(binding.subject_tuple(subject))

        return tuples_to_add, tuples_to_remove

    def bound_groups(self) -> QuerySet:
        """Get a QuerySet for all groups bound to this RoleBinding."""
        return Group.objects.filter(role_binding_entries__in=self.group_entries.all())

    def update_groups(self, groups: Iterable[Group]):
        """Update the groups bound to this RoleBinding."""
        with transaction.atomic():
            self.group_entries.all().delete()
            RoleBindingGroup.objects.bulk_create([RoleBindingGroup(binding=self, group=g) for g in set(groups)])

    def update_groups_by_uuid(self, uuids: Iterable[UUID | str]):
        """
        Update the groups bound to this RoleBinding by UUID.

        Raises a ValueError if one of the UUIDs cannot be found.
        """
        uuids = set(str(u) for u in uuids)

        groups = Group.objects.filter(uuid__in=uuids).only("id", "uuid")
        found_uuids = {str(g.uuid) for g in groups}

        if found_uuids != uuids:
            missing_uuids = uuids.difference(found_uuids)
            raise ValueError(f"Not all expected groups could be found. Missing UUIDs: {missing_uuids}")

        # Group.uuid is unique, so at most one Group will be found per UUID, and len(groups) <= len(uuids).
        # By construction, len(found_uuids) <= len(groups).
        # We have just checked that found_uuids = uuids, so len(found_uuids) = len(uuids) <= len(groups) <= len(uuids).
        # Thus, len(groups) = len(uuids), and we have found one group for each specified UUID.

        self.update_groups(groups)

    def bound_principals(self) -> QuerySet:
        """Get a QuerySet for all principals bound to this RoleBinding."""
        return Principal.objects.filter(role_binding_entries__in=self.principal_entries.all()).distinct()

    def update_principals(self, principals_by_source: Iterable[tuple[str, Principal]]):
        """
        Update the principals bound to this RoleBinding.

        principals_by_source is an iterable of pairs of the source string and the principal added from that source.
        """
        with transaction.atomic():
            self.principal_entries.all().delete()

            RoleBindingPrincipal.objects.bulk_create(
                [RoleBindingPrincipal(binding=self, principal=p, source=s) for s, p in set(principals_by_source)]
            )

    def update_principals_by_user_id(self, user_ids_by_source: Iterable[tuple[str, str]]):
        """
        Update the principals bound to this RoleBinding by user_id.

        Args:
            user_ids_by_source: An iterable of (source, user_id) pairs identifying
                each principal and the source it was added from.

        Raises:
            TypeError: If any user_id is None.
            ValueError: If any user_id cannot be found.
        """
        user_ids_by_source = set(user_ids_by_source)
        user_ids = set(entry[1] for entry in user_ids_by_source)

        if None in user_ids:
            raise TypeError("None user IDs are not supported.")

        principals = Principal.objects.filter(user_id__in=user_ids)
        found_user_ids: set[str] = {p.user_id for p in principals}

        if found_user_ids != user_ids:
            missing_user_ids = user_ids.difference(found_user_ids)
            raise ValueError(f"Not all expected principals could be found. Missing user IDs: {missing_user_ids}")

        # Principal.user_id is unique, so at most one Principal will be found per user ID, and we have:
        #   len(principals) <= len(user_ids).
        # By construction, len(found_user_ids) <= len(principals).
        # We have just checked that found_user_ids = user_ids, so we have:
        #   len(found_user_ids) = len(user_ids) <= len(principals) <= len(user_ids).
        # Thus, len(user_ids) = len(principals), and we have found one group for each specified UUID.

        principals_by_id = {p.user_id: p for p in principals}
        self.update_principals((s, principals_by_id[u]) for s, u in user_ids_by_source)

    def as_migration_value(self, force_group_uuids: Optional[list[str]] = None) -> V2rolebinding:
        """Return the V2rolebinding equivalent of this role binding.

        Args:
            force_group_uuids: If provided, use these group UUIDs instead of
                querying ``bound_groups()`` from the database. This is useful
                when the caller already knows the group membership (e.g. during
                migration) and wants to avoid an extra query.
        """
        if force_group_uuids is None:
            force_group_uuids = [str(u) for u in self.bound_groups().values_list("uuid", flat=True)]

        return V2rolebinding(
            id=str(self.uuid),
            role=self.role.as_migration_value(),
            resource=V2boundresource(
                # TODO: we currently assume all resources types are in namespace "rbac". This is currently true for
                #  all the types we care about, but is not necessarily true in general. The semantics of the
                #  Inventory API (which we will eventually have to migrate to) are different and do not have a
                #  resource type namespace, per se.
                resource_type=("rbac", self.resource_type),
                resource_id=self.resource_id,
            ),
            groups=force_group_uuids,
            users={},
        )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["role", "resource_type", "resource_id", "tenant"],
                name="unique role binding per role resource pair per tenant",
            ),
        ]


class RoleBindingGroup(models.Model):
    """The relationship between a RoleBinding and one of its group subjects."""

    group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name="role_binding_entries")
    binding = models.ForeignKey(RoleBinding, on_delete=models.CASCADE, related_name="group_entries")
    created = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "binding"], name="unique group binding pair")]


class RoleBindingPrincipal(models.Model):
    """The relationship between a RoleBinding and one of its principal subjects."""

    principal = models.ForeignKey(Principal, on_delete=models.PROTECT, related_name="role_binding_entries")
    binding = models.ForeignKey(RoleBinding, on_delete=models.CASCADE, related_name="principal_entries")
    source = models.CharField(max_length=128, null=False)
    created = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["principal", "binding", "source"], name="unique principal binding source triple"
            ),
            models.CheckConstraint(condition=~Q(source=""), name="role binding principal has source"),
        ]
