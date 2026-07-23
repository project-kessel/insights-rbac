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
"""Provide common helpers for V2 tenant relationships."""

import logging
from typing import Iterable, Optional

from management.group.platform import GlobalPolicyIdService
from management.permission.scope_service import Scope, TenantScopeResources
from management.relation_replicator.types import RelationTuple
from management.role.platform import platform_v2_role_uuid_for
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from migration_tool.utils import create_relationship

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def default_role_binding_tuples(
    tenant_mapping: TenantMapping,
    target_resources: TenantScopeResources,
    access_type: DefaultAccessType,
    policy_service: GlobalPolicyIdService,
    resource_binding_only: bool = False,
    target_scopes: Optional[Iterable[Scope]] = None,
) -> list[RelationTuple]:
    """
    Create the tuples used to bootstrap default access for a Workspace.

    If resource_binding_only is true, only return the relationship that binds the role binding to the target resource.
    This might be used when removing default access from a resource (while still leaving the role binding itself
    around, if it already exists).

    The optional policy_cache argument can be used to prevent redundant policy UUID lookups across calls.
    """
    target_scopes = set(target_scopes if target_scopes is not None else Scope)

    default_group_uuid = str(tenant_mapping.group_uuid_for(access_type))

    relationships: list[RelationTuple] = []

    for scope in target_scopes:
        # Always add the relationship from the role binding to the target resource.
        role_binding_uuid = str(tenant_mapping.default_role_binding_uuid_for(access_type, scope))
        target = target_resources.resource_for(scope)

        relationships.append(
            create_relationship(
                target.resource_type,
                target.resource_id,
                ("rbac", "role_binding"),
                role_binding_uuid,
                "binding",
            )
        )

        # Only add the remaining relationships if requested.
        if not resource_binding_only:
            # Since computing the role UUID can throw, only do it if necessary.
            default_role_uuid = str(platform_v2_role_uuid_for(access_type, scope, policy_service=policy_service))

            relationships.extend(
                [
                    create_relationship(
                        ("rbac", "role_binding"),
                        role_binding_uuid,
                        ("rbac", "role"),
                        default_role_uuid,
                        "role",
                    ),
                    create_relationship(
                        ("rbac", "role_binding"),
                        role_binding_uuid,
                        ("rbac", "group"),
                        default_group_uuid,
                        "subject",
                        "member",
                    ),
                ]
            )

    return relationships
