import itertools
import logging
from typing import Optional

from django.db.models import Q, UUIDField, F
from django.db.models.functions import Cast
from django.db.models.lookups import In

from api.models import Tenant
from management.atomic_transactions import atomic, atomic_with_retry
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
    PartitionKey,
)
from management.role.model import BindingMapping
from management.role.v2_model import RoleV2, CustomRoleV2
from management.role_binding.model import RoleBinding
from management.tenant_mapping.v2_activation import lock_tenant_version, TenantVersion
from management.tenant_service.v2 import lock_tenant_for_bootstrap
from management.workspace.model import Workspace
from migration_tool.migrate_binding_scope import migrate_all_role_bindings

logger = logging.getLogger(__name__)


@atomic
def _do_tear_down_tenant(tenant: Tenant, replicator: RelationReplicator):
    tenant_resource_id = tenant.tenant_resource_id()

    if tenant_resource_id is None:
        raise ValueError(f"Expected tenant to have resource ID; pk={tenant.pk!r}")

    tenant_lock = lock_tenant_for_bootstrap(tenant)

    role_bindings = list(
        RoleBinding.objects.filter(tenant=tenant)
        .exclude(uuid__in=tenant_lock.tenant_mapping.role_binding_ids())
        .prefetch_related("role", "role__permissions", "principal_entries", "group_entries")
        .select_for_update(of=["self"])
    )

    binding_mapping_predicate = Q(
        resource_type_namespace="rbac",
        resource_type_name="tenant",
        resource_id=tenant_resource_id,
    ) | (
        Q(
            resource_type_namespace="rbac",
            resource_type_name="workspace",
        )
        & In(Cast(F("resource_id"), UUIDField()), Workspace.objects.filter(tenant=tenant).values("id"))
    )

    if len(role_bindings) > 0:
        binding_mapping_predicate = binding_mapping_predicate | Q(
            mappings__id__in=(str(rb.uuid) for rb in role_bindings)
        )

    binding_mappings = list(BindingMapping.objects.filter(binding_mapping_predicate).select_for_update())

    v2_roles = list(
        CustomRoleV2.objects.filter(tenant=tenant)
        .prefetch_related("tenant", "permissions")
        .select_for_update(of=["self"])
    )

    logger.info(
        f"Found {len(binding_mappings)} BindingMappings, {len(role_bindings)} RoleBindings, and "
        f"{len(v2_roles)} custom RoleV2s in tenant with pk={tenant.pk!r}. "
        f"Deleting them in order to recompute all bindings."
    )

    tuples_to_remove = {
        *itertools.chain.from_iterable(bm.as_tuples() for bm in binding_mappings),
        *itertools.chain.from_iterable(rb.all_tuples() for rb in role_bindings),
        *itertools.chain.from_iterable(RoleV2.tuples_for_delete(r) for r in v2_roles),
    }

    for batch in itertools.batched(tuples_to_remove, 1000):
        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.REMIGRATE_ROLE_BINDING,
                partition_key=PartitionKey.byEnvironment(),
                remove=list(batch),
                info={"org_id": tenant.org_id},
            )
        )

    RoleBinding.objects.filter(pk__in=(rb.pk for rb in role_bindings)).delete()
    BindingMapping.objects.filter(pk__in=(bm.pk for bm in binding_mappings)).delete()
    CustomRoleV2.objects.filter(pk__in=(r.pk for r in v2_roles)).delete()


@atomic_with_retry(retries=3)
def _do_recreate_bindings(tenant: Tenant, replicator: RelationReplicator):
    tenant = Tenant.objects.get(pk=tenant.pk)
    tenant_version = lock_tenant_version(tenant)

    if tenant_version != TenantVersion.VERSION_1:
        logger.info(f"Not recreating role bindings for non-V1 tenant: pk={tenant.pk!r}")
        return

    _do_tear_down_tenant(tenant, replicator)
    migrate_all_role_bindings(replicator=replicator, tenant=tenant)


def recompute_tenant_role_bindings(tenant: Tenant, replicator: Optional[RelationReplicator] = None):
    """Recompute all BindingMappings and RoleBindings for a V1 tenant."""
    if replicator is None:
        replicator = OutboxReplicator()

    if tenant.tenant_name == "public":
        raise ValueError("Cannot recompute bindings for the public tenant")

    logger.info(f"Recomputing bindings for tenant: pk={tenant.pk!r}, org_id={tenant.org_id!r}")

    _do_recreate_bindings(tenant, replicator)
