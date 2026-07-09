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

import datetime
import logging
import time
from typing import Optional

from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task
def principal_cleanup():
    """Celery task to clean up principals no longer existing."""
    from management.principal.cleaner import clean_tenants_principals

    clean_tenants_principals()


@shared_task
def principal_cleanup_via_umb():
    """Celery task to clean up principals no longer existing."""
    from management.principal.cleaner import process_principal_events_from_umb

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
    from management.health.healthcheck import redis_health

    redis_health()


@shared_task
def migrate_data_in_worker(kwargs):
    """Celery task to migrate data from V1 to V2 spiceDB schema."""
    from migration_tool.migrate import migrate_data

    migrate_data(**kwargs)


@shared_task
def migrate_binding_scope_in_worker(sources: Optional[list[str]] = None):
    """Celery task to migrate role binding scopes."""
    from migration_tool.migrate_binding_scope import migrate_all_role_bindings

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
    from internal.utils import replicate_missing_binding_tuples

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
    from internal.utils import clean_invalid_workspace_resource_definitions

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
    from internal.migrations.remove_orphan_relations import cleanup_tenant_orphan_bindings

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
    from internal.utils import remove_unassigned_system_binding_mappings

    return remove_unassigned_system_binding_mappings()


@shared_task
def expire_orphaned_cross_account_requests_in_worker():
    """Celery task to expire orphaned cross-account requests."""
    from internal.utils import expire_orphaned_cross_account_requests

    return expire_orphaned_cross_account_requests()


@shared_task
def remove_deleted_workspace_bindings_in_worker():
    """Celery task to remove role bindings that reference deleted workspaces."""
    from internal.migrations.remove_deleted_workspace_bindings import remove_deleted_workspace_bindings

    return remove_deleted_workspace_bindings()


@shared_task
def replicate_default_workspaces_in_worker(limit: Optional[int] = None):
    """Celery task to replicate default workspaces."""
    from internal.migrations.replicate_workspaces import replicate_default_workspaces

    return replicate_default_workspaces(limit=limit)


@shared_task
def replicate_updated_workspaces_in_worker(since: str, exclude_unchanged_default_workspaces: bool):
    """Celery task to replicate updated workspaces."""
    from internal.migrations.replicate_workspaces import replicate_updated_workspaces

    return replicate_updated_workspaces(
        since=datetime.datetime.fromisoformat(since),
        exclude_unchanged_default_workspaces=exclude_unchanged_default_workspaces,
    )


@shared_task
def recompute_tenant_role_bindings_in_worker(org_id: str):
    """Celery task to recompute role bindings for tenant."""
    from api.models import Tenant
    from internal.migrations.recompute_role_bindings import recompute_tenant_role_bindings

    return recompute_tenant_role_bindings(tenant=Tenant.objects.get(org_id=org_id))


