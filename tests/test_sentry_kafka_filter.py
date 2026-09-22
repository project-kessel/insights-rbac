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

"""Tests for sentry_kafka_filter module."""

import sys
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

# Ensure the rbac module can be found when running in different environments
if "/var/workdir" in str(Path(__file__).parent):
    # Running in container environment, add parent directory to path
    sys.path.insert(0, "/var/workdir")
else:
    # Running in local environment
    project_root = Path(__file__).parent.parent
    rbac_source_dir = str(project_root / "rbac")

    # Only add the rbac source directory if it's not already in the path
    if rbac_source_dir not in sys.path:
        sys.path.insert(0, rbac_source_dir)

    # Also ensure the project root is early in sys.path for rbac package imports
    if str(project_root) not in sys.path:
        sys.path.insert(1, str(project_root))

import importlib  # noqa: I100

sentry_kafka_filter = importlib.import_module("rbac.sentry_kafka_filter")
_filter_kafka_benign_events = sentry_kafka_filter._filter_kafka_benign_events
_get_event_text_for_filtering = sentry_kafka_filter._get_event_text_for_filtering
_is_kafka_origin = sentry_kafka_filter._is_kafka_origin
STORM_THRESHOLD = sentry_kafka_filter.STORM_THRESHOLD


def _reset_benign_match_state():
    """Reset module-level benign match state (for test isolation)."""
    with sentry_kafka_filter._benign_match_lock:
        sentry_kafka_filter._benign_match_timestamps.clear()


class SentryKafkaFilterTests(TestCase):
    """Tests for Sentry kafka benign event filter."""

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

    def test_connection_reset_by_peer_event_dropped(self):
        """Test that 'Connection reset by peer' benign kafka event is dropped."""
        event = {
            "logger": "kafka.conn",
            "message": "Connection reset by peer",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Connection reset by peer' event should be dropped")

    def test_errno_104_event_dropped(self):
        """Test that 'Errno 104' benign kafka event is dropped."""
        event = {
            "logger": "kafka.conn",
            "message": "[Errno 104] Connection reset by peer",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Errno 104' event should be dropped")

    def test_metadata_refresh_failed_event_dropped(self):
        """Test that kafka-origin 'Metadata refresh: failed' benign event is dropped."""
        event = {
            "logger": "kafka.coordinator",
            "message": "Metadata refresh: failed",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Metadata refresh: failed' event should be dropped")

    def test_fetch_to_node_headline_only_dropped(self):
        """Test that kafka-origin 'Fetch to node' headline (no Errno-104 body) is dropped."""
        event = {
            "logger": "kafka.consumer.fetcher",
            "message": "Fetch to node 2 failed",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Fetch to node' event should be dropped")

    def test_abort_event_passed_through(self):
        """Test that 'Abort' event passes through ('Abort' deliberately excluded from signatures)."""
        event = {
            "logger": "kafka.conn",
            "message": "Abort",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Non-benign 'Abort' event should be sent")

    def test_connection_lost_event_dropped(self):
        """Test that kafka-origin 'Connection lost' benign event is dropped."""
        event = {
            "logger": "kafka.conn",
            "message": "Connection lost",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Connection lost' event should be dropped")

    def test_non_kafka_origin_benign_text_sent(self):
        """GUARDRAIL: RBAC app error with benign text but NO kafka origin is SENT."""
        event = {
            "message": "Task failed: Connection reset by peer",
            "culprit": "management.tasks.some_task",
            "entries": [
                {
                    "type": "message",
                    "data": {
                        "formatted": "Task failed: Connection reset by peer",
                    },
                }
            ],
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Non-kafka app error should be SENT even with benign text")

    def test_kafka_origin_non_benign_error_sent(self):
        """Test that kafka-origin non-benign error IS sent."""
        event = {
            "logger": "kafka.conn",
            "message": "Authentication failed",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertEqual(result, event, "Kafka-origin non-benign error should be sent")

    def test_storm_breakout_first_ten_dropped_eleventh_passed(self):
        """Test storm breakout: first 10 benign matches dropped, 11th+ passed through."""
        event = {
            "logger": "kafka.conn",
            "message": "Connection reset by peer",
        }
        hint = {}

        # Mock time to control window calculations
        with patch.object(sentry_kafka_filter, "_get_monotonic_time", side_effect=range(1, 100)):
            # First 10 should be dropped
            for i in range(STORM_THRESHOLD):
                result = _filter_kafka_benign_events(event.copy(), hint)
                self.assertIsNone(result, f"Benign event {i + 1} should be dropped (within threshold)")

            # 11th and beyond should pass through (storm breakout)
            for i in range(5):
                result = _filter_kafka_benign_events(event.copy(), hint)
                self.assertEqual(result, event, f"Benign event {STORM_THRESHOLD + i + 1} should break out")

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

    def test_kafka_origin_breadcrumb_category_detection(self):
        """Test that kafka origin is detected via breadcrumb category."""
        event = {
            "message": "Connection reset by peer",
            "entries": [
                {
                    "type": "breadcrumbs",
                    "data": {
                        "values": [
                            {
                                "category": "kafka.consumer.fetcher",
                                "message": "Some kafka breadcrumb",
                                "level": "error",
                            }
                        ]
                    },
                }
            ],
        }
        hint = {}

        is_kafka = _is_kafka_origin(event)
        self.assertTrue(is_kafka, "Event with kafka breadcrumb category should be kafka origin")

        # This benign event should be dropped
        result = _filter_kafka_benign_events(event, hint)
        self.assertIsNone(result, "Kafka-origin benign event should be dropped")

    def test_closing_idle_connection_dropped(self):
        """Test that 'Closing idle connection' benign event is dropped."""
        event = {
            "logger": "kafka.conn",
            "message": "Closing idle connection",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Closing idle connection' event should be dropped")

    def test_broken_pipe_errno_32_dropped(self):
        """Test that 'Broken pipe' (Errno 32) benign event is dropped."""
        event = {
            "logger": "kafka.conn",
            "message": "[Errno 32] Broken pipe",
        }
        hint = {}

        result = _filter_kafka_benign_events(event, hint)

        self.assertIsNone(result, "Benign 'Broken pipe' event should be dropped")

    def test_event_text_extraction_from_exception_hint(self):
        """Test that event text is extracted from hint exc_info."""

        class FakeException(Exception):
            def __str__(self):
                return "Connection reset by peer"

        event = {
            "logger": "kafka.conn",
        }
        hint = {"exc_info": (FakeException, FakeException(), None)}

        event_text = _get_event_text_for_filtering(event, hint)
        self.assertIn("connection reset by peer", event_text, "Should extract text from hint exc_info")

        # This benign event should be dropped
        result = _filter_kafka_benign_events(event, hint)
        self.assertIsNone(result, "Event with benign text in hint should be dropped")
