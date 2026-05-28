"""Orchestrator for disaster recovery reconciliation."""

import logging
import time

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

    events = read_events_in_window(start_timestamp_ms, end_timestamp_ms)

    if not events:
        logger.info("No events found in the time window, nothing to reconcile")
        return {
            "status": "completed",
            "time_window": {"start_ms": start_timestamp_ms, "end_ms": end_timestamp_ms},
            "events_read": 0,
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

    if replicator is None:
        replicator = OutboxReplicator()

    write_result = write_corrective_events(actions, replicator)

    elapsed = round(time.monotonic() - start, 3)

    result = {
        "status": "completed",
        "time_window": {"start_ms": start_timestamp_ms, "end_ms": end_timestamp_ms},
        "events_read": len(events),
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
