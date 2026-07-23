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
"""Unit tests for is_no_auth function.

NOTE: This test file was generated with AI assistance to ensure comprehensive
security coverage for the authentication bypass function.
"""

from unittest.mock import Mock

from django.test import TestCase
from django.urls import reverse

from rbac.middleware import is_no_auth


class IsNoAuthFunctionTest(TestCase):
    """Unit tests for the is_no_auth function to ensure proper authentication bypass."""

    def test_ready_endpoint_no_auth(self):
        """Test that the ready endpoint does not require auth."""
        mock_request = Mock(path=reverse("v1_api:server-ready"))
        self.assertTrue(is_no_auth(mock_request))

    def test_status_endpoint_no_auth(self):
        """Test that the status endpoint does not require auth."""
        mock_request = Mock(path=reverse("v1_api:server-status"))
        self.assertTrue(is_no_auth(mock_request))

    def test_openapi_endpoint_no_auth(self):
        """Test that the openapi endpoint does not require auth."""
        mock_request = Mock(path=reverse("v1_api:openapi"))
        self.assertTrue(is_no_auth(mock_request))

    def test_metrics_endpoint_no_auth(self):
        """Test that the metrics endpoint does not require auth."""
        mock_request = Mock(path="/metrics")
        self.assertTrue(is_no_auth(mock_request))

    def test_regular_endpoint_requires_auth(self):
        """Test that regular endpoints require authentication."""
        mock_request = Mock(path="/api/rbac/v1/groups/")
        self.assertFalse(is_no_auth(mock_request))

    def test_fake_status_suffix_requires_auth(self):
        """Test that endpoints with status as suffix require auth."""
        test_paths = [
            "/api/rbac/v1/groups/status/",
            "/api/rbac/v1/roles/status",
            "/something/status/",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(is_no_auth(mock_request), f"Path '{path}' should require authentication")

    def test_fake_status_prefix_requires_auth(self):
        """Test that endpoints with status as prefix require auth."""
        test_paths = [
            "/status/something",
            "/statuspage",
            "/status-check",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(is_no_auth(mock_request), f"Path '{path}' should require authentication")

    def test_fake_openapi_suffix_requires_auth(self):
        """Test that endpoints with openapi.json as suffix require auth."""
        test_paths = [
            "/api/rbac/v1/groups/openapi.json",
            "/api/v1/roles/openapi.json",
            "/something/openapi.json",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(is_no_auth(mock_request), f"Path '{path}' should require authentication")

    def test_fake_metrics_suffix_requires_auth(self):
        """Test that endpoints with metrics as suffix require auth."""
        test_paths = [
            "/api/rbac/v1/groups/metrics",
            "/api/v1/roles/metrics",
            "/something/metrics",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(is_no_auth(mock_request), f"Path '{path}' should require authentication")

    def test_fake_metrics_prefix_requires_auth(self):
        """Test that endpoints with metrics as prefix require auth."""
        test_paths = [
            "/metrics/something",
            "/metrics-data",
            "/metricspage",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(is_no_auth(mock_request), f"Path '{path}' should require authentication")

    def test_substring_injection_requires_auth(self):
        """Test that paths containing public endpoint names as substrings require auth."""
        test_paths = [
            "/api/status-check/",
            "/api/openapi.json.backup",
            "/api/metrics-collector/",
            "/prestatus",
            "/poststatus",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(is_no_auth(mock_request), f"Path '{path}' should require authentication")

    def test_path_traversal_attempts_require_auth(self):
        """Test that path traversal attempts require authentication."""
        test_paths = [
            "/api/rbac/v1/../status/",
            "/api/rbac/../v1/status/",
            "/../metrics",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(is_no_auth(mock_request), f"Path '{path}' should require authentication")

    def test_query_string_does_not_affect_public_endpoints(self):
        """Test that query strings on public endpoints don't affect no-auth status."""
        # Query strings should NOT be in the path attribute
        mock_request = Mock(path=reverse("v1_api:server-status"))
        self.assertTrue(is_no_auth(mock_request))

    def test_case_sensitivity(self):
        """Test that the function is case-sensitive for security."""
        test_paths = [
            "/METRICS",
            "/Metrics",
            "/api/rbac/v1/STATUS/",
            "/api/rbac/v1/OPENAPI.JSON",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                # These should require auth because exact match is case-sensitive
                self.assertFalse(is_no_auth(mock_request), f"Path '{path}' (uppercase) should require authentication")

    def test_trailing_slash_matters(self):
        """Test that trailing slashes are handled correctly."""
        # Status endpoint has trailing slash
        mock_request_with_slash = Mock(path="/api/rbac/v1/status/")
        self.assertTrue(is_no_auth(mock_request_with_slash))

        # Without trailing slash should NOT match (exact match)
        mock_request_no_slash = Mock(path="/api/rbac/v1/status")
        self.assertFalse(is_no_auth(mock_request_no_slash))

        # Metrics has no trailing slash
        mock_request_metrics = Mock(path="/metrics")
        self.assertTrue(is_no_auth(mock_request_metrics))

        # With trailing slash should NOT match
        mock_request_metrics_slash = Mock(path="/metrics/")
        self.assertFalse(is_no_auth(mock_request_metrics_slash))

    def test_double_slash_requires_auth(self):
        """Test that double slashes in path require authentication."""
        test_paths = [
            "/api/rbac//v1/status/",
            "//metrics",
            "/api//rbac/v1/openapi.json",
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(
                    is_no_auth(mock_request), f"Path '{path}' with double slashes should require authentication"
                )

    def test_url_encoded_paths_require_auth(self):
        """Test that URL-encoded variations of public paths require authentication."""
        test_paths = [
            "/api/rbac/v1%2Fstatus/",  # %2F is /
            "/%6Detrics",  # %6D is m
            "/api/rbac/v1/openapi%2Ejson",  # %2E is .
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(
                    is_no_auth(mock_request), f"Path '{path}' (URL encoded) should require authentication"
                )

    def test_unicode_variations_require_auth(self):
        """Test that unicode/homograph variations require authentication."""
        test_paths = [
            "/metrіcs",  # і is Cyrillic
            "/api/rbac/v1/stаtus/",  # а is Cyrillic
        ]
        for path in test_paths:
            with self.subTest(path=path):
                mock_request = Mock(path=path)
                self.assertFalse(
                    is_no_auth(mock_request), f"Path '{path}' (unicode variant) should require authentication"
                )

    def test_empty_path_requires_auth(self):
        """Test that empty path requires authentication."""
        mock_request = Mock(path="")
        self.assertFalse(is_no_auth(mock_request))

    def test_root_path_requires_auth(self):
        """Test that root path requires authentication."""
        mock_request = Mock(path="/")
        self.assertFalse(is_no_auth(mock_request))

    def test_exact_match_only(self):
        """Test that only exact matches bypass authentication."""
        # This is the comprehensive test that combines several checks
        public_paths = [
            reverse("v1_api:server-status"),
            reverse("v1_api:openapi"),
            "/metrics",
        ]

        for public_path in public_paths:
            with self.subTest(public_path=public_path):
                # Exact match should be no-auth
                mock_request = Mock(path=public_path)
                self.assertTrue(
                    is_no_auth(mock_request), f"Exact path '{public_path}' should not require authentication"
                )

                # Any variation should require auth
                variations = [
                    f"{public_path}/extra",
                    f"/prefix{public_path}",
                    f"{public_path}extra",
                    public_path.rstrip("/") if public_path.endswith("/") else f"{public_path}/",
                ]

                for variation in variations:
                    mock_request_var = Mock(path=variation)
                    self.assertFalse(
                        is_no_auth(mock_request_var),
                        f"Variation '{variation}' of public path should require authentication",
                    )
