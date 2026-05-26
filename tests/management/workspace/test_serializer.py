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
import uuid

from django.test import TestCase
from unittest.mock import Mock
from api.models import Tenant
from management.models import Workspace
from rest_framework import serializers
from management.workspace.serializer import (
    WorkspaceAncestrySerializer,
    WorkspaceListInputSerializer,
    WorkspaceSerializer,
    WorkspaceWithAncestrySerializer,
)


class WorkspaceSerializerTest(TestCase):
    """Test the workspace serializer"""

    def setUp(self):
        """Set up workspace serializer tests."""
        tenant = Tenant.objects.get(tenant_name="public")
        self.parent = Workspace.objects.create(
            name="Parent",
            description="Parent desc",
            tenant=tenant,
            type=Workspace.Types.ROOT,
        )
        self.child = Workspace.objects.create(
            name="Child", description="Child desc", tenant=tenant, parent=self.parent
        )

    def tearDown(self):
        """Tear down workspace serializer tests."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    def _format_timestamps(self, timestamp):
        return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def test_get_workspace_detail_child(self):
        """Return GET /workspace/<id>/ serializer response for child"""
        serializer = WorkspaceSerializer(self.child)
        expected_data = {
            "id": str(self.child.id),
            "name": self.child.name,
            "description": self.child.description,
            "parent_id": str(self.parent.id),
            "created": self._format_timestamps(self.child.created),
            "modified": self._format_timestamps(self.child.modified),
            "type": self.child.type,
        }

        self.assertDictEqual(serializer.data, expected_data)

    def test_get_workspace_detail_parent(self):
        """Return GET /workspace/<id>/ serializer response for parent"""
        serializer = WorkspaceSerializer(self.parent)
        expected_data = {
            "id": str(self.parent.id),
            "name": self.parent.name,
            "description": self.parent.description,
            "parent_id": None,
            "created": self._format_timestamps(self.parent.created),
            "modified": self._format_timestamps(self.parent.modified),
            "type": self.parent.type,
        }

        self.assertDictEqual(serializer.data, expected_data)

    def test_get_workspace_detail_with_ancestry(self):
        """Test workspace serializer with ancestry"""
        serializer = WorkspaceWithAncestrySerializer(self.child)
        expected_data = {
            "id": str(self.child.id),
            "name": self.child.name,
            "description": self.child.description,
            "parent_id": str(self.parent.id),
            "created": self._format_timestamps(self.child.created),
            "modified": self._format_timestamps(self.child.modified),
            "ancestry": [{"name": self.parent.name, "id": str(self.parent.id), "parent_id": None}],
            "created": self._format_timestamps(self.child.created),
            "modified": self._format_timestamps(self.child.modified),
            "type": self.child.type,
        }

        self.assertDictEqual(serializer.data, expected_data)

    def test_workspace_ancestry(self):
        """Test workspace ancestry serializer"""
        serializer = WorkspaceAncestrySerializer(self.child)
        expected_data = {"name": self.child.name, "parent_id": str(self.parent.id), "id": str(self.child.id)}

        self.assertDictEqual(serializer.data, expected_data)

    def test_validate_name_rejects_special_characters(self):
        """Test that names with disallowed characters are rejected."""
        serializer = WorkspaceSerializer()
        for invalid_name in ["ws@name", "ws#name", "ws!name", "ws.name", "ws/name"]:
            with self.assertRaises(serializers.ValidationError, msg=f"Expected error for '{invalid_name}'"):
                serializer.validate_name(invalid_name)

    def test_validate_name_accepts_valid_characters(self):
        """Test that names with allowed characters are accepted."""
        serializer = WorkspaceSerializer()
        for valid_name in ["My Workspace", "ws-name", "ws_name", "Workspace 123", "simple"]:
            result = serializer.validate_name(valid_name)
            self.assertEqual(result, valid_name)

    def test_validate_name_allows_unchanged_legacy_name(self):
        """Test that an unchanged legacy name (with special chars) passes validation."""
        serializer = WorkspaceSerializer(instance=self.child)
        self.child.name = "legacy@name"
        result = serializer.validate_name("legacy@name")
        self.assertEqual(result, "legacy@name")


class WorkspaceListInputSerializerTest(TestCase):
    """Test the WorkspaceListInputSerializer.

    Validates query parameter parsing and normalization for
    GET /v2/workspaces/.
    """

    # --- type ---

    def test_type_empty_returns_none(self):
        """Test that empty type is treated as unset (returns all)."""
        s = WorkspaceListInputSerializer(data={"type": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("type"))

    def test_type_omitted_returns_none(self):
        """Test that omitting type is valid."""
        s = WorkspaceListInputSerializer(data={})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("type"))

    def test_type_valid_single_value(self):
        """Test that a valid single type value is accepted."""
        s = WorkspaceListInputSerializer(data={"type": "standard"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["type"], ["standard"])

    def test_type_valid_comma_separated(self):
        """Test that comma-separated type values are split and validated."""
        s = WorkspaceListInputSerializer(data={"type": "standard,ungrouped-hosts"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["type"], ["standard", "ungrouped-hosts"])

    def test_type_case_insensitive(self):
        """Test that type values are lowercased."""
        s = WorkspaceListInputSerializer(data={"type": "STANDARD"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["type"], ["standard"])

    def test_type_all_returns_none(self):
        """Test that type=all means unfiltered (None)."""
        s = WorkspaceListInputSerializer(data={"type": "all"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("type"))

    def test_type_all_in_comma_list_returns_none(self):
        """Test that 'all' in a comma-separated list collapses to unfiltered."""
        s = WorkspaceListInputSerializer(data={"type": "standard,all"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("type"))

    def test_type_invalid_value(self):
        """Test that an invalid type value is rejected."""
        s = WorkspaceListInputSerializer(data={"type": "invalid"})
        self.assertFalse(s.is_valid())
        self.assertIn("type", s.errors)
        message = str(s.errors["type"])
        self.assertIn("invalid", message)
        self.assertIn("allowed", message.lower())

    def test_type_whitespace_only_returns_none(self):
        """Test that whitespace-only type is treated as unset."""
        s = WorkspaceListInputSerializer(data={"type": "   "})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("type"))

    # --- name ---

    def test_name_empty_returns_none(self):
        """Test that empty name is treated as unset."""
        s = WorkspaceListInputSerializer(data={"name": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("name"))

    def test_name_whitespace_returns_none(self):
        """Test that whitespace-only name is treated as unset."""
        s = WorkspaceListInputSerializer(data={"name": "   "})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("name"))

    def test_name_valid_passes_through(self):
        """Test that a valid name passes through unchanged."""
        s = WorkspaceListInputSerializer(data={"name": "test workspace"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["name"], "test workspace")

    # --- parent_id ---

    def test_parent_id_empty_returns_none(self):
        """Test that empty parent_id is treated as unset."""
        s = WorkspaceListInputSerializer(data={"parent_id": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("parent_id"))

    def test_parent_id_whitespace_returns_none(self):
        """Test that whitespace-only parent_id is treated as unset."""
        s = WorkspaceListInputSerializer(data={"parent_id": "   "})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("parent_id"))

    def test_parent_id_valid_uuid(self):
        """Test that a valid UUID parent_id passes validation."""
        test_uuid = str(uuid.uuid4())
        s = WorkspaceListInputSerializer(data={"parent_id": test_uuid})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["parent_id"], test_uuid)

    def test_parent_id_invalid_uuid(self):
        """Test that an invalid UUID parent_id is rejected."""
        s = WorkspaceListInputSerializer(data={"parent_id": "not-a-uuid"})
        self.assertFalse(s.is_valid())
        self.assertIn("parent_id", s.errors)

    # --- ids ---

    def test_ids_empty_returns_none(self):
        """Test that empty ids is treated as unset."""
        s = WorkspaceListInputSerializer(data={"ids": ""})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("ids"))

    def test_ids_whitespace_returns_none(self):
        """Test that whitespace-only ids is treated as unset."""
        s = WorkspaceListInputSerializer(data={"ids": "   "})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("ids"))

    def test_ids_single_valid_uuid(self):
        """Test that a single valid UUID is accepted."""
        test_uuid = str(uuid.uuid4())
        s = WorkspaceListInputSerializer(data={"ids": test_uuid})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["ids"], [test_uuid])

    def test_ids_comma_separated_valid(self):
        """Test that comma-separated UUIDs are accepted and deduplicated."""
        u1 = str(uuid.uuid4())
        u2 = str(uuid.uuid4())
        s = WorkspaceListInputSerializer(data={"ids": f"{u1},{u2},{u1}"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(len(s.validated_data["ids"]), 2)

    def test_ids_invalid_uuid(self):
        """Test that invalid UUIDs in ids are rejected."""
        s = WorkspaceListInputSerializer(data={"ids": "not-a-uuid"})
        self.assertFalse(s.is_valid())
        self.assertIn("ids", s.errors)

    # --- cross-field: ids + type ---

    def test_ids_without_type_defaults_to_standard(self):
        """Test that providing ids without type defaults type to standard."""
        test_uuid = str(uuid.uuid4())
        s = WorkspaceListInputSerializer(data={"ids": test_uuid})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["type"], [Workspace.Types.STANDARD])

    def test_ids_with_explicit_type_keeps_type(self):
        """Test that providing ids with explicit type preserves the type."""
        test_uuid = str(uuid.uuid4())
        s = WorkspaceListInputSerializer(data={"ids": test_uuid, "type": "root"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["type"], ["root"])

    def test_ids_with_type_all_keeps_none(self):
        """Test that providing ids with type=all returns unfiltered."""
        test_uuid = str(uuid.uuid4())
        s = WorkspaceListInputSerializer(data={"ids": test_uuid, "type": "all"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("type"))

    # --- NUL byte rejection ---

    def test_nul_byte_in_type_returns_400(self):
        """Test that NUL bytes in type are rejected."""
        s = WorkspaceListInputSerializer(data={"type": "standard\x00evil"})
        self.assertFalse(s.is_valid())
        self.assertIn("type", s.errors)

    def test_nul_byte_in_name_returns_400(self):
        """Test that NUL bytes in name are rejected."""
        s = WorkspaceListInputSerializer(data={"name": "test\x00evil"})
        self.assertFalse(s.is_valid())
        self.assertIn("name", s.errors)

    def test_nul_byte_in_parent_id_returns_400(self):
        """Test that NUL bytes in parent_id are rejected."""
        s = WorkspaceListInputSerializer(data={"parent_id": "abc\x00def"})
        self.assertFalse(s.is_valid())
        self.assertIn("parent_id", s.errors)

    def test_nul_byte_in_ids_returns_400(self):
        """Test that NUL bytes in ids are rejected."""
        s = WorkspaceListInputSerializer(data={"ids": "abc\x00def"})
        self.assertFalse(s.is_valid())
        self.assertIn("ids", s.errors)
