import dataclasses
import datetime
import itertools
import logging
import uuid
from typing import Optional

from django.db.models import F, Q, QuerySet
from django.db.models.lookups import LessThanOrEqual
from management.atomic_transactions import atomic_with_retry
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    ReplicationEventType,
    WorkspaceEventStream,
)
from management.workspace.model import Workspace
from management.workspace.utils.event import make_workspace_event


@dataclasses.dataclass(frozen=True)
class _ReplicateConfig:
    event_stream: WorkspaceEventStream
    with_update_event: bool


_permitted_types = [Workspace.Types.DEFAULT, Workspace.Types.STANDARD, Workspace.Types.UNGROUPED_HOSTS]


@atomic_with_retry(retries=20)
def _do_replicate_batch(
    replicator: RelationReplicator, config: _ReplicateConfig, raw_workspaces: list[Workspace]
) -> int:
    """Replicates a batch of workspaces and returns the number actually replicated."""
    if len(raw_workspaces) == 0:
        return 0

    workspaces = list(
        Workspace.objects.filter(pk__in=(w.pk for w in raw_workspaces))
        .select_related("tenant")
        .select_for_update(of=["self"])
    )

    for workspace in workspaces:
        # Paranoidly check that we haven't been provided with a workspace of a type that shouldn't be replicated.
        if workspace.type not in _permitted_types:
            raise AssertionError(f"Unexpected workspace type: {workspace.type}")

        replicator.replicate_workspace(
            make_workspace_event(workspace=workspace, event_type=ReplicationEventType.CREATE_WORKSPACE),
            config.event_stream,
        )

        if config.with_update_event:
            replicator.replicate_workspace(
                make_workspace_event(workspace=workspace, event_type=ReplicationEventType.UPDATE_WORKSPACE),
                config.event_stream,
            )

    return len(workspaces)


logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _Result:
    replicated_count: int
    failed_ws_ids: frozenset[uuid.UUID]


def _do_run_attempt(
    replicator: RelationReplicator, config: _ReplicateConfig, query: QuerySet, expected_count: int, batch_size: int
) -> _Result:
    actual_count = 0
    failed_ids: set[uuid.UUID] = set()

    for raw_batch in itertools.batched(query.iterator(), batch_size):
        try:
            actual_count += _do_replicate_batch(replicator=replicator, config=config, raw_workspaces=list(raw_batch))
            logger.info(f"Replicated {actual_count}/~{expected_count} expected workspaces (for this attempt).")
        except Exception:
            logger.error("Failed to replicate batch of workspaces", exc_info=True)
            failed_ids.update(w.id for w in raw_batch)

    return _Result(replicated_count=actual_count, failed_ws_ids=frozenset(failed_ids))


def _do_replicate(
    replicator: RelationReplicator,
    config: _ReplicateConfig,
    base_query: QuerySet,
    limit: Optional[int] = None,
    *,
    description: str,
):
    total_count = base_query.count()

    if limit is not None:
        limited_query = base_query[:limit]
        limited_count = min(total_count, limit)
    else:
        limited_query = base_query
        limited_count = total_count

    logger.info(f"About to replicate ~{limited_count} (out of ~{total_count} existing) {description}.")

    replicated_count = 0

    # Initial attempt.
    result = _do_run_attempt(
        replicator=replicator,
        config=config,
        query=limited_query,
        expected_count=limited_count,
        batch_size=500,
    )

    replicated_count += result.replicated_count
    failed_ids = result.failed_ws_ids

    logger.info(f"Replicated {replicated_count}/{limited_count} {description} on the initial attempt.")

    retry_attempts = 5

    for i in range(retry_attempts):
        if len(failed_ids) == 0:
            break

        logger.info(f"About to begin attempt {i + 2}; re-replicating {len(failed_ids)} {description}.")

        result = _do_run_attempt(
            replicator=replicator,
            config=config,
            query=base_query.all().filter(id__in=failed_ids),
            expected_count=len(failed_ids),
            # Try replicating each workspace individually on the last attempt.
            batch_size=(500 if i != (retry_attempts - 1) else 1),
        )

        replicated_count += result.replicated_count
        failed_ids = result.failed_ws_ids

        logger.info(f"Replicated an additional {result.replicated_count} {description} on this attempt.")

    logger.info(f"Replicated a total of {replicated_count}/{limited_count} {description}.")

    if failed_ids:
        raise RuntimeError(f"Failed to replicate the following {description}: {[str(u) for u in failed_ids]}")


def replicate_default_workspaces(replicator: Optional[RelationReplicator] = None, limit: Optional[int] = None):
    if replicator is None:
        replicator = OutboxReplicator()

    # If a workspace is concurrently modified, that modification will replicate a creation event as well as an
    # update event; see WorkspaceService.update. Since the HBI consumer will ignore any duplicate workspaces,
    # we can thus unconditionally replicate a create event to the bulk stream. (If an update happens to get there
    # first, then we'll replicate a no-op event, which is perfectly acceptable.)
    #
    # Since any modifications will also replicate an update event, concurrent updates will still definitely be
    # reflected. Whichever creation event is processed later will be ignored, but the update event will then ensure
    # that the HBI consumer ultimately sees the correct state.
    config = _ReplicateConfig(event_stream=WorkspaceEventStream.BULK, with_update_event=False)

    _do_replicate(
        replicator=replicator,
        config=config,
        base_query=Workspace.objects.filter(type=Workspace.Types.DEFAULT),
        limit=limit,
        description="default workspaces",
    )


def replicate_updated_workspaces(
    since: datetime.datetime,
    replicator: Optional[RelationReplicator] = None,
    exclude_unchanged_default_workspaces: bool = False,
):
    if replicator is None:
        replicator = OutboxReplicator()

    base_query = Workspace.objects.filter(type__in=_permitted_types).filter(
        Q(created__gte=since) | Q(modified__gte=since)
    )

    if exclude_unchanged_default_workspaces:
        # If we know that we have separately replicated or will separately re-replicate all existing default
        # workspaces, we can skip any that have never been modified. If they continue being unmodified through the
        # process, then everything is fine (since the bulk re-replication will handle the workspace in its initial
        # state, and that will end up being the final state). If the workspace is concurrently modified during or
        # after the bulk-rereplication, WorkspaceService will replicate both a create and update event for it,
        # as described above in replicate_default_workspaces, and will ultimately have replicated the correct data.
        #
        # If the default workspace was modified before the "since" time, we assume that it was correctly replicated at
        # that time, and thus we have no need to replicate it now. If it was modified after the "since" time,
        # it will be included in the below query and replicated here.
        #
        # Thus, we've correctly handled all possible cases.
        #
        # Note that since Django will give the created and modified fields slightly different values during creation,
        # we cannot just check for equality. We use 1ms as a strict threshold, since this appears to get most of the
        # variation in practice, and there will almost certainly be no network request that modifies a workspace in
        # the first millisecond after its creation. (It's okay if we find workspaces that were not actually modified,
        # but it is not acceptable to exclude workspaces that *were* modified.)
        base_query = base_query.exclude(
            Q(type=Workspace.Types.DEFAULT) & LessThanOrEqual((F("modified") - F("created")), "00:00:00.001")
        )

    config = _ReplicateConfig(event_stream=WorkspaceEventStream.STANDARD, with_update_event=True)

    _do_replicate(
        replicator=replicator,
        config=config,
        base_query=base_query,
        description="updated workspaces",
    )
