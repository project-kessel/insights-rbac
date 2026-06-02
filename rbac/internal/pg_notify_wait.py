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

"""Helpers to wait on PostgreSQL NOTIFY (coordination with the RBAC Kafka consumer)."""

import logging
import select
import time
import uuid
from collections import deque
from collections.abc import Callable

from django.db import connection, transaction
from internal.migration_coordination import migration_notify_coordination
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
    WorkspaceEvent,
    WorkspaceEventStream,
)
from psycopg2 import sql

logger = logging.getLogger(__name__)


def replicate_with_notify(
    replicator: RelationReplicator,
    event: ReplicationEvent,
) -> None:
    """Enqueue a replication event and LISTEN until the consumer NOTIFYs batch completion.

    When called inside ``transaction.atomic``, the LISTEN is deferred until after commit so the
    outbox row is visible to Debezium before the consumer can process it and send NOTIFY.
    """
    coordination = migration_notify_coordination(event.event_type)
    if coordination is None:
        raise ValueError(f"Event type {event.event_type.value} is not notify-coordinated")

    notify_token = str(uuid.uuid4())
    event.event_info["notify_token"] = notify_token
    replicator.replicate(event)

    def wait_for_batch_completion() -> None:
        wait_for_pg_notify(
            channel=coordination.channel,
            expected_payload=notify_token,
            timeout_seconds=coordination.timeout_seconds,
            log_label=coordination.log_label,
        )

    if connection.in_atomic_block:
        transaction.on_commit(wait_for_batch_completion)
    else:
        wait_for_batch_completion()


def wait_for_pg_notify(
    *,
    channel: str,
    expected_payload: str,
    timeout_seconds: float,
    log_label: str,
    on_success: Callable[[float], None] | None = None,
    on_timeout: Callable[[float], None] | None = None,
    timeout_error_message: str | None = None,
) -> None:
    """
    LISTEN on ``channel`` until a NOTIFY is received with payload matching ``expected_payload``.

    Shared by read-your-writes (workspace create) and migration jobs that coordinate with the
    RBAC Kafka consumer.

    Args:
        channel: PostgreSQL NOTIFY channel name (must be a valid unquoted identifier).
        expected_payload: Payload string to match (stripped of whitespace).
        timeout_seconds: Max seconds to wait; use ``<= 0`` to skip waiting (e.g. tests).
        log_label: Prefix for log messages (e.g. ``"[Service] RYW"``).
        on_success: Optional callback with elapsed seconds when a matching NOTIFY is received.
        on_timeout: Optional callback with elapsed seconds before :class:`TimeoutError` is raised.
        timeout_error_message: If set, used as the :class:`TimeoutError` message instead of a generic one.

    Raises:
        TimeoutError: If no matching NOTIFY arrives in time (only when timeout is positive).
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        logger.debug(
            "%s skipped waiting for NOTIFY (non-positive timeout) channel=%s payload=%s",
            log_label,
            channel,
            expected_payload,
        )
        return

    listen_sql = sql.SQL("LISTEN {};").format(sql.Identifier(channel))
    unlisten_sql = sql.SQL("UNLISTEN {};").format(sql.Identifier(channel))
    try:
        connection.ensure_connection()
        conn = connection.connection

        with connection.cursor() as cursor:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query
            cursor.execute(listen_sql)

        logger.info(
            "%s waiting for NOTIFY channel=%s payload=%s timeout=%ss",
            log_label,
            channel,
            expected_payload,
            timeout_seconds,
        )

        started = time.monotonic()
        deadline = started + float(timeout_seconds)
        expected_payload_str = str(expected_payload).strip()

        try:
            conn.poll()
            if getattr(conn, "notifies", None):
                conn.notifies.clear()
        except Exception:
            logger.debug("%s: failed to clear stale notifications before LISTEN, continuing anyway", log_label)

        fd = conn.fileno() if hasattr(conn, "fileno") else conn

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            readable, _, _ = select.select([fd], [], [], min(1.0, remaining))
            if not readable:
                continue

            conn.poll()
            notifies = getattr(conn, "notifies", None)
            if notifies:
                q = deque(notifies)
                notifies.clear()
                while q:
                    n = q.popleft()
                    payload = (getattr(n, "payload", "") or "").strip()
                    if n.channel == channel and payload == expected_payload_str:
                        duration = time.monotonic() - started
                        logger.info(
                            "%s received NOTIFY channel=%s payload=%s after %.3fs",
                            log_label,
                            channel,
                            payload,
                            duration,
                        )
                        if on_success is not None:
                            on_success(duration)
                        return

        duration = time.monotonic() - started
        logger.error(
            "%s timed out waiting for NOTIFY channel=%s payload=%s after %ss",
            log_label,
            channel,
            expected_payload_str,
            timeout_seconds,
        )
        if on_timeout is not None:
            on_timeout(duration)
        raise TimeoutError(
            timeout_error_message
            if timeout_error_message is not None
            else f"{log_label}: timed out after {timeout_seconds}s waiting for NOTIFY on {channel}"
        )
    except TimeoutError:
        raise
    except Exception:
        logger.exception("%s: error while waiting for NOTIFY", log_label)
        raise
    finally:
        try:
            with connection.cursor() as cursor:
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query
                cursor.execute(unlisten_sql)
        except Exception:
            pass


class NotifyCoordinatedReplicator(RelationReplicator):
    """Replicator wrapper that waits for the Kafka consumer to acknowledge each replicated event."""

    def __init__(
        self,
        inner: RelationReplicator,
        *,
        event_type: ReplicationEventType,
    ):
        """Wrap ``inner`` and wait for consumer NOTIFY after each coordinated ``replicate`` call."""
        if migration_notify_coordination(event_type) is None:
            raise ValueError(f"Event type {event_type.value} is not notify-coordinated")
        self._inner = inner
        self._event_type = event_type

    def replicate(self, event: ReplicationEvent) -> None:
        """Replicate ``event`` and block until the consumer acknowledges it."""
        if event.event_type != self._event_type:
            raise ValueError(
                f"NotifyCoordinatedReplicator configured for {self._event_type.value}, "
                f"got {event.event_type.value}"
            )
        replicate_with_notify(self._inner, event)

    def replicate_workspace(self, event: WorkspaceEvent, event_stream: WorkspaceEventStream) -> None:
        """Delegate workspace replication to the wrapped replicator."""
        self._inner.replicate_workspace(event, event_stream)
