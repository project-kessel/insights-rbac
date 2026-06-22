"""Orchestrator for disaster recovery reconciliation."""

import logging
import time

from management.disaster_recovery.corrective_writer import (
    generate_corrective_actions,
    write_corrective_events,
)
from management.disaster_recovery.kafka_reader import read_events_by_offset, read_events_in_window
from management.disaster_recovery.resource_checker import check_resources_exist
from management.relation_replicator.outbox_replicator import OutboxReplicator

logger = logging.getLogger(__name__)


def reconcile(
    restore_timestamp_ms: int | None = None,
    buffer_seconds: int = 300,
    replicator: OutboxReplicator | None = None,
    dry_run: bool = False,
    start_offset: int | None = None,
    end_offset: int | None = None,
) -> dict:
    """Run disaster recovery reconciliation.

    Supports two modes (mutually exclusive):
    - Timestamp mode: provide restore_timestamp_ms (+ buffer_seconds)
    - Offset mode: provide start_offset (+ optional end_offset)

    Steps:
    1. Read Kafka events from the specified window
    2. Extract all unique resource references from tuples
    3. Bulk-check resource existence in RBAC database
    4. Generate corrective actions per the truth table
    5. Write corrective events to outbox
    6. Return summary
    """
    use_offsets = start_offset is not None
    use_timestamp = restore_timestamp_ms is not None

    if not use_offsets and not use_timestamp:
        raise ValueError("Either restore_timestamp_ms or start_offset must be provided")
    if use_offsets and use_timestamp:
        raise ValueError("Cannot specify both restore_timestamp_ms and start_offset")

    start = time.monotonic()

    if use_offsets:
        logger.info(
            "Starting DR reconciliation by offset: [%d, %s)",
            start_offset,
            end_offset if end_offset is not None else "END",
        )
        events = read_events_by_offset(start_offset, end_offset)
        window_info: dict = {"start_offset": start_offset, "end_offset": end_offset}
    else:
        if buffer_seconds < 0:
            raise ValueError(f"buffer_seconds must be non-negative, got {buffer_seconds}")

        buffer_ms = buffer_seconds * 1000
        start_timestamp_ms_calc = restore_timestamp_ms - buffer_ms
        end_timestamp_ms = restore_timestamp_ms

        logger.info(
            "Starting DR reconciliation: window [%d, %d] (buffer=%ds)",
            start_timestamp_ms_calc,
            end_timestamp_ms,
            buffer_seconds,
        )

        events = read_events_in_window(start_timestamp_ms_calc, end_timestamp_ms)
        window_info = {"start_ms": start_timestamp_ms_calc, "end_ms": end_timestamp_ms}

    if not events:
        logger.info("No events found in the specified window, nothing to reconcile")
        return {
            "status": "completed",
            "window": window_info,
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
            "window": window_info,
            "events_read": len(events),
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
        "window": window_info,
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
