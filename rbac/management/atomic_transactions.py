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
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

import pgtransaction
from django.conf import settings
from django.db import transaction
from django.db.utils import OperationalError

# Shared isolation level configuration
ISOLATION_LEVEL = pgtransaction.SERIALIZABLE

# True only while the current context runs inside an atomic_block() SERIALIZABLE transaction.
_in_serializable_block: ContextVar[bool] = ContextVar("in_serializable_block", default=False)


def is_atomic_disabled():
    """Check if atomic transactions should be disabled (for tests)."""
    return settings.ATOMIC_RETRY_DISABLED


def atomic(func):
    """Wrap service methods in a SERIALIZABLE transaction.

    When already inside an ``atomic_block()`` SERIALIZABLE transaction, uses a
    plain savepoint (``transaction.atomic()``) instead of issuing another
    ``SET TRANSACTION ISOLATION LEVEL`` which PostgreSQL rejects after the first
    query.  The outer block already guarantees SERIALIZABLE isolation.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if is_atomic_disabled() or _in_serializable_block.get():
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


def run_atomic_with_retry[T](retries: int, callable: Callable[[], T]) -> T:  # noqa: D103
    """Run the provided function in a SERIALIZABLE transaction with the provided number of retries."""

    @atomic_with_retry(retries=retries)
    def wrapped():  # noqa: D103
        return callable()

    return wrapped()


def _is_serialization_or_deadlock(exc: OperationalError) -> bool:
    """Check if an OperationalError is a serialization failure (40001) or deadlock (40P01).

    Checks both by psycopg2 exception class (for class-based dispatch) and by
    pgcode (for raw OperationalError wrapping).
    """
    from psycopg2.errors import DeadlockDetected, SerializationFailure

    cause = exc.__cause__
    if cause is None:
        return False
    if isinstance(cause, (SerializationFailure, DeadlockDetected)):
        return True
    if hasattr(cause, "pgcode") and cause.pgcode in ("40001", "40P01"):
        return True
    return False


def run_nested_transaction_retries[T](retries: int, callable: Callable[[], T]):  # noqa: D103
    """
    Run the provided function in a nested transaction with the provided number of retries.

    Only retries on SerializationFailure (40001) or DeadlockDetected (40P01).
    Other database errors propagate immediately.

    NOTE: When running inside an outer SERIALIZABLE transaction, PostgreSQL
    aborts the *entire* transaction on serialization failure — not just the
    savepoint.  Inner savepoint retries therefore cannot recover from true
    serialization conflicts in that scenario.  Prefer ``run_atomic_with_retry``
    at the outermost boundary for proper retry semantics.
    """
    if retries < 1:
        raise ValueError("Must attempt at least 1")

    last_exception = None

    for i in range(retries):
        try:
            with transaction.atomic():
                return callable()
        except OperationalError as e:
            if _is_serialization_or_deadlock(e):
                last_exception = e
            else:
                raise

    raise last_exception


@contextmanager
def atomic_block():
    """Context manager for a SERIALIZABLE transaction block.

    Sets ``_in_serializable_block`` so that nested ``@atomic``-decorated methods
    use a plain savepoint instead of issuing a second
    ``SET TRANSACTION ISOLATION LEVEL`` (which PostgreSQL rejects after the first
    query in the transaction).
    """
    if is_atomic_disabled():
        with transaction.atomic():
            yield
        return

    token = _in_serializable_block.set(True)
    try:
        with pgtransaction.atomic(isolation_level=ISOLATION_LEVEL):
            yield
    finally:
        _in_serializable_block.reset(token)
