"""Helper utilities for api module."""

import logging

from django.db import connection, transaction
from django.shortcuts import get_object_or_404
from management.models import BindingMapping
from management.tenant_mapping.model import TenantMapping
from management.utils import account_id_for_tenant
from management.workspace.model import Workspace

from api.models import Tenant

RESOURCE_MODEL_MAPPING = {
    "workspace": Workspace,
    "mapping": TenantMapping,
    "binding": BindingMapping,
}
logger = logging.getLogger(__name__)


def populate_tenant_account_id():
    """Populate tenant's account id's."""
    tenants = Tenant.objects.filter(account_id__isnull=True).exclude(tenant_name="public")
    for tenant in tenants:
        with transaction.atomic():
            tenant.account_id = account_id_for_tenant(tenant)
            tenant.save()


def populate_tenant_org_id(tenants, account_org_mapping):
    """Populate tenant's org_id from account_id mapping.

    Deletes tenants in the following cases:
    - No org_id found in BOP mapping for the tenant's account_id
    - The mapped org_id already exists (assigned to another tenant)
    They are not reachable in RBAC because we search by org_id, so no
    relationships to remove.

    Args:
        tenants (QuerySet or list): List or QuerySet of Tenant objects to update
        account_org_mapping (dict): Mapping of account_id to org_id {account_id: org_id}

    Returns:
        dict: Statistics about the operation (updated, deleted_no_mapping, deleted_duplicate, errors)
    """
    provided_org_ids = [t.org_id for t in tenants if t.org_id is not None]

    if provided_org_ids:
        raise ValueError(f"Expected all tenants to have no org_id, got: {provided_org_ids}")

    stats = {"updated": 0, "deleted_no_mapping": 0, "deleted_duplicate": 0, "errors": 0, "error_details": []}

    # Create a mapping of account_id to tenant object for quick lookup
    tenant_by_account_id = {tenant.account_id: tenant for tenant in tenants if tenant.account_id}

    # Get set of existing org_ids that match the ones we want to assign
    # (more efficient than checking all org_ids)
    org_ids_to_assign = list(account_org_mapping.values())
    existing_org_ids = set(Tenant.objects.filter(org_id__in=org_ids_to_assign).values_list("org_id", flat=True))

    # Process tenants with account_ids in the provided list
    with transaction.atomic():
        for tenant in tenant_by_account_id.values():
            account_id = tenant.account_id
            try:
                # Check if we have a mapping for this account_id
                if account_id not in account_org_mapping:
                    # No mapping found - delete the tenant
                    logger.warning(f"No org_id mapping found for account_id={account_id}, deleting tenant {tenant.id}")
                    delete_tenant_with_resources(tenant)
                    stats["deleted_no_mapping"] += 1
                    continue

                org_id = account_org_mapping[account_id]

                # Check if this org_id already exists
                if org_id in existing_org_ids:
                    logger.warning(
                        f"org_id={org_id} already exists for another tenant, "
                        f"deleting tenant {tenant.id} with account_id={account_id}"
                    )
                    delete_tenant_with_resources(tenant)
                    stats["deleted_duplicate"] += 1
                else:
                    # Safe to update
                    logger.info(f"Updating tenant {tenant.id} with account_id={account_id} to org_id={org_id}")
                    tenant.org_id = org_id
                    tenant.save()
                    stats["updated"] += 1
                    # Add to existing set so next iteration knows about it
                    existing_org_ids.add(org_id)

            except Exception as e:
                logger.error(f"Error processing tenant with account_id={account_id}: {str(e)}")
                stats["errors"] += 1
                stats["error_details"].append({"account_id": account_id, "tenant_id": tenant.id, "error": str(e)})

    logger.info(f"Tenant org_id population completed. Stats: {stats}")
    return stats


def get_resources(resource, org_id):
    """Get queryset by org_id."""
    queryset = RESOURCE_MODEL_MAPPING[resource].objects.all()
    if org_id:
        if resource == "binding":
            raise ValueError("Binding cannot be filtered by org_id.")
        tenant = get_object_or_404(Tenant, org_id=org_id)
        queryset = queryset.filter(tenant=tenant)
    return queryset


def migration_resource_deletion(resource, org_id):
    """Delete migration related resources."""
    resource_objs = get_resources(resource, org_id)

    if resource == "workspace":
        # Have to delete the ones without children first or deletion will fail
        logger.info("Deleting workspaces without children.")
        resource_objs = resource_objs.order_by("id")
        chunk_delete(resource_objs.filter(children=None))
        logger.info("All workspaces without children removed.")
    chunk_delete(resource_objs)
    logger.info(f"Resources of type {resource} deleted.")


def delete_tenant_with_resources(tenant):
    """Delete tenant after removing its workspaces and other resources.

    Uses migration_resource_deletion to properly handle workspace deletion
    (children before parents due to PROTECT constraints).

    Args:
        tenant: Tenant object to delete
    """
    # Delete workspaces first (handles children-before-parents automatically)
    workspaces = Workspace.objects.filter(tenant=tenant).order_by("id")
    # Delete workspaces without children first (due to PROTECT on parent)
    chunk_delete(workspaces.filter(children__isnull=True))
    # Now delete remaining workspaces
    chunk_delete(workspaces)
    logger.info(f"Deleted all workspaces for tenant {tenant.id}")

    # Now tenant can be safely deleted (all workspaces are gone)
    tenant.delete()


def chunk_delete(queryset):
    """Delete queryset in chunks."""
    count = 0
    while True:
        delimiter = list(queryset.values_list("id", flat=True)[:10000])
        if not delimiter:
            break
        queryset.filter(id__in=delimiter).delete()
        count += len(delimiter)
        logger.info(f"Deleted {count} records.")


def reset_imported_tenants(query: str, limit: int, excluded: list[str]):
    """Reset (delete) imported tenants based on the given query, limit, and excluded list.

    Args:
        query (str): The SQL query to select tenants.
        limit (int): The limit on the number of tenants to delete.
        excluded (list[str]): The list of tenant IDs to exclude from deletion.
    """
    if limit == 0:
        logger.info("Limit is 0, nothing to do.")
        return

    with connection.cursor() as cursor:
        if limit > 0:
            subquery = f"SELECT id {query}"
            cursor.execute(
                "WITH delete_batch AS (" + subquery + ") "
                "DELETE FROM api_tenant as t USING delete_batch as del where t.id = del.id",
                (tuple(excluded),),
            )
        else:
            cursor.execute(
                "DELETE " + query,
                (tuple(excluded),),
            )
        result = cursor.rowcount

    logger.info(f"Deleted {result} tenants.")
