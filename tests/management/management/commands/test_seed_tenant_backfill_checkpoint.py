"""Tests for the one-time backfill checkpoint seed command."""

from django.core.management import CommandError, call_command
from django.test import TestCase

from api.models import Tenant
from management.tenant_backfill.model import TenantBackfillProgress


class SeedTenantBackfillCheckpointTests(TestCase):
    """Verify manual seeds cannot skip an unverified tenant range."""

    def setUp(self):
        """Create a small ordered tenant range."""
        Tenant.objects.exclude(tenant_name="public").delete()
        self.first = Tenant.objects.create(tenant_name="first", org_id="90001")
        Tenant.objects.create(tenant_name="second", org_id="90002")

    def test_rejects_mismatched_count(self):
        """A cursor alone is insufficient to claim an acknowledged range."""
        with self.assertRaises(CommandError):
            call_command(
                "seed_tenant_backfill_checkpoint",
                job="remove-legacy-root-workspace-tenant-parent",
                after_tenant_id=self.first.id,
                expected_processed_count=2,
            )
        self.assertFalse(TenantBackfillProgress.objects.exists())

    def test_seeds_matching_count_once(self):
        """A verified cursor persists and cannot be overwritten."""
        kwargs = {
            "job": "remove-legacy-root-workspace-tenant-parent",
            "after_tenant_id": self.first.id,
            "expected_processed_count": 1,
        }
        call_command("seed_tenant_backfill_checkpoint", **kwargs)
        progress = TenantBackfillProgress.objects.get()
        self.assertEqual((progress.last_tenant_id, progress.processed_count), (self.first.id, 1))

        with self.assertRaises(CommandError):
            call_command("seed_tenant_backfill_checkpoint", **kwargs)
