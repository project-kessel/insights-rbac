"""Seed a new tenant backfill from a verified consumer-acknowledged batch."""

from django.core.management import BaseCommand, CommandError
from internal.tenant_backfill_progress import load_backfill_progress

from api.models import Tenant


class Command(BaseCommand):
    """Initialize a checkpoint before replacing a legacy, non-resumable job."""

    help = "Seed a tenant backfill checkpoint from a verified consumer-acknowledged batch."

    def add_arguments(self, parser):
        """Parse the backfill name and its verified last acknowledged tenant."""
        parser.add_argument(
            "--job",
            required=True,
            choices=["remove-legacy-root-workspace-tenant-parent", "replicate-root-workspace-tenants"],
        )
        parser.add_argument("--after-tenant-id", type=int, required=True)
        parser.add_argument("--expected-processed-count", type=int, required=True)

    def handle(self, **options):
        """Create a checkpoint only when the ID and count agree with the database."""
        job_name = options["job"]
        if job_name == "remove-legacy-root-workspace-tenant-parent":
            query = Tenant.objects.exclude(tenant_name="public")
        else:
            query = Tenant.objects.exclude(org_id=None)

        try:
            progress = load_backfill_progress(
                job_name,
                query,
                resume_after_tenant_id=options["after_tenant_id"],
                expected_processed_count=options["expected_processed_count"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"Seeded {job_name} at tenant ID {progress.last_tenant_id} "
            f"({progress.processed_count} of {progress.total_count} tenants)."
        )
