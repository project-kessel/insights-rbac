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

"""Migration job NOTIFY coordination shared by outbox, producers, and the Kafka consumer."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Coordinates ``remove_legacy_root_workspace_tenant_parent_relations`` with the RBAC Kafka consumer.
REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_CHANNEL = "remove_legacy_root_workspace_parent_batch"

# Coordinates ``migrate_binding_scope`` with the RBAC Kafka consumer.
MIGRATE_BINDING_SCOPE_NOTIFY_CHANNEL = "migrate_binding_scope_batch"

MIGRATION_NOTIFY_TIMEOUT_SECONDS = 600

REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS = "remove_root_parent_tenant_relationships"
MIGRATE_BINDING_SCOPE = "migrate_binding_scope"


@dataclass(frozen=True)
class MigrationNotifyCoordination:
    """Configuration shared by migration producers, outbox context, and the Kafka consumer."""

    channel: str
    log_label: str
    timeout_seconds: float = MIGRATION_NOTIFY_TIMEOUT_SECONDS
    include_org_id_in_context: bool = True
    require_notify_token: bool = True


MIGRATION_NOTIFY_COORDINATIONS: dict[str, MigrationNotifyCoordination] = {
    REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS: MigrationNotifyCoordination(
        channel=REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_CHANNEL,
        log_label="remove_legacy_root_workspace_tenant_parent",
        include_org_id_in_context=False,
        require_notify_token=True,
    ),
    MIGRATE_BINDING_SCOPE: MigrationNotifyCoordination(
        channel=MIGRATE_BINDING_SCOPE_NOTIFY_CHANNEL,
        log_label="migrate_binding_scope",
        include_org_id_in_context=True,
        require_notify_token=False,
    ),
}


def _event_type_key(event_type: object) -> str:
    return event_type.value if hasattr(event_type, "value") else str(event_type)


def migration_notify_coordination(
    event_type: object,
) -> MigrationNotifyCoordination | None:
    """Return coordination config for ``event_type``, or ``None`` if not a coordinated migration."""
    return MIGRATION_NOTIFY_COORDINATIONS.get(_event_type_key(event_type))


def build_migration_notify_resource_context(
    event_type: object,
    event_info: dict[str, object],
    coordination: MigrationNotifyCoordination,
) -> dict[str, object] | None:
    """Build resource context when ``notify_token`` is present; otherwise return ``None``."""
    token = event_info.get("notify_token")
    if not token:
        if coordination.require_notify_token:
            logger.warning(
                "%s event missing notify_token in event_info=%s",
                _event_type_key(event_type),
                event_info,
            )
        else:
            logger.debug(
                "%s event missing optional notify_token in event_info=%s",
                _event_type_key(event_type),
                event_info,
            )
        return None

    org_id = str(event_info.get("org_id", "")) if coordination.include_org_id_in_context else ""
    return {
        "org_id": org_id,
        "event_type": _event_type_key(event_type),
        "created_at": int(time.time()),
        "notify_token": str(token),
    }


def notify_migration_batch_completion(
    event_type: str | None,
    resource_context: dict[str, object] | None,
    send_notify: Callable[[str, str, str], None],
) -> None:
    """NOTIFY the migration producer after the consumer applies a coordinated batch."""
    if not event_type or not resource_context:
        return

    notify_token = resource_context.get("notify_token")
    if not notify_token:
        return

    coordination = migration_notify_coordination(event_type)
    if coordination is None:
        return

    payload = str(notify_token).strip()
    send_notify(coordination.channel, payload, f"{event_type} batch (token={payload})")
