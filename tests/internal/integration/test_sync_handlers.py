#
# Copyright 2020 Red Hat, Inc.
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
"""Test the caching system."""

from unittest import skipIf
from unittest.mock import patch
from rbac.settings import KAFKA_ENABLED, MOCK_KAFKA

from django.test import TestCase

from management.models import Access, Group, Permission, Policy, Principal, ResourceDefinition, Role
from api.models import Tenant


@skipIf(not (KAFKA_ENABLED or MOCK_KAFKA), "Kafka is disabled.")
class ExternalSyncTest(TestCase):
    @classmethod
    def setUpClass(self):
        """Set up the tenant."""
        super().setUpClass()
        self.tenant = Tenant.objects.create(tenant_name="acct12345", org_id="12345")
        self.tenant.ready = True
        self.tenant.save()

    def setUp(self):
        """Set up Sync tests."""
        super().setUp()
        self.principal_a = Principal.objects.create(username="principal_a", tenant=self.tenant)
        self.principal_b = Principal.objects.create(username="principal_b", tenant=self.tenant)
        self.group_a = Group.objects.create(name="group_a", platform_default=True, tenant=self.tenant)
        self.group_b = Group.objects.create(name="group_b", tenant=self.tenant)
        self.policy_a = Policy.objects.create(name="policy_a", tenant=self.tenant)
        self.policy_b = Policy.objects.create(name="policy_b", tenant=self.tenant)
        self.role_a = Role.objects.create(name="role_a", tenant=self.tenant)
        self.role_b = Role.objects.create(name="role_b", tenant=self.tenant)

    @classmethod
    def tearDownClass(self):
        self.tenant.delete()
        super().tearDownClass()

    @patch("internal.integration.sync_handlers.send_sync_message")
    def test_group_sync_add_remove_signals(self, sync):
        """Test signals attached to Groups"""
        sync.reset_mock()

        # If a Principal is added to a group
        self.group_a.principals.add(self.principal_a)

        sync.assert_called_once_with(
            event_type="group_membership_changed",
            payload={"group": {"name": self.group_a.name, "uuid": str(self.group_a.uuid)}, "action": "add"},
        )

        sync.reset_mock()
        # If a Group is added to a Principal
        self.principal_b.group.add(self.group_a)
        sync.assert_called_once_with(
            event_type="group_membership_changed",
            payload={"group": {"name": self.group_a.name, "uuid": str(self.group_a.uuid)}, "action": "add"},
        )

        sync.reset_mock()
        # If a Principal is removed from a group
        self.group_a.principals.remove(self.principal_a)
        sync.assert_called_once_with(
            event_type="group_membership_changed",
            payload={"group": {"name": self.group_a.name, "uuid": str(self.group_a.uuid)}, "action": "remove"},
        )

        sync.reset_mock()
        # If a Group is removed from a Principal
        self.principal_b.group.remove(self.group_a)
        sync.assert_called_once_with(
            event_type="group_membership_changed",
            payload={"group": {"name": self.group_a.name, "uuid": str(self.group_a.uuid)}, "action": "remove"},
        )

    @patch("internal.integration.sync_handlers.send_sync_message")
    def test_group_sync_clear_signals(self, sync):
        # If all groups are removed from a Principal
        self.group_a.principals.add(self.principal_a, self.principal_b)
        sync.reset_mock()
        self.principal_a.group.clear()
        sync.assert_called_once_with(
            event_type="group_membership_changed",
            payload={"group": {"name": self.group_a.name, "uuid": str(self.group_a.uuid)}, "action": "clear"},
        )

        sync.reset_mock()
        # If all Principals are removed from a Group
        self.group_a.principals.clear()
        sync.assert_called_once_with(
            event_type="group_membership_changed",
            payload={"group": {"name": self.group_a.name, "uuid": str(self.group_a.uuid)}, "action": "clear"},
        )

    @patch("internal.integration.sync_handlers.send_sync_message")
    def test_group_sync_delete_group_signal(self, sync):
        self.group_a.principals.add(self.principal_a)
        sync.reset_mock()
        self.group_a.delete()
        sync.assert_called_once_with(
            event_type="group_deleted",
            payload={
                "group": {"name": self.group_a.name, "uuid": str(self.group_a.uuid)},
            },
        )

    @patch("internal.integration.sync_handlers.send_sync_message")
    def test_policy_sync_group_signals(self, sync):
        """Test signals attached to Groups"""
        self.group_a.principals.add(self.principal_a)
        self.group_b.principals.add(self.principal_b)
        sync.reset_mock()

        # If a policy has its group set
        self.policy_a.group = self.group_a
        self.policy_a.save()
        sync.assert_called_once_with(
            event_type="platform_default_group_changed",
            payload={
                "group": {"name": self.group_a.name, "uuid": str(self.group_a.uuid)},
            },
        )

        sync.reset_mock()
        # If a policy has its group changed
        self.policy_a.group = self.group_b
        self.policy_a.save()
        sync.assert_called_once_with(
            event_type="non_default_group_relations_changed",
            payload={
                "group": {"name": self.group_b.name, "uuid": str(self.group_b.uuid)},
            },
        )

        sync.reset_mock()
        # If a policy is deleted
        self.policy_a.delete()
        sync.assert_called_once_with(
            event_type="non_default_group_relations_changed",
            payload={
                "group": {"name": self.group_b.name, "uuid": str(self.group_b.uuid)},
            },
        )

    @patch("internal.integration.sync_handlers.send_sync_message")
    def test_sync_add_remove_roles_signals(self, sync):
        """Test signals attached to Policy/Roles"""
        self.group_b.principals.add(self.principal_b)
        self.policy_a.group = self.group_a
        self.policy_a.save()
        self.policy_b.group = self.group_b
        self.policy_b.save()
        sync.reset_mock()

        # If a Role is added to a platform default group's Policy
        self.policy_a.roles.add(self.role_a)
        self.policy_a.save()

        sync.reset_mock()
        # If a Policy is added to a Role
        self.role_b.policies.add(self.policy_a)
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_b.name, "uuid": str(self.role_b.uuid)},
            },
        )

        sync.reset_mock()
        # If a Role is removed from a platform default group's Policy
        self.policy_a.roles.remove(self.role_a)
        self.policy_a.save()

        sync.reset_mock()
        # If a Role is removed from a Policy
        self.policy_b.roles.remove(self.role_b)
        sync.assert_called_once_with(
            event_type="non_default_group_relations_changed",
            payload={
                "group": {"name": self.group_b.name, "uuid": str(self.group_b.uuid)},
            },
        )

        sync.reset_mock()
        # If a Policy is removed from a Role
        self.role_b.policies.remove(self.policy_b)
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_b.name, "uuid": str(self.role_b.uuid)},
            },
        )

    @patch("internal.integration.sync_handlers.send_sync_message")
    def test_policy_sync_clear_signals(self, sync):
        self.group_a.principals.add(self.principal_a)
        self.group_b.principals.add(self.principal_b)
        self.policy_a.group = self.group_a
        self.policy_a.save()
        self.policy_b.group = self.group_b
        self.policy_b.save()
        self.policy_a.roles.add(self.role_a)
        self.policy_b.roles.add(self.role_b)
        sync.reset_mock()

        # If all policies are removed from a role
        self.role_a.policies.clear()
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_a.name, "uuid": str(self.role_a.uuid)},
            },
        )

        sync.reset_mock()
        # If all Roles are removed from a Policy
        self.policy_b.roles.clear()
        sync.assert_called_once_with(
            event_type="non_default_group_relations_changed",
            payload={
                "group": {"name": self.group_b.name, "uuid": str(self.group_b.uuid)},
            },
        )

    @patch("internal.integration.sync_handlers.send_sync_message")
    def test_policy_sync_change_delete_roles_signals(self, sync):
        self.group_a.principals.add(self.principal_a)
        self.group_b.principals.add(self.principal_b)
        self.policy_a.group = self.group_a
        self.policy_a.save()
        self.policy_b.group = self.group_b
        self.policy_b.save()
        self.policy_a.roles.add(self.role_a)
        self.policy_b.roles.add(self.role_b)
        sync.reset_mock()

        # If a role is changed
        self.role_a.version += 1
        self.role_a.save()
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_a.name, "uuid": str(self.role_a.uuid)},
            },
        )

        sync.reset_mock()
        # If Access is added
        self.permission = Permission.objects.create(permission="foo:*:*", tenant=self.tenant)
        self.access_a = Access.objects.create(permission=self.permission, role=self.role_a, tenant=self.tenant)
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_a.name, "uuid": str(self.role_a.uuid)},
            },
        )

        sync.reset_mock()
        # If ResourceDefinition is added
        self.rd_a = ResourceDefinition.objects.create(access=self.access_a, tenant=self.tenant)
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_a.name, "uuid": str(self.role_a.uuid)},
            },
        )

        sync.reset_mock()
        # If ResourceDefinition is destroyed
        self.rd_a.delete()
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_a.name, "uuid": str(self.role_a.uuid)},
            },
        )

        sync.reset_mock()
        # If Access is destroyed
        self.access_a.delete()
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_a.name, "uuid": str(self.role_a.uuid)},
            },
        )

        sync.reset_mock()
        # If Role is destroyed
        self.role_a.delete()
        sync.assert_called_once_with(
            event_type="role_modified",
            payload={
                "role": {"name": self.role_a.name, "uuid": str(self.role_a.uuid)},
            },
        )
