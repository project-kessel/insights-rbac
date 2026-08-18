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
"""Tests for principal_cleanup_via_message_bus dispatcher task."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

from kafka.errors import KafkaError


class PrincipalCleanupDispatcherTest(TestCase):
    """Test the principal_cleanup_via_message_bus dispatcher task routing logic."""

    def setUp(self):
        """Set up test fixtures."""
        # Common settings that most tests will use
        self.default_settings = {
            "UMB_JOB_ENABLED": True,
            "KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED": True,
        }

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_umb_only_mode_processes_via_umb(self, mock_logger, mock_settings):
        """Test umb_only mode routes to UMB consumer."""
        from management.tasks import principal_cleanup_via_message_bus

        # Configure settings
        for key, value in self.default_settings.items():
            setattr(mock_settings, key, value)

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "umb_only"

            principal_cleanup_via_message_bus()

            # Verify UMB was called
            mock_umb.assert_called_once()
            # Verify Kafka was NOT called
            mock_kafka.assert_not_called()
            # Verify mode was logged
            mock_logger.info.assert_any_call("Principal cleanup mode: umb_only")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_umb_only_mode_when_umb_disabled(self, mock_logger, mock_settings):
        """Test umb_only mode logs warning when UMB is disabled."""
        from management.tasks import principal_cleanup_via_message_bus

        # UMB disabled
        mock_settings.UMB_JOB_ENABLED = False
        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = True

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "umb_only"

            principal_cleanup_via_message_bus()

            # Verify UMB was NOT called (it's disabled)
            mock_umb.assert_not_called()
            # Verify warning was logged
            mock_logger.warning.assert_any_call("UMB mode selected but UMB_JOB_ENABLED is False")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_shadow_mode_runs_both_consumers(self, mock_logger, mock_settings):
        """Test kafka_shadow mode runs both UMB (active) and Kafka (dry-run)."""
        from management.tasks import principal_cleanup_via_message_bus

        for key, value in self.default_settings.items():
            setattr(mock_settings, key, value)

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_shadow"

            principal_cleanup_via_message_bus()

            # Verify UMB was called (active mode)
            mock_umb.assert_called_once()
            # Verify Kafka was called with dry_run=True
            mock_kafka.assert_called_once_with(dry_run=True)
            # Verify mode was logged
            mock_logger.info.assert_any_call("Shadow mode: processing via UMB (active) and Kafka (dry-run)")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_shadow_mode_isolates_failures(self, mock_logger, mock_settings):
        """Test kafka_shadow mode continues if one consumer fails (isolation)."""
        from management.tasks import principal_cleanup_via_message_bus

        for key, value in self.default_settings.items():
            setattr(mock_settings, key, value)

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
            patch("sentry_sdk.capture_exception") as mock_capture,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_shadow"
            # UMB fails
            mock_umb.side_effect = Exception("UMB connection failed")

            # Should not raise - shadow mode isolates failures
            principal_cleanup_via_message_bus()

            # Verify UMB was attempted
            mock_umb.assert_called_once()
            # Verify Kafka still ran despite UMB failure
            mock_kafka.assert_called_once_with(dry_run=True)
            # Verify exception was captured
            mock_capture.assert_called()
            # Verify error was logged
            mock_logger.error.assert_any_call("Shadow mode: UMB consumer failed: %s", "UMB connection failed")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_shadow_mode_when_umb_disabled(self, mock_logger, mock_settings):
        """Test kafka_shadow mode logs warning when UMB is disabled."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.UMB_JOB_ENABLED = False
        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = True

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_shadow"

            principal_cleanup_via_message_bus()

            # Verify UMB was NOT called
            mock_umb.assert_not_called()
            # Verify Kafka still runs in shadow mode
            mock_kafka.assert_called_once_with(dry_run=True)
            # Verify warning was logged
            mock_logger.warning.assert_any_call("Shadow mode requires UMB but UMB_JOB_ENABLED is False")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_shadow_mode_when_kafka_disabled(self, mock_logger, mock_settings):
        """Test kafka_shadow mode logs warning when Kafka is disabled."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.UMB_JOB_ENABLED = True
        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = False

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_shadow"

            principal_cleanup_via_message_bus()

            # Verify UMB runs
            mock_umb.assert_called_once()
            # Verify Kafka was NOT called
            mock_kafka.assert_not_called()
            # Verify warning was logged
            mock_logger.warning.assert_any_call(
                "Shadow mode requires Kafka but KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED is False"
            )

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_validation_mode_kafka_succeeds(self, mock_logger, mock_settings):
        """Test kafka_validation mode when Kafka succeeds (no UMB fallback needed)."""
        from management.tasks import principal_cleanup_via_message_bus

        for key, value in self.default_settings.items():
            setattr(mock_settings, key, value)

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
            patch("management.principal.cleaner.kafka_validation_success_total") as mock_success_metric,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_validation"

            principal_cleanup_via_message_bus()

            # Verify Kafka was called (not dry-run)
            mock_kafka.assert_called_once_with(dry_run=False)
            # Verify UMB was NOT called (Kafka succeeded)
            mock_umb.assert_not_called()
            # Verify success metric incremented
            mock_success_metric.inc.assert_called_once()
            # Verify success logged
            mock_logger.info.assert_any_call("Validation mode: Kafka consumer completed successfully")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_validation_mode_kafka_fails_umb_succeeds(self, mock_logger, mock_settings):
        """Test kafka_validation mode when Kafka fails and UMB fallback succeeds."""
        from management.tasks import principal_cleanup_via_message_bus

        for key, value in self.default_settings.items():
            setattr(mock_settings, key, value)

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
            patch("management.principal.cleaner.kafka_validation_fallback_total") as mock_fallback_metric,
            patch("sentry_sdk.capture_exception") as mock_capture,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_validation"
            # Kafka fails
            kafka_error = Exception("Connection timeout")
            mock_kafka.side_effect = kafka_error

            principal_cleanup_via_message_bus()

            # Verify Kafka was attempted
            mock_kafka.assert_called_once_with(dry_run=False)
            # Verify UMB was called as fallback
            mock_umb.assert_called_once()
            # Verify fallback metric incremented
            mock_fallback_metric.inc.assert_called_once()
            # Verify exception was captured
            mock_capture.assert_called()
            # Verify error and fallback logged
            mock_logger.error.assert_any_call(
                "Validation mode: Kafka consumer failed: %s. Falling back to UMB.", "Connection timeout"
            )
            mock_logger.info.assert_any_call("Validation mode: UMB fallback completed successfully")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_validation_mode_both_fail(self, mock_logger, mock_settings):
        """Test kafka_validation mode when both Kafka and UMB fail (critical error)."""
        from management.tasks import principal_cleanup_via_message_bus

        for key, value in self.default_settings.items():
            setattr(mock_settings, key, value)

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
            patch("management.principal.cleaner.kafka_validation_fallback_total") as mock_fallback_metric,
            patch("sentry_sdk.capture_exception") as mock_capture,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_validation"
            # Both fail
            kafka_error = KafkaError("Connection timeout")
            umb_error = Exception("UMB broker unreachable")
            mock_kafka.side_effect = kafka_error
            mock_umb.side_effect = umb_error

            # Should raise RuntimeError
            with self.assertRaises(RuntimeError) as context:
                principal_cleanup_via_message_bus()

            # Verify error message
            self.assertIn("Both Kafka and UMB consumers failed", str(context.exception))
            # Verify both were attempted
            mock_kafka.assert_called_once_with(dry_run=False)
            mock_umb.assert_called_once()
            # Verify fallback metric incremented
            mock_fallback_metric.inc.assert_called_once()
            # Verify both exceptions were captured
            self.assertEqual(mock_capture.call_count, 2)
            # Verify error logged
            mock_logger.error.assert_any_call(
                "Validation mode: UMB fallback also failed: %s", "UMB broker unreachable"
            )

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_validation_mode_kafka_disabled(self, mock_logger, mock_settings):
        """Test kafka_validation mode when Kafka is disabled (should use UMB)."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.UMB_JOB_ENABLED = True
        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = False

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
            patch("management.principal.cleaner.kafka_validation_fallback_total") as mock_fallback_metric,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_validation"

            principal_cleanup_via_message_bus()

            # Verify Kafka was NOT called (disabled)
            mock_kafka.assert_not_called()
            # Verify UMB was called as fallback
            mock_umb.assert_called_once()
            # Verify fallback metric incremented
            mock_fallback_metric.inc.assert_called_once()
            # Verify warning logged
            mock_logger.warning.assert_any_call(
                "Validation mode requires Kafka but KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED is False"
            )

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_validation_mode_both_disabled(self, mock_logger, mock_settings):
        """Test kafka_validation mode when both Kafka and UMB are disabled."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.UMB_JOB_ENABLED = False
        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = False

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_validation"

            # Should raise RuntimeError
            with self.assertRaises(RuntimeError) as context:
                principal_cleanup_via_message_bus()

            # Verify error message
            self.assertIn("Kafka failed and no UMB fallback available", str(context.exception))
            # Verify neither was called
            mock_kafka.assert_not_called()
            mock_umb.assert_not_called()

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_active_mode_processes_via_kafka(self, mock_logger, mock_settings):
        """Test kafka_active mode routes to Kafka consumer."""
        from management.tasks import principal_cleanup_via_message_bus

        for key, value in self.default_settings.items():
            setattr(mock_settings, key, value)

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_active"

            principal_cleanup_via_message_bus()

            # Verify Kafka was called (not dry-run)
            mock_kafka.assert_called_once_with(dry_run=False)
            # Verify UMB was NOT called
            mock_umb.assert_not_called()
            # Verify mode was logged
            mock_logger.info.assert_any_call("Kafka-active mode: processing via Kafka")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_active_mode_raises_when_kafka_disabled(self, mock_logger, mock_settings):
        """Test kafka_active mode raises RuntimeError when Kafka is disabled (no silent fallback)."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.UMB_JOB_ENABLED = True
        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = False

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_active"

            # Should raise RuntimeError for configuration mismatch
            with self.assertRaises(RuntimeError) as context:
                principal_cleanup_via_message_bus()

            # Verify error message
            self.assertIn("Configuration mismatch", str(context.exception))
            self.assertIn("kafka_active mode selected in Unleash", str(context.exception))
            self.assertIn("KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED is False", str(context.exception))

            # Verify neither consumer was called
            mock_kafka.assert_not_called()
            mock_umb.assert_not_called()

            # Verify error was logged
            mock_logger.error.assert_called()

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_active_mode_both_disabled(self, mock_logger, mock_settings):
        """Test kafka_active mode raises when Kafka is disabled (even if UMB is also disabled)."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.UMB_JOB_ENABLED = False
        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = False

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "kafka_active"

            # Should raise RuntimeError for configuration mismatch
            with self.assertRaises(RuntimeError) as context:
                principal_cleanup_via_message_bus()

            # Verify error message
            self.assertIn("Configuration mismatch", str(context.exception))
            self.assertIn("kafka_active mode selected in Unleash", str(context.exception))

            # Verify neither was called
            mock_kafka.assert_not_called()
            mock_umb.assert_not_called()

            # Verify error was logged
            mock_logger.error.assert_called()

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_unknown_mode_defaults_to_umb(self, mock_logger, mock_settings):
        """Test that unknown mode defaults to UMB with error log."""
        from management.tasks import principal_cleanup_via_message_bus

        for key, value in self.default_settings.items():
            setattr(mock_settings, key, value)

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
            patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "invalid_mode"

            principal_cleanup_via_message_bus()

            # Verify UMB was called (fallback)
            mock_umb.assert_called_once()
            # Verify Kafka was NOT called
            mock_kafka.assert_not_called()
            # Verify error was logged
            mock_logger.error.assert_any_call("Unknown principal cleanup mode: invalid_mode, defaulting to UMB")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_unknown_mode_umb_disabled(self, mock_logger, mock_settings):
        """Test that unknown mode logs warning when UMB is disabled."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.UMB_JOB_ENABLED = False
        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = True

        with (
            patch("feature_flags.FEATURE_FLAGS") as mock_ff,
            patch("management.principal.cleaner.process_principal_events_from_umb") as mock_umb,
        ):

            mock_ff.get_principal_cleanup_mode.return_value = "invalid_mode"

            principal_cleanup_via_message_bus()

            # Verify UMB was NOT called
            mock_umb.assert_not_called()
            # Verify warnings were logged
            mock_logger.error.assert_any_call("Unknown principal cleanup mode: invalid_mode, defaulting to UMB")
            mock_logger.warning.assert_any_call("Fallback to UMB failed: UMB_JOB_ENABLED is False")
