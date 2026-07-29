"""Custom logging filters for RBAC."""

import logging

from rbac.request_context import org_id_var, request_id_var, user_id_var, user_type_var


class EnvironmentFilter(logging.Filter):
    """Injects the deployment environment name into every log record."""

    def __init__(self, env_name, **kwargs):
        """Initialize with the deployment environment name."""
        super().__init__(**kwargs)
        self._env_name = env_name

    def filter(self, record: logging.LogRecord) -> bool:
        """Add env_name attribute to the log record."""
        record.env_name = self._env_name
        return True


class RequestContextFilter(logging.Filter):
    """Injects request context from ``contextvars`` into every log record.

    Attributes added to each record:
    * ``request_id`` -- from ``X-RH-INSIGHTS-REQUEST-ID`` header or a
      generated fallback UUID.  Defaults to ``"-"`` outside a request.
    * ``org_id`` -- organisation identifier from the identity header.
    * ``user_id`` -- user ID from the identity header (or ``client_id``
      for service accounts).
    * ``user_type`` -- ``"user"``, ``"service_account"``, or ``"-"``.

    Safe defaults (``"-"``) ensure that log lines emitted outside a request
    context (Celery tasks, management commands, startup) never crash.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Read context variables and attach them to *record*."""
        record.request_id = request_id_var.get()
        record.org_id = org_id_var.get()
        record.user_id = user_id_var.get()
        record.user_type = user_type_var.get()
        return True
