#
# Copyright 2019 Red Hat, Inc.
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
"""Test the group definer."""

import json
from unittest.mock import Mock, patch
from api.models import Tenant

from django.db import transaction
from django.test import override_settings
from management.group.definer import (
    seed_group,
    add_roles,
    clone_default_group_in_public_schema,
    remove_roles,
    _roles_by_query_or_ids,
)
from management.role.definer import seed_roles
from management.notifications.model import RhNotificationEvent
from tests.identity_request import IdentityRequest
from tests.management.notifications.utils import collected_rh_events
from management.models import Group, Role


@override_settings(ATOMIC_RETRY_DISABLED=True)
class GroupDefinerTests(IdentityRequest):
    """Test the group definer functions."""

    def setUp(self):
        """Set up the group definer tests."""
        super().setUp()
        self.public_tenant = Tenant.objects.get(tenant_name="public")
        seed_roles()
        seed_group()

    def test_default_group_seeding_properly(self):
        """Test that default group are seeded properly."""
        group = Group.objects.get(platform_default=True)

        system_policy = group.policies.get(name="System Policy for Group {}".format(group.uuid))
        self.assertEqual(group.platform_default, True)
        self.assertEqual(group.system, True)
        self.assertEqual(group.name, "Default access")
        self.assertEqual(group.tenant, self.public_tenant)
        self.assertEqual(system_policy.system, True)
        self.assertEqual(system_policy.tenant, self.public_tenant)
        # only platform_default roles would be assigned to the default group
        for role in group.roles():
            self.assertTrue(role.platform_default)
            self.assertEqual(role.tenant, self.public_tenant)

    def test_default_group_seeding_skips(self):
        """Test that default groups with system flag false will be skipped during seeding"""

        self.modify_default_group(system=False)

        try:
            seed_group()
        except Exception:
            self.fail(msg="update seed_group encountered an exception")

        group = Group.objects.get(platform_default=True)
        self.assertEqual(group.system, False)
        self.assertEqual(group.tenant, self.public_tenant)
        group.roles().get(name="Approval Administrator Local Test")

    def test_default_group_seeding_reassign_roles(self):
        """Test that previous assigned roles would be eliminated before assigning new roles."""
        self.modify_default_group()
        new_platform_role = Role.objects.create(
            name="new_platform_role", platform_default=True, system=True, tenant=self.public_tenant
        )
        role_to_remove = Role.objects.get(name="User Access administrator")

        with self.settings(NOTIFICATIONS_RH_ENABLED=True, NOTIFICATIONS_ENABLED=True):
            with collected_rh_events(self) as events:
                try:
                    group, admin_group = seed_group()
                except Exception:
                    self.fail(msg="update seed_group encountered an exception")

            # Assert return values match the persisted default groups
            persisted_group = Group.objects.get(platform_default=True, tenant=self.public_tenant)
            persisted_admin_group = Group.objects.get(admin_default=True, tenant=self.public_tenant)
            self.assertEqual(group.pk, persisted_group.pk)
            self.assertEqual(admin_group.pk, persisted_admin_group.pk)

            group = persisted_group
            self.assertEqual(group.system, True)
            self.assertRaises(Role.DoesNotExist, group.roles().get, name=role_to_remove.name)
            self.assertIsNotNone(group.roles().get(name=new_platform_role.name))
            for role in group.roles():
                self.assertTrue(role.platform_default)

            added_event = {
                "event_type": "rh-new-role-added-to-default-access",
                "payload": {
                    "username": "Red Hat",
                    "name": group.name,
                    "uuid": str(group.uuid),
                    "role": {"name": new_platform_role.name, "uuid": str(new_platform_role.uuid)},
                },
            }
            self.assertEqual(events.count(added_event), 1)
            removed_event = {
                "event_type": "rh-role-removed-from-default-access",
                "payload": {
                    "username": "Red Hat",
                    "name": group.name,
                    "uuid": str(group.uuid),
                    "role": {"name": role_to_remove.name, "uuid": str(role_to_remove.uuid)},
                },
            }
            self.assertEqual(events.count(removed_event), 1)
            json.dumps(events)

    @override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_RH_ENABLED=True)
    def test_system_group_events_follow_role_change_transaction(self):
        """Both additions and removals queue before commit and roll back with their role changes."""
        for group in Group.objects.filter(tenant=self.public_tenant, system=True).filter(
            platform_default=True
        ) | Group.objects.filter(tenant=self.public_tenant, system=True, admin_default=True):
            role = Role.objects.create(name=f"transaction test {group.name}", tenant=self.public_tenant, system=True)
            for operation in ("added", "removed"):
                with self.subTest(group=group.name, operation=operation):
                    event_type = (
                        "rh-new-role-added-to-default-access"
                        if operation == "added"
                        else "rh-role-removed-from-default-access"
                    )
                    change = add_roles if operation == "added" else remove_roles
                    with self.assertRaisesMessage(RuntimeError, "rollback"):
                        with transaction.atomic():
                            change(group, [str(role.uuid)], self.public_tenant)
                            self.assertTrue(
                                RhNotificationEvent.objects.filter(
                                    event_type=event_type, payload__role__uuid=str(role.uuid)
                                ).exists()
                            )
                            raise RuntimeError("rollback")
                    self.assertEqual(group.roles().filter(pk=role.pk).exists(), operation == "removed")
                    self.assertFalse(
                        RhNotificationEvent.objects.filter(
                            event_type=event_type, payload__role__uuid=str(role.uuid)
                        ).exists()
                    )
                    with transaction.atomic():
                        change(group, [str(role.uuid)], self.public_tenant)
                        self.assertTrue(
                            RhNotificationEvent.objects.filter(
                                event_type=event_type, payload__role__uuid=str(role.uuid)
                            ).exists()
                        )

    @override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_RH_ENABLED=True)
    def test_system_queue_failure_rolls_back_role_change(self):
        """Queue failure prevents the corresponding group role change from committing."""
        group = Group.objects.get(platform_default=True, tenant=self.public_tenant)
        role = Role.objects.create(name="failed queue role", tenant=self.public_tenant, system=True)
        with patch(
            "management.notifications.notification_handlers.queue_rh_notification",
            side_effect=RuntimeError("queue failed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "queue failed"):
                with transaction.atomic():
                    add_roles(group, [str(role.uuid)], self.public_tenant)
        self.assertFalse(group.roles().filter(pk=role.pk).exists())

    @override_settings(NOTIFICATIONS_ENABLED=True, REPLICATION_TO_RELATION_ENABLED=False)
    def test_custom_group_notifications_wait_for_commit(self):
        """Custom-group Kafka notifications remain deferred until the transaction commits."""
        group = Group.objects.create(name="custom group", tenant=self.tenant)
        role = Role.objects.create(name="custom role", tenant=self.tenant)
        user = Mock()
        with patch("management.group.definer.group_role_change_notification_handler") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                add_roles(group, [str(role.uuid)], self.tenant, user)
                remove_roles(group, [str(role.uuid)], self.tenant, user)
                notify.assert_not_called()
            self.assertEqual([call.args[-1] for call in notify.call_args_list], ["added", "removed"])

    def modify_default_group(self, system=True):
        """Add a role to the default group and/or change the system flag"""
        group = Group.objects.get(platform_default=True)
        roles = Role.objects.filter(name="User Access administrator")
        add_roles(group, roles, self.public_tenant)

        group.system = system
        group.save()

        self.assertIsNotNone(group.roles().get(name=roles.first().name))

    def test_admin_default_group_seeding_properly(self):
        """Test that admin default group are seeded properly."""
        group = Group.objects.get(admin_default=True)

        system_policy = group.policies.get(name="System Policy for Group {}".format(group.uuid))
        self.assertEqual(group.admin_default, True)
        self.assertEqual(group.system, True)
        self.assertEqual(group.name, "Default admin access")
        self.assertEqual(group.tenant, self.public_tenant)
        self.assertEqual(system_policy.system, True)
        self.assertEqual(system_policy.tenant, self.public_tenant)
        # only admin_default roles would be assigned to the admin_default group
        for role in group.roles():
            self.assertTrue(role.admin_default)
            self.assertEqual(role.tenant, self.public_tenant)

    def test_clone_default_group_in_public_schema(self):
        """Test that the custom default group contains only public tenant roles by default."""
        invalid_role = Role.objects.create(
            platform_default=True, system=True, tenant=self.tenant, name="INVALID SYSTEM ROLE ON TENANT"
        )
        group = Group.objects.get(platform_default=True, tenant=self.public_tenant)
        self.assertEqual(Group.objects.filter(platform_default=True, tenant=self.tenant).count(), 0)
        clone_default_group_in_public_schema(group, self.tenant)

        self.assertEqual(Group.objects.filter(platform_default=True, tenant=self.tenant).count(), 1)
        custom_default_group = Group.objects.filter(platform_default=True, tenant=self.tenant).last()
        self.assertTrue(invalid_role not in list(custom_default_group.roles()))

    def test_roles_by_query_or_ids_with_tenant_filtering(self):
        """Test that _roles_by_query_or_ids properly filters roles by tenant."""
        # Create roles in different tenants with the same name
        tenant_a = Tenant.objects.create(tenant_name="tenant_a", org_id="111111")
        tenant_b = Tenant.objects.create(tenant_name="tenant_b", org_id="222222")

        role_name = "shared_role_name"
        role_a = Role.objects.create(name=role_name, tenant=tenant_a)
        role_b = Role.objects.create(name=role_name, tenant=tenant_b)

        # Create a queryset with both roles
        roles_queryset = Role.objects.filter(name=role_name)
        self.assertEqual(roles_queryset.count(), 2)

        # Test filtering by tenant_a - should only return role_a
        filtered_roles_a = _roles_by_query_or_ids(roles_queryset, tenant_a)
        self.assertEqual(filtered_roles_a.count(), 1)
        self.assertEqual(filtered_roles_a.first(), role_a)
        self.assertNotIn(role_b, filtered_roles_a)

        # Test filtering by tenant_b - should only return role_b
        filtered_roles_b = _roles_by_query_or_ids(roles_queryset, tenant_b)
        self.assertEqual(filtered_roles_b.count(), 1)
        self.assertEqual(filtered_roles_b.first(), role_b)
        self.assertNotIn(role_a, filtered_roles_b)

    def test_roles_by_query_or_ids_with_uuid_list(self):
        """Test that _roles_by_query_or_ids works with UUID list."""
        tenant_a = Tenant.objects.create(tenant_name="tenant_a", org_id="111111")
        role = Role.objects.create(name="test_role", tenant=tenant_a)

        # Test with UUID list
        role_uuids = [str(role.uuid)]
        filtered_roles = _roles_by_query_or_ids(role_uuids, tenant_a)

        self.assertEqual(filtered_roles.count(), 1)
        self.assertEqual(filtered_roles.first(), role)

    def test_roles_by_query_or_ids_nonexistent_roles(self):
        """Test that _roles_by_query_or_ids returns an empty queryset for nonexistent roles."""
        tenant_a = Tenant.objects.create(tenant_name="tenant_a", org_id="111111")

        # Create queryset with no matching roles for this tenant
        empty_queryset = Role.objects.filter(name="nonexistent_role")

        filtered_roles = _roles_by_query_or_ids(empty_queryset, tenant_a)
        self.assertEqual(filtered_roles.count(), 0)

    @patch("management.group.definer.settings.REPLICATION_TO_RELATION_ENABLED", False)
    def test_add_roles_tenant_isolation(self):
        """Test that add_roles only adds roles from the specified tenant."""

        tenant_a = Tenant.objects.create(tenant_name="tenant_a", org_id="111111")
        tenant_b = Tenant.objects.create(tenant_name="tenant_b", org_id="222222")

        # Create roles with same name in different tenants
        role_name = "test_role"
        role_a = Role.objects.create(name=role_name, tenant=tenant_a)
        role_b = Role.objects.create(name=role_name, tenant=tenant_b)

        # Create group in tenant_a
        group_a = Group.objects.create(name="test_group", tenant=tenant_a)

        # Create queryset with both roles
        roles_queryset = Role.objects.filter(name=role_name)
        self.assertEqual(roles_queryset.count(), 2)

        # Add roles to group in tenant_a - should only add role_a
        add_roles(group_a, roles_queryset, tenant_a)

        group_roles = list(group_a.roles())
        self.assertEqual(len(group_roles), 1)
        self.assertIn(role_a, group_roles)
        self.assertNotIn(role_b, group_roles)

    @patch("management.group.definer.settings.REPLICATION_TO_RELATION_ENABLED", False)
    def test_remove_roles_tenant_isolation(self):
        """Test that remove_roles only removes roles from the specified tenant."""

        tenant_a = Tenant.objects.create(tenant_name="tenant_a", org_id="111111")
        tenant_b = Tenant.objects.create(tenant_name="tenant_b", org_id="222222")

        # Create roles with same name in different tenants
        role_name = "test_role"
        role_a = Role.objects.create(name=role_name, tenant=tenant_a)
        role_b = Role.objects.create(name=role_name, tenant=tenant_b)

        # Create group in tenant_a and add the role
        group_a = Group.objects.create(name="test_group", tenant=tenant_a)
        roles_queryset = Role.objects.filter(name=role_name)
        add_roles(group_a, roles_queryset, tenant_a)

        # Verify the role from tenant_a is in the group initially
        initial_roles = list(group_a.roles())
        self.assertEqual(len(initial_roles), 1)
        self.assertIn(role_a, initial_roles)
        self.assertNotIn(role_b, initial_roles)

        # Try to remove roles by queryset - should remove role_a
        roles_queryset = Role.objects.filter(name=role_name)
        self.assertEqual(roles_queryset.count(), 2)
        self.assertIn(role_a, roles_queryset)
        self.assertIn(role_b, roles_queryset)

        remove_roles(group_a, roles_queryset, tenant_a)

        remaining_roles = list(group_a.roles())
        self.assertEqual(len(remaining_roles), 0)
        self.assertNotIn(role_a, remaining_roles)  # role_a was removed
        self.assertNotIn(role_b, remaining_roles)  # role_b was never added

    @patch("management.group.definer.settings.REPLICATION_TO_RELATION_ENABLED", False)
    def test_add_and_remove_roles_by_uuid(self):
        """Test that UUID-based operations still work (backward compatibility)."""

        tenant_a = Tenant.objects.create(tenant_name="tenant_a", org_id="111111")

        # Create role
        role = Role.objects.create(name="test_role", tenant=tenant_a)
        group = Group.objects.create(name="test_group", tenant=tenant_a)

        # Add role by UUID
        add_roles(group, [str(role.uuid)], tenant_a)

        group_roles = list(group.roles())
        self.assertEqual(len(group_roles), 1)
        self.assertIn(role, group_roles)

        # Remove role by UUID
        remove_roles(group, [str(role.uuid)], tenant_a)

        remaining_roles = list(group.roles())
        self.assertEqual(len(remaining_roles), 0)
