#
# Copyright 2024 Red Hat, Inc.
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

"""Tests for launch-rbac-kafka-consumer management command."""

import signal
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from django.core.management import call_command
from django.test import TestCase

# Ensure the rbac module can be found when running in different environments
if "/var/workdir" in str(Path(__file__).parent):
    # Running in container environment, add parent directory to path
    sys.path.insert(0, "/var/workdir")
else:
    # Running in local environment
    project_root = Path(__file__).parent.parent.parent
    rbac_source_dir = str(project_root / "rbac")

    # Only add the rbac source directory if it's not already in the path
    if rbac_source_dir not in sys.path:
        sys.path.insert(0, rbac_source_dir)

    # Also ensure the project root is early in sys.path for rbac package imports
    if str(project_root) not in sys.path:
        sys.path.insert(1, str(project_root))

import importlib

launch_rbac_kafka_consumer = importlib.import_module("management.management.commands.launch-rbac-kafka-consumer")
Command = launch_rbac_kafka_consumer.Command
_filter_kafka_benign_events = launch_rbac_kafka_consumer._filter_kafka_benign_events
_get_event_text_for_filtering = launch_rbac_kafka_consumer._get_event_text_for_filtering


class LaunchRBACKafkaConsumerCommandTests(TestCase):
    """Tests for launch-rbac-kafka-consumer command."""

    def setUp(self):
        """Set up test fixtures."""
        self.command = Command()

    @patch.object(launch_rbac_kafka_consumer.LoadedConfig, "metricsPort", 9000)
    @patch.object(launch_rbac_kafka_consumer, "start_http_server")
    @patch.object(launch_rbac_kafka_consumer, "RBACKafkaConsumer")
    def test_handle_success(self, mock_consumer_class, mock_start_http_server):
        """Test successful command execution."""
        mock_consumer = Mock()
        mock_consumer_class.return_value = mock_consumer

        # Mock start_consuming to avoid infinite loop
        def side_effect():
            # Simulate KeyboardInterrupt to exit gracefully
            raise KeyboardInterrupt()

        mock_consumer.start_consuming.side_effect = side_effect

        out = StringIO()

        # This should not raise an exception
        call_command("launch-rbac-kafka-consumer", stdout=out)

        mock_start_http_server.assert_called_once_with(9000, addr="0.0.0.0")
        mock_consumer_class.assert_called_once_with(topic=None)
        mock_consumer.start_consuming.assert_called_once()
        mock_consumer.stop_consuming.assert_called_once()

    @patch.object(launch_rbac_kafka_consumer.LoadedConfig, "metricsPort", 9000)
    @patch.object(launch_rbac_kafka_consumer, "start_http_server")
    @patch.object(launch_rbac_kafka_consumer, "RBACKafkaConsumer")
    def test_handle_with_custom_topic(self, mock_consumer_class, mock_start_http_server):
        """Test command execution with custom topic."""
        mock_consumer = Mock()
        mock_consumer_class.return_value = mock_consumer

        # Mock start_consuming to avoid infinite loop
        def side_effect():
            raise KeyboardInterrupt()

        mock_consumer.start_consuming.side_effect = side_effect

        out = StringIO()

        call_command("launch-rbac-kafka-consumer", "--topic", "custom-topic", stdout=out)

        mock_start_http_server.assert_called_once_with(9000, addr="0.0.0.0")
        mock_consumer_class.assert_called_once_with(topic="custom-topic")
        mock_consumer.start_consuming.assert_called_once()
        mock_consumer.stop_consuming.assert_called_once()

    @patch.object(launch_rbac_kafka_consumer.LoadedConfig, "metricsPort", 9000)
    @patch.object(launch_rbac_kafka_consumer, "start_http_server")
    @patch.object(launch_rbac_kafka_consumer, "RBACKafkaConsumer")
    @patch("sys.exit")
    def test_handle_consumer_exception(self, mock_exit, mock_consumer_class, mock_start_http_server):
        """Test handling of consumer exceptions."""
        mock_consumer = Mock()
        mock_consumer_class.return_value = mock_consumer

        # Mock start_consuming to raise an exception
        mock_consumer.start_consuming.side_effect = Exception("Consumer failed")

        out = StringIO()

        call_command("launch-rbac-kafka-consumer", stdout=out)

        mock_exit.assert_called_once_with(1)
        mock_consumer.stop_consuming.assert_called_once()

    @patch.object(launch_rbac_kafka_consumer, "RBACKafkaConsumer")
    def test_signal_handler_sigterm(self, mock_consumer_class):
        """Test SIGTERM signal handler."""
        mock_consumer = Mock()
        mock_consumer_class.return_value = mock_consumer

        command = Command()
        command.consumer = mock_consumer

        with patch("sys.exit") as mock_exit:
            command._signal_handler(signal.SIGTERM, None)

            mock_consumer.stop_consuming.assert_called_once()
            mock_exit.assert_called_once_with(0)

    @patch.object(launch_rbac_kafka_consumer, "RBACKafkaConsumer")
    def test_signal_handler_sigint(self, mock_consumer_class):
        """Test SIGINT signal handler."""
        mock_consumer = Mock()
        mock_consumer_class.return_value = mock_consumer

        command = Command()
        command.consumer = mock_consumer

        with patch("sys.exit") as mock_exit:
            command._signal_handler(signal.SIGINT, None)

            mock_consumer.stop_consuming.assert_called_once()
            mock_exit.assert_called_once_with(0)

    def test_cleanup_with_consumer(self):
        """Test cleanup when consumer exists."""
        mock_consumer = Mock()

        command = Command()
        command.consumer = mock_consumer

        command._cleanup()

        mock_consumer.stop_consuming.assert_called_once()
        self.assertIsNone(command.consumer)

    def test_cleanup_without_consumer(self):
        """Test cleanup when no consumer exists."""
        command = Command()
        command.consumer = None

        # Should not raise an exception
        command._cleanup()

        self.assertIsNone(command.consumer)

    @patch.object(launch_rbac_kafka_consumer.LoadedConfig, "metricsPort", 9000)
    @patch.object(launch_rbac_kafka_consumer, "start_http_server")
    @patch("signal.signal")
    @patch.object(launch_rbac_kafka_consumer, "RBACKafkaConsumer")
    def test_signal_registration(self, mock_consumer_class, mock_signal, mock_start_http_server):
        """Test that signal handlers are properly registered."""
        mock_consumer = Mock()
        mock_consumer_class.return_value = mock_consumer

        # Mock start_consuming to avoid infinite loop
        def side_effect():
            raise KeyboardInterrupt()

        mock_consumer.start_consuming.side_effect = side_effect

        out = StringIO()

        call_command("launch-rbac-kafka-consumer", stdout=out)

        # Verify signal handlers were registered
        self.assertEqual(mock_signal.call_count, 2)

        # Check that SIGTERM and SIGINT were registered
        signal_calls = mock_signal.call_args_list
        registered_signals = [call[0][0] for call in signal_calls]

        self.assertIn(signal.SIGTERM, registered_signals)
        self.assertIn(signal.SIGINT, registered_signals)

    def test_command_help_text(self):
        """Test command help text."""
        command = Command()

        expected_help = "Launches the RBAC Kafka consumer with validation and health checks"
        self.assertEqual(command.help, expected_help)

    @patch.object(launch_rbac_kafka_consumer.LoadedConfig, "metricsPort", 9000)
    @patch.object(launch_rbac_kafka_consumer, "start_http_server")
    @patch.object(launch_rbac_kafka_consumer, "RBACKafkaConsumer")
    def test_add_arguments(self, mock_consumer_class, mock_start_http_server):
        """Test command line argument parsing."""
        mock_consumer = Mock()
        mock_consumer_class.return_value = mock_consumer

        # Mock start_consuming to avoid infinite loop
        def side_effect():
            raise KeyboardInterrupt()

        mock_consumer.start_consuming.side_effect = side_effect

        out = StringIO()

        # Test with topic argument
        call_command("launch-rbac-kafka-consumer", "--topic", "test-topic", stdout=out)

        mock_consumer_class.assert_called_once_with(topic="test-topic")

    @patch.object(launch_rbac_kafka_consumer, "start_http_server")
    @patch.object(launch_rbac_kafka_consumer, "RBACKafkaConsumer")
    def test_default_metrics_port_fallback_when_attribute_missing(self, mock_consumer_class, mock_start_http_server):
        """Test that metrics server falls back to port 9000 when LoadedConfig.metricsPort is not defined."""
        # Save original attribute if it exists
        original_value = None
        has_original = hasattr(launch_rbac_kafka_consumer.LoadedConfig, "metricsPort")
        if has_original:
            original_value = launch_rbac_kafka_consumer.LoadedConfig.metricsPort
            delattr(launch_rbac_kafka_consumer.LoadedConfig, "metricsPort")

        try:
            mock_consumer = Mock()
            mock_consumer_class.return_value = mock_consumer

            # Mock start_consuming to avoid infinite loop
            def side_effect():
                raise KeyboardInterrupt()

            mock_consumer.start_consuming.side_effect = side_effect

            out = StringIO()

            call_command("launch-rbac-kafka-consumer", stdout=out)

            # Should use default port 9000
            mock_start_http_server.assert_called_once_with(9000, addr="0.0.0.0")
        finally:
            # Restore original attribute
            if has_original:
                setattr(launch_rbac_kafka_consumer.LoadedConfig, "metricsPort", original_value)


