"""Management command to backfill default role bindings for V2 opted-in tenants."""

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
from django.core.management import BaseCommand, CommandError
from internal.migrations.backfill_role_bindings import DEFAULT_BATCH_SIZE, backfill_default_role_bindings


class Command(BaseCommand):
    """Create the missing default role bindings for every V2 opted-in tenant."""

    help = (
        "Backfill default role bindings for tenants that opted into V2 before "
        "default bindings were created eagerly on opt-in."
    )

    def add_arguments(self, parser):
        """Add arguments for the command."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="process all V2 opted-in tenants",
        )
        parser.add_argument(
            "--org-ids",
            default="",
            help="comma-separated org IDs to restrict the backfill to (for a staged rollout)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"how many tenant mappings to read at a time (default: {DEFAULT_BATCH_SIZE})",
        )

    def handle(self, *args, **options):
        """Execute the command."""
        org_ids = [org_id.strip() for org_id in options["org_ids"].split(",") if org_id.strip()]

        if not options["all"] and not org_ids:
            raise CommandError(
                "Must pass --all to process all V2 opted-in tenants, or --org-ids to process specific ones.",
                returncode=2,
            )

        if options["batch_size"] <= 0:
            raise CommandError("Batch size must be positive.", returncode=2)

        result = backfill_default_role_bindings(org_ids=org_ids, batch_size=options["batch_size"])

        if result.errors:
            raise CommandError(
                "Failed to backfill default role bindings for tenants with the following "
                f"org_ids: {result.failed_orgs}",
                returncode=1,
            )

        # Incomplete tenants are not errors, but the backfill has not done its job for them and the lazy
        # creation path cannot be removed (RHCLOUD-50990) until a run leaves none behind.
        if result.incomplete:
            raise CommandError(
                "Default bindings are still incomplete for tenants with the following "
                f"org_ids: {result.incomplete_orgs}",
                returncode=1,
            )