@shared_task
def migrate_role_scope_if_changed_in_worker(role_uuid: str):
    """Celery task to migrate existing role bindings for a role if its scope has changed."""
    from internal.migrations.migrate_role_scope import migrate_role_scope_if_changed
    from management.role.model import Role

    return migrate_role_scope_if_changed(v1_role=Role.objects.filter(uuid=role_uuid).get())


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
    from management.parity_check import run_parity_checks

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
def run_kessel_parity_checks_in_worker(org_ids=None):
    """
    Celery task to run Kessel-RBAC parity checks for configured tenants.

    Args:
        org_ids: Optional list of org IDs to check. When provided, skips the
            PARITY_CHECK_ENABLED gate and uses the given list directly. When None,
            falls back to current behavior (reads from PARITY_CHECK_ORG_IDS env var
            and checks PARITY_CHECK_ENABLED gate).

    Returns:
        dict: Summary statistics with checks performed, passed, and failed counts.
    """
    if org_ids is None:
        # Scheduled cron path: respect PARITY_CHECK_ENABLED gate
        if not getattr(settings, "PARITY_CHECK_ENABLED", False):
            return {"message": "Parity checks disabled"}

        org_ids_str = settings.PARITY_CHECK_ORG_IDS
        org_ids = [org_id.strip() for org_id in org_ids_str.split(",") if org_id.strip()]
    else:
        # On-demand path: validate and deduplicate provided org_ids
        org_ids = [org_id.strip() for org_id in org_ids if org_id.strip()]

    # Deduplicate org_ids while preserving order to avoid redundant work and double-counting
    org_ids = list(dict.fromkeys(org_ids))

    if not org_ids:
        logger.info("[PCH] PARITY_CHECK_ORG_IDS not configured, skipping parity checks")
        return {"message": "No org_ids configured"}

    logger.info(f"[PCH] Starting Kessel parity checks for {len(org_ids)} org(s): {org_ids}")

    from api.models import Tenant
    from management.group.model import Group
    from management.inventory_checker.inventory_api_check import (
        BootstrappedTenantInventoryChecker,
        CustomRolePermissionChecker,
        GroupPrincipalInventoryChecker,
        SeededRoleHierarchyChecker,
        WorkspaceRelationInventoryChecker,
        generate_seeded_role_hierarchy_tuples,
    )
    from management.permission.scope_service import ImplicitResourceService
    from management.role.v2_model import CustomRoleV2, SeededRoleV2
    from management.tenant_mapping.model import TenantMapping
    from management.workspace.model import Workspace

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
            seeded_result_entry = {
                "role_uuid": str(seeded_role.uuid),
                "role_name": seeded_role.name,
                "v1_role_name": seeded_role.v1_source.name if seeded_role.v1_source else None,
                "tuple_count": len(hierarchy_tuples),
                "passed": role_passed,
            }
            if not role_passed:
                seeded_result_entry["expected_tuples"] = [t.stringify() for t in hierarchy_tuples[:5]]
                seeded_hierarchy_passed = False
            seeded_role_results.append(seeded_result_entry)
        except Exception as e:
            logger.exception("[PCH] Error checking seeded role hierarchy for role %s", seeded_role.name)
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
            f"[PCH] Seeded role hierarchy check: {len(seeded_role_results)} role(s) with hierarchy checked, "
            f"passed={seeded_hierarchy_passed}, took {seeded_elapsed:.3f}s"
        )

    if not seeded_hierarchy_passed:
        failed_seeded = [r for r in seeded_role_results if not r["passed"]]
        seeded_detail_lines = []
        seeded_missing_shown = 0
        for r in failed_seeded:
            if seeded_missing_shown >= 20:
                break
            for t_str in r.get("expected_tuples", [])[:5]:
                if seeded_missing_shown >= 20:
                    break
                seeded_detail_lines.append(f"[PCH]   - MISSING ({r['role_name']}): {t_str}")
                seeded_missing_shown += 1
        total_seeded_missing = sum(r.get("tuple_count", 0) for r in failed_seeded)
        if total_seeded_missing > seeded_missing_shown:
            seeded_detail_lines.append(f"[PCH]   ... and {total_seeded_missing - seeded_missing_shown} more")
        if seeded_detail_lines:
            logger.warning("\n".join(seeded_detail_lines))

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
            logger.warning(f"[PCH] Tenant not found for org_id: {org_id}")
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
            logger.info(f"[PCH] Running parity check for tenant {org_id}")
            stats["total_tenants"] += 1

            workspaces = (
                Workspace.objects.filter(tenant=tenant, parent_id__isnull=False)
                .exclude(type=Workspace.Types.ROOT)
                .values_list("id", "parent_id")
            )

            workspace_pairs = [(str(w_id), str(parent_id)) for (w_id, parent_id) in workspaces]
            pairs_count = len(workspace_pairs)

            workspace_pair_results = []
            if workspace_pairs:
                logger.info(f"[PCH] Checking {pairs_count} workspace parent relations for tenant {org_id}")
                workspace_check_passed, workspace_pair_results = workspace_checker.check_workspace_descendants(
                    workspace_pairs
                )
            else:
                logger.warning(f"[PCH] No workspace pairs to check for tenant {org_id} — missing default workspace?")
                workspace_check_passed = False

            stats["total_workspace_pairs_checked"] += pairs_count

            custom_roles = CustomRoleV2.objects.filter(tenant=tenant).prefetch_related("permissions")
            custom_role_check_passed = True
            role_results = []

            for role in custom_roles:
                permission_tuples = [CustomRoleV2._permission_tuple(role, perm) for perm in role.permissions.all()]
                role_passed = role_permission_checker.check_custom_role_permissions(permission_tuples, str(role.uuid))
                role_result_entry = {
                    "role_uuid": str(role.uuid),
                    "role_name": role.name,
                    "permission_count": len(permission_tuples),
                    "passed": role_passed,
                }
                if not role_passed:
                    role_result_entry["expected_tuples"] = [t.stringify() for t in permission_tuples[:5]]
                    custom_role_check_passed = False
                role_results.append(role_result_entry)

            stats["total_custom_roles_checked"] += len(role_results)
            if role_results:
                logger.info(f"[PCH] Checked {len(role_results)} custom role(s) for tenant {org_id}")

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
                    logger.warning(
                        f"[PCH] Missing root/default workspace for tenant {org_id}, skipping bootstrap check"
                    )
                    bootstrap_check_passed = False
            else:
                logger.warning(f"[PCH] No tenant mapping for org_id: {org_id}, skipping bootstrap check")
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

                group_result_entry = {
                    "group_uuid": str(group.uuid),
                    "group_name": group.name,
                    "principal_count": principal_count,
                    "passed": all_exist,
                }
                if not all_exist:
                    missing_ids = {pr["id"] for pr in result["principal_relations"] if not pr["relation_exists"]}
                    missing_rels = [r for r in relationships if r.subject.subject.id in missing_ids]
                    group_result_entry["missing_tuples"] = [r.stringify() for r in missing_rels[:5]]
                    group_result_entry["missing_count"] = len(missing_rels)
                    group_principal_check_passed = False
                group_results.append(group_result_entry)

            stats["total_groups_checked"] += len(group_results)
            stats["total_group_principal_relations_checked"] += tenant_group_principal_relations
            if group_results:
                logger.info(
                    f"[PCH] Checked {len(group_results)} group(s) with "
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
                logger.info(f"[PCH] Parity check PASSED for tenant {org_id}")
            else:
                stats["failed_tenants"] += 1
                logger.warning(f"[PCH] Parity check FAILED for tenant {org_id}")

            sub_check_log = logger.info if tenant_passed else logger.warning

            detail_lines = []
            detail_lines.append(f"[PCH]   Sub-check results for tenant {org_id}:")
            detail_lines.append(
                f"[PCH]     Workspace hierarchy: {'PASSED' if workspace_check_passed else 'FAILED'}"
                f" ({pairs_count} pairs)"
            )
            if not workspace_check_passed and workspace_pair_results:
                missing = [r for r in workspace_pair_results if not r["exists"]]
                ws_missing_shown = 0
                for r in missing:
                    if ws_missing_shown >= 20:
                        break
                    detail_lines.append(
                        f"[PCH]       - MISSING: rbac/workspace:{r['parent_id']}"
                        f"#parent@rbac/workspace:{r['workspace_id']}"
                    )
                    ws_missing_shown += 1
                if len(missing) > ws_missing_shown:
                    detail_lines.append(f"[PCH]       ... and {len(missing) - ws_missing_shown} more")

            detail_lines.append(
                f"[PCH]     Custom roles:        {'PASSED' if custom_role_check_passed else 'FAILED'}"
                f" ({len(role_results)} roles)"
            )
            if not custom_role_check_passed:
                failed_roles = [r for r in role_results if not r["passed"]]
                cr_missing_shown = 0
                for r in failed_roles:
                    if cr_missing_shown >= 20:
                        break
                    for t_str in r.get("expected_tuples", [])[:5]:
                        if cr_missing_shown >= 20:
                            break
                        detail_lines.append(f"[PCH]       - MISSING ({r['role_name']}): {t_str}")
                        cr_missing_shown += 1
                total_cr_missing = sum(r.get("permission_count", 0) for r in failed_roles)
                if total_cr_missing > cr_missing_shown:
                    detail_lines.append(f"[PCH]       ... and {total_cr_missing - cr_missing_shown} more")

            detail_lines.append(
                f"[PCH]     Bootstrap:           {'PASSED' if bootstrap_check_passed else 'FAILED'}"
                f" ({len(bootstrap_details)} checks)"
            )
            if not bootstrap_check_passed and bootstrap_details:
                missing = [d for d in bootstrap_details if not d["exists"]]
                for d in missing[:20]:
                    detail_lines.append(f"[PCH]       - MISSING: {d['name']} ({d.get('check', '')})")
                if len(missing) > 20:
                    detail_lines.append(f"[PCH]       ... and {len(missing) - 20} more")

            detail_lines.append(
                f"[PCH]     Group-principal:     {'PASSED' if group_principal_check_passed else 'FAILED'}"
                f" ({len(group_results)} groups, {tenant_group_principal_relations} relations)"
            )
            if not group_principal_check_passed:
                failed_groups = [g for g in group_results if not g["passed"]]
                gp_missing_shown = 0
                for g in failed_groups:
                    if gp_missing_shown >= 20:
                        break
                    for t_str in g.get("missing_tuples", [])[:5]:
                        if gp_missing_shown >= 20:
                            break
                        detail_lines.append(f"[PCH]       - MISSING ({g['group_name']}): {t_str}")
                        gp_missing_shown += 1
                total_gp_missing = sum(g.get("missing_count", 0) for g in failed_groups)
                if total_gp_missing > gp_missing_shown:
                    detail_lines.append(f"[PCH]       ... and {total_gp_missing - gp_missing_shown} more")

            sub_check_log("\n".join(detail_lines))

            tenant_elapsed = time.monotonic() - tenant_start
            tenant_durations.append(tenant_elapsed)
            logger.info(f"[PCH] Tenant {org_id} parity check took {tenant_elapsed:.3f}s")

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
            logger.exception(f"[PCH] Error checking parity for tenant {org_id}: {e}")
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
            f"[PCH] Timing: avg={timing_stats['avg_seconds']}s "
            f"p95={timing_stats['p95_seconds']}s "
            f"p99={timing_stats['p99_seconds']}s"
        )

    logger.info(
        f"[PCH] Parity check complete. Checked: {stats['total_tenants']}, "
        f"Passed: {stats['passed_tenants']}, "
        f"Failed: {stats['failed_tenants']}, "
        f"Not Found: {stats['tenants_not_found']}, "
        f"Total workspace pairs: {stats['total_workspace_pairs_checked']}, "
        f"Total custom roles: {stats['total_custom_roles_checked']}, "
        f"Total seeded roles: {stats['total_seeded_roles_checked']}, "
        f"Total bootstrap checks: {stats['total_bootstrap_checks']}, "
        f"Total groups: {stats['total_groups_checked']}, "
        f"Total group-principal relations: {stats['total_group_principal_relations_checked']}, "
        f"Seeded role hierarchy: {'PASSED' if seeded_hierarchy_passed else 'FAILED'}"
        f" ({stats['total_seeded_roles_checked']} roles)"
    )

    stats["timing"] = timing_stats
    return stats


