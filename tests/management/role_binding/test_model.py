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
"""Test the RoleBinding models."""

import uuid

from django.db import IntegrityError, transaction

from api.models import Tenant
from management.models import (
    Group,
    Permission,
    RoleBinding,
    RoleBindingGroup,
    RoleV2,
)
from management.principal.model import Principal
from management.relation_replicator.types import ObjectReference, ObjectType, RelationTuple, SubjectReference
from management.role_binding.model import RoleBindingPrincipal
from tests.identity_request import IdentityRequest


class RoleBindingModelTests(IdentityRequest):
    """Test the RoleBinding models."""

    def setUp(self):
        """Set up the RoleBinding model tests."""
        super().setUp()

        # Test role
        self.role = RoleV2.objects.create(name="test_role", tenant=self.tenant)

        self.role.permissions.add(
            Permission.objects.create(
                tenant=Tenant.objects.get(tenant_name="public"),
                permission="app:resource:verb",
            )
        )

        # Test groups
        self.group1 = Group.objects.create(name="group1", tenant=self.tenant)
        self.group2 = Group.objects.create(name="group2", tenant=self.tenant)

        self.principal1: Principal = Principal.objects.create(tenant=self.tenant, username="p1", user_id="p1")
        self.principal2: Principal = Principal.objects.create(tenant=self.tenant, username="p2", user_id="p2")

    def tearDown(self):
        """Tear down RoleBinding model tests."""
        RoleBinding.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleV2.objects.all().delete()
        Group.objects.all().delete()

    def test_rolebinding_creation(self):
        """Test basic RoleBinding creation."""
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        self.assertEqual(binding.role, self.role)
        self.assertEqual(binding.resource_type, "workspace")
        self.assertEqual(binding.resource_id, "ws-12345")
        self.assertEqual(binding.tenant, self.tenant)
        self.assertTrue(binding.uuid)

    def test_rolebinding_unique_constraint(self):
        """Test unique constraint on (role, resource_type, resource_id)."""
        resource_type = "workspace"
        resource_id = "ws-12345"
        RoleBinding.objects.create(
            role=self.role,
            resource_type=resource_type,
            resource_id=resource_id,
            tenant=self.tenant,
        )

        # Try to create duplicate
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoleBinding.objects.create(
                    role=self.role,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    tenant=self.tenant,
                )

    def test_rolebinding_different_resources_same_role(self):
        """Test that same role can be bound to different resources."""
        binding1 = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        binding2 = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-67890",
            tenant=self.tenant,
        )

        self.assertNotEqual(binding1.id, binding2.id)
        self.assertEqual(binding1.role, binding2.role)

    def test_rolebinding_cascade_delete_on_role(self):
        """Test that deleting a role deletes its bindings."""
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        binding_id = binding.id
        self.role.delete()

        # Binding should be deleted
        with self.assertRaises(RoleBinding.DoesNotExist):
            RoleBinding.objects.get(id=binding_id)

    def test_rolebinding_cascade_delete_on_tenant(self):
        """Test that deleting a tenant deletes its role bindings."""
        # Create separate tenant with role and binding
        new_tenant = Tenant.objects.create(
            tenant_name="test_tenant_delete",
            org_id="test_org_delete",
        )

        new_role = RoleV2.objects.create(name="temp_role", tenant=new_tenant)
        binding = RoleBinding.objects.create(
            role=new_role,
            resource_type="workspace",
            resource_id="ws-temp",
            tenant=new_tenant,
        )

        binding_id = binding.id
        new_tenant.delete()

        # Binding should be deleted
        with self.assertRaises(RoleBinding.DoesNotExist):
            RoleBinding.objects.get(id=binding_id)

        # Role should be deleted
        with self.assertRaises(RoleV2.DoesNotExist):
            RoleV2.objects.get(id=new_role.id)

    def test_rolebindinggroup_creation(self):
        """Test basic RoleBindingGroup creation."""
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        binding_group = RoleBindingGroup.objects.create(
            group=self.group1,
            binding=binding,
        )

        self.assertEqual(binding_group.group, self.group1)
        self.assertEqual(binding_group.binding, binding)

    def test_rolebindinggroup_unique_constraint(self):
        """Test unique constraint on (group, binding)."""
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        RoleBindingGroup.objects.create(
            group=self.group1,
            binding=binding,
        )

        # Try to create duplicate
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoleBindingGroup.objects.create(
                    group=self.group1,
                    binding=binding,
                )

    def test_rolebindinggroup_different_groups_same_binding(self):
        """Test that different groups can have same binding."""
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        bg1 = RoleBindingGroup.objects.create(
            group=self.group1,
            binding=binding,
        )

        bg2 = RoleBindingGroup.objects.create(
            group=self.group2,
            binding=binding,
        )

        self.assertNotEqual(bg1.id, bg2.id)
        self.assertEqual(bg1.binding, bg2.binding)

    def test_rolebindinggroup_cascade_delete_on_binding(self):
        """Test that deleting a binding deletes its group entries."""
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        bg = RoleBindingGroup.objects.create(
            group=self.group1,
            binding=binding,
        )

        bg_id = bg.id
        binding.delete()

        # Group entry should be deleted
        with self.assertRaises(RoleBindingGroup.DoesNotExist):
            RoleBindingGroup.objects.get(id=bg_id)

    def test_rolebindinggroup_protect_delete_on_group(self):
        """Test that deleting a group with existing binding entries fails."""
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        bg = RoleBindingGroup.objects.create(
            group=self.group1,
            binding=binding,
        )

        bg_id = bg.id
        group_id = self.group1.id

        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                self.group1.delete()

        self.assertTrue(Group.objects.filter(id=group_id).exists())
        self.assertTrue(RoleBindingGroup.objects.filter(id=bg_id).exists())

    def test_complete_rolebinding_scenario(self):
        """Test a complete scenario with role binding and groups."""
        # Create binding
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        # Add groups
        RoleBindingGroup.objects.create(
            group=self.group1,
            binding=binding,
        )
        RoleBindingGroup.objects.create(
            group=self.group2,
            binding=binding,
        )

        # Verify relationships
        self.assertEqual(binding.group_entries.count(), 2)

        # Verify reverse relationships
        self.assertEqual(self.group1.role_binding_entries.count(), 1)
        self.assertEqual(self.group2.role_binding_entries.count(), 1)

    def test_rolebinding_related_names(self):
        """Test related_name attributes from binding classes work correctly."""
        binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        # Test role.bindings
        self.assertIn(binding, self.role.bindings.all())

        # Test binding.group_entries
        bg = RoleBindingGroup.objects.create(
            group=self.group1,
            binding=binding,
        )

        self.assertIn(bg, binding.group_entries.all())

        # Test group.role_binding_entries
        self.assertIn(bg, self.group1.role_binding_entries.all())

    def test_as_migration_value(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        binding.group_entries.create(
            group=self.group1,
            binding=binding,
        )

        migration_value = binding.as_migration_value()

        self.assertEqual(str(binding.uuid), migration_value.id)
        self.assertEqual(("rbac", "workspace"), migration_value.resource.resource_type)
        self.assertEqual("ws-12345", migration_value.resource.resource_id)
        self.assertEqual(str(self.role.uuid), migration_value.role.id)
        self.assertCountEqual(["app_resource_verb"], migration_value.role.permissions)
        self.assertFalse(migration_value.role.is_system)
        self.assertCountEqual([str(self.group1.uuid)], migration_value.groups)
        self.assertEqual({}, migration_value.users)

    def test_as_migration_value_force_groups(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        group_uuid = uuid.uuid4()
        migration_value = binding.as_migration_value(force_group_uuids=[str(group_uuid)])

        self.assertEqual(str(binding.uuid), migration_value.id)
        self.assertEqual(("rbac", "workspace"), migration_value.resource.resource_type)
        self.assertEqual("ws-12345", migration_value.resource.resource_id)
        self.assertEqual(str(self.role.uuid), migration_value.role.id)
        self.assertCountEqual(["app_resource_verb"], migration_value.role.permissions)
        self.assertFalse(migration_value.role.is_system)
        self.assertCountEqual([str(group_uuid)], migration_value.groups)
        self.assertEqual({}, migration_value.users)

    def test_role_binding_group_has_created_timestamp(self):
        """Test that RoleBindingGroup gets a created timestamp when a group is assigned."""
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )
        binding.update_groups([self.group1])

        entry = RoleBindingGroup.objects.get(binding=binding, group=self.group1)
        self.assertIsNotNone(entry.created)

    def test_role_binding_principal_has_created_timestamp(self):
        """Test that RoleBindingPrincipal gets a created timestamp when a principal is assigned."""
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )
        binding.update_principals([("test_source", self.principal1)])

        entry = RoleBindingPrincipal.objects.get(binding=binding, principal=self.principal1)
        self.assertIsNotNone(entry.created)

    def test_update_groups(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        binding.update_groups([self.group1])
        self.assertCountEqual([self.group1], binding.bound_groups())

        binding.update_groups([self.group2])
        self.assertCountEqual([self.group2], binding.bound_groups())

        binding.update_groups([self.group1, self.group2])
        self.assertCountEqual([self.group1, self.group2], binding.bound_groups())

        binding.update_groups([])
        self.assertCountEqual([], binding.bound_groups())

    def test_update_groups_by_uuid(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        binding.update_groups_by_uuid([self.group1.uuid])
        self.assertCountEqual([self.group1], binding.bound_groups())

        binding.update_groups_by_uuid([self.group2.uuid])
        self.assertCountEqual([self.group2], binding.bound_groups())

        binding.update_groups_by_uuid([self.group1.uuid, self.group2.uuid])
        self.assertCountEqual([self.group1, self.group2], binding.bound_groups())

        binding.update_groups_by_uuid([])
        self.assertCountEqual([], binding.bound_groups())

    def test_update_groups_by_uuid_invalid(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        binding.update_groups_by_uuid([self.group1.uuid, self.group2.uuid])
        self.assertCountEqual([self.group1, self.group2], binding.bound_groups())

        with self.assertRaises(ValueError):
            # Attempt to pass a non-existent group UUID.
            binding.update_groups_by_uuid([self.group1.uuid, uuid.uuid4()])

        # The set of groups should not change after a failed attempt to set the groups.
        self.assertCountEqual([self.group1, self.group2], binding.bound_groups())

    def test_update_principals(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        pair1 = ("car/1", self.principal1)
        pair2 = ("car/2", self.principal2)

        binding.update_principals([pair1])
        self.assertCountEqual([self.principal1], binding.bound_principals())

        binding.update_principals([pair2])
        self.assertCountEqual([self.principal2], binding.bound_principals())

        binding.update_principals([pair1, pair2])
        self.assertCountEqual([self.principal1, self.principal2], binding.bound_principals())

        binding.update_principals([])
        self.assertCountEqual([], binding.bound_principals())

        # Multiple principals from the same source should work.
        binding.update_principals([("car", self.principal1), ("car", self.principal2)])
        self.assertCountEqual([self.principal1, self.principal2], binding.bound_principals())

        # Multiple sources of the same principal should work.
        binding.update_principals([("car/1", self.principal1), ("car/2", self.principal1)])
        self.assertCountEqual([self.principal1], binding.bound_principals())
        self.assertEqual(binding.principal_entries.all().count(), 2)

    def test_update_principals_by_user_id(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        pair1: tuple[str, str] = ("car/1", self.principal1.user_id)
        pair2: tuple[str, str] = ("car/2", self.principal2.user_id)

        binding.update_principals_by_user_id([pair1])
        self.assertCountEqual([self.principal1], binding.bound_principals())

        binding.update_principals_by_user_id([pair2])
        self.assertCountEqual([self.principal2], binding.bound_principals())

        binding.update_principals_by_user_id([pair1, pair2])
        self.assertCountEqual([self.principal1, self.principal2], binding.bound_principals())

        binding.update_principals_by_user_id([])
        self.assertCountEqual([], binding.bound_principals())

        # Multiple principals from the same source should work.
        binding.update_principals_by_user_id([("car", self.principal1.user_id), ("car", self.principal2.user_id)])
        self.assertCountEqual([self.principal1, self.principal2], binding.bound_principals())

        # Multiple sources of the same principal should work.
        binding.update_principals_by_user_id([("car/1", self.principal1.user_id), ("car/2", self.principal1.user_id)])
        self.assertCountEqual([self.principal1], binding.bound_principals())
        self.assertEqual(binding.principal_entries.all().count(), 2)

    def test_update_principals_by_user_id_nonexistent(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        binding.update_principals([("car/1", self.principal1)])

        with self.assertRaisesRegex(ValueError, ".*test_nonexistent.*"):
            binding.update_principals_by_user_id([("car", "test_nonexistent")])

        # The existing principals should be unchanged.
        self.assertCountEqual([self.principal1], binding.bound_principals())

    def test_principal_no_source(self):
        binding: RoleBinding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-12345",
            tenant=self.tenant,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoleBindingPrincipal.objects.create(
                    binding=binding,
                    principal=self.principal1,
                )


class RoleBindingQuerySetTests(IdentityRequest):
    """Tests for the RoleBindingQuerySet custom methods."""

    def setUp(self):
        """Set up test data for queryset tests."""
        super().setUp()
        self.role = RoleV2.objects.create(name="test_role", tenant=self.tenant)
        self.group = Group.objects.create(name="group1", tenant=self.tenant)
        self.principal = Principal.objects.create(tenant=self.tenant, username="p1", user_id="p1")

    def _create_binding(self, resource_id="ws-1"):
        return RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id=resource_id,
            tenant=self.tenant,
        )

    def test_orphaned_returns_binding_with_no_subjects(self):
        """A binding with no groups or principals is orphaned."""
        binding = self._create_binding()

        orphaned = RoleBinding.objects.filter(id=binding.id).orphaned()
        self.assertEqual(list(orphaned), [binding])

    def test_orphaned_excludes_binding_with_group(self):
        """A binding with a group attached is not orphaned."""
        binding = self._create_binding()
        RoleBindingGroup.objects.create(binding=binding, group=self.group)

        orphaned = RoleBinding.objects.filter(id=binding.id).orphaned()
        self.assertEqual(list(orphaned), [])

    def test_orphaned_excludes_binding_with_principal(self):
        """A binding with a principal attached is not orphaned."""
        binding = self._create_binding()
        RoleBindingPrincipal.objects.create(binding=binding, principal=self.principal, source="v2_api")

        orphaned = RoleBinding.objects.filter(id=binding.id).orphaned()
        self.assertEqual(list(orphaned), [])

    def test_orphaned_filters_mixed_set(self):
        """Given a mix of orphaned and non-orphaned bindings, only orphaned are returned."""
        orphan = self._create_binding(resource_id="ws-orphan")
        active = self._create_binding(resource_id="ws-active")
        RoleBindingGroup.objects.create(binding=active, group=self.group)

        orphaned = RoleBinding.objects.filter(id__in=[orphan.id, active.id]).orphaned()
        self.assertEqual(list(orphaned), [orphan])

    def test_orphaned_delete_removes_only_orphans(self):
        """Calling .orphaned().delete() removes only the orphaned bindings."""
        orphan = self._create_binding(resource_id="ws-orphan")
        active = self._create_binding(resource_id="ws-active")
        RoleBindingGroup.objects.create(binding=active, group=self.group)

        RoleBinding.objects.filter(id__in=[orphan.id, active.id]).orphaned().delete()

        self.assertFalse(RoleBinding.objects.filter(id=orphan.id).exists())
        self.assertTrue(RoleBinding.objects.filter(id=active.id).exists())


class RoleBindingReplicationTupleTests(IdentityRequest):
    """Tests for RoleBinding relation tuple generation methods.

    Verifies that the pure data transformation methods on RoleBinding
    produce the correct RelationTuple objects for SpiceDB replication.
    """

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.role = RoleV2.objects.create(name="test_role", tenant=self.tenant)
        self.group = Group.objects.create(name="group1", tenant=self.tenant)
        self.principal = Principal.objects.create(tenant=self.tenant, username="p1", user_id="user123")

        self.binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="ws-1",
            tenant=self.tenant,
        )

    # ── Individual tuple methods ─────────────────────────────────────

    def test_role_relation_tuple(self):
        """_role_relation_tuple produces role_binding#role@role."""
        t = self.binding._role_relation_tuple()

        self.assertEqual(t.resource.type.namespace, "rbac")
        self.assertEqual(t.resource.type.name, "role_binding")
        self.assertEqual(t.resource.id, str(self.binding.uuid))
        self.assertEqual(t.relation, "role")
        self.assertEqual(t.subject.subject.type.namespace, "rbac")
        self.assertEqual(t.subject.subject.type.name, "role")
        self.assertEqual(t.subject.subject.id, str(self.role.uuid))
        self.assertIsNone(t.subject.relation)

    def test_resource_binding_tuple(self):
        """_resource_binding_tuple produces resource#binding@role_binding."""
        t = self.binding._resource_binding_tuple()

        self.assertEqual(t.resource.type.namespace, "rbac")
        self.assertEqual(t.resource.type.name, "workspace")
        self.assertEqual(t.resource.id, "ws-1")
        self.assertEqual(t.relation, "binding")
        self.assertEqual(t.subject.subject.type.name, "role_binding")
        self.assertEqual(t.subject.subject.id, str(self.binding.uuid))

    def test_group_subject_tuple(self):
        """_group_subject_tuple produces role_binding#subject@group[#member]."""
        t = self.binding._group_subject_tuple(self.group)

        self.assertEqual(t.resource.type.name, "role_binding")
        self.assertEqual(t.resource.id, str(self.binding.uuid))
        self.assertEqual(t.relation, "subject")
        self.assertEqual(t.subject.subject.type.name, "group")
        self.assertEqual(t.subject.subject.id, str(self.group.uuid))
        self.assertEqual(t.subject.relation, "member")

    def test_user_subject_tuple(self):
        """_user_subject_tuple produces role_binding#subject@principal."""
        t = self.binding._user_subject_tuple(self.principal)

        self.assertEqual(t.resource.type.name, "role_binding")
        self.assertEqual(t.resource.id, str(self.binding.uuid))
        self.assertEqual(t.relation, "subject")
        self.assertEqual(t.subject.subject.type.name, "principal")
        self.assertIn("user123", t.subject.subject.id)
        self.assertIsNone(t.subject.relation)

    def test_binding_tuples_returns_role_and_resource(self):
        """binding_tuples returns exactly the role + resource tuples."""
        tuples = self.binding.binding_tuples()

        self.assertEqual(len(tuples), 2)
        relations = {t.relation for t in tuples}
        self.assertEqual(relations, {"role", "binding"})

    def test_subject_tuple_dispatches_to_group(self):
        """subject_tuple with a Group calls _group_subject_tuple."""
        t = self.binding.subject_tuple(self.group)

        self.assertEqual(t.subject.subject.type.name, "group")
        self.assertEqual(t.subject.relation, "member")

    def test_subject_tuple_dispatches_to_principal(self):
        """subject_tuple with a Principal calls _user_subject_tuple."""
        t = self.binding.subject_tuple(self.principal)

        self.assertEqual(t.subject.subject.type.name, "principal")
        self.assertIsNone(t.subject.relation)

    # ── replication_tuples (changeset → full tuple list) ─────────────

    def test_replication_tuples_empty_changeset(self):
        """Empty changeset produces empty tuple lists."""
        to_add, to_remove = RoleBinding.replication_tuples(subject=self.group)

        self.assertEqual(to_add, [])
        self.assertEqual(to_remove, [])

    def test_replication_tuples_created_bindings(self):
        """Created bindings produce binding-level add tuples."""
        to_add, to_remove = RoleBinding.replication_tuples(
            subject=self.group,
            bindings_created=[self.binding],
        )

        # 2 tuples: role + resource
        self.assertEqual(len(to_add), 2)
        self.assertEqual(to_remove, [])
        relations = {t.relation for t in to_add}
        self.assertEqual(relations, {"role", "binding"})

    def test_replication_tuples_deleted_bindings(self):
        """Deleted bindings produce binding-level remove tuples."""
        to_add, to_remove = RoleBinding.replication_tuples(
            subject=self.group,
            bindings_deleted=[self.binding],
        )

        self.assertEqual(to_add, [])
        self.assertEqual(len(to_remove), 2)
        relations = {t.relation for t in to_remove}
        self.assertEqual(relations, {"role", "binding"})

    def test_replication_tuples_subject_linked(self):
        """Subject linked produces subject add tuples."""
        to_add, to_remove = RoleBinding.replication_tuples(
            subject=self.group,
            subject_linked_to=[self.binding],
        )

        self.assertEqual(len(to_add), 1)
        self.assertEqual(to_remove, [])
        self.assertEqual(to_add[0].relation, "subject")
        self.assertEqual(to_add[0].subject.subject.type.name, "group")

    def test_replication_tuples_subject_unlinked(self):
        """Subject unlinked produces subject remove tuples."""
        to_add, to_remove = RoleBinding.replication_tuples(
            subject=self.principal,
            subject_unlinked_from=[self.binding],
        )

        self.assertEqual(to_add, [])
        self.assertEqual(len(to_remove), 1)
        self.assertEqual(to_remove[0].relation, "subject")
        self.assertEqual(to_remove[0].subject.subject.type.name, "principal")

    def test_replication_tuples_full_changeset(self):
        """Full changeset with creates, deletes, links, and unlinks."""
        role2 = RoleV2.objects.create(name="role2", tenant=self.tenant)
        binding2 = RoleBinding.objects.create(
            role=role2,
            resource_type="workspace",
            resource_id="ws-1",
            tenant=self.tenant,
        )

        to_add, to_remove = RoleBinding.replication_tuples(
            subject=self.group,
            bindings_created=[self.binding],
            bindings_deleted=[binding2],
            subject_linked_to=[self.binding],
            subject_unlinked_from=[binding2],
        )

        # Adds: 2 binding tuples (role + resource) + 1 subject tuple = 3
        self.assertEqual(len(to_add), 3)
        # Removes: 2 binding tuples (role + resource) + 1 subject tuple = 3
        self.assertEqual(len(to_remove), 3)

    # ── all_tuples (full snapshot for a binding) ───────────────────

    def test_all_tuples_returns_exact_tuples_for_each_subject_combination(self):
        """all_tuples returns the exact set of SpiceDB tuples derived from DB state."""
        binding_ref = ObjectReference(
            type=ObjectType(namespace="rbac", name="role_binding"), id=str(self.binding.uuid)
        )
        principal_resource_id = Principal.user_id_to_principal_resource_id(self.principal.user_id)

        role_tuple = RelationTuple(
            resource=binding_ref,
            relation="role",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="role"), id=str(self.role.uuid))
            ),
        )
        resource_tuple = RelationTuple(
            resource=ObjectReference(type=ObjectType(namespace="rbac", name="workspace"), id="ws-1"),
            relation="binding",
            subject=SubjectReference(subject=binding_ref),
        )
        group_subject = RelationTuple(
            resource=binding_ref,
            relation="subject",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="group"), id=str(self.group.uuid)),
                relation="member",
            ),
        )
        principal_subject = RelationTuple(
            resource=binding_ref,
            relation="subject",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="principal"), id=principal_resource_id)
            ),
        )

        cases = [
            ("group_only", [self.group], [], {role_tuple, resource_tuple, group_subject}),
            ("principal_only", [], [self.principal], {role_tuple, resource_tuple, principal_subject}),
            ("both", [self.group], [self.principal], {role_tuple, resource_tuple, group_subject, principal_subject}),
        ]
        for label, groups, principals, expected in cases:
            with self.subTest(label):
                self.binding.group_entries.all().delete()
                RoleBindingPrincipal.objects.filter(binding=self.binding).delete()
                for g in groups:
                    RoleBindingGroup.objects.create(binding=self.binding, group=g)
                for p in principals:
                    RoleBindingPrincipal.objects.create(binding=self.binding, principal=p, source="direct")

                self.assertEqual(set(self.binding.all_tuples()), expected)
