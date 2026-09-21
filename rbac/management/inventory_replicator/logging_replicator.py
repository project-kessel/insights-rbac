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

"""InventoryReplicator which just logs added tuples."""

import logging
from typing import Union

from kessel.inventory.v1beta2 import relationship_pb2
from management.inventory_replicator.inventory_replicator import InventoryReplicator, ReplicationEvent
from management.inventory_replicator.types import RelationTuple

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class LoggingReplicator(InventoryReplicator):
    """Just logs relations."""

    def replicate(self, event: ReplicationEvent):
        """Log the event's tuples."""
        for rel in event.add:
            logger.info(stringify_spicedb_relationship(rel))


def stringify_spicedb_relationship(rel: Union[RelationTuple, relationship_pb2.Relationship]):
    """Stringify a relationship for logging.

    Works with both RelationTuple and protobuf Relationship since they
    share the same nested field structure (resource.type.name, etc.).
    """
    return (
        f"{rel.resource.type.name}:{rel.resource.id}#{rel.relation}@{rel.subject.subject.type.name}:"
        f"{rel.subject.subject.id}"
    )
