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

"""Class to handle Dual Write API related operations."""

import logging
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, TYPE_CHECKING, Union

from django.conf import settings
from kessel.relations.v1beta1 import common_pb2

if TYPE_CHECKING:
    from management.relation_replicator.types import RelationTuple

logger = logging.getLogger(__name__)


class DualWriteException(Exception):
    """DualWrite exception."""

    pass


class ReplicationEventType(str, Enum):
    """Replication event type."""

    CREATE_SYSTEM_ROLE = "create_system_role"
    UPDATE_SYSTEM_ROLE = "update_system_role"
    DELETE_SYSTEM_ROLE = "delete_system_role"
    CREATE_CUSTOM_ROLE = "create_custom_role"
    UPDATE_CUSTOM_ROLE = "update_custom_role"
    DELETE_CUSTOM_ROLE = "delete_custom_role"
    ASSIGN_ROLE = "assign_role"
    UNASSIGN_ROLE = "unassign_role"
    CREATE_GROUP = "create_group"
    UPDATE_GROUP = "update_group"
    DELETE_GROUP = "delete_group"
    ADD_PRINCIPALS_TO_GROUP = "add_principals_to_group"
    REMOVE_PRINCIPALS_FROM_GROUP = "remove_principals_from_group"
    BOOTSTRAP_TENANT = "bootstrap_tenant"
    BULK_BOOTSTRAP_TENANT = "bulk_bootstrap_tenant"
    EXTERNAL_USER_UPDATE = "external_user_update"
    EXTERNAL_USER_DISABLE = "external_user_disable"
    BULK_EXTERNAL_USER_UPDATE = "bulk_external_user_update"
    MIGRATE_CUSTOM_ROLE = "migrate_custom_role"
    MIGRATE_TENANT_GROUPS = "migrate_tenant_groups"
    CUSTOMIZE_DEFAULT_GROUP = "customize_default_group"
    MIGRATE_SYSTEM_ROLE_ASSIGNMENT = "migrate_system_role_assignment"
    APPROVE_CROSS_ACCOUNT_REQUEST = "approve_cross_account_request"
    DENY_CROSS_ACCOUNT_REQUEST = "deny_cross_account_request"
    EXPIRE_CROSS_ACCOUNT_REQUEST = "expire_cross_account_request"
    MIGRATE_CROSS_ACCOUNT_REQUEST = "migrate_cross_account_request"
    DELETE_BINDING_MAPPINGS = "delete_binding_mappings"
    CREATE_UNGROUPED_HOSTS_WORKSPACE = "create_ungrouped_hosts_workspace"
    FIX_RESOURCE_DEFINITIONS = "fix_resource_definitions"
    # Binding scope migration
    MIGRATE_BINDING_SCOPE = "migrate_binding_scope"
    REMIGRATE_ROLE_BINDING = "remigrate_role_binding"
    DUPLICATE_BINDING_CLEANUP = "duplicate_binding_cleanup"
    WORKSPACE_IMPORT = "workspace_import"
    CREATE_WORKSPACE = "create_workspace"
    UPDATE_WORKSPACE = "update_workspace"
    DELETE_WORKSPACE = "delete_workspace"
    MOVE_WORKSPACE = "move_workspace"
    CLEANUP_ORPHAN_BINDINGS = "cleanup_orphan_bindings"
    REMOVE_UNASSIGNED_BINDING_MAPPINGS = "remove_unassigned_binding_mappings"
    BATCH_CREATE_ROLE_BINDING = "batch_create_role_binding"
    UPDATE_ROLE_BINDINGS_FOR_SUBJECT = "update_role_bindings_for_subject"
    REMOVE_DELETED_WORKSPACE_BINDINGS = "remove_deleted_workspace_bindings"
    UPDATE_ROOT_WORKSPACE_TENANTS = "update_root_workspace_tenants"
    REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS = "remove_root_parent_tenant_relationships"


class ReplicationEvent:
    """What tuples changes to replicate."""

    event_type: ReplicationEventType
    event_info: dict[str, object]
    partition_key: "PartitionKey"
    add: list[Union["RelationTuple", common_pb2.Relationship]]
    remove: list[Union["RelationTuple", common_pb2.Relationship]]

    def __init__(
        self,
        event_type: ReplicationEventType,
        partition_key: "PartitionKey",
        add: list[Union["RelationTuple", common_pb2.Relationship]] = [],
        remove: list[Union["RelationTuple", common_pb2.Relationship]] = [],
        info: dict[str, object] = {},
    ):
        """Initialize ReplicationEvent."""
        self.partition_key = partition_key
        self.event_type = event_type
        self.add = add
        self.remove = remove
        self.event_info = info

    def resource_context(self) -> Dict[str, object] | None:
        """Build context for all replication events that have identifiable resources."""
        if self.event_type == ReplicationEventType.REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS:
            token = self.event_info.get("notify_token")
            if not token:
                logger.warning(
                    "remove_root_parent_tenant_relationships batch missing notify_token in event_info=%s",
                    self.event_info,
                )
                return None
            context = ReplicationEventResourceContext(
                org_id="",
                event_type=self.event_type.value,
            )
            result = context.to_json()
            result["notify_token"] = str(token)
            return result

        # Validate org_id exists for all events
        org_id = str(self.event_info.get("org_id", ""))
        if not org_id:
            logger.warning(
                f"Missing required org_id for {self.event_type.value} event. " f"event_info: {self.event_info}"
            )

        if self.event_type == ReplicationEventType.CREATE_WORKSPACE:
            if "workspace_id" not in self.event_info:
                logger.warning(f"Missing workspace_id for CREATE_WORKSPACE event. " f"event_info: {self.event_info}")
                return None

            resource_id = str(self.event_info["workspace_id"])
            context = ReplicationEventResourceContext(
                resource_type="Workspace",
                resource_id=resource_id,
                org_id=org_id,
                event_type=self.event_type.value,
            )
            return context.to_json()

        else:
            context = ReplicationEventResourceContext(
                org_id=org_id,
                event_type=self.event_type.value,
            )
            return context.to_json()


