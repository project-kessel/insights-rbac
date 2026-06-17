from internal.migrations.recompute_role_bindings import recompute_tenant_role_bindings
from management.group.definer import seed_group
from management.group.platform import GlobalPolicyIdService
from management.permission.scope_service import Scope
from management.role.definer import seed_roles
from management.role.model import BindingMapping
from management.role.platform import platform_v2_role_uuid_for
from management.role.v2_model import CustomRoleV2
from management.role_binding.model import RoleBinding
from management.role_binding.service import RoleBindingService
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from migration_tool.in_memory_tuples import InMemoryRelationReplicator, all_of, relation, subject, resource
from tests.management.role.test_dual_write import DualWriteTestCase
from django.test import override_settings

from tests.util import assert_v1_v2_tuples_fully_consistent
from tests.v2_util import bootstrap_tenant_for_v2_test


@override_settings(
    ATOMIC_RETRY_DISABLED=True,
    TENANT_SCOPE_PERMISSIONS="",
    ROOT_SCOPE_PERMISSIONS="",
    V2_BOOTSTRAP_TENANT=True,
    REPLICATION_TO_RELATION_ENABLED=True,
)
class RecomputeRoleBindingsTest(DualWriteTestCase):
    def _do_recompute(self):
        recompute_tenant_role_bindings(tenant=self.tenant, replicator=InMemoryRelationReplicator(self.tuples))

    def _do_test_recompute(self, delete_custom: bool, delete_v2: bool):
        system_role = self.given_v1_system_role("system role", ["rbac:*:*"])
        custom_role = self.given_v1_role("custom role", default=["rbac:*:*"])

        group, _ = self.given_group("group", ["p1"])

        def assert_assignments():
            self.assertEqual(1, BindingMapping.objects.filter(role=system_role).count())
            self.assertEqual(1, BindingMapping.objects.filter(role=custom_role).count())

            self.assertEqual(1, RoleBinding.objects.filter(role__v1_source=system_role).count())
            self.assertEqual(1, RoleBinding.objects.filter(role__v1_source=custom_role).count())

            self.expect_1_role_binding_to_workspace(
                self.default_workspace(),
                for_v2_roles=[str(system_role.uuid)],
                for_groups=[
                    str(group.uuid),
                ],
            )

            self.expect_1_role_binding_to_workspace(
                self.default_workspace(),
                for_v2_roles=[str(CustomRoleV2.objects.get(v1_source=custom_role).uuid)],
                for_groups=[str(group.uuid)],
            )

        self.given_roles_assigned_to_group(group, [system_role, custom_role])
        assert_assignments()

        target_role = custom_role if delete_custom else system_role

        if delete_v2:
            _, deleted_counts = RoleBinding.objects.filter(role__v1_source=target_role).delete()
            self.assertEqual(deleted_counts["management.RoleBinding"], 1)
        else:
            _, deleted_counts = BindingMapping.objects.filter(role=target_role).delete()
            self.assertEqual(deleted_counts["management.BindingMapping"], 1)

        self._do_recompute()

        assert_assignments()
        assert_v1_v2_tuples_fully_consistent(test=self, tuples=self.tuples)

    def test_recompute_system_missing_v1(self):
        self._do_test_recompute(delete_custom=False, delete_v2=False)

    def test_recompute_custom_missing_v1(self):
        self._do_test_recompute(delete_custom=True, delete_v2=False)

    def test_recompute_system_missing_v2(self):
        self._do_test_recompute(delete_custom=False, delete_v2=True)

    def test_recompute_custom_missing_v2(self):
        self._do_test_recompute(delete_custom=True, delete_v2=True)

    def _do_test_preserve_default_access(self, *, custom_default_group: bool):
        # Create default access roles and groups, which are needed by RoleBindingService to create default access
        # bindings.
        seed_roles()
        seed_group()

        bootstrap_tenant_for_v2_test(self.tenant, tuples=self.tuples)

        tenant_mapping: TenantMapping = self.tenant.tenant_mapping
        replicator = InMemoryRelationReplicator(self.tuples)

        def assert_default_binding_models_exist(value: bool):
            builtin_binding_ids = {
                tenant_mapping.default_role_binding_uuid_for(access_type, scope)
                for access_type in DefaultAccessType
                for scope in Scope
                if not (custom_default_group and access_type == DefaultAccessType.USER)
            }

            self.assertEqual(
                RoleBinding.objects.filter(tenant=self.tenant).filter(uuid__in=builtin_binding_ids).distinct().count(),
                len(builtin_binding_ids) if value else 0,
            )

        def assert_default_binding_tuples_exist():
            policy_service = GlobalPolicyIdService()

            for access_type in DefaultAccessType:
                for scope in Scope:
                    binding_id = str(tenant_mapping.default_role_binding_uuid_for(access_type, scope))
                    role_id = str(platform_v2_role_uuid_for(access_type, scope, policy_service))

                    self.assertEqual(
                        (0 if (access_type == DefaultAccessType.USER and custom_default_group) else 1),
                        self.tuples.count_tuples(
                            all_of(
                                relation("binding"),
                                subject("rbac", "role_binding", binding_id),
                            )
                        ),
                    )

                    self.assertEqual(
                        1,
                        self.tuples.count_tuples(
                            all_of(
                                resource("rbac", "role_binding", binding_id),
                                relation("subject"),
                            )
                        ),
                    )

                    self.assertEqual(
                        1,
                        self.tuples.count_tuples(
                            all_of(
                                resource("rbac", "role_binding", binding_id),
                                relation("role"),
                                subject("rbac", "role", role_id),
                            )
                        ),
                    )

        if custom_default_group:
            self.given_custom_default_group()

        assert_default_binding_models_exist(False)
        assert_default_binding_tuples_exist()

        # Solely for side-effect of creating RoleBinding models for built-in role bindings.
        RoleBindingService(tenant=self.tenant, replicator=replicator).get_role_bindings_by_subject(
            {"resource_type": "workspace", "resource_id": self.default_workspace()}
        )

        assert_default_binding_models_exist(True)
        assert_default_binding_tuples_exist()

        self._do_recompute()

        assert_default_binding_models_exist(True)
        assert_default_binding_tuples_exist()

    def test_preserve_default_access(self):
        """Test that RoleBindings for built-in bindings are not removed when recomputing."""
        self._do_test_preserve_default_access(custom_default_group=False)

    def test_preserve_default_access_with_default_group(self):
        self._do_test_preserve_default_access(custom_default_group=True)
