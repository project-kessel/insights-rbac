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
"""Test the role definer."""

from uuid import UUID
from django.conf import settings
from django.test.utils import override_settings
from unittest.mock import ANY, call, patch, mock_open

from api.models import Tenant
from management.group.definer import seed_group
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.models import (
    Access,
    ExtRoleRelation,
    Permission,
    ResourceDefinition,
    Role,
    Group,
    PlatformRoleV2,
    SeededRoleV2,
)
from management.permission.scope_service import Scope
from management.relation_replicator.relation_replicator import ReplicationEvent, ReplicationEventType
from management.role.definer import seed_roles, seed_permissions, _seed_platform_roles
from management.role.platform import (
    ADMIN_DEFAULT_SEEDED_ROLES_FORCE_ROOT_SCOPE,
    admin_platform_parent_scope_for_seeded_system_role,
    platform_v2_role_uuid_for,
)
from management.role.relation_api_dual_write_handler import (
    RelationApiDualWriteHandler,
    SeedingRelationApiDualWriteHandler,
)
from management.tenant_mapping.model import DefaultAccessType
from management.tenant_service.v2 import V2TenantBootstrapService
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    all_of,
    relation,
    resource,
    subject,
)
from tests.core.test_kafka import copy_call_args
from tests.identity_request import IdentityRequest
from tests.management.role.test_dual_write import RbacFixture


def _role_resource(role_uuid: str | UUID):
    return resource("rbac", "role", str(role_uuid))


def _role_subject(role_uuid: str | UUID):
    return subject("rbac", "role", str(role_uuid))


