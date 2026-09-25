#
# Copyright 2025 Red Hat, Inc.
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
"""Backfill remote principals in SpiceDB via TenantMapping update_user."""

import copy
from functools import wraps
from typing import Optional

from django.conf import settings
from django.db import transaction as django_transaction
from management.atomic_transactions import (
    atomic as atomic_serializable,
    atomic_with_retry as atomic_serializable_with_retry,
    is_atomic_disabled,
)
from management.models import Principal
from pgtransaction import transaction


def backfill_atomic(retries: Optional[int] = None):
    """Transform functions that need to be SERIALIZABLE transactions if and only if backfill_remote_principal is.

    Selects a transaction wrapper at call time based on the current settings:
    - When ``ATOMIC_RETRY_DISABLED`` is true, uses plain Django ``transaction.atomic()``
      (avoids ``PGTRANSACTION_RETRY`` side-effects in test/disabled mode).
    - When ``PRINCIPAL_BACKFILL_AUTHORITATIVE_ENABLED`` is true, uses a
      SERIALIZABLE transaction (with optional retries).
    - Otherwise, uses a regular pgtransaction atomic (no SERIALIZABLE isolation).

    Args:
        retries: Optional number of retry attempts for serialization failures.
    """

    def real_decorator():
        """Return the appropriate transaction wrapper based on current settings."""
        if is_atomic_disabled():
            return django_transaction.atomic()

        if settings.PRINCIPAL_BACKFILL_AUTHORITATIVE_ENABLED:
            if retries is not None:
                return atomic_serializable_with_retry(retries=retries)

            return atomic_serializable
        else:
            return transaction.atomic(retry=retries)

    def decorator(fn):
        """Wrap ``fn`` so the transaction wrapper is evaluated on each call.

        Settings are evaluated at call time (not import time) because they can
        change during tests.
        """

        @wraps(fn)
        def wrapped(*args, **kwargs):
            """Execute ``fn`` inside the dynamically-selected transaction wrapper."""
            return real_decorator()(fn)(*args, **kwargs)

        return wrapped

    return decorator


@backfill_atomic()
def backfill_remote_principal(bootstrap_service, user, tenant):
    """Backfill a single user's TenantMapping membership via update_user.

    Checks whether the user's Principal record already has a ``user_id`` set;
    if so, no sync is needed.  System users, service accounts, inactive users,
    and users without a ``user_id`` are skipped.

    Validates the user's org_id against the tenant and falls back to the
    tenant's org_id when the user has none.  A shallow copy is used when a
    fallback is needed so the caller's object is never mutated.

    Raises on failure — callers that want best-effort behaviour should catch
    exceptions themselves.

    Args:
        bootstrap_service: TenantBootstrapService instance.
        user: User object to sync.
        tenant: Tenant instance for principal lookup.

    Raises:
        ValueError: If the user's org_id does not match the tenant's org_id.
    """
    if user.system or user.is_service_account:
        return
    if not user.username:
        return
    if not user.user_id or not user.is_active:
        return
    # Cross-access requests rewrite username to "{org_id}-{user_id}" for CAR principal
    # lookup. Do not create/upsert a real principal under that synthetic name — it
    # collides with the requester's existing user_id (RHCLOUD-51516).
    if user.cross_access:
        return

    if user.org_id and user.org_id != tenant.org_id:
        raise ValueError(f"User {user.username} org_id {user.org_id} does not match tenant org_id {tenant.org_id}")

    try:
        principal = Principal.objects.get(username__iexact=user.username, tenant=tenant)
        if principal.user_id is not None:
            return
    except Principal.DoesNotExist:
        pass  # New principal — needs sync.

    if settings.PRINCIPAL_BACKFILL_AUTHORITATIVE_ENABLED:
        effective_user = user

        # TODO: remove this
        #
        # Although org_id appears to always be included in Users produced from real PrincipalProxy responses, some tests
        # do not include it, so this is a hack to avoid having to update every test that ends up touching this code.
        # Really, we should just make org_id mandatory, but that's a bigger change.
        if not user.org_id:
            effective_user = copy.copy(user)
            effective_user.org_id = tenant.org_id

        bootstrap_service.update_user(effective_user, upsert=True)
    else:
        # There is potentially a race condition with Kafka here, since we might do this with a user we received that
        # was, at the time, active but has since been deactivated. In the worst case, we'll just have a principal for
        # an inactive user with no default permissions. We choose to accept this risk. If this becomes a proble,
        # in the worst case we can run the job to remove all inactive principals.
        Principal.objects.filter(username__iexact=user.username).get_or_create(
            tenant=tenant,
            defaults={"username": user.username, "user_id": user.user_id},
        )


def backfill_remote_principals(bootstrap_service, users, tenant):
    """Backfill a list of users' TenantMapping membership via update_user.

    Delegates org_id validation and backfill to ``backfill_remote_principal``.
    Exceptions propagate to the caller.

    Args:
        bootstrap_service: TenantBootstrapService instance.
        users: Iterable of User objects to sync.
        tenant: Tenant instance for principal lookup.

    Raises:
        ValueError: If a user's org_id does not match the tenant's org_id.
    """
    for user in users:
        backfill_remote_principal(bootstrap_service, user, tenant)
