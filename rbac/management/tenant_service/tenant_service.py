"""Common objects for tenant services."""

import logging
from typing import NamedTuple, Optional, Protocol

from django.db import IntegrityError, transaction
from management.atomic_transactions import atomic
from management.group.model import Group
from management.inventory_replicator.inventory_replicator import (
    InventoryReplicator,
    PartitionKey,
    ReplicationEvent,
    ReplicationEventType,
)
from management.principal.model import Principal
from management.role_binding.model import RoleBinding, RoleBindingPrincipal
from management.tenant_mapping.model import TenantMapping
from management.workspace.model import Workspace
from migration_tool.in_memory_tuples import RelationTuple

from api.models import Tenant, User

logger = logging.getLogger(__name__)


def _normalize_user_id(user_id) -> Optional[str]:
    """Normalize BOP/identity user_id values to the string form stored on Principal."""
    if user_id is None or user_id == "":
        return None
    return str(user_id)


def _is_missing_user_id(user_id) -> bool:
    return _normalize_user_id(user_id) is None


def _transfer_role_binding_entries(obsolete: Principal, survivor: Principal) -> None:
    """Move direct role-binding entries from obsolete to survivor."""
    binding_ids = set(obsolete.role_binding_entries.values_list("binding_id", flat=True))
    if not binding_ids:
        return

    # Lock the bindings and evaluate; only consider entries from locked bindings
    # to avoid picking up new bindings committed by concurrent transactions.
    locked_binding_ids = set(
        RoleBinding.objects.select_for_update().filter(pk__in=binding_ids).values_list("pk", flat=True)
    )
    entries = list(
        RoleBindingPrincipal.objects.select_related("binding").filter(
            principal=obsolete, binding_id__in=locked_binding_ids
        )
    )

    for entry in entries:
        RoleBindingPrincipal.objects.get_or_create(
            binding=entry.binding,
            principal=survivor,
            source=entry.source,
        )
        entry.delete()


def _transfer_group_memberships(
    obsolete: Principal,
    survivor: Principal,
    obsolete_groups: list[Group],
) -> list[RelationTuple]:
    """Transfer group memberships and return SpiceDB remove tuples for the obsolete principal."""
    tuples_to_remove: list[RelationTuple] = []
    for group in obsolete_groups:
        if obsolete.user_id:
            tuples_to_remove.append(Group.relationship_to_user_id_for_group(str(group.uuid), obsolete.user_id))
        group.principals.add(survivor)
        group.principals.remove(obsolete)
    return tuples_to_remove


def _assign_user_id_and_replicate_merge(
    survivor: Principal,
    obsolete_username: str,
    tuples_to_remove: list[RelationTuple],
    user_id: str,
    replicator: InventoryReplicator,
) -> None:
    survivor.user_id = user_id
    survivor.save()

    tuples_to_add = _group_member_tuples_for_principal(survivor)

    if tuples_to_add or tuples_to_remove:
        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.ADD_PRINCIPALS_TO_GROUP,
                info={
                    "org_id": str(survivor.tenant.org_id),
                    "merged_from_username": obsolete_username,
                    "merged_into_username": survivor.username,
                    "user_id": user_id,
                },
                partition_key=PartitionKey.byEnvironment(),
                add=tuples_to_add,
                remove=tuples_to_remove,
            )
        )


@atomic
def merge_obsolete_principal_into_survivor(
    survivor: Principal,
    obsolete: Principal,
    user_id: str,
    replicator: InventoryReplicator,
) -> None:
    """
    Merge an older principal (has user_id) into the current principal (no user_id).

    The survivor is typically the latest BOP username: it could not receive user_id due to the
    global uniqueness constraint, but may already have been added to groups. Group memberships
    and direct role-binding entries from the obsolete principal are transferred to the survivor
    when both principals are in the same tenant. user_id is assigned to the survivor and the
    obsolete principal is deleted.

    Raises RuntimeError if the principals belong to different tenants.
    """
    if survivor.pk == obsolete.pk:
        return

    if survivor.tenant_id != obsolete.tenant_id:
        raise RuntimeError(
            f"Refusing cross-tenant principal merge. "
            f"survivor_id={survivor.pk} survivor_username={survivor.username} "
            f"obsolete_id={obsolete.pk} obsolete_username={obsolete.username} "
            f"user_id={user_id} org_id={survivor.tenant.org_id}"
        )

    obsolete_groups = list(obsolete.group.all())
    obsolete_username = obsolete.username

    tuples_to_remove = _transfer_group_memberships(obsolete, survivor, obsolete_groups)
    _transfer_role_binding_entries(obsolete, survivor)
    obsolete.delete()
    _assign_user_id_and_replicate_merge(
        survivor,
        obsolete_username,
        tuples_to_remove,
        user_id,
        replicator,
    )

    if logger.isEnabledFor(logging.INFO):
        logger.info(
            "Merged obsolete principal into survivor. obsolete_username=%s survivor_username=%s user_id=%s groups=%d",
            obsolete_username,
            survivor.username,
            user_id,
            survivor.group.count(),
        )


