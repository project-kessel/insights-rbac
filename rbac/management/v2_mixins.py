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
"""Mixins for v2 API ViewSets."""

import logging

import pgtransaction
from django.db import OperationalError
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from psycopg2.errors import DeadlockDetected, SerializationFailure
from rest_framework import status
from rest_framework.response import Response

from management.atomic_transactions import ISOLATION_LEVEL, is_atomic_disabled  # noqa: I100, I202

logger = logging.getLogger(__name__)


class AtomicOperationsMixin:
    """Mixin providing atomic create/update/destroy with SERIALIZABLE isolation.

    Subclasses should NOT override create/update/destroy directly. Instead, override
    the perform_atomic_* hooks which run inside the SERIALIZABLE transaction with
    automatic retries and concurrency error handling.

    Set ATOMIC_RETRY_DISABLED=True in settings to skip the transaction wrapper entirely.
    """

    # Prevent subclasses from accidentally overriding core atomic methods;
    # enforce overrides must go through the 'perform_atomic_*' hooks instead.
    _GUARDED_METHODS = frozenset(("create", "update", "destroy"))

    atomic_retry = 3

    def __init_subclass__(cls, **kwargs):
        """Initialize the subclass and check for accidental method overrides."""
        super().__init_subclass__(**kwargs)
        for method in AtomicOperationsMixin._GUARDED_METHODS:
            if method in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} must not override '{method}()'. "
                    f"Override 'perform_atomic_{method}()' instead to run "
                    f"inside the SERIALIZABLE transaction."
                )

    def _handle_concurrency_error(self, e, operation_name):
        if hasattr(e, "__cause__"):
            if isinstance(e.__cause__, SerializationFailure):
                logger.exception("SerializationFailure in %s operation", operation_name)
                return Response(
                    {"detail": "Too many concurrent updates. Please retry."},
                    status=status.HTTP_409_CONFLICT,
                )
            elif isinstance(e.__cause__, DeadlockDetected):
                logger.exception("DeadlockDetected in %s operation", operation_name)
                return Response(
                    {"detail": "Internal server error. Please try again later."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        return None

    def _run_atomic(self, operation, request, *args, **kwargs):
        if is_atomic_disabled():
            ensure_v2_write_activated(request.tenant)
            return operation(request, *args, **kwargs)

        @pgtransaction.atomic(isolation_level=ISOLATION_LEVEL, retry=self.atomic_retry)
        def atomic_operation():
            ensure_v2_write_activated(request.tenant)
            return operation(request, *args, **kwargs)

        return atomic_operation()

    def _atomic_action(self, operation, operation_name, request, *args, **kwargs):
        """Run an operation inside a SERIALIZABLE transaction with concurrency error handling."""
        try:
            return self._run_atomic(operation, request, *args, **kwargs)
        except OperationalError as e:
            response = self._handle_concurrency_error(e, operation_name)
            if response:
                return response
            raise

    def create(self, request, *args, **kwargs):
        """Create with atomic transaction and concurrency handling. Override perform_atomic_create instead."""
        return self._atomic_action(self.perform_atomic_create, "create", request, *args, **kwargs)

    def perform_atomic_create(self, request, *args, **kwargs):
        """Override to customize create logic. Runs inside a SERIALIZABLE transaction."""
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        """Update with atomic transaction and concurrency handling. Override perform_atomic_update instead."""
        return self._atomic_action(self.perform_atomic_update, "update", request, *args, **kwargs)

    def perform_atomic_update(self, request, *args, **kwargs):
        """Override to customize update logic. Runs inside a SERIALIZABLE transaction."""
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Destroy with atomic transaction and concurrency handling. Override perform_atomic_destroy instead."""
        return self._atomic_action(self.perform_atomic_destroy, "destroy", request, *args, **kwargs)

    def perform_atomic_destroy(self, request, *args, **kwargs):
        """Override to customize destroy logic. Runs inside a SERIALIZABLE transaction."""
        return super().destroy(request, *args, **kwargs)
