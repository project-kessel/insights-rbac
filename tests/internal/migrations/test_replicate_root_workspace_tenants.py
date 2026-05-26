from django.test import TestCase, override_settings

from api.models import Tenant
from internal.migrations.replicate_root_workspace_tenants import replicate_root_workspace_tenants
from management.relation_replicator.noop_replicator import NoopReplicator
from management.tenant_service import V2TenantBootstrapService
from management.models import Workspace
from migration_tool.in_memory_tuples import (
    InMemoryTuples,
    InMemoryRelationReplicator,
    resource,
    relation,
    subject,
    all_of,
)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class ReplicateRootWorkspacesTest(TestCase):
    def setUp(self):
        self.tuples = InMemoryTuples()

        # We will need to know exactly what tenants exist, so delete all non-public tenants.
        Tenant.objects.exclude(tenant_name="public").delete()

    def test_replicate_root_workspace_tenants(self):
        bootstrap_service = V2TenantBootstrapService(replicator=NoopReplicator())

        tenant_a = bootstrap_service.new_bootstrapped_tenant(org_id="a").tenant
        tenant_b = bootstrap_service.new_bootstrapped_tenant(org_id="b").tenant

        replicate_root_workspace_tenants(replicator=InMemoryRelationReplicator(self.tuples))

        self.assertEqual(len(self.tuples), 2)

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(Workspace.objects.root(tenant_a).id)),
                    relation("tenant"),
                    subject("rbac", "tenant", tenant_a.tenant_resource_id()),
                )
            ),
        )

        self.assertEqual(
            1,
            self.tuples.count_tuples(
                all_of(
                    resource("rbac", "workspace", str(Workspace.objects.root(tenant_b).id)),
                    relation("tenant"),
                    subject("rbac", "tenant", tenant_b.tenant_resource_id()),
                )
            ),
        )

    def test_replicate_unbootstrapped(self):
        Tenant.objects.create(tenant_name="unbootstrapped tenant", org_id="unbootstrapped")

        with self.assertRaises(RuntimeError) as ctx:
            replicate_root_workspace_tenants(NoopReplicator())

        self.assertIn("bootstrap", str(ctx.exception))
