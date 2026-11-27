"""Persist tenant backfill progress only after the consumer acknowledges a batch."""

from django.utils import timezone
from management.tenant_backfill.model import TenantBackfillProgress


def load_backfill_progress(
    job_name: str,
    tenant_query,
    *,
    resume_after_tenant_id: int | None = None,
    expected_processed_count: int | None = None,
) -> TenantBackfillProgress:
    """Load the saved cursor, or snapshot the upper tenant ID for a new run.

    An operator may seed a new run from a previously acknowledged batch. Both
    values are required so a mistaken tenant ID cannot silently skip records.
    """
    seeded = resume_after_tenant_id is not None or expected_processed_count is not None
    if seeded and (resume_after_tenant_id is None or expected_processed_count is None):
        raise ValueError("Both resume_after_tenant_id and expected_processed_count are required")

    existing = TenantBackfillProgress.objects.filter(job_name=job_name).first()
    if existing is not None:
        if seeded:
            raise ValueError(f"Backfill {job_name} already has a checkpoint; omit the initial seed")
        return existing

    upper_tenant_id = tenant_query.order_by("-id").values_list("id", flat=True).first() or 0
    bounded_query = tenant_query.filter(id__lte=upper_tenant_id)
    total_count = bounded_query.count()
    after_id = 0 if resume_after_tenant_id is None else resume_after_tenant_id
    processed_count = 0 if expected_processed_count is None else expected_processed_count

    if after_id < 0 or after_id > upper_tenant_id:
        raise ValueError(f"Resume tenant ID must be between 0 and {upper_tenant_id}")
    if seeded and bounded_query.filter(id__lte=after_id).count() != processed_count:
        raise ValueError("Resume tenant ID does not match the acknowledged tenant count")

    progress, created = TenantBackfillProgress.objects.get_or_create(
        job_name=job_name,
        defaults={
            "last_tenant_id": after_id,
            "processed_count": processed_count,
            "upper_tenant_id": upper_tenant_id,
            "total_count": total_count,
        },
    )
    if seeded and not created:
        raise ValueError(f"Backfill {job_name} already has a checkpoint; omit the initial seed")
    return progress


def record_backfill_progress(progress: TenantBackfillProgress, *, last_tenant_id: int, scanned_count: int) -> None:
    """Advance a cursor after its batch was acknowledged, or after skipped tenants."""
    if scanned_count <= 0 or not progress.last_tenant_id < last_tenant_id <= progress.upper_tenant_id:
        raise ValueError("Backfill checkpoint must advance within the initial tenant range")

    updated_at = timezone.now()
    updated = TenantBackfillProgress.objects.filter(
        pk=progress.pk,
        last_tenant_id=progress.last_tenant_id,
        completed_at__isnull=True,
    ).update(
        last_tenant_id=last_tenant_id,
        processed_count=progress.processed_count + scanned_count,
        updated_at=updated_at,
    )
    if updated != 1:
        raise RuntimeError("Backfill checkpoint changed concurrently; stopping this worker")

    progress.last_tenant_id = last_tenant_id
    progress.processed_count += scanned_count
    progress.updated_at = updated_at


def finish_backfill_progress(progress: TenantBackfillProgress) -> None:
    """Mark a run complete after all its batches are acknowledged."""
    if progress.completed_at is not None:
        return

    completed_at = timezone.now()
    updated = TenantBackfillProgress.objects.filter(
        pk=progress.pk,
        last_tenant_id=progress.last_tenant_id,
        completed_at__isnull=True,
    ).update(completed_at=completed_at, updated_at=completed_at)
    if updated != 1:
        raise RuntimeError("Backfill checkpoint changed concurrently; stopping this worker")
    progress.completed_at = completed_at
    progress.updated_at = completed_at
