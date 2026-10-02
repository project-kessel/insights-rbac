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
"""Test role relations utilities."""

from django.test import TestCase
from management.inventory_replicator.logging_replicator import stringify_spicedb_relationship
from management.role.relations import deduplicate_role_permission_relationships
from migration_tool.utils import create_relationship


class DeduplicateRolePermissionRelationshipsTest(TestCase):
    """Test the deduplicate_role_permission_relationships function."""

    def test_deduplicates_role_to_principal_permission_tuples(self):
        """
        Test deduplication behavior for different relationship types.

        Verifies that:
        - Duplicate role-to-principal permission tuples ARE deduplicated (expected)
        - Duplicate non-permission tuples are NOT deduplicated (they indicate bugs)
        """
        role_id = "role-123"

        # Create duplicate role-to-principal permission tuples (SHOULD be deduplicated)
        perm1 = create_relationship(("rbac", "role"), role_id, ("rbac", "principal"), "*", "inventory_groups_read")
        perm2 = create_relationship(("rbac", "role"), role_id, ("rbac", "principal"), "*", "inventory_groups_read")
        perm3 = create_relationship(("rbac", "role"), role_id, ("rbac", "principal"), "*", "inventory_hosts_write")
        perm4 = create_relationship(("rbac", "role"), role_id, ("rbac", "principal"), "*", "inventory_hosts_write")

        # Add unique non-permission tuples
        binding1 = create_relationship(("rbac", "role_binding"), "binding-1", ("rbac", "role"), role_id, "role")
        binding2 = create_relationship(("rbac", "role_binding"), "binding-2", ("rbac", "role"), role_id, "role")

        # Add duplicate non-permission tuple (should NOT be deduplicated - this is a bug!)
        binding_dup1 = create_relationship(("rbac", "role_binding"), "binding-dup", ("rbac", "role"), role_id, "role")
        binding_dup2 = create_relationship(("rbac", "role_binding"), "binding-dup", ("rbac", "role"), role_id, "role")

        relationships = [perm1, perm2, perm3, perm4, binding1, binding2, binding_dup1, binding_dup2]

        # Call deduplication
        deduplicated = deduplicate_role_permission_relationships(relationships)

        # Should have 6 relationships:
        # - 2 unique permission tuples (duplicates removed)
        # - 4 binding tuples (including the 2 duplicate bindings which are NOT removed)
        self.assertEqual(len(deduplicated), 6)

        # Verify the actual content - check each specific relationship is present
        deduplicated_strings = [stringify_spicedb_relationship(rel) for rel in deduplicated]

        # Expected relationships
        expected = [
            "role:role-123#inventory_groups_read@principal:*",  # perm duplicate removed
            "role:role-123#inventory_hosts_write@principal:*",  # perm duplicate removed
            "role_binding:binding-1#role@role:role-123",
            "role_binding:binding-2#role@role:role-123",
            "role_binding:binding-dup#role@role:role-123",  # First duplicate
            "role_binding:binding-dup#role@role:role-123",  # Second duplicate (NOT removed!)
        ]

        for expected_rel in expected:
            self.assertIn(
                expected_rel,
                deduplicated_strings,
                f"Expected relationship {expected_rel} should be in deduplicated list",
            )

        # Verify exactly these relationships (no extras)
        self.assertCountEqual(deduplicated_strings, expected)

    def test_handles_empty_list(self):
        """Test that empty list is handled correctly."""
        relationships = []
        deduplicated = deduplicate_role_permission_relationships(relationships)
        self.assertEqual(len(deduplicated), 0)
