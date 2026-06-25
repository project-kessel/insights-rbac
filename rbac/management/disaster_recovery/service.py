"""Orchestrator for disaster recovery reconciliation."""

import logging
import time

from management.disaster_recovery.corrective_writer import (
    generate_corrective_actions,
    write_corrective_events,
)
from management.disaster_recovery.kafka_reader import ParsedReplicationEvent, read_events_in_window
from management.disaster_recovery.resource_checker import check_resources_exist
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType

logger = logging.getLogger(__name__)

_BOOTSTRAP_EVENT_TYPES = frozenset({ReplicationEventType.BOOTSTRAP_TENANT, ReplicationEventType.BULK_BOOTSTRAP_TENANT})


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

    if not all_events:
        logger.info("No events found in the time window, nothing to reconcile")
        return {
            "status": "completed",
            "time_window": {"start_ms": start_timestamp_ms, "end_ms": end_timestamp_ms},
            "events_read": 0,
            "tuples_processed": 0,
            "corrective_adds": 0,
            "corrective_removes": 0,
            "skipped": 0,
            "skipped_bootstrap_events": 0,
            "errors": 0,
            "duration_seconds": round(time.monotonic() - start, 3),
        }

    events, skipped_bootstrap = _partition_bootstrap_events(all_events)

    all_tuples = []
    for event in events:
        all_tuples.extend(event.relations_to_add)
        all_tuples.extend(event.relations_to_remove)

    logger.info(
        "Read %d events with %d total tuples (%d bootstrap events skipped)",
        len(events),
        len(all_tuples),
        skipped_bootstrap,
    )

    if not events:
        elapsed = round(time.monotonic() - start, 3)
        logger.info("All %d events in the window were bootstrap events, nothing to reconcile", skipped_bootstrap)
        return {
            "status": "completed",
            "time_window": {"start_ms": start_timestamp_ms, "end_ms": end_timestamp_ms},
            "events_read": len(all_events),
            "tuples_processed": 0,
            "corrective_adds": 0,
            "corrective_removes": 0,
            "skipped": 0,
            "skipped_bootstrap_events": skipped_bootstrap,
            "errors": 0,
            "duration_seconds": elapsed,
        }

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
            "events_read": len(all_events),
            "tuples_processed": len(all_tuples),
            "corrective_adds": len(adds),
            "corrective_removes": len(removes),
            "skipped": len(skips),
            "skipped_bootstrap_events": skipped_bootstrap,
            "errors": 0,
            "actions": action_details,
            "duration_seconds": elapsed,
        }
        logger.info(
            "DR reconciliation DRY RUN: events=%d tuples=%d would_add=%d would_remove=%d skipped=%d "
            "bootstrap_skipped=%d (%.3fs)",
            len(events),
            len(all_tuples),
            len(adds),
            len(removes),
            len(skips),
            skipped_bootstrap,
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
        "events_read": len(all_events),
        "tuples_processed": len(all_tuples),
        "skipped_bootstrap_events": skipped_bootstrap,
        "duration_seconds": elapsed,
    }
    result.update(write_result)

    logger.info(
        "DR reconciliation completed: events=%d tuples=%d adds=%d removes=%d skipped=%d "
        "bootstrap_skipped=%d errors=%d (%.3fs)",
        result["events_read"],
        result["tuples_processed"],
        result["corrective_adds"],
        result["corrective_removes"],
        result["skipped"],
        skipped_bootstrap,
        result["errors"],
        elapsed,
    )

    return result


def _partition_bootstrap_events(
    events: list[ParsedReplicationEvent],
) -> tuple[list[ParsedReplicationEvent], int]:
    """Separate bootstrap tenant events from events that need reconciliation.

    Bootstrap events create virtual resources (groups, role bindings) stored
    only in TenantMapping, not as Django model instances. Per-resource existence
    checking would incorrectly flag them as missing. These events are atomic:
    the tenant is either fully bootstrapped or not present at all.
    """
    non_bootstrap = []
    skipped = 0

    for event in events:
        if event.event_type in _BOOTSTRAP_EVENT_TYPES:
            skipped += 1
            bootstrap_tuples = len(event.relations_to_add) + len(event.relations_to_remove)
            logger.info(
                "Skipping bootstrap event (type=%s, org_id=%s, offset=%d, partition=%d, tuples=%d)",
                event.event_type,
                event.org_id,
                event.offset,
                event.partition,
                bootstrap_tuples,
            )
        else:
            non_bootstrap.append(event)

    return non_bootstrap, skipped
