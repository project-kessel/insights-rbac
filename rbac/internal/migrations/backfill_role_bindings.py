"""Backfill default role bindings for tenants that opted into V2 before eager creation."""

import dataclasses
import logging
import uuid
from typing import Optional, Sequence

from django.db.models import Exists, OuterRef, QuerySet
from management.group.model import Group
from management.permission.scope_service import CONCRETE_SCOPES
from management.role_binding.model import RoleBinding
from management.role_binding.service import RoleBindingService
from management.tenant_mapping.model import DefaultAccessType, TenantMapping

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500

# A systemic problem (unseeded platform roles, an unset platform role UUID setting) leaves every tenant
# unsuccessful, so the run gives up instead of working through the whole tenant set to report the same
# failure once per tenant.
ABORT_THRESHOLD = 10


@dataclasses.dataclass
class BackfillResult:
    """Summary of a default role binding backfill run."""

    tenants_processed: int = 0
    already_complete: int = 0
    tenants_backfilled: int = 0
    incomplete_orgs: list[str] = dataclasses.field(default_factory=list)
    failed_orgs: list[str] = dataclasses.field(default_factory=list)
    aborted: bool = False

    @property
    def incomplete(self) -> int:
        """How many tenants still lack some of their default bindings."""
        return len(self.incomplete_orgs)

    @property
    def errors(self) -> int:
        """How many tenants raised while their bindings were being created."""
        return len(self.failed_orgs)


def _opted_in_mappings() -> QuerySet:
    """Return the V2 opted-in tenant mappings, annotated with whether the tenant has a custom default group.

    v2_write_activated_at is only ever set alongside v2_opted_in_at (enforced by the
    "V2 write activated only if opted in" check constraint), so opt-in alone covers both.
    """
    return (
        TenantMapping.objects.filter(v2_opted_in_at__isnull=False)
        .select_related("tenant")
        .annotate(
            has_custom_default_group=Exists(Group.objects.filter(tenant=OuterRef("tenant"), platform_default=True))
        )
    )


def _expected_default_binding_uuids(mapping: TenantMapping) -> list[uuid.UUID]:
    """Return the UUIDs of every default binding the tenant should have, for every concrete scope.

    ADMIN bindings are always expected. USER bindings are only expected when the tenant has no custom
    default group, since that group replaces the platform default access and ``create_default_bindings``
    skips them (``delete_user_default_bindings`` removes them when such a group appears).

    The mapping must come from ``_opted_in_mappings``, for the ``has_custom_default_group`` annotation.
    """
    access_types = [DefaultAccessType.ADMIN]

    if not mapping.has_custom_default_group:
        access_types.append(DefaultAccessType.USER)

    return [
        mapping.default_role_binding_uuid_for(access_type, scope)
        for access_type in access_types
        for scope in CONCRETE_SCOPES
    ]


def _default_bindings_complete(mapping: TenantMapping) -> bool:
    """Return whether every default binding the tenant should have already exists."""
    expected_uuids = _expected_default_binding_uuids(mapping)

    return RoleBinding.objects.filter(uuid__in=expected_uuids).count() == len(expected_uuids)


def backfill_default_role_bindings(
    org_ids: Optional[Sequence[str]] = None, batch_size: int = DEFAULT_BATCH_SIZE
) -> BackfillResult:
    """Create the missing default role bindings for every V2 opted-in tenant.

    Tenants that opted in before default bindings were created eagerly on opt-in (RHCLOUD-50167)
    have no default bindings until something reads them. This walks every opted-in tenant and
    creates them up front.

    Each tenant's bindings are created in their own transaction (``create_default_bindings`` is
    atomic), and a failure for one tenant does not abort the run. The run does give up once
    ``ABORT_THRESHOLD`` consecutive tenants end up failed or incomplete, since that means the
    cause is not tenant-specific.

    Args:
        org_ids: Optional org IDs to restrict the backfill to, for a staged rollout.
        batch_size: How many tenant mappings to pull from the database at a time.

    Returns:
        A summary of how many tenants were processed and what happened to each.
    """
    query = _opted_in_mappings()

    if org_ids:
        query = query.filter(tenant__org_id__in=org_ids)

    total_count = query.count()
    logger.info("About to backfill default role bindings for %s opted-in tenants.", total_count)

    result = BackfillResult()
    consecutive_unsuccessful = 0

    for mapping in query.order_by("id").iterator(chunk_size=batch_size):
        result.tenants_processed += 1
        org_id = mapping.tenant.org_id
        successful = False

        try:
            if _default_bindings_complete(mapping):
                result.already_complete += 1
                successful = True
            else:
                RoleBindingService(mapping.tenant).create_default_bindings(mapping)

                if _default_bindings_complete(mapping):
                    result.tenants_backfilled += 1
                    successful = True
                else:
                    # create_default_bindings swallows DefaultGroupNotAvailableError, and skips any scope
                    # whose resource is missing, so the tenant needs its platform roles seeded (or its
                    # workspaces created) before a later run can finish the job.
                    result.incomplete_orgs.append(org_id)
                    logger.warning("Default bindings are still incomplete for tenant %s.", org_id)
        except Exception:
            result.failed_orgs.append(org_id)
            logger.exception("Failed to backfill default role bindings for tenant %s.", org_id)

        consecutive_unsuccessful = 0 if successful else consecutive_unsuccessful + 1

        if consecutive_unsuccessful >= ABORT_THRESHOLD:
            result.aborted = True
            logger.error(
                "Aborting after %s consecutive tenants failed or stayed incomplete.", consecutive_unsuccessful
            )
            break

        if result.tenants_processed % batch_size == 0:
            logger.info("Processed %s/%s opted-in tenants.", result.tenants_processed, total_count)

    logger.info("Finished backfilling default role bindings: %s", dataclasses.asdict(result))

    return result
