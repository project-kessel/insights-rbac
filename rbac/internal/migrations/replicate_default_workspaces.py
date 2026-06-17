import dataclasses
import itertools
import logging
import uuid
from typing import Optional

from django.db.models import QuerySet
from management.atomic_transactions import atomic_with_retry
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    ReplicationEventType,
    WorkspaceEventStream,
)
from management.workspace.model import Workspace
from management.workspace.utils.event import make_workspace_event


@atomic_with_retry(retries=20)
def _do_replicate_batch(replicator: RelationReplicator, raw_workspaces: list[Workspace]) -> int:
    """Replicates a batch of workspaces and returns the number actually replicated."""
    if len(raw_workspaces) == 0:
        return 0

    workspaces = list(
        Workspace.objects.filter(type=Workspace.Types.DEFAULT)
        .filter(pk__in=(w.pk for w in raw_workspaces))
        .select_related("tenant")
        .select_for_update(of=["self"])
    )

    for workspace in workspaces:
        # We can unconditionally replicate a create event, since the HBI consumer will ignore any duplicate workspaces.
        #
        # If a workspace is concurrently modified, that modification will replicate a creation event as well as an
        # update event; see WorkspaceService.update. Whichever creation event is processed later will be ignored,
        # but the update event will then ensure that the HBI consumer ultimately sees the correct state.
        replicator.replicate_workspace(
            make_workspace_event(workspace=workspace, event_type=ReplicationEventType.CREATE_WORKSPACE),
            WorkspaceEventStream.BULK,
        )

    return len(workspaces)


logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _Result:
    replicated_count: int
    failed_ws_ids: frozenset[uuid.UUID]


def _do_run_attempt(replicator: RelationReplicator, query: QuerySet, expected_count: int, batch_size: int) -> _Result:
    actual_count = 0
    failed_ids: set[uuid.UUID] = set()

    for raw_batch in itertools.batched(query.iterator(), batch_size):
        try:
            actual_count += _do_replicate_batch(replicator, list(raw_batch))
            logger.info(f"Replicated {actual_count}/~{expected_count} expected default workspaces (for this attempt).")
        except Exception:
            logger.error("Failed to replicate batch of default workspaces", exc_info=True)
            failed_ids.update(w.id for w in raw_batch)

    return _Result(replicated_count=actual_count, failed_ws_ids=frozenset(failed_ids))


def replicate_default_workspaces(replicator: Optional[RelationReplicator] = None, limit: Optional[int] = None):
    if replicator is None:
        replicator = OutboxReplicator()

    base_query = Workspace.objects.filter(type=Workspace.Types.DEFAULT)
    total_count = base_query.count()

    if limit is not None:
        limited_query = base_query[:limit]
        limited_count = min(total_count, limit)
    else:
        limited_query = base_query
        limited_count = total_count

    logger.info(f"About to replicate ~{limited_count} (out of ~{total_count} existing) default workspaces.")

    replicated_count = 0

    # Initial attempt.
    result = _do_run_attempt(
        replicator=replicator,
        query=limited_query,
        expected_count=limited_count,
        batch_size=500,
    )

    replicated_count += result.replicated_count
    failed_ids = result.failed_ws_ids

    logger.info(f"Replicated {replicated_count}/{limited_count} default workspaces on the initial attempt.")

    retry_attempts = 5

    for i in range(retry_attempts):
        if len(failed_ids) == 0:
            break

        logger.info(f"About to begin attempt {i + 2}; re-replicating {len(failed_ids)} default workspaces.")

        result = _do_run_attempt(
            replicator=replicator,
            query=base_query.filter(id__in=failed_ids),
            expected_count=len(failed_ids),
            # Try replicating each workspace individually on the last attempt.
            batch_size=(500 if i != (retry_attempts - 1) else 1),
        )

        replicated_count += result.replicated_count
        failed_ids = result.failed_ws_ids

        logger.info(f"Replicated an additional {result.replicated_count} default workspaces on this attempt.")

    logger.info(f"Replicated a total of {replicated_count}/{limited_count} workspaces.")

    if failed_ids:
        raise RuntimeError(f"Failed to replicate the following default workspaces: {[str(u) for u in failed_ids]}")
