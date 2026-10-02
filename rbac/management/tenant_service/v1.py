"""V1 implementation of tenant bootstrapping."""

from typing import Optional

from management.inventory_replicator.noop_replicator import NoopReplicator
from management.principal.model import Principal
from management.tenant_mapping.model import logger
from management.tenant_service.tenant_service import BootstrappedTenant
from management.tenant_service.tenant_service import _ensure_principal_with_user_id_in_tenant


from api.models import Tenant, User
from api.serializers import create_tenant_name


class V1TenantBootstrapService:
    """Service for bootstrapping tenants which retains V1-only behavior."""

    def new_bootstrapped_tenant(self, org_id: str, account_number: Optional[str] = None) -> BootstrappedTenant:
        """Create a new tenant."""
        return self._get_or_bootstrap_tenant(org_id, account_number)

    def update_user(
        self,
        user: User,
        upsert: bool = False,
        bootstrapped_tenant: Optional[BootstrappedTenant] = None,
        ready_tenant: bool = True,
    ) -> Optional[BootstrappedTenant]:
        """Bootstrap a user in a tenant."""
        if user.is_active:
            return self._update_active_user(user, upsert, ready_tenant)
        else:
            self._update_inactive_user(user)
            return None

    def _update_active_user(self, user: User, upsert: bool, ready_tenant: bool) -> Optional[BootstrappedTenant]:
        if upsert:
            bootstrapped = self._get_or_bootstrap_tenant(user.org_id, user.account, ready=ready_tenant)
        else:
            try:
                tenant = Tenant.objects.get(org_id=user.org_id)
                bootstrapped = BootstrappedTenant(tenant=tenant, mapping=None)
            except Tenant.DoesNotExist:
                return None

        _ensure_principal_with_user_id_in_tenant(user, bootstrapped.tenant, upsert=upsert, replicator=NoopReplicator())

        return bootstrapped

    def _get_or_bootstrap_tenant(
        self, org_id: Optional[str], account_number: Optional[str] = None, ready: bool = True
    ) -> BootstrappedTenant:
        tenant_name = create_tenant_name(account_number)
        tenant, _ = Tenant.objects.get_or_create(
            org_id=org_id,
            defaults={"ready": ready, "account_id": account_number, "tenant_name": tenant_name},
        )
        return BootstrappedTenant(tenant=tenant, mapping=None)

    def _update_inactive_user(self, user: User) -> None:
        try:
            tenant = Tenant.objects.get(org_id=user.org_id)
            principal = Principal.objects.get(username=user.username, tenant=tenant)
            groups = []
            for group in principal.group.all():
                groups.append(group)
                # We have to do the removal explicitly in order to clear the cache,
                # or the console will still show the cached number of members
                group.principals.remove(principal)
            principal.delete()
            if not groups:
                logger.info(f"Principal {user.user_id} was not under any groups.")
            for group in groups:
                logger.info(f"Principal {user.user_id} was in group with uuid: {group.uuid}")
        except (Tenant.DoesNotExist, Principal.DoesNotExist):
            return None
