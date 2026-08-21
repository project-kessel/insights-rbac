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
"""Tests for internal tenant org_config API."""

from unittest.mock import patch

from django.test.utils import override_settings
from rest_framework import status
from rest_framework.test import APIClient
from tests.identity_request import IdentityRequest


@override_settings(WORKSPACE_ORG_CREATION_LIMIT=120)
class TenantOrgConfigInternalTests(IdentityRequest):
    """Tests for GET/PATCH /_private/api/utils/tenant_org_config/<org_id>/."""

    def setUp(self):
        """Set up internal identity headers."""
        super().setUp()
        self.client = APIClient()
        ctx = self._create_request_context(self.customer_data, self.user_data, is_internal=True)
        self.internal_headers = ctx["request"].META
        self.url = f"/_private/api/utils/tenant_org_config/{self.tenant.org_id}/"
        self.tenant.org_config = {}
        self.tenant.save(update_fields=["org_config"])

    def tearDown(self):
        """Reset org_config so class-level tenant does not leak overrides."""
        self.tenant.org_config = {}
        self.tenant.save(update_fields=["org_config"])
        super().tearDown()

    def test_get_returns_global_default_when_empty(self):
        """GET returns empty org_config and the effective global limit."""
        response = self.client.get(self.url, **self.internal_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "org_id": self.tenant.org_id,
                "org_config": {},
                "workspace_creation_limit": 120,
            },
        )

    def test_get_returns_override(self):
        """GET returns stored org_config and the effective override."""
        self.tenant.org_config = {"workspace_creation_limit": 500}
        self.tenant.save(update_fields=["org_config"])

        response = self.client.get(self.url, **self.internal_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "org_id": self.tenant.org_id,
                "org_config": {"workspace_creation_limit": 500},
                "workspace_creation_limit": 500,
            },
        )

    def test_get_unknown_org_returns_404(self):
        """GET for a missing org_id returns 404."""
        response = self.client.get("/_private/api/utils/tenant_org_config/missing-org/", **self.internal_headers)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_patch_sets_limit(self):
        """PATCH stores workspace_creation_limit and returns the effective value."""
        response = self.client.patch(
            self.url, {"workspace_creation_limit": 500}, format="json", **self.internal_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "org_id": self.tenant.org_id,
                "org_config": {"workspace_creation_limit": 500},
                "workspace_creation_limit": 500,
            },
        )
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.org_config, {"workspace_creation_limit": 500})

    def test_patch_unsets_limit(self):
        """PATCH with null removes the override."""
        self.tenant.org_config = {"workspace_creation_limit": 500}
        self.tenant.save(update_fields=["org_config"])

        response = self.client.patch(
            self.url, {"workspace_creation_limit": None}, format="json", **self.internal_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "org_id": self.tenant.org_id,
                "org_config": {},
                "workspace_creation_limit": 120,
            },
        )
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.org_config, {})

    @patch("internal.views.TENANTS.delete_tenant")
    def test_patch_invalidates_tenant_cache(self, delete_tenant):
        """PATCH deletes the TenantCache entry for the org."""
        response = self.client.patch(
            self.url, {"workspace_creation_limit": 500}, format="json", **self.internal_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        delete_tenant.assert_called_once_with(self.tenant.org_id)

    def test_patch_rejects_unknown_keys(self):
        """PATCH with an unknown key returns 400 and does not write."""
        response = self.client.patch(
            self.url, {"workspace_hierarchy_depth_limit": 10}, format="json", **self.internal_headers
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unknown org_config keys", response.json()["error"])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.org_config, {})

    def test_patch_rejects_non_int(self):
        """PATCH with a non-integer limit returns 400."""
        response = self.client.patch(
            self.url, {"workspace_creation_limit": "lots"}, format="json", **self.internal_headers
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("workspace_creation_limit", response.json()["error"])

    def test_patch_rejects_empty_body(self):
        """PATCH with {} returns 400."""
        response = self.client.patch(self.url, {}, format="json", **self.internal_headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No org_config keys provided", response.json()["error"])

    def test_patch_rejects_invalid_json(self):
        """PATCH with a non-object body returns 400."""
        response = self.client.patch(self.url, [1, 2], format="json", **self.internal_headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["error"], "Request body must be a JSON object.")

    def test_patch_rejects_invalid_values(self):
        """PATCH rejects bool, zero, and other invalid workspace_creation_limit values."""
        for invalid in (True, 0):
            with self.subTest(value=invalid):
                response = self.client.patch(
                    self.url, {"workspace_creation_limit": invalid}, format="json", **self.internal_headers
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("workspace_creation_limit", response.json()["error"])
                self.tenant.refresh_from_db()
                self.assertEqual(self.tenant.org_config, {})

    def test_post_not_allowed(self):
        """POST is rejected."""
        response = self.client.post(
            self.url, {"workspace_creation_limit": 500}, format="json", **self.internal_headers
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(
            response.json(),
            {"errors": [{"detail": 'Invalid method, only "GET" and "PATCH" are allowed.', "status": "405"}]},
        )
