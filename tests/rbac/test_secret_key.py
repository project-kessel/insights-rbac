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

    def test_explicit_key_used_when_set(self):
        """DJANGO_SECRET_KEY env var should be used verbatim."""
        mod = self._reload_settings(DJANGO_SECRET_KEY="explicit-test-key")
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

    def tearDown(self):
        """Restore settings to working state after each test."""
        import rbac.settings as settings_mod

        # Reload with original env to leave settings in a valid state
        importlib.reload(settings_mod)
