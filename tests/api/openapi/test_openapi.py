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
"""Test the openapi API."""

import json
import os
from importlib import reload
from unittest.mock import mock_open, patch

from django.test.utils import override_settings
from django.urls import clear_url_caches, reverse
from django.urls.exceptions import NoReverseMatch
from rest_framework.test import APIClient

from rbac import urls
from tests.identity_request import IdentityRequest


class OpenAPIViewTest(IdentityRequest):
    """Tests the openapi view."""

    def test_openapi_endpoint_success(self):
        """Test the openapi endpoint returns 200 and valid JSON."""
        url = reverse("v1_api:openapi")
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

        # Verify response is valid JSON
        data = response.json()
        self.assertIsInstance(data, dict)

        # Verify it contains OpenAPI spec structure
        self.assertIn("openapi", data)
        self.assertIn("info", data)
        self.assertIn("paths", data)

    def test_openapi_endpoint_allows_anonymous(self):
        """Test the openapi endpoint allows anonymous access."""
        url = reverse("v1_api:openapi")
        client = APIClient()
        # Don't provide any authentication headers
        response = client.get(url)

        # Should still return 200 (AllowAny permission)
        self.assertEqual(response.status_code, 200)

    def test_openapi_spec_includes_itself(self):
        """Test that the openapi spec includes documentation for the /openapi.json endpoint."""
        url = reverse("v1_api:openapi")
        client = APIClient()
        response = client.get(url, **self.headers)

        data = response.json()
        paths = data.get("paths", {})

        # Verify that /openapi.json is documented in the spec
        self.assertIn("/openapi.json", paths)

        # Verify it has a GET method
        openapi_endpoint = paths["/openapi.json"]
        self.assertIn("get", openapi_endpoint)

        # Verify basic metadata
        get_method = openapi_endpoint["get"]
        self.assertIn("summary", get_method)
        self.assertIn("operationId", get_method)
        self.assertEqual(get_method["operationId"], "getOpenAPISpec")

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_openapi_file_not_found(self, mock_file):
        """Test that missing openapi.json file returns proper error response."""
        url = reverse("v1_api:openapi")
        client = APIClient()
        response = client.get(url, **self.headers)

        # Should return 500 error
        self.assertEqual(response.status_code, 500)

        # Verify error response structure
        data = response.json()
        self.assertIn("errors", data)
        self.assertEqual(len(data["errors"]), 1)

        error = data["errors"][0]
        self.assertIn("OpenAPI specification file not found", error["detail"])
        self.assertEqual(error["status"], "500")

    @patch("builtins.open", new_callable=mock_open, read_data="invalid json {{{")
    def test_openapi_invalid_json(self, mock_file):
        """Test that malformed openapi.json file returns proper error response."""
        url = reverse("v1_api:openapi")
        client = APIClient()
        response = client.get(url, **self.headers)

        # Should return 500 error
        self.assertEqual(response.status_code, 500)

        # Verify error response structure
        data = response.json()
        self.assertIn("errors", data)
        self.assertEqual(len(data["errors"]), 1)

        error = data["errors"][0]
        self.assertIn("invalid JSON", error["detail"])
        self.assertEqual(error["status"], "500")


@override_settings(V2_APIS_ENABLED=True)
class OpenAPIV2ViewTest(IdentityRequest):
    """Tests the V2 openapi view."""

    @classmethod
    def setUpClass(cls):
        """Set up the test class."""
        super().setUpClass()
        # Reload URLs to register v2_api namespace when V2_APIS_ENABLED=True
        reload(urls)
        clear_url_caches()

    @classmethod
    def tearDownClass(cls):
        """Tear down the test class."""
        super().tearDownClass()
        # Reload URLs to restore original state
        reload(urls)
        clear_url_caches()

    def test_openapi_v2_endpoint_success(self):
        """Test the V2 openapi endpoint returns 200 and valid JSON."""
        url = reverse("v2_api:openapi")
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

        # Verify response is valid JSON
        data = response.json()
        self.assertIsInstance(data, dict)

        # Verify it contains OpenAPI spec structure
        self.assertIn("openapi", data)
        self.assertIn("info", data)
        self.assertIn("paths", data)

    def test_openapi_v2_endpoint_allows_anonymous(self):
        """Test the V2 openapi endpoint allows anonymous access."""
        url = reverse("v2_api:openapi")
        client = APIClient()
        # Don't provide any authentication headers
        response = client.get(url)

        # Should still return 200 (AllowAny permission)
        self.assertEqual(response.status_code, 200)

    def test_openapi_v2_spec_includes_workspaces(self):
        """Test that the V2 openapi spec includes workspace endpoints."""
        url = reverse("v2_api:openapi")
        client = APIClient()
        response = client.get(url, **self.headers)

        data = response.json()
        paths = data.get("paths", {})

        # Verify workspace endpoints are documented
        workspace_paths = [path for path in paths.keys() if "workspace" in path.lower()]
        self.assertGreater(len(workspace_paths), 0, "V2 spec should include workspace endpoints")

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_openapi_v2_file_not_found(self, mock_file):
        """Test that missing V2 openapi.json file returns proper error response."""
        url = reverse("v2_api:openapi")
        client = APIClient()
        response = client.get(url, **self.headers)

        # Should return 500 error
        self.assertEqual(response.status_code, 500)

        # Verify error response structure
        data = response.json()
        self.assertIn("errors", data)
        self.assertEqual(len(data["errors"]), 1)

        error = data["errors"][0]
        self.assertIn("V2 OpenAPI specification file not found", error["detail"])
        self.assertEqual(error["status"], "500")

    @patch("builtins.open", new_callable=mock_open, read_data="invalid json {{{")
    def test_openapi_v2_invalid_json(self, mock_file):
        """Test that malformed V2 openapi.json file returns proper error response."""
        url = reverse("v2_api:openapi")
        client = APIClient()
        response = client.get(url, **self.headers)

        # Should return 500 error
        self.assertEqual(response.status_code, 500)

        # Verify error response structure
        data = response.json()
        self.assertIn("errors", data)
        self.assertEqual(len(data["errors"]), 1)

        error = data["errors"][0]
        self.assertIn("invalid JSON", error["detail"])
        self.assertEqual(error["status"], "500")


@override_settings(V2_APIS_ENABLED=False)
class OpenAPIV2DisabledTest(IdentityRequest):
    """Test V2 OpenAPI endpoint when V2 APIs are disabled."""

    def setUp(self):
        """Set up the test."""
        reload(urls)
        clear_url_caches()
        super().setUp()

    def test_openapi_v2_endpoint_not_available_when_disabled(self):
        """Test that V2 openapi endpoint is not available when V2_APIS_ENABLED is False."""
        # When V2_APIS_ENABLED is False, the v2_api namespace doesn't exist
        with self.assertRaises(NoReverseMatch) as context:
            reverse("v2_api:openapi")

        # Verify the error message mentions the namespace
        self.assertIn("v2_api", str(context.exception))
        self.assertIn("not a registered namespace", str(context.exception))