class SentryBeforeSendFilterTests(TestCase):
    """Tests for Sentry before_send filter that drops benign kafka-python events."""

    def test_kafka_conn_errno_104_dropped(self):
        """Test that kafka.conn logger with 'Connection reset by peer' is dropped."""
        event = {
            "logger": "kafka.conn",
            "logentry": {"message": "[Errno 104] Connection reset by peer"},
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign kafka.conn Errno 104 event should be dropped")

    def test_kafka_fetcher_fetch_to_node_dropped(self):
        """Test that kafka.consumer.fetcher logger with 'Fetch to node' is dropped."""
        event = {
            "logger": "kafka.consumer.fetcher",
            "logentry": {"formatted": "Fetch to node 3 failed with error: Connection refused"},
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign kafka fetcher 'Fetch to node' event should be dropped")

    def test_kafka_conn_authentication_failed_sent(self):
        """Test that kafka.conn logger with 'Authentication failed' is SENT (not dropped)."""
        event = {
            "logger": "kafka.conn",
            "logentry": {"message": "Authentication failed for node 1"},
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Real kafka authentication error should be SENT")

    def test_non_kafka_logger_sent(self):
        """Test that non-kafka logger events are always SENT."""
        event = {
            "logger": "management.principal.cleaner",
            "logentry": {"message": "process_kafka_message: Error processing message"},
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Non-kafka logger events should always be SENT")

    def test_exception_in_hint_dropped(self):
        """Test that benign text in exception (via hint) is caught and dropped."""
        event = {
            "logger": "kafka.conn",
            "logentry": {"message": "Error in connection"},
        }
        # Simulate exc_info tuple: (type, value, traceback)
        exc_instance = ConnectionResetError("[Errno 104] Connection reset by peer")
        hint = {
            "exc_info": (ConnectionResetError, exc_instance, None),
        }

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Event with benign text in exception (hint) should be dropped")

    def test_malformed_event_sent(self):
        """Test that malformed/empty events are SENT (fail-open, never raise)."""
        event = {}  # Missing all expected keys
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Malformed event should be SENT (fail-open)")

    def test_closing_idle_connection_dropped(self):
        """Test that 'Closing idle connection' signature is dropped."""
        event = {
            "logger": "kafka.conn",
            "logentry": {"message": "Closing idle connection to broker 192.168.1.1:9092"},
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Closing idle connection' event should be dropped")

    def test_broken_pipe_dropped(self):
        """Test that 'Broken pipe' signature is dropped."""
        event = {
            "logger": "kafka.client",
            "logentry": {"message": "[Errno 32] Broken pipe"},
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Broken pipe' event should be dropped")

    def test_case_insensitive_matching(self):
        """Test that signature matching is case-insensitive."""
        event = {
            "logger": "kafka.conn",
            "logentry": {"message": "connection reset by PEER"},  # Mixed case
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Case-insensitive matching should drop the event")

    def test_get_event_text_extracts_from_exception_values(self):
        """Test that _get_event_text_for_filtering extracts text from event exception values."""
        event = {
            "logger": "kafka.conn",
            "exception": {
                "values": [
                    {"value": "[Errno 104] Connection reset by peer"},
                ]
            },
        }
        hint = {}

        text = _get_event_text_for_filtering(event, hint)

        self.assertIn("connection reset by peer", text.lower())

    def test_filter_preserves_event_when_no_match(self):
        """Test that non-matching kafka events are preserved unchanged."""
        event = {
            "logger": "kafka.coordinator",
            "logentry": {"message": "OffsetOutOfRange error for partition 0"},
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Non-benign kafka event should be SENT unchanged")
