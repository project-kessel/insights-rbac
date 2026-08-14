"""Tenant management business logic."""

from typing import Callable, Optional

from django.conf import settings
from management.inventory_replicator.inventory_replicator import InventoryReplicator
from management.tenant_service.tenant_service import TenantBootstrapService
from management.tenant_service.v1 import V1TenantBootstrapService
from management.tenant_service.v2 import V2TenantBootstrapService

from api.models import User


def get_tenant_bootstrap_service(
    replicator: InventoryReplicator, get_user_id: Optional[Callable[[User], str]] = None
) -> "TenantBootstrapService":
    """Get a TenantBootstrapService instance based on settings."""
    return (
        V2TenantBootstrapService(replicator, get_user_id=get_user_id)
        if settings.V2_BOOTSTRAP_TENANT
        else V1TenantBootstrapService()
    )
