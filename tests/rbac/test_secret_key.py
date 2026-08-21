"""Tests for SECRET_KEY configuration in settings.py."""

import importlib
import os
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase


class SecretKeyConfigurationTests(TestCase):
    """Verify SECRET_KEY is handled securely depending on DEBUG and env."""

    @staticmethod
    def _reload_settings(exclude_keys=None, clear=False, **env_overrides):
        """Reload settings module with the given environment overrides.

        Args:
            exclude_keys: Iterable of env var names to remove before reload.
            clear: If True, only env_overrides will be present in the environment.
            **env_overrides: Environment variables to set.
        """
        env = dict(os.environ) if not clear else {}
        if exclude_keys:
            for key in exclude_keys:
                env.pop(key, None)
        env.update(env_overrides)
        with mock.patch.dict(os.environ, env, clear=True):
            import rbac.settings as settings_mod

            importlib.reload(settings_mod)
            return settings_mod

    def test_explicit_key_used_when_set_debug_true(self):
        """DJANGO_SECRET_KEY env var should be used verbatim in debug mode."""
        mod = self._reload_settings(DJANGO_SECRET_KEY="explicit-test-key", DJANGO_DEBUG="True")
        self.assertEqual(mod.SECRET_KEY, "explicit-test-key")

    def test_explicit_key_used_when_set_debug_false(self):
        """DJANGO_SECRET_KEY env var should be used verbatim in production mode."""
        mod = self._reload_settings(DJANGO_SECRET_KEY="explicit-test-key", DJANGO_DEBUG="False")
        self.assertEqual(mod.SECRET_KEY, "explicit-test-key")

    def test_random_key_generated_in_debug_mode(self):
        """When DEBUG=True and no key is set, a random key should be generated."""
        sentinel = "sentinel-random-key-for-testing"
        with mock.patch("django.core.management.utils.get_random_secret_key", return_value=sentinel):
            mod = self._reload_settings(exclude_keys=["DJANGO_SECRET_KEY"], DJANGO_DEBUG="True")
        self.assertEqual(mod.SECRET_KEY, sentinel)

    def test_missing_key_raises_in_non_debug(self):
        """When DEBUG=False and no key is set, ImproperlyConfigured should be raised."""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_settings(exclude_keys=["DJANGO_SECRET_KEY"], DJANGO_DEBUG="False")
        self.assertIn("DJANGO_SECRET_KEY", str(ctx.exception))

    def test_empty_string_key_with_debug_true(self):
        """Empty string DJANGO_SECRET_KEY should be treated as unset — generates random key."""
        sentinel = "sentinel-random-key-for-testing"
        with mock.patch("django.core.management.utils.get_random_secret_key", return_value=sentinel):
            mod = self._reload_settings(DJANGO_SECRET_KEY="", DJANGO_DEBUG="True")
        self.assertEqual(mod.SECRET_KEY, sentinel)

    def test_empty_string_key_with_debug_false(self):
        """Empty string DJANGO_SECRET_KEY should raise in production mode."""
        with self.assertRaises(ImproperlyConfigured):
            self._reload_settings(DJANGO_SECRET_KEY="", DJANGO_DEBUG="False")

    def test_debug_only_false_for_exact_string(self):
        """Only the exact string 'False' produces DEBUG=False; all others are truthy."""
        for truthy_val in ["True", "1", "true", "yes", "", "0"]:
            mod = self._reload_settings(DJANGO_DEBUG=truthy_val, DJANGO_SECRET_KEY="k")
            self.assertTrue(mod.DEBUG, f"DJANGO_DEBUG={truthy_val!r} should produce DEBUG=True")

    def test_debug_false_for_exact_false_string(self):
        """DJANGO_DEBUG='False' (exact) should produce DEBUG=False."""
        mod = self._reload_settings(DJANGO_DEBUG="False", DJANGO_SECRET_KEY="k")
        self.assertFalse(mod.DEBUG)

    def tearDown(self):
        """Restore settings to working state after each test."""
        import rbac.settings as settings_mod

        # Reload with original env to leave settings in a valid state
        importlib.reload(settings_mod)
