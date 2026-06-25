import datetime
from collections.abc import Iterable
from datetime import timezone

from django.test import TestCase, override_settings
from internal.migrations.replicate_workspaces import replicate_default_workspaces, replicate_updated_workspaces
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    ReplicationEventType,
    WorkspaceEvent,
    WorkspaceEventStream,
)
from management.tenant_service import V2TenantBootstrapService
from management.workspace.model import Workspace
from management.workspace.service import WorkspaceService
from tests.v2_util import WorkspaceCacheReplicator, bootstrap_tenant_for_v2_test

from api.models import Tenant


def _bulk_bootstrapped_tenants(count: int) -> list[Tenant]:
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


@override_settings(ATOMIC_RETRY_DISABLED=True)
class ReplicateDefaultWorkspacesTest(TestCase):
    def setUp(self):
        super().setUp()

        # We are performing operations that depend on all tenants, so we need to exactly control which tenants exist.
        Tenant.objects.exclude(tenant_name="public").delete()

    def test_replication(self):
        tenants = _bulk_bootstrapped_tenants(1000)

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
        _bulk_bootstrapped_tenants(1000)

        replicator = WorkspaceCacheReplicator(NoopReplicator())

        replicate_default_workspaces(replicator=replicator, limit=500)

        self.assertEqual(len(replicator.workspace_events_for(WorkspaceEventStream.STANDARD)), 0)
        self.assertEqual(len(replicator.workspace_events_for(WorkspaceEventStream.BULK)), 500)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class ReplicateUpdatedWorkspacesTest(TestCase):
    def setUp(self):
        super().setUp()

        # We are performing operations that depend on all tenants, so we need to exactly control which tenants exist.
        Tenant.objects.exclude(tenant_name="public").delete()

        self.tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="an_org")
        bootstrap_tenant_for_v2_test(self.tenant)

        Workspace.objects.filter(tenant=self.tenant).update(
            created="2026-06-24T00:00:00Z", modified="2026-06-24T00:00:00Z"
        )

        self.default_workspace = Workspace.objects.default(tenant=self.tenant)
        self.workspace = WorkspaceService(NoopReplicator()).create({"name": "a workspace"}, request_tenant=self.tenant)

        self.workspace.created = "2026-06-25T00:00:00Z"
        self.workspace.modified = "2026-06-25T00:00:00Z"
        self.workspace.save()

    def _do_replicate(self, **kwargs) -> list[WorkspaceEvent]:
        replicator = WorkspaceCacheReplicator(NoopReplicator())
        replicate_updated_workspaces(replicator=replicator, **kwargs)

        self.assertEqual(len(replicator.workspace_events_for(WorkspaceEventStream.BULK)), 0)
        return replicator.workspace_events_for(WorkspaceEventStream.STANDARD)

    def _assert_event_ids(self, events: list[WorkspaceEvent], ids: Iterable[str]):
        ids = set(ids)

        self.assertCountEqual(
            [
                (type, id)
                for id in ids
                for type in [ReplicationEventType.CREATE_WORKSPACE, ReplicationEventType.UPDATE_WORKSPACE]
            ],
            [(event.event_type, event.workspace["id"]) for event in events],
        )

        # We also need to check that each create event precedes the corresponding update event.
        ids_created: set[str] = set()

        for event in events:
            if event.event_type == ReplicationEventType.CREATE_WORKSPACE:
                ids_created.add(event.workspace["id"])
            elif event.event_type == ReplicationEventType.UPDATE_WORKSPACE:
                self.assertIn(event.workspace["id"], ids_created)
            else:
                self.fail(f"Unexpected event type: {event.event_type}")

        # Final paranoid check
        self.assertEqual(ids_created, ids)

    def test_replication(self):
        events = self._do_replicate(since=datetime.datetime.fromisoformat("2026-06-23T00:00:00Z"))
        self._assert_event_ids(events, [str(self.default_workspace.id), str(self.workspace.id)])

    def test_exclude_past_modified(self):
        events = self._do_replicate(since=datetime.datetime.fromisoformat("2026-06-24T12:00:00Z"))
        self._assert_event_ids(events, [str(self.workspace.id)])

    def test_exclude_unmodified_default_workspace(self):
        events = self._do_replicate(
            since=datetime.datetime.fromisoformat("2026-06-23T00:00:00Z"), exclude_unchanged_default_workspaces=True
        )

        self._assert_event_ids(events, [str(self.workspace.id)])

    def test_include_modified_default_workspace(self):
        self.default_workspace.modified = "2026-06-25T00:00:00Z"
        self.default_workspace.save()

        events = self._do_replicate(
            since=datetime.datetime.fromisoformat("2026-06-24T12:00:00Z"), exclude_unchanged_default_workspaces=True
        )

        self._assert_event_ids(events, [str(self.default_workspace.id), str(self.workspace.id)])
