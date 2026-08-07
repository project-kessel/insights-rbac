"""Tests for logging handler deduplication and propagation logic in settings."""

import importlib
import logging
import os
from unittest import mock

from django.test import SimpleTestCase

from rbac.settings import _parse_logging_handlers


class TestLoggingHandlerDeduplication(SimpleTestCase):
    """Verify that console+ecs handlers are deduplicated to prevent duplicate log lines."""

    def test_console_only_unchanged(self):
        """DJANGO_LOG_HANDLERS=console should keep console."""
        result = _parse_logging_handlers("console")
        self.assertEqual(result, ["console"])

    def test_ecs_only_unchanged(self):
        """DJANGO_LOG_HANDLERS=ecs should keep ecs."""
        result = _parse_logging_handlers("ecs")
        self.assertEqual(result, ["ecs"])

    def test_console_and_ecs_deduplicates_to_console(self):
        """DJANGO_LOG_HANDLERS=console,ecs should drop ecs, keep console."""
        result = _parse_logging_handlers("console,ecs")
        self.assertEqual(result, ["console"])

    def test_ecs_and_console_deduplicates_to_console(self):
        """DJANGO_LOG_HANDLERS=ecs,console should drop ecs, keep console."""
        result = _parse_logging_handlers("ecs,console")
        self.assertEqual(result, ["console"])

    def test_console_ecs_watchtower_keeps_console_and_watchtower(self):
        """DJANGO_LOG_HANDLERS=console,ecs,watchtower should drop ecs only."""
        result = _parse_logging_handlers("console,ecs,watchtower")
        self.assertEqual(result, ["console", "watchtower"])

    def test_console_and_file_unchanged(self):
        """DJANGO_LOG_HANDLERS=console,file should keep both (no ecs conflict)."""
        result = _parse_logging_handlers("console,file")
        self.assertEqual(result, ["console", "file"])

    def test_whitespace_around_handler_names_stripped(self):
        """DJANGO_LOG_HANDLERS='console, ecs' should strip whitespace and deduplicate."""
        result = _parse_logging_handlers("console, ecs")
        self.assertEqual(result, ["console"])

    def test_watchtower_only_unchanged(self):
        """DJANGO_LOG_HANDLERS=watchtower should pass through unchanged."""
        result = _parse_logging_handlers("watchtower")
        self.assertEqual(result, ["watchtower"])

    def test_whitespace_only_entries_dropped(self):
        """Empty/whitespace-only entries from trailing commas are dropped."""
        result = _parse_logging_handlers("console,,ecs, ")
        self.assertEqual(result, ["console"])

    @mock.patch.dict(os.environ, {"DJANGO_LOG_HANDLERS": "console,ecs"})
    def test_settings_logging_config_uses_console_only(self):
        """When settings.py is loaded with console,ecs, LOGGING loggers should only have console."""
        # Reload settings to pick up the patched env var
        import rbac.settings as settings_module

        importlib.reload(settings_module)
        self.addCleanup(importlib.reload, settings_module)

        # All application loggers should use the deduplicated handlers
        for logger_name in ("rbac", "api", "management", "internal", "django"):
            handlers = settings_module.LOGGING["loggers"][logger_name]["handlers"]
            self.assertNotIn(
                "ecs",
                handlers,
                f"Logger '{logger_name}' should not have 'ecs' when 'console' is also configured",
            )
            self.assertIn("console", handlers, f"Logger '{logger_name}' should have 'console' handler")


class TestWatchtowerFormatterConfig(SimpleTestCase):
    """Verify that the watchtower handler always uses ecs_formatter for CloudWatch."""

    @mock.patch("boto3.client")
    @mock.patch.dict(
        os.environ,
        {
            "DJANGO_LOG_FORMATTER": "simple",
            "CW_AWS_ACCESS_KEY_ID": "test-key",
            "CW_AWS_SECRET_ACCESS_KEY": "test-secret",
            "CW_AWS_REGION": "us-east-1",
            "CW_LOG_GROUP": "test-group",
            "CW_CREATE_LOG_GROUP": "True",
        },
    )
    def test_watchtower_uses_ecs_formatter_regardless_of_log_formatter(self, mock_boto):
        """Watchtower handler always uses ecs_formatter even when DJANGO_LOG_FORMATTER=simple."""
        import rbac.settings as settings_module

        importlib.reload(settings_module)
        self.addCleanup(importlib.reload, settings_module)

        self.assertIn("watchtower", settings_module.LOGGING["handlers"])
        self.assertEqual(
            settings_module.LOGGING["handlers"]["watchtower"]["formatter"],
            "ecs_formatter",
            "CloudWatch watchtower handler must always use ecs_formatter, not the env-controlled LOGGING_FORMATTER",
        )


class TestLoggerPropagationConfig(SimpleTestCase):
    """Verify propagate=False on all app loggers prevents duplicate log output."""

    def setUp(self):
        """Load the LOGGING config from settings."""
        from rbac.settings import LOGGING

        self.logging_config = LOGGING

    def test_all_app_loggers_have_propagate_false(self):
        """Every logger with explicit handlers must have propagate=False to prevent duplicates."""
        loggers = self.logging_config["loggers"]
        for name, config in loggers.items():
            self.assertFalse(
                config.get("propagate", True),
                f"Logger '{name}' must have propagate=False to prevent duplicate log lines",
            )

    def test_root_logger_has_handlers(self):
        """Root logger must have handlers so unconfigured loggers still produce output."""
        root = self.logging_config.get("root")
        self.assertIsNotNone(root, "LOGGING must include a 'root' key")
        self.assertTrue(len(root.get("handlers", [])) > 0, "Root logger must have at least one handler")

    def test_root_logger_level_is_warning(self):
        """Root logger should be WARNING to avoid noise from unconfigured third-party loggers."""
        root = self.logging_config["root"]
        self.assertEqual(root["level"], "WARNING")

    def test_core_logger_is_configured(self):
        """The 'core' namespace (kafka, kafka_dr) must have an explicit logger entry."""
        loggers = self.logging_config["loggers"]
        self.assertIn("core", loggers, "Missing 'core' logger — core.kafka and core.kafka_dr would fall to root")
        self.assertFalse(loggers["core"].get("propagate", True))

    def test_all_code_namespaces_have_loggers(self):
        """Every top-level source namespace under rbac/ should have a matching logger entry."""
        expected = {"django", "api", "internal", "rbac", "management", "core", "migration_tool", "feature_flags"}
        configured = set(self.logging_config["loggers"].keys())
        missing = expected - configured
        self.assertEqual(missing, set(), f"Missing logger entries for namespaces: {missing}")

    def test_propagate_false_applied_at_runtime(self):
        """Verify propagate=False is applied to live logger objects, not just the config dict."""
        app_loggers = ["django", "api", "internal", "rbac", "management", "core", "migration_tool", "feature_flags"]
        for name in app_loggers:
            live_logger = logging.getLogger(name)
            self.assertFalse(
                live_logger.propagate,
                f"Logger '{name}' has propagate=True at runtime — log messages will duplicate via root handler",
            )

    def test_app_loggers_have_handlers_at_runtime(self):
        """Each app logger must have at least one handler so logs are not silently dropped."""
        for name in ("django", "api", "rbac", "management", "core"):
            live_logger = logging.getLogger(name)
            self.assertTrue(
                len(live_logger.handlers) > 0,
                f"Logger '{name}' has no handlers — log messages will be silently dropped",
            )
