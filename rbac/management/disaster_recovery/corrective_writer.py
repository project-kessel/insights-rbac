"""Generate and write corrective events for disaster recovery reconciliation."""

import logging
from dataclasses import dataclass
from typing import Literal

from django.db import transaction
from management.disaster_recovery.kafka_reader import ParsedReplicationEvent
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    ReplicationEvent,
    ReplicationEventType,
)
from management.relation_replicator.types import RelationTuple

logger = logging.getLogger(__name__)


@dataclass
class CorrectiveAction:
    """A single corrective action derived from the truth table."""

    action: Literal["add", "remove", "skip"]
    tuple: RelationTuple
    reason: str
    source_event_offset: int
    source_event_partition: int
    org_id: str = ""


def generate_corrective_actions(
    parsed_events: list[ParsedReplicationEvent],
    existence_map: dict[tuple[str, str], bool],
) -> list[CorrectiveAction]:
    """Apply the truth table to generate corrective actions.

    relations_to_add + exists     -> SKIP (both correct)
    relations_to_add + not exists -> corrective DELETE (orphaned tuple in SpiceDB)
    relations_to_remove + exists  -> corrective ADD (tuple was deleted, resource restored)
    relations_to_remove + not exists -> SKIP (correctly deleted in both)
    """
    actions: list[CorrectiveAction] = []

    for event in parsed_events:
        for rel_tuple in event.relations_to_add:
            key = (rel_tuple.resource.type.name, rel_tuple.resource.id)
            exists = existence_map.get(key, True)

            if exists:
                actions.append(
                    CorrectiveAction(
                        action="skip",
                        tuple=rel_tuple,
                        reason="Resource exists, add is correct",
                        source_event_offset=event.offset,
                        source_event_partition=event.partition,
                        org_id=event.org_id,
                    )
                )
            else:
                actions.append(
                    CorrectiveAction(
                        action="remove",
                        tuple=rel_tuple,
                        reason="Resource does not exist, orphaned tuple needs removal",
                        source_event_offset=event.offset,
                        source_event_partition=event.partition,
                        org_id=event.org_id,
                    )
                )

        for rel_tuple in event.relations_to_remove:
            key = (rel_tuple.resource.type.name, rel_tuple.resource.id)
            exists = existence_map.get(key, True)

            if exists:
                actions.append(
                    CorrectiveAction(
                        action="add",
                        tuple=rel_tuple,
                        reason="Resource exists, deleted tuple needs restoration",
                        source_event_offset=event.offset,
                        source_event_partition=event.partition,
                        org_id=event.org_id,
                    )
                )
            else:
                actions.append(
                    CorrectiveAction(
                        action="skip",
                        tuple=rel_tuple,
                        reason="Resource does not exist, deletion is correct",
                        source_event_offset=event.offset,
                        source_event_partition=event.partition,
                        org_id=event.org_id,
                    )
                )

    return actions


def write_corrective_events(
    actions: list[CorrectiveAction],
    replicator: OutboxReplicator,
) -> dict:
    """Write corrective events to the outbox table.

    Each event is written independently. If the process crashes mid-write,
    re-running reconciliation for the same window is safe because SpiceDB
    relation writes are idempotent.

    Returns counts of adds, removes, skips, and errors.
    """
    adds = [a for a in actions if a.action == "add"]
    removes = [a for a in actions if a.action == "remove"]
    skips = [a for a in actions if a.action == "skip"]
    errors = 0

    partition_key = PartitionKey.byEnvironment()

    for action in adds:
        try:
            event = ReplicationEvent(
                event_type=ReplicationEventType.DR_CORRECTIVE_ADD,
                partition_key=partition_key,
                add=[action.tuple],
                info={
                    "org_id": action.org_id,
                    "source_offset": action.source_event_offset,
                    "reason": action.reason,
                },
            )
            with transaction.atomic():
                replicator.replicate(event)
        except Exception as e:
            logger.exception("Failed to write corrective ADD for %s: %s", action.tuple.stringify(), e)
            errors += 1

    for action in removes:
        try:
            event = ReplicationEvent(
                event_type=ReplicationEventType.DR_CORRECTIVE_REMOVE,
                partition_key=partition_key,
                remove=[action.tuple],
                info={
                    "org_id": action.org_id,
                    "source_offset": action.source_event_offset,
                    "reason": action.reason,
                },
            )
            with transaction.atomic():
                replicator.replicate(event)
        except Exception as e:
            logger.exception("Failed to write corrective REMOVE for %s: %s", action.tuple.stringify(), e)
            errors += 1

    return {
        "corrective_adds": len(adds),
        "corrective_removes": len(removes),
        "skipped": len(skips),
        "errors": errors,
    }
