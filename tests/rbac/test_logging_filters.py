"""Tests for the logging filters."""

import contextvars
import logging

from django.test import SimpleTestCase

from rbac.logging_filters import EnvironmentFilter, RequestContextFilter
from rbac.request_context import org_id_var, request_id_var, user_id_var


class TestEnvironmentFilter(SimpleTestCase):
    def setUp(self):
        self.record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test message",
            args=(),
            exc_info=None,
        )

    def test_filter_injects_env_name_and_returns_true(self):
        f = EnvironmentFilter(env_name="stage")
        result = f.filter(self.record)
        self.assertTrue(result)
        self.assertTrue(hasattr(self.record, "env_name"))
        self.assertEqual(self.record.env_name, "stage")

    def test_filter_reflects_custom_env_name(self):
        f = EnvironmentFilter(env_name="prod")
        f.filter(self.record)
        self.assertEqual(self.record.env_name, "prod")

    def test_filter_reflects_stage_env_name(self):
        f = EnvironmentFilter(env_name="stage")
        f.filter(self.record)
        self.assertEqual(self.record.env_name, "stage")


class TestRequestContextFilter(SimpleTestCase):
    """Tests for the RequestContextFilter that injects contextvars into log records."""

    def setUp(self):
        self.filter = RequestContextFilter()
        self.record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test message",
            args=(),
            exc_info=None,
        )
        # Reset context vars to defaults so copy_context() captures clean
        # state.  Without this, middleware from earlier tests can leak a
        # UUID into request_id_var and cause false failures.
        # Using addCleanup guarantees reset even if setUp raises midway.
        req_token = request_id_var.set("-")
        org_token = org_id_var.set("-")
        user_token = user_id_var.set("-")
        self.addCleanup(request_id_var.reset, req_token)
        self.addCleanup(org_id_var.reset, org_token)
        self.addCleanup(user_id_var.reset, user_token)

    def test_defaults_when_no_context_set(self):
        """Filter injects safe defaults when no context vars are set."""
        ctx = contextvars.copy_context()

        def _run():
            result = self.filter.filter(self.record)
            self.assertTrue(result)
            self.assertEqual(self.record.request_id, "-")
            self.assertEqual(self.record.org_id, "-")
            self.assertEqual(self.record.user_id, "-")

        ctx.run(_run)

    def test_injects_actual_values_when_context_vars_set(self):
        """Filter injects actual values from context vars."""
        ctx = contextvars.copy_context()

        def _run():
            request_id_var.set("abc-123")
            org_id_var.set("org-456")
            user_id_var.set("12345")

            result = self.filter.filter(self.record)
            self.assertTrue(result)
            self.assertEqual(self.record.request_id, "abc-123")
            self.assertEqual(self.record.org_id, "org-456")
            self.assertEqual(self.record.user_id, "12345")

        ctx.run(_run)

    def test_partial_context_uses_defaults_for_unset(self):
        """Unset context vars use defaults while set ones use actual values."""
        ctx = contextvars.copy_context()

        def _run():
            request_id_var.set("req-789")
            # org_id and user_id left unset

            self.filter.filter(self.record)
            self.assertEqual(self.record.request_id, "req-789")
            self.assertEqual(self.record.org_id, "-")
            self.assertEqual(self.record.user_id, "-")

        ctx.run(_run)

    def test_filter_always_returns_true(self):
        """Filter never suppresses log records."""
        ctx = contextvars.copy_context()

        def _run():
            self.assertTrue(self.filter.filter(self.record))
            request_id_var.set("id")
            self.assertTrue(self.filter.filter(self.record))

        ctx.run(_run)
