"""Common objects for tenant services."""

import logging
from typing import NamedTuple, Optional, Protocol

from django.db import IntegrityError
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


def _resolve_user_id_conflict(
    survivor: Principal,
    user_id: str,
    replicator: InventoryReplicator,
) -> None:
    normalized_user_id = _normalize_user_id(user_id)
    if normalized_user_id is None:
        raise ValueError(f"Cannot resolve user_id conflict without user_id. survivor_id={survivor.pk}")

    obsolete = Principal.objects.filter(user_id=normalized_user_id).exclude(pk=survivor.pk).first()
    if obsolete is None:
        survivor.refresh_from_db()
        if survivor.user_id == normalized_user_id:
            return
        raise RuntimeError(
            f"user_id={normalized_user_id} conflict but no owner found and survivor does not have it. "
            f"survivor_id={survivor.pk} survivor_username={survivor.username}"
        )
    merge_obsolete_principal_into_survivor(survivor, obsolete, user_id=normalized_user_id, replicator=replicator)


def _ensure_principal_with_user_id_in_tenant(
    user: User,
    tenant: Tenant,
    upsert: bool = False,
    *,
    replicator: InventoryReplicator,
):
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
