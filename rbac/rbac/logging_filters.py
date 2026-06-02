"""Custom logging filters for RBAC."""

import logging


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
