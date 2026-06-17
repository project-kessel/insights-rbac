"""Command to replicate existing default workspaces."""

from django.core.management import BaseCommand, CommandError
from internal.migrations.replicate_default_workspaces import replicate_default_workspaces


class Command(BaseCommand):
    """Command for replicating existing default workspaces."""

    help = "Replicate existing default workspaces."

    def add_arguments(self, parser):
        """Parse command arguments."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="whether to replicate all default workspaces (required for forwards compatibility)",
        )

    def handle(self, **options):
        """Run the command."""
        if not options["all"]:
            raise CommandError("Must pass --all to request replicating all default workspaces", returncode=1)

        replicate_default_workspaces()
