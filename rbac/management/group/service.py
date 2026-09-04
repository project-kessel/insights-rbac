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
"""Service layer for group principal synchronization."""

import logging
from typing import List

from management.principal.proxy import external_principal_to_user
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.tenant_service import get_tenant_bootstrap_service

logger = logging.getLogger(__name__)


def sync_new_principals_to_tenant_mapping(principals_needing_v2_sync: List[dict], org_id: str) -> None:
    """Sync newly introduced principals to TenantMapping default/admin groups in SpiceDB.

    Converts BOP response items to User objects and calls update_user() for each
    active principal that has a user_id. Failures are logged per-principal so that
    one bad record does not prevent the remaining principals from being synced.

    Args:
        principals_needing_v2_sync: BOP response items for principals that were
            newly created or had user_id populated for the first time.
        org_id: The organization ID to use as fallback for principals missing org_id.
    """
    if not principals_needing_v2_sync:
        return

    bootstrap_service = get_tenant_bootstrap_service(OutboxReplicator())
    for bop_item in principals_needing_v2_sync:
        try:
            user_obj = external_principal_to_user(bop_item)
            if not user_obj.org_id:
                user_obj.org_id = org_id
            if user_obj.user_id and user_obj.is_active:
                bootstrap_service.update_user(user_obj, upsert=True)
        except Exception:
            logger.warning(
                "Failed to sync TenantMapping membership for principal %s in org %s",
                bop_item.get("username", "unknown"),
                org_id,
                exc_info=True,
            )