def _child_predicate(parent_uuid: str | UUID, child_uuid: str | UUID):
    return all_of(
        _role_resource(parent_uuid),
        relation("child"),
        _role_subject(child_uuid),
    )


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RoleDefinerTests(IdentityRequest):
    """Test the role definer functions."""

    def _assert_child(self, tuples: InMemoryTuples, parent_uuid: str | UUID, child_uuid: str | UUID):
        self.assertEqual(
            1,
            len(tuples.find_tuples(_child_predicate(parent_uuid=parent_uuid, child_uuid=child_uuid))),
            f"Expected child relation to be present: parent={str(parent_uuid)}, child={str(child_uuid)}",
        )

        platform_role = PlatformRoleV2.objects.get(uuid=parent_uuid)
        child_role = SeededRoleV2.objects.get(uuid=child_uuid)

        self.assertTrue(platform_role.children.contains(child_role))

    def _assert_not_child(self, tuples: InMemoryTuples, parent_uuid: str | UUID, child_uuid: str | UUID):
        self.assertEqual(
            0,
            len(tuples.find_tuples(_child_predicate(parent_uuid=parent_uuid, child_uuid=child_uuid))),
            f"Expected child relation to be absent: parent={str(parent_uuid)}, child={str(child_uuid)}",
        )

        platform_role = PlatformRoleV2.objects.get(uuid=parent_uuid)
        child_role = SeededRoleV2.objects.get(uuid=child_uuid)

        self.assertFalse(platform_role.children.contains(child_role))

    def setUp(self):
        """Set up the role definer tests."""
        super().setUp()
        self.public_tenant = Tenant.objects.get(tenant_name="public")

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_role_create(self, send_kafka_message):
        kafka_mock = copy_call_args(send_kafka_message)
        """Test that we can run a role seeding update."""
        with self.settings(NOTIFICATIONS_RH_ENABLED=True, NOTIFICATIONS_ENABLED=True):
            self.try_seed_roles()

            roles = Role.objects.filter(platform_default=True)

            org_id = self.customer_data["org_id"]

            self.assertTrue(len(roles))
            self.assertFalse(Role.objects.get(name="User Access administrator").platform_default)

            kafka_mock.assert_any_call(
                settings.NOTIFICATIONS_TOPIC,
                {
                    "bundle": "console",
                    "application": "rbac",
                    "event_type": "rh-new-role-available",
                    "timestamp": ANY,
                    "events": [
                        {
                            "metadata": {},
                            "payload": {
                                "name": roles.first().name,
                                "username": "Red Hat",
                                "uuid": str(roles.first().uuid),
                            },
                        }
                    ],
                    "org_id": org_id,
                },
                ANY,
            )

    def test_role_update(self):
        """Test that role seeding update will re-create the roles."""
        self.try_seed_roles()

        # delete all the roles and re-create roles again when seed_roles is called
        Role.objects.all().delete()
        roles = Role.objects.filter(platform_default=True)
        self.assertFalse(len(roles))

        seed_roles()
        roles = Role.objects.filter(platform_default=True)
        self.assertTrue(len(roles))

        for access in Access.objects.all():
            self.assertEqual(access.tenant, self.public_tenant)
            for rd in ResourceDefinition.objects.filter(access=access):
                self.assertEqual(rd.tenant, self.public_tenant)

    def test_role_update_version_diff(self):
        """Test that role seeding updates attribute when version is changed."""
        self.try_seed_roles()

        # set the version to zero, so it would update attribute when seed_roles is called
        roles = Role.objects.filter(platform_default=True).update(version=0, platform_default=False)
        self.assertFalse(len(Role.objects.filter(platform_default=True)))

        seed_roles()
        roles = Role.objects.filter(platform_default=True)
        self.assertTrue(len(roles))

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_role_update_platform_default_role(self, send_kafka_message):
        """Test that role seeding updates send out notification."""
        kafka_mock = copy_call_args(send_kafka_message)
        self.try_seed_roles()

        # Update non platform default role
        non_platform_role_to_update = Role.objects.get(name="User Access administrator")
        non_platform_role_to_update.version = 0
        access = non_platform_role_to_update.access.first()
        access.permission = Permission.objects.get(permission="rbac:principal:read")
        non_platform_role_to_update.save()
        access.save()

        # Update platform default role
        platform_role_to_update = Role.objects.get(name="Notifications viewer")
        platform_role_to_update.version = 0
        access = platform_role_to_update.access.first()
        # Create the permission if it doesn't exist, then use it
        write_permission, _ = Permission.objects.get_or_create(
            permission="notifications:notifications:write", tenant=self.public_tenant
        )
        access.permission = write_permission
        platform_role_to_update.save()
        access.save()

        org_id = self.customer_data["org_id"]
        Tenant.objects.create(tenant_name="unready1", org_id="unready1", ready=False)
        Tenant.objects.create(tenant_name="unready2", org_id="unready2", ready=False)

        with self.settings(NOTIFICATIONS_RH_ENABLED=True, NOTIFICATIONS_ENABLED=True):
            seed_roles()

            platform_role_to_update.refresh_from_db()
            non_platform_role_to_update.refresh_from_db()
            self.assertEqual(
                non_platform_role_to_update.access.first().permission.permission,
                "rbac:*:*",
            )
            self.assertEqual(
                platform_role_to_update.access.first().permission.permission,
                "notifications:notifications:read",
            )

            notification_messages = [
                call(
                    settings.NOTIFICATIONS_TOPIC,
                    {
                        "bundle": "console",
                        "application": "rbac",
                        "event_type": "rh-non-platform-default-role-updated",
                        "timestamp": ANY,
                        "events": [
                            {
                                "metadata": {},
                                "payload": {
                                    "name": non_platform_role_to_update.name,
                                    "username": "Red Hat",
                                    "uuid": str(non_platform_role_to_update.uuid),
                                },
                            }
                        ],
                        "org_id": org_id,
                    },
                    ANY,
                ),
                call(
                    settings.NOTIFICATIONS_TOPIC,
                    {
                        "bundle": "console",
                        "application": "rbac",
                        "event_type": "rh-platform-default-role-updated",
                        "timestamp": ANY,
                        "events": [
                            {
                                "metadata": {},
                                "payload": {
                                    "name": platform_role_to_update.name,
                                    "username": "Red Hat",
                                    "uuid": str(platform_role_to_update.uuid),
                                },
                            }
                        ],
                        "org_id": org_id,
                    },
                    ANY,
                ),
            ]
            kafka_mock.assert_has_calls(notification_messages, any_order=True)

            for call_args in kafka_mock.call_args_list:
                topic = call_args.args[0]
                if topic != settings.NOTIFICATIONS_TOPIC:
                    continue
                body = call_args.args[1]
                self.assertNotIn(
                    body.get("org_id"),
                    ["unready1", "unready2"],
                    "Unready tenant should not be notified",
                )

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_role_create_skip_notifications(self, send_kafka_message):
        """Test that skip_notifications suppresses RH notifications during role seeding."""
        kafka_mock = copy_call_args(send_kafka_message)
        with self.settings(NOTIFICATIONS_RH_ENABLED=True, NOTIFICATIONS_ENABLED=True):
            from management.seeds import role_seeding

            role_seeding(skip_notifications=True)

        notification_calls = [c for c in kafka_mock.call_args_list if c.args[0] == settings.NOTIFICATIONS_TOPIC]
        self.assertEqual(len(notification_calls), 0, "No notifications should be sent when skip_notifications=True")

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_role_update_skip_notifications(self, send_kafka_message):
        """Test that skip_notifications suppresses RH notifications during role update seeding."""
        kafka_mock = copy_call_args(send_kafka_message)
        self.try_seed_roles()

        # Force an update by zeroing the version
        Role.objects.filter(platform_default=True).update(version=0)

        with self.settings(NOTIFICATIONS_RH_ENABLED=True, NOTIFICATIONS_ENABLED=True):
            from management.seeds import role_seeding

            role_seeding(skip_notifications=True)

        notification_calls = [c for c in kafka_mock.call_args_list if c.args[0] == settings.NOTIFICATIONS_TOPIC]
        self.assertEqual(len(notification_calls), 0, "No notifications should be sent when skip_notifications=True")

    def try_seed_roles(self):
        """Try to seed roles"""
        try:
            seed_roles()
        except Exception:
            self.fail(msg="seed_roles encountered an exception")

        # External role relations are seeded
        ext_relations = ExtRoleRelation.objects.all()
        self.assertNotEqual(len(ext_relations), 0)
        for relation in ext_relations:
            self.assertIsNotNone(relation.role)

        # Update relation to point to a new role. Seed again would update relation back.
        ext_relation = ExtRoleRelation.objects.first()
        origin_role = ext_relation.role
        ext_relation.role = Role.objects.get(name="User Access administrator")
        ext_relation.save()
        origin_role.version = 1
        origin_role.save()
        seed_roles()

        ext_relation.refresh_from_db()
        self.assertEqual(origin_role, ext_relation.role)

    def test_try_seed_permissions(self):
        """Test permission seeding."""
        self.assertFalse(len(Permission.objects.all()))

        try:
            seed_permissions()
        except Exception:
            self.fail(msg="seed_permissions encountered an exception")

        self.assertTrue(len(Permission.objects.all()))

        permission = Permission.objects.first()
        self.assertTrue(permission.application)
        self.assertTrue(permission.resource_type)
        self.assertTrue(permission.verb)
        self.assertTrue(permission.permission)
        self.assertEqual(permission.tenant, self.public_tenant)

    def test_try_seed_permissions_update_description(self):
        """Test permission seeding update description, skip string configs."""
        permission_string = "approval:templates:read"
        self.assertFalse(len(Permission.objects.all()))
        Permission.objects.create(permission=permission_string, tenant=self.public_tenant)

        try:
            seed_permissions()
        except Exception:
            self.fail(msg="seed_permissions encountered an exception")

        self.assertEqual(
            Permission.objects.exclude(permission=permission_string).values_list("description").distinct().first(),
            ("",),
        )

        permission = Permission.objects.filter(permission=permission_string)
        self.assertEqual(len(permission), 1)
        self.assertEqual(permission.first().description, "Approval local test templates read.")
        # Previous string verb still works
        self.assertEqual(Permission.objects.filter(permission="inventory:*:*").count(), 1)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch("management.role.definer.destructive_ok")
    def test_seed_permissions_delete_permission(self, _, replicate):
        """Test permission seeding delete permission."""
        self.assertFalse(len(Permission.objects.all()))

        # Create a permission in the database that's not in config
        permission_to_delete = Permission.objects.create(
            permission="dummy:permission:delete", tenant=self.public_tenant
        )
        relation_tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(relation_tuples)
        replicate.side_effect = replicator.replicate
        fixture = RbacFixture(V2TenantBootstrapService(replicator))
        fixture.bootstrap_tenant(self.tenant)
        role_system = fixture.new_system_role(name="system_role", permissions=["dummy:permission:delete"])
        dual_write_handler = SeedingRelationApiDualWriteHandler(role_system, replicator=replicator)
        dual_write_handler.replicate_new_system_role()

        role_custom = fixture.new_custom_role(
            name="custom_role",
            tenant=self.tenant,
            resource_access=fixture.workspace_access(["dummy:permission:delete"]),
        )
        dual_write = RelationApiDualWriteHandler(
            role_custom, ReplicationEventType.CREATE_CUSTOM_ROLE, replicator=replicator
        )
        dual_write.replicate_new_or_updated_role(role_custom)

        # Before removing the permission
        self.assertEqual(
            1,
            relation_tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role_system.uuid)),
                    relation("dummy_permission_delete"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )
        for binding in role_custom.binding_mappings.all():
            v2_role_id = binding.mappings["role"]["id"]
            self.assertEqual(
                1,
                relation_tuples.count_tuples(
                    all_of(
                        resource("rbac", "role", v2_role_id),
                        relation("dummy_permission_delete"),
                        subject("rbac", "principal", "*"),
                    )
                ),
            )

        try:
            seed_permissions()
        except Exception:
            self.fail(msg="seed_permissions encountered an exception")

        # Verify permission was deleted from the database
        self.assertFalse(Permission.objects.filter(id=permission_to_delete.id).exists())
        # After removing the permission
        self.assertEqual(
            0,
            relation_tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role_system.uuid)),
                    relation("dummy_permission_delete"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )
        for binding in role_custom.binding_mappings.all():
            v2_role_id = binding.mappings["role"]["id"]
            self.assertEqual(
                0,
                relation_tuples.count_tuples(
                    all_of(
                        resource("rbac", "role", v2_role_id),
                        relation("dummy_permission_delete"),
                        subject("rbac", "principal", "*"),
                    )
                ),
            )

    def is_create_event(self, relation: str, evt: ReplicationEvent) -> bool:
        return evt.event_type == ReplicationEventType.CREATE_SYSTEM_ROLE and any(
            t.relation == relation for t in evt.add
        )

    def is_remove_event(self, relation: str, evt: ReplicationEvent) -> bool:
        return evt.event_type == ReplicationEventType.DELETE_SYSTEM_ROLE and any(
            t.relation == relation for t in evt.remove
        )

    def is_update_event(self, relation: str, evt: ReplicationEvent) -> bool:
        return evt.event_type == ReplicationEventType.UPDATE_SYSTEM_ROLE and any(
            t.relation == relation for t in evt.add
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_seed_roles_new_role(self, mock_replicate):
        seed_roles()
        self.assertTrue(
            any(self.is_create_event("inventory_hosts_read", args[0]) for args, _ in mock_replicate.call_args_list)
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch("management.role.definer.destructive_ok")
    @patch("builtins.open", new_callable=mock_open, read_data='{"roles": []}')
    @patch("os.listdir")
    @patch("os.path.isfile")
    def test_seed_roles_delete_role(
        self,
        mock_isfile,
        mock_listdir,
        mock_open,
        mock_destructive_ok,
        mock_replicate,
    ):
        mock_destructive_ok.return_value = True
        # mock files
        mock_listdir.return_value = ["role.json"]
        mock_isfile.return_value = True

        # create a role in the database that's not in config
        role_to_delete = Role.objects.create(name="dummy_role_delete", system=True, tenant=self.public_tenant)
        permission, _ = Permission.objects.get_or_create(permission="inventory:hosts:read", tenant=self.public_tenant)
        _ = Access.objects.create(permission=permission, role=role_to_delete, tenant=self.public_tenant)

        role_to_delete.save()

        seed_roles()

        self.assertTrue(
            any(self.is_remove_event("inventory_hosts_read", args[0]) for args, _ in mock_replicate.call_args_list)
        )

        # verify role was deleted from the database
        self.assertFalse(Role.objects.filter(id=role_to_delete.id).exists())

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data='{"roles": [{"name": "dummy_role_update", "system": true, "version": 3, "access": [{"permission": "dummy:hosts:read"}]}]}',
    )
    @patch("os.listdir")
    @patch("os.path.isfile")
    def test_seed_roles_update_role(
        self,
        mock_isfile,
        mock_listdir,
        mock_open,
        mock_replicate,
    ):
        # mock files
        mock_listdir.return_value = ["role.json"]
        mock_isfile.return_value = True

        # create a role in the database that exists in config
        Role.objects.create(name="dummy_role_update", system=True, version=1, tenant=self.public_tenant)

        seed_roles()

        self.assertTrue(
            any(self.is_update_event("dummy_hosts_read", args[0]) for args, _ in mock_replicate.call_args_list)
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_seed_roles_create_and_delete_role(
        self,
        mock_replicate,
    ):
        # seed to create role
        seed_roles()
        self.assertTrue(
            any(self.is_create_event("inventory_hosts_read", args[0]) for args, _ in mock_replicate.call_args_list)
        )

        # seed to remove role
        with (
            patch("os.path.isfile") as mock_isfile,
            patch("os.listdir") as mock_listdir,
            patch("builtins.open", mock_open(read_data='{"roles": []}')) as mock_file,
            patch("management.role.definer.destructive_ok") as mock_destructive_ok,
        ):
            # mock files
            mock_destructive_ok.return_value = True
            mock_listdir.return_value = ["role.json"]
            mock_isfile.return_value = True

            seed_roles()

            self.assertTrue(
                any(self.is_remove_event("inventory_hosts_read", args[0]) for args, _ in mock_replicate.call_args_list)
            )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data='{"roles": [{"name": "existing_system_role", "system": true, "version": 1, "access": [{"permission": "dummy:hosts:read"}]}, {"name": "role_wants_update", "system": true, "version": 3, "access": [{"permission": "dummy:hosts:write"}]}]}',
    )
    @patch("os.listdir")
    @patch("os.path.isfile")
    def test_seed_roles_existing_role_add_tuples(
        self,
        mock_isfile,
        mock_listdir,
        mock_open,
        mock_replicate,
    ):
        # mock files
        mock_listdir.return_value = ["role.json"]
        mock_isfile.return_value = True

        # create a role in the database that exists in config with no changes.
        existing_role = Role.objects.create(
            name="existing_system_role",
            system=True,
            version=1,
            tenant=self.public_tenant,
        )
        permission, _ = Permission.objects.get_or_create(permission="dummy:hosts:read", tenant=self.public_tenant)
        _ = Access.objects.create(permission=permission, role=existing_role, tenant=self.public_tenant)

        existing_role.save()

        # create a role in the database that exists in config with changes.
        Role.objects.create(name="role_wants_update", system=True, version=1, tenant=self.public_tenant)

        seed_roles(force_create_relationships=True)

        self.assertTrue(
            any(self.is_create_event("dummy_hosts_read", args[0]) for args, _ in mock_replicate.call_args_list)
        )
        self.assertTrue(
            any(self.is_update_event("dummy_hosts_write", args[0]) for args, _ in mock_replicate.call_args_list)
        )

    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data='{"roles": [{"name": "dummy_role_update", "system": true, "version": 3, "access": [{"permission": '
        '"dummy:hosts:read"}]}]}',
    )
    @patch("os.listdir")
    @patch("os.path.isfile")
    def test_seed_roles_does_not_update_custom_roles_of_the_same_name(self, mock_isfile, mock_listdir, mock_open):
        # mock files
        mock_listdir.return_value = ["role.json"]
        mock_isfile.return_value = True

        # create a role in the database that exists in config for both public tenant and custom tenant
        Role.objects.create(name="dummy_role_update", system=True, version=1, tenant=self.public_tenant)
        custom = Role.objects.create(name="dummy_role_update", system=False, version=1, tenant=self.tenant)

        seed_roles()

        self.assertFalse(Role.objects.get(pk=custom.pk).system)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data='{"roles": [{"name": "test_seeding_role", "display_name": "Test Seeding Role", "system": true, "version": 1, "platform_default": true, "access": [{"permission": "inventory:hosts:read"}]}]}',
    )
    @patch("os.listdir")
    @patch("os.path.isfile")
    def test_seed_roles_generate_relations_for_role(
        self,
        mock_isfile,
        mock_listdir,
        mock_open,
        mock_replicate,
    ):
        """Test that seed_roles generates relations for roles using _generate_relations_for_role."""
        # Mock the file system to return our test role config
        mock_listdir.return_value = ["test_role.json"]
        mock_isfile.return_value = True

        # Ensure the permission exists
        Permission.objects.get_or_create(permission="inventory:hosts:read", tenant=self.public_tenant)

        # Call seed_roles which should trigger _generate_relations_for_role
        seed_roles()

        # Verify the role was created
        role = Role.objects.get(name="test_seeding_role", tenant=self.public_tenant)
        self.assertTrue(role.system)
        self.assertTrue(role.platform_default)

        # Verify that replicate was called (which means _generate_relations_for_role was executed)
        self.assertTrue(mock_replicate.called)

        # Verify the replication event was for creating a system role
        self.assertTrue(
            any(self.is_create_event("inventory_hosts_read", args[0]) for args, _ in mock_replicate.call_args_list)
        )

    def test_force_conflict(self):
        """Test that attempting to set both force_create_relationships and force_update_relationships fails."""
        self.assertRaises(
            ValueError,
            seed_roles,
            force_create_relationships=True,
            force_update_relationships=True,
        )

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_force_update_relationships(self, replicate):
        """Test that using force_update_relationships results in updating default scopes when they have changed."""
        tuples = InMemoryTuples()
        replicate.side_effect = InMemoryRelationReplicator(tuples).replicate

        seed_group()

        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            seed_roles()

        policy_cache = GlobalPolicyIdService()

        default_platform_default_uuid = policy_cache.platform_default_policy_uuid()
        default_admin_default_uuid = policy_cache.admin_default_policy_uuid()
        root_platform_default_uuid = UUID(settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID)
        tenant_platform_default_uuid = UUID(settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID)
        tenant_admin_default_uuid = UUID(settings.SYSTEM_ADMIN_TENANT_ROLE_UUID)
        root_admin_default_uuid = UUID(settings.SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID)

        initial_count = len(tuples)

        # Note that notifications_role and approval_role are platform_default, while user_access_role is admin_default.
        notifications_role = Role.objects.public_tenant_only().get(name="Notifications viewer")
        approval_role = Role.objects.public_tenant_only().get(name="Approval Approver")
        user_access_role = Role.objects.public_tenant_only().get(name="User Access administrator")

        # This role is a special case: when assigned as part of default access (only), it should always be assigned in
        # root scope. See RHCLOUD-45734 and https://github.com/project-kessel/insights-rbac/pull/2653
        inventory_role = Role.objects.public_tenant_only().get(name="Inventory Groups Administrator")

        # Assert that seed_role creates relations in the default scope for ordinary roles.
        self._assert_child(
            tuples,
            parent_uuid=default_platform_default_uuid,
            child_uuid=notifications_role.uuid,
        )
        self._assert_child(
            tuples,
            parent_uuid=default_platform_default_uuid,
            child_uuid=approval_role.uuid,
        )
        self._assert_child(
            tuples,
            parent_uuid=default_admin_default_uuid,
            child_uuid=user_access_role.uuid,
        )

        # As mentioned above, this role is a special case and should always have root scope.
        self._assert_child(
            tuples,
            parent_uuid=root_admin_default_uuid,
            child_uuid=inventory_role.uuid,
        )

        # Force updating the existing relationships even though the role version numbers have not changed.
        # This puts approval_role in root scope and user_access_role in tenant scope.
        #
        # inventory_role's permissions become tenant-scoped, but the admin-default assignment always stays in root
        # scope.
        with self.settings(
            ROOT_SCOPE_PERMISSIONS="approval:actions:create",
            TENANT_SCOPE_PERMISSIONS="rbac:*:*,inventory:*:*",
        ):
            seed_roles(force_update_relationships=True)

        # Assert that relations for non-default-scope roles were removed.
        self._assert_not_child(
            tuples,
            parent_uuid=default_platform_default_uuid,
            child_uuid=approval_role.uuid,
        )
        self._assert_not_child(
            tuples,
            parent_uuid=default_admin_default_uuid,
            child_uuid=inventory_role.uuid,
        )

        # Assert that we end up with the correct relations.
        self._assert_child(
            tuples,
            parent_uuid=default_platform_default_uuid,
            child_uuid=notifications_role.uuid,
        )
        self._assert_child(
            tuples,
            parent_uuid=root_platform_default_uuid,
            child_uuid=approval_role.uuid,
        )
        self._assert_child(
            tuples,
            parent_uuid=tenant_admin_default_uuid,
            child_uuid=user_access_role.uuid,
        )

        # inventory_role should still be in the root scope.
        self._assert_child(
            tuples,
            parent_uuid=root_admin_default_uuid,
            child_uuid=inventory_role.uuid,
        )
        self._assert_not_child(
            tuples,
            parent_uuid=tenant_admin_default_uuid,
            child_uuid=inventory_role.uuid,
        )

        self.assertEqual(
            initial_count,
            len(tuples),
            "Expected overall number of tuples not to change.",
        )

        count_after_first_update = len(tuples)

        # Check that we can also move roles between non-default scopes.
        # This puts both approval_role and inventory_role permissions in tenant scope.
        with self.settings(
            ROOT_SCOPE_PERMISSIONS="",
            TENANT_SCOPE_PERMISSIONS="approval:actions:create,rbac:*:*",
        ):
            seed_roles(force_update_relationships=True)

        # Assert that relations for non-default-scope roles were removed.
        self._assert_not_child(
            tuples,
            parent_uuid=root_platform_default_uuid,
            child_uuid=approval_role.uuid,
        )

        # Assert that we end up with the correct relations.
        self._assert_child(
            tuples,
            parent_uuid=default_platform_default_uuid,
            child_uuid=notifications_role.uuid,
        )
        self._assert_child(
            tuples,
            parent_uuid=tenant_platform_default_uuid,
            child_uuid=approval_role.uuid,
        )
        self._assert_child(
            tuples,
            parent_uuid=default_platform_default_uuid,
            child_uuid=approval_role.uuid,
        )
        self._assert_child(
            tuples,
            parent_uuid=tenant_admin_default_uuid,
            child_uuid=user_access_role.uuid,
        )

        # inventory_role should still not have moved.
        self._assert_child(
            tuples,
            parent_uuid=root_admin_default_uuid,
            child_uuid=inventory_role.uuid,
        )
        self._assert_not_child(
            tuples,
            parent_uuid=default_admin_default_uuid,
            child_uuid=inventory_role.uuid,
        )
        self._assert_not_child(
            tuples,
            parent_uuid=tenant_admin_default_uuid,
            child_uuid=inventory_role.uuid,
        )

        # Second force-update net tuple delta (+2): both platform-default approval roles in
        # approval_local_test.json move from ROOT-only to TENANT + DEFAULT platform parents.
        #   • Approval Approver: remove root platform parent, add tenant + default (+1)
        #   • Approval Administrator Local Test: same scope change (+1)
        # All other roles seeded here keep the same binding scopes, so their parent tuples
        # are removed and re-added unchanged (net 0).
        self.assertEqual(
            count_after_first_update + 2,
            len(tuples),
            "Both approval platform-default roles gain tenant and default workspace platform parents.",
        )

    def test_admin_default_seeded_roles_force_root_scope(self):
        """Test that all roles in ADMIN_DEFAULT_SEEDED_ROLES_FORCE_ROOT_SCOPE are overridden to ROOT."""
        for role_name in ADMIN_DEFAULT_SEEDED_ROLES_FORCE_ROOT_SCOPE:
            for derived_scope in Scope:
                result = admin_platform_parent_scope_for_seeded_system_role(
                    role_name, derived_scope, apply_override=True
                )
                self.assertEqual(
                    result,
                    Scope.ROOT,
                    f"{role_name!r} with derived scope {derived_scope.name} should be forced to ROOT",
                )

    def test_admin_default_force_root_does_not_apply_without_override(self):
        """Test that the ROOT override is skipped when apply_override=False."""
        for role_name in ADMIN_DEFAULT_SEEDED_ROLES_FORCE_ROOT_SCOPE:
            for derived_scope in Scope:
                result = admin_platform_parent_scope_for_seeded_system_role(
                    role_name, derived_scope, apply_override=False
                )
                self.assertEqual(result, derived_scope)

    def test_admin_default_force_root_does_not_apply_to_other_roles(self):
        """Test that ordinary admin_default roles are NOT forced to ROOT."""
        result = admin_platform_parent_scope_for_seeded_system_role(
            "Some Other Role", Scope.DEFAULT, apply_override=True
        )
        self.assertEqual(result, Scope.DEFAULT)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class V2RoleSeedingTests(IdentityRequest):
    """Test V2 role seeding functionality."""

    def setUp(self):
        """Set up V2 role seeding tests."""
        super().setUp()
        self.public_tenant = Tenant.objects.get(tenant_name="public")
        # Clear any existing platform roles and default groups
        PlatformRoleV2.objects.all().delete()
        SeededRoleV2.objects.all().delete()
        GlobalPolicyIdService.clear_shared()

    def tearDown(self):
        """Clean up after tests."""
        GlobalPolicyIdService.clear_shared()
        super().tearDown()

    def test_seed_platform_roles_creates_all_six_roles(self):
        """Test that _seed_platform_roles creates all 6 platform roles (3 scopes × 2 access types)."""
        # Seed default groups first
        seed_group()

        # Seed platform roles
        platform_roles = _seed_platform_roles()

        # Should have 6 platform roles (3 scopes × 2 access types)
        self.assertEqual(len(platform_roles), 6)
        self.assertEqual(PlatformRoleV2.objects.count(), 6)

        # Verify all combinations exist
        for access_type in DefaultAccessType:
            for scope in Scope:
                self.assertIn((access_type, scope), platform_roles)
                platform_role = platform_roles[(access_type, scope)]
                self.assertIsInstance(platform_role, PlatformRoleV2)
                self.assertEqual(platform_role.tenant, self.public_tenant)

    def test_seed_platform_roles_with_correct_names(self):
        """Test that platform roles are created with correct names."""
        seed_group()
        platform_roles = _seed_platform_roles()

        # Test specific role names
        user_default_role = platform_roles[(DefaultAccessType.USER, Scope.DEFAULT)]
        self.assertEqual(user_default_role.name, "User default Platform Role")
        self.assertIn("user access at default scope", user_default_role.description)

        admin_tenant_role = platform_roles[(DefaultAccessType.ADMIN, Scope.TENANT)]
        self.assertEqual(admin_tenant_role.name, "Admin tenant Platform Role")
        self.assertIn("admin access at tenant scope", admin_tenant_role.description)

    def test_seed_platform_roles_auto_creates_default_groups(self):
        """Test that _seed_platform_roles automatically creates default groups if they don't exist."""
        Group.objects.filter(platform_default=True).delete()
        Group.objects.filter(admin_default=True).delete()
        GlobalPolicyIdService.clear_shared()

        # Platform roles seeding should create groups automatically
        platform_roles = _seed_platform_roles()

        # Should have created 6 platform roles
        self.assertEqual(len(platform_roles), 6)
        self.assertEqual(PlatformRoleV2.objects.count(), 6)

        # Default groups should now exist
        self.assertTrue(Group.objects.filter(platform_default=True).exists())
        self.assertTrue(Group.objects.filter(admin_default=True).exists())

    def test_seed_platform_roles_uses_correct_uuids(self):
        """Test that platform roles are created with correct UUIDs from settings."""
        seed_group()
        platform_roles = _seed_platform_roles()

        # Test that root and tenant scope roles use UUIDs from settings
        root_user_role = platform_roles[(DefaultAccessType.USER, Scope.ROOT)]
        self.assertEqual(root_user_role.uuid, UUID(settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID))

        tenant_user_role = platform_roles[(DefaultAccessType.USER, Scope.TENANT)]
        self.assertEqual(tenant_user_role.uuid, UUID(settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID))

        tenant_admin_role = platform_roles[(DefaultAccessType.ADMIN, Scope.TENANT)]
        self.assertEqual(tenant_admin_role.uuid, UUID(settings.SYSTEM_ADMIN_TENANT_ROLE_UUID))

    def test_seed_platform_roles_idempotent(self):
        """Test that _seed_platform_roles can be called multiple times."""
        seed_group()

        # First seeding
        platform_roles_1 = _seed_platform_roles()
        first_count = PlatformRoleV2.objects.count()
        self.assertEqual(first_count, 6)

        # Second seeding should update, not create duplicates
        platform_roles_2 = _seed_platform_roles()
        second_count = PlatformRoleV2.objects.count()
        self.assertEqual(second_count, 6)

        self.assertEqual(platform_roles_1, platform_roles_2)

    def test_seed_v2_role_from_v1(self):
        """Test that V2 roles are created from V1 roles during seeding."""
        # Seed default groups and roles using actual role definitions
        seed_group()
        seed_roles()

        # Get all system V1 roles from the actual seeded roles
        v1_roles = Role.objects.filter(system=True, tenant=self.public_tenant)
        self.assertGreater(v1_roles.count(), 0, "Should have at least one system role")

        # Verify V2 role exists for each V1 role
        for v1_role in v1_roles:
            with self.subTest(role=v1_role.name):
                # V2 role should be created with same UUID
                v2_role = SeededRoleV2.objects.get(uuid=v1_role.uuid)
                self.assertEqual(v2_role.name, v1_role.display_name)
                self.assertEqual(v2_role.tenant, self.public_tenant)
                self.assertEqual(v2_role.v1_source, v1_role)

                # V2 role should have the same permissions as V1 role
                v1_permissions = set(access.permission for access in v1_role.access.all())
                v2_permissions = set(v2_role.permissions.all())
                self.assertEqual(v1_permissions, v2_permissions)

    def test_platform_role_has_seeded_role_as_child(self):
        """Test that platform roles have seeded roles as children after seeding."""
        # Seed default groups and roles
        seed_group()
        seed_roles()

        # Get all platform_default V1 roles
        platform_v1_roles = Role.objects.filter(platform_default=True, tenant=self.public_tenant)
        self.assertGreater(platform_v1_roles.count(), 0, "Should have at least one platform_default role")

        # For each scope, check that the user platform role has appropriate children
        policy_service = GlobalPolicyIdService.shared()

        # Check at least one scope has children
        found_child = False
        for scope in Scope:
            # Get the user platform role for this scope
            platform_role_uuid = platform_v2_role_uuid_for(DefaultAccessType.USER, scope, policy_service)
            platform_role = PlatformRoleV2.objects.get(uuid=platform_role_uuid)

            # Get children of this platform role as SeededRoleV2 instances
            child_uuids = platform_role.children.values_list("uuid", flat=True)
            children = SeededRoleV2.objects.filter(uuid__in=child_uuids)

            if children.exists():
                found_child = True
                # Verify all children have platform_default V1 source
                for child in children:
                    self.assertIsNotNone(child.v1_source, "Child should have a V1 source")
                    self.assertTrue(child.v1_source.platform_default, "V1 source should be platform_default")

        self.assertTrue(found_child, "At least one user platform role should have children")

    def test_admin_platform_role_has_admin_seeded_role_as_child(self):
        """Test that admin platform roles have admin seeded roles as children."""
        # Seed default groups and roles
        seed_group()
        seed_roles()

        # Get all admin_default V1 roles
        admin_v1_roles = Role.objects.filter(admin_default=True, tenant=self.public_tenant)
        self.assertGreater(admin_v1_roles.count(), 0, "Should have at least one admin_default role")

        # For each scope, check that the admin platform role has appropriate children
        policy_service = GlobalPolicyIdService.shared()

        # Check at least one scope has children
        found_child = False
        for scope in Scope:
            # Get the admin platform role for this scope
            admin_platform_role_uuid = platform_v2_role_uuid_for(DefaultAccessType.ADMIN, scope, policy_service)
            admin_platform_role = PlatformRoleV2.objects.get(uuid=admin_platform_role_uuid)

            # Get children of this admin platform role as SeededRoleV2 instances
            child_uuids = admin_platform_role.children.values_list("uuid", flat=True)
            children = SeededRoleV2.objects.filter(uuid__in=child_uuids)

            if children.exists():
                found_child = True
                # Verify all children have admin_default V1 source
                for child in children:
                    self.assertIsNotNone(child.v1_source, "Child should have a V1 source")
                    self.assertTrue(child.v1_source.admin_default, "V1 source should be admin_default")

        self.assertTrue(found_child, "At least one admin platform role should have children")

    @patch("management.role.definer.seed_group")
    def test_seed_platform_roles_calls_seed_group_when_needed(self, mock_seed_group):
        """Test that _seed_platform_roles calls seed_group when default groups are missing."""
        # Configure mock to create the groups
        mock_seed_group.side_effect = lambda: seed_group()

        Group.objects.filter(platform_default=True).delete()
        Group.objects.filter(admin_default=True).delete()
        GlobalPolicyIdService.clear_shared()

        # Seed platform roles
        platform_roles = _seed_platform_roles()

        # seed_group should have been called when DefaultGroupNotAvailableError was raised
        self.assertTrue(mock_seed_group.called)
        self.assertEqual(len(platform_roles), 6)

    def test_v2_role_parents_cleared_when_scope_changes(self):
        """Test that v2_role.parents is cleared when the role's scope changes due to settings."""
        seed_group()

        # First seeding with role in DEFAULT scope (no ROOT_SCOPE_PERMISSIONS or TENANT_SCOPE_PERMISSIONS)
        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            seed_roles()

        policy_service = GlobalPolicyIdService.shared()

        # Get a platform_default role that we can track
        notifications_role = Role.objects.public_tenant_only().get(name="Notifications viewer")
        v2_role = SeededRoleV2.objects.get(uuid=notifications_role.uuid)

        # Verify initial state: v2_role should have DEFAULT scope platform role as parent
        default_platform_role_uuid = platform_v2_role_uuid_for(DefaultAccessType.USER, Scope.DEFAULT, policy_service)
        default_platform_role = PlatformRoleV2.objects.get(uuid=default_platform_role_uuid)

        self.assertCountEqual(
            list(v2_role.parents.all()),
            [default_platform_role],
            "v2_role should have DEFAULT platform role as parent initially",
        )

        # Now change scope by adding a permission to ROOT_SCOPE_PERMISSIONS
        # "notifications:*:*" will match any notifications permission
        with self.settings(ROOT_SCOPE_PERMISSIONS="notifications:*:*", TENANT_SCOPE_PERMISSIONS=""):
            seed_roles()

        # Refresh from database
        v2_role.refresh_from_db()

        # Get the ROOT scope platform role
        root_platform_role_uuid = platform_v2_role_uuid_for(DefaultAccessType.USER, Scope.ROOT, policy_service)
        root_platform_role = PlatformRoleV2.objects.get(uuid=root_platform_role_uuid)

        # Verify: old parent (DEFAULT) should be removed, new parent (ROOT) should be present
        parents = list(v2_role.parents.all())
        self.assertCountEqual(
            parents,
            [root_platform_role],
            "Scope change to ROOT should leave a single ROOT platform parent",
        )

    def test_v2_role_parents_cleared_when_scope_changes_to_tenant(self):
        """Test that v2_role.parents reflect binding scopes when tenant scope is introduced."""
        seed_group()

        # First seeding with role in DEFAULT scope
        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            seed_roles()

        policy_service = GlobalPolicyIdService.shared()

        # Get a platform_default role
        notifications_role = Role.objects.public_tenant_only().get(name="Notifications viewer")
        v2_role = SeededRoleV2.objects.get(uuid=notifications_role.uuid)

        # Verify initial state: DEFAULT scope
        default_platform_role_uuid = platform_v2_role_uuid_for(DefaultAccessType.USER, Scope.DEFAULT, policy_service)
        default_platform_role = PlatformRoleV2.objects.get(uuid=default_platform_role_uuid)

        self.assertCountEqual(list(v2_role.parents.all()), [default_platform_role])

        # notifications:notifications:read becomes TENANT-scoped; integrations:endpoints:read stays DEFAULT.
        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="notifications:*:*"):
            seed_roles()

        v2_role.refresh_from_db()

        # Get the TENANT scope platform role
        tenant_platform_role_uuid = platform_v2_role_uuid_for(DefaultAccessType.USER, Scope.TENANT, policy_service)
        tenant_platform_role = PlatformRoleV2.objects.get(uuid=tenant_platform_role_uuid)

        parents = list(v2_role.parents.all())
        self.assertCountEqual(
            parents,
            [default_platform_role, tenant_platform_role],
            "Mixed TENANT and DEFAULT permissions should yield both platform parents",
        )

    def test_v2_role_seeded_for_unchanged_v1_role(self):
        """Test that V2 role is seeded even when V1 role is unchanged."""
        seed_group()
        seed_roles()

        # Get a V1 role and its V2 counterpart
        v1_role = Role.objects.public_tenant_only().get(name="Notifications viewer")
        v2_role = SeededRoleV2.objects.get(uuid=v1_role.uuid)

        # Delete the V2 role to simulate V2 tables being newly added
        v2_role.delete()
        self.assertFalse(SeededRoleV2.objects.filter(uuid=v1_role.uuid).exists())

        # Seed again - V1 role is unchanged but V2 should be recreated
        seed_roles()

        # V2 role should exist again
        self.assertTrue(SeededRoleV2.objects.filter(uuid=v1_role.uuid).exists())
        v2_role = SeededRoleV2.objects.get(uuid=v1_role.uuid)
        self.assertEqual(v2_role.v1_source, v1_role)

    def test_non_default_role_has_no_parent(self):
        """Test that a role that's neither platform_default nor admin_default has no parents."""
        seed_group()
        seed_roles()

        # Find a role that's neither platform_default nor admin_default
        non_default_role = (
            Role.objects.public_tenant_only().filter(platform_default=False, admin_default=False).first()
        )

        self.assertIsNotNone(non_default_role, "Should have at least one non-default role")

        # Get its V2 counterpart
        v2_role = SeededRoleV2.objects.get(uuid=non_default_role.uuid)

        # Should have no parents
        self.assertEqual(
            v2_role.parents.count(),
            0,
            "Non-default role should have no parents",
        )

    def test_role_without_bindings_does_not_trigger_migration(self):
        """Test that roles without bindings don't trigger migration.

        Migration is triggered based on whether a role has actual bindings in V1 tenants.
        Roles without any bindings (whether default or non-default) don't need migration.
        If a role later gets manual bindings and then changes scope, those bindings WILL
        be migrated.
        """
        seed_group()

        # First seeding with role in DEFAULT scope
        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            seed_roles()

        # Pick a specific non-default role by name from the test seed data
        # "Cost Administrator Local Test" is a known non-default system role with permissions in test data
        non_default_role = Role.objects.public_tenant_only().filter(name="Cost Administrator Local Test").first()
        self.assertIsNotNone(
            non_default_role,
            "Expected 'Cost Administrator Local Test' role to exist in seeded roles",
        )

        # Mock the migration function
        with patch("management.role.definer._migrate_bindings_for_scope_change") as mock_migrate:
            # Change the scope for this role's permissions by moving its permissions to ROOT
            permission_pattern = f"{non_default_role.access.first().permission.application}:*:*"

            # Re-seed with the permission now in ROOT scope
            with self.settings(ROOT_SCOPE_PERMISSIONS=permission_pattern, TENANT_SCOPE_PERMISSIONS=""):
                seed_roles()

            # Verify that migration was NOT called for this role
            # The new _determine_old_scopes checks for actual bindings in V1 tenants.
            # Since this test doesn't create any bindings for this role,
            # _determine_old_scopes returns empty set and migration is skipped.
            migration_called_for_role = any(call[0][0] == non_default_role for call in mock_migrate.call_args_list)
            self.assertFalse(
                migration_called_for_role,
                f"Migration should NOT be called for role {non_default_role.name} "
                "because it has no bindings to migrate (only roles with actual bindings need migration)",
            )

    def test_v2_role_permissions_updated_when_v1_changes(self):
        """Test that V2 role permissions are updated when V1 role permissions change."""
        seed_group()
        seed_roles()

        # Get a V1 role and its V2 counterpart
        v1_role = Role.objects.public_tenant_only().get(name="Notifications viewer")
        v2_role = SeededRoleV2.objects.get(uuid=v1_role.uuid)

        # Record initial permissions
        initial_v2_permissions = set(v2_role.permissions.all())
        self.assertGreater(len(initial_v2_permissions), 0, "Should have initial permissions")

        # Add a new permission to V1 role
        new_permission, _ = Permission.objects.get_or_create(
            permission="test:resource:action",
            tenant=self.public_tenant,
        )
        Access.objects.create(permission=new_permission, role=v1_role, tenant=self.public_tenant)

        # Bump version to trigger update
        v1_role.version += 1
        v1_role.save()

        # Seed again
        seed_roles()

        # V2 role should have the new permission
        v2_role.refresh_from_db()
        v2_permissions = set(v2_role.permissions.all())

        # V2 should now match V1's permissions (which were reset during seeding)
        v1_permissions = set(access.permission for access in v1_role.access.all())
        self.assertEqual(v2_permissions, v1_permissions)


