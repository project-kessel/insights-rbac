#
# Copyright 2024 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Test that system workspaces have default descriptions."""

from api.models import Tenant
from management.models import Workspace
from management.tenant_service.v2 import V2TenantBootstrapService
from migration_tool.in_memory_tuples import InMemoryRelationReplicator
from tests.identity_request import IdentityRequest


class SystemWorkspaceDescriptionsTest(IdentityRequest):
    """Test that system workspaces are created with default descriptions."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.replicator = InMemoryRelationReplicator()
        self.bootstrap_service = V2TenantBootstrapService(replicator=self.replicator)

    def test_root_workspace_has_default_description(self):
        """Test that root workspace is created with a default description."""
        tenant = Tenant.objects.create(org_id="test-org-root", account_id="test-account")
        self.bootstrap_service.bootstrap_tenant(tenant)

        root = Workspace.objects.get(tenant=tenant, type=Workspace.Types.ROOT)
        self.assertIsNotNone(root.description)
        self.assertEqual(root.description, Workspace.SpecialDescriptions.ROOT)
        self.assertIn("top-level", root.description.lower())

    def test_default_workspace_has_default_description(self):
        """Test that default workspace is created with a default description."""
        tenant = Tenant.objects.create(org_id="test-org-default", account_id="test-account")
        self.bootstrap_service.bootstrap_tenant(tenant)

        default = Workspace.objects.get(tenant=tenant, type=Workspace.Types.DEFAULT)
        self.assertIsNotNone(default.description)
        self.assertEqual(default.description, Workspace.SpecialDescriptions.DEFAULT)
        self.assertIn("default", default.description.lower())

    def test_ungrouped_hosts_workspace_has_default_description(self):
        """Test that ungrouped hosts workspace is created with a default description."""
        tenant = Tenant.objects.create(org_id="test-org-ungrouped", account_id="test-account")
        self.bootstrap_service.bootstrap_tenant(tenant)

        ungrouped = self.bootstrap_service.create_ungrouped_workspace(tenant.org_id)
        self.assertIsNotNone(ungrouped.description)
        self.assertEqual(ungrouped.description, Workspace.SpecialDescriptions.UNGROUPED_HOSTS)
        self.assertIn("ungrouped", ungrouped.description.lower())

    def test_standard_workspace_description_is_optional(self):
        """Test that standard workspaces can be created without a description."""
        tenant = Tenant.objects.create(org_id="test-org-standard", account_id="test-account")
        self.bootstrap_service.bootstrap_tenant(tenant)

        default = Workspace.objects.get(tenant=tenant, type=Workspace.Types.DEFAULT)
        standard = Workspace.objects.create(
            name="Test Standard Workspace", tenant=tenant, parent=default, type=Workspace.Types.STANDARD
        )

        self.assertIsNone(standard.description)

    def test_all_system_workspaces_have_unique_descriptions(self):
        """Test that each system workspace type has a unique description."""
        descriptions = [
            Workspace.SpecialDescriptions.ROOT,
            Workspace.SpecialDescriptions.DEFAULT,
            Workspace.SpecialDescriptions.UNGROUPED_HOSTS,
        ]

        # All descriptions should be unique
        self.assertEqual(len(descriptions), len(set(descriptions)))

        # All descriptions should be non-empty strings
        for desc in descriptions:
            self.assertIsInstance(desc, str)
            self.assertGreater(len(desc), 0)

    def test_manually_created_system_workspaces_get_auto_description(self):
        """Test that manually created system workspaces automatically get descriptions."""
        tenant = Tenant.objects.create(org_id="test-manual-workspace", account_id="test-account")

        # Manually create a root workspace without specifying description
        root = Workspace.objects.create(name="Manual Root", tenant=tenant, type=Workspace.Types.ROOT)
        self.assertEqual(root.description, Workspace.SpecialDescriptions.ROOT)

        # Manually create a default workspace without specifying description
        default = Workspace.objects.create(
            name="Manual Default", tenant=tenant, type=Workspace.Types.DEFAULT, parent=root
        )
        self.assertEqual(default.description, Workspace.SpecialDescriptions.DEFAULT)

        # Manually create an ungrouped-hosts workspace without specifying description
        ungrouped = Workspace.objects.create(
            name="Manual Ungrouped", tenant=tenant, type=Workspace.Types.UNGROUPED_HOSTS, parent=default
        )
        self.assertEqual(ungrouped.description, Workspace.SpecialDescriptions.UNGROUPED_HOSTS)

    def test_manually_set_description_not_overridden(self):
        """Test that manually set descriptions on system workspaces are not overridden."""
        tenant = Tenant.objects.create(org_id="test-custom-description", account_id="test-account")

        # Create a root workspace with a custom description
        custom_root_desc = "My custom root description"
        root = Workspace.objects.create(
            name="Custom Root", tenant=tenant, type=Workspace.Types.ROOT, description=custom_root_desc
        )
        self.assertEqual(root.description, custom_root_desc)  # Should keep custom description, not auto-set

        # Create a default workspace with a custom description
        custom_default_desc = "My custom default description"
        default = Workspace.objects.create(
            name="Custom Default",
            tenant=tenant,
            type=Workspace.Types.DEFAULT,
            parent=root,
            description=custom_default_desc,
        )
        self.assertEqual(default.description, custom_default_desc)

        # Create an ungrouped-hosts workspace with a custom description
        custom_ungrouped_desc = "My custom ungrouped description"
        ungrouped = Workspace.objects.create(
            name="Custom Ungrouped",
            tenant=tenant,
            type=Workspace.Types.UNGROUPED_HOSTS,
            parent=default,
            description=custom_ungrouped_desc,
        )
        self.assertEqual(ungrouped.description, custom_ungrouped_desc)
