import datetime
import uuid
from collections.abc import Iterable
from unittest.mock import MagicMock

from django.test import TestCase, override_settings
from internal.migrations.replicate_workspaces import (
    replicate_default_workspaces,
    replicate_deleted_workspaces,
    replicate_updated_workspaces,
)
from internal.utils import get_or_create_ungrouped_workspace
from management.audit_log.model import AuditLog
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
from tests.management.role.test_dual_write import DualWriteTestCase
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

        Workspace.objects.filter(pk=self.workspace.id).update(
            created="2026-06-25T00:00:00Z", modified="2026-06-25T00:00:00Z"
        )

    def _do_replicate(self, stream: WorkspaceEventStream, **kwargs) -> list[WorkspaceEvent]:
        replicator = WorkspaceCacheReplicator(NoopReplicator())
        replicate_updated_workspaces(replicator=replicator, stream=stream, **kwargs)

        for possible_stream in WorkspaceEventStream:
            if possible_stream != stream:
                self.assertCountEqual([], replicator.workspace_events_for(possible_stream))

        return replicator.workspace_events_for(stream)

    def _assert_event_ids(self, events: list[WorkspaceEvent], ids: Iterable[str], create_only_ids: Iterable[str] = ()):
        ids = set(ids)
        create_only_ids = set(create_only_ids)

        self.assertTrue(create_only_ids.issubset(ids))

        self.assertCountEqual(
            [
                *((ReplicationEventType.CREATE_WORKSPACE, id) for id in ids),
                *((ReplicationEventType.UPDATE_WORKSPACE, id) for id in (ids - create_only_ids)),
            ],
            [(event.event_type, event.workspace["id"]) for event in events],
        )

        # We also need to check that each create event precedes the corresponding update event.
        ids_created: set[str] = set()
        ids_updated: set[str] = set()

        for event in events:
            if event.event_type == ReplicationEventType.CREATE_WORKSPACE:
                ids_created.add(event.workspace["id"])
            elif event.event_type == ReplicationEventType.UPDATE_WORKSPACE:
                self.assertIn(event.workspace["id"], ids_created)
                ids_updated.add(event.workspace["id"])
            else:
                self.fail(f"Unexpected event type: {event.event_type}")

        # Final paranoid check.
        self.assertEqual(ids_created, ids)
        self.assertEqual(ids_updated, ids - create_only_ids)

    def test_replication(self):
        events = self._do_replicate(
            stream=WorkspaceEventStream.STANDARD,
            since=datetime.datetime.fromisoformat("2026-06-23T00:00:00Z"),
        )

        self._assert_event_ids(events, [str(self.default_workspace.id), str(self.workspace.id)])

    def test_replication_bulk(self):
        events = self._do_replicate(
            stream=WorkspaceEventStream.BULK,
            since=datetime.datetime.fromisoformat("2026-06-23T00:00:00Z"),
        )

        self._assert_event_ids(events, [str(self.default_workspace.id), str(self.workspace.id)])

    def test_exclude_past_modified(self):
        events = self._do_replicate(
            stream=WorkspaceEventStream.STANDARD,
            since=datetime.datetime.fromisoformat("2026-06-24T12:00:00Z"),
        )

        self._assert_event_ids(events, [str(self.workspace.id)])

    def test_exclude_unmodified_default_workspace(self):
        events = self._do_replicate(
            stream=WorkspaceEventStream.STANDARD,
            since=datetime.datetime.fromisoformat("2026-06-23T00:00:00Z"),
            exclude_unchanged_default_workspaces=True,
        )

        self._assert_event_ids(events, [str(self.workspace.id)])

    def test_include_modified_default_workspace(self):
        Workspace.objects.filter(pk=self.default_workspace.pk).update(modified="2026-06-25T00:00:00Z")

        events = self._do_replicate(
            stream=WorkspaceEventStream.STANDARD,
            since=datetime.datetime.fromisoformat("2026-06-24T12:00:00Z"),
            exclude_unchanged_default_workspaces=True,
        )

        self._assert_event_ids(events, [str(self.default_workspace.id), str(self.workspace.id)])

    def test_ungrouped_hosts(self):
        ungrouped_ws = get_or_create_ungrouped_workspace(self.tenant)

        events = self._do_replicate(
            stream=WorkspaceEventStream.STANDARD,
            since=ungrouped_ws.modified,
        )

        self._assert_event_ids(events, [str(ungrouped_ws.id)], create_only_ids=[str(ungrouped_ws.id)])


@override_settings(ATOMIC_RETRY_DISABLED=True)
class ReplicateDeletedWorkspacesTest(DualWriteTestCase):
    def setUp(self):
        super().setUp()

        AuditLog.objects.all().delete()
        self.service = WorkspaceService(NoopReplicator())

    def _make_delete_log(self, name: str) -> AuditLog:
        workspace = self.service.create({"name": name}, self.tenant)

        mock_request = MagicMock()
        mock_request.user.username = "test_user"
        mock_request._user.org_id = self.tenant.org_id

        create_log = AuditLog()
        create_log.log_v2(mock_request, "workspace", AuditLog.CREATE, workspace.id, f"Created workspace: {name}")

        delete_log = AuditLog()
        delete_log.log_v2(mock_request, "workspace", AuditLog.DELETE, workspace.id, f"Deleted workspace: {name}")

        self.service.destroy(workspace)
        return delete_log

    def test_remove(self):
        log_a = self._make_delete_log("a")
        log_b = self._make_delete_log("b")

        self.assertGreater(log_b.created, log_a.created)

        def test_replicate_from(since: datetime.datetime, entries: list[tuple[uuid.UUID, str]]):
            replicator = WorkspaceCacheReplicator(NoopReplicator())
            replicate_deleted_workspaces(since=since, replicator=replicator)

            events = replicator.workspace_events_for(WorkspaceEventStream.BULK)

            self.assertCountEqual([{"id": str(e[0]), "name": e[1]} for e in entries], [e.workspace for e in events])

            self.assertTrue(all(e.org_id == self.tenant.org_id for e in events))
            self.assertTrue(all(e.account_number == self.tenant.account_id for e in events))
            self.assertTrue(all(e.event_type == ReplicationEventType.DELETE_WORKSPACE for e in events))

            self.assertCountEqual([], replicator.workspace_events_for(WorkspaceEventStream.STANDARD))

        test_replicate_from(log_a.created, [(log_a.resource_uuid, "a"), (log_b.resource_uuid, "b")])
        test_replicate_from(log_b.created, [(log_b.resource_uuid, "b")])
        test_replicate_from(log_b.created + datetime.timedelta(minutes=1), [])

    def test_error_on_reused_id(self):
        log = self._make_delete_log("workspace")

        Workspace.objects.create(
            tenant=self.tenant,
            id=log.resource_uuid,
            name="test workspace",
            parent=Workspace.objects.default(tenant=self.tenant),
        )

        with self.assertRaises(AssertionError):
            replicate_deleted_workspaces(since=log.created, replicator=NoopReplicator())
