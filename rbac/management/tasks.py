#
# Copyright 2019 Red Hat, Inc.
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
"""Celery tasks."""

from __future__ import absolute_import, unicode_literals

import logging
import time
from typing import Optional

from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from internal.migrations.recompute_role_bindings import recompute_tenant_role_bindings
from internal.migrations.remove_deleted_workspace_bindings import remove_deleted_workspace_bindings
from internal.migrations.remove_orphan_relations import cleanup_tenant_orphan_bindings
from internal.migrations.replicate_default_workspaces import replicate_default_workspaces
from internal.utils import (
    clean_invalid_workspace_resource_definitions,
    expire_orphaned_cross_account_requests,
    remove_unassigned_system_binding_mappings,
    replicate_missing_binding_tuples,
)
from management.group.model import Group
from management.health.healthcheck import redis_health
from management.inventory_checker.inventory_api_check import (
    BootstrappedTenantInventoryChecker,
    CustomRolePermissionChecker,
    GroupPrincipalInventoryChecker,
    SeededRoleHierarchyChecker,
    WorkspaceRelationInventoryChecker,
    generate_seeded_role_hierarchy_tuples,
)
from management.parity_check import run_parity_checks
from management.permission.scope_service import ImplicitResourceService
from management.principal.cleaner import (
    clean_tenants_principals,
    process_principal_events_from_umb,
)
from management.role.v2_model import CustomRoleV2, SeededRoleV2
from management.tenant_mapping.model import TenantMapping
from management.workspace.model import Workspace
from migration_tool.migrate import migrate_data
from migration_tool.migrate_binding_scope import migrate_all_role_bindings

from api.models import Tenant

logger = logging.getLogger(__name__)


@shared_task
def principal_cleanup():
    """Celery task to clean up principals no longer existing."""
    clean_tenants_principals()


@shared_task
def principal_cleanup_via_umb():
    """Celery task to clean up principals no longer existing."""
    process_principal_events_from_umb()


@shared_task
def run_migrations_in_worker():
    """Celery task to run migrations."""
    call_command("migrate")


@shared_task
def run_seeds_in_worker(kwargs):
    """Celery task to run seeds."""
    call_command("seeds", **kwargs)


@shared_task
def run_sync_schemas_in_worker(kwargs):
    """Celery task to sync schemas."""
    call_command("sync_schemas", **kwargs)


@shared_task
def run_ocm_performance_in_worker():
    """Celery task to run ocm performance tests."""
    call_command("ocm_performance")


@shared_task
def run_redis_cache_health():
    """Celery task to check health of redis cache."""
    redis_health()


@shared_task
def migrate_data_in_worker(kwargs):
    """Celery task to migrate data from V1 to V2 spiceDB schema."""
    migrate_data(**kwargs)


@shared_task
def migrate_binding_scope_in_worker(sources: Optional[list[str]] = None):
    """Celery task to migrate role binding scopes."""
    return migrate_all_role_bindings(sources=set(sources) if sources is not None else None)


@shared_task
def fix_missing_binding_base_tuples_in_worker(binding_uuids=None):
    """
    Celery task to fix missing base tuples for bindings.

    Args:
        binding_uuids (list[str], optional): List of binding UUIDs to fix. If None, fixes all bindings.

    Returns:
        dict: Results with bindings_checked, bindings_fixed, and tuples_added count.
    """
    return replicate_missing_binding_tuples(binding_uuids=binding_uuids)


@shared_task
def clean_invalid_workspace_resource_definitions_in_worker(dry_run=False):
    """
    Celery task to clean invalid workspace resource definitions.

    Args:
        dry_run (bool): If True, only report what would be changed without making changes.

    Returns:
        dict: Results with roles_checked, resource_definitions_fixed, bindings_deleted, and changes list.
    """
    return clean_invalid_workspace_resource_definitions(dry_run=dry_run)


