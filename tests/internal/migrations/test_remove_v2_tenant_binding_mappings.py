from unittest.mock import patch

from django.test import override_settings

from internal.migrations.remove_v2_tenant_binding_mappings import remove_v2_tenant_binding_mappings
from management.role.model import BindingMapping
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from migration_tool.in_memory_tuples import InMemoryRelationReplicator
from tests.management.role.test_dual_write import DualWriteTestCase


@override_settings(ATOMIC_RETRY_DISABLED=True)
class TestRemoveV2TenantBindingMappings(DualWriteTestCase):
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_migration(self, mock_replicate):
        mock_replicate.side_effect = InMemoryRelationReplicator(self.tuples).replicate

        system_role = self.given_v1_system_role("system role", ["rbac:*:*"])
        custom_role = self.given_v1_role("custom role", default=["rbac:*:*"], **{self.ws_1_id: ["inventory:*:*"]})

        group, _ = self.given_group("group", ["p1"])
        self.given_roles_assigned_to_group(group, [system_role, custom_role])

        mapping_query = BindingMapping.objects.filter(role__in=[system_role, custom_role])

        prior_mappings = list(mapping_query.all())
        self.assertEqual(len(prior_mappings), 3)

        ensure_v2_write_activated(self.tenant)

        # Emulate the BindingMappings not being removed during an old version of V1-to-V2 conversion.
        for mapping in prior_mappings:
            mapping.save(force_insert=True)

        self.assertEqual(mapping_query.all().count(), 3)
        initial_tuples = set(self.tuples)

        remove_v2_tenant_binding_mappings()

        self.assertEqual(mapping_query.all().count(), 0)
        self.assertSetEqual(set(self.tuples), initial_tuples)
