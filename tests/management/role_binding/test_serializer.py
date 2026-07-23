#
# Copyright 2025 Red Hat, Inc.
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
"""Test role binding serializers."""

import uuid
from datetime import datetime, timezone
from unittest.mock import Mock

from django.test import TestCase, override_settings

from management.models import Group, Permission, Principal, RoleBinding, RoleBindingGroup, RoleV2, Workspace
from management.role.v2_service import RoleV2Service
from management.role_binding.serializer import (
    BatchCreateRoleBindingRequestSerializer,
    BatchCreateRoleBindingResponseItemSerializer,
    RoleBindingByGroupSerializer,
    RoleBindingBySubjectFieldSelection,
    RoleBindingFieldSelection,
    RoleBindingInputSerializer,
    RoleBindingListInputSerializer,
    RoleBindingListOutputSerializer,
    RoleBindingOutputSerializer,
    UpdateRoleBindingRequestSerializer,
    UpdateRoleBindingResponseSerializer,
)
from management.role_binding.service import ExcludeSources, UpdateRoleBindingResult
from management.subject.model import SubjectType
from management.utils import FieldSelection
from tests.identity_request import IdentityRequest

# Sentinel value used to signal that a key should be removed from test data.
_REMOVE = object()


class RoleBindingByGroupSerializerTest(IdentityRequest):
    """Test the RoleBindingByGroupSerializer.

    Tests verify the serializer produces output matching the API spec:
    - last_modified: datetime timestamp
    - subject: {id: UUID, type: "group", group: {name, description, user_count}}
    - roles: [{id: UUID, name: string}]
    - resource: {id, name, type}

    Note: sources field is defined in spec but not yet implemented.
    """

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.permission = Permission.objects.create(
            permission="app:resource:read",
            tenant=self.tenant,
        )

        self.role = RoleV2.objects.create(
            name="test_role",
            tenant=self.tenant,
        )
        self.role.permissions.add(self.permission)

        self.group = Group.objects.create(
            name="test_group",
            description="Test group description",
            tenant=self.tenant,
        )

        self.principal = Principal.objects.create(
            username=self.user_data["username"],
            tenant=self.tenant,
            type=Principal.Types.USER,
        )
        self.group.principals.add(self.principal)

        self.binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        self.binding_group = RoleBindingGroup.objects.create(
            group=self.group,
            binding=self.binding,
        )

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        self.group.principals.clear()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    # get_last_modified tests

    def test_last_modified_returns_modified_from_dict(self):
        """Test get_last_modified with dict containing 'modified' key."""
        modified_time = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        obj = {"modified": modified_time}

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_last_modified(obj)

        self.assertEqual(result, modified_time)

    def test_last_modified_returns_latest_modified_from_dict(self):
        """Test get_last_modified with dict containing 'latest_modified' key."""
        latest_modified_time = datetime(2025, 1, 20, 14, 0, 0, tzinfo=timezone.utc)
        obj = {"latest_modified": latest_modified_time}

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_last_modified(obj)

        self.assertEqual(result, latest_modified_time)

    def test_last_modified_prefers_modified_over_latest_modified_in_dict(self):
        """Test get_last_modified prefers 'modified' over 'latest_modified' in dict."""
        modified_time = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        latest_modified_time = datetime(2025, 1, 20, 14, 0, 0, tzinfo=timezone.utc)
        obj = {"modified": modified_time, "latest_modified": latest_modified_time}

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_last_modified(obj)

        self.assertEqual(result, modified_time)

    def test_last_modified_returns_none_for_empty_dict(self):
        """Test get_last_modified with empty dict returns None."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_last_modified({})

        self.assertIsNone(result)

    def test_last_modified_returns_latest_modified_attribute_from_group(self):
        """Test get_last_modified with Group object uses latest_modified attribute."""
        mock_group = Mock(spec=Group)
        mock_group.latest_modified = datetime(2025, 1, 25, 12, 0, 0, tzinfo=timezone.utc)

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_last_modified(mock_group)

        self.assertEqual(result, mock_group.latest_modified)

    def test_last_modified_returns_none_when_group_has_no_latest_modified(self):
        """Test get_last_modified with Group object without latest_modified returns None."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_last_modified(self.group)

        self.assertIsNone(result)

    # get_subject tests

    def test_subject_returns_correct_structure_for_group(self):
        """Test get_subject with Group object returns correct default structure.

        Default behavior (no field_selection) returns only id and type.
        """
        self.group.principalCount = 1

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_subject(self.group)

        # Default behavior: only id and type
        expected = {
            "id": self.group.uuid,
            "type": "group",
        }
        self.assertEqual(result, expected)

    def test_subject_handles_group_with_no_description(self):
        """Test get_subject with Group that has no description.

        Default behavior returns only id and type (no group details).
        """
        group = Group.objects.create(
            name="no_desc_group",
            description=None,
            tenant=self.tenant,
        )
        group.principalCount = 0

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_subject(group)

        # Default behavior: only id and type, no group details
        self.assertEqual(result["id"], group.uuid)
        self.assertEqual(result["type"], "group")
        self.assertNotIn("group", result)

        group.delete()

    def test_subject_includes_correct_user_count_for_multiple_principals(self):
        """Test get_subject with Group having multiple principals.

        Requires field_selection to include group.user_count.
        """
        principal2 = Principal.objects.create(
            username="user2",
            tenant=self.tenant,
            type=Principal.Types.USER,
        )
        self.group.principals.add(principal2)
        self.group.principalCount = 2

        field_selection = RoleBindingFieldSelection.parse("subject(group.user_count)")
        serializer = RoleBindingByGroupSerializer(context={"field_selection": field_selection})
        result = serializer.get_subject(self.group)

        self.assertEqual(result["type"], "group")
        self.assertEqual(result["group"]["user_count"], 2)

        principal2.delete()

    def test_subject_returns_none_for_non_group_object(self):
        """Test get_subject with non-Group object returns None."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_subject({"name": "not a group"})

        self.assertIsNone(result)

    def test_subject_returns_none_for_none_input(self):
        """Test get_subject with None returns None."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_subject(None)

        self.assertIsNone(result)

    # get_roles tests

    def test_roles_returns_roles_list_from_dict(self):
        """Test get_roles with dict containing roles list."""
        roles = [
            {"id": "uuid-1", "name": "Role 1"},
            {"id": "uuid-2", "name": "Role 2"},
        ]
        obj = {"roles": roles}

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles(obj)

        self.assertEqual(result, roles)

    def test_roles_returns_empty_list_from_dict_with_empty_roles(self):
        """Test get_roles with dict containing empty roles list."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles({"roles": []})

        self.assertEqual(result, [])

    def test_roles_returns_empty_list_from_dict_without_roles_key(self):
        """Test get_roles with dict without roles key."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles({})

        self.assertEqual(result, [])

    def test_roles_extracts_roles_from_group_prefetched_bindings(self):
        """Test get_roles with Group having prefetched bindings.

        id is always included.
        """
        mock_binding = Mock()
        mock_binding.role = self.role

        mock_binding_group = Mock()
        mock_binding_group.binding = mock_binding

        mock_group = Mock(spec=Group)
        mock_group.filtered_bindings = [mock_binding_group]

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles(mock_group)

        self.assertEqual(len(result), 1)
        # id is always included
        self.assertEqual(result[0]["id"], self.role.uuid)
        # Default behavior: no name included
        self.assertNotIn("name", result[0])

    def test_roles_deduplicates_same_role_from_multiple_bindings(self):
        """Test get_roles deduplicates roles when same role appears multiple times."""
        mock_binding = Mock()
        mock_binding.role = self.role

        mock_binding_group1 = Mock()
        mock_binding_group1.binding = mock_binding

        mock_binding_group2 = Mock()
        mock_binding_group2.binding = mock_binding

        mock_group = Mock(spec=Group)
        mock_group.filtered_bindings = [mock_binding_group1, mock_binding_group2]

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles(mock_group)

        self.assertEqual(len(result), 1)

    def test_roles_returns_multiple_different_roles(self):
        """Test get_roles with Group having multiple different roles."""
        role2 = RoleV2.objects.create(name="test_role_2", tenant=self.tenant)

        mock_binding1 = Mock()
        mock_binding1.role = self.role

        mock_binding2 = Mock()
        mock_binding2.role = role2

        mock_binding_group1 = Mock()
        mock_binding_group1.binding = mock_binding1

        mock_binding_group2 = Mock()
        mock_binding_group2.binding = mock_binding2

        mock_group = Mock(spec=Group)
        mock_group.filtered_bindings = [mock_binding_group1, mock_binding_group2]

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles(mock_group)

        self.assertEqual(len(result), 2)
        role_ids = {r["id"] for r in result}
        self.assertIn(self.role.uuid, role_ids)
        self.assertIn(role2.uuid, role_ids)

        role2.delete()

    def test_roles_returns_empty_list_when_group_has_no_filtered_bindings(self):
        """Test get_roles with Group without filtered_bindings attribute."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles(self.group)

        self.assertEqual(result, [])

    def test_roles_skips_binding_with_no_role(self):
        """Test get_roles with binding that has no role."""
        mock_binding = Mock()
        mock_binding.role = None

        mock_binding_group = Mock()
        mock_binding_group.binding = mock_binding

        mock_group = Mock(spec=Group)
        mock_group.filtered_bindings = [mock_binding_group]

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles(mock_group)

        self.assertEqual(result, [])

    def test_roles_skips_binding_group_with_no_binding(self):
        """Test get_roles when binding_group has no binding."""
        mock_binding_group = Mock()
        mock_binding_group.binding = None

        mock_group = Mock(spec=Group)
        mock_group.filtered_bindings = [mock_binding_group]

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_roles(mock_group)

        self.assertEqual(result, [])

    # get_resource tests

    def test_resource_returns_resource_from_dict(self):
        """Test get_resource with dict containing resource data."""
        resource_data = {
            "id": "ws-12345",
            "name": "Test Workspace",
            "type": "workspace",
        }
        obj = {"resource": resource_data}

        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_resource(obj)

        self.assertEqual(result, resource_data)

    def test_resource_returns_empty_dict_from_dict_with_empty_resource(self):
        """Test get_resource with dict containing empty resource."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_resource({"resource": {}})

        self.assertEqual(result, {})

    def test_resource_returns_empty_dict_from_dict_without_resource_key(self):
        """Test get_resource with dict without resource key."""
        serializer = RoleBindingByGroupSerializer()
        result = serializer.get_resource({})

        self.assertEqual(result, {})

    def test_resource_builds_from_context_for_group(self):
        """Test get_resource with Group object and context.

        Default behavior returns only resource id.
        """
        context = {
            "request": Mock(),
            "resource_id": "ws-12345",
            "resource_name": "Test Workspace",
            "resource_type": "workspace",
        }

        serializer = RoleBindingByGroupSerializer(context=context)
        result = serializer.get_resource(self.group)

        # Default behavior: only id
        expected = {"id": "ws-12345"}
        self.assertEqual(result, expected)

    def test_resource_returns_data_when_no_request_in_context(self):
        """Test get_resource with Group returns data even without request in context.

        Default behavior returns only resource id.
        """
        context = {
            "resource_id": "ws-12345",
            "resource_name": "Test Workspace",
            "resource_type": "workspace",
        }

        serializer = RoleBindingByGroupSerializer(context=context)
        result = serializer.get_resource(self.group)

        # Default behavior: only id
        expected = {"id": "ws-12345"}
        self.assertEqual(result, expected)

    def test_resource_returns_none_when_context_is_empty(self):
        """Test get_resource with Group and empty context."""
        serializer = RoleBindingByGroupSerializer(context={})
        result = serializer.get_resource(self.group)

        self.assertIsNone(result)

    def test_resource_handles_partial_context_values(self):
        """Test get_resource with partial context values.

        Default behavior returns only resource id.
        """
        context = {
            "request": Mock(),
            "resource_id": "ws-12345",
        }

        serializer = RoleBindingByGroupSerializer(context=context)
        result = serializer.get_resource(self.group)

        # Default behavior: only id
        expected = {"id": "ws-12345"}
        self.assertEqual(result, expected)

    # Full serialization tests

    def test_full_serialization_with_dict_input(self):
        """Test full serialization with dict input.

        Default behavior returns only basic fields (no last_modified).
        """
        modified_time = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        obj = {
            "modified": modified_time,
            "roles": [{"id": "550e8400-e29b-41d4-a716-446655440002", "name": "Workspace Admin"}],
            "resource": {
                "id": "550e8400-e29b-41d4-a716-446655440001",
                "name": "Engineering Workspace",
                "type": "workspace",
            },
        }

        serializer = RoleBindingByGroupSerializer(obj)
        data = serializer.data

        # Default behavior: last_modified not included
        self.assertNotIn("last_modified", data)
        self.assertIsNone(data["subject"])
        self.assertEqual(len(data["roles"]), 1)
        # Roles from dict pass through as-is
        self.assertEqual(data["roles"][0]["name"], "Workspace Admin")
        # Resource from dict passes through as-is
        self.assertEqual(data["resource"]["type"], "workspace")

    def test_full_serialization_with_group_and_context(self):
        """Test full serialization with Group object and context.

        Default behavior returns only basic fields:
        - subject: id, type (no group details)
        - roles: id only (no name)
        - resource: id only (no name, type)
        - no last_modified
        """
        self.group.principalCount = 1
        self.group.latest_modified = datetime(2025, 1, 20, 14, 0, 0, tzinfo=timezone.utc)

        mock_binding = Mock()
        mock_binding.role = self.role

        mock_binding_group = Mock()
        mock_binding_group.binding = mock_binding

        self.group.filtered_bindings = [mock_binding_group]

        context = {
            "request": Mock(),
            "resource_id": "ws-12345",
            "resource_name": "Test Workspace",
            "resource_type": "workspace",
        }

        serializer = RoleBindingByGroupSerializer(self.group, context=context)
        data = serializer.data

        # Default behavior: only basic fields
        self.assertNotIn("last_modified", data)
        self.assertEqual(data["subject"]["id"], self.group.uuid)
        self.assertEqual(data["subject"]["type"], "group")
        self.assertNotIn("group", data["subject"])
        self.assertEqual(len(data["roles"]), 1)
        self.assertEqual(data["roles"][0]["id"], self.role.uuid)
        self.assertNotIn("name", data["roles"][0])
        self.assertEqual(data["resource"], {"id": "ws-12345"})

    def test_full_serialization_with_multiple_groups(self):
        """Test serialization of multiple groups.

        Default behavior returns only basic fields (id, type) for each subject.
        """
        group2 = Group.objects.create(
            name="test_group_2",
            description="Second group",
            tenant=self.tenant,
        )
        group2.principalCount = 0
        self.group.principalCount = 1

        context = {
            "request": Mock(),
            "resource_id": "ws-12345",
            "resource_name": "Test Workspace",
            "resource_type": "workspace",
        }

        serializer = RoleBindingByGroupSerializer([self.group, group2], many=True, context=context)
        data = serializer.data

        self.assertEqual(len(data), 2)
        # Default behavior: only id and type, no group details
        self.assertEqual(data[0]["subject"]["id"], self.group.uuid)
        self.assertEqual(data[0]["subject"]["type"], "group")
        self.assertNotIn("group", data[0]["subject"])
        self.assertEqual(data[1]["subject"]["id"], group2.uuid)
        self.assertEqual(data[1]["subject"]["type"], "group")
        self.assertNotIn("group", data[1]["subject"])

        group2.delete()

    def test_serialized_output_matches_expected_structure(self):
        """Test that serialized output matches the expected default structure.

        Default behavior returns only basic fields:
        - subject: id, type (no group details)
        - roles: id only
        - resource: id only
        - sources: id only
        - no last_modified
        """
        self.group.principalCount = 5
        self.group.latest_modified = datetime(2025, 1, 20, 14, 0, 0, tzinfo=timezone.utc)

        mock_binding = Mock()
        mock_binding.role = self.role
        mock_binding.resource_type = "workspace"
        mock_binding.resource_id = "550e8400-e29b-41d4-a716-446655440001"

        mock_binding_group = Mock()
        mock_binding_group.binding = mock_binding

        self.group.filtered_bindings = [mock_binding_group]

        context = {
            "request": Mock(),
            "resource_id": "550e8400-e29b-41d4-a716-446655440001",
            "resource_name": "Engineering Workspace",
            "resource_type": "workspace",
        }

        serializer = RoleBindingByGroupSerializer(self.group, context=context)
        data = serializer.data

        # Verify top-level fields (no last_modified by default)
        self.assertNotIn("last_modified", data)
        self.assertIn("subject", data)
        self.assertIn("roles", data)
        self.assertIn("resource", data)
        self.assertIn("sources", data)

        # Verify subject structure - only id and type by default
        subject = data["subject"]
        self.assertEqual(subject["type"], "group")
        self.assertIn("id", subject)
        self.assertNotIn("group", subject)

        # Verify roles structure - only id by default
        self.assertIsInstance(data["roles"], list)
        if data["roles"]:
            role = data["roles"][0]
            self.assertIn("id", role)
            self.assertNotIn("name", role)

        # Verify resource structure - only id by default
        resource = data["resource"]
        self.assertIn("id", resource)
        self.assertNotIn("name", resource)
        self.assertNotIn("type", resource)

        # Verify sources structure - only id by default
        sources = data["sources"]
        self.assertIsInstance(sources, list)
        self.assertEqual(len(sources), 1)
        self.assertIn("id", sources[0])
        self.assertNotIn("name", sources[0])
        self.assertNotIn("type", sources[0])

    def test_sources_returns_name_and_type_with_field_selection(self):
        """Test get_sources returns name and type when requested via field_selection."""
        self.group.principalCount = 1

        mock_binding = Mock()
        mock_binding.role = self.role
        mock_binding.resource_type = "workspace"
        mock_binding.resource_id = "550e8400-e29b-41d4-a716-446655440001"
        mock_binding.resource_name = "Engineering Workspace"

        mock_binding_group = Mock()
        mock_binding_group.binding = mock_binding

        self.group.filtered_bindings = [mock_binding_group]

        context = {
            "request": Mock(),
            "resource_id": "550e8400-e29b-41d4-a716-446655440001",
            "resource_name": "Engineering Workspace",
            "resource_type": "workspace",
            "field_selection": RoleBindingBySubjectFieldSelection.parse("sources(name,type)"),
        }

        serializer = RoleBindingByGroupSerializer(self.group, context=context)
        data = serializer.data

        self.assertIn("sources", data)
        sources = data["sources"]
        self.assertIsInstance(sources, list)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], "550e8400-e29b-41d4-a716-446655440001")
        self.assertEqual(sources[0]["name"], "Engineering Workspace")
        self.assertEqual(sources[0]["type"], "workspace")

    def test_sources_deduplicates_multiple_bindings_from_same_resource(self):
        """Test that sources returns unique resources even when multiple bindings exist."""
        self.group.principalCount = 1

        # Create two bindings from the same resource
        mock_binding1 = Mock()
        mock_binding1.role = self.role
        mock_binding1.resource_type = "workspace"
        mock_binding1.resource_id = "ws-123"

        mock_binding2 = Mock()
        mock_binding2.role = self.role
        mock_binding2.resource_type = "workspace"
        mock_binding2.resource_id = "ws-123"

        mock_binding_group1 = Mock()
        mock_binding_group1.binding = mock_binding1

        mock_binding_group2 = Mock()
        mock_binding_group2.binding = mock_binding2

        self.group.filtered_bindings = [mock_binding_group1, mock_binding_group2]

        context = {
            "request": Mock(),
            "resource_id": "ws-123",
            "resource_type": "workspace",
        }

        serializer = RoleBindingByGroupSerializer(self.group, context=context)
        data = serializer.data

        # Should have only one source, deduplicated
        sources = data["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], "ws-123")

    def test_sources_returns_multiple_unique_resources(self):
        """Test that sources returns multiple unique resources when bindings come from different places."""
        self.group.principalCount = 1

        # Create bindings from different resources (direct + inherited)
        mock_binding1 = Mock()
        mock_binding1.role = self.role
        mock_binding1.resource_type = "workspace"
        mock_binding1.resource_id = "ws-direct"

        mock_binding2 = Mock()
        mock_binding2.role = self.role
        mock_binding2.resource_type = "workspace"
        mock_binding2.resource_id = "ws-parent"

        mock_binding_group1 = Mock()
        mock_binding_group1.binding = mock_binding1

        mock_binding_group2 = Mock()
        mock_binding_group2.binding = mock_binding2

        self.group.filtered_bindings = [mock_binding_group1, mock_binding_group2]

        context = {
            "request": Mock(),
            "resource_id": "ws-direct",
            "resource_type": "workspace",
        }

        serializer = RoleBindingByGroupSerializer(self.group, context=context)
        data = serializer.data

        # Should have two sources
        sources = data["sources"]
        self.assertEqual(len(sources), 2)
        source_ids = {s["id"] for s in sources}
        self.assertEqual(source_ids, {"ws-direct", "ws-parent"})


class RoleBindingListInputSerializerTest(TestCase):
    """Test the RoleBindingListInputSerializer.

    Validates query parameter parsing for GET /role-bindings/.
    Uses subTest for parametrized coverage of valid/invalid inputs.
    """

    # --- role_id ---

    def test_role_id_valid_inputs(self):
        """Test that valid UUID formats are accepted for role_id."""
        valid_uuids = [
            ("standard", "550e8400-e29b-41d4-a716-446655440000"),
            ("zeros", "00000000-0000-0000-0000-000000000000"),
            ("generated", str(uuid.uuid4())),
        ]
        for label, value in valid_uuids:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"role_id": value})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["role_id"], uuid.UUID(value))

    def test_role_id_invalid_inputs(self):
        """Test that non-UUID values are rejected for role_id."""
        invalid_values = [
            ("not-a-uuid", "not-a-uuid"),
            ("integer", "12345"),
        ]
        for label, value in invalid_values:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"role_id": value})
                self.assertFalse(s.is_valid())
                self.assertIn("role_id", s.errors)

    def test_role_id_empty_returns_none(self):
        """Test that empty string for role_id is treated as unset."""
        for label, value in [("empty", ""), ("spaces", "   ")]:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"role_id": value})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertIsNone(s.validated_data["role_id"])

    def test_role_id_omitted_is_valid(self):
        """Test that omitting role_id is valid (required=False)."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("role_id", s.validated_data)

    # --- fields ---

    def test_fields_valid_inputs(self):
        """Test that valid field selection strings are parsed correctly."""
        valid_fields = [
            ("role_name", "role(name)", {"role"}),
            ("subject_group", "subject(group.name)", {"subject"}),
            ("resource_type", "resource(type)", {"resource"}),
            ("combined", "role(name),resource(type)", {"role", "resource"}),
        ]
        for label, value, expected_nested_keys in valid_fields:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"fields": value})
                self.assertTrue(s.is_valid(), s.errors)
                fs = s.validated_data["fields"]
                self.assertIsNotNone(fs)
                for key in expected_nested_keys:
                    self.assertTrue(len(fs.get_nested(key)) > 0)

    def test_fields_invalid_inputs(self):
        """Test that invalid field selection strings are rejected."""
        invalid_fields = [
            ("unknown_object", "bogus(nope)"),
            ("invalid_role_field", "role(nonexistent)"),
            ("invalid_subject_field", "subject(bad_field)"),
        ]
        for label, value in invalid_fields:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"fields": value})
                self.assertFalse(s.is_valid())
                self.assertIn("fields", s.errors)

    def test_fields_omitted_is_valid(self):
        """Test that omitting fields is valid (required=False)."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("fields", s.validated_data)

    # --- order_by ---

    def test_order_by_valid_inputs(self):
        """Test that valid order_by values pass through."""
        valid_values = [
            ("role_name", "role.name"),
            ("descending", "-role.name"),
            ("role_uuid", "role.uuid"),
        ]
        for label, value in valid_values:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"order_by": value})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["order_by"], value)

    def test_order_by_omitted_is_valid(self):
        """Test that omitting order_by is valid (required=False)."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("order_by", s.validated_data)

    # --- resource_id ---

    def test_resource_id_valid_inputs(self):
        """Test that valid resource ID formats are accepted for resource_id."""
        valid_ids = [
            ("uuid", "550e8400-e29b-41d4-a716-446655440000"),
            ("zeros_uuid", "00000000-0000-0000-0000-000000000000"),
            ("generated_uuid", str(uuid.uuid4())),
            ("tenant_format", "redhat/12345678"),
        ]
        for label, value in valid_ids:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"resource_id": value, "resource_type": "workspace"})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["resource_id"], value)

    def test_resource_id_empty_returns_none(self):
        """Test that empty string for resource_id is treated as unset."""
        s = RoleBindingListInputSerializer(data={"resource_id": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data["resource_id"])

    def test_resource_id_omitted_is_valid(self):
        """Test that omitting resource_id is valid (required=False)."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("resource_id", s.validated_data)

    # --- resource_type ---

    def test_resource_type_valid_inputs(self):
        """Test that valid resource_type values are accepted."""
        cases = [
            ("workspace", "workspace"),
            ("custom", "custom_type"),
        ]
        resource_id = "550e8400-e29b-41d4-a716-446655440000"
        for label, value in cases:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"resource_type": value, "resource_id": resource_id})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["resource_type"], value)

    def test_resource_type_omitted_is_valid(self):
        """Test that omitting resource_type is valid (required=False)."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("resource_type", s.validated_data)

    def test_resource_id_and_type_together(self):
        """Test that resource_id and resource_type work together."""
        res_uuid = str(uuid.uuid4())
        s = RoleBindingListInputSerializer(data={"resource_id": res_uuid, "resource_type": "workspace"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["resource_id"], res_uuid)
        self.assertEqual(s.validated_data["resource_type"], "workspace")

    def test_resource_id_tenant_format_with_type(self):
        """Test that tenant resource ID format with resource_type=tenant works."""
        s = RoleBindingListInputSerializer(data={"resource_id": "redhat/12345678", "resource_type": "tenant"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["resource_id"], "redhat/12345678")
        self.assertEqual(s.validated_data["resource_type"], "tenant")

    # --- subject_type ---

    def test_subject_type_valid_inputs(self):
        """Test that valid subject_type values are accepted."""
        cases = [
            ("group", "group"),
            ("user", "user"),
        ]
        for label, value in cases:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"subject_type": value})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["subject_type"], value)

    def test_subject_type_omitted_is_valid(self):
        """Test that omitting subject_type is valid (required=False)."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("subject_type", s.validated_data)

    # --- subject_id ---

    def test_subject_id_valid_inputs(self):
        """Test that valid UUID formats are accepted for subject_id."""
        valid_uuids = [
            ("standard", "550e8400-e29b-41d4-a716-446655440000"),
            ("zeros", "00000000-0000-0000-0000-000000000000"),
            ("generated", str(uuid.uuid4())),
        ]
        for label, value in valid_uuids:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"subject_id": value})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["subject_id"], uuid.UUID(value))

    def test_subject_id_invalid_inputs(self):
        """Test that non-UUID values are rejected for subject_id."""
        invalid_values = [
            ("not-a-uuid", "not-a-uuid"),
            ("integer", "12345"),
        ]
        for label, value in invalid_values:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"subject_id": value})
                self.assertFalse(s.is_valid())
                self.assertIn("subject_id", s.errors)

    def test_subject_id_empty_returns_none(self):
        """Test that empty string for subject_id is treated as unset."""
        for label, value in [("empty", ""), ("spaces", "   ")]:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"subject_id": value})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertIsNone(s.validated_data["subject_id"])

    def test_subject_id_omitted_is_valid(self):
        """Test that omitting subject_id is valid (required=False)."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("subject_id", s.validated_data)

    # --- NUL byte sanitization ---

    def test_nul_bytes_stripped_from_all_string_params(self):
        """Test that NUL bytes are stripped from all string parameters."""
        test_cases = [
            ("fields", "\x00role(name)\x00", {}),
            ("order_by", "\x00role.name\x00", {}),
            ("resource_type", "\x00workspace\x00", {"resource_id": "550e8400-e29b-41d4-a716-446655440000"}),
            ("subject_type", "\x00group\x00", {}),
        ]
        for label, raw, extra_params in test_cases:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={label: raw, **extra_params})
                self.assertTrue(s.is_valid(), s.errors)

    def test_nul_bytes_stripped_from_resource_id(self):
        """Test that NUL bytes in resource_id are stripped before validation."""
        valid_uuid = "550e8400-e29b-41d4-a716-446655440000"
        s = RoleBindingListInputSerializer(data={"resource_id": f"\x00{valid_uuid}\x00", "resource_type": "workspace"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["resource_id"], valid_uuid)
        self.assertIsInstance(s.validated_data["resource_id"], str)

    def test_nul_bytes_stripped_from_role_id(self):
        """Test that NUL bytes in role_id are stripped before UUID validation."""
        valid_uuid = "550e8400-e29b-41d4-a716-446655440000"
        s = RoleBindingListInputSerializer(data={"role_id": f"\x00{valid_uuid}\x00"})
        self.assertTrue(s.is_valid(), s.errors)

    def test_nul_bytes_stripped_from_subject_id(self):
        """Test that NUL bytes in subject_id are stripped before UUID validation."""
        valid_uuid = "550e8400-e29b-41d4-a716-446655440000"
        s = RoleBindingListInputSerializer(data={"subject_id": f"\x00{valid_uuid}\x00"})
        self.assertTrue(s.is_valid(), s.errors)

    # --- empty string normalization (all optional filter params) ---

    def test_all_empty_filter_params_valid(self):
        """Test that all filter params as empty strings produce valid result with None values."""
        s = RoleBindingListInputSerializer(
            data={
                "role_id": "",
                "resource_id": "",
                "resource_type": "",
                "subject_type": "",
                "subject_id": "",
                "granted_subject_type": "",
                "granted_subject_id": "",
                "fields": "",
                "order_by": "",
                "exclude_sources": "",
            }
        )
        self.assertTrue(s.is_valid(), s.errors)
        for field in [
            "role_id",
            "resource_id",
            "resource_type",
            "subject_type",
            "subject_id",
            "granted_subject_type",
            "granted_subject_id",
            "fields",
            "order_by",
        ]:
            self.assertIsNone(s.validated_data[field], f"{field} should be None for empty string")
        self.assertEqual(
            s.validated_data["exclude_sources"], ExcludeSources.NONE, "exclude_sources should default to 'none'"
        )

    def test_resource_type_empty_returns_none(self):
        """Test that empty string for resource_type is treated as unset."""
        s = RoleBindingListInputSerializer(data={"resource_type": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data["resource_type"])

    def test_subject_type_empty_returns_none(self):
        """Test that empty string for subject_type is treated as unset."""
        s = RoleBindingListInputSerializer(data={"subject_type": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data["subject_type"])

    def test_granted_subject_type_empty_returns_none(self):
        """Test that empty string for granted_subject_type is treated as unset."""
        s = RoleBindingListInputSerializer(data={"granted_subject_type": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data["granted_subject_type"])

    def test_granted_subject_id_empty_returns_none(self):
        """Test that empty string for granted_subject_id is treated as unset."""
        s = RoleBindingListInputSerializer(data={"granted_subject_id": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data["granted_subject_id"])

    # --- granted_subject_type ---

    def test_granted_subject_type_valid_inputs(self):
        """Test that valid granted_subject_type values are accepted."""
        user_group_cases = [
            ("user", "user"),
            ("group", "group"),
        ]
        for label, value in user_group_cases:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(
                    data={"granted_subject_type": value, "granted_subject_id": str(uuid.uuid4())}
                )
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["granted_subject_type"], value)
        s = RoleBindingListInputSerializer(
            data={"granted_subject_type": "principal", "granted_subject.principal.user_id": "ext-id"}
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["granted_subject_type"], "principal")

    def test_granted_subject_type_invalid_value(self):
        """Test that invalid granted_subject_type values are rejected."""
        s = RoleBindingListInputSerializer(
            data={"granted_subject_type": "service-account", "granted_subject_id": str(uuid.uuid4())}
        )
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_granted_subject_type_omitted_is_valid(self):
        """Test that omitting granted_subject_type is valid (required=False)."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("granted_subject_type", s.validated_data)

    # --- granted_subject_id ---

    def test_granted_subject_id_valid_inputs(self):
        """Test that valid string values are accepted for granted_subject_id."""
        valid_values = [
            ("uuid", "550e8400-e29b-41d4-a716-446655440000"),
            ("arbitrary_string", "some-identifier"),
            ("generated_uuid", str(uuid.uuid4())),
        ]
        for label, value in valid_values:
            with self.subTest(label=label):
                s = RoleBindingListInputSerializer(data={"granted_subject_type": "user", "granted_subject_id": value})
                self.assertTrue(s.is_valid(), s.errors)
                self.assertEqual(s.validated_data["granted_subject_id"], value)

    # --- granted_subject cross-field validation ---

    def test_granted_subject_id_without_type_is_invalid(self):
        """Test that providing granted_subject_id without granted_subject_type is rejected."""
        s = RoleBindingListInputSerializer(data={"granted_subject_id": str(uuid.uuid4())})
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_granted_subject_with_subject_type_is_invalid(self):
        """Test that granted_subject params cannot be combined with subject_type."""
        s = RoleBindingListInputSerializer(
            data={
                "granted_subject_type": "user",
                "granted_subject_id": str(uuid.uuid4()),
                "subject_type": "group",
            }
        )
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_granted_subject_with_subject_id_is_invalid(self):
        """Test that granted_subject params cannot be combined with subject_id."""
        s = RoleBindingListInputSerializer(
            data={
                "granted_subject_type": "user",
                "granted_subject_id": str(uuid.uuid4()),
                "subject_id": str(uuid.uuid4()),
            }
        )
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    # --- resource.tenant.org_id (dotted query param) ---

    def test_resource_tenant_org_id_accepted_via_dotted_key(self):
        """Test that resource.tenant.org_id is remapped and accepted."""
        s = RoleBindingListInputSerializer(data={"resource.tenant.org_id": "12345678"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["resource_tenant_org_id"], "12345678")

    def test_resource_tenant_org_id_with_resource_type_tenant(self):
        """Test that resource.tenant.org_id with resource_type=tenant is valid."""
        s = RoleBindingListInputSerializer(data={"resource.tenant.org_id": "12345678", "resource_type": "tenant"})
        self.assertTrue(s.is_valid(), s.errors)

    def test_resource_tenant_org_id_with_resource_id_is_invalid(self):
        """Test that resource.tenant.org_id cannot be combined with resource_id."""
        s = RoleBindingListInputSerializer(
            data={"resource.tenant.org_id": "12345678", "resource_id": "redhat/12345678"}
        )
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_resource_tenant_org_id_with_wrong_resource_type_is_invalid(self):
        """Test that resource.tenant.org_id with resource_type != 'tenant' is rejected."""
        s = RoleBindingListInputSerializer(data={"resource.tenant.org_id": "12345678", "resource_type": "workspace"})
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_resource_tenant_org_id_omitted_is_valid(self):
        """Test that omitting resource.tenant.org_id is valid."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("resource_tenant_org_id", s.validated_data)

    # --- granted_subject.principal.user_id (dotted query param) ---

    def test_granted_subject_principal_user_id_accepted_via_dotted_key(self):
        """Test that granted_subject.principal.user_id is remapped and accepted."""
        s = RoleBindingListInputSerializer(
            data={
                "granted_subject_type": "principal",
                "granted_subject.principal.user_id": "jsmith",
            }
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["granted_subject_principal_user_id"], "jsmith")

    def test_granted_subject_principal_user_id_without_principal_type_is_invalid(self):
        """Test that granted_subject.principal.user_id requires granted_subject_type=principal."""
        s = RoleBindingListInputSerializer(
            data={
                "granted_subject_type": "user",
                "granted_subject_id": str(uuid.uuid4()),
                "granted_subject.principal.user_id": "jsmith",
            }
        )
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_granted_subject_principal_user_id_alone_is_invalid(self):
        """Test that granted_subject.principal.user_id without granted_subject_type is invalid."""
        s = RoleBindingListInputSerializer(data={"granted_subject.principal.user_id": "jsmith"})
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_granted_subject_principal_user_id_omitted_is_valid(self):
        """Test that omitting granted_subject.principal.user_id is valid."""
        s = RoleBindingListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("granted_subject_principal_user_id", s.validated_data)

    # --- granted_subject_type=principal ---

    def test_granted_subject_type_principal_without_user_id_is_invalid(self):
        """Test that granted_subject_type=principal requires granted_subject.principal.user_id."""
        s = RoleBindingListInputSerializer(data={"granted_subject_type": "principal"})
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_granted_subject_type_principal_with_user_id(self):
        """Test that granted_subject_type=principal with granted_subject.principal.user_id is valid."""
        s = RoleBindingListInputSerializer(
            data={
                "granted_subject_type": "principal",
                "granted_subject.principal.user_id": "jsmith",
            }
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_granted_subject_type_principal_with_subject_type_is_invalid(self):
        """Test that granted_subject_type=principal cannot be combined with subject_type."""
        s = RoleBindingListInputSerializer(
            data={
                "granted_subject_type": "principal",
                "granted_subject.principal.user_id": "jsmith",
                "subject_type": "group",
            }
        )
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_granted_subject_type_user_without_id_is_invalid(self):
        """Test that granted_subject_type=user requires granted_subject_id."""
        s = RoleBindingListInputSerializer(data={"granted_subject_type": "user"})
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    def test_granted_subject_type_group_without_id_is_invalid(self):
        """Test that granted_subject_type=group requires granted_subject_id."""
        s = RoleBindingListInputSerializer(data={"granted_subject_type": "group"})
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)

    # --- Combined ---

    def test_all_params_together(self):
        """Test that all parameters work together."""
        s = RoleBindingListInputSerializer(
            data={
                "role_id": str(uuid.uuid4()),
                "resource_id": str(uuid.uuid4()),
                "resource_type": "workspace",
                "subject_type": "group",
                "subject_id": str(uuid.uuid4()),
                "fields": "role(name),resource(type)",
                "order_by": "-role.name",
            }
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIn("role_id", s.validated_data)
        self.assertIn("resource_id", s.validated_data)
        self.assertIn("resource_type", s.validated_data)
        self.assertIn("subject_type", s.validated_data)
        self.assertIn("subject_id", s.validated_data)
        self.assertIsNotNone(s.validated_data["fields"])
        self.assertEqual(s.validated_data["order_by"], "-role.name")

    def test_granted_subject_with_other_filters(self):
        """Test that granted_subject params work with non-subject filters."""
        s = RoleBindingListInputSerializer(
            data={
                "role_id": str(uuid.uuid4()),
                "resource_id": str(uuid.uuid4()),
                "resource_type": "workspace",
                "granted_subject_type": "user",
                "granted_subject_id": str(uuid.uuid4()),
                "fields": "role(name),resource(type)",
            }
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIn("granted_subject_type", s.validated_data)
        self.assertIn("granted_subject_id", s.validated_data)
        self.assertIn("role_id", s.validated_data)


class RoleBindingInputSerializerTest(TestCase):
    """Test the RoleBindingInputSerializer (by-subject endpoint).

    Validates empty-as-unset normalization for query parameters
    on GET /role-bindings/by-subject/.
    """

    def test_resource_id_empty_triggers_cross_field_error(self):
        """Test that empty resource_id is normalized to None, triggering cross-field validation."""
        s = RoleBindingInputSerializer(data={"resource_id": "", "resource_type": "workspace"})
        self.assertFalse(s.is_valid())
        self.assertIn("resource_id", s.errors)

    def test_resource_type_empty_triggers_cross_field_error(self):
        """Test that empty resource_type is normalized to None, triggering cross-field validation."""
        s = RoleBindingInputSerializer(data={"resource_id": "test-id", "resource_type": ""})
        self.assertFalse(s.is_valid())
        self.assertIn("resource_type", s.errors)

    def test_resource_tenant_org_id_empty_returns_none(self):
        """Test that empty resource.tenant.org_id is normalized to None (falls through to resource_id check)."""
        s = RoleBindingInputSerializer(
            data={"resource.tenant.org_id": "", "resource_id": "test-id", "resource_type": "workspace"}
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("resource_tenant_org_id"))

    def test_subject_type_empty_returns_none(self):
        """Test that empty subject_type is normalized to None."""
        s = RoleBindingInputSerializer(
            data={"resource_id": "test-id", "resource_type": "workspace", "subject_type": ""}
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("subject_type"))

    def test_valid_resource_params_pass_through(self):
        """Test that valid resource params are not modified."""
        s = RoleBindingInputSerializer(data={"resource_id": "test-id", "resource_type": "workspace"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["resource_id"], "test-id")
        self.assertEqual(s.validated_data["resource_type"], "workspace")


class RoleBindingListOutputSerializerTest(IdentityRequest):
    """Test the RoleBindingListOutputSerializer.

    Tests verify the serializer produces output matching the API spec:
    - role: {id: UUID, name?: string}
    - subject: {id?: UUID, type: "group", group?: {name, description, user_count}}
    - resource: {id: string, type?: string}

    Uses subTest for parametrized field selection coverage.
    """

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.permission = Permission.objects.create(
            permission="app:resource:read",
            tenant=self.tenant,
        )

        self.role = RoleV2.objects.create(
            name="test_role",
            tenant=self.tenant,
        )
        self.role.permissions.add(self.permission)

        self.group = Group.objects.create(
            name="test_group",
            description="Test group description",
            tenant=self.tenant,
        )

        self.principal = Principal.objects.create(
            username=self.user_data["username"],
            tenant=self.tenant,
            type=Principal.Types.USER,
        )
        self.group.principals.add(self.principal)

        self.binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        self.binding_group = RoleBindingGroup.objects.create(
            group=self.group,
            binding=self.binding,
        )

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        self.group.principals.clear()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def _serialize(self, obj, field_selection=None):
        """Serialize a RoleBinding with optional field selection."""
        context = {"request": Mock(), "field_selection": field_selection}
        return RoleBindingListOutputSerializer(obj, context=context).data

    # --- Default behavior (no field selection) ---

    def test_default_response_structure(self):
        """Test that default response has role, subject, resource keys only."""
        data = self._serialize(self.binding)

        self.assertIn("role", data)
        self.assertIn("subject", data)
        self.assertIn("resource", data)
        self.assertNotIn("last_modified", data)
        self.assertNotIn("roles", data)

    def test_default_role_returns_id_only(self):
        """Test that default role contains only id."""
        data = self._serialize(self.binding)

        self.assertEqual(data["role"], {"id": self.role.uuid})

    def test_default_subject_returns_id_and_type(self):
        """Test that default subject contains id and type."""
        data = self._serialize(self.binding)

        self.assertEqual(data["subject"]["id"], self.group.uuid)
        self.assertEqual(data["subject"]["type"], "group")
        self.assertNotIn("group", data["subject"])

    def test_default_resource_returns_id_only(self):
        """Test that default resource contains only id."""
        data = self._serialize(self.binding)

        self.assertEqual(data["resource"], {"id": "ws-12345"})

    # --- Edge cases ---

    def test_subject_no_group_entries(self):
        """Test that subject returns type-only when binding has no group entries."""
        # Delete the binding-group relationship
        self.binding_group.delete()

        data = self._serialize(self.binding)

        self.assertEqual(data["subject"], {"type": "group"})

    def test_many_serialization(self):
        """Test serialization of multiple bindings."""
        role2 = RoleV2.objects.create(name="test_role_2", tenant=self.tenant)
        binding2 = RoleBinding.objects.create(
            role=role2,
            resource_type="workspace",
            resource_id="ws-99999",
            tenant=self.tenant,
        )

        context = {"request": Mock(), "field_selection": None}
        data = RoleBindingListOutputSerializer([self.binding, binding2], many=True, context=context).data

        self.assertEqual(len(data), 2)
        role_ids = {item["role"]["id"] for item in data}
        self.assertIn(self.role.uuid, role_ids)
        self.assertIn(role2.uuid, role_ids)

        binding2.delete()
        role2.delete()

    # --- Field selection (parametrized) ---

    def test_field_selection_includes_requested_fields(self):
        """Test that field selection includes the correct fields in response."""
        test_cases = [
            (
                "role_name",
                "role(name)",
                lambda d: ("name" in d["role"] and d["role"]["name"] == "test_role"),
            ),
            (
                "resource_type",
                "resource(type)",
                lambda d: ("type" in d["resource"] and d["resource"]["type"] == "workspace"),
            ),
            (
                "subject_id",
                "subject(id)",
                lambda d: ("id" in d["subject"]),
            ),
            (
                "subject_group_name",
                "subject(group.name)",
                lambda d: (
                    "group" in d["subject"]
                    and d["subject"]["group"]["name"] == "test_group"
                    and "id" not in d["subject"]  # id excluded when not requested
                ),
            ),
            (
                "subject_group_description",
                "subject(group.description)",
                lambda d: (
                    "group" in d["subject"] and d["subject"]["group"]["description"] == "Test group description"
                ),
            ),
        ]
        for label, fields_str, check_fn in test_cases:
            with self.subTest(label=label):
                fs = RoleBindingFieldSelection.parse(fields_str)
                data = self._serialize(self.binding, field_selection=fs)
                self.assertTrue(check_fn(data), f"Check failed for {label}: {data}")

    def test_field_selection_excludes_unrequested_fields(self):
        """Test that unrequested fields are excluded from response."""
        test_cases = [
            ("role_name_only", "role(name)", "resource", lambda d: "type" not in d["resource"]),
            ("resource_type_only", "resource(type)", "role", lambda d: "name" not in d["role"]),
        ]
        for label, fields_str, check_key, check_fn in test_cases:
            with self.subTest(label=label):
                fs = RoleBindingFieldSelection.parse(fields_str)
                data = self._serialize(self.binding, field_selection=fs)
                self.assertTrue(check_fn(data), f"Exclusion check failed for {label}: {data}")

    def test_combined_field_selection(self):
        """Test combined field selection across multiple objects."""
        fs = RoleBindingFieldSelection.parse("role(name),subject(group.name),resource(type)")
        data = self._serialize(self.binding, field_selection=fs)

        # Role includes id (always) + name
        self.assertEqual(data["role"]["id"], self.role.uuid)
        self.assertEqual(data["role"]["name"], "test_role")

        # Subject includes type (always) + group.name, but not id
        self.assertEqual(data["subject"]["type"], "group")
        self.assertNotIn("id", data["subject"])
        self.assertEqual(data["subject"]["group"]["name"], "test_group")

        # Resource includes id (always) + type
        self.assertEqual(data["resource"]["id"], "ws-12345")
        self.assertEqual(data["resource"]["type"], "workspace")

    def test_field_selection_resource_name(self):
        """resource(name) resolves workspace display name via queryset annotation."""
        prev_resource_id = self.binding.resource_id
        root = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=root,
        )
        ws = Workspace.objects.create(
            name="List RB Name Test WS",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=default_ws,
        )
        try:
            self.binding.resource_id = str(ws.id)
            self.binding.save(update_fields=["resource_id"])

            annotated_binding = RoleBinding.objects.filter(pk=self.binding.pk).with_resource_names().get()
            fs = RoleBindingFieldSelection.parse("resource(name)")
            data = RoleBindingListOutputSerializer(
                annotated_binding,
                context={"request": Mock(), "field_selection": fs},
            ).data
            self.assertEqual(data["resource"]["name"], "List RB Name Test WS")
        finally:
            self.binding.resource_id = prev_resource_id
            self.binding.save(update_fields=["resource_id"])
            ws.delete()
            default_ws.delete()
            root.delete()


class ExpandPlatformRolesTest(IdentityRequest):
    """Test the _expand_platform_roles helper from the view module."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        from management.role.v2_model import PlatformRoleV2, SeededRoleV2

        self.PlatformRoleV2 = PlatformRoleV2
        self.SeededRoleV2 = SeededRoleV2

    def test_non_platform_binding_passes_through(self):
        """Test that a non-platform role binding is returned unchanged."""
        from management.role_binding.view import _expand_platform_roles

        role = RoleV2.objects.create(name="custom_role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=role, resource_type="workspace", resource_id="ws-1", tenant=self.tenant
        )
        self.addCleanup(binding.delete)
        self.addCleanup(role.delete)

        result = _expand_platform_roles([binding])

        self.assertEqual(len(result), 1)
        self.assertIs(result[0], binding)

    def test_platform_role_expanded_to_children(self):
        """Test that a platform role binding is expanded into one entry per child."""
        from management.role_binding.view import _expand_platform_roles

        public_tenant = self.tenant
        platform_role = self.PlatformRoleV2.objects.create(name="Platform Test", tenant=public_tenant)
        child_a = self.SeededRoleV2.objects.create(name="Child A", tenant=public_tenant)
        child_b = self.SeededRoleV2.objects.create(name="Child B", tenant=public_tenant)
        platform_role.children.add(child_a, child_b)

        binding = RoleBinding.objects.create(
            role=platform_role, resource_type="workspace", resource_id="ws-1", tenant=self.tenant
        )
        group = Group.objects.create(name="test_expand_group", tenant=self.tenant)
        RoleBindingGroup.objects.create(group=group, binding=binding)

        self.addCleanup(RoleBindingGroup.objects.filter(binding=binding).delete)
        self.addCleanup(binding.delete)
        self.addCleanup(group.delete)
        self.addCleanup(platform_role.children.clear)
        self.addCleanup(child_a.delete)
        self.addCleanup(child_b.delete)
        self.addCleanup(platform_role.delete)

        result = _expand_platform_roles([binding])

        self.assertEqual(len(result), 2)
        role_uuids = {entry.role.uuid for entry in result}
        self.assertEqual(role_uuids, {child_a.uuid, child_b.uuid})

        # Verify proxy preserves binding attributes
        for entry in result:
            self.assertEqual(entry.resource_type, "workspace")
            self.assertEqual(entry.resource_id, "ws-1")

    def test_platform_role_no_children_produces_no_entries(self):
        """Test that a platform role with no children produces zero entries."""
        from management.role_binding.view import _expand_platform_roles

        platform_role = self.PlatformRoleV2.objects.create(name="Empty Platform", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=platform_role, resource_type="workspace", resource_id="ws-1", tenant=self.tenant
        )
        self.addCleanup(binding.delete)
        self.addCleanup(platform_role.delete)

        result = _expand_platform_roles([binding])

        self.assertEqual(len(result), 0)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class BatchCreateRequestSerializerTests(IdentityRequest):
    """Tests for BatchCreateRoleBindingRequestSerializer input validation."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.permission = Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        self.role_service = RoleV2Service()
        self.role = self.role_service.create(
            name="test_role",
            description="Test role",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "read"}],
            tenant=self.tenant,
        )
        self.group = Group.objects.create(name="test_group", tenant=self.tenant)
        self.mock_request = Mock()
        self.mock_request.tenant = self.tenant

        self.valid_payload = {
            "requests": [
                {
                    "resource": {"id": str(uuid.uuid4()), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                }
            ],
        }

    def tearDown(self):
        """Tear down test data."""
        RoleBinding.objects.all().delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def _make_serializer(self, data):
        return BatchCreateRoleBindingRequestSerializer(data=data, context={"request": self.mock_request})

    def test_valid_request_passes_validation(self):
        """Minimal valid payload passes is_valid()."""
        serializer = self._make_serializer(self.valid_payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_empty_requests_list(self):
        """Empty requests list fails min_length=1."""
        serializer = self._make_serializer({"requests": []})
        self.assertFalse(serializer.is_valid())
        self.assertIn("requests", serializer.errors)

    def test_rejects_missing_requests(self):
        """Missing requests key fails."""
        serializer = self._make_serializer({})
        self.assertFalse(serializer.is_valid())
        self.assertIn("requests", serializer.errors)

    def test_rejects_invalid_subject_type(self):
        """Subject type not in ['user', 'group'] fails."""
        payload = {
            "requests": [
                {
                    "resource": {"id": str(uuid.uuid4()), "type": "workspace"},
                    "subject": {"id": str(uuid.uuid4()), "type": "foo_type"},
                    "role": {"id": str(uuid.uuid4())},
                }
            ],
        }
        serializer = self._make_serializer(payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("requests", serializer.errors)

    def test_rejects_missing_role_id(self):
        """Request item with missing role.id fails."""
        payload = {
            "requests": [
                {
                    "resource": {"id": str(uuid.uuid4()), "type": "workspace"},
                    "subject": {"id": str(uuid.uuid4()), "type": "group"},
                    "role": {},
                }
            ],
        }
        serializer = self._make_serializer(payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("requests", serializer.errors)

    def test_rejects_invalid_uuid(self):
        """Non-UUID role.id fails."""
        payload = {
            "requests": [
                {
                    "resource": {"id": str(uuid.uuid4()), "type": "workspace"},
                    "subject": {"id": str(uuid.uuid4()), "type": "group"},
                    "role": {"id": "not-a-uuid"},
                }
            ],
        }
        serializer = self._make_serializer(payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("requests", serializer.errors)

    def test_rejects_invalid_fields_param(self):
        """Invalid field mask fails validation."""
        payload = {**self.valid_payload, "fields": "unknown(foo)"}
        serializer = self._make_serializer(payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("fields", serializer.errors)

    def test_valid_fields_param_parsed(self):
        """Valid field mask is parsed into RoleBindingFieldSelection."""
        payload = {**self.valid_payload, "fields": "role(name)"}
        serializer = self._make_serializer(payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNotNone(serializer.validated_data["fields"])
        self.assertIn("name", serializer.validated_data["fields"].get_nested("role"))

    def test_valid_request_with_tenant_resource(self):
        """Batch create with resource_type=tenant and redhat/org_id format passes validation."""
        payload = {
            "requests": [
                {
                    "resource": {"id": "redhat/12345678", "type": "tenant"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                }
            ],
        }
        serializer = self._make_serializer(payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_resource_id_is_string_after_validation(self):
        """Resource ID is a plain string after validation, for both UUID and tenant formats."""
        cases = [
            ("uuid", str(uuid.uuid4()), "workspace"),
            ("tenant", "redhat/12345678", "tenant"),
        ]
        for label, resource_id, resource_type in cases:
            with self.subTest(label=label):
                payload = {
                    "requests": [
                        {
                            "resource": {"id": resource_id, "type": resource_type},
                            "subject": {"id": str(self.group.uuid), "type": "group"},
                            "role": {"id": str(self.role.uuid)},
                        }
                    ],
                }
                serializer = self._make_serializer(payload)
                self.assertTrue(serializer.is_valid(), serializer.errors)
                validated_id = serializer.validated_data["requests"][0]["resource"]["id"]
                self.assertIsInstance(validated_id, str)
                self.assertEqual(validated_id, resource_id)

    def test_rejects_over_max_items_limit(self):
        """101 items exceeds max_length=100 and fails validation."""
        payload = {
            "requests": [
                {
                    "resource": {"id": str(uuid.uuid4()), "type": "workspace"},
                    "subject": {"id": str(self.group.uuid), "type": "group"},
                    "role": {"id": str(self.role.uuid)},
                }
            ]
            * 101,
        }
        serializer = self._make_serializer(payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("requests", serializer.errors)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class BatchCreateResponseSerializerTests(IdentityRequest):
    """Tests for BatchCreateRoleBindingResponseItemSerializer output formatting."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.role = RoleV2.objects.create(name="test_role", tenant=self.tenant)
        self.group = Group.objects.create(name="test_group", description="Test group", tenant=self.tenant)
        self.principal = Principal.objects.create(
            username="testuser", tenant=self.tenant, user_id="testuser", type=Principal.Types.USER
        )

        self.group_result = {
            "role": self.role,
            "subject_type": "group",
            "subject": self.group,
            "resource_type": "workspace",
            "resource_id": "ws-123",
            "resource_name": "Test Workspace",
        }

    def tearDown(self):
        """Tear down test data."""
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def test_default_fields(self):
        """Default output includes role.id, subject.id, subject.type, resource.id."""
        serializer = BatchCreateRoleBindingResponseItemSerializer(self.group_result, context={})
        data = serializer.data

        self.assertEqual(data["role"], {"id": self.role.uuid})
        self.assertEqual(data["subject"]["id"], self.group.uuid)
        self.assertEqual(data["subject"]["type"], "group")
        self.assertEqual(data["resource"], {"id": "ws-123"})

    def test_fields_context_filters_response(self):
        """With fields=role(name,id), response includes only role with those sub-fields."""
        fields = RoleBindingFieldSelection.parse("role(name,id)")
        serializer = BatchCreateRoleBindingResponseItemSerializer(
            self.group_result, context={"field_selection": fields}
        )
        data = serializer.data

        self.assertEqual(data["role"]["id"], self.role.uuid)
        self.assertEqual(data["role"]["name"], "test_role")

    def test_fields_context_masks_sub_fields(self):
        """With fields=role(id), only role.id is populated."""
        fields = RoleBindingFieldSelection.parse("role(id)")
        serializer = BatchCreateRoleBindingResponseItemSerializer(
            self.group_result, context={"field_selection": fields}
        )
        data = serializer.data

        self.assertEqual(data["role"], {"id": self.role.uuid})
        self.assertNotIn("name", data["role"])
        self.assertNotIn("subject", data)
        self.assertNotIn("resource", data)

    def test_group_subject_includes_group_details(self):
        """With fields=subject(group.name), group sub-object is included."""
        fields = RoleBindingFieldSelection.parse("subject(group.name)")
        serializer = BatchCreateRoleBindingResponseItemSerializer(
            self.group_result, context={"field_selection": fields}
        )
        data = serializer.data

        self.assertIn("group", data["subject"])
        self.assertEqual(data["subject"]["group"]["name"], "test_group")


class UpdateRoleBindingRequestSerializerTests(IdentityRequest):
    """Tests for UpdateRoleBindingRequestSerializer.

    Organized into parameterized groups:
    - Happy-path valid inputs
    - Missing required fields
    - Invalid input / validation errors
    - NUL byte sanitization
    """

    def setUp(self):
        """Set up test data."""
        super().setUp()

    def _make_valid_data(self, **overrides):
        """Build a valid serializer input dict, with optional field overrides.

        Use ``_REMOVE`` sentinel value to omit a key entirely.
        """
        data = {
            "resource_id": "ws-123",
            "resource_type": "workspace",
            "subject_id": "550e8400-e29b-41d4-a716-446655440000",
            "subject_type": "group",
            "roles": [{"id": "550e8400-e29b-41d4-a716-446655440001"}],
        }
        for key, value in overrides.items():
            if value is _REMOVE:
                data.pop(key, None)
            else:
                data[key] = value
        return data

    # ── Happy-path tests (parameterized) ─────────────────────────────

    def test_valid_inputs(self):
        """Test various valid inputs pass validation."""
        cases = [
            ("group subject type", {}),
            ("user subject type", {"subject_type": "user"}),
            (
                "multiple roles",
                {
                    "roles": [
                        {"id": "550e8400-e29b-41d4-a716-446655440001"},
                        {"id": "550e8400-e29b-41d4-a716-446655440002"},
                    ]
                },
            ),
            ("empty roles list removes all bindings", {"roles": []}),
            ("fields param parsed", {"fields": "subject(group.name)"}),
            ("fields omitted", {"fields": _REMOVE}),
            ("fields blank string", {"fields": ""}),
        ]

        for description, overrides in cases:
            with self.subTest(case=description):
                data = self._make_valid_data(**overrides)
                serializer = UpdateRoleBindingRequestSerializer(data=data)
                self.assertTrue(serializer.is_valid(), f"{description}: {serializer.errors}")

    def test_fields_param_parsed_into_field_selection(self):
        """Test that a valid fields param is parsed into a FieldSelection object."""
        data = self._make_valid_data(fields="subject(group.name)")
        serializer = UpdateRoleBindingRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsInstance(serializer.validated_data["fields"], FieldSelection)
        self.assertIn("group.name", serializer.validated_data["fields"].get_nested("subject"))

    def test_fields_omitted_defaults_to_default_selection(self):
        """Test that omitting the fields param applies the default field selection."""
        data = self._make_valid_data(fields=_REMOVE)
        serializer = UpdateRoleBindingRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        field_selection = serializer.validated_data["fields"]
        self.assertIsInstance(field_selection, FieldSelection)
        self.assertIn("id", field_selection.get_nested("resource"))
        self.assertIn("id", field_selection.get_nested("subject"))
        self.assertIn("id", field_selection.get_nested("roles"))

    def test_fields_blank_string_defaults_to_default_selection(self):
        """Test that a blank fields param applies the default field selection."""
        data = self._make_valid_data(fields="")
        serializer = UpdateRoleBindingRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        field_selection = serializer.validated_data["fields"]
        self.assertIsInstance(field_selection, FieldSelection)
        self.assertIn("id", field_selection.get_nested("resource"))
        self.assertIn("id", field_selection.get_nested("subject"))
        self.assertIn("id", field_selection.get_nested("roles"))

    def test_multiple_roles_validated(self):
        """Test that multiple valid role UUIDs are accepted."""
        data = self._make_valid_data(
            roles=[
                {"id": "550e8400-e29b-41d4-a716-446655440001"},
                {"id": "550e8400-e29b-41d4-a716-446655440002"},
            ]
        )
        serializer = UpdateRoleBindingRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(len(serializer.validated_data["roles"]), 2)

    # ── resource.tenant.org_id tests ──────────────────────────────────

    def test_resource_tenant_org_id_accepted_via_dotted_key(self):
        """Test that resource.tenant.org_id is remapped and accepted."""
        data = self._make_valid_data(resource_id=_REMOVE, resource_type=_REMOVE)
        data["resource.tenant.org_id"] = "12345678"
        s = UpdateRoleBindingRequestSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["resource_tenant_org_id"], "12345678")

    def test_resource_tenant_org_id_with_resource_id_is_invalid(self):
        """Test that resource.tenant.org_id cannot be combined with resource_id."""
        data = self._make_valid_data()
        data["resource.tenant.org_id"] = "12345678"
        s = UpdateRoleBindingRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("resource.tenant.org_id cannot be combined", str(s.errors))

    def test_resource_tenant_org_id_with_wrong_resource_type_is_invalid(self):
        """Test that resource.tenant.org_id with resource_type != 'tenant' is rejected."""
        data = self._make_valid_data(resource_id=_REMOVE)
        data["resource.tenant.org_id"] = "12345678"
        data["resource_type"] = "workspace"
        s = UpdateRoleBindingRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("resource_type must be 'tenant'", str(s.errors))

    # ── Empty resource_* normalization ────────────────────────────────

    def test_resource_id_empty_triggers_cross_field_error(self):
        """Test that empty resource_id is normalized to None, triggering cross-field validation."""
        data = self._make_valid_data(resource_id="")
        s = UpdateRoleBindingRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("resource_id", s.errors)

    def test_resource_type_empty_triggers_cross_field_error(self):
        """Test that empty resource_type is normalized to None, triggering cross-field validation."""
        data = self._make_valid_data(resource_type="")
        s = UpdateRoleBindingRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("resource_type", s.errors)

    def test_resource_tenant_org_id_empty_returns_none(self):
        """Test that empty resource.tenant.org_id is normalized to None."""
        data = self._make_valid_data()
        data["resource.tenant.org_id"] = ""
        s = UpdateRoleBindingRequestSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("resource_tenant_org_id"))

    # ── Missing required fields (parameterized) ──────────────────────

    def test_missing_required_field_returns_error(self):
        """Test that omitting any single required field fails validation."""
        required_fields = ["subject_id", "subject_type", "roles"]

        for field in required_fields:
            with self.subTest(missing_field=field):
                data = self._make_valid_data(**{field: _REMOVE})
                serializer = UpdateRoleBindingRequestSerializer(data=data)
                self.assertFalse(serializer.is_valid())
                self.assertIn(field, serializer.errors)

    def test_missing_resource_id_without_org_id_returns_error(self):
        """Test that omitting resource_id without resource.tenant.org_id fails validation."""
        data = self._make_valid_data(resource_id=_REMOVE)
        serializer = UpdateRoleBindingRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_id", serializer.errors)

    def test_missing_resource_type_without_org_id_returns_error(self):
        """Test that omitting resource_type without resource.tenant.org_id fails validation."""
        data = self._make_valid_data(resource_type=_REMOVE)
        serializer = UpdateRoleBindingRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_type", serializer.errors)

    # ── Validation error tests (parameterized) ───────────────────────

    def test_invalid_input_returns_validation_error(self):
        """Test various invalid inputs are rejected with correct error fields and messages."""
        cases = [
            # (description, overrides, error_field, expected_message_substring)
            (
                "invalid subject type",
                {"subject_type": "invalid_type"},
                "subject_type",
                "Unsupported subject type: 'invalid_type'",
            ),
            (
                "invalid UUID in roles",
                {"roles": [{"id": "not-a-uuid"}]},
                "roles",
                None,  # DRF UUID message varies; just check field presence
            ),
            (
                "missing id in role item",
                {"roles": [{}]},
                "roles",
                None,
            ),
            (
                "invalid fields param",
                {"fields": "bogus_field"},
                "fields",
                "Invalid field(s):",
            ),
        ]

        for description, overrides, error_field, expected_msg in cases:
            with self.subTest(case=description):
                data = self._make_valid_data(**overrides)
                serializer = UpdateRoleBindingRequestSerializer(data=data)
                self.assertFalse(serializer.is_valid(), f"Expected invalid for: {description}")
                self.assertIn(error_field, serializer.errors, f"Expected '{error_field}' in errors for: {description}")
                if expected_msg:
                    error_messages = str(serializer.errors[error_field])
                    self.assertIn(expected_msg, error_messages, f"Expected message for: {description}")

    # ── NUL byte sanitization (parameterized) ────────────────────────

    def test_nul_bytes_sanitized_from_string_fields(self):
        """Test that NUL bytes are stripped from all string fields."""
        cases = [
            ("resource_id", "ws-123\x00", "ws-123"),
            ("resource_type", "work\x00space", "workspace"),
            (
                "subject_id",
                "550e8400-e29b-41d4-a716-446655440000\x00",
                "550e8400-e29b-41d4-a716-446655440000",
            ),
            ("subject_type", "gro\x00up", "group"),
        ]

        for field_name, nul_value, expected_clean in cases:
            with self.subTest(field=field_name):
                data = self._make_valid_data(**{field_name: nul_value})
                serializer = UpdateRoleBindingRequestSerializer(data=data)
                self.assertTrue(serializer.is_valid(), f"NUL in {field_name}: {serializer.errors}")
                self.assertEqual(serializer.validated_data[field_name], expected_clean)


