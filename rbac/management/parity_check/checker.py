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
"""Parity access checker for comparing RBAC access with Kessel PDP."""

import logging
import random
import time
from dataclasses import dataclass, field

import sentry_sdk
from django.conf import settings
from management.group.model import Group
from management.parity_check.metrics import (
    parity_check_duration_seconds,
    parity_checks_total,
    parity_discrepancies_total,
    parity_job_runs_total,
    parity_principals_checked_total,
    parity_tenants_checked_total,
    parity_workspace_check_duration_seconds,
)
from management.permissions.workspace_inventory_access import WorkspaceInventoryAccessChecker
from management.principal.model import Principal
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from management.tenant_mapping.model import TenantMapping

from api.models import Tenant

logger = logging.getLogger(__name__)

# Default relation to check for workspace access
WORKSPACE_VIEW_RELATION = "workspace_view"


@dataclass
class ParityCheckResult:
    """Result of a parity check for a single principal."""

    org_id: str
    principal_id: str
    user_id: str
    rbac_workspaces: set[str] = field(default_factory=set)
    pdp_workspaces: set[str] = field(default_factory=set)
    match: bool = True
    only_in_rbac: set[str] = field(default_factory=set)
    only_in_pdp: set[str] = field(default_factory=set)
    error: str | None = None

    def has_discrepancy(self) -> bool:
        """Check if there is a discrepancy between RBAC and PDP."""
        return bool(self.only_in_rbac or self.only_in_pdp)


