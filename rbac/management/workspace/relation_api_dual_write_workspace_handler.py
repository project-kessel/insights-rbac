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
from typing import Optional

from django.db import OperationalError
from management.models import Workspace
from management.relation_replicator.relation_replicator import (
    DualWriteException,
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
    WorkspaceEventStream,
)
from management.role.relation_api_dual_write_handler import BaseRelationApiDualWriteHandler
from management.workspace.utils.event import make_workspace_event
from migration_tool.utils import create_relationship

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class RelationApiDualWriteWorkspaceHandler(BaseRelationApiDualWriteHandler):
    """Class to handle Dual Write for group bindings and membership."""

    workspace: Workspace

    def __init__(
        self,
        workspace: Workspace,
        event_type: ReplicationEventType,
        replicator: Optional[RelationReplicator] = None,
    ):
        """Initialize RelationApiDualWriteGroupHandler."""
        if not self.replication_enabled():
            return

        try:
            self.workspace = workspace
            self.event_type = event_type
            self.relations_to_add = []
            self.relations_to_remove = []
            super().__init__(replicator)
        except Exception as e:
            raise DualWriteException(e)

    def replicate_new_workspace(self):
        """Replicate new principals into group."""
        if not self.replication_enabled():
            return
        self.generate_relations_to_add_workspace()
        self._replicate()

    def replicate_updated_workspace(
        self, previous_parent, *, skip_ws_events: bool = False, force_create: bool = False
    ):
        """
        Replicate updated principals into group.

        skip_ws_events can be used for events that are known to not need replication (e.g. moves, since the
        replication event doesn't include parent information).

        force_create should be used for workspaces that have not necessarily had a create event sent before being
        updated.
        """
        if not self.replication_enabled():
            return
        self.generate_relations_to_update_workspace(previous_parent)
        self._replicate(skip_ws_events=skip_ws_events, force_create=force_create)

    def replicate_deleted_workspace(self):
        """Replicate deleted principals into group."""
        if not self.replication_enabled():
            return
        self.generate_relations_to_remove_workspace()
        self._replicate()

    def _replicate(self, skip_ws_events: bool = False, force_create: bool = False) -> None:
        if not self.replication_enabled():
            return
        try:
            if force_create:
                if skip_ws_events:
                    raise ValueError("Cannot have skip_ws_events and force_create both be set")

                if self.event_type != ReplicationEventType.UPDATE_WORKSPACE:
                    raise ValueError("Expected force_create to be true only for UPDATE_WORKSPACE events")

            if self.relations_to_remove or self.relations_to_add:
                self._replicator.replicate(
                    ReplicationEvent(
                        event_type=self.event_type,
                        info={"workspace_id": str(self.workspace.id), "org_id": str(self.workspace.tenant.org_id)},
                        partition_key=PartitionKey.byEnvironment(),
                        remove=self.relations_to_remove,
                        add=self.relations_to_add,
                    ),
                )
            if not skip_ws_events:
                if force_create:
                    # If force_create is set, we may not have previously sent a creation event for this workspace,
                    # which we must do before updating it. If we *have* previously sent a creation event for this
                    # workspace, the HBI consumer will just ignore it.
                    self._replicator.replicate_workspace(
                        make_workspace_event(
                            workspace=self.workspace,
                            event_type=ReplicationEventType.CREATE_WORKSPACE,
                        ),
                        WorkspaceEventStream.STANDARD,
                    )

                self._replicator.replicate_workspace(
                    make_workspace_event(
                        workspace=self.workspace,
                        event_type=self.event_type,
                    ),
                    WorkspaceEventStream.STANDARD,
                )
        except OperationalError:
            logger.warning(
                "Database operational error during workspace dual write replication, "
                "workspace_id='%s' event_type='%s'. "
                "The transaction may be retried by pgtransaction.",
                self.workspace.id,
                self.event_type,
                exc_info=True,
            )
            raise
        except Exception as e:
            raise DualWriteException(e)

    def _get_workspace_relationship(self, workspace: Workspace, parent: Workspace):
        """Get the relationship for the workspace."""
        return create_relationship(
            ("rbac", "workspace"),
            str(workspace.id),
            ("rbac", "workspace"),
            str(parent.id),
            "parent",
        )

    def generate_relations_to_add_workspace(self):
        """Generate relations to add workspace."""
        if not self.replication_enabled():
            return

        self.relations_to_add.append(self._get_workspace_relationship(self.workspace, self.workspace.parent))

    def generate_relations_to_update_workspace(self, previous_parent):
        """Generate relations to update workspace."""
        if not self.replication_enabled():
            return

        if self.workspace.parent == previous_parent:
            return
        self.relations_to_remove.append(self._get_workspace_relationship(self.workspace, previous_parent))
        self.relations_to_add.append(self._get_workspace_relationship(self.workspace, self.workspace.parent))

    def generate_relations_to_remove_workspace(self):
        """Reset the relationships for the workspace."""
        if not self.replication_enabled():
            return
        # Remove parent relationship
        self.relations_to_remove.append(self._get_workspace_relationship(self.workspace, self.workspace.parent))