class UpdateRoleBindingResponseSerializerTests(IdentityRequest):
    """Tests for UpdateRoleBindingResponseSerializer field masking.

    Verifies that the response serializer respects field_selection context:
    - Default (no field_selection): subject has id+type, roles have id only,
      resource has id only.
    - With field_selection: only explicitly requested fields appear.
    """

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.role1 = RoleV2.objects.create(name="role_one", tenant=self.tenant)
        self.role2 = RoleV2.objects.create(name="role_two", tenant=self.tenant)

        self.group = Group.objects.create(
            name="test_group",
            description="A test group",
            tenant=self.tenant,
        )
        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            type=Principal.Types.USER,
        )
        self.group.principals.add(self.principal)

    def tearDown(self):
        """Tear down test data."""
        self.group.principals.clear()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def _make_group_result(self, roles=None):
        """Build an UpdateRoleBindingResult for a group subject."""
        return UpdateRoleBindingResult(
            subject_type=SubjectType.GROUP,
            roles=roles or [self.role1],
            resource_id="ws-123",
            resource_type="workspace",
            subject=self.group,
            resource_name="My Workspace",
        )

    def _make_user_result(self, roles=None):
        """Build an UpdateRoleBindingResult for a user subject."""
        return UpdateRoleBindingResult(
            subject_type=SubjectType.USER,
            roles=roles or [self.role1],
            resource_id="ws-123",
            resource_type="workspace",
            subject=self.principal,
            resource_name="My Workspace",
        )

    # ── Default behaviour (no field_selection) ───────────────────────

    def test_default_group_response(self):
        """Default response for a group subject includes id+type, role id only, resource id only."""
        result = self._make_group_result()
        serializer = UpdateRoleBindingResponseSerializer(result)
        data = serializer.data

        # subject: id + type only
        self.assertEqual(data["subject"], {"id": self.group.uuid, "type": "group"})
        # roles: id only
        self.assertEqual(data["roles"], [{"id": self.role1.uuid}])
        # resource: id only
        self.assertEqual(data["resource"], {"id": "ws-123"})
        # only subject, roles, resource keys
        self.assertEqual(set(data.keys()), {"subject", "roles", "resource"})

    def test_default_user_response(self):
        """Default response for a user subject includes id+type+user details."""
        result = self._make_user_result()
        serializer = UpdateRoleBindingResponseSerializer(result)
        data = serializer.data

        self.assertEqual(
            data["subject"],
            {"id": self.principal.uuid, "type": "user", "user": {"username": "testuser"}},
        )
        self.assertEqual(data["roles"], [{"id": self.role1.uuid}])
        self.assertEqual(data["resource"], {"id": "ws-123"})
        self.assertEqual(set(data.keys()), {"subject", "roles", "resource"})

    def test_default_multiple_roles_returns_id_only(self):
        """Default response with multiple roles returns id for each, no name."""
        result = self._make_group_result(roles=[self.role1, self.role2])
        serializer = UpdateRoleBindingResponseSerializer(result)
        data = serializer.data

        role_ids = {r["id"] for r in data["roles"]}
        self.assertEqual(role_ids, {self.role1.uuid, self.role2.uuid})
        for role in data["roles"]:
            self.assertNotIn("name", role)

    # ── With field_selection ─────────────────────────────────────────

    def test_field_selection_role_name(self):
        """Requesting roles(name) returns only name."""
        result = self._make_group_result()
        field_selection = RoleBindingBySubjectFieldSelection(nested_fields={"roles": {"name"}})
        serializer = UpdateRoleBindingResponseSerializer(result, context={"field_selection": field_selection})
        data = serializer.data

        self.assertEqual(data, {"roles": [{"name": "role_one"}]})

    def test_field_selection_resource_name_and_type(self):
        """Requesting resource(name,type) returns only those."""
        result = self._make_group_result()
        field_selection = RoleBindingBySubjectFieldSelection(nested_fields={"resource": {"name", "type"}})
        serializer = UpdateRoleBindingResponseSerializer(result, context={"field_selection": field_selection})
        data = serializer.data

        self.assertEqual(data, {"resource": {"name": "My Workspace", "type": "workspace"}})

    def test_field_selection_subject_id(self):
        """Requesting subject(id) returns id and always-included type discriminator."""
        result = self._make_group_result()
        field_selection = RoleBindingBySubjectFieldSelection(nested_fields={"subject": {"id"}})
        serializer = UpdateRoleBindingResponseSerializer(result, context={"field_selection": field_selection})
        data = serializer.data

        self.assertEqual(data, {"subject": {"id": self.group.uuid, "type": "group"}})

    def test_field_selection_subject_without_id(self):
        """When only subject(group.name) is requested, type plus group details appear."""
        result = self._make_group_result()
        field_selection = RoleBindingBySubjectFieldSelection(nested_fields={"subject": {"group.name"}})
        serializer = UpdateRoleBindingResponseSerializer(result, context={"field_selection": field_selection})
        data = serializer.data

        self.assertEqual(data, {"subject": {"type": "group", "group": {"name": "test_group"}}})

    def test_field_selection_group_details(self):
        """Requesting subject(group.name,group.description,group.user_count) returns only those."""
        self.group.principalCount = 3
        result = self._make_group_result()
        field_selection = RoleBindingBySubjectFieldSelection(
            nested_fields={"subject": {"group.name", "group.description", "group.user_count"}}
        )
        serializer = UpdateRoleBindingResponseSerializer(result, context={"field_selection": field_selection})
        data = serializer.data

        self.assertEqual(
            data,
            {
                "subject": {
                    "type": "group",
                    "group": {"name": "test_group", "description": "A test group", "user_count": 3},
                }
            },
        )

    def test_field_selection_user_details(self):
        """Requesting subject(id,user.username) returns only those."""
        result = self._make_user_result()
        field_selection = RoleBindingBySubjectFieldSelection(nested_fields={"subject": {"id", "user.username"}})
        serializer = UpdateRoleBindingResponseSerializer(result, context={"field_selection": field_selection})
        data = serializer.data

        self.assertEqual(
            data,
            {"subject": {"id": self.principal.uuid, "type": "user", "user": {"username": "testuser"}}},
        )

    def test_field_selection_resource_id_only(self):
        """Requesting resource(id) returns only id."""
        result = self._make_group_result()
        field_selection = RoleBindingBySubjectFieldSelection(nested_fields={"resource": {"id"}})
        serializer = UpdateRoleBindingResponseSerializer(result, context={"field_selection": field_selection})
        data = serializer.data

        self.assertEqual(data, {"resource": {"id": "ws-123"}})


