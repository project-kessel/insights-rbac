#
# Copyright 2019 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Management application configuration module."""

import logging
import sys

from django.apps import AppConfig
from django.db.utils import OperationalError, ProgrammingError
from management.seeds import group_seeding, permission_seeding, role_seeding

from rbac.settings import (
    ENV_NAME,
    GIT_COMMIT,
    GROUP_SEEDING_ENABLED,
    KAFKA_ENABLED,
    PERMISSION_SEEDING_ENABLED,
    REPLICATION_TO_RELATION_ENABLED,
    ROLE_SEEDING_ENABLED,
    V2_APIS_ENABLED,
)

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class ManagementConfig(AppConfig):
    """Management application configuration."""

    name = "management"

    def ready(self):
        """Determine if app is ready on application startup."""
        # Don't run on Django tab completion commands
        if "manage.py" in sys.argv[0] and "runserver" not in sys.argv:
            return

        # Startup configuration - SEC-MON-REQ-1 compliance (EOI-3 admin_action)
        logger.info(
            "Service started",
            extra={
                "action": "START",
                "resource_type": "service",
                "outcome": "success",
                "principal": "system:django:app_ready",
                "version": GIT_COMMIT,
                "v2_apis_enabled": V2_APIS_ENABLED,
                "replication_to_relation_enabled": REPLICATION_TO_RELATION_ENABLED,
                "env_name": ENV_NAME,
                "kafka_enabled": KAFKA_ENABLED,
            },
        )

        try:
            if PERMISSION_SEEDING_ENABLED:
                permission_seeding()
            if ROLE_SEEDING_ENABLED:
                role_seeding()
            if GROUP_SEEDING_ENABLED:
                group_seeding()

        except (OperationalError, ProgrammingError) as op_error:
            if "no such table" in str(op_error) or "does not exist" in str(op_error):
                # skip this if we haven't created tables yet.
                return
            else:
                logger.error("Error: %s.", op_error)
