#
# Copyright 2024 Red Hat, Inc.
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

"""RelationReplicator which writes to the outbox table."""

import logging
from typing import Any, Dict, List, NotRequired, Optional, Protocol, TypedDict, Union

from django.db import transaction
from google.protobuf import json_format
from kessel.relations.v1beta1 import common_pb2
from management.models import Outbox
from management.relation_replicator.logging_replicator import stringify_spicedb_relationship
from management.relation_replicator.relation_replicator import (
    AggregateTypes,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
    WorkspaceEvent,
    WorkspaceEventStream,
)
from management.relation_replicator.types import RelationTuple
from prometheus_client import Counter

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

relations_replication_event_total = Counter(
    "relations_replication_event_total", "Total count of relations replication events"
)
workspace_replication_event_total = Counter(
    "workspace_replication_event_total", "Total count of workspace replication events"
)

OPERATION_MAPPING = {
    ReplicationEventType.CREATE_WORKSPACE: "create",
    ReplicationEventType.DELETE_WORKSPACE: "delete",
    ReplicationEventType.UPDATE_WORKSPACE: "update",
}


class ReplicationEventPayload(TypedDict):
    """Typed dictionary for ReplicationEvent payload."""

    relations_to_add: List[Dict[str, Any]]
    relations_to_remove: List[Dict[str, Any]]
    resource_context: NotRequired[Dict[str, object]]


class WorkspaceEventPayload(TypedDict):
    """Typed dictionary for WorkspaceEvent payload."""

    org_id: str
    account_number: str
    workspace: Dict[str, str]
    operation: str


class OutboxReplicator(RelationReplicator):
    """Replicates relations via the outbox table."""

    def __init__(self, log: Optional["OutboxLog"] = None):
        """Initialize the OutboxReplicator with an optional OutboxLog implementation."""
        self._log = log if log is not None else OutboxWAL()

    def replicate(self, event: ReplicationEvent):
        """Replicate the given event to Kessel Relations via the Outbox."""
        payload = self._build_replication_event(event)
        self._save_replication_event(payload, event.event_type, event.event_info, str(event.partition_key))

    def _check_for_duplicate_relationships(self, relationships):
        """
        Check for duplicate relationships and raise an error if any are found.

        Duplicate relationships indicate a bug in tuple generation and must be fixed at the source.
        """
        seen = set()
        duplicates = []

        for rel in relationships:
            # Use string representation as unique key (cleaner than tuple of components)
            key = stringify_spicedb_relationship(rel)

            if key in seen:
                # Duplicate found - this is a bug!
                duplicates.append(key)
            else:
                seen.add(key)

        if duplicates:
            # This indicates a bug in tuple generation - fail fast
            dup_info = "\n".join(f"  - {dup}" for dup in duplicates[:10])
            raise ValueError(
                f"Found {len(duplicates)} duplicate relationships (bug in tuple generation!):\n{dup_info}\n"
                f"Total relationships: {len(relationships)}, Duplicates: {len(duplicates)}"
            )

    def replicate_workspace(self, event: WorkspaceEvent, event_stream: WorkspaceEventStream):
        """Replicate the event of workspace."""
        payload = WorkspaceEventPayload(
            org_id=event.org_id,
            account_number=event.account_number,
            workspace=event.workspace,
            operation=OPERATION_MAPPING[event.event_type],
        )

        self._save_workspace_event(
            payload=payload,
            event_type=event.event_type,
            aggregatetype=event_stream.aggregate_type(),
            aggregateid=str(event.partition_key),
        )

    @staticmethod
    def _relation_to_dict(rel: Union[RelationTuple, common_pb2.Relationship]) -> dict[str, Any]:
        """Serialize a RelationTuple or protobuf Relationship to a dict."""
        if isinstance(rel, RelationTuple):
            return rel.to_dict()
        return json_format.MessageToDict(rel)

    def _build_replication_event(self, event: ReplicationEvent) -> ReplicationEventPayload:
        """Build replication event."""
        # Check for duplicates in relationships to add (will raise error if found)
        self._check_for_duplicate_relationships(event.add)

        add_json: list[dict[str, Any]] = [self._relation_to_dict(rel) for rel in event.add]
        remove_json: list[dict[str, Any]] = [self._relation_to_dict(rel) for rel in event.remove]

        payload: ReplicationEventPayload = {
            "relations_to_add": add_json,
            "relations_to_remove": remove_json,
        }

        resource_context = event.resource_context()
        if resource_context:
            payload["resource_context"] = resource_context

        return payload

    def _save_replication_event(
        self,
        payload: ReplicationEventPayload,
        event_type: ReplicationEventType,
        event_info: dict[str, object],
        aggregateid: str,
    ):
        """Save replication event."""
        # TODO: Can we add these as proper fields for kibana but also get logged in simple formatter?
        logged_info = " ".join([f"info.{key}='{str(value)}'" for key, value in event_info.items()])

        if not payload["relations_to_add"] and not payload["relations_to_remove"]:
            logger.warning(
                "[Dual Write] Skipping empty replication event. "
                "An empty event is always a bug. "
                "Calling code should avoid this and if not obvious, log why there is nothing to replicate. "
                "aggregateid='%s' event_type='%s' %s",
                aggregateid,
                event_type,
                logged_info,
            )
            return

        logger.info(
            "[Dual Write] Publishing replication event. aggregateid='%s' event_type='%s' %s",
            aggregateid,
            event_type,
            logged_info,
        )

        transaction.on_commit(relations_replication_event_total.inc)

        # https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html#basic-outbox-table
        outbox = Outbox(
            aggregatetype=AggregateTypes.RELATIONS,
            aggregateid=aggregateid,
            event_type=event_type,
            payload=payload,
        )

        self._log.log(outbox)

    def _save_workspace_event(
        self,
        payload: WorkspaceEventPayload,
        event_type: ReplicationEventType,
        aggregatetype: AggregateTypes,
        aggregateid: str,
    ):
        """Save replication event."""
        transaction.on_commit(workspace_replication_event_total.inc)

        outbox = Outbox(
            aggregatetype=aggregatetype.value,
            aggregateid=aggregateid,
            event_type=event_type,
            payload=payload,
        )

        self._log.log(outbox)


class OutboxLog(Protocol):
    """Protocol for logging outbox events."""

    def log(self, outbox: Outbox):
        """Log the given outbox event."""
        ...


class OutboxWAL:
    """Writes to the outbox table."""

    def log(self, outbox: Outbox):
        """Log the given outbox event."""
        outbox.save(force_insert=True)
        # Immediately deleted to avoid filling up the table.
        # Keeping outbox records around is not as useful as it may seem,
        # because they will not necessarily be sorted in the order they appear in the WAL.
        outbox.delete()


class InMemoryLog:
    """Logs to memory."""

    def __init__(self):
        """Initialize the InMemoryLog with an empty log."""
        self._log = []

    def __len__(self):
        """Return the number of logged events."""
        return len(self._log)

    def __iter__(self):
        """Return an iterator over the logged events."""
        return iter(self._log)

    def __getitem__(self, index) -> Outbox:
        """Return the logged event at the given index."""
        return self._log[index]

    def first(self) -> Outbox:
        """Return the first logged event."""
        if not self._log:
            raise IndexError("No events logged")
        return self._log[0]

    def latest(self) -> Outbox:
        """Return the latest logged event."""
        if not self._log:
            raise IndexError("No events logged")
        return self._log[-1]

    def log(self, outbox: Outbox):
        """Log the given outbox event."""
        self._log.append(outbox)

    def clear(self):
        """Clear all logged events."""
        self._log.clear()
