#
# Copyright 2019 Red Hat, Inc.
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
from django.test import TestCase
from django.test.utils import override_settings
from unittest.mock import Mock
from api.models import Tenant
from management.models import Permission, Workspace
from management.role.serializer import RoleSerializer, ResourceDefinitionSerializer

import random


class RoleSerializerTest(TestCase):
    """Test the role serializer"""

    def prepare_serializer(self, role_data):
        serializer = RoleSerializer(data=role_data)
        serializer.is_valid()
        tenant = Tenant.objects.get(tenant_name="public")
        request = Mock()
        request.tenant = tenant
        serializer.context["request"] = request

        return serializer

    def test_create_role_with_none_exist_permission_failed(self):
        """If the permission does not exist, error will be thrown for creating role."""
        # Prepare dict input
        role_data = {
            "name": "RoleA",
            "access": [
                {
                    "permission": "app:*:write",
                    "resourceDefinitions": [
                        {"attributeFilter": {"key": "app.attribute.case", "operation": "equal", "value": "thevalue"}}
                    ],
                }
            ],
        }

        # Serialize the input
        serializer = self.prepare_serializer(role_data)

        # Create the role
        self.assertRaises(Permission.DoesNotExist, serializer.create, serializer.validated_data)

    def test_update_role_with_none_exist_permission_failed(self):
        """If the permission does not exist, error will be thrown for updating role."""
        # Prepare dict input
        role_data = {
            "name": "RoleA",
            "access": [
                {
                    "permission": "app:*:read",
                    "resourceDefinitions": [
                        {"attributeFilter": {"key": "app.attribute.case", "operation": "equal", "value": "thevalue"}}
                    ],
                }
            ],
        }

        # Serialize the input
        serializer = self.prepare_serializer(role_data)
        Permission.objects.create(permission="app:*:read", tenant=Tenant.objects.get(tenant_name="public"))

        # Create the role
        role = serializer.create(serializer.validated_data)

        # Update the role with non exist permission
        role_data["access"][0]["permission"] = "app:*:write"
        serializer = self.prepare_serializer(role_data)
        # Update the role
        self.assertRaises(Permission.DoesNotExist, serializer.update, role, serializer.validated_data)

    def test_create_role_with_invalid_group_id_integer_fails(self):
        """Test that creating a role with integer values in group.id resource definition fails."""
        tenant = Tenant.objects.get(tenant_name="public")
        Permission.objects.create(permission="inventory:groups:read", tenant=tenant)

        # Prepare role data with integer in group.id
        role_data = {
            "name": "Invalid Role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "in",
                                "value": ["valid-uuid-string", 12345, 67890],  # Contains integers
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        serializer = self.prepare_serializer(role_data)

        # Validation should fail
        self.assertFalse(serializer.is_valid())
        self.assertIn("access", serializer.errors)

    def test_create_role_with_invalid_group_id_equal_integer_fails(self):
        """Test that creating a role with integer value in group.id with operation='equal' fails."""
        tenant = Tenant.objects.get(tenant_name="public")
        Permission.objects.create(permission="inventory:groups:read", tenant=tenant)

        # Prepare role data with integer in group.id
        role_data = {
            "name": "Invalid Role Equal",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": 12345,  # Integer value
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        serializer = self.prepare_serializer(role_data)

        # Validation should fail
        self.assertFalse(serializer.is_valid())
        self.assertIn("access", serializer.errors)

    def test_update_role_with_invalid_group_id_integer_fails(self):
        """Test that updating a role with integer values in group.id resource definition fails."""
        tenant = Tenant.objects.get(tenant_name="public")
        Permission.objects.create(permission="inventory:groups:read", tenant=tenant)

        # First create a valid role
        valid_role_data = {
            "name": "Valid Role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "in",
                                "value": ["95473d62-56ea-4c0c-8945-4f3f6a620669"],  # Valid UUID
                            }
                        }
                    ],
                }
            ],
        }
        serializer = self.prepare_serializer(valid_role_data)
        role = serializer.create(serializer.validated_data)

        # Now try to update with invalid data
        invalid_role_data = {
            "name": "Updated Role",
            "access": [
                {
                    "permission": "inventory:groups:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "in",
                                "value": [123, 456],  # Invalid: integers
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the update
        update_serializer = self.prepare_serializer(invalid_role_data)

        # Validation should fail
        self.assertFalse(update_serializer.is_valid())
        self.assertIn("access", update_serializer.errors)


@override_settings(WORKSPACE_HIERARCHY_ENABLED=True)
class ResourceDefinitionTest(TestCase):
    """Test the resource definition serializer"""

    def setUp(self):
        self.tenant = Tenant.objects.get(tenant_name="public")
        self.root_workspace = Workspace.objects.create(
            name="Root", tenant=self.tenant, parent=None, type=Workspace.Types.ROOT
        )
        self.default_workspace = Workspace.objects.create(
            name="Default", tenant=self.tenant, parent=self.root_workspace, type=Workspace.Types.DEFAULT
        )
        self.standard_workspace = Workspace.objects.create(
            name="Standard", tenant=self.tenant, parent=self.default_workspace
        )
        self.sub_workspace_a = Workspace.objects.create(
            name="Sub a", tenant=self.tenant, parent=self.standard_workspace
        )
        self.sub_workspace_b = Workspace.objects.create(
            name="Sub b", tenant=self.tenant, parent=self.standard_workspace
        )

    def test_get_with_inventory_groups_filter_in_for_access(self):
        """Return the hierarchy locally for access."""
        permission_str = "inventory:groups:read"
        # Create another valid workspace for testing
        test_workspace = Workspace.objects.create(
            name="Test Workspace", tenant=self.tenant, parent=self.standard_workspace
        )
        role_data = {
            "name": "Inventory Group Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "in",
                                "value": [str(self.default_workspace.id), str(test_workspace.id)],
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(
            resource_definition, context={"for_access": True}
        )
        updated_operation = resource_definition_serializer.data.get("attributeFilter").get("operation")
        actual = set(resource_definition_serializer.data.get("attributeFilter").get("value"))
        expected = {
            str(self.default_workspace.id),
            str(self.standard_workspace.id),
            str(self.sub_workspace_a.id),
            str(self.sub_workspace_b.id),
            str(test_workspace.id),
        }

        self.assertEqual(actual, expected)
        self.assertEqual(updated_operation, "in")

    def test_get_with_inventory_groups_filter_equal_for_access(self):
        """Return the hierarchy locally for access."""
        permission_str = "inventory:groups:read"
        role_data = {
            "name": "Inventory Group Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.default_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(
            resource_definition, context={"for_access": True}
        )
        updated_operation = resource_definition_serializer.data.get("attributeFilter").get("operation")
        actual = set(resource_definition_serializer.data.get("attributeFilter").get("value"))
        expected = {
            str(self.default_workspace.id),
            str(self.standard_workspace.id),
            str(self.sub_workspace_a.id),
            str(self.sub_workspace_b.id),
        }
        self.assertEqual(actual, expected)
        self.assertEqual(updated_operation, "in")

    def test_get_with_inventory_groups_filter_in_for_roles(self):
        """Return the hierarchy locally for roles."""
        permission_str = "inventory:groups:read"
        # Create another workspace for testing
        test_workspace_2 = Workspace.objects.create(
            name="Test Workspace 2", tenant=self.tenant, parent=self.root_workspace
        )
        role_data = {
            "name": "Inventory Group Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "in",
                                "value": [str(self.default_workspace.id), str(test_workspace_2.id)],
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(resource_definition)
        updated_operation = resource_definition_serializer.data.get("attributeFilter").get("operation")
        actual = set(resource_definition_serializer.data.get("attributeFilter").get("value"))
        expected = {str(self.default_workspace.id), str(test_workspace_2.id)}

        self.assertEqual(actual, expected)
        self.assertEqual(updated_operation, "in")

    def test_get_with_inventory_groups_filter_equal_for_roles(self):
        """Return the hierarchy locally for roles."""
        permission_str = "inventory:groups:read"
        role_data = {
            "name": "Inventory Group Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.default_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(resource_definition)
        updated_operation = resource_definition_serializer.data.get("attributeFilter").get("operation")
        actual = resource_definition_serializer.data.get("attributeFilter").get("value")
        expected = str(self.default_workspace.id)

        self.assertEqual(actual, expected)
        self.assertEqual(updated_operation, "equal")

    def test_get_with_inventory_wildcard_filter_for_access(self):
        """Return the hierarchy locally for access with inventory:*:read permission."""
        permission_str = "inventory:*:read"
        # Create another workspace for testing
        test_workspace_3 = Workspace.objects.create(
            name="Test Workspace 3", tenant=self.tenant, parent=self.standard_workspace
        )
        role_data = {
            "name": "Inventory Wildcard Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "in",
                                "value": [str(self.default_workspace.id), str(test_workspace_3.id)],
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(
            resource_definition, context={"for_access": True}
        )
        updated_operation = resource_definition_serializer.data.get("attributeFilter").get("operation")
        actual = set(resource_definition_serializer.data.get("attributeFilter").get("value"))
        expected = {
            str(self.default_workspace.id),
            str(self.standard_workspace.id),
            str(self.sub_workspace_a.id),
            str(self.sub_workspace_b.id),
            str(test_workspace_3.id),
        }

        self.assertEqual(actual, expected)
        self.assertEqual(updated_operation, "in")

    def test_get_with_inventory_wildcard_all_filter_for_access(self):
        """Return the hierarchy locally for access with inventory:*:* permission."""
        permission_str = "inventory:*:*"
        role_data = {
            "name": "Inventory Wildcard All Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.default_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(
            resource_definition, context={"for_access": True}
        )
        updated_operation = resource_definition_serializer.data.get("attributeFilter").get("operation")
        actual = set(resource_definition_serializer.data.get("attributeFilter").get("value"))
        expected = {
            str(self.default_workspace.id),
            str(self.standard_workspace.id),
            str(self.sub_workspace_a.id),
            str(self.sub_workspace_b.id),
        }

        self.assertEqual(actual, expected)
        self.assertEqual(updated_operation, "in")

    @override_settings(WORKSPACE_RESOURCE_TYPE=["groups"])
    def test_workspace_hierarchy_with_single_resource_type(self):
        """Test that workspace hierarchy works with single resource type in list."""
        permission_str = "inventory:groups:read"
        role_data = {
            "name": "Inventory Groups Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.default_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(
            resource_definition, context={"for_access": True}
        )

        # Should trigger hierarchy since "groups" is in the configured resource types
        updated_operation = resource_definition_serializer.data.get("attributeFilter").get("operation")
        actual = set(resource_definition_serializer.data.get("attributeFilter").get("value"))
        expected = {
            str(self.default_workspace.id),
            str(self.standard_workspace.id),
            str(self.sub_workspace_a.id),
            str(self.sub_workspace_b.id),
        }

        self.assertEqual(actual, expected)
        self.assertEqual(updated_operation, "in")

    @override_settings(WORKSPACE_RESOURCE_TYPE=["hosts"])
    def test_workspace_hierarchy_with_custom_resource_types(self):
        """Test that workspace hierarchy respects custom WORKSPACE_RESOURCE_TYPE setting."""
        permission_str = "inventory:hosts:read"
        role_data = {
            "name": "Inventory Hosts Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "group.id",
                                "operation": "equal",
                                "value": str(self.default_workspace.id),
                            }
                        }
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(
            resource_definition, context={"for_access": True}
        )

        # Should trigger hierarchy since "hosts" is in the configured resource types
        updated_operation = resource_definition_serializer.data.get("attributeFilter").get("operation")
        actual = set(resource_definition_serializer.data.get("attributeFilter").get("value"))
        expected = {
            str(self.default_workspace.id),
            str(self.standard_workspace.id),
            str(self.sub_workspace_a.id),
            str(self.sub_workspace_b.id),
        }

        self.assertEqual(actual, expected)
        self.assertEqual(updated_operation, "in")

    def test_get_with_other_filter(self):
        """Return correct filter values."""

        # Use a valid workspace UUID for group.id
        resource_value = str(self.standard_workspace.id)
        permission_str = "foo:bar:baz"
        role_data = {
            "name": "Inventory Group Role",
            "access": [
                {
                    "permission": permission_str,
                    "resourceDefinitions": [
                        {"attributeFilter": {"key": "group.id", "operation": "in", "value": [resource_value]}}
                    ],
                }
            ],
        }

        # Serialize the input
        role_serializer = RoleSerializerTest.prepare_serializer(RoleSerializerTest, role_data)
        Permission.objects.create(permission=permission_str, tenant=self.tenant)

        # Create the role
        role = role_serializer.create(role_serializer.validated_data)

        # Get the Resource Definition
        access = role.access.last()
        resource_definition = access.resourceDefinitions.last()
        resource_definition_serializer = ResourceDefinitionSerializer(resource_definition)
        actual = set(resource_definition_serializer.data.get("attributeFilter").get("value"))
        expected = {resource_value}
        self.assertEqual(actual, expected)

    def test_validate_group_id_rejects_integer_in_list(self):
        """Test that group.id with operation='in' rejects integer values."""
        serializer = ResourceDefinitionSerializer(
            data={
                "attributeFilter": {
                    "key": "group.id",
                    "operation": "in",
                    "value": [str(self.default_workspace.id), 123, "invalid-string"],
                }
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("attributeFilter", serializer.errors)
        self.assertIn("format", serializer.errors["attributeFilter"])
        self.assertIn("invalid values", str(serializer.errors["attributeFilter"]["format"][0]))

    def test_validate_group_id_rejects_integer_with_equal(self):
        """Test that group.id with operation='equal' rejects integer values."""
        serializer = ResourceDefinitionSerializer(
            data={
                "attributeFilter": {
                    "key": "group.id",
                    "operation": "equal",
                    "value": 12345,
                }
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("attributeFilter", serializer.errors)
        self.assertIn("format", serializer.errors["attributeFilter"])
        # Integer values are rejected by the type check which happens before group.id validation
        error_msg = str(serializer.errors["attributeFilter"]["format"][0])
        self.assertTrue(
            "String value" in error_msg or "must be a valid UUID" in error_msg,
            f"Unexpected error message: {error_msg}",
        )

    def test_validate_group_id_accepts_valid_uuids(self):
        """Test that group.id accepts valid UUIDs."""
        serializer = ResourceDefinitionSerializer(
            data={
                "attributeFilter": {
                    "key": "group.id",
                    "operation": "in",
                    "value": [str(self.default_workspace.id), str(self.standard_workspace.id)],
                }
            }
        )
        self.assertTrue(serializer.is_valid())

    def test_validate_group_id_accepts_none_in_list(self):
        """Test that group.id accepts None (for ungrouped workspace) in list."""
        serializer = ResourceDefinitionSerializer(
            data={
                "attributeFilter": {
                    "key": "group.id",
                    "operation": "in",
                    "value": [None, str(self.default_workspace.id)],
                }
            }
        )
        self.assertTrue(serializer.is_valid())

    def test_validate_group_id_accepts_none_with_equal(self):
        """Test that group.id accepts None (for ungrouped workspace) with operation='equal'."""
        serializer = ResourceDefinitionSerializer(
            data={
                "attributeFilter": {
                    "key": "group.id",
                    "operation": "equal",
                    "value": None,
                }
            }
        )
        self.assertTrue(serializer.is_valid())

    def test_validate_group_id_rejects_invalid_uuid_string(self):
        """Test that group.id rejects invalid UUID strings."""
        serializer = ResourceDefinitionSerializer(
            data={
                "attributeFilter": {
                    "key": "group.id",
                    "operation": "in",
                    "value": ["not-a-valid-uuid", "also-invalid"],
                }
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("attributeFilter", serializer.errors)
        self.assertIn("format", serializer.errors["attributeFilter"])
        self.assertIn("invalid values", str(serializer.errors["attributeFilter"]["format"][0]))

    def test_validate_non_group_id_allows_integers(self):
        """Test that non-group.id keys can still have integer values (backwards compatibility)."""
        serializer = ResourceDefinitionSerializer(
            data={
                "attributeFilter": {
                    "key": "other.id",
                    "operation": "equal",
                    "value": "12345",
                }
            }
        )
        self.assertTrue(serializer.is_valid())