@override_settings(
    REPLICATION_TO_RELATION_ENABLED=True,
    ROOT_SCOPE_PERMISSIONS="advisor:*:*",
    TENANT_SCOPE_PERMISSIONS="subscriptions:*:*",
    SYSTEM_DEFAULT_TENANT_ROLE_UUID="3c9e6f1a-8b2d-4e5c-9a7f-1d3b5c8e2a4f",
    SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID="5e8a2c4f-9d1b-4c7e-8f3a-6d2b9c1e5a7f",
    SYSTEM_ADMIN_TENANT_ROLE_UUID="a7f3c8b2-1d4e-4f9a-8c6d-2b5e7a9f1c3d",
    SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID="9b4c7e1f-3a5d-4f8c-9e2a-7c1d5b8f3a6e",
)
@patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
class SeedMixedScopeTest(IdentityRequest):
    """Test that seeding a mixed-scope system role attaches to multiple platform parents."""

    def setUp(self):
        super().setUp()
        self.public_tenant = Tenant.objects.get(tenant_name="public")

    def test_seed_mixed_scope_system_role_multiple_platform_parents(self, replicate):
        """A mixed-scope system role generates platform parent relations at each binding scope."""
        tuples = InMemoryTuples()
        replicate.side_effect = InMemoryRelationReplicator(tuples).replicate

        # Create a system role with mixed permissions directly (bypasses full seeding).
        from management.permission.scope_service import ImplicitResourceService

        resource_service = ImplicitResourceService.from_settings()

        role = Role.objects.create(
            name="Mixed Scope Test Role",
            system=True,
            platform_default=True,
            tenant=self.public_tenant,
        )
        # subscriptions → TENANT, advisor → ROOT
        sub_perm, _ = Permission.objects.get_or_create(
            permission="subscriptions:organization:read", tenant=self.public_tenant
        )
        adv_perm, _ = Permission.objects.get_or_create(
            permission="advisor:recommendation:read", tenant=self.public_tenant
        )
        Access.objects.create(role=role, permission=sub_perm, tenant=self.public_tenant)
        Access.objects.create(role=role, permission=adv_perm, tenant=self.public_tenant)

        # Verify the role is mixed TENANT + ROOT
        binding_scopes = resource_service.binding_scopes_for_role(role)
        self.assertEqual(sorted(binding_scopes), sorted([Scope.TENANT, Scope.ROOT]))

        # Replicate as new system role via the SeedingHandler
        handler = SeedingRelationApiDualWriteHandler(role=role, replicator=InMemoryRelationReplicator(tuples))
        handler.replicate_new_system_role()

        # The handler should generate platform parent relations for BOTH scopes.
        # It generates child relations: platform_role#child@this_role
        platform_tenant_uuid = settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID
        platform_root_uuid = settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID

        tenant_parent_tuples = tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_tenant_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(
            len(tenant_parent_tuples),
            1,
            "Mixed-scope role should have TENANT platform parent relation",
        )

        root_parent_tuples = tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_root_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(
            len(root_parent_tuples),
            1,
            "Mixed-scope role should have ROOT platform parent relation",
        )

    def test_existing_tenant_only_role_splits_on_re_seed(self, replicate):
        """A role initially seeded at TENANT-only scope gains a ROOT parent when re-seeded with mixed permissions."""
        tuples = InMemoryTuples()
        replicate.side_effect = InMemoryRelationReplicator(tuples).replicate

        platform_tenant_uuid = settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID
        platform_root_uuid = settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID

        # Phase 1: create a role with only TENANT-scoped permissions.
        role = Role.objects.create(
            name="Re-seed Split Role",
            system=True,
            platform_default=True,
            tenant=self.public_tenant,
        )
        sub_perm, _ = Permission.objects.get_or_create(
            permission="subscriptions:organization:read", tenant=self.public_tenant
        )
        Access.objects.create(role=role, permission=sub_perm, tenant=self.public_tenant)

        handler = SeedingRelationApiDualWriteHandler(role=role, replicator=InMemoryRelationReplicator(tuples))
        handler.replicate_new_system_role()

        # Only TENANT parent should exist.
        tenant_tuples_before = tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_tenant_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(tenant_tuples_before), 1, "Should start with TENANT parent")

        root_tuples_before = tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_root_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(root_tuples_before), 0, "Should NOT have ROOT parent initially")

        # Phase 2: add a ROOT-scoped permission (advisor) and re-seed.
        adv_perm, _ = Permission.objects.get_or_create(
            permission="advisor:recommendation:read", tenant=self.public_tenant
        )
        Access.objects.create(role=role, permission=adv_perm, tenant=self.public_tenant)

        handler2 = SeedingRelationApiDualWriteHandler(role=role, replicator=InMemoryRelationReplicator(tuples))
        handler2.prepare_for_update()
        handler2.replicate_update_system_role()

        # Now BOTH parents should exist.
        tenant_tuples_after = tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_tenant_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(tenant_tuples_after), 1, "Should still have TENANT parent after split")

        root_tuples_after = tuples.find_tuples(
            all_of(
                resource("rbac", "role", platform_root_uuid),
                relation("child"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(root_tuples_after), 1, "Should now also have ROOT parent after split")
