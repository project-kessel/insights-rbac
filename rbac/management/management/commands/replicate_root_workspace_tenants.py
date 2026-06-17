"""Command to replicate tenants for existing root workspaces."""

from django.core.management import BaseCommand, CommandError
from internal.migrations.replicate_root_workspace_tenants import replicate_root_workspace_tenants


class Command(BaseCommand):
    """Command for replicating tenant relations for root workspaces."""

    help = "Replicate existing default workspaces."

    def add_arguments(self, parser):
        """Parse command arguments."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="whether to replicate tenants for all root workspaces (required for forwards compatibility)",
        )

        parser.add_argument(
            "--sleep-sec",
            type=float,
            default=0.0,
            help=(
                "if positive, the number of seconds to sleep in between each replication batch "
                "(defaults to 0, no sleeping)"
            ),
        )

    def handle(self, **options):
        """Run the command."""
        if not options["all"]:
            raise CommandError("Must pass --all to request replicating tenants for all root workspaces", returncode=1)

        replicate_root_workspace_tenants(batch_sleep_seconds=options["sleep_sec"])
