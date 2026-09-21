"""Tests for atomic transaction utilities."""

import logging
import threading
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

import pgtransaction
from django.db.utils import IntegrityError, OperationalError
from django.test import TransactionTestCase, override_settings
from management.atomic_transactions import (
    ISOLATION_LEVEL,
    _is_serialization_or_deadlock,
    run_atomic_with_retry,
    run_nested_transaction_retries,
)

from api.models import Tenant
from tests.identity_request import TransactionalIdentityRequest


@contextmanager
def _enable_logging():
    """Re-enable logging temporarily for assertLogs under parallel test runner.

    Django's parallel test runner disables low-level logs via logging.disable().
    This context manager restores logging within its scope and resets afterward.
    """
    prior_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    try:
        yield
    finally:
        logging.disable(prior_disable)


def _make_serialization_error(msg="conflict"):
    """Build an OperationalError wrapping a psycopg2 SerializationFailure."""
    from psycopg2.errors import SerializationFailure as Psycopg2SerializationFailure

    pg_exc = Psycopg2SerializationFailure(msg)
    exc = OperationalError(msg)
    exc.__cause__ = pg_exc
    return exc


def _make_deadlock_error(msg="deadlock detected"):
    """Build an OperationalError wrapping a psycopg2 DeadlockDetected."""
    from psycopg2.errors import DeadlockDetected as Psycopg2DeadlockDetected

    pg_exc = Psycopg2DeadlockDetected(msg)
    exc = OperationalError(msg)
    exc.__cause__ = pg_exc
    return exc


class IsSerializationOrDeadlockTests(TransactionTestCase):
    """Tests for the _is_serialization_or_deadlock helper."""

    def test_serialization_failure_returns_true(self):
        """Verify serialization failure (pgcode 40001) is detected."""
        exc = _make_serialization_error()
        self.assertTrue(_is_serialization_or_deadlock(exc))

    def test_deadlock_returns_true(self):
        """Verify deadlock (pgcode 40P01) is detected."""
        exc = _make_deadlock_error()
        self.assertTrue(_is_serialization_or_deadlock(exc))

    def test_plain_operational_error_returns_false(self):
        """Verify plain OperationalError without pgcode returns False."""
        exc = OperationalError("connection refused")
        self.assertFalse(_is_serialization_or_deadlock(exc))

    def test_operational_error_without_cause_returns_false(self):
        """Verify OperationalError without __cause__ returns False."""
        exc = OperationalError("some error")
        exc.__cause__ = None
        self.assertFalse(_is_serialization_or_deadlock(exc))


