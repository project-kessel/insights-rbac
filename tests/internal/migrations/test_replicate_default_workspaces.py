from django.test import TestCase

from api.models import Tenant
from internal.migrations.replicate_default_workspaces import replicate_default_workspaces
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.relation_replicator import WorkspaceEventStream, ReplicationEventType, PartitionKey
from management.tenant_service import V2TenantBootstrapService
from management.workspace.model import Workspace
from tests.v2_util import WorkspaceCacheReplicator

from django.test import override_settings


@override_settings(ATOMIC_RETRY_DISABLED=True)
class ReplicateDefaultWorkspacesTest(TestCase):
    def _bulk_bootstrapped_tenants(self, count: int) -> list[Tenant]:
        bootstrap_service = V2TenantBootstrapService(NoopReplicator())

        return [
            b.tenant
            for b in bootstrap_service.bootstrap_tenants(
                Tenant.objects.bulk_create(
                    [
                        Tenant(tenant_name=f"test-tenant-{i}", org_id=f"test-tenant-{i}", account_id=f"acct-{i}")
                        for i in range(count)
                    ]
                )
            )
        ]

    def test_replication(self):
        Tenant.objects.exclude(tenant_name="public").delete()
        tenants = self._bulk_bootstrapped_tenants(1000)

        tenants_by_org_id = {t.org_id: t for t in tenants}
        default_workspaces_by_org_id = {
            w.tenant.org_id: w for w in Workspace.objects.filter(type=Workspace.Types.DEFAULT).select_related("tenant")
        }

        replicator = WorkspaceCacheReplicator(NoopReplicator())

        replicate_default_workspaces(replicator=replicator)

        self.assertEqual(len(replicator.workspace_events_for(WorkspaceEventStream.STANDARD)), 0)
        self.assertEqual(len(replicator.workspace_events_for(WorkspaceEventStream.BULK)), len(tenants))

        events = replicator.workspace_events_for(WorkspaceEventStream.BULK)

        self.assertEqual(set(e.org_id for e in events), set(t.org_id for t in tenants))

        for event in events:
            self.assertEqual(event.event_type, ReplicationEventType.CREATE_WORKSPACE)
            self.assertEqual(event.account_number, tenants_by_org_id[event.org_id].account_id)
            self.assertEqual(str(event.partition_key), str(PartitionKey.byEnvironment()))
            self.assertEqual(event.workspace["id"], str(default_workspaces_by_org_id[event.org_id].id))
            self.assertEqual(event.workspace["type"], Workspace.Types.DEFAULT)
            self.assertEqual(event.workspace["name"], Workspace.SpecialNames.DEFAULT)

    def test_replication_limit(self):
        Tenant.objects.exclude(tenant_name="public").delete()
        tenants = self._bulk_bootstrapped_tenants(1000)

        replicator = WorkspaceCacheReplicator(NoopReplicator())

        replicate_default_workspaces(replicator=replicator, limit=500)

        self.assertEqual(len(replicator.workspace_events_for(WorkspaceEventStream.STANDARD)), 0)
        self.assertEqual(len(replicator.workspace_events_for(WorkspaceEventStream.BULK)), 500)
