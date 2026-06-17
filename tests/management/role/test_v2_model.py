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
"""Test the RoleV2 models."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase
from rest_framework import serializers

from api.models import Tenant
from management.models import (
    CustomRoleV2,
    Group,
    Permission,
    PlatformRoleV2,
    Role,
    RoleV2,
    SeededRoleV2,
)
from management.principal.model import Principal
from management.relation_replicator.types import ObjectReference, ObjectType, RelationTuple, SubjectReference
from management.role.relations import role_owner_relationship
from management.role_binding.model import RoleBindingPrincipal
from migration_tool.models import role_permission_tuple
from tests.identity_request import IdentityRequest
from tests.v2_util import seed_v2_role_from_v1


class RoleV2ModelTests(IdentityRequest):
    """Test the RoleV2 models."""

    def setUp(self):
        """Set up the RoleV2 model tests."""
        super().setUp()

        self.permission1 = Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        self.permission2 = Permission.objects.create(permission="app:resource:write", tenant=self.tenant)

    def tearDown(self):
        """Tear down RoleV2 model tests."""
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()

    def test_rolev2_creation(self):
        """Test basic RoleV2 creation."""
        role = RoleV2.objects.create(
            name="test_role",
            description="Test role description",
            tenant=self.tenant,
        )
        self.assertEqual(role.name, "test_role")
        self.assertEqual(role.description, "Test role description")
        self.assertEqual(role.type, RoleV2.Types.CUSTOM)  # Default type is CUSTOM
        self.assertEqual(role.tenant, self.tenant)
        self.assertTrue(role.uuid)  # UUID exists and is auto-generated

    def test_rolev2_unique_name_per_tenant(self):
        """Test unique name per tenant."""
        RoleV2.objects.create(name="unique_test_role_1", tenant=self.tenant)
        with self.assertRaises(ValidationError):
            RoleV2.objects.create(name="unique_test_role_1", tenant=self.tenant)

    def test_role2_creation_with_wrong_type(self):
        """Test RoleV2 creation with wrong type."""
        with self.assertRaises(ValidationError) as cm:
            RoleV2.objects.create(name="test_role", type="wrong_type", tenant=self.tenant)
        self.assertIn("Value 'wrong_type' is not a valid choice.", str(cm.exception))

    def test_custom_role_creation(self):
        """Test V2 Custom Role creation."""
        role = CustomRoleV2.objects.create(name="custom_role", tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.CUSTOM)
        self.assertIsInstance(role, CustomRoleV2)
        self.assertIsInstance(role, RoleV2)

    def test_custom_role_creation_with_type(self):
        """Test V2 Custom Role creation with type."""
        role = CustomRoleV2.objects.create(name="custom_role", type=RoleV2.Types.CUSTOM, tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.CUSTOM)
        self.assertIsInstance(role, CustomRoleV2)
        self.assertIsInstance(role, RoleV2)

    def test_custom_role_creation_from_base_role(self):
        """Test V2 Custom Role creation from base role."""
        role = RoleV2.objects.create(name="custom_role", type=RoleV2.Types.CUSTOM, tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.CUSTOM)
        self.assertIsInstance(role, RoleV2)

        custom_role = CustomRoleV2.objects.get(id=role.id)
        self.assertEqual(custom_role.type, RoleV2.Types.CUSTOM)
        self.assertIsInstance(custom_role, CustomRoleV2)
        self.assertIsInstance(custom_role, RoleV2)

    def test_custom_role_creation_with_wrong_type(self):
        """Test V2 Custom Role creation with wrong type causes the exception."""
        with self.assertRaises(serializers.ValidationError) as cm:
            CustomRoleV2.objects.create(name="custom_role", type=RoleV2.Types.PLATFORM, tenant=self.tenant)
        self.assertIn("Expected role to have type custom, but found platform", str(cm.exception))

    def test_custom_role_type_validation_on_init(self):
        """Test type validation when creating V2 Custom Role with wrong type."""
        # Create base role with type = PLATFORM
        base_role = RoleV2.objects.create(name="platform_role", type=RoleV2.Types.PLATFORM, tenant=self.tenant)

        # Try to wrap as CustomRoleV2 - should fail
        with self.assertRaises(serializers.ValidationError) as cm:
            CustomRoleV2(id=base_role.id, name=base_role.name, type=base_role.type, tenant=self.tenant)
        self.assertIn("Expected role to have type custom, but found platform", str(cm.exception))

        # No custom roles exist with this id
        with self.assertRaises(CustomRoleV2.DoesNotExist):
            CustomRoleV2.objects.get(id=base_role.id)

        # Role exists as platform role
        platform_role = PlatformRoleV2.objects.get(id=base_role.id)
        self.assertEqual(platform_role.type, RoleV2.Types.PLATFORM)
        self.assertIsInstance(platform_role, PlatformRoleV2)
        self.assertIsInstance(platform_role, RoleV2)

    def test_custom_role_type_validation_on_save(self):
        """Test type validation when saving V2 Custom Role with wrong type."""
        role = CustomRoleV2(name="test_role", tenant=self.tenant)
        role.type = RoleV2.Types.PLATFORM  # Wrong type

        with self.assertRaises(serializers.ValidationError) as cm:
            role.save()
        self.assertIn("Expected role to have type custom, but found platform", str(cm.exception))

    def test_custom_role_cannot_have_children(self):
        """Test that V2 Custom Role cannot have children."""
        parent = CustomRoleV2.objects.create(name="parent", tenant=self.tenant)
        child = CustomRoleV2.objects.create(name="child", tenant=self.tenant)

        with transaction.atomic():
            with self.assertRaises(serializers.ValidationError) as cm:
                parent.children.add(child)
            self.assertIn("Custom roles cannot have children", str(cm.exception))

    def test_seeded_role_creation(self):
        """Test V2 Seeded Role creation."""
        role = SeededRoleV2.objects.create(name="seeded_role", tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.SEEDED)
        self.assertIsInstance(role, SeededRoleV2)
        self.assertIsInstance(role, RoleV2)

    def test_seeded_role_creation_with_type(self):
        """Test V2 Seeded Role creation with type."""
        role = SeededRoleV2.objects.create(name="seeded_role", type=RoleV2.Types.SEEDED, tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.SEEDED)
        self.assertIsInstance(role, SeededRoleV2)
        self.assertIsInstance(role, RoleV2)

    def test_seeded_role_creation_from_base_role(self):
        """Test V2 Seeded Role creation from base role."""
        role = RoleV2.objects.create(name="seeded_role", type=RoleV2.Types.SEEDED, tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.SEEDED)
        self.assertIsInstance(role, RoleV2)

        seeded_role = SeededRoleV2.objects.get(id=role.id)
        self.assertEqual(seeded_role.type, RoleV2.Types.SEEDED)
        self.assertIsInstance(seeded_role, SeededRoleV2)
        self.assertIsInstance(seeded_role, RoleV2)

    def test_seeded_role_creation_with_wrong_type(self):
        """Test V2 Seeded Role creation with wrong type causes the exception."""
        with self.assertRaises(serializers.ValidationError) as cm:
            SeededRoleV2.objects.create(name="seeded_role", type=RoleV2.Types.CUSTOM, tenant=self.tenant)
        self.assertIn("Expected role to have type seeded, but found custom", str(cm.exception))

    def test_seeded_role_type_validation_on_init(self):
        """Test type validation when creating V2 Seeded Role with wrong type."""
        # Create base role with wrong type
        base_role = RoleV2.objects.create(name="custom_role", type=RoleV2.Types.CUSTOM, tenant=self.tenant)

        # Try to wrap as SeededRoleV2 - should fail
        with self.assertRaises(serializers.ValidationError) as cm:
            SeededRoleV2(id=base_role.id, name=base_role.name, type=base_role.type, tenant=self.tenant)
        self.assertIn("Expected role to have type seeded, but found custom", str(cm.exception))

        # No seeded roles exist with this id
        with self.assertRaises(SeededRoleV2.DoesNotExist):
            SeededRoleV2.objects.get(id=base_role.id)

        # Role exists as custom role
        custom_role = CustomRoleV2.objects.get(id=base_role.id)
        self.assertEqual(custom_role.type, RoleV2.Types.CUSTOM)
        self.assertIsInstance(custom_role, CustomRoleV2)
        self.assertIsInstance(custom_role, RoleV2)

    def test_seeded_role_type_validation_on_save(self):
        """Test type validation when saving V2 Seeded Role with wrong type."""
        role = SeededRoleV2(name="test_role", tenant=self.tenant)
        role.type = RoleV2.Types.PLATFORM  # Wrong type

        with self.assertRaises(serializers.ValidationError) as cm:
            role.save()
        self.assertIn("Expected role to have type seeded", str(cm.exception))

    def test_seeded_role_cannot_have_children(self):
        """Test that V2 Seeded Role cannot have children."""
        parent = SeededRoleV2.objects.create(name="parent", tenant=self.tenant)
        child = SeededRoleV2.objects.create(name="child", tenant=self.tenant)

        with transaction.atomic():
            with self.assertRaises(serializers.ValidationError) as cm:
                parent.children.add(child)
            self.assertIn("Seeded roles cannot have children", str(cm.exception))

    def test_platform_role_creation(self):
        """Test V2 Platform Role creation."""
        role = PlatformRoleV2.objects.create(name="platform_role", tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.PLATFORM)
        self.assertIsInstance(role, PlatformRoleV2)
        self.assertIsInstance(role, RoleV2)

    def test_platform_role_creation_with_type(self):
        """Test V2 Platform Role creation with type."""
        role = PlatformRoleV2.objects.create(name="platform_role", type=RoleV2.Types.PLATFORM, tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.PLATFORM)
        self.assertIsInstance(role, PlatformRoleV2)
        self.assertIsInstance(role, RoleV2)

    def test_platform_role_creation_from_base_role(self):
        """Test V2 Platform Role creation from base role."""
        role = RoleV2.objects.create(name="platform_role", type=RoleV2.Types.PLATFORM, tenant=self.tenant)
        self.assertEqual(role.type, RoleV2.Types.PLATFORM)
        self.assertIsInstance(role, RoleV2)

        platform_role = PlatformRoleV2.objects.get(id=role.id)
        self.assertEqual(platform_role.type, RoleV2.Types.PLATFORM)
        self.assertIsInstance(platform_role, PlatformRoleV2)
        self.assertIsInstance(platform_role, RoleV2)

    def test_platform_role_creation_with_wrong_type(self):
        """Test V2 Platform Role creation with wrong type causes the exception."""
        with self.assertRaises(serializers.ValidationError) as cm:
            PlatformRoleV2.objects.create(name="platform_role", type=RoleV2.Types.SEEDED, tenant=self.tenant)
        self.assertIn("Expected role to have type platform, but found seeded", str(cm.exception))

    def test_platform_role_type_validation_on_init(self):
        """Test type validation when creating V2 Platform Role with wrong type."""
        # Create base role with wrong type
        base_role = RoleV2.objects.create(name="seeded_role", type=RoleV2.Types.SEEDED, tenant=self.tenant)

        # Try to wrap as PlatformRoleV2 - should fail
        with self.assertRaises(serializers.ValidationError) as cm:
            PlatformRoleV2(id=base_role.id, name=base_role.name, type=base_role.type, tenant=self.tenant)
        self.assertIn("Expected role to have type platform, but found seeded", str(cm.exception))

        # No platform roles exist with this id
        with self.assertRaises(PlatformRoleV2.DoesNotExist):
            PlatformRoleV2.objects.get(id=base_role.id)

        # Role exists as seeded role
        seeded_role = SeededRoleV2.objects.get(id=base_role.id)
        self.assertEqual(seeded_role.type, RoleV2.Types.SEEDED)
        self.assertIsInstance(seeded_role, SeededRoleV2)
        self.assertIsInstance(seeded_role, RoleV2)

    def test_platform_role_type_validation_on_save(self):
        """Test type validation when saving V2 Platform Role with wrong type."""
        role = PlatformRoleV2(name="test_role", tenant=self.tenant)
        role.type = RoleV2.Types.CUSTOM  # Wrong type

        with self.assertRaises(serializers.ValidationError) as cm:
            role.save()
        self.assertIn("Expected role to have type platform", str(cm.exception))

    def test_platform_role_can_have_seeded_children(self):
        """Test that V2 Platform Role can have seeded children."""
        parent = PlatformRoleV2.objects.create(name="parent", tenant=self.tenant)
        child = SeededRoleV2.objects.create(name="child", tenant=self.tenant)
        # No exception should be raised
        try:
            parent.children.add(child)
        except serializers.ValidationError:
            self.fail("V2 Platform Role should be able to have seeded children")

        children = parent.children.all()
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].id, child.id)

    def test_platform_role_cannot_have_custom_children(self):
        """Test that V2 Platform Role cannot have custom children."""
        parent = PlatformRoleV2.objects.create(name="parent", tenant=self.tenant)
        child = CustomRoleV2.objects.create(name="child", tenant=self.tenant)

        with transaction.atomic():
            with self.assertRaises(serializers.ValidationError) as cm:
                parent.children.add(child)
            self.assertIn("Platform roles can only have seeded roles as children", str(cm.exception))

    def test_platform_role_cannot_have_platform_children(self):
        """Test that V2 Platform Role cannot have platform children."""
        parent = PlatformRoleV2.objects.create(name="parent", tenant=self.tenant)
        child = PlatformRoleV2.objects.create(name="child", tenant=self.tenant)

        with transaction.atomic():
            with self.assertRaises(serializers.ValidationError) as cm:
                parent.children.add(child)
            self.assertIn("Platform roles can only have seeded roles as children", str(cm.exception))

    def test_platform_role_mixed_children_validation(self):
        """Test V2 Platform Role validation with mixed valid/invalid children."""
        parent = PlatformRoleV2.objects.create(name="parent", tenant=self.tenant)
        valid_child = SeededRoleV2.objects.create(name="seeded_child", tenant=self.tenant)
        invalid_child = CustomRoleV2.objects.create(name="custom_child", tenant=self.tenant)

        with transaction.atomic():
            with self.assertRaises(serializers.ValidationError) as cm:
                parent.children.add(invalid_child)
            self.assertIn("Platform roles can only have seeded roles as children", str(cm.exception))

    def test_custom_role_add_permissions(self):
        """Test adding permissions to a custom role."""
        role = CustomRoleV2.objects.create(name="test_role", tenant=self.tenant)

        role.permissions.add(self.permission1, self.permission2)

        self.assertEqual(role.permissions.count(), 2)
        self.assertIn(self.permission1, role.permissions.all())
        self.assertIn(self.permission2, role.permissions.all())

    def test_custom_role_remove_permissions(self):
        """Test removing permissions from a custom role."""
        role = CustomRoleV2.objects.create(name="test_role", tenant=self.tenant)
        role.permissions.add(self.permission1, self.permission2)
        role.permissions.remove(self.permission1)
        self.assertEqual(role.permissions.count(), 1)
        self.assertNotIn(self.permission1, role.permissions.all())
        self.assertIn(self.permission2, role.permissions.all())

    def test_platform_role_add_permissions(self):
        """Test adding permissions to a platform role."""
        role = PlatformRoleV2.objects.create(name="test_role", tenant=self.tenant)

        role.permissions.add(self.permission1, self.permission2)

        self.assertEqual(role.permissions.count(), 2)
        self.assertIn(self.permission1, role.permissions.all())
        self.assertIn(self.permission2, role.permissions.all())

    def test_platform_role_remove_permissions(self):
        """Test removing permissions from a platform role."""
        role = PlatformRoleV2.objects.create(name="test_role", tenant=self.tenant)
        role.permissions.add(self.permission1, self.permission2)
        role.permissions.remove(self.permission1)
        self.assertEqual(role.permissions.count(), 1)
        self.assertNotIn(self.permission1, role.permissions.all())
        self.assertIn(self.permission2, role.permissions.all())

    def test_seeded_role_add_permissions(self):
        """Test adding permissions to a seeded role."""
        role = SeededRoleV2.objects.create(name="test_role", tenant=self.tenant)

        role.permissions.add(self.permission1, self.permission2)

        self.assertEqual(role.permissions.count(), 2)
        self.assertIn(self.permission1, role.permissions.all())
        self.assertIn(self.permission2, role.permissions.all())

    def test_seeded_role_remove_permissions(self):
        """Test removing permissions from a seeded role."""
        role = SeededRoleV2.objects.create(name="test_role", tenant=self.tenant)
        role.permissions.add(self.permission1, self.permission2)
        role.permissions.remove(self.permission1)
        self.assertEqual(role.permissions.count(), 1)
        self.assertNotIn(self.permission1, role.permissions.all())
        self.assertIn(self.permission2, role.permissions.all())

    def test_role_hierarchy(self):
        """Test role parent-child relationships."""
        parent = PlatformRoleV2.objects.create(name="parent", tenant=self.tenant)
        child1 = SeededRoleV2.objects.create(name="child1", tenant=self.tenant)
        child2 = SeededRoleV2.objects.create(name="child2", tenant=self.tenant)

        parent.children.add(child1, child2)

        self.assertEqual(parent.children.count(), 2)
        self.assertEqual(child1.parents.count(), 1)
        self.assertEqual(child1.parents.first(), parent)

    def test_mixed_type_queries(self):
        """Test querying across different role types."""
        custom_role = CustomRoleV2.objects.create(name=RoleV2.Types.CUSTOM, tenant=self.tenant)
        seeded_role = SeededRoleV2.objects.create(name=RoleV2.Types.SEEDED, tenant=self.tenant)
        platform_role = PlatformRoleV2.objects.create(name=RoleV2.Types.PLATFORM, tenant=self.tenant)

        # Base RoleV2 manager should return all types
        all_roles = RoleV2.objects.all()
        self.assertEqual(len(all_roles), 3)

        # Type-specific managers should filter correctly
        self.assertEqual(CustomRoleV2.objects.count(), 1)
        self.assertEqual(SeededRoleV2.objects.count(), 1)
        self.assertEqual(PlatformRoleV2.objects.count(), 1)

    def test_type_field_values(self):
        """Test that type field contains correct values."""
        custom_role = CustomRoleV2.objects.create(name=RoleV2.Types.CUSTOM, tenant=self.tenant)
        seeded_role = SeededRoleV2.objects.create(name=RoleV2.Types.SEEDED, tenant=self.tenant)
        platform_role = PlatformRoleV2.objects.create(name=RoleV2.Types.PLATFORM, tenant=self.tenant)

        # Refresh from DB to ensure values are persisted correctly
        custom_role.refresh_from_db()
        seeded_role.refresh_from_db()
        platform_role.refresh_from_db()

        self.assertEqual(custom_role.type, RoleV2.Types.CUSTOM)
        self.assertEqual(seeded_role.type, RoleV2.Types.SEEDED)
        self.assertEqual(platform_role.type, RoleV2.Types.PLATFORM)


class SeededRoleV2Tests(TestCase):
    def setUp(self):
        self.public_tenant = Tenant.objects.get(tenant_name="public")

    def test_v2_roles_for_empty(self):
        self.assertEqual(set(), SeededRoleV2.for_v1_roles([]))

    def test_v2_roles_for(self):
        v1_role_a = Role.objects.create(tenant=self.public_tenant, name="a system role", system=True)
        v2_role_a = seed_v2_role_from_v1(v1_role_a)

        v1_role_b = Role.objects.create(tenant=self.public_tenant, name="another system role", system=True)
        v2_role_b = seed_v2_role_from_v1(v1_role_b)

        self.assertEqual(
            {v2_role_a},
            SeededRoleV2.for_v1_roles([v1_role_a]),
        )

        self.assertEqual(
            {v2_role_a},
            SeededRoleV2.for_v1_roles([v1_role_a, v1_role_a]),
        )

        self.assertEqual(
            {v2_role_a, v2_role_b},
            SeededRoleV2.for_v1_roles([v1_role_a, v1_role_b]),
        )

    def test_v2_roles_for_fails(self):
        v1_role = Role.objects.create(tenant=self.public_tenant, name="a system role", system=True)

        with self.assertRaises(ValueError) as context:
            SeededRoleV2.for_v1_roles([v1_role])

        self.assertIn(repr(v1_role.pk), str(context.exception))

    def test_v2_roles_for_non_system(self):
        tenant = Tenant.objects.create(tenant_name="a tenant", org_id="123456")
        v1_role = Role.objects.create(tenant=tenant, name="a custom role", system=False)

        with self.assertRaises(ValueError) as context:
            SeededRoleV2.for_v1_roles([v1_role])

        self.assertIn(repr(v1_role.pk), str(context.exception))


class RoleV2QuerySetTests(IdentityRequest):
    """Tests for RoleV2QuerySet."""

    def setUp(self):
        """Set up roles of each type."""
        super().setUp()
        self.custom_role = RoleV2.objects.create(name="custom_role", type=RoleV2.Types.CUSTOM, tenant=self.tenant)
        self.seeded_role = RoleV2.objects.create(name="seeded_role", type=RoleV2.Types.SEEDED, tenant=self.tenant)
        self.platform_role = RoleV2.objects.create(
            name="platform_role", type=RoleV2.Types.PLATFORM, tenant=self.tenant
        )

    def tearDown(self):
        """Clean up roles."""
        RoleV2.objects.all().delete()

    def test_assignable_excludes_platform_roles(self):
        """assignable() should exclude platform roles."""
        assignable = RoleV2.objects.assignable()
        self.assertIn(self.custom_role, assignable)
        self.assertIn(self.seeded_role, assignable)
        self.assertNotIn(self.platform_role, assignable)

    def test_assignable_returns_only_custom_and_seeded(self):
        """assignable() should return exactly the custom and seeded roles."""
        assignable_ids = set(RoleV2.objects.assignable().values_list("uuid", flat=True))
        expected_ids = {self.custom_role.uuid, self.seeded_role.uuid}
        self.assertEqual(assignable_ids, expected_ids)

    def test_assignable_is_chainable(self):
        """assignable() should be chainable with other queryset methods."""
        result = RoleV2.objects.assignable().filter(name="custom_role")
        self.assertEqual(result.count(), 1)
        self.assertEqual(result.first(), self.custom_role)

    def test_assignable_with_no_platform_roles(self):
        """assignable() returns all roles when no platform roles exist."""
        self.platform_role.delete()
        self.assertEqual(RoleV2.objects.assignable().count(), 2)
        self.assertEqual(RoleV2.objects.count(), 2)

    def test_assignable_with_only_platform_roles(self):
        """assignable() returns empty queryset when only platform roles exist."""
        self.custom_role.delete()
        self.seeded_role.delete()
        self.assertEqual(RoleV2.objects.assignable().count(), 0)


class RoleV2ReplicationTupleTests(IdentityRequest):
    """Tests for RoleV2 relation tuple generation methods."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.perm_read = Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        self.perm_write = Permission.objects.create(permission="app:resource:write", tenant=self.tenant)
        self.perm_delete = Permission.objects.create(permission="app:resource:delete", tenant=self.tenant)

        self.custom_role = CustomRoleV2.objects.create(name="test_role", tenant=self.tenant)
        self.seeded_role = SeededRoleV2.objects.create(
            name="system_role", tenant=Tenant.objects.get(tenant_name="public")
        )

    def _custom_role_owner_tuple(self) -> RelationTuple:
        return role_owner_relationship(
            role_uuid=str(self.custom_role.uuid), tenant_resource_id=self.tenant.tenant_resource_id()
        )

    def _permission_tuple(self, role: RoleV2, permission: Permission) -> RelationTuple:
        """Build the expected permission tuple for self.role."""
        return role_permission_tuple(role_id=str(role.uuid), permission=permission.v2_string())

    def test_tuples_for_update_computes_permission_diff(self):
        """Test that tuples_for_update correctly computes permission differences."""
        read = self._permission_tuple(self.custom_role, self.perm_read)
        write = self._permission_tuple(self.custom_role, self.perm_write)
        delete = self._permission_tuple(self.custom_role, self.perm_delete)

        cases = [
            (
                "create",
                [],
                [self.perm_read, self.perm_write],
                {read, write},
                set(),
            ),
            (
                "noop",
                [self.perm_read, self.perm_write],
                [self.perm_read, self.perm_write],
                set(),
                set(),
            ),
            (
                "update_swap_permission",
                [self.perm_read, self.perm_write],
                [self.perm_write, self.perm_delete],
                {delete},
                {read},
            ),
            (
                "delete",
                [self.perm_read, self.perm_write],
                [],
                set(),
                {read, write},
            ),
        ]

        for label, old, new, expected_add, expected_remove in cases:
            with self.subTest(label):
                to_add, to_remove = RoleV2.tuples_for_update(
                    self.custom_role, old_permissions=old, new_permissions=new
                )
                self.assertEqual(set(to_add), expected_add)
                self.assertEqual(set(to_remove), expected_remove)

    def test_tuples_for_custom(self):
        """Test that creating/deleting a custom role uses both permission tuples and the owner tuple."""
        self.custom_role.permissions.set([self.perm_read])

        # These are semantically different, but they have the same behavior for the moment. We test them together for
        # simplicity.
        cases = [
            ("create", RoleV2.tuples_for_create),
            ("delete", RoleV2.tuples_for_delete),
        ]

        for label, function in cases:
            with self.subTest(label):
                self.assertCountEqual(
                    function(self.custom_role),
                    {
                        self._permission_tuple(self.custom_role, self.perm_read),
                        self._custom_role_owner_tuple(),
                    },
                )

    def test_tuples_for_delete_seeded(self):
        """Test that creating/deleting a seeded role uses only permission tuples."""
        self.seeded_role.permissions.set([self.perm_read])

        # Tested together for the reasons given above.
        cases = [
            ("create", RoleV2.tuples_for_create),
            ("delete", RoleV2.tuples_for_delete),
        ]

        for label, function in cases:
            with self.subTest(label):
                self.assertCountEqual(
                    function(self.seeded_role),
                    {
                        self._permission_tuple(self.seeded_role, self.perm_read),
                    },
                )
