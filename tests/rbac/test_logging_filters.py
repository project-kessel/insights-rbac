"""Tests for the EnvironmentFilter logging filter."""

import logging

from django.test import SimpleTestCase

from rbac.logging_filters import EnvironmentFilter


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
