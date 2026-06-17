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

"""Backfill rbac/role#owner@rbac/tenant for custom V2 roles from each role's tenant row.

Scans the ``RoleV2`` table only (custom roles are on the order of thousands), not the tenant table
(millions of rows). Each batch is locked with ``select_for_update()`` inside an ``atomic_block()``
to prevent concurrent V1 dual-write operations (which run at READ COMMITTED) from modifying roles
mid-replication.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from management.atomic_transactions import atomic_block
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
)
from management.role.relations import role_owner_relationship
from management.role.v2_model import RoleV2

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 500


class TenantResourceIdMissingError(ValueError):
    """Raised when a custom role's tenant has no ``tenant_resource_id()`` (no ``org_id``)."""


def _require_tenant_resource_id(role: RoleV2) -> str:
    resource_id = role.tenant.tenant_resource_id()
    if resource_id:
        return resource_id
    tenant = role.tenant
    raise TenantResourceIdMissingError(
        f"Tenant for custom role has no tenant_resource_id (org_id missing): "
        f"role_uuid={role.uuid} role_name={role.name!r} tenant_id={tenant.pk}"
    )


def _base_queryset():
    """Return all custom V2 roles (custom roles are only created for org tenants in practice)."""
    return RoleV2.objects.filter(type=RoleV2.Types.CUSTOM).select_related("tenant").order_by("pk")


def _replicate_role(role: RoleV2, replicator: RelationReplicator) -> None:
    resource_id = _require_tenant_resource_id(role)
    tenant = role.tenant

    replicator.replicate(
        ReplicationEvent(
            event_type=ReplicationEventType.UPDATE_CUSTOM_ROLE,
            info={
                "role_uuid": str(role.uuid),
                "org_id": str(tenant.org_id) if tenant.org_id else "",
            },
            partition_key=PartitionKey.byEnvironment(),
            add=[role_owner_relationship(role.uuid, resource_id)],
            remove=[],
        )
    )
    logger.info(
        "Replicated owner tuple for custom V2 role uuid=%s name=%r tenant org_id=%s",
        role.uuid,
        role.name,
        tenant.org_id,
    )


def replicate_custom_v2_role_owner_relationships(
    *,
    replicator: RelationReplicator | None = None,
) -> dict[str, Any]:
    """Emit owner tuples for every custom V2 role from ``role.tenant.tenant_resource_id()``.

    Iterates **custom roles only** (typically thousands of rows), not tenants. Does not change PostgreSQL
    role rows; only replicates ``rbac/role:<uuid>#owner@rbac/tenant:<id>``.

    Raises ``TenantResourceIdMissingError`` if any custom role's tenant has no ``tenant_resource_id()``.

    Each batch of up to ``DEFAULT_CHUNK_SIZE`` roles is locked with ``select_for_update()`` inside
    an ``atomic_block()`` (SERIALIZABLE) to guard against concurrent V1 dual-write operations that
    run at READ COMMITTED. Keyset pagination (``pk__gt``) is used to advance between batches.
    """
    if replicator is None:
        if not settings.REPLICATION_TO_RELATION_ENABLED:
            raise ValueError("Replication to relations is disabled")
        replicator = OutboxReplicator()

    replicated_count = 0
    last_pk = 0

    while True:
        with atomic_block():
            chunk = list(_base_queryset().filter(pk__gt=last_pk).select_for_update()[:DEFAULT_CHUNK_SIZE])
            if not chunk:
                break
            for role in chunk:
                _replicate_role(role, replicator)
                replicated_count += 1
            last_pk = chunk[-1].pk

    return {
        "replicated_count": replicated_count,
    }
