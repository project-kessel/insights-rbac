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
"""Workspace event utilities."""

from management.relation_replicator.relation_replicator import PartitionKey, ReplicationEventType, WorkspaceEvent
from management.workspace.model import Workspace


def make_workspace_event(workspace: Workspace, event_type: ReplicationEventType):
    """Create a WorkspaceEvent with the provided workspace and event type."""
    # To avoid circular dependency.
    from management.workspace.serializer import WorkspaceEventSerializer

    if event_type not in (
        ReplicationEventType.CREATE_WORKSPACE,
        ReplicationEventType.UPDATE_WORKSPACE,
        ReplicationEventType.DELETE_WORKSPACE,
    ):
        raise ValueError(f"Unexpected event_type: {event_type}")

    return WorkspaceEvent(
        account_number=workspace.tenant.account_id,
        org_id=str(workspace.tenant.org_id),
        workspace=WorkspaceEventSerializer(workspace).data,
        event_type=event_type,
        partition_key=PartitionKey.byEnvironment(),
    )
