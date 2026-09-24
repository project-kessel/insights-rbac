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
from unittest.mock import patch


class PrincipalCleanupDispatcherTest(TestCase):
    """Test the principal_cleanup_via_message_bus dispatcher task routing logic."""

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_dispatcher_routes_to_kafka_when_enabled(self, mock_logger, mock_settings):
        """Test dispatcher routes to Kafka when KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED is True."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = True

        with patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka:
            principal_cleanup_via_message_bus()

            # Verify Kafka was called (not dry-run)
            mock_kafka.assert_called_once_with(dry_run=False)
            # Verify mode was logged
            mock_logger.info.assert_any_call("Kafka principal cleanup: processing via Kafka")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_dispatcher_warns_when_kafka_disabled(self, mock_logger, mock_settings):
        """Test dispatcher logs warning when KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED is False."""
        from management.tasks import principal_cleanup_via_message_bus

        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = False

        with patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka:
            principal_cleanup_via_message_bus()

            # Verify Kafka was NOT called (disabled)
            mock_kafka.assert_not_called()
            # Verify warning was logged
            mock_logger.warning.assert_any_call(
                "Kafka principal cleanup job is disabled (KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED is False)"
            )

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_tick_directly_processes_when_enabled(self, mock_logger, mock_settings):
        """Test principal_cleanup_kafka_tick directly processes via Kafka when enabled."""
        from management.tasks import principal_cleanup_kafka_tick

        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = True

        with patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka:
            principal_cleanup_kafka_tick()

            # Verify Kafka was called (not dry-run)
            mock_kafka.assert_called_once_with(dry_run=False)
            # Verify mode was logged
            mock_logger.info.assert_any_call("Kafka principal cleanup: processing via Kafka")

    @patch("management.tasks.settings")
    @patch("management.tasks.logger")
    def test_kafka_tick_warns_when_disabled(self, mock_logger, mock_settings):
        """Test principal_cleanup_kafka_tick logs warning when disabled."""
        from management.tasks import principal_cleanup_kafka_tick

        mock_settings.KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED = False

        with patch("management.principal.cleaner.process_principal_events_from_kafka") as mock_kafka:
            principal_cleanup_kafka_tick()

            # Verify Kafka was NOT called
            mock_kafka.assert_not_called()
            # Verify warning was logged
            mock_logger.warning.assert_any_call(
                "Kafka principal cleanup job is disabled (KAFKA_PRINCIPAL_CLEANUP_JOB_ENABLED is False)"
            )
