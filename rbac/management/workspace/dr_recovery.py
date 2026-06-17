#
# Copyright 2026 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Workspace disaster recovery -- generate corrective workspace events after a DB restore.

After an RBAC database restore, downstream services (HBI and others) retain workspace state
that may diverge from the restored RBAC state. This module reads workspace events from Kafka
for the data loss window, compares against current RBAC DB state, and writes corrective
workspace events to the outbox table so Debezium publishes them for downstream consumption.

Corrective event logic (truth table):
    create event + workspace NOT in RBAC -> write delete corrective event (orphaned)
    create event + workspace exists      -> SKIP (both agree)
    delete event + workspace exists      -> write create corrective event with current state (missing)
    delete event + workspace NOT in RBAC -> SKIP (both agree)
    update event + workspace exists      -> write update corrective event with current state (stale)
    update event + workspace NOT in RBAC -> write delete corrective event (orphaned)
"""

import logging
from typing import TypedDict

from core.kafka_dr import KafkaEvent
from django.db import transaction
from management.models import Outbox
from management.relation_replicator.outbox_replicator import OutboxLog, OutboxWAL, WorkspaceEventPayload
from management.relation_replicator.relation_replicator import (
    AggregateTypes,
    PartitionKey,
    ReplicationEventType,
)
from management.workspace.model import Workspace
from management.workspace.serializer import WorkspaceEventSerializer

logger = logging.getLogger(__name__)

PROCESSABLE_WORKSPACE_TYPES = {Workspace.Types.STANDARD, Workspace.Types.DEFAULT}
_PROCESSABLE_TYPE_VALUES = {t.value for t in PROCESSABLE_WORKSPACE_TYPES}  # type: ignore[attr-defined]


class WorkspaceKafkaEvent(TypedDict):
    """Parsed workspace event from Kafka."""

    workspace_id: str
    org_id: str
    account_number: str
    operation: str
    workspace_data: dict[str, str]


class CorrectiveEventStats(TypedDict):
    """Statistics returned by the corrective event generation."""

    total_events: int
    corrective_creates: int
    corrective_deletes: int
    corrective_updates: int
    skipped: int
    errors: int
    error_details: list[dict[str, str]]


def parse_workspace_kafka_events(kafka_events: list[KafkaEvent]) -> list[WorkspaceKafkaEvent]:
    """Parse raw Kafka events into WorkspaceKafkaEvent dicts.

    Filters to only workspace aggregate type events and deduplicates by workspace ID,
    keeping only the latest event per workspace (chronological order by Kafka timestamp).
    """
    workspace_events: dict[str, WorkspaceKafkaEvent] = {}

    for event in kafka_events:
        value = event.value
        aggregate_type = value.get("aggregatetype", "")
        if aggregate_type != AggregateTypes.WORKSPACE.value:
            continue

        payload = value.get("payload")
        if not isinstance(payload, dict):
            continue

        workspace_data = payload.get("workspace", {})
        workspace_id = workspace_data.get("id", "")
        if not workspace_id:
            continue

        operation = payload.get("operation", "")
        if operation not in ("create", "update", "delete"):
            continue

        ws_type = workspace_data.get("type", "")
        if ws_type not in _PROCESSABLE_TYPE_VALUES:
            continue

        parsed = WorkspaceKafkaEvent(
            workspace_id=workspace_id,
            org_id=payload.get("org_id", ""),
            account_number=payload.get("account_number", ""),
            operation=operation,
            workspace_data=workspace_data,
        )

        workspace_events[workspace_id] = parsed

    return list(workspace_events.values())


def _build_workspace_lookup(workspace_ids: list[str]) -> dict[str, Workspace]:
    """Bulk-fetch existing workspaces and build a lookup dict by string ID."""
    if not workspace_ids:
        return {}
    workspaces = Workspace.objects.filter(id__in=workspace_ids).select_related("tenant")
    return {str(ws.id): ws for ws in workspaces}


def _serialize_workspace(workspace: Workspace) -> dict[str, str]:
    """Serialize a workspace model to the event payload format."""
    return WorkspaceEventSerializer(workspace).data


def _write_corrective_event(
    payload: WorkspaceEventPayload,
    event_type: ReplicationEventType,
    outbox_log: OutboxLog,
) -> None:
    """Write a corrective workspace event to the outbox."""
    outbox = Outbox(
        aggregatetype=AggregateTypes.WORKSPACE.value,
        aggregateid=str(PartitionKey.byEnvironment()),
        event_type=event_type,
        payload=payload,
    )
    outbox_log.log(outbox)


def generate_corrective_workspace_events(
    kafka_events: list[KafkaEvent],
    outbox_log: OutboxLog | None = None,
    dry_run: bool = False,
) -> CorrectiveEventStats:
    """Generate corrective workspace events based on Kafka events vs current DB state.

    For each workspace event in the lost window:
    - create + not in DB -> delete corrective event (orphaned workspace)
    - create + in DB     -> skip (both agree)
    - delete + in DB     -> create corrective event with current state (missing workspace)
    - delete + not in DB -> skip (both agree)
    - update + in DB     -> update corrective event with current state (stale workspace)
    - update + not in DB -> delete corrective event (orphaned workspace)

    Args:
        kafka_events: Raw Kafka events from the workspace topic within the lost window.
        outbox_log: Optional OutboxLog implementation (defaults to OutboxWAL for production).

    Returns:
        CorrectiveEventStats with counts of each action taken.
    """
    if outbox_log is None:
        outbox_log = OutboxWAL()

    stats = CorrectiveEventStats(
        total_events=0,
        corrective_creates=0,
        corrective_deletes=0,
        corrective_updates=0,
        skipped=0,
        errors=0,
        error_details=[],
    )

    parsed_events = parse_workspace_kafka_events(kafka_events)
    stats["total_events"] = len(parsed_events)

    if not parsed_events:
        logger.info("No workspace events to process in the recovery window")
        return stats

    workspace_ids = [e["workspace_id"] for e in parsed_events]
    workspace_lookup = _build_workspace_lookup(workspace_ids)

    for event in parsed_events:
        ws_id = event["workspace_id"]
        operation = event["operation"]
        existing_ws = workspace_lookup.get(ws_id)

        try:
            if operation == "create":
                if existing_ws is None:
                    if dry_run:
                        logger.info("DRY RUN: would write delete corrective for orphaned workspace %s", ws_id)
                    else:
                        with transaction.atomic():
                            _write_delete_corrective(event, outbox_log)
                    stats["corrective_deletes"] += 1
                else:
                    stats["skipped"] += 1

            elif operation == "delete":
                if existing_ws is not None:
                    if dry_run:
                        logger.info("DRY RUN: would write create corrective for missing workspace %s", ws_id)
                    else:
                        with transaction.atomic():
                            _write_existing_ws_corrective(existing_ws, "create", outbox_log)
                    stats["corrective_creates"] += 1
                else:
                    stats["skipped"] += 1

            elif operation == "update":
                if existing_ws is not None:
                    if dry_run:
                        logger.info("DRY RUN: would write update corrective for stale workspace %s", ws_id)
                    else:
                        with transaction.atomic():
                            _write_existing_ws_corrective(existing_ws, "update", outbox_log)
                    stats["corrective_updates"] += 1
                else:
                    if dry_run:
                        logger.info("DRY RUN: would write delete corrective for orphaned workspace %s", ws_id)
                    else:
                        with transaction.atomic():
                            _write_delete_corrective(event, outbox_log)
                    stats["corrective_deletes"] += 1

        except Exception as e:
            stats["errors"] += 1
            stats["error_details"].append({"workspace_id": ws_id, "operation": operation, "error": str(e)})
            logger.exception("Error processing corrective event for workspace %s", ws_id)

    logger.info(
        "Workspace DR recovery complete: total=%d creates=%d deletes=%d updates=%d skipped=%d errors=%d",
        stats["total_events"],
        stats["corrective_creates"],
        stats["corrective_deletes"],
        stats["corrective_updates"],
        stats["skipped"],
        stats["errors"],
    )

    return stats


_OPERATION_TO_EVENT_TYPE = {
    "create": ReplicationEventType.CREATE_WORKSPACE,
    "update": ReplicationEventType.UPDATE_WORKSPACE,
}


def _write_existing_ws_corrective(workspace: Workspace, operation: str, outbox_log: OutboxLog) -> None:
    """Write a create or update corrective event using current RBAC workspace state."""
    payload = WorkspaceEventPayload(
        org_id=str(workspace.tenant.org_id),
        account_number=str(workspace.tenant.account_id),
        workspace=_serialize_workspace(workspace),
        operation=operation,
    )
    _write_corrective_event(payload, _OPERATION_TO_EVENT_TYPE[operation], outbox_log)


def _write_delete_corrective(event: WorkspaceKafkaEvent, outbox_log: OutboxLog) -> None:
    """Write a delete corrective event using the original Kafka event data."""
    payload = WorkspaceEventPayload(
        org_id=event["org_id"],
        account_number=event["account_number"],
        workspace=event["workspace_data"],
        operation="delete",
    )
    _write_corrective_event(payload, ReplicationEventType.DELETE_WORKSPACE, outbox_log)