def _group_member_tuples_for_principal(principal: Principal) -> list:
    tuples_to_add = []
    for group in principal.group.all():
        member_tuple = group.relationship_to_principal(principal)
        if member_tuple is not None:
            tuples_to_add.append(member_tuple)
    return tuples_to_add


def _group_member_tuples_for_user_id(principal: Principal, user_id: str) -> list[RelationTuple]:
    """Build group#member tuples for principal's current groups using an explicit user_id."""
    return [Group.relationship_to_user_id_for_group(str(group.uuid), user_id) for group in principal.group.all()]


def _role_binding_subject_tuples_for_user_id(principal: Principal, user_id: str) -> list[RelationTuple]:
    """Build role_binding#subject tuples for principal's direct bindings using an explicit user_id."""
    # Build via temporary user_id so RoleBinding._user_subject_tuple stays the single source of truth.
    original_user_id = principal.user_id
    principal.user_id = user_id
    try:
        binding_ids_seen: set[int] = set()
        tuples: list[RelationTuple] = []
        entries = RoleBindingPrincipal.objects.filter(principal=principal).select_related("binding")
        for entry in entries:
            if entry.binding_id in binding_ids_seen:
                continue
            binding_ids_seen.add(entry.binding_id)
            tuples.append(entry.binding._user_subject_tuple(principal))
        return tuples
    finally:
        principal.user_id = original_user_id


def _relation_tuples_for_principal_user_id(principal: Principal, user_id: str) -> list[RelationTuple]:
    """Group membership and direct role-binding subject tuples for a principal as user_id."""
    return _group_member_tuples_for_user_id(principal, user_id) + _role_binding_subject_tuples_for_user_id(
        principal, user_id
    )


