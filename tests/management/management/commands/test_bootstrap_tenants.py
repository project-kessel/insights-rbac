import datetime
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command, CommandError
from django.test.utils import override_settings

from management.group.definer import seed_group
from management.group.platform import GlobalPolicyIdService
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.tenant_mapping.model import TenantMapping
from management.tenant_service import V2TenantBootstrapService
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    all_of,
    resource,
    one_of,
    relation,
    RelationPredicate,
    subject,
)
from tests.management.role.test_dual_write import DualWriteTestCase

from api.models import Tenant
from management.models import Group, Role


@override_settings(
    V2_BOOTSTRAP_TENANT=True,
    REPLICATON_TO_RELATION_ENABLED=True,
    ROOT_SCOPE_PERMISSIONS="root:*:*",
    TENANT_SCOPE_PERMISSIONS="tenant:*:*",
)
class TestBootstrapTenants(DualWriteTestCase):
    def setUp(self):
        Tenant.objects.exclude(tenant_name="public").delete()
        super().setUp()

    def _invoke(self, *args):
        call_command("bootstrap_tenants", *args)

    # Using this allows updating self.tuples without having to re-update the replicate mock's side effect.
    def _tuples_replicate(self, *args, **kwargs):
        return InMemoryRelationReplicator(self.tuples).replicate(*args, **kwargs)

    def _remove_tuples(self, predicate: RelationPredicate):
        self.tuples = InMemoryTuples(set(self.tuples) - set(self.tuples.find_tuples(predicate)))

    def test_no_args(self):
        self.assertRaisesMessage(
            CommandError,
            "Must either specify --all to bootstrap all tenants or use --org-id to specify one or more tenants to bootstrap.",
            self._invoke,
        )

    def test_both(self):
        self.assertRaisesMessage(
            CommandError,
            "Must either specify --all to bootstrap all tenants or use --org-id to specify one or more tenants to bootstrap.",
            self._invoke,
            "--all",
            "--org-id=12345",
        )

    def _do_test_simple_bootstrap(self, invocation: list[str], created_org_id="12345"):
        with patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate") as replicate:
            replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

            self.switch_tenant(self.fixture.new_unbootstrapped_tenant(org_id=created_org_id))
            self._invoke(*invocation)

            policy_service = GlobalPolicyIdService()

            # Trivially check that the tenant was bootstrapped.
            self.expect_1_role_binding_to_workspace(
                workspace=self.default_workspace(),
                for_v2_roles=[str(policy_service.platform_default_policy_uuid())],
                for_groups=[str(self.tenant.tenant_mapping.default_group_uuid)],
            )

    def test_all(self):
        self._do_test_simple_bootstrap(["--all"])

    def test_single(self):
        self._do_test_simple_bootstrap(["--org-id=12345"], created_org_id="12345")

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_bulk(self, replicate):
        replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        tenants = [self.fixture.new_unbootstrapped_tenant(org_id=f"test-{i}") for i in range(100)]
        self._invoke(["--all"])

        policy_service = GlobalPolicyIdService()

        # Trivially check that the tenant was bootstrapped.
        for tenant in tenants:
            self.expect_1_role_binding_to_workspace(
                workspace=self.default_workspace(tenant),
                for_v2_roles=[str(policy_service.platform_default_policy_uuid())],
                for_groups=[str(tenant.tenant_mapping.default_group_uuid)],
            )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_bulk_fallback(self, replicate):
        replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        tenants = [self.fixture.new_unbootstrapped_tenant(org_id=f"test-{i}") for i in range(100)]

        # This unfortunately depends on implementation details, but it is worthwhile to test the fallback to single
        # bootstrapping.
        with patch("management.tenant_service.v2.V2TenantBootstrapService.bootstrap_tenants") as bootstrap_tenants:
            bootstrap_tenants.side_effect = NotImplementedError("no")
            self._invoke(["--all"])

        policy_service = GlobalPolicyIdService()

        # Trivially check that the tenant was bootstrapped.
        for tenant in tenants:
            self.expect_1_role_binding_to_workspace(
                workspace=self.default_workspace(tenant),
                for_v2_roles=[str(policy_service.platform_default_policy_uuid())],
                for_groups=[str(tenant.tenant_mapping.default_group_uuid)],
            )

    def test_missing(self):
        self.assertRaisesMessage(
            CommandError,
            "The following org IDs were requested to be bootstrapped but were not found: {'nonexistent'}",
            self._invoke,
            f"--org-id={self.tenant.org_id}",  # Include a real org_id to check that it's not included as missing.
            "--org-id=nonexistent",
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_force_required_if_bootstrapped(self, replicate):
        replicate.side_effect = self._tuples_replicate
        self.tuples.clear()

        # The existing tenant is already bootstrapped, so nothing should happen here.
        self._invoke("--org-id", self.tenant.org_id)
        self.assertEqual(len(self.tuples), 0)

        # Force bootstrapping should actually replicate something.
        self._invoke(f"--org-id", self.tenant.org_id, "--force")
        self.assertGreater(len(self.tuples), 0)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_fix_custom_default_group(self, replicate):
        replicate.side_effect = self._tuples_replicate

        policy_service = GlobalPolicyIdService()

        self.switch_tenant(self.fixture.new_unbootstrapped_tenant(org_id="12345"))

        default_group = Group.objects.create(
            tenant=self.tenant,
            name="Custom default access",
            platform_default=True,
            system=False,
        )

        # Note that we can't use self.fixture.bootstrap_tenant because self.fixture uses a V2TenantBootstrapService
        # with a NoopReplicator.
        V2TenantBootstrapService(replicator=OutboxReplicator()).bootstrap_tenant(self.tenant)
        self.assertEqual(default_group.uuid, self.tenant.tenant_mapping.default_group_uuid)

        # Remove the custom default group (thus restoring default access).
        self.given_group_removed(default_group)

        initial_tuples = set(self.tuples)

        def assert_default_access(num: int):
            self.expect_role_bindings_to_workspace(
                num=num,
                workspace=self.default_workspace(),
                for_v2_roles=[str(policy_service.platform_default_policy_uuid())],
                for_groups=[str(self.tenant.tenant_mapping.default_group_uuid)],
            )

        # Initially, default access in the default workspace should be present.
        assert_default_access(1)

        # In a previous version of RBAC, there was a bug [0] (fixed at [1]) where only the (default workspace,
        # role binding) relation would be re-created when attempting to restore default access after a custom default
        # group is deleted.
        #
        # Here, we recreate that scenario.
        #
        # This bug only existed before org-level permissions were implemented, so we are only interested in access in
        # the default workspace scope.
        #
        # [0] https://issues.redhat.com/browse/RHCLOUD-42333
        # [1] https://github.com/project-kessel/insights-rbac/pull/1982

        self._remove_tuples(
            all_of(
                resource("rbac", "role_binding", str(self.tenant.tenant_mapping.default_role_binding_uuid)),
                one_of(
                    relation("role"),
                    relation("subject"),
                ),
            )
        )

        # Check that we actually removed two tuples (and that this removed the role binding).
        self.assertEqual(len(initial_tuples) - 2, len(self.tuples))
        assert_default_access(0)

        # Force bootstrapping the tenant should actually restore the correct default access.
        self._invoke(f"--org-id={self.tenant.org_id}", "--force")

        # The role binding we broke should now be restored, as well as all removed tuples.
        assert_default_access(1)
        self.assertEqual(initial_tuples, set(self.tuples))

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_add_org_level_permissions(self, replicate):
        replicate.side_effect = self._tuples_replicate

        self.switch_tenant(self.fixture.new_unbootstrapped_tenant(org_id="12345"))
        V2TenantBootstrapService(replicator=OutboxReplicator()).bootstrap_tenant(self.tenant)

        initial_tuples = set(self.tuples)
        mapping: TenantMapping = self.tenant.tenant_mapping

        def assert_scoped_default_access(num: int):
            self.expect_role_bindings_to_workspace(
                num,
                workspace=self.root_workspace(),
                for_v2_roles=[settings.SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID],
                for_groups=[str(mapping.default_group_uuid)],
            )

            self.expect_role_bindings_to_workspace(
                num,
                workspace=self.root_workspace(),
                for_v2_roles=[settings.SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID],
                for_groups=[str(mapping.default_admin_group_uuid)],
            )

            self.expect_role_bindings_to_tenant(
                num,
                org_id=self.tenant.org_id,
                for_v2_roles=[settings.SYSTEM_DEFAULT_TENANT_ROLE_UUID],
                for_groups=[str(mapping.default_group_uuid)],
            )

            self.expect_role_bindings_to_tenant(
                num,
                org_id=self.tenant.org_id,
                for_v2_roles=[settings.SYSTEM_ADMIN_TENANT_ROLE_UUID],
                for_groups=[str(mapping.default_admin_group_uuid)],
            )

        # A newly-bootstrapped tenant should have default access set up in all scopes.
        assert_scoped_default_access(1)

        # Set the relations to what they would be if this tenant had been bootstrapped before org-level permissions
        # were implemented.
        for role_binding_uuid in [
            mapping.root_scope_default_role_binding_uuid,
            mapping.root_scope_default_admin_role_binding_uuid,
            mapping.tenant_scope_default_role_binding_uuid,
            mapping.tenant_scope_default_admin_role_binding_uuid,
        ]:
            self._remove_tuples(
                one_of(
                    resource("rbac", "role_binding", str(role_binding_uuid)),
                    subject("rbac", "role_binding", str(role_binding_uuid)),
                )
            )

        # Check that this actually removed the expected role bindings (3 tuples per binding, for a total of 12).
        assert_scoped_default_access(0)
        self.assertEqual(len(initial_tuples) - 12, len(self.tuples))

        # Forcibly bootstrapping the tenant should create the default access role bindings in non-default scope.
        self._invoke(f"--org-id={self.tenant.org_id}", "--force")

        # Check that the role bindings were actually created and that we restored all removed tuples.
        assert_scoped_default_access(1)
        self.assertEqual(initial_tuples, set(self.tuples))
