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
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "profile-service-tests"}}
)
class ProfileServiceTests(SimpleTestCase):
    """Tests for profile_service helpers."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_not_configured_without_url(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(ps.profile_service_configured())
            self.assertEqual(ps.get_profiles_from_service("12345"), {})

    def test_missing_org_id_returns_empty(self):
        with patch.dict("os.environ", {"PRINCIPAL_PROFILE_SERVICE_URL": "http://profile:8099"}):
            self.assertTrue(ps.profile_service_configured())
            self.assertEqual(ps.get_profiles_from_service(""), {})
            self.assertEqual(ps.get_profiles_from_service(None), {})

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

        with patch.dict(
            "os.environ",
            {
                "PRINCIPAL_PROFILE_SERVICE_URL": "http://profile:8099/",
                "PRINCIPAL_PROFILE_TOKEN": "secret",
            },
        ):
            profiles = ps.get_profiles_from_service("12345")

        self.assertIn("admin", profiles)
        self.assertTrue(profiles["admin"]["is_org_admin"])
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "http://profile:8099/v1/orgs/12345/profiles")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")

    @patch("management.principal.profile_service.requests.get")
    def test_non_string_email_becomes_empty(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "profiles": {
                "alice": {
                    "email": 12345,
                    "first_name": "Alice",
                    "last_name": "User",
                    "is_org_admin": False,
                    "enabled": True,
                }
            }
        }
        mock_get.return_value = response

        with patch.dict("os.environ", {"PRINCIPAL_PROFILE_SERVICE_URL": "http://profile:8099"}):
            profiles = ps.get_profiles_from_service("12345")

        self.assertEqual(profiles["alice"]["email"], "")

    @patch("management.principal.profile_service.requests.get", side_effect=RuntimeError("boom"))
    def test_swallows_errors(self, _mock_get):
        with patch.dict("os.environ", {"PRINCIPAL_PROFILE_SERVICE_URL": "http://profile:8099"}):
            self.assertEqual(ps.get_profiles_from_service("12345"), {})

    @patch("management.principal.profile_service.requests.get", side_effect=RuntimeError("boom"))
    def test_failed_fetch_is_cached(self, mock_get):
        with patch.dict("os.environ", {"PRINCIPAL_PROFILE_SERVICE_URL": "http://profile:8099"}):
            self.assertEqual(ps.get_profiles_from_service("12345"), {})
            self.assertEqual(ps.get_profiles_from_service("12345"), {})
        self.assertEqual(mock_get.call_count, 1)

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

        with patch.dict("os.environ", {"PRINCIPAL_PROFILE_SERVICE_URL": "http://profile:8099"}):
            first = ps.get_profiles_from_service("12345")
            second = ps.get_profiles_from_service("12345")

        self.assertEqual(first, second)
        self.assertEqual(mock_get.call_count, 1)