@transaction.atomic
def rewrite_principal_user_id(
    principal: Principal,
    new_user_id: str,
    replicator: InventoryReplicator,
) -> None:
    """
    Rewrite principal.user_id in place and replicate SpiceDB tuple changes.

    Removes group#member and direct role_binding#subject tuples for the old user_id, updates
    the principal row, then adds the same relations for the new user_id. If another principal
    already owns new_user_id, that obsolete principal is merged into this one first (groups and
    direct role-binding entries transferred, obsolete deleted).

    Raises:
        ValueError: if new_user_id is missing, or principal has no existing user_id to rewrite.
        RuntimeError: if the owner of new_user_id is in a different tenant.
    """
    normalized_new = _normalize_user_id(new_user_id)
    if normalized_new is None:
        raise ValueError(f"Cannot rewrite principal without new user_id. principal_id={principal.pk}")

    old_user_id = _normalize_user_id(principal.user_id)
    if old_user_id is None:
        raise ValueError(
            f"Cannot rewrite principal with missing user_id; assign instead. "
            f"principal_id={principal.pk} username={principal.username}"
        )
    if old_user_id == normalized_new:
        return

    # Collect removes for the old id while the principal still has its current memberships.
    tuples_to_remove = _relation_tuples_for_principal_user_id(principal, old_user_id)

    obsolete = Principal.objects.filter(user_id=normalized_new).exclude(pk=principal.pk).first()
    obsolete_username = None
    if obsolete is not None:
        if obsolete.tenant_id != principal.tenant_id:
            raise RuntimeError(
                f"Refusing cross-tenant principal user_id rewrite/merge. "
                f"survivor_id={principal.pk} survivor_username={principal.username} "
                f"obsolete_id={obsolete.pk} obsolete_username={obsolete.username} "
                f"old_user_id={old_user_id} new_user_id={normalized_new} "
                f"org_id={principal.tenant.org_id}"
            )
        obsolete_username = obsolete.username
        obsolete_groups = list(obsolete.group.all())
        tuples_to_remove.extend(_transfer_group_memberships(obsolete, principal, obsolete_groups))
        _transfer_role_binding_entries(obsolete, principal)
        logger.warning(
            "Deleting obsolete principal during user_id rewrite. "
            "obsolete_username=%s obsolete_pk=%d survivor_username=%s new_user_id=%s org_id=%s",
            obsolete.username,
            obsolete.pk,
            principal.username,
            normalized_new,
            principal.tenant.org_id,
        )
        obsolete.delete()

    try:
        with transaction.atomic():
            principal.user_id = normalized_new
            principal.save()
    except IntegrityError:
        _resolve_user_id_conflict(principal, normalized_new, replicator)
        return

    tuples_to_add = _relation_tuples_for_principal_user_id(principal, normalized_new)

    # Default-group tuples are managed via TenantMapping in SpiceDB, not through the
    # Django M2M relation, so _relation_tuples_for_principal_user_id does not cover them.
    try:
        mapping = TenantMapping.objects.get(tenant=principal.tenant)
        m2m_group_uuids = set(principal.group.values_list("uuid", flat=True))
        # Every user is a member of the platform-default group — always transition.
        if mapping.default_group_uuid not in m2m_group_uuids:
            tuples_to_remove.append(
                Group.relationship_to_user_id_for_group(str(mapping.default_group_uuid), old_user_id)
            )
            tuples_to_add.append(
                Group.relationship_to_user_id_for_group(str(mapping.default_group_uuid), normalized_new)
            )
        # Admin-default group: always remove the old user_id tuple, but do NOT add the
        # new one.  Admin status is unavailable at this level; the caller's
        # _default_group_tuple_edits (V2 sync path) handles admin-group membership
        # correctly based on user.admin.  Not adding the tuple here prevents granting
        # admin-group access to non-admin users through a user_id rewrite.
        if mapping.default_admin_group_uuid not in m2m_group_uuids:
            tuples_to_remove.append(
                Group.relationship_to_user_id_for_group(str(mapping.default_admin_group_uuid), old_user_id)
            )
    except TenantMapping.DoesNotExist:
        pass

    # Deduplicate: when the obsolete principal shared groups with this principal under the
    # same user_id, the same tuple appears in both add and remove lists.  These cancel out
    # (the SpiceDB tuple already exists and should stay) — strip them so the consumer never
    # sees a conflicting add+remove for the same relation.
    common = set(tuples_to_add) & set(tuples_to_remove)
    if common:
        tuples_to_add = [t for t in tuples_to_add if t not in common]
        tuples_to_remove = [t for t in tuples_to_remove if t not in common]

    if tuples_to_add or tuples_to_remove:
        info = {
            "org_id": str(principal.tenant.org_id),
            "username": principal.username,
            "old_user_id": old_user_id,
            "user_id": normalized_new,
        }
        if obsolete_username is not None:
            info["merged_from_username"] = obsolete_username
        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.EXTERNAL_USER_UPDATE,
                info=info,
                partition_key=PartitionKey.byEnvironment(),
                add=tuples_to_add,
                remove=tuples_to_remove,
            )
        )

    if logger.isEnabledFor(logging.INFO):
        logger.info(
            "Rewrote principal user_id. username=%s old_user_id=%s new_user_id=%s "
            "merged_from_username=%s groups=%d",
            principal.username,
            old_user_id,
            normalized_new,
            obsolete_username,
            principal.group.count(),
        )


