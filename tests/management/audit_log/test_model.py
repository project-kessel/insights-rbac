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
"""Test the Audit Logs Model."""

from unittest.mock import Mock

from management.models import AuditLog
from tests.identity_request import IdentityRequest


class AuditLogModelTests(IdentityRequest):
    """Test the Audit Log Model."""

    def setUp(self):
        """Set up the audit log model tests."""
        super().setUp()
        self.request = self.request_context["request"]
        self.request.user = Mock(username=self.user_data["username"])
        self.request._user = Mock(org_id=self.customer_data["org_id"])
        self.request.mcp_source = False

        self.audit_log = AuditLog.objects.create(
            principal_username="test_user",
            resource_type=AuditLog.ROLE,
            resource_id="1",
            description="Created a role asdf1234",
            action=AuditLog.CREATE,
            tenant_id="2",
        )

    def tearDown(self):
        """Tear down group model tests."""
        AuditLog.objects.all().delete()

    def test_audit_log_creation(self):
        """Test whether log was created through model."""
        self.assertEqual(self.audit_log.principal_username, "test_user")
        self.assertEqual(self.audit_log.resource_type, "role")
        self.assertEqual(self.audit_log.resource_id, "1")
        self.assertEqual(self.audit_log.description, "Created a role asdf1234")
        self.assertEqual(self.audit_log.action, "create")
        self.assertEqual(self.audit_log.tenant_id, "2")

    def test_audit_log_source_default_is_none(self):
        """Audit log entries default to source=None."""
        self.assertIsNone(self.audit_log.source)

    def test_audit_log_source_ai_assistant(self):
        """Audit log entries can be created with ai_assistant source."""
        entry = AuditLog.objects.create(
            principal_username="mcp_user",
            resource_type=AuditLog.GROUP,
            resource_id="10",
            description="Created group: Test",
            action=AuditLog.CREATE,
            source=AuditLog.SOURCE_AI_ASSISTANT,
            tenant_id="2",
        )
        self.assertEqual(entry.source, AuditLog.SOURCE_AI_ASSISTANT)

    def test_apply_source_sets_ai_assistant_when_mcp(self):
        """_apply_source sets source to ai_assistant when request has mcp_source=True."""
        request = Mock()
        request.mcp_source = True
        audit_log = AuditLog()
        audit_log._apply_source(request)
        self.assertEqual(audit_log.source, AuditLog.SOURCE_AI_ASSISTANT)

    def test_apply_source_does_not_set_when_no_mcp(self):
        """_apply_source leaves source as None when request has no mcp_source."""
        request = Mock(spec=[])
        audit_log = AuditLog()
        audit_log._apply_source(request)
        self.assertIsNone(audit_log.source)

    def test_log_create_from_object(self):
        """log_create_from_object creates an audit log entry using the object directly."""
        from management.group.model import Group

        group = Group.objects.create(
            name="Custom default access",
            tenant=self.tenant,
            platform_default=True,
            system=False,
        )

        audit_log = AuditLog()
        audit_log.log_create_from_object(self.request, AuditLog.GROUP, group)

        self.assertEqual(audit_log.principal_username, self.user_data["username"])
        self.assertEqual(audit_log.resource_type, AuditLog.GROUP)
        self.assertEqual(audit_log.resource_id, group.id)
        self.assertEqual(audit_log.resource_uuid, group.uuid)
        self.assertEqual(audit_log.description, "Created group: Custom default access")
        self.assertEqual(audit_log.action, AuditLog.CREATE)

        group.delete()
