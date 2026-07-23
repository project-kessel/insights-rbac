"""Management command to delete stale workspace(root)#parent@tenant tuples."""

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
import logging

from django.core.management import BaseCommand, CommandError
from internal.utils import remove_legacy_root_workspace_tenant_parent_relations

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Remove legacy workspace(root)#parent@tenant tuples via the outbox."""

    help = (
        "Enqueue removal of legacy workspace(root)#parent@tenant relationship tuples "
        "(superseded hierarchy; bootstrapping no longer creates this edge)."
    )

    def add_arguments(self, parser):
        """Add arguments for the command."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="process all non-public tenants",
        )

    def handle(self, *args, **options):
        """Execute the command."""
        if not options["all"]:
            raise CommandError(
                "Must pass --all to process all non-public tenants.",
                returncode=2,
            )

        logger.info("Removing legacy root workspace parent=tenant tuples for all non-public tenants.")

        result = remove_legacy_root_workspace_tenant_parent_relations()

        if result.get("skipped"):
            reason = result.get("reason", "skipped")
            logger.warning("Skipped: %s", reason)
            return

        logger.info("Processed %s tenants", result["tenants_processed"])
