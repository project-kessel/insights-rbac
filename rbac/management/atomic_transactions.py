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
"""Decorators for v2 services."""

import functools

import pgtransaction
from django.conf import settings
from django.db import transaction

# Shared isolation level configuration
ISOLATION_LEVEL = pgtransaction.SERIALIZABLE


def is_atomic_disabled():
    """Check if atomic transactions should be disabled (for tests)."""
    return getattr(settings, "ATOMIC_RETRY_DISABLED", False)


def atomic(func):
    """Wrap service methods in a SERIALIZABLE transaction."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if is_atomic_disabled():
            with transaction.atomic():
                return func(*args, **kwargs)
        else:
            with pgtransaction.atomic(isolation_level=ISOLATION_LEVEL):
                return func(*args, **kwargs)

    return wrapper


def atomic_with_retry(retries: int):
    """Wrap a method in a SERIALIZABLE transaction, while ensuring it retries on serialization failure."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if is_atomic_disabled():
                return transaction.atomic()(func)(*args, **kwargs)
            else:
                return pgtransaction.atomic(isolation_level=ISOLATION_LEVEL, retry=retries)(func)(*args, **kwargs)

        return wrapper

    return decorator


def atomic_block():
    """Return a context manager that can be used to turn a block into a SERIALIZABLE transaction."""
    if is_atomic_disabled():
        return transaction.atomic()

    return pgtransaction.atomic(isolation_level=ISOLATION_LEVEL)
