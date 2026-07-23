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
"""Test the workspace model."""

from api.models import Tenant
from management.models import Workspace
from tests.identity_request import IdentityRequest

from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from rest_framework import serializers


class WorkspaceBaseTestCase(IdentityRequest):
    """Base class for Workspace tests for helpers/shared methods."""

    def manager_assertions_for_type(self, func, expected_data):
        if isinstance(expected_data, list):
            self.assertCountEqual(list(func(tenant=self.tenant)), expected_data)
            self.assertCountEqual(list(func(tenant_id=self.tenant.id)), expected_data)
        else:
            self.assertEqual(func(tenant=self.tenant), expected_data)
            self.assertEqual(func(tenant_id=self.tenant.id), expected_data)

        with self.assertRaises(ValueError) as assertion:
            func()
        self.assertEqual("You must supply either a tenant object or tenant_id value.", str(assertion.exception))

    def manager_assertion_for_descendant_ids(self, result_ids, expected_ids):
        self.assertCountEqual(result_ids, [str(id) for id in expected_ids])


class WorkspaceModelTests(WorkspaceBaseTestCase):
    """Test the workspace model."""

    def setUp(self):
        """Set up the workspace model tests."""
        super().setUp()

    def tearDown(self):
        """Tear down workspace model tests."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    def test_child_parent_relations(self):
        """Test that workspaces can add/have parents as well as children"""
        tenant = Tenant.objects.create(tenant_name="Child/Parent Relations")
        parent = Workspace.objects.create(name="Parent", tenant=tenant, type=Workspace.Types.ROOT)
        child = Workspace.objects.create(name="Child", tenant=tenant, parent=parent, type=Workspace.Types.DEFAULT)
        self.assertEqual(child.parent, parent)
        self.assertEqual(list(parent.children.all()), [child])

    def test_delete_fails_when_children(self):
        """Test that workspaces will not be deleted when children exist"""
        tenant = Tenant.objects.create(tenant_name="Delete Fails With Children")
        parent = Workspace.objects.create(name="Parent", tenant=tenant, type=Workspace.Types.ROOT)
        child = Workspace.objects.create(name="Child", tenant=tenant, parent=parent, type=Workspace.Types.DEFAULT)
        self.assertRaises(ProtectedError, parent.delete)

    def test_ancestors(self):
        """Test ancestors on a workspace"""
        root = Workspace.objects.create(name="Root", tenant=self.tenant, parent=None, type=Workspace.Types.ROOT)
        level_1 = Workspace.objects.create(name="Level 1", tenant=self.tenant, parent=root)
        level_2 = Workspace.objects.create(name="Level 2", tenant=self.tenant, parent=level_1)
        level_3 = Workspace.objects.create(name="Level 3", tenant=self.tenant, parent=level_2)
        level_4 = Workspace.objects.create(name="Level 4", tenant=self.tenant, parent=level_3)
        self.assertCountEqual(level_3.ancestors(), [root, level_1, level_2])

    def test_unique_name_parent(self):
        """"""
        tenant = Tenant.objects.create(tenant_name="Name/Parent uniqueness")
        root = Workspace.objects.create(name="root", tenant=tenant, type=Workspace.Types.ROOT)
        default = Workspace.objects.create(name="default", tenant=tenant, type=Workspace.Types.DEFAULT, parent=root)

        Workspace.objects.create(name="Child", tenant=tenant, parent=default, type=Workspace.Types.STANDARD)

        # Create a child with same name with the same parent
        self.assertRaises(
            ValidationError,
            Workspace.objects.create,
            name="Child",
            tenant=tenant,
            parent=default,
            type=Workspace.Types.STANDARD,
        )
        # Create a child with same name but the different case within the same parent
        self.assertRaises(
            ValidationError,
            Workspace.objects.create,
            name="child",
            tenant=tenant,
            parent=default,
            type=Workspace.Types.STANDARD,
        )

        # Create a child with the same name but different parent
        parent_2 = Workspace.objects.create(
            name="Parent 2", tenant=tenant, type=Workspace.Types.STANDARD, parent=default
        )
        Workspace.objects.create(name="Child", tenant=tenant, parent=parent_2, type=Workspace.Types.STANDARD)

        # If parent is null, it is allowed to have same name
        tenant_2 = Tenant.objects.create(tenant_name="Name/Parent uniqueness 2")
        Workspace.objects.create(name="root", tenant=tenant_2, type=Workspace.Types.ROOT)


class WorkspaceDescendants(WorkspaceBaseTestCase):
    def setUp(self):
        """Set up the workspace descendant tests."""
        self.root = Workspace.objects.create(name="Root", tenant=self.tenant, parent=None, type=Workspace.Types.ROOT)
        self.level_1a = Workspace.objects.create(name="Level 1a", tenant=self.tenant, parent=self.root)
        self.level_2a = Workspace.objects.create(name="Level 2a", tenant=self.tenant, parent=self.level_1a)
        self.level_3a = Workspace.objects.create(name="Level 3a", tenant=self.tenant, parent=self.level_2a)
        self.level_4a = Workspace.objects.create(name="Level 4a", tenant=self.tenant, parent=self.level_3a)
        self.level_4b = Workspace.objects.create(name="Level 4b", tenant=self.tenant, parent=self.level_3a)
        self.level_1b = Workspace.objects.create(name="Level 1b", tenant=self.tenant, parent=self.root)
        self.level_2b = Workspace.objects.create(name="Level 2b", tenant=self.tenant, parent=self.level_1b)
        self.level_3b = Workspace.objects.create(name="Level 3b", tenant=self.tenant, parent=self.level_2b)

        self.t2 = Tenant.objects.create(tenant_name="T2")
        self.t2_root = Workspace.objects.create(name="Root", tenant=self.t2, parent=None, type=Workspace.Types.ROOT)
        self.t2_level_1 = Workspace.objects.create(name="T2 Level 1", tenant=self.t2, parent=self.t2_root)

        super().setUp()

    def test_descendant_ids_with_parents(self):
        """Test returning descendant IDs with parents"""

        self.manager_assertion_for_descendant_ids(
            Workspace.objects.descendant_ids_with_parents([self.root.id], self.tenant.id),
            [
                self.root.id,
                self.level_1a.id,
                self.level_2a.id,
                self.level_3a.id,
                self.level_4a.id,
                self.level_4b.id,
                self.level_1b.id,
                self.level_2b.id,
                self.level_3b.id,
            ],
        )

        self.manager_assertion_for_descendant_ids(
            Workspace.objects.descendant_ids_with_parents([self.level_2a.id], self.tenant.id),
            [self.level_2a.id, self.level_3a.id, self.level_4a.id, self.level_4b.id],
        )

        self.manager_assertion_for_descendant_ids(
            Workspace.objects.descendant_ids_with_parents([self.level_1b.id], self.tenant.id),
            [self.level_1b.id, self.level_2b.id, self.level_3b.id],
        )

        tenant_from_other_org = Tenant.objects.create(tenant_name="foo")
        self.manager_assertion_for_descendant_ids(
            Workspace.objects.descendant_ids_with_parents([self.level_1b.id], tenant_from_other_org.id),
            [],
        )


class Types(WorkspaceBaseTestCase):
    """Test types on a workspace."""

    def setUp(self):
        """Set up the workspace model tests."""
        self.tenant_1_root_workspace = Workspace.objects.create(
            name="T1 Root Workspace", tenant=self.tenant, type=Workspace.Types.ROOT
        )
        self.tenant_1_default_workspace = Workspace.objects.create(
            name="T1 Default Workspace",
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=self.tenant_1_root_workspace,
        )
        self.tenant_1_ungrouped_hosts_workspace = Workspace.objects.create(
            name="T1 Ungrouped Hosts Workspace",
            tenant=self.tenant,
            type=Workspace.Types.UNGROUPED_HOSTS,
            parent=self.tenant_1_root_workspace,
        )
        self.tenant_1_standard_workspace = Workspace.objects.create(
            name="T1 Standard Workspace",
            tenant=self.tenant,
            parent=self.tenant_1_default_workspace,
        )
        super().setUp()

    def tearDown(self):
        """Tear down workspace model tests."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    def test_default_value(self):
        """Test the default value of a workspace type when not supplied"""
        self.assertEqual(self.tenant_1_standard_workspace.type, "standard")

    def test_single_root_per_tenant(self):
        """Test tenant can only have one root workspace"""
        with self.assertRaises(ValidationError) as assertion:
            Workspace.objects.create(name="T1 Root Workspace Number 2", type=Workspace.Types.ROOT, tenant=self.tenant)
        error_messages = assertion.exception.messages
        self.assertEqual(len(error_messages), 1)
        self.assertIn("unique_default_root_workspace_per_tenant", error_messages[0])

    def test_single_default_per_tenant(self):
        """Test tenant can only have one default workspace"""
        with self.assertRaises(ValidationError) as assertion:
            Workspace.objects.create(
                name="T1 Default Workspace Number 2",
                type=Workspace.Types.DEFAULT,
                tenant=self.tenant,
                parent=self.tenant_1_root_workspace,
            )
        error_messages = assertion.exception.messages
        self.assertEqual(len(error_messages), 1)
        self.assertIn("unique_default_root_workspace_per_tenant", error_messages[0])

    def test_single_ungrouped_hosts_per_tenant(self):
        """Test tenant can only have one ungrouped hosts workspace"""
        with self.assertRaises(ValidationError) as assertion:
            Workspace.objects.create(
                name="T1 Ungrouped Hosts Workspace Number 2",
                type=Workspace.Types.UNGROUPED_HOSTS,
                tenant=self.tenant,
                parent=self.tenant_1_root_workspace,
            )
        error_messages = assertion.exception.messages
        self.assertEqual(len(error_messages), 1)
        self.assertIn("unique_default_root_workspace_per_tenant", error_messages[0])

    def test_multiple_specific_ws_multiple_tenants(self):
        """Test that multiple tenants can have more than one root/default/ungrouped-hosts workspace"""
        try:
            tenant_2 = Tenant.objects.create(tenant_name="Tenant 2")
            root = Workspace.objects.create(name="Root Workspace Number 2", type=Workspace.Types.ROOT, tenant=tenant_2)
            default = Workspace.objects.create(
                name="Default Workspace Number 2",
                type=Workspace.Types.DEFAULT,
                tenant=tenant_2,
                parent=root,
            )
            Workspace.objects.create(
                name="Ungrouped Hosts Workspace Number 2",
                type=Workspace.Types.UNGROUPED_HOSTS,
                tenant=tenant_2,
                parent=root,
            )
        except ValidationError as e:
            self.fail("test_multiple_root_and_default_multiple_tenants raised ValidationError unexpectedly")

    def test_multiple_standard_per_tenant(self):
        """Test tenant can have multiple standard workspaces"""
        try:
            for n in ["1", "2", "3"]:
                Workspace.objects.create(
                    name=f"T1 Standard Workspace Number {n}",
                    type=Workspace.Types.STANDARD,
                    tenant=self.tenant,
                    parent=self.tenant_1_default_workspace,
                )
        except ValidationError as e:
            self.fail("test_multiple_standard_per_tenant raised ValidationError unexpectedly")

    def test_standard_can_belong_to_root(self):
        """Test that a standard workspace can belong to a root workspace"""
        try:
            workspace = Workspace.objects.create(
                name=f"T1 Standard Workspace",
                type=Workspace.Types.STANDARD,
                tenant=self.tenant,
                parent=self.tenant_1_root_workspace,
            )
            self.assertEqual(workspace.parent, self.tenant_1_root_workspace)
        except ValidationError as e:
            self.fail("test_standard_can_belong_to_root raised ValidationError unexpectedly")

    def test_invalid_type(self):
        """Test invalid workspace type"""
        invalid_type = "foo"
        with self.assertRaises(ValidationError) as assertion:
            Workspace.objects.create(
                name="Invalid Type Workspace",
                type=invalid_type,
                tenant=self.tenant,
                parent=self.tenant_1_default_workspace,
            )
        error_messages = assertion.exception.messages
        self.assertEqual(len(error_messages), 1)
        self.assertEqual(f"Value '{invalid_type}' is not a valid choice.", error_messages[0])

    def test_default_no_parent(self):
        """Test default workspace creation with no parent"""
        tenant = Tenant.objects.create(tenant_name="Default no parent")
        with self.assertRaises(ValidationError) as assertion:
            Workspace.objects.create(name="Default", type=Workspace.Types.DEFAULT, tenant=tenant)
        self.assertEqual(
            {"workspace": ["default workspaces must have a parent workspace."]},
            assertion.exception.message_dict,
        )

    def test_ungrouped_hosts_no_parent(self):
        """Test ungrouped hosts workspace creation with no parent"""
        tenant = Tenant.objects.create(tenant_name="Ungrouped Hosts no parent")
        with self.assertRaises(ValidationError) as assertion:
            Workspace.objects.create(name="Ungrouped Hosts", type=Workspace.Types.UNGROUPED_HOSTS, tenant=tenant)
        self.assertEqual(
            {"workspace": ["ungrouped-hosts workspaces must have a parent workspace."]},
            assertion.exception.message_dict,
        )

    def test_built_in_types_queryset(self):
        """Test the WorkspaceQuerySet on the Workspace model for built_in."""
        self.manager_assertions_for_type(
            Workspace.objects.built_in, [self.tenant_1_root_workspace, self.tenant_1_default_workspace]
        )

    def test_standard_types_queryset(self):
        """Test the WorkspaceQuerySet on the Workspace model for standard."""
        self.manager_assertions_for_type(Workspace.objects.standard, [self.tenant_1_standard_workspace])

    def test_root_type_manager(self):
        """Test the WorkspaceManager on the Workspace model for root."""
        self.manager_assertions_for_type(Workspace.objects.root, self.tenant_1_root_workspace)

    def test_default_type_manager(self):
        """Test the WorkspaceManager on the Workspace model for default."""
        self.manager_assertions_for_type(Workspace.objects.default, self.tenant_1_default_workspace)

    def test_root_and_default_parent(self):
        """Test root/default workspace creation with parent"""
        tenant = Tenant.objects.create(tenant_name="Root with parent")
        root = Workspace.objects.create(name="Root", type=Workspace.Types.ROOT, tenant=tenant)
        # Default cannot be created without parent
        with self.assertRaises(ValidationError) as assertion:
            Workspace.objects.create(name="Default", type=Workspace.Types.DEFAULT, tenant=tenant)

        default = Workspace.objects.create(name="Default", type=Workspace.Types.DEFAULT, tenant=tenant, parent=root)
        with self.assertRaises(serializers.ValidationError) as assertion:
            root.parent = default
            root.save()
            self.assertEqual(
                {"parent_id": ["Root workspaces cannot have a parent."]},
                assertion.exception.message_dict,
            )