@shared_task
def cleanup_tenant_orphan_bindings_in_worker(org_id, dry_run=False):
    """
    Celery task to clean up orphaned role binding relationships for a tenant.

    Args:
        org_id (str): Organization ID for the tenant to clean up
        dry_run (bool): If True, only report what would be deleted without making changes

    Returns:
        dict: Results with cleanup counts and migration results
    """
    return cleanup_tenant_orphan_bindings(org_id=org_id, dry_run=dry_run)


@shared_task
def bulk_cleanup_orphan_bindings_in_worker(tenant_limit: int):
    """
    Celery task to clean up orphaned relationships.

    Args:
        tenant_limit (int): maximum number of tenants to process
    """
    return call_command("fix_orphan_relations", tenant_limit=tenant_limit)


@shared_task
def remove_unassigned_system_binding_mappings_in_worker():
    """Celery to remove unassigned system BindingMappings."""
    return remove_unassigned_system_binding_mappings()


@shared_task
def expire_orphaned_cross_account_requests_in_worker():
    """Celery task to expire orphaned cross-account requests."""
    return expire_orphaned_cross_account_requests()


@shared_task
def remove_deleted_workspace_bindings_in_worker():
    """Celery task to remove role bindings that reference deleted workspaces."""
    return remove_deleted_workspace_bindings()


@shared_task
def replicate_default_workspaces_in_worker(limit: Optional[int] = None):
    """Celery task to replicate default workspaces."""
    return replicate_default_workspaces(limit=limit)


@shared_task
def recompute_tenant_role_bindings_in_worker(org_id: str):
    """Celery task to recompute role bindings for tenant."""
    return recompute_tenant_role_bindings(tenant=Tenant.objects.get(org_id=org_id))


@shared_task
def run_parity_access_checks_in_worker(
    tenant_sample_size: Optional[int] = None,
    principal_sample_size: Optional[int] = None,
) -> dict:
    """Celery task to run parity access checks between RBAC and Kessel PDP.

    This task compares workspace access permissions computed by RBAC with those
    returned by the Kessel Inventory API (PDP). Any discrepancies are logged
    as metrics and warnings for monitoring and alerting.

    Args:
        tenant_sample_size: Maximum number of v2-enabled tenants to check per run.
        principal_sample_size: Maximum number of principals per tenant to check.

    Returns:
        dict: Summary of check results including counts and any discrepancies found.
    """
    result = run_parity_checks(
        tenant_sample_size=tenant_sample_size,
        principal_sample_size=principal_sample_size,
    )

    return {
        "tenants_checked": result.tenants_checked,
        "principals_checked": result.principals_checked,
        "checks_passed": result.checks_passed,
        "checks_failed": result.checks_failed,
        "discrepancies_count": len(result.discrepancies),
        "errors_count": len(result.errors),
        "duration_seconds": result.duration_seconds,
    }


