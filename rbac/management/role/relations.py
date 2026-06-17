#
# Copyright 2025 Red Hat, Inc.
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
"""Contains utilities for handling relations between V2 roles."""

import logging
from typing import Union
from uuid import UUID

from kessel.relations.v1beta1.common_pb2 import Relationship
from management.relation_replicator.logging_replicator import stringify_spicedb_relationship
from management.relation_replicator.types import RelationTuple
from migration_tool.utils import create_relationship

logger = logging.getLogger(__name__)


def deduplicate_role_permission_relationships(
    relationships: list[Union[RelationTuple, Relationship]],
) -> list[Union[RelationTuple, Relationship]]:
    """
    Deduplicate role-to-principal permission relationships.

    When multiple bindings share the same V2 role, each binding generates identical
    role-to-principal permission tuples (e.g., rbac/role:X#inventory_groups_read@rbac/principal:*).
    This function deduplicates these expected duplicates while preserving all other relationships.

    Args:
        relationships: List of RelationTuple or Relationship objects

    Returns:
        Deduplicated list of relationship objects
    """
    seen = set()
    deduplicated = []
    role_permission_duplicates = []

    for rel in relationships:
        key = stringify_spicedb_relationship(rel)

        if key in seen:
            # Check if this is a role-to-principal permission tuple
            is_role_permission = (
                rel.resource.type.namespace == "rbac"
                and rel.resource.type.name == "role"
                and rel.subject.subject.type.namespace == "rbac"
                and rel.subject.subject.type.name == "principal"
                and rel.subject.subject.id == "*"
            )

            if is_role_permission:
                # Expected duplicate - multiple bindings share same V2role
                role_permission_duplicates.append(key)
                continue

        seen.add(key)
        deduplicated.append(rel)

    if role_permission_duplicates:
        logger.info(
            f"Deduplicated {len(role_permission_duplicates)} role-to-principal permission tuples "
            f"(had {len(relationships)} relationships, kept {len(deduplicated)})"
        )

    return deduplicated


def role_child_relationship(parent_uuid: UUID | str, child_uuid: UUID | str) -> RelationTuple:
    """Get the relationship to for a parent-child relationship between the provided roles."""
    return create_relationship(
        resource_name=("rbac", "role"),
        resource_id=str(parent_uuid),
        subject_name=("rbac", "role"),
        subject_id=str(child_uuid),
        relation="child",
    )


def role_owner_relationship(role_uuid: UUID | str, tenant_resource_id: str) -> RelationTuple:
    """Get the relationship for a role's owner tenant (rbac/role:<uuid>#owner@rbac/tenant:<id>)."""
    return create_relationship(
        resource_name=("rbac", "role"),
        resource_id=str(role_uuid),
        subject_name=("rbac", "tenant"),
        subject_id=tenant_resource_id,
        relation="owner",
    )
