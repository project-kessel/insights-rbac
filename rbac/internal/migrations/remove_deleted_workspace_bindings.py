import itertools
from typing import Optional

from django.conf import settings
from django.db.models import Exists, OuterRef, UUIDField
from django.db.models.functions import Cast

from api.models import Tenant
from management.atomic_transactions import atomic, atomic_with_retry
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
    PartitionKey,
)
from management.role_binding.model import RoleBinding
from management.tenant_service.v2 import lock_tenant_for_bootstrap
from management.workspace.model import Workspace


@atomic_with_retry(retries=3)
def _handle_role_binding_batch(tenant: Tenant, raw_bindings: list[RoleBinding], replicator: RelationReplicator):
    bootstrap_lock = lock_tenant_for_bootstrap(tenant)

    # Exclude any default access bindings here due to paranoia, even though they should never end up referencing a
    # deleted workspace.
    #
    # The workspaces subquery here synchronizes with workspaces being created in a SERIALIZABLE transaction in
    # WorkspaceService.
    role_bindings = list(
        RoleBinding.objects.filter(pk__in=(b.pk for b in raw_bindings))
        .filter(tenant=tenant)
        .filter(resource_type="workspace")
        .filter(~Exists(Workspace.objects.filter(id=Cast(OuterRef("resource_id"), UUIDField()))))
        .exclude(uuid__in=bootstrap_lock.tenant_mapping.role_binding_ids())
        .prefetch_related("group_entries", "principal_entries")
        .select_for_update()
    )

    for tuples_batch in itertools.batched(
        itertools.chain.from_iterable(rb.all_tuples() for rb in role_bindings), 1000
    ):
        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.REMOVE_DELETED_WORKSPACE_BINDINGS,
                partition_key=PartitionKey.byEnvironment(),
                remove=list(tuples_batch),
                info={"org_id": tenant.org_id},
            )
        )

    RoleBinding.objects.filter(pk__in=(rb.pk for rb in role_bindings)).delete()


def remove_deleted_workspace_bindings(replicator: Optional[RelationReplicator] = None):
    """Remove RoleBindings that are bound to a workspace that no longer exists."""
    if replicator is None:
        replicator = OutboxReplicator()

    if not settings.REPLICATION_TO_RELATION_ENABLED:
        raise RuntimeError("Cannot remove bindings without replicating.")

    missing_workspace_bindings = (
        RoleBinding.objects.filter(resource_type="workspace")
        .filter(~Exists(Workspace.objects.filter(id=Cast(OuterRef("resource_id"), UUIDField()))))
        .order_by("tenant")
    )

    for binding_batch in itertools.batched(missing_workspace_bindings.iterator(), 1000):
        for tenant, tenant_bindings in itertools.groupby(binding_batch, lambda w: w.tenant):
            _handle_role_binding_batch(tenant=tenant, raw_bindings=list(tenant_bindings), replicator=replicator)

    # There are not currently any BindingMappings that reference deleted workspaces, so we don't have to handle them
    # here.
