#
# Copyright 2024 Red Hat, Inc.
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
"""Test ordering functionality for workspace list endpoint."""

from importlib import reload
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings
from django.urls import clear_url_caches
from rest_framework import status
from rest_framework.test import APIClient

from management.workspace.model import Workspace
from rbac import urls
from tests.identity_request import IdentityRequest


@override_settings(V2_APIS_ENABLED=True)
@patch(
    "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
    return_value=False,
)
class WorkspaceOrderingTestCase(IdentityRequest, TestCase):
    """Test workspace ordering with order_by parameter using '-' prefix for descending order."""

    def setUp(self):
        """Set up test workspaces with different attributes."""
        super().setUp()

        # Reload URLs to register V2 API endpoints
        reload(urls)
        clear_url_caches()

        # Create workspace hierarchy: ROOT -> DEFAULT -> STANDARD
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=self.tenant,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            tenant=self.tenant,
            parent=self.root_workspace,
        )

        # Create workspaces with different names and timestamps
        self.workspace_alpha = Workspace.objects.create(
            name="Alpha",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
            parent=self.default_workspace,
        )
        self.workspace_beta = Workspace.objects.create(
            name="Beta",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
            parent=self.default_workspace,
        )
        self.workspace_gamma = Workspace.objects.create(
            name="Gamma",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
            parent=self.default_workspace,
        )

    def _get_workspaces(self, query_params):
        """Helper method to fetch workspaces with query parameters."""
        client = APIClient()
        # Default to type=standard to filter out ROOT and DEFAULT infrastructure workspaces
        # unless type is already specified in query_params
        if "type=" not in query_params:
            separator = "&" if query_params else ""
            query_params = f"{query_params}{separator}type=standard"
        url = f"/api/rbac/v2/workspaces/?{query_params}"
        return client.get(url, **self.headers)

    def _get_workspaces_data(self, query_params, expected_status=status.HTTP_200_OK):
        """Helper to fetch workspaces, assert status, and return data."""
        response = self._get_workspaces(query_params)
        self.assertEqual(response.status_code, expected_status)
        return response.json()["data"]

    def _assert_field_sorted(self, data, field, reverse=False):
        """Helper to assert that a field is sorted in data."""
        values = [item[field] for item in data]
        expected = sorted(values, reverse=reverse)
        self.assertEqual(values, expected)

    def test_ordering_by_field(self, mock_flag):
        """Test ordering by all fields in both directions using '-' prefix."""
        test_cases = [
            {"query": "order_by=name", "field": "name", "reverse": False},
            {"query": "order_by=-name", "field": "name", "reverse": True},
            {"query": "order_by=created", "field": "created", "reverse": False, "first": "Alpha"},
            {"query": "order_by=-created", "field": "created", "reverse": True, "first": "Gamma"},
            {"query": "order_by=modified", "field": "modified", "reverse": False, "first": "Alpha"},
            {"query": "order_by=-modified", "field": "modified", "reverse": True, "first": "Gamma"},
            {"query": "order_by=type", "field": "type", "reverse": False},
            {"query": "order_by=-type", "field": "type", "reverse": True},
        ]

        for test_case in test_cases:
            direction = "desc" if test_case["reverse"] else "asc"
            with self.subTest(field=test_case["field"], direction=direction):
                data = self._get_workspaces_data(test_case["query"])
                # For timestamp fields, check first item name instead of full sort
                if "first" in test_case:
                    self.assertEqual(data[0]["name"], test_case["first"])
                else:
                    self._assert_field_sorted(data, test_case["field"], reverse=test_case["reverse"])

    def test_default_ordering(self, mock_flag):
        """Test default ordering behavior (no order_by or empty order_by)."""
        test_cases = [
            {"query": "", "description": "no order_by parameter"},
            {"query": "order_by=", "description": "empty order_by parameter"},
        ]

        for test_case in test_cases:
            with self.subTest(description=test_case["description"]):
                data = self._get_workspaces_data(test_case["query"])
                # Should fall back to default ordering (name ascending)
                self._assert_field_sorted(data, "name")

    def test_multiple_field_ordering(self, mock_flag):
        """Test ordering by multiple fields."""
        # Create additional workspaces with same type to test multi-field ordering
        Workspace.objects.create(
            name="Zeta",  # Different name but same type as existing workspaces
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
        )

        data = self._get_workspaces_data("order_by=type,name")

        # Verify primary ordering: types should be sorted
        self._assert_field_sorted(data, "type")

        # Verify secondary ordering: names should be sorted within the STANDARD type group
        standard_ws = [w["name"] for w in data if w["type"] == "standard"]
        self.assertEqual(standard_ws, sorted(standard_ws))

    def test_invalid_ordering_fields(self, mock_flag):
        """Test that invalid order_by fields return 400 Bad Request."""
        test_cases = [
            {"query": "order_by=invalid_field", "invalid": ["invalid_field"]},
            {"query": "order_by=id", "invalid": ["id"]},
            {"query": "order_by=-invalid", "invalid": ["invalid"]},
            {"query": "order_by=name;DROP TABLE", "invalid": ["name;DROP TABLE"]},
            {"query": "order_by=name,invalid,type", "invalid": ["invalid"]},
        ]

        for test_case in test_cases:
            with self.subTest(query=test_case["query"]):
                response = self._get_workspaces(test_case["query"])
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                data = response.json()
                self.assertIn("errors", data)
                for invalid_field in test_case["invalid"]:
                    self.assertIn(invalid_field, str(data))

    def test_ordering_integration(self, mock_flag):
        """Test ordering with filters, pagination, and other query parameters."""
        test_cases = [
            {
                "query": "type=standard&order_by=-name",
                "description": "with type filter",
                "validator": lambda self, data: (
                    all(w["type"] == "standard" for w in data),
                    self._assert_field_sorted(data, "name", reverse=True),
                )[1],
            },
            {
                "query": "order_by=name&limit=2&offset=0",
                "description": "with pagination",
                "validator": lambda self, data: (
                    self.assertEqual(len(data), 2),
                    self.assertEqual(data[0]["name"], "Alpha"),
                    self.assertEqual(data[1]["name"], "Beta"),
                )[2],
            },
            {
                "query": "name=Alpha&order_by=created",
                "description": "with name filter",
                "validator": lambda self, data: self.assertEqual(data[0]["name"], "Alpha") if len(data) > 0 else None,
            },
        ]

        for test_case in test_cases:
            with self.subTest(description=test_case["description"]):
                data = self._get_workspaces_data(test_case["query"])
                test_case["validator"](self, data)

    def test_timestamp_field_ordering(self, mock_flag):
        """Test ordering on timestamp fields checks both boundaries."""
        # Additional validation for timestamp fields - check both first and last items
        data = self._get_workspaces_data("order_by=-created")
        # Most recently created should be first (Gamma)
        self.assertEqual(data[0]["name"], "Gamma")
        # Oldest created should be last (Alpha)
        self.assertEqual(data[-1]["name"], "Alpha")

    def test_malformed_ordering_parameters_invalid(self, mock_flag):
        """Test that malformed ordering parameters return 400 Bad Request."""
        test_cases = [
            {"query": "order_by=--name", "description": "double minus prefix"},
            {"query": "order_by=name--", "description": "trailing dashes"},
            {"query": "order_by=NAME", "description": "uppercase field name"},
            {"query": "order_by=-NAME", "description": "uppercase field with prefix"},
            {"query": "order_by=../../../etc/passwd", "description": "path traversal attempt"},
        ]

        for test_case in test_cases:
            with self.subTest(description=test_case["description"]):
                response = self._get_workspaces(test_case["query"])
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                data = response.json()
                self.assertIn("errors", data)

    def test_malformed_ordering_parameters_valid(self, mock_flag):
        """Test that whitespace-trimmed and duplicate params are handled."""
        test_cases = [
            {"query": "order_by= name", "field": "name", "description": "leading space (trimmed to 'name')"},
            {"query": "order_by=name ", "field": "name", "description": "trailing space (trimmed to 'name')"},
            {
                "query": "order_by=name&order_by=type",
                "field": "type",
                "description": "duplicate param (last value wins)",
            },
        ]

        for test_case in test_cases:
            with self.subTest(description=test_case["description"]):
                response = self._get_workspaces(test_case["query"])
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                data = response.json()["data"]
                self.assertIsNotNone(data)
                self._assert_field_sorted(data, test_case["field"])
