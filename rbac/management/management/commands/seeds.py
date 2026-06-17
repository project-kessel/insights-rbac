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
"""Seeds command."""

import logging

from django.core.management import CommandError
from django.core.management.base import BaseCommand
from management.seeds import group_seeding, permission_seeding, role_seeding

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Command(BaseCommand):
    """Command class for running seeds."""

    help = "Runs the seeding for roles, permissions and groups"

    def add_arguments(self, parser):
        """Add arguments to command."""
        parser.add_argument("--permissions", action="store_true")
        parser.add_argument("--roles", action="store_true")
        parser.add_argument("--groups", action="store_true")
        parser.add_argument("--force-create-relationships", action="store_true")
        parser.add_argument("--force-update-relationships", action="store_true")
        parser.add_argument("--skip-notifications", action="store_true")

    def handle(self, *args, **options):
        """Handle method for command."""
        seed_all = not (options["permissions"] or options["roles"] or options["groups"])

        force_create_relationships = options.get("force_create_relationships", False)
        force_update_relationships = options.get("force_update_relationships", False)
        skip_notifications = options.get("skip_notifications", False)

        if force_create_relationships and force_update_relationships:
            raise CommandError("Relationships cannot be both forcibly created and forcibly updated!", returncode=1)

        if options["permissions"] or seed_all:
            logger.info("*** Seeding permissions... ***")
            permission_seeding(skip_notifications=skip_notifications)
            logger.info("*** Permission seeding completed. ***\n")

        if options["roles"] or seed_all:
            logger.info("*** Seeding roles... ***")
            logger.info(f"Running with force-create-relationships: {force_create_relationships}")
            logger.info(f"Running with force-update-relationships: {force_update_relationships}")
            logger.info(f"Running with skip-notifications: {skip_notifications}")

            role_seeding(
                force_create_relationships=force_create_relationships,
                force_update_relationships=force_update_relationships,
                skip_notifications=skip_notifications,
            )

            logger.info("*** Role seeding completed. ***\n")

        if options["groups"] or seed_all:
            logger.info("*** Seeding groups... ***")
            group_seeding(skip_notifications=skip_notifications)
            logger.info("*** Group seeding completed. ***\n")

        # Since the cache will expire in 10 min. We can let it expire by itself. Not worth to explicitly expire it
        # currently becuthere might be some other unexpected issues. Can enable it in the future if it becomes an issue.
        # purge_cache_for_all_tenants()
