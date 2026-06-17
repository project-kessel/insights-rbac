import itertools
import logging
import time
from typing import Optional

from api.models import Tenant
from management.atomic_transactions import atomic_with_retry
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
    PartitionKey,
)
from management.tenant_service.v2 import try_lock_tenants_for_bootstrap
from management.workspace.model import Workspace
from migration_tool.utils import create_relationship

logger = logging.getLogger(__name__)


@atomic_with_retry(retries=10)
def _do_replicate(replicator: RelationReplicator, raw_tenants: list[Tenant]) -> int:
    tenants = list(Tenant.objects.filter(pk__in=(t.pk for t in raw_tenants)).exclude(org_id=None))
    bootstrap_locks = {t: l for t, l in try_lock_tenants_for_bootstrap(tenants).items() if l is not None}

    if len(tenants) != len(bootstrap_locks):
        expected_pks = set(t.pk for t in tenants)
        actual_pks = set(t.pk for t in bootstrap_locks.keys())

        raise RuntimeError(f"Failed to lock some tenants for bootstrap: pks={expected_pks - actual_pks}")

    tenants_by_id = {t.id: t for t in tenants}

    if len(tenants_by_id) != len(tenants):
        raise AssertionError("Found tenants with duplicate IDs")

    root_workspaces = list(
        Workspace.objects.filter(type=Workspace.Types.ROOT).filter(tenant__in=tenants).select_for_update()
    )

    if len(root_workspaces) != len(tenants):
        raise AssertionError("Found tenant without a root workspace")

    tuples = []

    for root_workspace in root_workspaces:
        tuples.append(
            create_relationship(
                resource_name=("rbac", "workspace"),
                resource_id=str(root_workspace.id),
                subject_name=("rbac", "tenant"),
                subject_id=tenants_by_id[root_workspace.tenant_id].tenant_resource_id(),
                relation="tenant",
            )
        )

    for tuples_batch in itertools.batched(tuples, 1000):
        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.UPDATE_ROOT_WORKSPACE_TENANTS,
                partition_key=PartitionKey.byEnvironment(),
                add=list(tuples_batch),
            )
        )

    return len(tenants)


def replicate_root_workspace_tenants(
    replicator: Optional[RelationReplicator] = None, *, batch_sleep_seconds: int | float = 0
):
    """
    Replicate the tenant relation for all existing root workspaces.

    Args:
        replicator: the replicator to use (defaulting to OutboxReplicator)
        batch_sleep_seconds: if positive, how many seconds to sleep between each "batch" (defaulting to 0)
    """
    if replicator is None:
        replicator = OutboxReplicator()

    query = Tenant.objects.exclude(org_id=None)
    tenant_count = query.count()

    logger.info(f"About to replicate root workspace -> tenant relations for ~{tenant_count} tenants.")

    replicated_count = 0

    for raw_tenant_batch in itertools.batched(query.iterator(), 1000):
        replicated_count += _do_replicate(replicator, list(raw_tenant_batch))
        logger.info(f"Replicated {replicated_count}/~{tenant_count} root workspace tenants.")

        if batch_sleep_seconds > 0:
            time.sleep(batch_sleep_seconds)

    logger.info(f"Finished replicating a total of {replicated_count} root workspace tenants.")
