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
"""Test the Audit Logs View."""

from datetime import timedelta
from django.utils import timezone
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from management.models import AuditLog
from tests.identity_request import IdentityRequest


class AuditLogViewTests(IdentityRequest):
    """Test the Audit Log View."""

    def setUp(self):
        """Set up the audit log view tests."""
        super().setUp()
        self.client = APIClient()

        # Create test audit logs with different attributes
        now = timezone.now()
        self.audit_log1 = AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.ROLE,
            resource_id=1,
            description="Created role test1",
            action=AuditLog.CREATE,
            tenant=self.tenant,
            created=now - timedelta(days=3),
        )
        self.audit_log2 = AuditLog.objects.create(
            principal_username="user2",
            resource_type=AuditLog.GROUP,
            resource_id=2,
            description="Deleted group test2",
            action=AuditLog.DELETE,
            tenant=self.tenant,
            created=now - timedelta(days=2),
        )
        self.audit_log3 = AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.ROLE,
            resource_id=3,
            description="Edited role test3",
            action=AuditLog.EDIT,
            tenant=self.tenant,
            created=now - timedelta(days=1),
        )
        self.audit_log4 = AuditLog.objects.create(
            principal_username="admin",
            resource_type=AuditLog.USER,
            resource_id=4,
            description="Added user to group",
            action=AuditLog.ADD,
            tenant=self.tenant,
            created=now,
        )

    def tearDown(self):
        """Tear down audit log view tests."""
        AuditLog.objects.all().delete()

    def test_list_audit_logs(self):
        """Test listing audit logs with default ordering (newest first)."""
        url = reverse("v1_management:auditlog-list")
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)
        # Verify default ordering is by created date descending (newest first)
        self.assertEqual(response.data.get("data")[0]["principal_username"], "admin")
        self.assertEqual(response.data.get("data")[0]["action"], "add")
        # Oldest should be last
        self.assertEqual(response.data.get("data")[3]["principal_username"], "user1")
        self.assertEqual(response.data.get("data")[3]["action"], "create")

    def test_filter_by_principal_username_exact(self):
        """Test filtering audit logs by principal username with exact match."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?principal_username=user1&name_match=exact"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)
        usernames = [log["principal_username"] for log in response.data.get("data")]
        self.assertTrue(all(username == "user1" for username in usernames))

    def test_filter_by_principal_username_partial(self):
        """Test filtering audit logs by principal username with partial match."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?principal_username=user"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 3)
        usernames = [log["principal_username"] for log in response.data.get("data")]
        self.assertTrue(all("user" in username.lower() for username in usernames))

    def test_filter_by_resource_type_single(self):
        """Test filtering audit logs by single resource type."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?resource_type=role"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)
        resource_types = [log["resource_type"] for log in response.data.get("data")]
        self.assertTrue(all(rt == "role" for rt in resource_types))

    def test_filter_by_resource_type_multiple(self):
        """Test filtering audit logs by multiple resource types."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?resource_type=role&resource_type=group"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 3)
        resource_types = [log["resource_type"] for log in response.data.get("data")]
        self.assertTrue(all(rt in ["role", "group"] for rt in resource_types))

    def test_filter_by_action_single(self):
        """Test filtering audit logs by single action."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?action=create"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        self.assertEqual(response.data.get("data")[0]["action"], "create")

    def test_filter_by_action_multiple(self):
        """Test filtering audit logs by multiple actions."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?action=create&action=delete"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)
        actions = [log["action"] for log in response.data.get("data")]
        self.assertTrue(all(action in ["create", "delete"] for action in actions))

    def test_filter_combined(self):
        """Test filtering audit logs with multiple filters."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?principal_username=user1&resource_type=role"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)
        for log in response.data.get("data"):
            self.assertEqual(log["principal_username"], "user1")
            self.assertEqual(log["resource_type"], "role")

    def test_ordering_by_created_desc(self):
        """Test ordering audit logs by created date descending (default)."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?order_by=-created"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)
        # Most recent should be first
        self.assertEqual(response.data.get("data")[0]["principal_username"], "admin")

    def test_ordering_by_created_asc(self):
        """Test ordering audit logs by created date ascending."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?order_by=created"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)
        # Oldest should be first
        self.assertEqual(response.data.get("data")[0]["principal_username"], "user1")
        self.assertEqual(response.data.get("data")[0]["action"], "create")

    def test_ordering_by_principal_username_asc(self):
        """Test ordering audit logs by principal username ascending."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?order_by=principal_username"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)
        usernames = [log["principal_username"] for log in response.data.get("data")]
        self.assertEqual(usernames, sorted(usernames))

    def test_ordering_by_principal_username_desc(self):
        """Test ordering audit logs by principal username descending."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?order_by=-principal_username"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)
        usernames = [log["principal_username"] for log in response.data.get("data")]
        self.assertEqual(usernames, sorted(usernames, reverse=True))

    def test_ordering_by_resource_type(self):
        """Test ordering audit logs by resource type."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?order_by=resource_type"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)
        resource_types = [log["resource_type"] for log in response.data.get("data")]
        self.assertEqual(resource_types, sorted(resource_types))

    def test_ordering_by_action(self):
        """Test ordering audit logs by action."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?order_by=action"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)
        actions = [log["action"] for log in response.data.get("data")]
        self.assertEqual(actions, sorted(actions))

    def test_pagination(self):
        """Test pagination of audit logs."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?limit=2&offset=0"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 4)
        self.assertEqual(len(response.data.get("data")), 2)

    def _format_dt(self, dt):
        """Format datetime as ISO 8601 with Z suffix for use in query parameters."""
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_filter_by_created_after(self):
        """Test filtering audit logs created after a given timestamp."""
        url = reverse("v1_management:auditlog-list")
        # audit_log3 is 1 day ago, audit_log4 is now — both should match
        cutoff = self._format_dt(timezone.now() - timedelta(days=1, seconds=1))
        url = f"{url}?created_after={cutoff}"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)

    def test_filter_by_created_before(self):
        """Test filtering audit logs created before a given timestamp."""
        url = reverse("v1_management:auditlog-list")
        # audit_log1 is 3 days ago, audit_log2 is 2 days ago — both should match
        cutoff = self._format_dt(timezone.now() - timedelta(days=1, seconds=1))
        url = f"{url}?created_before={cutoff}"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)

    def test_filter_by_date_range(self):
        """Test filtering audit logs within a date range (created_after + created_before)."""
        url = reverse("v1_management:auditlog-list")
        # Only audit_log2 (2 days ago) and audit_log3 (1 day ago) should match
        after = self._format_dt(timezone.now() - timedelta(days=2, seconds=1))
        before = self._format_dt(timezone.now() - timedelta(seconds=1))
        url = f"{url}?created_after={after}&created_before={before}"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)
        usernames = {log["principal_username"] for log in response.data.get("data")}
        self.assertEqual(usernames, {"user2", "user1"})

    def test_filter_created_after_with_other_filters(self):
        """Test combining created_after with resource_type filter."""
        url = reverse("v1_management:auditlog-list")
        cutoff = self._format_dt(timezone.now() - timedelta(days=1, seconds=1))
        url = f"{url}?created_after={cutoff}&resource_type=role"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 1)
        self.assertEqual(response.data.get("data")[0]["action"], "edit")

    def test_filter_created_after_no_results(self):
        """Test created_after filter that excludes all entries."""
        url = reverse("v1_management:auditlog-list")
        future = self._format_dt(timezone.now() + timedelta(days=1))
        url = f"{url}?created_after={future}"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 0)

    def test_filter_and_ordering_combined(self):
        """Test combining filtering and ordering."""
        url = reverse("v1_management:auditlog-list")
        url = f"{url}?resource_type=role&order_by=-created"
        client = APIClient()
        response = client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("meta").get("count"), 2)
        # Should have role logs ordered by created desc
        self.assertEqual(response.data.get("data")[0]["action"], "edit")
        self.assertEqual(response.data.get("data")[1]["action"], "create")
