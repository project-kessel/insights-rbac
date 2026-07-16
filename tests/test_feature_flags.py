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
"""Test the feature flags module."""

import threading
import time
from django.conf import settings
from django.test import TestCase
from unittest.mock import patch
from feature_flags import FEATURE_FLAGS


class FeatureFlagsTest(TestCase):
    """Tests feature flags functions."""

    def test_feature_flags_client(self):
        """Test that we can initialize feature flags with defaults."""
        FEATURE_FLAGS.initialize()
        client = FEATURE_FLAGS.client
        self.assertEqual(client.unleash_url, "http://localhost:4242/api")
        self.assertEqual(client.unleash_app_name, "rbac")
        self.assertEqual(FEATURE_FLAGS.is_enabled("foo"), False)

    def test_feature_flags_client_not_initialized(self):
        """Test that we can still check flags without a client."""
        FEATURE_FLAGS.client = None
        self.assertEqual(FEATURE_FLAGS.client, None)
        self.assertEqual(FEATURE_FLAGS.is_enabled("foo"), False)

    def test_feature_flags_client_not_initialized_custom_fallback(self):
        """Test that we can still check flags without a client but a custom fallback."""
        FEATURE_FLAGS.client = None
        self.assertEqual(FEATURE_FLAGS.client, None)
        self.assertEqual(FEATURE_FLAGS.is_enabled("foo", fallback_function=self._truthy_fallback), True)

    def test_thread_safe_initialization(self):
        """Test that initialization is thread-safe."""
        FEATURE_FLAGS.client = None

        # Track initialization attempts
        initialization_count = 0
        original_init = FEATURE_FLAGS._init_unleash_client

        def counting_init():
            nonlocal initialization_count
            initialization_count += 1
            # Add small delay to increase chance of race condition
            time.sleep(0.01)
            return original_init()

        FEATURE_FLAGS._init_unleash_client = counting_init

        # Start multiple threads trying to initialize
        threads = []
        for i in range(5):
            thread = threading.Thread(target=FEATURE_FLAGS.initialize)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Should only initialize once despite multiple threads
        self.assertEqual(initialization_count, 1)
        self.assertIsNotNone(FEATURE_FLAGS.client)

        # Restore original method
        FEATURE_FLAGS._init_unleash_client = original_init

    def test_multiple_initialize_calls(self):
        """Test that multiple calls to initialize are safe."""
        FEATURE_FLAGS.client = None

        # Call initialize multiple times - should be safe
        FEATURE_FLAGS.initialize()
        FEATURE_FLAGS.initialize()

        # Should only be initialized once
        self.assertIsNotNone(FEATURE_FLAGS.client)

    def test_initialization_retry_on_failure(self):
        """Test that failed initialization can be retried."""
        FEATURE_FLAGS.client = None

        # Mock a failing initialization
        original_init = FEATURE_FLAGS._init_unleash_client

        def failing_init():
            raise Exception("Initialization failed")

        FEATURE_FLAGS._init_unleash_client = failing_init

        # First call should fail
        FEATURE_FLAGS.initialize()
        self.assertIsNone(FEATURE_FLAGS.client)

        # Restore working initialization
        FEATURE_FLAGS._init_unleash_client = original_init

        # Second call should succeed
        FEATURE_FLAGS.initialize()
        self.assertIsNotNone(FEATURE_FLAGS.client)

    def _truthy_fallback(self, feature_name, context):
        return True

    def test_is_use_role_binding_view_permission_enabled_defaults_to_true(self):
        """Test that is_use_role_binding_view_permission_enabled defaults to True from settings."""
        FEATURE_FLAGS.client = None
        # When no feature flag client is available, it should fallback to settings.USE_ROLE_BINDING_VIEW_PERMISSION
        # which defaults to True
        self.assertTrue(settings.USE_ROLE_BINDING_VIEW_PERMISSION)
        self.assertTrue(FEATURE_FLAGS.is_use_role_binding_view_permission_enabled())

    @patch("feature_flags.FEATURE_FLAGS.initialize")
    def test_get_principal_cleanup_mode_defaults_to_umb_only_without_client(self, mock_initialize):
        """Test that get_principal_cleanup_mode returns 'umb_only' when client is not initialized."""
        FEATURE_FLAGS.client = None
        mode = FEATURE_FLAGS.get_principal_cleanup_mode()
        self.assertEqual(mode, "umb_only")
        # Verify that initialize was called when client was None
        mock_initialize.assert_called_once()

    def test_get_principal_cleanup_mode_returns_umb_only_when_flag_disabled(self):
        """Test that get_principal_cleanup_mode returns 'umb_only' when flag is disabled."""
        FEATURE_FLAGS.initialize()
        # Mock the is_enabled method to return False
        original_is_enabled = FEATURE_FLAGS.is_enabled

        def mock_is_enabled(feature_name, **kwargs):
            if feature_name == FEATURE_FLAGS.TOGGLE_USE_KAFKA_CLEANUP:
                return False
            return original_is_enabled(feature_name, **kwargs)

        FEATURE_FLAGS.is_enabled = mock_is_enabled

        mode = FEATURE_FLAGS.get_principal_cleanup_mode()
        self.assertEqual(mode, "umb_only")

        # Restore original method
        FEATURE_FLAGS.is_enabled = original_is_enabled

    def test_get_principal_cleanup_mode_returns_kafka_shadow_with_variant(self):
        """Test that get_principal_cleanup_mode returns 'kafka_shadow' when variant is set."""
        FEATURE_FLAGS.initialize()
        # Fail explicitly if client is unavailable
        self.assertIsNotNone(FEATURE_FLAGS.client, "FEATURE_FLAGS.client is None - cannot run test")

        # Mock both is_enabled and get_variant
        original_is_enabled = FEATURE_FLAGS.is_enabled

        def mock_is_enabled(feature_name, **kwargs):
            if feature_name == FEATURE_FLAGS.TOGGLE_USE_KAFKA_CLEANUP:
                return True
            return original_is_enabled(feature_name, **kwargs)

        FEATURE_FLAGS.is_enabled = mock_is_enabled

        # Mock get_variant to return kafka_shadow
        original_get_variant = FEATURE_FLAGS.client.get_variant

        def mock_get_variant(feature_name, **kwargs):
            if feature_name == FEATURE_FLAGS.TOGGLE_USE_KAFKA_CLEANUP:
                return {"name": "kafka_shadow", "enabled": True}
            return original_get_variant(feature_name, **kwargs)

        FEATURE_FLAGS.client.get_variant = mock_get_variant

        # Run the test - moved outside conditional so it always runs
        mode = FEATURE_FLAGS.get_principal_cleanup_mode()
        self.assertEqual(mode, "kafka_shadow")

        # Restore original methods
        FEATURE_FLAGS.client.get_variant = original_get_variant
        FEATURE_FLAGS.is_enabled = original_is_enabled

    def test_get_principal_cleanup_mode_returns_kafka_active_with_default_variant(self):
        """Test that get_principal_cleanup_mode returns 'kafka_active' when flag is enabled without specific variant."""
        FEATURE_FLAGS.initialize()
        # Fail explicitly if client is unavailable
        self.assertIsNotNone(FEATURE_FLAGS.client, "FEATURE_FLAGS.client is None - cannot run test")

        # Mock both is_enabled and get_variant
        original_is_enabled = FEATURE_FLAGS.is_enabled

        def mock_is_enabled(feature_name, **kwargs):
            if feature_name == FEATURE_FLAGS.TOGGLE_USE_KAFKA_CLEANUP:
                return True
            return original_is_enabled(feature_name, **kwargs)

        FEATURE_FLAGS.is_enabled = mock_is_enabled

        # Mock get_variant to return kafka_active (or any other variant)
        original_get_variant = FEATURE_FLAGS.client.get_variant

        def mock_get_variant(feature_name, **kwargs):
            if feature_name == FEATURE_FLAGS.TOGGLE_USE_KAFKA_CLEANUP:
                return {"name": "kafka_active", "enabled": True}
            return original_get_variant(feature_name, **kwargs)

        FEATURE_FLAGS.client.get_variant = mock_get_variant

        # Run the test - moved outside conditional so it always runs
        mode = FEATURE_FLAGS.get_principal_cleanup_mode()
        self.assertEqual(mode, "kafka_active")

        # Restore original methods
        FEATURE_FLAGS.client.get_variant = original_get_variant
        FEATURE_FLAGS.is_enabled = original_is_enabled

    def test_is_kafka_shadow_mode_enabled_returns_true_for_shadow_mode(self):
        """Test that is_kafka_shadow_mode_enabled returns True when in shadow mode."""
        FEATURE_FLAGS.initialize()
        # Mock get_principal_cleanup_mode to return 'kafka_shadow'
        original_get_mode = FEATURE_FLAGS.get_principal_cleanup_mode

        def mock_get_mode():
            return "kafka_shadow"

        FEATURE_FLAGS.get_principal_cleanup_mode = mock_get_mode

        self.assertTrue(FEATURE_FLAGS.is_kafka_shadow_mode_enabled())

        # Restore original method
        FEATURE_FLAGS.get_principal_cleanup_mode = original_get_mode

    def test_is_kafka_shadow_mode_enabled_returns_false_for_other_modes(self):
        """Test that is_kafka_shadow_mode_enabled returns False for non-shadow modes."""
        FEATURE_FLAGS.initialize()
        # Test for umb_only
        original_get_mode = FEATURE_FLAGS.get_principal_cleanup_mode

        def mock_get_mode_umb():
            return "umb_only"

        FEATURE_FLAGS.get_principal_cleanup_mode = mock_get_mode_umb
        self.assertFalse(FEATURE_FLAGS.is_kafka_shadow_mode_enabled())

        # Test for kafka_active
        def mock_get_mode_kafka():
            return "kafka_active"

        FEATURE_FLAGS.get_principal_cleanup_mode = mock_get_mode_kafka
        self.assertFalse(FEATURE_FLAGS.is_kafka_shadow_mode_enabled())

        # Restore original method
        FEATURE_FLAGS.get_principal_cleanup_mode = original_get_mode