@shared_task
def run_kessel_parity_checks_in_worker():
    """
    Celery task to run Kessel-RBAC parity checks for configured tenants.

    Returns:
        dict: Summary statistics with checks performed, passed, and failed counts.
    """
    if not getattr(settings, "PARITY_CHECK_ENABLED", False):
        return {"message": "Parity checks disabled"}

    org_ids_str = settings.PARITY_CHECK_ORG_IDS
    org_ids = [org_id.strip() for org_id in org_ids_str.split(",") if org_id.strip()]
    # Deduplicate org_ids while preserving order to avoid redundant work and double-counting
    org_ids = list(dict.fromkeys(org_ids))

    if not org_ids:
        logger.info("PARITY_CHECK_ORG_IDS not configured, skipping parity checks")
        return {"message": "No org_ids configured"}

    logger.info(f"Starting Kessel parity checks for {len(org_ids)} org(s): {org_ids}")

    stats = {
        "total_tenants": 0,
        "total_workspace_pairs_checked": 0,
        "total_custom_roles_checked": 0,
        "total_seeded_roles_checked": 0,
        "total_bootstrap_checks": 0,
        "total_groups_checked": 0,
        "total_group_principal_relations_checked": 0,
        "passed_tenants": 0,
        "failed_tenants": 0,
        "tenants_not_found": 0,
        "seeded_role_hierarchy": {},
        "tenants_checked": [],
    }
    tenant_durations = []

    workspace_checker = WorkspaceRelationInventoryChecker()
    role_permission_checker = CustomRolePermissionChecker()
    hierarchy_checker = SeededRoleHierarchyChecker()
    bootstrap_checker = BootstrappedTenantInventoryChecker()
    group_principal_checker = GroupPrincipalInventoryChecker()

    # Seeded role hierarchy check (global, not per-tenant)
    seeded_start = time.monotonic()
    seeded_role_results = []
    seeded_hierarchy_passed = True

    seeded_roles = SeededRoleV2.objects.select_related("v1_source").prefetch_related("v1_source__access")
    implicit_resource_service = ImplicitResourceService.from_settings() if seeded_roles.exists() else None

    for seeded_role in seeded_roles:
        try:
            hierarchy_tuples = generate_seeded_role_hierarchy_tuples(seeded_role, implicit_resource_service)
            if not hierarchy_tuples:
                continue
            role_passed = hierarchy_checker.check_seeded_role_hierarchy(hierarchy_tuples, str(seeded_role.uuid))
            seeded_role_results.append(
                {
                    "role_uuid": str(seeded_role.uuid),
                    "role_name": seeded_role.name,
                    "v1_role_name": seeded_role.v1_source.name if seeded_role.v1_source else None,
                    "tuple_count": len(hierarchy_tuples),
                    "passed": role_passed,
                }
            )
            if not role_passed:
                seeded_hierarchy_passed = False
        except Exception as e:
            logger.exception("Error checking seeded role hierarchy for role %s", seeded_role.name)
            seeded_hierarchy_passed = False
            seeded_role_results.append(
                {
                    "role_uuid": str(seeded_role.uuid),
                    "role_name": seeded_role.name,
                    "v1_role_name": seeded_role.v1_source.name if seeded_role.v1_source else None,
                    "passed": False,
                    "error": str(e),
                }
            )

    seeded_elapsed = time.monotonic() - seeded_start
    stats["total_seeded_roles_checked"] = len(seeded_role_results)
    stats["seeded_role_hierarchy"] = {
        "total_seeded_roles": seeded_roles.count(),
        "roles_with_hierarchy_checked": len(seeded_role_results),
        "passed": seeded_hierarchy_passed,
        "role_results": seeded_role_results,
        "duration_seconds": round(seeded_elapsed, 3),
    }

    if seeded_role_results:
        logger.info(
            f"Seeded role hierarchy check: {len(seeded_role_results)} role(s) with hierarchy checked, "
            f"passed={seeded_hierarchy_passed}, took {seeded_elapsed:.3f}s"
        )

    # Bulk fetch all tenants to avoid N+1 queries
    tenants = {t.org_id: t for t in Tenant.objects.filter(org_id__in=org_ids)}
    # Bulk fetch tenant mappings
    tenant_mappings = {tm.tenant_id: tm for tm in TenantMapping.objects.filter(tenant__in=tenants.values())}

    # Bulk fetch workspaces for bootstrap checks to avoid N+1 queries
    relevant_workspace_types = (Workspace.Types.ROOT, Workspace.Types.DEFAULT, Workspace.Types.UNGROUPED_HOSTS)
    workspace_index: dict[tuple[int, str], Workspace] = {}
    for ws in Workspace.objects.filter(tenant__in=tenants.values(), type__in=relevant_workspace_types):
        workspace_index[(ws.tenant_id, ws.type)] = ws

    for org_id in org_ids:
        tenant = tenants.get(org_id)
        if not tenant:
            logger.warning(f"Tenant not found for org_id: {org_id}")
            stats["tenants_not_found"] += 1
            continue

        pairs_count = 0
        workspace_check_passed = False
        role_results = []
        custom_role_check_passed = True
        bootstrap_check_passed = False
        bootstrap_details: list[dict] = []
        group_results = []
        group_principal_check_passed = True

        try:
            tenant_start = time.monotonic()
            logger.info(f"Running parity check for tenant {org_id}")
            stats["total_tenants"] += 1

            workspaces = (
                Workspace.objects.filter(tenant=tenant, parent_id__isnull=False)
                .exclude(type=Workspace.Types.ROOT)
                .values_list("id", "parent_id")
            )

            workspace_pairs = [(str(w_id), str(parent_id)) for (w_id, parent_id) in workspaces]
            pairs_count = len(workspace_pairs)

            if workspace_pairs:
                logger.info(f"Checking {pairs_count} workspace parent relations for tenant {org_id}")
                workspace_check_passed = workspace_checker.check_workspace_descendants(workspace_pairs)
            else:
                logger.warning(f"No workspace pairs to check for tenant {org_id} — missing default workspace?")
                workspace_check_passed = False

            stats["total_workspace_pairs_checked"] += pairs_count

            custom_roles = CustomRoleV2.objects.filter(tenant=tenant).prefetch_related("permissions")
            custom_role_check_passed = True
            role_results = []

            for role in custom_roles:
                permission_tuples = [CustomRoleV2._permission_tuple(role, perm) for perm in role.permissions.all()]
                role_passed = role_permission_checker.check_custom_role_permissions(permission_tuples, str(role.uuid))
                role_results.append(
                    {
                        "role_uuid": str(role.uuid),
                        "role_name": role.name,
                        "permission_count": len(permission_tuples),
                        "passed": role_passed,
                    }
                )
                if not role_passed:
                    custom_role_check_passed = False

            stats["total_custom_roles_checked"] += len(role_results)
            if role_results:
                logger.info(f"Checked {len(role_results)} custom role(s) for tenant {org_id}")

            # Bootstrap completeness check
            mapping = tenant_mappings.get(tenant.id)
            if mapping:
                root_ws = workspace_index.get((tenant.id, Workspace.Types.ROOT))
                default_ws = workspace_index.get((tenant.id, Workspace.Types.DEFAULT))
                ungrouped_ws = workspace_index.get((tenant.id, Workspace.Types.UNGROUPED_HOSTS))

                if root_ws and default_ws:
                    bootstrap_check_passed, bootstrap_details = bootstrap_checker.check_bootstrapped_tenant(
                        org_id=org_id,
                        tenant_mapping=mapping,
                        root_workspace_id=str(root_ws.id),
                        default_workspace_id=str(default_ws.id),
                        ungrouped_workspace_id=str(ungrouped_ws.id) if ungrouped_ws else None,
                    )
                    stats["total_bootstrap_checks"] += len(bootstrap_details)
                else:
                    logger.warning(f"Missing root/default workspace for tenant {org_id}, skipping bootstrap check")
                    bootstrap_check_passed = False
            else:
                logger.warning(f"No tenant mapping for org_id: {org_id}, skipping bootstrap check")
                bootstrap_check_passed = False

            groups = Group.objects.filter(tenant=tenant).prefetch_related("principals")
            tenant_group_principal_relations = 0

            for group in groups:
                relationships = [group.relationship_to_principal(p) for p in group.principals.all()]
                relationships = [r for r in relationships if r is not None]
                principal_count = len(relationships)
                tenant_group_principal_relations += principal_count

                if relationships:
                    result = group_principal_checker.check_relationships(relationships)
                    all_exist = all(pr["relation_exists"] for pr in result["principal_relations"])
                else:
                    all_exist = True

                group_results.append(
                    {
                        "group_uuid": str(group.uuid),
                        "group_name": group.name,
                        "principal_count": principal_count,
                        "passed": all_exist,
                    }
                )
                if not all_exist:
                    group_principal_check_passed = False

            stats["total_groups_checked"] += len(group_results)
            stats["total_group_principal_relations_checked"] += tenant_group_principal_relations
            if group_results:
                logger.info(
                    f"Checked {len(group_results)} group(s) with "
                    f"{tenant_group_principal_relations} principal relation(s) for tenant {org_id}"
                )

            tenant_passed = (
                workspace_check_passed
                and custom_role_check_passed
                and bootstrap_check_passed
                and group_principal_check_passed
            )

            if tenant_passed:
                stats["passed_tenants"] += 1
                logger.info(f"Parity check PASSED for tenant {org_id}")
            else:
                stats["failed_tenants"] += 1
                logger.warning(f"Parity check FAILED for tenant {org_id}")

            tenant_elapsed = time.monotonic() - tenant_start
            tenant_durations.append(tenant_elapsed)
            logger.info(f"Tenant {org_id} parity check took {tenant_elapsed:.3f}s")

            stats["tenants_checked"].append(
                {
                    "org_id": org_id,
                    "workspace_pairs_checked": pairs_count,
                    "workspace_check_passed": workspace_check_passed,
                    "custom_roles_checked": len(role_results),
                    "custom_role_check_passed": custom_role_check_passed,
                    "bootstrap_checks": len(bootstrap_details),
                    "bootstrap_check_passed": bootstrap_check_passed,
                    "bootstrap_details": bootstrap_details,
                    "role_results": role_results,
                    "groups_checked": len(group_results),
                    "group_principal_check_passed": group_principal_check_passed,
                    "group_results": group_results,
                    "passed": tenant_passed,
                    "duration_seconds": round(tenant_elapsed, 3),
                }
            )

        except Exception as e:
            tenant_elapsed = time.monotonic() - tenant_start
            tenant_durations.append(tenant_elapsed)
            logger.exception(f"Error checking parity for tenant {org_id}: {e}")
            stats["failed_tenants"] += 1
            stats["tenants_checked"].append(
                {
                    "org_id": org_id,
                    "workspace_pairs_checked": pairs_count,
                    "workspace_check_passed": workspace_check_passed,
                    "custom_roles_checked": len(role_results),
                    "custom_role_check_passed": custom_role_check_passed,
                    "bootstrap_checks": len(bootstrap_details),
                    "bootstrap_check_passed": bootstrap_check_passed,
                    "bootstrap_details": bootstrap_details,
                    "role_results": role_results,
                    "groups_checked": len(group_results),
                    "group_principal_check_passed": group_principal_check_passed,
                    "group_results": group_results,
                    "passed": False,
                    "error": str(e),
                    "duration_seconds": round(tenant_elapsed, 3),
                }
            )

    timing_stats = {}
    if tenant_durations:
        sorted_durations = sorted(tenant_durations)
        n = len(sorted_durations)
        timing_stats = {
            "avg_seconds": round(sum(sorted_durations) / n, 3),
            "p95_seconds": round(sorted_durations[min(int(n * 0.95), n - 1)], 3),
            "p99_seconds": round(sorted_durations[min(int(n * 0.99), n - 1)], 3),
        }
        logger.info(
            f"Timing: avg={timing_stats['avg_seconds']}s "
            f"p95={timing_stats['p95_seconds']}s "
            f"p99={timing_stats['p99_seconds']}s"
        )

    logger.info(
        f"Parity check complete. Checked: {stats['total_tenants']}, "
        f"Passed: {stats['passed_tenants']}, "
        f"Failed: {stats['failed_tenants']}, "
        f"Not Found: {stats['tenants_not_found']}, "
        f"Total workspace pairs: {stats['total_workspace_pairs_checked']}, "
        f"Total custom roles: {stats['total_custom_roles_checked']}, "
        f"Total seeded roles: {stats['total_seeded_roles_checked']}, "
        f"Total bootstrap checks: {stats['total_bootstrap_checks']}, "
        f"Total groups: {stats['total_groups_checked']}, "
        f"Total group-principal relations: {stats['total_group_principal_relations_checked']}"
    )

    stats["timing"] = timing_stats
    return stats
