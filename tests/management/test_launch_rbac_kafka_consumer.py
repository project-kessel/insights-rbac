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

import importlib
import signal
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

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

launch_rbac_kafka_consumer = importlib.import_module("management.management.commands.launch-rbac-kafka-consumer")
Command = launch_rbac_kafka_consumer.Command
_filter_kafka_benign_events = launch_rbac_kafka_consumer._filter_kafka_benign_events
_get_event_text_for_filtering = launch_rbac_kafka_consumer._get_event_text_for_filtering
_is_kafka_origin = launch_rbac_kafka_consumer._is_kafka_origin

# The filter logic and its module-level state (counter, monotonic clock) now live in
# rbac.sentry_kafka_filter. The command module re-imports these names, but the filter
# functions resolve them from the sentry_kafka_filter namespace, so mock.patch targets
# must point there, not at the command module's re-exported copies.
sentry_kafka_filter = importlib.import_module("rbac.sentry_kafka_filter")


def _reset_benign_match_state():
    """Reset module-level benign match state (for test isolation)."""
    with launch_rbac_kafka_consumer._benign_match_lock:
        launch_rbac_kafka_consumer._benign_match_timestamps.clear()


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

    def setUp(self):
        """Reset benign match state before each test for isolation."""
        _reset_benign_match_state()

    def tearDown(self):
        """Reset benign match state after each test for isolation."""
        _reset_benign_match_state()

    def test_production_fetch_to_node_event_dropped(self):
        """Test that REAL production 'Fetch to node' event (GlitchTip issue 4709802) is dropped.

        This is the actual payload shape from production: no top-level "logger" field,
        message in entries[].data.formatted, kafka origin only in breadcrumbs.
        """
        event = {
            "message": "Fetch to node 3 failed: KafkaConnectionError: [Errno 104] Connection reset by peer",
            "metadata": {
                "title": "Fetch to node 3 failed: KafkaConnectionError: [Errno 104] Connection reset by peer"
            },
            "culprit": "management.tasks.principal_cleanup_via_message_bus",
            "entries": [
                {
                    "type": "message",
                    "data": {
                        "message": "Fetch to node %s failed: %s",
                        "formatted": (
                            "Fetch to node 3 failed: KafkaConnectionError: [Errno 104] Connection reset by peer"
                        ),
                        "params": ["3", "KafkaConnectionError(ConnectionResetError(104, 'Connection reset by peer'))"],
                    },
                },
                {
                    "type": "breadcrumbs",
                    "data": {
                        "values": [
                            {
                                "category": "kafka.consumer.fetcher",
                                "message": "Fetch to node 3 failed with KafkaConnectionError",
                                "level": "error",
                            }
                        ]
                    },
                },
            ],
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Production 'Fetch to node' event should be dropped")

    def test_entries_formatted_only_dropped(self):
        """Test that benign text in entries[].data.formatted (no top-level message) is caught."""
        event = {
            "logger": "kafka.conn",
            "entries": [
                {
                    "type": "message",
                    "data": {
                        "formatted": "[Errno 104] Connection reset by peer",
                    },
                }
            ],
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Event with benign text in entries[].data.formatted should be dropped")

    def test_app_error_sent_guardrail(self):
        """GUARDRAIL: Real RBAC app error with NO benign signature is SENT (not dropped)."""
        event = {
            "message": "process_kafka_message: Error decoding payload",
            "culprit": "management.principal.cleaner",
            "entries": [
                {
                    "type": "message",
                    "data": {
                        "formatted": "process_kafka_message: Error decoding payload - invalid JSON",
                    },
                }
            ],
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Real RBAC app error should be SENT")

    def test_kafka_authentication_failed_sent_guardrail(self):
        """GUARDRAIL: Kafka authentication failure (kafka origin, not on benign list) is SENT."""
        event = {
            "logger": "kafka.conn",
            "message": "Authentication failed for node 1",
            "entries": [
                {
                    "type": "message",
                    "data": {
                        "formatted": "Authentication failed for node 1",
                    },
                }
            ],
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Kafka authentication error should be SENT")

    def test_malformed_event_sent(self):
        """Test that malformed/empty events are SENT (fail-open, never raise)."""
        event = {}  # Missing all expected keys
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Malformed event should be SENT (fail-open)")

    def test_malformed_entries_non_list_sent(self):
        """Test that event with entries as non-list is SENT (fail-open)."""
        event = {
            "entries": "not a list",  # Invalid type
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Event with malformed entries should be SENT (fail-open)")

    def test_closing_idle_connection_dropped(self):
        """Test that 'Closing idle connection' signature is dropped."""
        event = {
            "logger": "kafka.conn",
            "message": "Closing idle connection to broker 192.168.1.1:9092",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Closing idle connection' event should be dropped")

    def test_broken_pipe_dropped(self):
        """Test that 'Broken pipe' signature is dropped."""
        event = {
            "logger": "kafka.conn",
            "message": "[Errno 32] Broken pipe",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Broken pipe' event should be dropped")

    def test_exception_in_hint_dropped(self):
        """Test that benign text in exception (via hint) is caught and dropped."""
        event = {
            "logger": "kafka.conn",
            "message": "Error in connection",
        }
        # Simulate exc_info tuple: (type, value, traceback)
        exc_instance = ConnectionResetError("[Errno 104] Connection reset by peer")
        hint = {
            "exc_info": (ConnectionResetError, exc_instance, None),
        }

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Event with benign text in exception (hint) should be dropped")

    def test_case_insensitive_matching(self):
        """Test that signature matching is case-insensitive."""
        event = {
            "logger": "kafka.conn",
            "message": "connection reset by PEER",  # Mixed case
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Case-insensitive matching should drop the event")

    def test_get_event_text_extracts_from_exception_values(self):
        """Test that _get_event_text_for_filtering extracts text from event exception values."""
        event = {
            "exception": {
                "values": [
                    {"value": "[Errno 104] Connection reset by peer"},
                ]
            },
        }
        hint = {}

        text = _get_event_text_for_filtering(event, hint)

        self.assertIn("connection reset by peer", text.lower())

    def test_get_event_text_handles_message_as_dict(self):
        """Test that _get_event_text_for_filtering handles message field as dict."""
        event = {
            "message": {
                "message": "raw message",
                "formatted": "Fetch to node 5 failed",
            }
        }
        hint = {}

        text = _get_event_text_for_filtering(event, hint)

        self.assertIn("fetch to node", text.lower())

    def test_top_level_title_extracted(self):
        """Test that top-level title field is extracted."""
        event = {
            "title": "Connection reset by peer",
        }
        hint = {}

        text = _get_event_text_for_filtering(event, hint)

        self.assertIn("connection reset by peer", text.lower())

    def test_steady_low_rate_all_dropped(self):
        """Test that steady low-rate benign events (< threshold) are all dropped."""
        benign_event = {
            "logger": "kafka.conn",
            "message": "[Errno 104] Connection reset by peer",
        }
        hint = {}

        # Call before_send 5 times (well under threshold of 10)
        for i in range(5):
            result = _filter_kafka_benign_events(benign_event, hint)
            self.assertIsNone(result, f"Benign event {i + 1} should be dropped (low rate)")

    @patch.object(sentry_kafka_filter, "kafka_benign_events_matched_total")
    def test_storm_breakout_passes_through_after_threshold(self, mock_counter):
        """Test that storm breakout passes events through after threshold is reached."""
        benign_event = {
            "logger": "kafka.conn",
            "message": "[Errno 104] Connection reset by peer",
        }
        hint = {}

        # Call before_send 15 times in quick succession
        for i in range(15):
            result = _filter_kafka_benign_events(benign_event, hint)
            if i < 10:
                # First 10 should be dropped (under threshold)
                self.assertIsNone(result, f"Benign event {i + 1} should be dropped (under threshold)")
            else:
                # Events 11-15 should pass through (storm breakout)
                self.assertEqual(result, benign_event, f"Benign event {i + 1} should pass through (storm breakout)")

        # All 15 benign matches should have incremented the counter
        # (10 suppressed + 5 passed through = 15 total)
        self.assertEqual(mock_counter.inc.call_count, 15)

    @patch.object(sentry_kafka_filter, "_get_monotonic_time")
    def test_window_eviction_resumes_suppression(self, mock_time):
        """Test that suppression resumes after old timestamps evict from the window."""
        benign_event = {
            "logger": "kafka.conn",
            "message": "[Errno 104] Connection reset by peer",
        }
        hint = {}

        # Start at time 0
        mock_time.return_value = 0.0

        # Add 5 events at time 0 (all dropped)
        for i in range(5):
            result = _filter_kafka_benign_events(benign_event, hint)
            self.assertIsNone(result, f"Event {i + 1} at t=0 should be dropped")

        # Advance time to 400 seconds (past the 300-second window)
        mock_time.return_value = 400.0

        # Events from t=0 are now outside the window, so suppression should resume
        result = _filter_kafka_benign_events(benign_event, hint)
        self.assertIsNone(result, "Event after window eviction should be dropped (suppression resumed)")

    def test_fetch_to_node_with_reset_dropped(self):
        """Test that 'Fetch to node' with a reset signature is dropped."""
        event = {
            "logger": "kafka.consumer.fetcher",
            "message": "Fetch to node 3 failed: KafkaConnectionError: [Errno 104] Connection reset by peer",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "'Fetch to node' with reset signature should be dropped")

    def test_fetch_to_node_headline_dropped(self):
        """Test that a 'Fetch to node' headline is dropped even without reset text.

        'Fetch to node' is itself a benign signature (kafka connection churn is grouped
        under this headline in GlitchTip), so a kafka-origin 'Fetch to node' event is
        suppressed regardless of whether the Errno-104 reset text is also present.
        """
        event = {
            "logger": "kafka.consumer.fetcher",
            "message": "Fetch to node 3 failed",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "'Fetch to node' headline should be dropped")

    def test_kafka_origin_non_benign_message_sent(self):
        """Test that a kafka-origin event matching NO benign signature is SENT.

        Guards the signature list: a genuine kafka failure (e.g. auth) whose text does
        not match any benign signature must still surface, even from a kafka logger.
        """
        event = {
            "logger": "kafka.conn",
            "message": "Authentication failed: SASL handshake error",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Kafka-origin non-benign error should be SENT")

    def test_non_kafka_origin_with_benign_text_sent_guardrail(self):
        """GUARDRAIL: Non-kafka app error containing benign text is SENT (origin gate blocks drop).

        Addresses reviewer concern: an RBAC task error wrapping 'Connection reset by peer'
        must not be suppressed just because the text matches a benign signature.
        """
        event = {
            "message": "principal_cleanup_via_message_bus failed: Connection reset by peer",
            "culprit": "management.tasks.principal_cleanup_via_message_bus",
            "entries": [
                {
                    "type": "message",
                    "data": {
                        "formatted": "principal_cleanup_via_message_bus failed: Connection reset by peer",
                    },
                }
            ],
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Non-kafka app error with benign text must be SENT (origin gate)")

    def test_non_kafka_origin_broken_pipe_sent_guardrail(self):
        """GUARDRAIL: Non-kafka error with 'Broken pipe' text is SENT when origin is not kafka."""
        event = {
            "logger": "management.principal.cleaner",
            "message": "[Errno 32] Broken pipe",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Non-kafka logger with benign text must be SENT")

    def test_breadcrumb_kafka_origin_detected(self):
        """Test that kafka origin is detected via breadcrumb category (no logger field)."""
        event = {
            "message": "[Errno 104] Connection reset by peer",
            "entries": [
                {
                    "type": "breadcrumbs",
                    "data": {
                        "values": [
                            {
                                "category": "kafka.consumer.fetcher",
                                "message": "Connection error",
                                "level": "error",
                            }
                        ]
                    },
                }
            ],
        }

        self.assertTrue(_is_kafka_origin(event), "Breadcrumb with kafka. category should detect kafka origin")

    def test_no_kafka_origin_without_markers(self):
        """Test that events without kafka logger or breadcrumbs are not kafka origin."""
        event = {
            "logger": "management.principal.cleaner",
            "message": "Some error occurred",
        }

        self.assertFalse(_is_kafka_origin(event), "Non-kafka logger should not be kafka origin")

    def test_non_kafka_logger_with_kafka_breadcrumbs_not_kafka_origin(self):
        """An explicit non-Kafka logger wins even when kafka breadcrumbs exist."""
        event = {
            "logger": "celery.task",
            "message": "Broken pipe",
            "breadcrumbs": {
                "values": [
                    {
                        "category": "kafka.conn",
                        "message": "connection closed",
                    }
                ]
            },
        }
        self.assertFalse(
            _is_kafka_origin(event),
            "Explicit non-kafka logger must override kafka breadcrumbs",
        )

    def test_empty_event_not_kafka_origin(self):
        """Test that empty event is not kafka origin (fail-open)."""
        self.assertFalse(_is_kafka_origin({}), "Empty event should not be kafka origin")

    def test_top_level_breadcrumbs_kafka_origin(self):
        """Test that kafka origin is detected via top-level breadcrumbs dict (SDK shape)."""
        event = {
            "message": "Connection reset by peer",
            "breadcrumbs": {
                "values": [
                    {
                        "category": "kafka.conn",
                        "message": "connection closed",
                    }
                ]
            },
        }

        self.assertTrue(_is_kafka_origin(event), "Top-level breadcrumbs with kafka. category = kafka origin")