@override_settings(ATOMIC_RETRY_DISABLED=False)
class RunNestedTransactionRetriesTests(TransactionTestCase):
    """Tests for run_nested_transaction_retries error handling.

    Uses a mocked transaction.atomic to avoid Django's exception-handling
    interference in tests, while verifying the retry/filter logic.
    """

    @patch("management.atomic_transactions.transaction.atomic", return_value=nullcontext())
    def test_retries_on_serialization_failure(self, _mock_atomic):
        """Verify only SerializationFailure / DeadlockDetected are retried."""
        call_count = {"n": 0}

        def flaky():
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise _make_serialization_error()
            return "ok"

        result = run_nested_transaction_retries(5, flaky)
        self.assertEqual(result, "ok")
        self.assertEqual(call_count["n"], 3)

    @patch("management.atomic_transactions.transaction.atomic", return_value=nullcontext())
    def test_retries_on_deadlock(self, _mock_atomic):
        """Verify deadlock errors are retried."""
        call_count = {"n": 0}

        def flaky():
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise _make_deadlock_error()
            return "ok"

        result = run_nested_transaction_retries(3, flaky)
        self.assertEqual(result, "ok")
        self.assertEqual(call_count["n"], 2)

    @patch("management.atomic_transactions.transaction.atomic", return_value=nullcontext())
    def test_does_not_retry_on_integrity_error(self, _mock_atomic):
        """Check IntegrityError propagates immediately without retry."""
        call_count = {"n": 0}

        def raise_integrity():
            call_count["n"] += 1
            raise IntegrityError("duplicate key")

        with self.assertRaises(IntegrityError):
            run_nested_transaction_retries(5, raise_integrity)

        # Should have been called exactly once — no retry for IntegrityError
        self.assertEqual(call_count["n"], 1)

    @patch("management.atomic_transactions.transaction.atomic", return_value=nullcontext())
    def test_does_not_retry_on_non_serialization_operational_error(self, _mock_atomic):
        """Non-serialization OperationalError propagates immediately."""
        call_count = {"n": 0}

        def raise_connection_error():
            call_count["n"] += 1
            raise OperationalError("connection refused")

        with self.assertRaises(OperationalError):
            run_nested_transaction_retries(5, raise_connection_error)

        self.assertEqual(call_count["n"], 1)

    @patch("management.atomic_transactions.transaction.atomic", return_value=nullcontext())
    def test_raises_last_exception_when_all_retries_exhausted(self, _mock_atomic):
        """All retries exhausted raises the last exception."""

        def always_fail():
            raise _make_serialization_error()

        with self.assertRaises(OperationalError):
            run_nested_transaction_retries(3, always_fail)

    def test_savepoint_retry_cannot_recover_serialization_in_outer_serializable_tx(self):
        """Confirm inner savepoint retry does NOT recover serialization failure in outer SERIALIZABLE tx.

        PostgreSQL aborts the entire transaction on serialization failure, not
        just the savepoint.  This test uses real concurrent SERIALIZABLE
        transactions to demonstrate the limitation.  The inner retry (savepoint)
        cannot recover — only an outer-level retry can.
        """
        tenant = Tenant.objects.create(tenant_name="serial-test", org_id="serial-test-org", ready=True)

        barrier = threading.Barrier(2, timeout=10)
        results = {"outer_error": None}

        def conflicting_writer():
            """Update the same row in a separate SERIALIZABLE transaction."""
            try:
                barrier.wait()
                with pgtransaction.atomic(isolation_level=ISOLATION_LEVEL):
                    Tenant.objects.filter(pk=tenant.pk).update(tenant_name="serial-test-updated")
                barrier.wait()
            except Exception:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass

        t = threading.Thread(target=conflicting_writer)
        t.start()

        try:
            with pgtransaction.atomic(isolation_level=ISOLATION_LEVEL):
                # Read to establish serialization dependency
                Tenant.objects.get(pk=tenant.pk)

                # Let the other thread update + commit
                barrier.wait()
                barrier.wait()

                # Inner savepoint retry should NOT recover
                try:
                    run_nested_transaction_retries(
                        3,
                        lambda: Tenant.objects.filter(pk=tenant.pk).update(tenant_name="should-not-work"),
                    )
                except OperationalError:
                    results["outer_error"] = "serialization_failed"
        except OperationalError:
            results["outer_error"] = "serialization_failed"

        t.join(timeout=10)

        self.assertEqual(
            results["outer_error"],
            "serialization_failed",
            "Inner savepoint retry should NOT recover from serialization failure in outer SERIALIZABLE transaction. "
            "Use run_atomic_with_retry at the outermost boundary instead.",
        )

        Tenant.objects.filter(org_id="serial-test-org").delete()


@override_settings(ATOMIC_RETRY_DISABLED=False)
class RunAtomicWithRetryTests(TransactionTestCase):
    """Tests for run_atomic_with_retry handling serialization retries correctly."""

    def test_retries_and_succeeds_on_serialization_failure(self):
        """Verify run_atomic_with_retry retries the full transaction on serialization failure."""
        call_count = {"n": 0}

        def flaky_work():
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise _make_serialization_error()
            return "success"

        result = run_atomic_with_retry(5, flaky_work)
        self.assertEqual(result, "success")
        self.assertGreaterEqual(call_count["n"], 3)

    def test_non_serialization_error_propagates(self):
        """Non-serialization errors are not retried."""
        call_count = {"n": 0}

        def always_fail():
            call_count["n"] += 1
            raise ValueError("bad data")

        with self.assertRaises(ValueError):
            run_atomic_with_retry(5, always_fail)

        self.assertEqual(call_count["n"], 1)