class RoleBindingUserSubjectSerializerTest(IdentityRequest):
    """Test the RoleBindingOutputSerializer with user (Principal) subjects.

    Tests verify the serializer produces output matching the API spec for user subjects:
    - last_modified: datetime timestamp
    - subject: {id: UUID, type: "user", user: {username}}
    - roles: [{id: UUID, name: string}]
    - resource: {id, name, type}
    """

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.permission = Permission.objects.create(
            permission="app:resource:read",
            tenant=self.tenant,
        )

        self.role = RoleV2.objects.create(
            name="test_role",
            tenant=self.tenant,
        )
        self.role.permissions.add(self.permission)

        self.group = Group.objects.create(
            name="test_group",
            description="Test group description",
            tenant=self.tenant,
        )

        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            type=Principal.Types.USER,
        )
        self.group.principals.add(self.principal)

        self.binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        self.binding_group = RoleBindingGroup.objects.create(
            group=self.group,
            binding=self.binding,
        )

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        self.group.principals.clear()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    # get_subject tests for user (Principal)

    def test_subject_returns_correct_structure_for_user(self):
        """Test get_subject with Principal object returns correct default structure.

        Default behavior (no field_selection) returns only id and type.
        """
        serializer = RoleBindingOutputSerializer()
        result = serializer.get_subject(self.principal)

        # Default behavior: only id and type
        expected = {
            "id": self.principal.uuid,
            "type": "user",
        }
        self.assertEqual(result, expected)

    def test_subject_returns_user_type_for_principal(self):
        """Test that Principal objects return type='user'."""
        serializer = RoleBindingOutputSerializer()
        result = serializer.get_subject(self.principal)

        self.assertEqual(result["type"], "user")

    def test_subject_includes_username_when_requested(self):
        """Test that user.username is included when field selection requests it."""
        field_selection = RoleBindingFieldSelection(nested_fields={"subject": {"user.username"}})
        serializer = RoleBindingOutputSerializer(context={"field_selection": field_selection})
        result = serializer.get_subject(self.principal)

        self.assertEqual(result["type"], "user")
        self.assertIn("user", result)
        self.assertEqual(result["user"]["username"], "testuser")

    def test_subject_includes_id_when_explicitly_requested(self):
        """Test that id is included when field selection requests it."""
        field_selection = RoleBindingFieldSelection(nested_fields={"subject": {"id", "user.username"}})
        serializer = RoleBindingOutputSerializer(context={"field_selection": field_selection})
        result = serializer.get_subject(self.principal)

        self.assertEqual(result["type"], "user")
        self.assertEqual(result["id"], self.principal.uuid)
        self.assertIn("user", result)
        self.assertEqual(result["user"]["username"], "testuser")

    def test_subject_excludes_id_when_not_requested_with_field_selection(self):
        """Test that id is excluded when field selection doesn't include it."""
        field_selection = RoleBindingFieldSelection(nested_fields={"subject": {"user.username"}})
        serializer = RoleBindingOutputSerializer(context={"field_selection": field_selection})
        result = serializer.get_subject(self.principal)

        self.assertEqual(result["type"], "user")
        self.assertNotIn("id", result)

    def test_subject_returns_none_for_invalid_object(self):
        """Test get_subject with invalid object returns None."""
        serializer = RoleBindingOutputSerializer()
        result = serializer.get_subject({"name": "not a principal"})

        self.assertIsNone(result)

    # get_roles tests for user (Principal)

    def test_roles_extracts_roles_from_user_bindings(self):
        """Test get_roles with Principal having prefetched bindings via RoleBindingPrincipal."""
        # Set up prefetched data structure
        mock_binding = Mock()
        mock_binding.role = self.role

        # Mock RoleBindingPrincipal (directly on Principal)
        mock_binding_principal = Mock()
        mock_binding_principal.binding = mock_binding

        self.principal.filtered_bindings = [mock_binding_principal]

        serializer = RoleBindingOutputSerializer()
        result = serializer.get_roles(self.principal)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], self.role.uuid)

    def test_roles_includes_name_when_requested(self):
        """Test that role name is included when field selection requests it."""
        mock_binding = Mock()
        mock_binding.role = self.role

        # Mock RoleBindingPrincipal (directly on Principal)
        mock_binding_principal = Mock()
        mock_binding_principal.binding = mock_binding

        self.principal.filtered_bindings = [mock_binding_principal]

        field_selection = RoleBindingFieldSelection(nested_fields={"role": {"name"}})
        serializer = RoleBindingOutputSerializer(context={"field_selection": field_selection})
        result = serializer.get_roles(self.principal)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], self.role.uuid)
        self.assertEqual(result[0]["name"], "test_role")

    def test_roles_deduplicates_same_role_from_multiple_bindings(self):
        """Test get_roles deduplicates roles when same role appears in multiple bindings."""
        mock_binding = Mock()
        mock_binding.role = self.role

        # Mock two RoleBindingPrincipal entries pointing to the same role
        mock_binding_principal1 = Mock()
        mock_binding_principal1.binding = mock_binding

        mock_binding_principal2 = Mock()
        mock_binding_principal2.binding = mock_binding

        self.principal.filtered_bindings = [mock_binding_principal1, mock_binding_principal2]

        serializer = RoleBindingOutputSerializer()
        result = serializer.get_roles(self.principal)

        # Should deduplicate and return only one role
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], self.role.uuid)

    def test_roles_returns_empty_list_when_no_filtered_bindings(self):
        """Test get_roles with Principal without filtered_bindings attribute."""
        serializer = RoleBindingOutputSerializer()
        result = serializer.get_roles(self.principal)

        self.assertEqual(result, [])

    # Full serialization tests for user

    def test_full_serialization_with_user_and_context(self):
        """Test full serialization with Principal object and context.

        Default behavior returns only basic fields:
        - subject: id, type (no user details)
        - roles: id only (no name)
        - resource: id only (no name, type)
        - no last_modified
        """
        self.principal.latest_modified = datetime(2025, 1, 20, 14, 0, 0, tzinfo=timezone.utc)

        mock_binding = Mock()
        mock_binding.role = self.role

        # Mock RoleBindingPrincipal (directly on Principal, not through groups)
        mock_binding_principal = Mock()
        mock_binding_principal.binding = mock_binding

        self.principal.filtered_bindings = [mock_binding_principal]

        context = {
            "request": Mock(),
            "resource_id": "ws-12345",
            "resource_name": "Test Workspace",
            "resource_type": "workspace",
        }

        serializer = RoleBindingOutputSerializer(self.principal, context=context)
        data = serializer.data

        # Default behavior: only basic fields
        self.assertNotIn("last_modified", data)
        self.assertEqual(data["subject"]["id"], self.principal.uuid)
        self.assertEqual(data["subject"]["type"], "user")
        self.assertNotIn("user", data["subject"])
        self.assertEqual(len(data["roles"]), 1)
        self.assertEqual(data["roles"][0]["id"], self.role.uuid)
        self.assertNotIn("name", data["roles"][0])
        self.assertEqual(data["resource"], {"id": "ws-12345"})

    def test_full_serialization_with_user_field_selection(self):
        """Test full serialization with field selection for user details."""
        self.principal.latest_modified = datetime(2025, 1, 20, 14, 0, 0, tzinfo=timezone.utc)

        mock_binding = Mock()
        mock_binding.role = self.role

        # Mock RoleBindingPrincipal (directly on Principal, not through groups)
        mock_binding_principal = Mock()
        mock_binding_principal.binding = mock_binding

        self.principal.filtered_bindings = [mock_binding_principal]

        field_selection = RoleBindingFieldSelection(
            root_fields={"last_modified"},
            nested_fields={
                "subject": {"id", "user.username"},
                "role": {"name"},
                "resource": {"name", "type"},
            },
        )

        context = {
            "request": Mock(),
            "resource_id": "ws-12345",
            "resource_name": "Test Workspace",
            "resource_type": "workspace",
            "field_selection": field_selection,
        }

        serializer = RoleBindingOutputSerializer(self.principal, context=context)
        data = serializer.data

        # Check subject with user details
        self.assertEqual(data["subject"]["id"], self.principal.uuid)
        self.assertEqual(data["subject"]["type"], "user")
        self.assertEqual(data["subject"]["user"]["username"], "testuser")

        # Check roles with name
        self.assertEqual(data["roles"][0]["id"], self.role.uuid)
        self.assertEqual(data["roles"][0]["name"], "test_role")

        # Check resource with name and type
        self.assertEqual(data["resource"]["id"], "ws-12345")
        self.assertEqual(data["resource"]["name"], "Test Workspace")
        self.assertEqual(data["resource"]["type"], "workspace")

        # Check last_modified
        self.assertIn("last_modified", data)