@dataclass
class ParityJobResult:
    """Result of a full parity check job run."""

    tenants_checked: int = 0
    principals_checked: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    discrepancies: list[ParityCheckResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


class ParityAccessChecker:
    """Compares RBAC access with Kessel PDP for workspace access."""

    def __init__(
        self,
        tenant_sample_size: int | None = None,
        principal_sample_size: int | None = None,
        relation: str = WORKSPACE_VIEW_RELATION,
    ):
        """Initialize the parity checker.

        Args:
            tenant_sample_size: Maximum number of v2-enabled tenants to check.
                If None, uses PARITY_CHECK_TENANT_SAMPLE_SIZE setting.
            principal_sample_size: Maximum number of principals per tenant to check.
                If None, uses PARITY_CHECK_PRINCIPAL_SAMPLE_SIZE setting.
            relation: The relation to check for workspace access (default: workspace_view).
        """
        self.tenant_sample_size: int = (
            tenant_sample_size
            if tenant_sample_size is not None
            else getattr(settings, "PARITY_CHECK_TENANT_SAMPLE_SIZE", 10)
        )
        self.principal_sample_size: int = (
            principal_sample_size
            if principal_sample_size is not None
            else getattr(settings, "PARITY_CHECK_PRINCIPAL_SAMPLE_SIZE", 50)
        )
        self.relation = relation
        self.inventory_checker = WorkspaceInventoryAccessChecker()

    def get_bootstrapped_tenants(self) -> list[Tenant]:
        """Get all bootstrapped tenants (those with TenantMapping records)."""
        tenant_mappings = TenantMapping.objects.select_related("tenant").all()

        tenants = [tm.tenant for tm in tenant_mappings if tm.tenant.org_id is not None]

        total_count = len(tenants)
        if total_count > self.tenant_sample_size:
            tenants = random.sample(tenants, self.tenant_sample_size)
            logger.info(
                "Sampled %d tenants from %d bootstrapped tenants",
                self.tenant_sample_size,
                total_count,
            )

        return tenants

    def get_principals_for_tenant(self, tenant: Tenant) -> list[Principal]:
        """Get principals for a tenant that have user_id set (required for PDP checks)."""
        principals = list(
            Principal.objects.filter(
                tenant=tenant,
                user_id__isnull=False,
                type=Principal.Types.USER,
            ).exclude(user_id="")
        )

        if len(principals) > self.principal_sample_size:
            principals = random.sample(principals, self.principal_sample_size)
            logger.debug(
                "Sampled %d principals from tenant %s",
                self.principal_sample_size,
                tenant.org_id,
            )

        return principals

    def get_rbac_accessible_workspaces(self, principal: Principal, tenant: Tenant) -> set[str]:
        """Get workspace IDs that a principal has access to via RBAC role bindings.

        This queries the v2 data model (RoleBinding) to determine which workspaces
        the principal can access, either directly or via group membership.
        """
        workspace_ids: set[str] = set()

        direct_bindings = RoleBindingPrincipal.objects.filter(
            principal=principal,
            binding__tenant=tenant,
            binding__resource_type="workspace",
        ).select_related("binding")

        for entry in direct_bindings:
            workspace_ids.add(entry.binding.resource_id)

        group_bindings = RoleBindingGroup.objects.filter(
            group__principals=principal,
            binding__tenant=tenant,
            binding__resource_type="workspace",
        ).select_related("binding")

        for entry in group_bindings:
            workspace_ids.add(entry.binding.resource_id)

        # Include workspaces where user is in the default group for the tenant
        # Check if principal is in the tenant's default group
        try:
            tenant_mapping = TenantMapping.objects.get(tenant=tenant)

            # Check if principal is in default group
            default_group_uuid = str(tenant_mapping.default_group_uuid)
            if Group.objects.filter(uuid=default_group_uuid, principals=principal).exists():
                # Add all workspaces with default role bindings
                default_bindings = RoleBinding.objects.filter(
                    tenant=tenant,
                    resource_type="workspace",
                    uuid__in=[
                        tenant_mapping.default_role_binding_uuid,
                        tenant_mapping.root_scope_default_role_binding_uuid,
                        tenant_mapping.tenant_scope_default_role_binding_uuid,
                    ],
                )
                for binding in default_bindings:
                    workspace_ids.add(binding.resource_id)

            # Check if principal is in admin default group
            admin_group_uuid = str(tenant_mapping.default_admin_group_uuid)
            if Group.objects.filter(uuid=admin_group_uuid, principals=principal).exists():
                admin_bindings = RoleBinding.objects.filter(
                    tenant=tenant,
                    resource_type="workspace",
                    uuid__in=[
                        tenant_mapping.default_admin_role_binding_uuid,
                        tenant_mapping.root_scope_default_admin_role_binding_uuid,
                        tenant_mapping.tenant_scope_default_admin_role_binding_uuid,
                    ],
                )
                for binding in admin_bindings:
                    workspace_ids.add(binding.resource_id)

        except TenantMapping.DoesNotExist:
            logger.warning("TenantMapping not found for tenant %s", tenant.org_id)

        return workspace_ids

    def get_pdp_accessible_workspaces(self, principal: Principal) -> set[str]:
        """Get workspace IDs that a principal has access to via Kessel PDP."""
        principal_resource_id = principal.principal_resource_id()
        if not principal_resource_id:
            logger.warning(
                "Principal %s has no user_id, cannot check PDP access",
                principal.username,
            )
            return set()

        return self.inventory_checker.lookup_accessible_workspaces(
            principal_id=principal_resource_id,
            relation=self.relation,
        )

    def check_principal_parity(self, principal: Principal, tenant: Tenant) -> ParityCheckResult:
        """Check parity for a single principal."""
        result = ParityCheckResult(
            org_id=tenant.org_id or "",
            principal_id=str(principal.uuid),
            user_id=principal.user_id or "",
        )

        start_time = time.perf_counter()

        try:
            # Get RBAC accessible workspaces
            result.rbac_workspaces = self.get_rbac_accessible_workspaces(principal, tenant)

            # Get PDP accessible workspaces
            result.pdp_workspaces = self.get_pdp_accessible_workspaces(principal)

            # Compare results
            result.only_in_rbac = result.rbac_workspaces - result.pdp_workspaces
            result.only_in_pdp = result.pdp_workspaces - result.rbac_workspaces
            result.match = not result.has_discrepancy()

        except Exception as e:
            result.error = str(e)
            result.match = False
            logger.exception(
                "Error checking parity for principal %s in tenant %s: %s",
                principal.username,
                tenant.org_id,
                e,
            )
            sentry_sdk.capture_exception(e)

        duration = time.perf_counter() - start_time
        parity_workspace_check_duration_seconds.observe(duration)

        return result

    def run_parity_checks(self) -> ParityJobResult:
        """Run parity checks for sampled bootstrapped tenants and principals."""
        job_result = ParityJobResult()
        job_start_time = time.perf_counter()

        logger.info(
            "Starting parity check job with tenant_sample_size=%d, principal_sample_size=%d",
            self.tenant_sample_size,
            self.principal_sample_size,
        )

        try:
            tenants = self.get_bootstrapped_tenants()
            job_result.tenants_checked = len(tenants)

            for tenant in tenants:
                with parity_check_duration_seconds.labels(check_type="tenant").time():
                    self._check_tenant(tenant, job_result)

                parity_tenants_checked_total.labels(status="checked").inc()

        except Exception as e:
            error_msg = f"Error running parity checks: {e}"
            job_result.errors.append(error_msg)
            logger.exception(error_msg)
            sentry_sdk.capture_exception(e)
            parity_job_runs_total.labels(status="error").inc()
            job_result.duration_seconds = time.perf_counter() - job_start_time
            return job_result

        job_result.duration_seconds = time.perf_counter() - job_start_time

        # Record job completion
        if job_result.discrepancies:
            parity_job_runs_total.labels(status="completed_with_discrepancies").inc()
        else:
            parity_job_runs_total.labels(status="success").inc()

        # Log job summary for Kibana
        logger.info(
            "Parity check job completed: tenants=%d, principals=%d, passed=%d, failed=%d, "
            "discrepancies=%d, errors=%d, duration=%.2fs",
            job_result.tenants_checked,
            job_result.principals_checked,
            job_result.checks_passed,
            job_result.checks_failed,
            len(job_result.discrepancies),
            len(job_result.errors),
            job_result.duration_seconds,
        )

        return job_result

    def _check_tenant(self, tenant: Tenant, job_result: ParityJobResult) -> None:
        """Check all sampled principals for a tenant."""
        principals = self.get_principals_for_tenant(tenant)
        logger.debug("Checking %d principals for tenant %s", len(principals), tenant.org_id)

        for principal in principals:
            result = self.check_principal_parity(principal, tenant)
            job_result.principals_checked += 1

            if result.error:
                job_result.errors.append(result.error)
                job_result.checks_failed += 1
                parity_principals_checked_total.labels(status="error").inc()
                parity_checks_total.labels(check_type="workspace", result="error").inc()

            elif result.has_discrepancy():
                job_result.discrepancies.append(result)
                job_result.checks_failed += 1
                parity_principals_checked_total.labels(status="discrepancy").inc()
                parity_checks_total.labels(check_type="workspace", result="mismatch").inc()

                # Record discrepancy metrics
                self._log_discrepancy(result)

            else:
                job_result.checks_passed += 1
                parity_principals_checked_total.labels(status="match").inc()
                parity_checks_total.labels(check_type="workspace", result="match").inc()

    def _log_discrepancy(self, result: ParityCheckResult) -> None:
        """Log a discrepancy and record metrics."""
        if result.only_in_rbac:
            parity_discrepancies_total.labels(discrepancy_type="rbac_only").inc(len(result.only_in_rbac))

            # Log for Kibana with structured fields
            logger.warning(
                "Parity discrepancy: workspaces in RBAC but not in PDP | "
                "org_id=%s user_id=%s principal_id=%s workspaces=%s",
                result.org_id,
                result.user_id,
                result.principal_id,
                list(result.only_in_rbac),
            )

        if result.only_in_pdp:
            parity_discrepancies_total.labels(discrepancy_type="pdp_only").inc(len(result.only_in_pdp))

            # Log for Kibana with structured fields
            logger.warning(
                "Parity discrepancy: workspaces in PDP but not in RBAC | "
                "org_id=%s user_id=%s principal_id=%s workspaces=%s",
                result.org_id,
                result.user_id,
                result.principal_id,
                list(result.only_in_pdp),
            )


def run_parity_checks(
    tenant_sample_size: int | None = None,
    principal_sample_size: int | None = None,
) -> ParityJobResult:
    """Run parity access checks between RBAC and Kessel PDP.

    This is the main entry point for the parity check background job.

    Args:
        tenant_sample_size: Maximum number of tenants to check (optional).
        principal_sample_size: Maximum number of principals per tenant to check (optional).

    Returns:
        ParityJobResult containing check statistics and any discrepancies found.
    """
    checker = ParityAccessChecker(
        tenant_sample_size=tenant_sample_size,
        principal_sample_size=principal_sample_size,
    )
    return checker.run_parity_checks()
