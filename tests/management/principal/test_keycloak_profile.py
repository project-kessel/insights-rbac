#
# Copyright 2026 Red Hat, Inc.
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

"""Tests for optional Keycloak principal enrichment."""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from requests import HTTPError, Response

from management.principal import keycloak_profile as kp


class KeycloakProfileTests(SimpleTestCase):
    def setUp(self):
        kp._reset_local_cache()
        try:
            kp.cache.delete(kp._profile_cache_key())
        except Exception:  # noqa: BLE001
            pass

    def tearDown(self):
        kp._reset_local_cache()
        try:
            kp.cache.delete(kp._profile_cache_key())
        except Exception:  # noqa: BLE001
            pass

    def test_keycloak_configured_requires_env(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(kp.keycloak_configured())
        with patch.dict(
            "os.environ",
            {
                "KEYCLOAK_URL": "http://keycloak:8080",
                "KEYCLOAK_CLIENT_ID": "rbac-sync-client",
                "KEYCLOAK_CLIENT_SECRET": "secret",
            },
            clear=True,
        ):
            self.assertTrue(kp.keycloak_configured())

    def test_get_keycloak_profiles_returns_empty_when_unconfigured(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(kp.get_keycloak_profiles(), {})

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    )
    @patch.dict(
        "os.environ",
        {
            "KEYCLOAK_URL": "http://keycloak:8080",
            "KEYCLOAK_REALM": "kubernetes",
            "KEYCLOAK_CLIENT_ID": "rbac-sync-client",
            "KEYCLOAK_CLIENT_SECRET": "secret",
            "KEYCLOAK_TLS_VERIFY": "false",
        },
        clear=False,
    )
    @patch("management.principal.keycloak_profile._KeycloakAdminClient")
    def test_get_keycloak_profiles_maps_users_and_org_admins(self, mock_client_cls):
        client = MagicMock()
        mock_client_cls.return_value = client
        client.list_org_admin_usernames.return_value = {"alice"}
        client.list_users.return_value = [
            {
                "username": "Alice",
                "firstName": "Alice",
                "lastName": "Admin",
                "email": "alice@example.com",
                "enabled": True,
            },
            {
                "username": "bob",
                "firstName": "Bob",
                "lastName": "User",
                "email": "bob@example.com",
                "enabled": True,
            },
        ]

        profiles = kp.get_keycloak_profiles()
        self.assertEqual(
            profiles["alice"],
            {
                "first_name": "Alice",
                "last_name": "Admin",
                "email": "alice@example.com",
                "is_org_admin": True,
                "enabled": True,
            },
        )
        self.assertFalse(profiles["bob"]["is_org_admin"])

        # Second call should hit cache and not construct another client.
        mock_client_cls.reset_mock()
        again = kp.get_keycloak_profiles()
        self.assertEqual(again["alice"]["is_org_admin"], True)
        mock_client_cls.assert_not_called()

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    )
    @patch.dict(
        "os.environ",
        {
            "KEYCLOAK_URL": "http://keycloak:8080",
            "KEYCLOAK_CLIENT_ID": "rbac-sync-client",
            "KEYCLOAK_CLIENT_SECRET": "secret",
        },
        clear=False,
    )
    @patch(
        "management.principal.keycloak_profile._fetch_profiles_from_keycloak",
        side_effect=RuntimeError("boom"),
    )
    def test_get_keycloak_profiles_swallows_errors(self, _mock_fetch):
        self.assertEqual(kp.get_keycloak_profiles(), {})
        # Negative cache: second call must not retry the fetch immediately.
        _mock_fetch.reset_mock()
        self.assertEqual(kp.get_keycloak_profiles(), {})
        _mock_fetch.assert_not_called()

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    )
    @patch.dict(
        "os.environ",
        {
            "KEYCLOAK_URL": "http://keycloak:8080",
            "KEYCLOAK_REALM": "kubernetes",
            "KEYCLOAK_CLIENT_ID": "rbac-sync-client",
            "KEYCLOAK_CLIENT_SECRET": "secret",
        },
        clear=False,
    )
    def test_profile_cache_key_includes_url_and_realm(self):
        self.assertIn("http://keycloak:8080", kp._profile_cache_key())
        self.assertIn("kubernetes", kp._profile_cache_key())

    def test_list_org_admin_usernames_missing_role_returns_empty(self):
        client = kp._KeycloakAdminClient.__new__(kp._KeycloakAdminClient)
        response = Response()
        response.status_code = 404

        def raise_404(*_args, **_kwargs):
            raise HTTPError(response=response)

        client._get = raise_404  # type: ignore[method-assign]
        self.assertEqual(client.list_org_admin_usernames(), set())
