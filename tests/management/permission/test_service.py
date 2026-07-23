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
"""Tests for PermissionService."""

from django.test import override_settings
from management.permission.exceptions import InvalidPermissionDataError
from management.permission.model import Permission
from management.permission.service import PermissionService
from tests.identity_request import IdentityRequest


@override_settings(ATOMIC_RETRY_DISABLED=True)
class PermissionServiceTests(IdentityRequest):
    """Tests for PermissionService."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.service = PermissionService()

        self.permission1 = Permission.objects.create(
            permission="inventory:hosts:read",
            tenant=self.tenant,
        )
        self.permission2 = Permission.objects.create(
            permission="inventory:hosts:write",
            tenant=self.tenant,
        )

    def test_resolve_single_permission(self):
        """Test resolving a single permission."""
        permission_data = [
            {"application": "inventory", "resource_type": "hosts", "operation": "read"},
        ]

        result = self.service.resolve(permission_data)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], self.permission1)

    def test_resolve_multiple_permissions(self):
        """Test resolving multiple permissions."""
        permission_data = [
            {"application": "inventory", "resource_type": "hosts", "operation": "read"},
            {"application": "inventory", "resource_type": "hosts", "operation": "write"},
        ]

        result = self.service.resolve(permission_data)

        self.assertEqual(len(result), 2)
        self.assertIn(self.permission1, result)
        self.assertIn(self.permission2, result)

    def test_resolve_with_verb_key(self):
        """Test that 'verb' key works as alternative to 'operation'."""
        permission_data = [
            {"application": "inventory", "resource_type": "hosts", "verb": "read"},
        ]

        result = self.service.resolve(permission_data)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], self.permission1)

    def test_resolve_both_operation_and_verb_raises_error(self):
        permission_data = [
            {"application": "inventory", "resource_type": "hosts", "operation": "read", "verb": "write"},
        ]

        with self.assertRaises(InvalidPermissionDataError) as context:
            self.service.resolve(permission_data)

        self.assertIn("Cannot specify both", str(context.exception))

    def test_resolve_empty_list_returns_empty(self):
        """Test that empty permission list returns empty list."""
        result = self.service.resolve([])

        self.assertEqual(result, [])

    def test_resolve_not_found_returns_empty(self):
        """Test that non-existent permissions return empty list."""
        permission_data = [
            {"application": "nonexistent", "resource_type": "foo", "operation": "bar"},
        ]

        result = self.service.resolve(permission_data)

        self.assertEqual(result, [])

    def test_resolve_partial_returns_only_found(self):
        """Test that only found permissions are returned."""
        permission_data = [
            {"application": "inventory", "resource_type": "hosts", "operation": "read"},
            {"application": "nonexistent", "resource_type": "foo", "operation": "bar"},
        ]

        result = self.service.resolve(permission_data)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], self.permission1)

    def test_resolve_returns_all_found(self):
        """Test that all found permissions are returned."""
        permission_data = [
            {"application": "inventory", "resource_type": "hosts", "operation": "write"},
            {"application": "inventory", "resource_type": "hosts", "operation": "read"},
        ]

        result = self.service.resolve(permission_data)

        self.assertEqual(len(result), 2)
        self.assertCountEqual(result, [self.permission1, self.permission2])
