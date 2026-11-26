"""Tests for durable tenant backfill checkpoints."""

from django.test import TestCase

from api.models import Tenant
from internal.tenant_backfill_progress import (
    finish_backfill_progress,
    load_backfill_progress,
    record_backfill_progress,
)
from management.tenant_backfill.model import TenantBackfillProgress


class TenantBackfillProgressTests(TestCase):
    def setUp(self):
        Tenant.objects.exclude(tenant_name="public").delete()
        self.first = Tenant.objects.create(tenant_name="first", org_id="90001")
        self.second = Tenant.objects.create(tenant_name="second", org_id="90002")
        self.query = Tenant.objects.exclude(tenant_name="public")

    def test_seed_requires_exact_count_and_is_only_allowed_once(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            load_backfill_progress(
                "test-backfill",
                self.query,
                resume_after_tenant_id=self.first.id,
                expected_processed_count=2,
            )
        self.assertFalse(TenantBackfillProgress.objects.exists())

        progress = load_backfill_progress(
            "test-backfill",
            self.query,
            resume_after_tenant_id=self.first.id,
            expected_processed_count=1,
        )
        self.assertEqual((progress.last_tenant_id, progress.processed_count), (self.first.id, 1))

        with self.assertRaisesRegex(ValueError, "already has a checkpoint"):
            load_backfill_progress(
                "test-backfill",
                self.query,
                resume_after_tenant_id=self.first.id,
                expected_processed_count=1,
            )

    def test_checkpoint_is_monotonic_and_snapshots_upper_id(self):
        progress = load_backfill_progress("test-backfill", self.query)
        third = Tenant.objects.create(tenant_name="third", org_id="90003")
        self.assertLess(progress.upper_tenant_id, third.id)

        record_backfill_progress(progress, last_tenant_id=self.first.id, scanned_count=1)
        self.assertEqual(load_backfill_progress("test-backfill", self.query).last_tenant_id, self.first.id)
        with self.assertRaisesRegex(ValueError, "must advance"):
            record_backfill_progress(progress, last_tenant_id=self.first.id, scanned_count=1)

        record_backfill_progress(progress, last_tenant_id=self.second.id, scanned_count=1)
        finish_backfill_progress(progress)
        self.assertIsNotNone(load_backfill_progress("test-backfill", self.query).completed_at)
