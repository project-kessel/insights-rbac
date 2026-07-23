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
