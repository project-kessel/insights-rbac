from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from api.models import Tenant
from internal.migration_coordination import REPLICATE_ROOT_WORKSPACE_TENANTS_NOTIFY_CHANNEL
from internal.migrations.replicate_root_workspace_tenants import replicate_root_workspace_tenants
from management.inventory_replicator.inventory_replicator import ReplicationEventType
from management.inventory_replicator.noop_replicator import NoopReplicator
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

        wait_patcher = patch("internal.pg_notify_wait.wait_for_pg_notify")
        self.mock_wait_for_pg_notify = wait_patcher.start()
        self.addCleanup(wait_patcher.stop)

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

    def test_waits_for_consumer_after_each_batch(self):
        bootstrap_service = V2TenantBootstrapService(replicator=NoopReplicator())

        bootstrap_service.new_bootstrapped_tenant(org_id="a")
        bootstrap_service.new_bootstrapped_tenant(org_id="b")

        replicator = Mock(wraps=InMemoryRelationReplicator(self.tuples))

        with self.captureOnCommitCallbacks(execute=True):
            replicate_root_workspace_tenants(replicator=replicator)

        events = [c.args[0] for c in replicator.replicate.call_args_list]

        self.assertEqual(
            [(e.event_type, e.event_info["batch_size"], len(e.add)) for e in events],
            [(ReplicationEventType.UPDATE_ROOT_WORKSPACE_TENANTS, 2, 2)],
        )
        self.assertEqual(
            [(c.kwargs["channel"], c.kwargs["expected_payload"]) for c in self.mock_wait_for_pg_notify.call_args_list],
            [(REPLICATE_ROOT_WORKSPACE_TENANTS_NOTIFY_CHANNEL, events[0].event_info["notify_token"])],
        )

    def test_replicate_unbootstrapped(self):
        Tenant.objects.create(tenant_name="unbootstrapped tenant", org_id="unbootstrapped")

        with self.assertRaises(RuntimeError) as ctx:
            replicate_root_workspace_tenants(NoopReplicator())

        self.assertIn("bootstrap", str(ctx.exception))