class ReplicationEventResourceContext:
    """Replication event resource context."""

    resource_type: str | None
    resource_id: str | None
    org_id: str
    event_type: str
    created_at: int  # Unix timestamp when event was created (whole seconds)

    def __init__(
        self,
        org_id: str,
        event_type: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        created_at: int | None = None,
    ):
        """Initialize ReplicationEventResourceContext."""
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.org_id = org_id
        self.event_type = event_type
        # Capture creation timestamp for latency tracking
        self.created_at = created_at if created_at is not None else int(time.time())

    def to_json(self) -> Dict[str, object]:
        """Convert to JSON dictionary."""
        result: Dict[str, object] = {
            "org_id": self.org_id,
            "event_type": self.event_type,
            "created_at": self.created_at,
        }
        # Only include resource_type if it's present
        if self.resource_type is not None:
            result["resource_type"] = self.resource_type
        # Only include resource_id if it's present
        if self.resource_id is not None:
            result["resource_id"] = self.resource_id
        return result


class WorkspaceEvent:
    """Workspace event."""

    org_id: str
    account_number: str
    workspace: dict[str, str]
    event_type: ReplicationEventType
    partition_key: "PartitionKey"

    def __init__(
        self,
        org_id: str,
        account_number: str,
        workspace: dict[str, str],
        event_type: ReplicationEventType,
        partition_key: "PartitionKey",
    ):
        """Initialize WorkspaceEvent."""
        self.org_id = org_id
        self.account_number = account_number
        self.workspace = workspace
        self.event_type = event_type
        self.partition_key = partition_key


class AggregateTypes(str, Enum):
    """Aggregate types for outbox events."""

    RELATIONS = "relations-replication-event"
    WORKSPACE = "workspace"
    WORKSPACE_BULK = "workspace-bulk"


class WorkspaceEventStream(Enum):
    """
    The class that a WorkspaceEvent belongs to.

    As opposed to PartitionKey (which, for Kafka replicators, represents a partition within the same topic),
    different WorkspaceEventClasses represent entirely different Kafka topics.
    """

    STANDARD = "standard"
    BULK = "bulk"

    def aggregate_type(self) -> AggregateTypes:
        """Get the AggregateType that should be used for this stream in a Kafka replicator."""
        if self == WorkspaceEventStream.STANDARD:
            return AggregateTypes.WORKSPACE

        if self == WorkspaceEventStream.BULK:
            return AggregateTypes.WORKSPACE_BULK

        raise AssertionError(f"Unexpected WorkspaceEventClass: {self!r}")


class RelationReplicator(ABC):
    """Type responsible for replicating relations to Kessel Relations."""

    @abstractmethod
    def replicate(self, event: ReplicationEvent):
        """Replicate the given event to Kessel Relations."""
        pass

    def replicate_workspace(self, event: WorkspaceEvent, event_stream: WorkspaceEventStream):
        """Replicate the given workspace event to Kessel Relations."""
        pass


class PartitionKey(ABC):
    """
    Parent type for all partition keys.

    Partition keys define the partitions in which replication events are ordered.
    """

    @staticmethod
    def byEnvironment() -> "PartitionKey":
        """
        Order all events within the environment.

        This makes all changes follow the same order as the database (assuming one database per environment),
        however it means all events can only be processed by a single consumer at a time.
        """
        return EnvironmentPartitionKey()

    # TODO: Eventually we may want to scale out replication via more partitions.
    # To do this we will need a procedure like this:
    # 1. Add a field to the outbox table that can control the topic
    # 3. Add configuration for `route.by.field` in Debezium
    # 4. Start writing events with the new partition key, with a new value for this field.
    #    Events in the WAL with the global key will be routed to the "old" topic,
    #    while events with the new partition key(s) will be routed to a new topic.
    # 5. Let the "old" topic be consumed entirely by the sink.
    #    This maintains order for those with respect to anything new.
    # 6. Once that is empty, switch the sink to the new topic.
    # This just introduces some delay in new access, but otherwise doesn't introduce an outage.

    # Example: byTenant(tenant: Tenant) -> "PartitionKey":
    # ...

    # When partitioning by another key, be mindful of causal relationships between events,
    # and other operations which may change the same tuples.
    # The same tuples MUST only ever be changed with the same partition.

    @abstractmethod
    def __str__(self) -> str:
        """Return the string value of the partition key."""
        pass


class EnvironmentPartitionKey(PartitionKey):
    """Environment partition key globally orders all events within the environment."""

    def __str__(self) -> str:
        """Return the environment name."""
        return settings.ENV_NAME
