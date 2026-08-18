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
"""Tests for settings validation logic."""

import importlib
import os
import sys
from unittest import TestCase
from unittest.mock import patch


class SettingsValidationTest(TestCase):
    """Test settings validation that occurs at module import time."""

    def test_kafka_principal_cleanup_topic_required_when_kafka_enabled(self):
        """Test that settings raises ValueError when Kafka cleanup is enabled but topic is empty."""
        # Mock environment variables to simulate misconfiguration
        env_vars = {
            "PRINCIPAL_CLEANUP_DELETION_ENABLED_KAFKA": "True",
            "KAFKA_PRINCIPAL_CLEANUP_TOPIC": "",  # Empty topic - should fail
        }

        with patch.dict(os.environ, env_vars, clear=False):
            # Remove settings module from cache to force reimport with new env vars
            if "rbac.settings" in sys.modules:
                del sys.modules["rbac.settings"]

            # Attempting to import settings should raise ValueError
            with self.assertRaises(ValueError) as context:
                import rbac.settings  # noqa: F401

            self.assertIn("PRINCIPAL_CLEANUP_DELETION_ENABLED_KAFKA is True", str(context.exception))
            self.assertIn("KAFKA_PRINCIPAL_CLEANUP_TOPIC is not configured", str(context.exception))

    def test_kafka_principal_cleanup_topic_not_required_when_kafka_disabled(self):
        """Test that settings loads successfully when Kafka cleanup is disabled, even with empty topic."""
        # Mock environment variables - Kafka disabled, empty topic is OK
        env_vars = {
            "PRINCIPAL_CLEANUP_DELETION_ENABLED_KAFKA": "False",
            "KAFKA_PRINCIPAL_CLEANUP_TOPIC": "",  # Empty topic is OK when disabled
        }

        with patch.dict(os.environ, env_vars, clear=False):
            # Remove settings module from cache to force reimport
            if "rbac.settings" in sys.modules:
                del sys.modules["rbac.settings"]

            # Should not raise - this is valid configuration
            try:
                import rbac.settings  # noqa: F401

                # If we get here, import succeeded - this is expected
                self.assertTrue(True)
            except ValueError:
                self.fail("Settings should not raise ValueError when Kafka cleanup is disabled")
            finally:
                # Re-import settings to restore normal state
                if "rbac.settings" in sys.modules:
                    del sys.modules["rbac.settings"]
                importlib.import_module("rbac.settings")

    def test_kafka_principal_cleanup_succeeds_with_valid_topic(self):
        """Test that settings loads successfully when Kafka cleanup is enabled with valid topic."""
        # Mock environment variables - Kafka enabled with valid topic
        env_vars = {
            "PRINCIPAL_CLEANUP_DELETION_ENABLED_KAFKA": "True",
            "KAFKA_PRINCIPAL_CLEANUP_TOPIC": "platform.principal.events",  # Valid topic
        }

        with patch.dict(os.environ, env_vars, clear=False):
            # Remove settings module from cache to force reimport
            if "rbac.settings" in sys.modules:
                del sys.modules["rbac.settings"]

            # Should not raise - this is valid configuration
            try:
                import rbac.settings  # noqa: F401

                # Verify the topic is correctly set
                self.assertEqual(rbac.settings.KAFKA_PRINCIPAL_CLEANUP_TOPIC, "platform.principal.events")
                self.assertTrue(rbac.settings.PRINCIPAL_CLEANUP_DELETION_ENABLED_KAFKA)
            except ValueError:
                self.fail("Settings should not raise ValueError with valid Kafka topic configuration")
            finally:
                # Re-import settings to restore normal state
                if "rbac.settings" in sys.modules:
                    del sys.modules["rbac.settings"]
                importlib.import_module("rbac.settings")
