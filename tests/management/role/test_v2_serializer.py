#
# Copyright 2026 Red Hat, Inc.
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
"""Test the RoleV2 serializers."""

from unittest.mock import Mock

from django.db.models import Count
from django.test import override_settings
from rest_framework import serializers

from api.models import Tenant
from management.models import Permission
from management.role.v2_model import CustomRoleV2, RoleV2
from management.role.v2_serializer import (
    PermissionSerializer,
    RoleFieldSelection,
    RoleV2ListSerializer,
    RoleV2RequestSerializer,
    RoleV2ResponseSerializer,
)
from management.role.v2_service import RoleV2Service
from management.utils import FieldSelectionValidationError
from management.utils import PRINCIPAL_CACHE
from tests.identity_request import IdentityRequest


class PermissionSerializerTests(IdentityRequest):
    """Test the PermissionSerializer."""

    def test_permission_serializer_input(self):
        """Test that PermissionSerializer correctly deserializes input."""
        data = {
            "application": "inventory",
            "resource_type": "hosts",
            "operation": "read",
        }
        serializer = PermissionSerializer(data=data)

        self.assertTrue(serializer.is_valid())
        # Note: 'operation' maps to 'verb' via source
        self.assertEqual(serializer.validated_data["application"], "inventory")
        self.assertEqual(serializer.validated_data["resource_type"], "hosts")
        self.assertEqual(serializer.validated_data["verb"], "read")

    def test_permission_serializer_missing_application(self):
        """Test that missing application field fails validation."""
        data = {"resource_type": "hosts", "operation": "read"}
        serializer = PermissionSerializer(data=data)

        self.assertFalse(serializer.is_valid())
        self.assertIn("application", serializer.errors)

    def test_permission_serializer_missing_resource_type(self):
        """Test that missing resource_type field fails validation."""
        data = {"application": "inventory", "operation": "read"}
        serializer = PermissionSerializer(data=data)

        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_type", serializer.errors)

    def test_permission_serializer_missing_operation(self):
        """Test that missing operation field fails validation."""
        data = {"application": "inventory", "resource_type": "hosts"}
        serializer = PermissionSerializer(data=data)

        self.assertFalse(serializer.is_valid())
        self.assertIn("operation", serializer.errors)


class RoleV2ResponseSerializerTests(IdentityRequest):
    """Test the RoleV2ResponseSerializer (output serializer)."""

    def setUp(self):
        """Set up the serializer tests."""
        super().setUp()
        self.permission = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        self.role = CustomRoleV2.objects.create(
            name="Test Role",
            description="A test role",
            tenant=self.tenant,
        )
        self.role.permissions.add(self.permission)

    def tearDown(self):
        """Tear down serializer tests."""
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()

        # Clear principal cache to avoid test isolation issues
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)
        super().tearDown()

    def test_response_serializer_output_fields(self):
        """Test that response serializer includes all expected fields."""
        serializer = RoleV2ResponseSerializer(self.role)
        data = serializer.data

        self.assertIn("id", data)
        self.assertIn("name", data)
        self.assertIn("description", data)
        self.assertIn("permissions", data)
        self.assertIn("last_modified", data)

    def test_response_serializer_uuid_mapping(self):
        """Test that 'id' field maps to model's 'uuid'."""
        serializer = RoleV2ResponseSerializer(self.role)
        data = serializer.data

        self.assertEqual(str(self.role.uuid), data["id"])

    def test_response_serializer_last_modified_mapping(self):
        """Test that 'last_modified' field maps to model's 'modified'."""
        serializer = RoleV2ResponseSerializer(self.role)
        data = serializer.data

        self.assertIsNotNone(data["last_modified"])


