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

"""Unit tests for Cost principal profile HTTP client."""

from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from management.principal import profile_service as ps


@override_settings(
    CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "profile-service-tests"}
    },
    PRINCIPAL_PROFILE_SERVICE_URL="",
    PRINCIPAL_PROFILE_TOKEN="",
    PRINCIPAL_PROFILE_REQUEST_TIMEOUT=5,
    PRINCIPAL_PROFILE_CACHE_TTL=30,
)
class ProfileServiceTests(SimpleTestCase):
    """Tests for profile_service helpers."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_not_configured_without_url(self):
        self.assertFalse(ps.profile_service_configured())
        self.assertEqual(ps.get_profiles_from_service("12345"), {})

    @override_settings(PRINCIPAL_PROFILE_SERVICE_URL="http://profile:8099")
    def test_missing_org_id_returns_empty(self):
        self.assertTrue(ps.profile_service_configured())
        self.assertEqual(ps.get_profiles_from_service(""), {})
        self.assertEqual(ps.get_profiles_from_service(None), {})

    @override_settings(
        PRINCIPAL_PROFILE_SERVICE_URL="http://profile:8099/",
        PRINCIPAL_PROFILE_TOKEN="secret",
    )
    @patch("management.principal.profile_service.requests.get")
    def test_maps_profiles_lower(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "profiles": {
                "Admin": {
                    "email": "admin@example.com",
                    "first_name": "Org",
                    "last_name": "Admin",
                    "is_org_admin": True,
                    "enabled": True,
                }
            }
        }
        mock_get.return_value = response

        profiles = ps.get_profiles_from_service("12345")

        self.assertIn("admin", profiles)
        self.assertTrue(profiles["admin"]["is_org_admin"])
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "http://profile:8099/v1/orgs/12345/profiles")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")

    @override_settings(PRINCIPAL_PROFILE_SERVICE_URL="http://profile:8099")
    @patch("management.principal.profile_service.requests.get")
    def test_non_string_fields_become_empty(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "profiles": {
                "alice": {
                    "email": 12345,
                    "first_name": {"bad": True},
                    "last_name": ["x"],
                    "is_org_admin": False,
                    "enabled": True,
                }
            }
        }
        mock_get.return_value = response

        profiles = ps.get_profiles_from_service("12345")

        self.assertEqual(profiles["alice"]["email"], "")
        self.assertEqual(profiles["alice"]["first_name"], "")
        self.assertEqual(profiles["alice"]["last_name"], "")

    @override_settings(PRINCIPAL_PROFILE_SERVICE_URL="http://profile:8099")
    @patch("management.principal.profile_service.requests.get", side_effect=RuntimeError("boom"))
    def test_swallows_errors(self, _mock_get):
        self.assertEqual(ps.get_profiles_from_service("12345"), {})

    @override_settings(PRINCIPAL_PROFILE_SERVICE_URL="http://profile:8099")
    @patch("management.principal.profile_service.requests.get", side_effect=RuntimeError("boom"))
    def test_failed_fetch_is_cached(self, mock_get):
        self.assertEqual(ps.get_profiles_from_service("12345"), {})
        self.assertEqual(ps.get_profiles_from_service("12345"), {})
        self.assertEqual(mock_get.call_count, 1)

    @override_settings(PRINCIPAL_PROFILE_SERVICE_URL="http://profile:8099")
    @patch("management.principal.profile_service.requests.get")
    def test_successful_fetch_is_cached(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "profiles": {
                "alice": {
                    "email": "alice@example.com",
                    "first_name": "Alice",
                    "last_name": "User",
                    "is_org_admin": False,
                    "enabled": True,
                }
            }
        }
        mock_get.return_value = response

        first = ps.get_profiles_from_service("12345")
        second = ps.get_profiles_from_service("12345")

        self.assertEqual(first, second)
        self.assertEqual(mock_get.call_count, 1)

    @override_settings(PRINCIPAL_PROFILE_SERVICE_URL="http://profile:8099")
    @patch("management.principal.profile_service.cache.get", side_effect=RuntimeError("redis down"))
    @patch("management.principal.profile_service.requests.get", side_effect=RuntimeError("boom"))
    def test_cache_read_failure_falls_back(self, mock_get, _mock_cache_get):
        self.assertEqual(ps.get_profiles_from_service("12345"), {})
        mock_get.assert_called_once()
