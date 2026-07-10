"""Orchestrator for disaster recovery reconciliation."""

import logging
import time

from django.conf import settings
from management.disaster_recovery.corrective_writer import (
    generate_corrective_actions,
    write_corrective_events,
)
from management.disaster_recovery.kafka_reader import read_events_in_window
from management.disaster_recovery.resource_checker import check_resources_exist
from management.relation_replicator.outbox_replicator import OutboxReplicator

logger = logging.getLogger(__name__)


def reconcile(
    restore_timestamp_ms: int,
    buffer_seconds: int = 300,
    replicator: OutboxReplicator | None = None,
    dry_run: bool = False,
) -> dict:
    """Run disaster recovery reconciliation.

    1. Calculate time window [restore_timestamp - buffer, restore_timestamp]
    2. Read Kafka events from that window
    3. Extract all unique resource references from tuples
    4. Bulk-check resource existence in RBAC database
    5. Generate corrective actions per the truth table
    6. Write corrective events to outbox
    7. Return summary
    """
    if buffer_seconds < 0:
        raise ValueError(f"buffer_seconds must be non-negative, got {buffer_seconds}")

    start = time.monotonic()

    buffer_ms = buffer_seconds * 1000
    start_timestamp_ms = restore_timestamp_ms - buffer_ms
    end_timestamp_ms = restore_timestamp_ms

    logger.info(
        "Starting DR reconciliation: window [%d, %d] (buffer=%ds)",
        start_timestamp_ms,
        end_timestamp_ms,
        buffer_seconds,
    )

    all_events = read_events_in_window(start_timestamp_ms, end_timestamp_ms)

    skip_event_types = frozenset(settings.DR_SKIP_EVENT_TYPES)
    skipped_by_type = [e for e in all_events if e.event_type in skip_event_types]
    events = [e for e in all_events if e.event_type not in skip_event_types]

    if skipped_by_type:
        logger.info(
            "Skipped %d event(s) by type (types: %s)",
            len(skipped_by_type),
            ", ".join(sorted({e.event_type for e in skipped_by_type})),
        )

    if not events:
        logger.info("No events found in the time window, nothing to reconcile")
        return {
            "status": "completed",
            "time_window": {"start_ms": start_timestamp_ms, "end_ms": end_timestamp_ms},
            "events_read": 0,
            "events_skipped_by_type": len(skipped_by_type),
            "tuples_processed": 0,
            "corrective_adds": 0,
            "corrective_removes": 0,
            "skipped": 0,
            "errors": 0,
            "duration_seconds": round(time.monotonic() - start, 3),
        }

    all_tuples = []
    for event in events:
        all_tuples.extend(event.relations_to_add)
        all_tuples.extend(event.relations_to_remove)

    logger.info("Read %d events with %d total tuples", len(events), len(all_tuples))

    existence_map = check_resources_exist(all_tuples)
    actions = generate_corrective_actions(events, existence_map)

    adds = [a for a in actions if a.action == "add"]
    removes = [a for a in actions if a.action == "remove"]
    skips = [a for a in actions if a.action == "skip"]

    if dry_run:
        elapsed = round(time.monotonic() - start, 3)
        action_details = [
            {
                "action": a.action,
                "tuple": a.tuple.stringify(),
                "reason": a.reason,
                "source_partition": a.source_event_partition,
                "source_offset": a.source_event_offset,
            }
            for a in actions
            if a.action != "skip"
        ]
        for detail in action_details:
            logger.info(
                "DRY RUN: would %s %s (reason: %s, partition=%d, offset=%d)",
                detail["action"],
                detail["tuple"],
                detail["reason"],
                detail["source_partition"],
                detail["source_offset"],
            )

        result = {
            "status": "dry_run",
            "time_window": {"start_ms": start_timestamp_ms, "end_ms": end_timestamp_ms},
            "events_read": len(events),
            "events_skipped_by_type": len(skipped_by_type),
            "tuples_processed": len(all_tuples),
            "corrective_adds": len(adds),
            "corrective_removes": len(removes),
            "skipped": len(skips),
            "errors": 0,
            "actions": action_details,
            "duration_seconds": elapsed,
        }
        logger.info(
            "DR reconciliation DRY RUN: events=%d tuples=%d would_add=%d would_remove=%d skipped=%d (%.3fs)",
            len(events),
            len(all_tuples),
            len(adds),
            len(removes),
            len(skips),
            elapsed,
        )
        return result

    if replicator is None:
        replicator = OutboxReplicator()

    write_result = write_corrective_events(actions, replicator)

    elapsed = round(time.monotonic() - start, 3)

    result = {
        "status": "completed",
        "time_window": {"start_ms": start_timestamp_ms, "end_ms": end_timestamp_ms},
        "events_read": len(events),
        "events_skipped_by_type": len(skipped_by_type),
        "tuples_processed": len(all_tuples),
        "duration_seconds": elapsed,
    }
    result.update(write_result)

    logger.info(
        "DR reconciliation completed: events=%d tuples=%d adds=%d removes=%d skipped=%d errors=%d (%.3fs)",
        result["events_read"],
        result["tuples_processed"],
        result["corrective_adds"],
        result["corrective_removes"],
        result["skipped"],
        result["errors"],
        elapsed,
    )

    return result