class RoleV2SerializerFieldSelectionTests(IdentityRequest):
    """Test the RoleV2 serializer field selection."""

    def setUp(self):
        """Set up the serializer tests."""
        super().setUp()
        self.permission = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)
        self.role = RoleV2.objects.create(
            name="test_role",
            description="Test role description",
            tenant=self.tenant,
        )
        self.role.permissions.add(self.permission)

    def tearDown(self):
        """Tear down the serializer tests."""
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()

        # Clear principal cache to avoid test isolation issues
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)
        super().tearDown()

    def _get_annotated_role(self):
        """Get the role with the permissions_count annotation."""
        return RoleV2.objects.annotate(permissions_count_annotation=Count("permissions", distinct=True)).get(
            pk=self.role.pk
        )

    def _build_context(self, fields=None):
        """Build serializer context with resolved field set."""
        if not fields:
            return {"fields": RoleV2Service.DEFAULT_LIST_FIELDS}
        fields = fields.replace("\x00", "")
        field_selection = RoleFieldSelection.parse(fields)
        resolved = field_selection.root_fields & set(RoleV2ResponseSerializer.Meta.fields)
        return {"fields": resolved or RoleV2Service.DEFAULT_LIST_FIELDS}

    def test_default_fields_when_no_field_selection(self):
        """Test that default fields are returned when no fields param is provided."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context())
        data = serializer.data

        # Should return exactly the default fields
        expected_fields = {"id", "name", "description", "last_modified"}
        self.assertEqual(set(data.keys()), expected_fields)

        self.assertEqual(data["name"], "test_role")
        self.assertEqual(data["description"], "Test role description")
        self.assertEqual(data["id"], str(self.role.uuid))
        self.assertIsNotNone(data["last_modified"])

    def test_custom_fields_with_field_selection(self):
        """Test that only requested fields are returned with field selection."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("id,name,permissions_count"))
        data = serializer.data

        self.assertEqual(set(data.keys()), {"id", "name", "permissions_count"})
        self.assertEqual(data["name"], "test_role")
        self.assertEqual(data["permissions_count"], 1)

    def test_permissions_field_returns_permission_objects(self):
        """Test that permissions field returns properly formatted permission objects."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("id,permissions"))
        data = serializer.data

        self.assertIn("permissions", data)
        self.assertEqual(len(data["permissions"]), 1)

        perm = data["permissions"][0]
        self.assertEqual(perm["application"], "inventory")
        self.assertEqual(perm["resource_type"], "hosts")
        self.assertEqual(perm["operation"], "read")

    def test_all_fields_can_be_selected(self):
        """Test that all valid fields can be selected."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(
            role,
            context=self._build_context("id,name,description,permissions_count,last_modified,permissions"),
        )
        data = serializer.data

        expected = {"id", "name", "description", "permissions_count", "last_modified", "permissions"}
        self.assertEqual(set(data.keys()), expected)

    def test_invalid_fields_raise_validation_error(self):
        """Test that invalid field names raise a validation error during parsing."""

        with self.assertRaises(FieldSelectionValidationError):
            RoleFieldSelection.parse("id,invalid_field,name")

    def test_multiple_permissions_serialization(self):
        """Test that role with multiple permissions serializes correctly."""
        # Add more permissions
        perm2 = Permission.objects.create(permission="inventory:hosts:write", tenant=self.tenant)
        perm3 = Permission.objects.create(permission="rbac:roles:read", tenant=self.tenant)
        self.role.permissions.add(perm2, perm3)

        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("id,permissions,permissions_count"))
        data = serializer.data

        self.assertEqual(data["permissions_count"], 3)
        self.assertEqual(len(data["permissions"]), 3)

        # Verify all permissions have correct structure
        for perm in data["permissions"]:
            self.assertIn("application", perm)
            self.assertIn("resource_type", perm)
            self.assertIn("operation", perm)

    def test_role_with_no_permissions(self):
        """Test that role with no permissions returns empty list."""
        self.role.permissions.clear()

        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("id,permissions,permissions_count"))
        data = serializer.data

        self.assertEqual(data["permissions_count"], 0)
        self.assertEqual(data["permissions"], [])

    def test_fields_with_whitespace(self):
        """Test that fields with whitespace around commas are parsed correctly."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("id, name, description"))
        data = serializer.data

        self.assertEqual(set(data.keys()), {"id", "name", "description"})

    def test_fields_with_null_character(self):
        """Test that null characters in fields param are sanitized."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("id,na\x00me,description"))
        data = serializer.data

        # Null character should be removed, "name" should be parsed correctly
        self.assertEqual(set(data.keys()), {"id", "name", "description"})

    def test_fields_with_duplicates(self):
        """Test that duplicate field names are handled."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("id,name,id,name"))
        data = serializer.data

        self.assertEqual(set(data.keys()), {"id", "name"})

    def test_fields_with_empty_entries(self):
        """Test that empty entries between commas are ignored."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("id,,name,,,description"))
        data = serializer.data

        self.assertEqual(set(data.keys()), {"id", "name", "description"})

    def test_only_permissions_count_field(self):
        """Test requesting only permissions_count field."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role, context=self._build_context("permissions_count"))
        data = serializer.data

        self.assertEqual(set(data.keys()), {"permissions_count"})
        self.assertEqual(data["permissions_count"], 1)

    def test_serializer_without_context_returns_all_fields(self):
        """Test that serializer without context keeps all fields."""
        role = self._get_annotated_role()
        serializer = RoleV2ResponseSerializer(role)
        data = serializer.data

        expected = {"id", "name", "description", "permissions_count", "permissions", "last_modified", "org_id"}
        self.assertEqual(set(data.keys()), expected)


class RoleV2ListSerializerTests(IdentityRequest):
    """Test the RoleV2ListSerializer."""

    def test_nul_bytes_stripped_from_name(self):
        """Test that NUL bytes in name are stripped before validation."""
        serializer = RoleV2ListSerializer(data={"name": "admin\x00role"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["name"], "adminrole")

    def test_nul_bytes_stripped_from_fields(self):
        """Test that NUL bytes in fields are stripped before validation."""
        serializer = RoleV2ListSerializer(data={"fields": "id,na\x00me"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIn("name", serializer.validated_data["fields"])

    def test_name_passes_through(self):
        """Test that a non-empty name passes through as-is."""
        serializer = RoleV2ListSerializer(data={"name": "my_role"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["name"], "my_role")

    def test_empty_name_returns_none(self):
        """Test that an empty name string is normalized to None."""
        serializer = RoleV2ListSerializer(data={"name": ""})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNone(serializer.validated_data["name"])

    def test_no_name_param_is_valid(self):
        """Test that omitting name entirely is valid."""
        serializer = RoleV2ListSerializer(data={})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("name", serializer.validated_data)

    def test_no_fields_param_returns_defaults(self):
        """Test that omitting fields returns DEFAULT_LIST_FIELDS."""
        serializer = RoleV2ListSerializer(data={})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["fields"], RoleV2Service.DEFAULT_LIST_FIELDS)

    def test_empty_fields_returns_defaults(self):
        """Test that an empty fields string returns DEFAULT_LIST_FIELDS."""
        serializer = RoleV2ListSerializer(data={"fields": ""})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["fields"], RoleV2Service.DEFAULT_LIST_FIELDS)

    def test_valid_fields_resolve_to_requested_set(self):
        """Test that valid field names resolve to the requested set."""
        serializer = RoleV2ListSerializer(data={"fields": "id,name"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["fields"], {"id", "name"})

    def test_invalid_fields_raise_validation_error(self):
        """Test that invalid field names raise a validation error."""
        serializer = RoleV2ListSerializer(data={"fields": "id,bogus_field"})
        self.assertFalse(serializer.is_valid())
        self.assertIn("fields", serializer.errors)

    def test_all_valid_response_fields_can_be_requested(self):
        """Test that all fields from RoleV2ResponseSerializer.Meta.fields are accepted."""
        all_fields = ",".join(RoleV2ResponseSerializer.Meta.fields)
        serializer = RoleV2ListSerializer(data={"fields": all_fields})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["fields"], set(RoleV2ResponseSerializer.Meta.fields))

    def test_valid_resource_id_as_uuid_for_workspace(self):
        """resource_id is normalized to a UUID string when resource_type is workspace."""
        uid = "550e8400-e29b-41d4-a716-446655440000"
        serializer = RoleV2ListSerializer(data={"resource_type": "workspace", "resource_id": uid})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["resource_id"], uid)

    def test_invalid_resource_id_returns_error_for_workspace(self):
        """Non-UUID resource_id is rejected when resource_type is workspace."""
        serializer = RoleV2ListSerializer(data={"resource_type": "workspace", "resource_id": "not-a-uuid"})
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_id", serializer.errors)

    def test_tenant_resource_id_accepted(self):
        """Tenant resource IDs (domain/org_id) are accepted when resource_type is tenant."""
        tenant_resource_id = Tenant.org_id_to_tenant_resource_id("20228402")
        serializer = RoleV2ListSerializer(
            data={"resource_type": "tenant", "resource_id": tenant_resource_id},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["resource_id"], tenant_resource_id)

    def test_resource_tenant_org_id_cannot_combine_with_resource_id(self):
        """resource.tenant.org_id and resource_id are mutually exclusive."""
        serializer = RoleV2ListSerializer(
            data={
                "resource_tenant_org_id": "20228402",
                "resource_id": Tenant.org_id_to_tenant_resource_id("20228402"),
            },
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_tenant_org_id", serializer.errors)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RoleV2RequestSerializerTests(IdentityRequest):
    """Test the RoleV2RequestSerializer (request serializer for create/update)."""

    def setUp(self):
        """Set up the serializer tests."""
        super().setUp()
        self.permission = Permission.objects.create(permission="inventory:hosts:read", tenant=self.tenant)

        # Mock request with tenant
        self.mock_request = Mock()
        self.mock_request.tenant = self.tenant

    def tearDown(self):
        """Tear down serializer tests."""
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)
        super().tearDown()

    def test_serializer_validates_required_name(self):
        """Test that name is required."""
        data = {
            "description": "A role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertFalse(serializer.is_valid())
        self.assertIn("name", serializer.errors)

    def test_serializer_accepts_missing_description(self):
        """Test that missing description is accepted and defaults to empty string."""
        data = {
            "name": "Test Role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["description"], "")

    def test_serializer_validates_required_permissions(self):
        """Test that permissions are required."""
        data = {
            "name": "Test Role",
            "description": "A role",
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertFalse(serializer.is_valid())
        self.assertIn("permissions", serializer.errors)

    def test_serializer_accepts_blank_description(self):
        """Test that blank description is accepted."""
        data = {
            "name": "Test Role",
            "description": "",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["description"], "")

    def test_serializer_create_success(self):
        """Test that serializer.create() creates a role successfully."""
        data = {
            "name": "New Role",
            "description": "A new role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid())
        role = serializer.save()

        self.assertIsInstance(role, CustomRoleV2)
        self.assertEqual(role.name, "New Role")
        self.assertEqual(role.description, "A new role")
        self.assertEqual(role.permissions.count(), 1)

    def test_serializer_create_duplicate_name_raises_validation_error(self):
        """Test that creating a role with duplicate name raises ValidationError."""
        # Create first role
        CustomRoleV2.objects.create(
            name="Duplicate Role",
            description="First role",
            tenant=self.tenant,
        )

        # Try to create via serializer
        data = {
            "name": "Duplicate Role",
            "description": "Second role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid())
        with self.assertRaises(serializers.ValidationError) as cm:
            serializer.save()

        self.assertIn("name", cm.exception.detail)

    def test_serializer_create_invalid_permission_raises_validation_error(self):
        """Test that non-existent permission raises ValidationError."""
        data = {
            "name": "Role with Bad Permission",
            "description": "Has invalid permission",
            "permissions": [{"application": "nonexistent", "resource_type": "resource", "operation": "action"}],
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid())
        with self.assertRaises(serializers.ValidationError) as cm:
            serializer.save()

        self.assertIn("permissions", cm.exception.detail)

    def test_serializer_service_injectable_via_context(self):
        """Test that service can be overridden via context."""
        mock_service = Mock()
        mock_service.create.return_value = CustomRoleV2(
            name="Injected",
            description="Via context",
            tenant=self.tenant,
        )

        data = {
            "name": "Injected Role",
            "description": "Test",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        serializer = RoleV2RequestSerializer(
            data=data,
            context={"request": self.mock_request, "role_service": mock_service},
        )
        serializer.is_valid()
        serializer.save()

        # Verify the injected service was used
        mock_service.create.assert_called_once()

    def test_serializer_update_success(self):
        """Test that update delegates to service and returns updated role."""
        role = CustomRoleV2.objects.create(
            name="Original Role",
            description="Original description",
            tenant=self.tenant,
        )
        role.permissions.add(self.permission)

        data = {
            "name": "Updated Role",
            "description": "Updated description",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        serializer = RoleV2RequestSerializer(instance=role, data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid())
        updated_role = serializer.save()

        self.assertEqual(updated_role.name, "Updated Role")
        self.assertEqual(updated_role.description, "Updated description")

    def test_serializer_update_nonexistent_role_raises_not_found(self):
        """Test that updating a nonexistent role raises NotFoundError."""
        from management.exceptions import NotFoundError

        role = CustomRoleV2.objects.create(
            name="Original Role",
            description="Original description",
            tenant=self.tenant,
        )
        # Delete the role to simulate it not existing
        role_uuid = role.uuid
        role.delete()

        # Create a mock instance with the deleted UUID
        mock_instance = Mock()
        mock_instance.uuid = role_uuid

        data = {
            "name": "Updated Role",
            "description": "Updated description",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        serializer = RoleV2RequestSerializer(instance=mock_instance, data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid())
        with self.assertRaises(NotFoundError):
            serializer.save()

    def test_serializer_update_duplicate_name_raises_validation_error(self):
        """Test that updating to a duplicate name raises ValidationError."""
        role1 = CustomRoleV2.objects.create(
            name="Role One",
            description="First role",
            tenant=self.tenant,
        )

        role2 = CustomRoleV2.objects.create(
            name="Role Two",
            description="Second role",
            tenant=self.tenant,
        )

        data = {
            "name": "Role One",
            "description": "Second role",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }

        serializer = RoleV2RequestSerializer(instance=role2, data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid())
        with self.assertRaises(serializers.ValidationError) as cm:
            serializer.save()

        self.assertIn("name", cm.exception.detail)

    def test_serializer_rejects_name_with_asterisk(self):
        """Test that a name containing '*' is rejected at validation time."""
        data = {
            "name": "role_*_admin",
            "description": "Should fail validation",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertFalse(serializer.is_valid())
        self.assertIn("name", serializer.errors)

    def test_serializer_accepts_name_with_other_special_chars(self):
        """Test that names with non-asterisk special characters are accepted."""
        data = {
            "name": "Role with Special Chars: @#$%",
            "description": "Should pass validation",
            "permissions": [{"application": "inventory", "resource_type": "hosts", "operation": "read"}],
        }
        serializer = RoleV2RequestSerializer(data=data, context={"request": self.mock_request})

        self.assertTrue(serializer.is_valid(), serializer.errors)
