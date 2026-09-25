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
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings

from tests.identity_request import IdentityRequest

from feature_flags import FEATURE_FLAGS, FeatureFlags, rbac_unleash_fetch_total, rbac_unleash_last_fetch_timestamp


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

    @override_settings(
        FEATURE_FLAGS_URL="http://unleash:4242/api",
        FEATURE_FLAGS_TOKEN="test-token",
        UNLEASH_REFRESH_INTERVAL=45,
        UNLEASH_REQUEST_TIMEOUT=20,
    )
    @patch("feature_flags.UnleashClient")
    def test_init_uses_configurable_refresh_interval(self, mock_unleash_cls):
        """Test that _init_unleash_client passes configurable refresh_interval and request_timeout."""
        mock_client = MagicMock()
        mock_unleash_cls.return_value = mock_client

        FEATURE_FLAGS.client = None
        result = FEATURE_FLAGS._init_unleash_client()

        self.assertEqual(result, mock_client)
        mock_client.initialize_client.assert_called_once()
        mock_unleash_cls.assert_called_once()
        call_kwargs = mock_unleash_cls.call_args[1]
        self.assertEqual(call_kwargs["refresh_interval"], 45)
        self.assertEqual(call_kwargs["request_timeout"], 20)
        self.assertIn("event_callback", call_kwargs)

    @override_settings(FEATURE_FLAGS_URL="http://unleash:4242/api", FEATURE_FLAGS_TOKEN="test-token")
    @patch("feature_flags.UnleashClient")
    def test_init_default_refresh_interval(self, mock_unleash_cls):
        """Test that _init_unleash_client uses default refresh_interval and request_timeout."""
        mock_client = MagicMock()
        mock_unleash_cls.return_value = mock_client

        FEATURE_FLAGS.client = None
        result = FEATURE_FLAGS._init_unleash_client()

        self.assertEqual(result, mock_client)
        mock_client.initialize_client.assert_called_once()
        call_kwargs = mock_unleash_cls.call_args[1]
        self.assertEqual(call_kwargs["refresh_interval"], 30)
        self.assertEqual(call_kwargs["request_timeout"], 30)


class OCMV2FeatureFlagsTest(IdentityRequest):
    """Exercise the independent OCM rollout without external services."""

    def test_target_org_and_unleash_result(self):
        """Respect Unleash for either flag state and pass the target org as a string."""
        flags = FeatureFlags()
        flags.client = MagicMock()
        for enabled in (True, False):
            with self.subTest(enabled=enabled), override_settings(OCM_V2_ENABLED=not enabled):
                flags.client.is_enabled.return_value = enabled
                self.assertIs(flags.is_ocm_v2_enabled(12345), enabled)
                args, kwargs = flags.client.is_enabled.call_args
                self.assertEqual(args, ("rbac.ocm-v2.enabled", {"orgId": "12345"}))
                self.assertIs(kwargs["fallback_function"](*args), not enabled)

    def test_unavailable_client_fallback_is_independent(self):
        """Workspace activation settings do not control OCM's fallback."""
        flags = FeatureFlags()
        with patch.object(flags, "initialize") as mock_initialize:
            for enabled in (True, False):
                with (
                    self.subTest(enabled=enabled),
                    override_settings(
                        OCM_V2_ENABLED=enabled, V2_EDIT_API_ENABLED=not enabled, V2_APIS_ENABLED=not enabled
                    ),
                ):
                    self.assertIs(flags.is_ocm_v2_enabled("12345"), enabled)
            self.assertEqual(mock_initialize.call_count, 2)

    @override_settings(OCM_V2_ENABLED=False, V2_EDIT_API_ENABLED=True)
    def test_missing_flag_defaults_off(self):
        """A missing Unleash flag leaves OCM on V1 even when workspace writes are enabled."""
        flags = FeatureFlags()
        flags.client = MagicMock()
        flags.client.is_enabled.side_effect = lambda name, context, fallback_function: fallback_function(name, context)
        self.assertFalse(flags.is_ocm_v2_enabled("12345"))

    def test_global_method_omits_org_context(self):
        """is_ocm_v2_enabled_global evaluates without orgId context."""
        flags = FeatureFlags()
        flags.client = MagicMock()
        flags.client.is_enabled.return_value = True
        self.assertTrue(flags.is_ocm_v2_enabled_global())
        args, kwargs = flags.client.is_enabled.call_args
        # context is passed as None (no org) vs per-org which passes {"orgId": ...}
        self.assertEqual(args, ("rbac.ocm-v2.enabled", None))
        self.assertNotIn("orgId", (args[1] or {}))

    @override_settings(OCM_V2_ENABLED=True)
    def test_global_fallback_uses_env(self):
        """is_ocm_v2_enabled_global falls back to OCM_V2_ENABLED when client unavailable."""
        flags = FeatureFlags()
        with patch.object(flags, "initialize") as mock_initialize:
            self.assertTrue(flags.is_ocm_v2_enabled_global())
            mock_initialize.assert_called_once_with()

    def test_on_unleash_event_tracks_fetched(self):
        """Test that _on_unleash_event increments poll counter on FETCHED events."""
        from UnleashClient.events import UnleashEventType, UnleashFetchedEvent
        import uuid

        before_count = rbac_unleash_fetch_total.labels(status="success")._value.get()

        event = UnleashFetchedEvent(
            event_type=UnleashEventType.FETCHED,
            event_id=uuid.uuid4(),
            raw_features="{}",
        )
        FeatureFlags._on_unleash_event(event)

        after_count = rbac_unleash_fetch_total.labels(status="success")._value.get()
        self.assertEqual(after_count, before_count + 1)

    def test_on_unleash_event_updates_timestamp(self):
        """Test that _on_unleash_event updates last poll timestamp on FETCHED events."""
        from UnleashClient.events import UnleashEventType, UnleashFetchedEvent
        import uuid

        event = UnleashFetchedEvent(
            event_type=UnleashEventType.FETCHED,
            event_id=uuid.uuid4(),
            raw_features="{}",
        )
        before_time = time.time()
        FeatureFlags._on_unleash_event(event)
        after_time = time.time()

        ts = rbac_unleash_last_fetch_timestamp._value.get()
        self.assertGreaterEqual(ts, before_time)
        self.assertLessEqual(ts, after_time)
