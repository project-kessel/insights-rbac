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
"""V2 write activation state for tenants.

Provides functions to lock and check whether a tenant has been activated for V2
writes. Once activated, a tenant must never write via V1 again, regardless of
feature flag state.

Uses SELECT FOR SHARE for read-lock checks so concurrent V1/V2 reads do not
block each other. Django ORM has no FOR SHARE equivalent so raw SQL is used
for that path only. The write path (escalating to exclusive) uses the ORM's
select_for_update().

The SQL strings are static literals with no interpolation; the only runtime value
(tenant_id) is always passed as a parameterized %s placeholder, so there is no
SQL injection risk despite the Sourcery warning.

Usage:
    # In V2 write paths (inside SERIALIZABLE transaction):
    ensure_v2_write_activated(tenant)

    # In V1 write paths (inside transaction):
    assert_v1_write_allowed(tenant)
"""

import enum
import logging

from django.db import connection
from django.utils import timezone
from management.tenant_mapping.model import TenantMapping
from management.tenant_service.v2 import TenantNotBootstrappedError

from api.models import Tenant

logger = logging.getLogger(__name__)

# Static SQL literals — no interpolation, tenant_id is always a %s parameter.
# FOR SHARE is intentional: concurrent V1 writes can proceed in parallel;
# only a V2 activation (FOR UPDATE) is blocked. Django ORM has no FOR SHARE
# equivalent, so raw SQL is necessary here.
_LOCK_FOR_SHARE_SQL = (  # sourcery: disable=sql-injection-risk
    "SELECT id, v2_write_activated_at" " FROM management_tenantmapping" " WHERE tenant_id = %s" " FOR SHARE"
)


def _lock_for_share(tenant: Tenant) -> tuple:
    """Lock the TenantMapping row FOR SHARE. Returns (id, v2_write_activated_at) or None."""
    with connection.cursor() as cursor:
        cursor.execute(_LOCK_FOR_SHARE_SQL, [tenant.id])  # sourcery: disable=sql-injection-risk
        row = cursor.fetchone()

        if row is None:
            raise TenantNotBootstrappedError(
                f"Tenant {tenant.org_id} has no TenantMapping; writes require tenant bootstrapping."
            )

        return row


class V1WriteBlockedError(Exception):
    """Raised when a V1 write is attempted on a tenant that has been activated for V2."""

    pass


def ensure_v2_write_activated(tenant: Tenant):
    """Mark the tenant as V2-activated if not already. Must be called inside a transaction.

    Takes a shared lock first; if already V2, returns immediately (allows concurrent V2
    reads). If not yet V2, escalates to an exclusive lock and writes. This minimises
    contention compared to always using FOR UPDATE.
    """
    _pk, v2_activated = _lock_for_share(tenant)

    if v2_activated is not None:
        return

    mapping = TenantMapping.objects.select_for_update().filter(tenant=tenant).first()
    if mapping is None:
        raise TenantNotBootstrappedError(
            f"Tenant {tenant.org_id} has no TenantMapping; V2 writes require tenant bootstrapping."
        )
    if mapping.v2_write_activated_at is not None:
        return

    mapping.v2_write_activated_at = timezone.now()
    mapping.save(update_fields=["v2_write_activated_at"])
    logger.info("Tenant %s activated for V2 writes", tenant.org_id)


def is_v2_write_activated(tenant: Tenant):
    """Check if the tenant has been activated for V2 writes (without locking).

    This is a non-locking read for use in permission classes as a fast check.
    The authoritative check with locking is done inside the transaction by
    assert_v1_write_allowed.
    """
    try:
        mapping = TenantMapping.objects.get(tenant=tenant)
        return mapping.v2_write_activated_at is not None
    except TenantMapping.DoesNotExist:
        return False


class TenantVersion(enum.IntEnum):
    """The possible versions a tenant can be in."""

    VERSION_1 = 1
    VERSION_2 = 2


def lock_tenant_version(tenant: Tenant):
    """Lock a tenant to its current version for the duration of the transaction. Returns the version of the tenant."""
    _, v2_activated = _lock_for_share(tenant)

    if v2_activated is None:
        return TenantVersion.VERSION_1

    return TenantVersion.VERSION_2


def assert_v1_write_allowed(tenant: Tenant):
    """Assert that V1 writes are still allowed for this tenant. Must be called inside a transaction.

    Uses FOR SHARE: a tenant cannot transition V2->V1, and the shared lock prevents a
    concurrent V2 activation (which needs FOR UPDATE) from converting the tenant while
    this V1 write is in progress. Concurrent V1 writes are not blocked by each other.
    """
    # We could just use lock_tenant_version here, but we instead do the check directly in order to give a better error
    # message.

    _pk, v2_activated = _lock_for_share(tenant)

    if v2_activated is not None:
        raise V1WriteBlockedError(
            f"Tenant {tenant.org_id} has been activated for V2 writes "
            f"(since {v2_activated}). V1 writes are no longer permitted."
        )
