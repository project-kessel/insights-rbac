from django.test import override_settings
from internal.migrations.migrate_role_scope import migrate_role_scope_if_changed
from management.permission.scope_service import Scope
from management.role.model import RoleScopeState
from migration_tool.in_memory_tuples import InMemoryRelationReplicator
from tests.management.role.test_dual_write import DualWriteTestCase


@override_settings(ATOMIC_RETRY_DISABLED=True, ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="")
class MigrateRoleScopeTest(DualWriteTestCase):
    def setUp(self):
        super().setUp()

        self.role = self.given_v1_system_role("role", ["rbac:*:*"])

        self.group, _ = self.given_group("group", ["p1"])
        self.given_roles_assigned_to_group(self.group, [self.role])

    def _do_migrate(self):
        migrate_role_scope_if_changed(self.role, InMemoryRelationReplicator(self.tuples))

    @override_settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="rbac:*:*")
    def test_migrate_unmigrated_role(self):
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[str(self.role.uuid)],
            for_groups=[str(self.group.uuid)],
        )

        RoleScopeState.objects.create(role=(self.role), version=1, computed_scopes=[Scope.ROOT], migrated=False)

        with self.settings(ROOT_SCOPE_PERMISSIONS="rbac:*:*", TENANT_SCOPE_PERMISSIONS=""):
            self._do_migrate()

        self.role.refresh_from_db()
        self.assertEqual(self.role.scope_state.version, 1)
        self.assertEqual(self.role.scope_state.computed_scopes, [Scope.ROOT])
        self.assertTrue(self.role.scope_state.migrated)

        self.expect_1_role_binding_to_workspace(
            self.root_workspace(),
            for_v2_roles=[str(self.role.uuid)],
            for_groups=[str(self.group.uuid)],
        )

    def test_no_migrate_previously_migrated_role(self):
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[str(self.role.uuid)],
            for_groups=[str(self.group.uuid)],
        )

        RoleScopeState.objects.create(role=self.role, version=1, computed_scopes=[Scope.ROOT], migrated=True)

        with self.settings(ROOT_SCOPE_PERMISSIONS="rbac:*:*", TENANT_SCOPE_PERMISSIONS=""):
            self._do_migrate()

        self.role.refresh_from_db()
        self.assertEqual(self.role.scope_state.version, 1)
        self.assertEqual(self.role.scope_state.computed_scopes, [Scope.ROOT])
        self.assertTrue(self.role.scope_state.migrated)

        # The role was already marked as migrated, so nothing should be done.
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[str(self.role.uuid)],
            for_groups=[str(self.group.uuid)],
        )

    def test_no_migrate_scope_mismatch(self):
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[str(self.role.uuid)],
            for_groups=[str(self.group.uuid)],
        )

        RoleScopeState.objects.create(role=(self.role), version=1, computed_scopes=[Scope.ROOT], migrated=False)

        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS="rbac:*:*"):
            self._do_migrate()

        self.role.refresh_from_db()
        self.assertFalse(self.role.scope_state.migrated)

        # The scopes in the settings will not match the RoleScopeState, so no migration should be performed.
        self.expect_1_role_binding_to_workspace(
            self.default_workspace(),
            for_v2_roles=[str(self.role.uuid)],
            for_groups=[str(self.group.uuid)],
        )

    def test_no_migrate_new_role(self):
        self.assertFalse(RoleScopeState.objects.filter(role=self.role).exists())

        with self.settings(ROOT_SCOPE_PERMISSIONS="", TENANT_SCOPE_PERMISSIONS=""):
            self._do_migrate()

        self.assertFalse(RoleScopeState.objects.filter(role=self.role).exists())

    @override_settings(REPLICATION_TO_RELATION_ENABLED=False)
    def test_no_migrate_without_replication(self):
        self._do_migrate()
        self.assertFalse(RoleScopeState.objects.filter(role=self.role).exists())

    def test_no_migrate_deleted_role(self):
        """Early exit when role is concurrently deleted."""
        role_pk = self.role.pk
        self.role.delete()

        from management.role.model import Role

        deleted_role = Role(pk=role_pk)
        migrate_role_scope_if_changed(deleted_role, InMemoryRelationReplicator(self.tuples))
        self.assertFalse(RoleScopeState.objects.filter(role_id=role_pk).exists())

    def test_raises_for_non_system_role(self):
        """ValueError when a non-system role is passed."""
        self.role.system = False
        self.role.save()

        with self.assertRaises(ValueError):
            self._do_migrate()