@shared_task
def recover_workspace_events_in_worker(
    restore_timestamp_iso: str,
    buffer_minutes: int = 5,
    dry_run: bool = False,
) -> dict:
    """Celery task to generate corrective workspace events after a DB restore.

    Reads workspace events from Kafka for the data loss window, compares against
    current RBAC DB state, and writes corrective events to the outbox table.

    Args:
        restore_timestamp_iso: ISO 8601 timestamp of the DB restore point (T-30).
        buffer_minutes: Minutes to extend the window before restore_timestamp.

    Returns:
        dict: Summary statistics of corrective events generated.
    """
    if not getattr(settings, "DR_WORKSPACE_RECONCILE_ENABLED", False):
        return {"message": "DR recovery disabled (DR_WORKSPACE_RECONCILE_ENABLED=False)"}

    from django.core.cache import cache

    lock_key = "dr_workspace_recovery_lock"
    if not cache.add(lock_key, "running", timeout=3600):
        return {"message": "A workspace DR recovery task is already in progress"}

    start = time.monotonic()

    try:
        restore_dt = datetime.datetime.fromisoformat(restore_timestamp_iso)
        if restore_dt.tzinfo is None:
            restore_dt = restore_dt.replace(tzinfo=datetime.timezone.utc)

        buffer_delta = datetime.timedelta(minutes=buffer_minutes)
        start_dt = restore_dt - buffer_delta
        end_dt = datetime.datetime.now(datetime.timezone.utc)

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        topic = getattr(settings, "DR_WORKSPACE_TOPIC", "outbox.event.workspace")

        logger.info(
            "Starting workspace DR recovery: topic=%s start=%s end=%s buffer_minutes=%d",
            topic,
            start_dt.isoformat(),
            end_dt.isoformat(),
            buffer_minutes,
        )

        from core.kafka_dr import read_events_by_timestamp
        from management.workspace.dr_recovery import generate_corrective_workspace_events

        kafka_events = read_events_by_timestamp(
            topic=topic,
            start_timestamp_ms=start_ms,
            end_timestamp_ms=end_ms,
        )

        logger.info("Read %d Kafka events from topic %s", len(kafka_events), topic)

        stats = generate_corrective_workspace_events(kafka_events, dry_run=dry_run)

        elapsed = time.monotonic() - start
        result = dict(stats)
        result["duration_seconds"] = round(elapsed, 3)
        result["restore_timestamp"] = restore_timestamp_iso
        result["buffer_minutes"] = buffer_minutes
        result["kafka_events_read"] = len(kafka_events)

        logger.info("Workspace DR recovery task completed in %.3fs: %s", elapsed, result)

        return result
    finally:
        cache.delete(lock_key)


