from unittest.mock import patch

from django.test import override_settings

from internal.migrations.remove_deleted_workspace_bindings import remove_deleted_workspace_bindings
from management.role_binding.model import RoleBinding
from management.role_binding.service import RoleBindingService, CreateBindingRequest
from management.workspace.model import Workspace
from management.workspace.service import WorkspaceService
from migration_tool.in_memory_tuples import InMemoryRelationReplicator, all_of, resource, relation
from tests.management.role.test_dual_write import DualWriteTestCase


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RemoveDeletedWorkspaceBindingsTest(DualWriteTestCase):
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_v2_removal(self, replicate):
        replicator = InMemoryRelationReplicator(self.tuples)
        replicate.side_effect = replicator.replicate

        workspace_service = WorkspaceService()
        binding_service = RoleBindingService(tenant=self.tenant)

        workspace_a = workspace_service.create({"name": "Workspace A"}, request_tenant=self.tenant)
        workspace_b = workspace_service.create({"name": "Workspace B"}, request_tenant=self.tenant)

        system_role = self.given_v1_system_role("system role", ["rbac:*:*"])
        group, _ = self.given_group("a group", ["p1"])

        def expect_ws_binding_count(workspace_id: str, count: int):
            self.assertEqual(
                count,
                self.tuples.count_tuples(
                    all_of(
                        resource("rbac", "workspace", workspace_id),
                        relation("binding"),
                    )
                ),
            )

            self.expect_role_bindings_to_workspace(
                count,
                workspace_id,
                for_v2_roles=[str(system_role.uuid)],
                for_groups=[str(group.uuid)],
            )

            self.assertEqual(
                count,
                RoleBinding.objects.filter(resource_type="workspace", resource_id=workspace_id).count(),
            )

        def expect_binding_counts(a_count: int, b_count: int):
            expect_ws_binding_count(str(workspace_a.id), a_count)
            expect_ws_binding_count(str(workspace_b.id), b_count)

        binding_service.batch_create(
            [
                CreateBindingRequest(
                    role_id=str(system_role.uuid),
                    resource_type="workspace",
                    resource_id=str(ws.id),
                    subject_type="group",
                    subject_id=str(group.uuid),
                )
                for ws in [workspace_a, workspace_b]
            ]
        )

        expect_binding_counts(1, 1)

        # Simulate workspace A being deleted before RoleBindings were removed for it.
        Workspace.objects.filter(pk=workspace_a.pk).delete()

        expect_binding_counts(1, 1)

        remove_deleted_workspace_bindings(replicator=replicator)

        # The binding for the deleted workspace A should have been removed, but the binding for the extant
        # workspace B should still exist.
        expect_binding_counts(0, 1)