def _resolve_user_id_conflict(
    survivor: Principal,
    user_id: str,
    replicator: InventoryReplicator,
) -> None:
    """Resolve a user_id uniqueness conflict after a failed principal save.

    Refreshes the survivor from the database (callers may have mutated user_id in memory
    before the IntegrityError), then either rewrites the survivor's stale user_id or merges
    the obsolete owner of user_id into the survivor.

    Raises:
        ValueError: if user_id is missing.
        RuntimeError: if the conflict owner cannot be found and the survivor does not hold
            the target user_id.
    """
    normalized_user_id = _normalize_user_id(user_id)
    if normalized_user_id is None:
        raise ValueError(f"Cannot resolve user_id conflict without user_id. survivor_id={survivor.pk}")

    # Always use persisted state: callers may have set survivor.user_id in memory before a
    # failed save() (IntegrityError), which would make rewrite see old==new and no-op.
    survivor.refresh_from_db()

    obsolete = Principal.objects.filter(user_id=normalized_user_id).exclude(pk=survivor.pk).first()
    if obsolete is None:
        if survivor.user_id == normalized_user_id:
            return
        raise RuntimeError(
            f"user_id={normalized_user_id} conflict but no owner found and survivor does not have it. "
            f"survivor_id={survivor.pk} survivor_username={survivor.username}"
        )

    survivor_user_id = _normalize_user_id(survivor.user_id)
    if survivor_user_id is not None and survivor_user_id != normalized_user_id:
        # Survivor already has a different user_id; rewrite so old SpiceDB tuples are removed.
        rewrite_principal_user_id(survivor, normalized_user_id, replicator)
        return
    merge_obsolete_principal_into_survivor(survivor, obsolete, user_id=normalized_user_id, replicator=replicator)


def _ensure_principal_with_user_id_in_tenant(
    user: User,
    tenant: Tenant,
    upsert: bool = False,
    *,
    replicator: InventoryReplicator,
):
    """Ensure a principal exists for user in tenant with the correct user_id.

    Handles four scenarios: creation (upsert), assignment of a missing user_id, in-place
    rewrite of a mismatched (stale) user_id, and merge when the target user_id belongs to an
    obsolete principal in the same tenant. IntegrityError on save is caught and delegated to
    _resolve_user_id_conflict.
    """
    created = False
    principal = None

    user_id = _normalize_user_id(user.user_id)

    if upsert:
        try:
            defaults = {"user_id": user_id} if user_id is not None else {}
            principal, created = Principal.objects.get_or_create(
                username=user.username,
                tenant=tenant,
                defaults=defaults,
            )
        except IntegrityError:
            if user_id is None:
                raise
            survivor, _ = Principal.objects.get_or_create(username=user.username, tenant=tenant)
            _resolve_user_id_conflict(survivor, user_id, replicator)
            return
    else:
        try:
            principal = Principal.objects.get(username=user.username, tenant=tenant)
        except Principal.DoesNotExist:
            pass
        except Principal.MultipleObjectsReturned:
            logger.warning(
                f"Multiple principals returned for the same username. username={user.username} org_id={tenant.org_id}"
            )

    if created or principal is None:
        return

    if user_id is None:
        return

    if principal.user_id == user_id:
        return

    if not _is_missing_user_id(principal.user_id):
        # Same username with a conflicting non-empty user_id is unexpected data corruption.
        # Do not auto-rewrite on the Kafka/bootstrap ensure path; remediate via the internal
        # bootstrap_users_from_user_ids utility (one-off rewrite).
        raise RuntimeError(
            f"Principal user_id does not match BOP user_id. "
            f"username={principal.username} principal_user_id={principal.user_id} "
            f"bop_user_id={user_id} org_id={tenant.org_id}"
        )

    obsolete = Principal.objects.filter(user_id=user_id, tenant=tenant).exclude(pk=principal.pk).first()
    if obsolete is not None:
        merge_obsolete_principal_into_survivor(principal, obsolete, user_id=user_id, replicator=replicator)
        return

    principal.user_id = user_id
    try:
        with transaction.atomic():
            principal.save()
    except IntegrityError:
        _resolve_user_id_conflict(principal, user_id, replicator)


class BootstrappedTenant(NamedTuple):
    """Tenant information."""

    tenant: Tenant
    mapping: Optional[TenantMapping]
    default_workspace: Optional[Workspace] = None
    root_workspace: Optional[Workspace] = None


class TenantBootstrapService(Protocol):
    """Service for bootstrapping users in tenants."""

    def update_user(
        self,
        user: User,
        upsert: bool = False,
        bootstrapped_tenant: Optional[BootstrappedTenant] = None,
        ready_tenant: bool = True,
    ) -> Optional[BootstrappedTenant]:
        """Bootstrap a user in a tenant."""
        ...

    def new_bootstrapped_tenant(self, org_id: str, account_number: Optional[str] = None) -> BootstrappedTenant:
        """Create a new tenant."""
        ...