@shared_task
def run_disaster_recovery_reconcile(
    restore_timestamp_ms: int, buffer_seconds: int = 300, dry_run: bool = False
) -> dict:
    """Celery task for disaster recovery reconciliation of Kessel Relations.

    Reads Kafka events from the data loss window, validates against current
    RBAC database state, and writes corrective events through the outbox table.

    Args:
        restore_timestamp_ms: Unix timestamp in milliseconds of the DB restore point.
        buffer_seconds: Extra time before restore_timestamp to include (default 300).
        dry_run: If True, analyze but don't write corrective events.

    Returns:
        dict: Summary with counts of corrective actions taken.
    """
    if not getattr(settings, "DR_RELATIONS_RECONCILE_ENABLED", False):
        return {"message": "Disaster recovery reconciliation is disabled"}

    if not getattr(settings, "KAFKA_ENABLED", False):
        return {"message": "Disaster recovery reconciliation requires Kafka (KAFKA_ENABLED=False)"}

    if not getattr(settings, "RBAC_KAFKA_CONSUMER_TOPIC", None):
        return {"message": "Disaster recovery reconciliation requires RBAC_KAFKA_CONSUMER_TOPIC to be configured"}

    try:
        from management.disaster_recovery.service import reconcile

        return reconcile(
            restore_timestamp_ms=restore_timestamp_ms,
            buffer_seconds=buffer_seconds,
            dry_run=dry_run,
        )
    except Exception as e:
        logger.exception("Disaster recovery reconciliation failed")
        return {
            "status": "failed",
            "error": str(e),
        }
