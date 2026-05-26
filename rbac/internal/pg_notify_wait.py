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
from collections import deque
from collections.abc import Callable

from django.db import connection
from psycopg2 import sql

logger = logging.getLogger(__name__)

# Coordinates ``remove_legacy_root_workspace_tenant_parent_relations`` with the RBAC Kafka consumer.
REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_CHANNEL = "remove_legacy_root_workspace_parent_batch"
REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_TIMEOUT_SECONDS = 600


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
