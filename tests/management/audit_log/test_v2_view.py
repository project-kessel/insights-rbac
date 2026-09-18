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
"""Test the Audit Log V2 API."""

import uuid
from datetime import timedelta
from importlib import reload
from unittest.mock import patch
from urllib.parse import urlencode

from django.test.utils import override_settings
from django.urls import clear_url_caches
from django.utils import timezone
from management.models import AuditLog
from rest_framework import status
from rest_framework.test import APIClient
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test

from api.models import Tenant
from rbac import urls

V2_URL = "/api/rbac/v2/auditlogs/"

PRINCIPAL_ID_PATCH = "management.permissions.auditlog_v2_access.get_kessel_principal_id"
ACCESS_CHECK_PATCH = "management.permissions.auditlog_v2_access.WorkspaceInventoryAccessChecker.check_resource_access"


@override_settings(V2_APIS_ENABLED=True)
class AuditLogV2ViewTests(IdentityRequest):
    """Test the Audit Log V2 list endpoint."""

    def setUp(self):
        """Set up the audit log v2 tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_tenant_for_v2_test(self.tenant)

        self.client = APIClient()

        self.enterContext(patch(PRINCIPAL_ID_PATCH, return_value="localhost/test-user-id"))
        self.mock_check_access = self.enterContext(patch(ACCESS_CHECK_PATCH, return_value=True))

        self.now = timezone.now()
        self.workspace_uuid = uuid.uuid4()

        self.audit_log1 = AuditLog.objects.create(
            principal_username="alice",
            resource_type=AuditLog.ROLE,
            resource_id=1,
            description="Created role test1",
            action=AuditLog.CREATE,
            tenant=self.tenant,
            created=self.now - timedelta(days=3),
        )
        self.audit_log2 = AuditLog.objects.create(
            principal_username="bob",
            resource_type=AuditLog.GROUP,
            resource_id=2,
            description="Deleted group test2",
            action=AuditLog.DELETE,
            tenant=self.tenant,
            created=self.now - timedelta(days=2),
        )
        self.audit_log3 = AuditLog.objects.create(
            principal_username="alice",
            resource_type=AuditLog.WORKSPACE,
            resource_uuid=self.workspace_uuid,
            description="Edited workspace test3",
            action=AuditLog.EDIT,
            tenant=self.tenant,
            created=self.now - timedelta(days=1),
            source=AuditLog.SOURCE_AI_ASSISTANT,
        )
        self.audit_log4 = AuditLog.objects.create(
            principal_username="admin",
            resource_type=AuditLog.USER,
            resource_id=4,
            description="Added user to group",
            action=AuditLog.ADD,
            tenant=self.tenant,
            created=self.now,
        )

    def tearDown(self):
        """Tear down audit log v2 tests."""
        AuditLog.objects.all().delete()
        super().tearDown()

    def _usernames(self, response):
        return [entry["principal_username"] for entry in response.data["data"]]

    # ------------------------------------------------------------------ list

    def test_list_returns_tenant_entries_newest_first(self):
        """List returns the tenant's entries ordered by created descending."""
        response = self.client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 4)
        self.assertEqual(self._usernames(response), ["admin", "alice", "bob", "alice"])

    def test_list_response_shape(self):
        """Entries expose the v2 field set and omit the integer primary key."""
        response = self.client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entry = response.data["data"][0]
        self.assertEqual(
            set(entry.keys()),
            {"created", "principal_username", "description", "resource_type", "resource_id", "action", "source"},
        )
        self.assertNotIn("sequence", entry)
        self.assertEqual(entry["principal_username"], "admin")
        self.assertEqual(entry["action"], "add")
        self.assertEqual(entry["resource_type"], "user")
        self.assertIsNone(entry["resource_id"])
        self.assertIsNone(entry["source"])

    def test_list_exposes_resource_uuid_as_resource_id(self):
        """resource_id is populated from the entry's resource UUID."""
        response = self.client.get(f"{V2_URL}?resource_type=workspace", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["resource_id"], str(self.workspace_uuid))
        self.assertEqual(response.data["data"][0]["source"], AuditLog.SOURCE_AI_ASSISTANT)

    def test_list_is_tenant_scoped(self):
        """Entries belonging to another tenant are never returned."""
        other_tenant = Tenant.objects.create(tenant_name="other", org_id="99999")
        AuditLog.objects.create(
            principal_username="intruder",
            resource_type=AuditLog.ROLE,
            resource_id=99,
            description="Created role in another tenant",
            action=AuditLog.CREATE,
            tenant=other_tenant,
            created=self.now,
        )

        try:
            response = self.client.get(V2_URL, **self.headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertNotIn("intruder", self._usernames(response))
            self.assertEqual(len(response.data["data"]), 4)
        finally:
            AuditLog.objects.filter(tenant=other_tenant).delete()
            other_tenant.delete()

    def test_list_rejects_write_methods(self):
        """The endpoint is read-only."""
        for method in (self.client.post, self.client.put, self.client.patch, self.client.delete):
            with self.subTest(method=method.__name__):
                response = method(V2_URL, **self.headers)
                self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    # ------------------------------------------------------------ pagination

    def test_pagination_default_shape(self):
        """The list response uses the v2 cursor pagination envelope."""
        response = self.client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data.keys()), {"meta", "links", "data"})
        self.assertEqual(response.data["meta"]["limit"], 10)
        self.assertEqual(set(response.data["links"].keys()), {"next", "previous"})
        self.assertIsNone(response.data["links"]["next"])
        self.assertIsNone(response.data["links"]["previous"])
        self.assertNotIn("count", response.data["meta"])

    def test_pagination_walks_pages_with_cursor(self):
        """Following the next cursor returns the remaining entries without overlap."""
        response = self.client.get(f"{V2_URL}?limit=2", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)
        self.assertEqual(self._usernames(response), ["admin", "alice"])

        next_link = response.data["links"]["next"]
        self.assertIsNotNone(next_link)
        self.assertIn("cursor=", next_link)

        page2 = self.client.get(next_link, **self.headers)
        self.assertEqual(page2.status_code, status.HTTP_200_OK)
        self.assertEqual(self._usernames(page2), ["bob", "alice"])
        self.assertIsNone(page2.data["links"]["next"])
        self.assertIsNotNone(page2.data["links"]["previous"])

        page1_again = self.client.get(page2.data["links"]["previous"], **self.headers)
        self.assertEqual(page1_again.status_code, status.HTTP_200_OK)
        self.assertEqual(self._usernames(page1_again), ["admin", "alice"])

    def test_pagination_limit_minus_one_returns_all(self):
        """limit=-1 disables pagination."""
        response = self.client.get(f"{V2_URL}?limit=-1", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 4)
        self.assertEqual(response.data["meta"]["limit"], 4)
        self.assertIsNone(response.data["links"]["next"])

    def test_order_by_created_ascending(self):
        """order_by=created returns the oldest entry first."""
        response = self.client.get(f"{V2_URL}?order_by=created", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._usernames(response), ["alice", "bob", "alice", "admin"])

    def test_order_by_invalid_field_returns_400(self):
        """An unsupported order_by value is rejected."""
        response = self.client.get(f"{V2_URL}?order_by=principal_username", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response["Content-Type"], "application/problem+json")

    # --------------------------------------------------------------- filters

    def test_filter_by_action(self):
        """Filtering by action returns only matching entries."""
        response = self.client.get(f"{V2_URL}?action=create", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["description"], "Created role test1")

    def test_filter_by_invalid_action_returns_400(self):
        """An unknown action value is rejected."""
        response = self.client.get(f"{V2_URL}?action=explode", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response["Content-Type"], "application/problem+json")

    def test_filter_by_resource_type(self):
        """Filtering by resource_type returns only matching entries."""
        response = self.client.get(f"{V2_URL}?resource_type=role", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["resource_type"], "role")

    def test_filter_by_invalid_resource_type_returns_400(self):
        """An unknown resource_type value is rejected."""
        response = self.client.get(f"{V2_URL}?resource_type=spaceship", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_by_resource_id(self):
        """Filtering by resource_id matches the entry's resource UUID."""
        response = self.client.get(f"{V2_URL}?resource_id={self.workspace_uuid}", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["description"], "Edited workspace test3")

    def test_filter_by_malformed_resource_id_returns_400(self):
        """A non-UUID resource_id is rejected."""
        response = self.client.get(f"{V2_URL}?resource_id=not-a-uuid", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_by_principal_username_substring(self):
        """principal_username uses a case-insensitive substring match by default."""
        response = self.client.get(f"{V2_URL}?principal_username=LIC", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._usernames(response), ["alice", "alice"])

    def test_filter_by_principal_username_glob(self):
        """principal_username supports * glob patterns."""
        response = self.client.get(f"{V2_URL}?principal_username=a*", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(sorted(set(self._usernames(response))), ["admin", "alice"])

    def test_filter_by_date_range(self):
        """created_after and created_before bound the results inclusively."""
        params = urlencode(
            {
                "created_after": (self.now - timedelta(days=2, hours=1)).isoformat(),
                "created_before": (self.now - timedelta(hours=12)).isoformat(),
            }
        )

        response = self.client.get(f"{V2_URL}?{params}", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._usernames(response), ["alice", "bob"])

    def test_filter_by_created_after_only(self):
        """created_after alone excludes older entries."""
        params = urlencode({"created_after": (self.now - timedelta(days=1, hours=1)).isoformat()})

        response = self.client.get(f"{V2_URL}?{params}", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._usernames(response), ["admin", "alice"])

    def test_filter_inverted_date_range_returns_400(self):
        """created_after later than created_before is rejected."""
        params = urlencode(
            {
                "created_after": self.now.isoformat(),
                "created_before": (self.now - timedelta(days=2)).isoformat(),
            }
        )

        response = self.client.get(f"{V2_URL}?{params}", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_by_malformed_date_returns_400(self):
        """A non-ISO created_after value is rejected."""
        response = self.client.get(f"{V2_URL}?created_after=yesterday", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filters_combine(self):
        """Multiple filters are ANDed together."""
        response = self.client.get(f"{V2_URL}?principal_username=alice&action=edit", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["description"], "Edited workspace test3")

    # ---------------------------------------------------------------- fields

    def test_fields_parameter_masks_response(self):
        """The fields parameter restricts the returned keys."""
        response = self.client.get(f"{V2_URL}?fields=action,created", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data["data"][0].keys()), {"action", "created"})

    def test_fields_parameter_rejects_unknown_field(self):
        """An unknown field name is rejected."""
        response = self.client.get(f"{V2_URL}?fields=action,not_a_field", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(V2_APIS_ENABLED=True)
class AuditLogV2AuthorizationTests(IdentityRequest):
    """Test Kessel-backed authorization for the Audit Log V2 endpoint."""

    def setUp(self):
        """Set up the authorization tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_tenant_for_v2_test(self.tenant)
        self.client = APIClient()

        AuditLog.objects.create(
            principal_username="alice",
            resource_type=AuditLog.ROLE,
            resource_id=1,
            description="Created role test1",
            action=AuditLog.CREATE,
            tenant=self.tenant,
            created=timezone.now(),
        )

    def tearDown(self):
        """Tear down the authorization tests."""
        AuditLog.objects.all().delete()
        super().tearDown()

    def test_allowed_when_kessel_grants_relation(self):
        """Access is granted when Kessel reports the audit log read relation."""
        with (
            patch(PRINCIPAL_ID_PATCH, return_value="localhost/test-user-id"),
            patch(ACCESS_CHECK_PATCH, return_value=True) as mock_check,
        ):
            response = self.client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_check.assert_called_once()
        self.assertEqual(mock_check.call_args.kwargs["relation"], "rbac_audit_log_read")
        self.assertEqual(mock_check.call_args.kwargs["resource_type"], "tenant")

    def test_forbidden_when_kessel_denies_relation(self):
        """Access is denied with RFC 7807 problem details when Kessel says no."""
        with (
            patch(PRINCIPAL_ID_PATCH, return_value="localhost/test-user-id"),
            patch(ACCESS_CHECK_PATCH, return_value=False),
        ):
            response = self.client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response["Content-Type"], "application/problem+json")
        body = response.json()
        self.assertEqual(body["status"], 403)
        self.assertEqual(body["type"], "http://project-kessel.org/problems/insufficient-permission")
        self.assertEqual(body["title"], "You do not have permission to perform this action.")

    def test_forbidden_when_principal_id_unresolvable(self):
        """Access is denied when the Kessel principal ID cannot be determined."""
        with patch(PRINCIPAL_ID_PATCH, return_value=None), patch(ACCESS_CHECK_PATCH, return_value=True) as mock_check:
            response = self.client.get(V2_URL, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_check.assert_not_called()

    def test_org_admin_does_not_bypass_kessel(self):
        """Org admins get no bypass: a Kessel denial still results in a 403."""
        admin_request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=True)
        admin_headers = admin_request_context["request"].META

        with (
            patch(PRINCIPAL_ID_PATCH, return_value="localhost/test-user-id"),
            patch(ACCESS_CHECK_PATCH, return_value=False),
        ):
            response = self.client.get(V2_URL, **admin_headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
