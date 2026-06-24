import logging

from django.db import transaction

from api.models import Tenant
from management.role.model import BindingMapping
from management.tenant_mapping.v2_activation import lock_tenant_version, TenantVersion

logger = logging.getLogger(__name__)


@transaction.atomic
def _do_remove_for(tenant: Tenant):
    tenant_version = lock_tenant_version(tenant)

    if tenant_version == TenantVersion.VERSION_1:
        return

    deleted_count, _ = BindingMapping.objects.filter(BindingMapping.filter_known_in_tenant(tenant)).delete()
    logger.info(f"Deleted {deleted_count} BindingMappings for tenant with org_id {tenant.org_id!r}")


def remove_v2_tenant_binding_mappings():
    """Remove old BindingMappings for tenants that have migrated to V2."""
    logger.info("About to remove BindingMappings for tenants that have migrated to V2.")

    count = 0

    for tenant in Tenant.objects.exclude(tenant_mapping__v2_write_activated_at=None).iterator():
        _do_remove_for(tenant)
        count += 1

    logger.info(f"Removed BindingMappings for {count} V2 tenants.")
