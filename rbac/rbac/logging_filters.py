"""Custom logging filters for RBAC."""

import logging
import os
from typing import ClassVar


class EnvironmentFilter(logging.Filter):
    """Injects the deployment environment name into every log record."""

    _env_name: ClassVar[str] = os.getenv("ENV_NAME", "stage")

    def filter(self, record: logging.LogRecord) -> bool:
        """Add env_name attribute to the log record."""
        record.env_name = self._env_name
        return True