class RaiseDualWriteExceptionTests(TransactionalIdentityRequest):
    """Tests for raise_dual_write_exception retry transparency."""

    def test_reraise_serialization_failure_as_operational_error(self):
        """Serialization failures must not be wrapped as DualWriteException."""
        from management.inventory_replicator.inventory_replicator import (
            DualWriteException,
            raise_dual_write_exception,
        )

        exc = _make_serialization_error()
        with self.assertRaises(OperationalError) as ctx:
            raise_dual_write_exception(exc)
        self.assertIs(ctx.exception, exc)
        self.assertNotIsInstance(ctx.exception, DualWriteException)

    def test_reraise_deadlock_as_operational_error(self):
        """Deadlocks must not be wrapped as DualWriteException."""
        from management.inventory_replicator.inventory_replicator import raise_dual_write_exception

        exc = _make_deadlock_error()
        with self.assertRaises(OperationalError) as ctx:
            raise_dual_write_exception(exc)
        self.assertIs(ctx.exception, exc)

    def test_wraps_other_errors_as_dual_write_exception(self):
        """Non-retriable errors are still wrapped as DualWriteException."""
        from management.inventory_replicator.inventory_replicator import (
            DualWriteException,
            raise_dual_write_exception,
        )

        with self.assertRaises(DualWriteException) as ctx:
            raise_dual_write_exception(ValueError("boom"))
        self.assertIsInstance(ctx.exception.args[0], ValueError)

    def test_retriable_conflict_logs_info_not_error(self):
        """Serialization conflicts must not emit ERROR (avoids Glitchtip noise on successful retries)."""
        from management.inventory_replicator.inventory_replicator import raise_dual_write_exception

        with (
            _enable_logging(),
            self.assertLogs("management.inventory_replicator.inventory_replicator", level="INFO") as cm,
        ):
            with self.assertRaises(OperationalError):
                raise_dual_write_exception(_make_serialization_error(), context="Replication event for group X")
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelname, "INFO")
        self.assertIn("retriable serialization/deadlock", cm.records[0].getMessage())

    def test_non_retriable_failure_logs_error(self):
        """Hard dual-write failures still log at ERROR."""
        from management.inventory_replicator.inventory_replicator import (
            DualWriteException,
            raise_dual_write_exception,
        )

        with (
            _enable_logging(),
            self.assertLogs("management.inventory_replicator.inventory_replicator", level="INFO") as cm,
        ):
            with self.assertRaises(DualWriteException):
                raise_dual_write_exception(ValueError("boom"), context="Replication event for group X")
        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual(len(error_records), 1)
        self.assertIn("Replication event for group X", error_records[0].getMessage())

    @override_settings(ATOMIC_RETRY_DISABLED=False)
    def test_atomic_with_retry_retries_when_handler_uses_raise_dual_write_exception(self):
        """Simulates dual-write wrapping: SSI via raise_dual_write_exception is retried."""
        from management.inventory_replicator.inventory_replicator import (
            DualWriteException,
            raise_dual_write_exception,
        )

        call_count = {"n": 0}

        def flaky_work():
            call_count["n"] += 1
            if call_count["n"] < 3:
                try:
                    raise_dual_write_exception(_make_serialization_error())
                except DualWriteException:
                    self.fail("serialization error should not be wrapped")
            return "ok"

        result = run_atomic_with_retry(5, flaky_work)
        self.assertEqual(result, "ok")
        self.assertEqual(call_count["n"], 3)
