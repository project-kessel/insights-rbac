"""Seeds command."""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from migration_tool.migrate import migrate_data

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Command(BaseCommand):
    """Command class for migrating v1 RBAC to v2 relations."""

    help = "Migrates v1 RBAC data to v2 relations"

    def add_arguments(self, parser):
        """Add arguments to command."""
        parser.add_argument("--org-list", nargs="+", default=[])
        parser.add_argument(
            "--exclude-apps",
            nargs="+",
            default=settings.V2_MIGRATION_APP_EXCLUDE_LIST,
            help="List of apps to exclude. Default comes from environment.",
        )
        parser.add_argument(
            "--write-relationships",
            default="False",
            choices=["True", "False", "inventory-api", "outbox"],
            help="Whether to replicate relationships and how. True is == 'inventory-api' for compatibility.",
        )
        parser.add_argument(
            "--skip-roles",
            default="False",
            choices=["True", "False"],
            help="Whether to skip migrate roles.",
        )

    def handle(self, *args, **options):
        """Handle method for command."""
        logger.info("*** Migrating v1 RBAC data to v2 relations... ***")
        kwargs = {
            "exclude_apps": options["exclude_apps"],
            "orgs": options["org_list"],
            "write_relationships": options["write_relationships"],
            "skip_roles": options["skip_roles"] == "True",
        }
        migrate_data(**kwargs)
        logger.info("*** Migration completed. ***\n")
