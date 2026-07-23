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
"""Tests for RoleBindingQuerySet."""

import uuid

from api.models import Tenant
from management.group.model import Group
from management.role.v2_model import RoleV2
from management.models import Principal
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from tests.identity_request import IdentityRequest


class RoleBindingQuerySetTest(IdentityRequest):
    """Tests for RoleBindingQuerySet chainable methods."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.role_a = RoleV2.objects.create(name="role_a", tenant=self.tenant)
        self.role_b = RoleV2.objects.create(name="role_b", tenant=self.tenant)

        self.group = Group.objects.create(name="test_group", tenant=self.tenant)

        self.binding_a = RoleBinding.objects.create(
            role=self.role_a,
            resource_type="workspace",
            resource_id="res-1",
            tenant=self.tenant,
        )
        self.binding_b = RoleBinding.objects.create(
            role=self.role_b,
            resource_type="workspace",
            resource_id="res-2",
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(group=self.group, binding=self.binding_a)
        RoleBindingGroup.objects.create(group=self.group, binding=self.binding_b)

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    # --- Tenant filtering ---

    def test_returns_bindings_for_tenant(self):
        """Test that for_tenant returns all bindings belonging to the tenant."""
        qs = RoleBinding.objects.for_tenant(self.tenant)
        self.assertEqual(set(qs), {self.binding_a, self.binding_b})

    def test_excludes_other_tenant_bindings(self):
        """Test that for_tenant excludes bindings from other tenants."""
        other_tenant = Tenant.objects.create(tenant_name="other", org_id="other_org")
        other_role = RoleV2.objects.create(name="other_role", tenant=other_tenant)
        other_binding = RoleBinding.objects.create(
            role=other_role,
            resource_type="workspace",
            resource_id="other-res",
            tenant=other_tenant,
        )

        try:
            qs = RoleBinding.objects.for_tenant(self.tenant)
            self.assertNotIn(other_binding, qs)
            self.assertEqual(qs.count(), 2)
        finally:
            other_binding.delete()
            other_role.delete()
            other_tenant.delete()

    def test_returns_empty_for_tenant_with_no_bindings(self):
        """Test that for_tenant returns empty queryset for a tenant with no bindings."""
        empty_tenant = Tenant.objects.create(tenant_name="empty", org_id="empty_org")
        try:
            qs = RoleBinding.objects.for_tenant(empty_tenant)
            self.assertEqual(qs.count(), 0)
        finally:
            empty_tenant.delete()

    # --- role_id filtering ---

    def test_role_id_filtering(self):
        """Test that for_role filters correctly for various inputs."""
        cases = [
            ("match_role_a", self.role_a.uuid, 1, {self.binding_a}),
            ("match_role_b", self.role_b.uuid, 1, {self.binding_b}),
            ("no_match", uuid.uuid4(), 0, set()),
        ]
        for label, role_id, expected_count, expected_set in cases:
            with self.subTest(label=label):
                qs = RoleBinding.objects.for_tenant(self.tenant).for_role(role_id)
                self.assertEqual(qs.count(), expected_count)
                self.assertEqual(set(qs), expected_set)

        # None case: no for_role call returns all
        qs = RoleBinding.objects.for_tenant(self.tenant)
        self.assertEqual(qs.count(), 2)
        self.assertEqual(set(qs), {self.binding_a, self.binding_b})

    # --- Eager loading ---

    def test_eager_loading(self):
        """Test that role and group relations are eagerly loaded (no extra queries)."""
        qs = RoleBinding.objects.for_tenant(self.tenant)
        binding = list(qs)[0]  # evaluate queryset to trigger prefetch

        cases = [
            ("select_related_role", lambda b: b.role.name),
            (
                "prefetch_group_entries",
                lambda b: [e.group.name for e in b.group_entries.all()],
            ),
        ]
        for label, access_fn in cases:
            with self.subTest(label=label):
                with self.assertNumQueries(0):
                    access_fn(binding)

    # --- No-op chainable method tests ---

    def test_for_resource_filter_noop_when_no_args(self):
        """for_resource_filter() with no args should be a no-op and return all tenant bindings."""
        qs_all = RoleBinding.objects.for_tenant(self.tenant)
        qs_filtered = qs_all.for_resource_filter()
        self.assertEqual(set(qs_filtered), set(qs_all))

    def test_for_subject_noop_when_no_args(self):
        """for_subject() with no args should be a no-op and return all tenant bindings."""
        qs_all = RoleBinding.objects.for_tenant(self.tenant)
        qs_filtered = qs_all.for_subject()
        self.assertEqual(set(qs_filtered), set(qs_all))

    # --- resource_id / resource_type filtering ---

    def test_resource_filtering(self):
        """Test that resource_id and resource_type filter independently."""
        cases = [
            ("both_match", "res-1", "workspace", 1, {self.binding_a}),
            ("both_match_2", "res-2", "workspace", 1, {self.binding_b}),
            ("both_no_match_id", "nonexistent", "workspace", 0, set()),
            ("both_no_match_type", "res-1", "other_type", 0, set()),
            ("id_only", "res-1", None, 1, {self.binding_a}),
            ("id_only_no_match", "nonexistent", None, 0, set()),
            ("type_only", None, "workspace", 2, {self.binding_a, self.binding_b}),
            ("type_only_no_match", None, "other_type", 0, set()),
            ("none_returns_all", None, None, 2, {self.binding_a, self.binding_b}),
        ]
        for label, res_id, res_type, expected_count, expected_set in cases:
            with self.subTest(label=label):
                qs = RoleBinding.objects.for_tenant(self.tenant).for_resource_filter(
                    resource_id=res_id, resource_type=res_type
                )
                self.assertEqual(qs.count(), expected_count)
                self.assertEqual(set(qs), expected_set)

    # --- subject_id filtering ---

    def test_subject_id_filtering(self):
        """Test that subject_id filters bindings by group UUID."""
        other_group = Group.objects.create(name="other_group", tenant=self.tenant)
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingGroup.objects.create(group=other_group, binding=binding_c)

        cases = [
            ("match_self_group", self.group.uuid, 2, {self.binding_a, self.binding_b}),
            ("match_other_group", other_group.uuid, 1, {binding_c}),
            ("no_match", uuid.uuid4(), 0, set()),
            ("none_returns_all", None, 3, {self.binding_a, self.binding_b, binding_c}),
        ]
        try:
            for label, subject_id, expected_count, expected_set in cases:
                with self.subTest(label=label):
                    qs = RoleBinding.objects.for_tenant(self.tenant).for_subject(subject_id=subject_id)
                    self.assertEqual(qs.count(), expected_count)
                    self.assertEqual(set(qs), expected_set)
        finally:
            RoleBindingGroup.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            other_group.delete()

    # --- subject_type filtering ---

    def test_subject_type_filtering(self):
        """Test that subject_type filters correctly for group, user, and unknown types."""
        principal = Principal.objects.create(username="test_user", tenant=self.tenant, user_id="user-123")
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            cases = [
                ("group_returns_group_bindings", "group", 2),
                ("user_returns_user_bindings", "user", 1),
                ("unknown_returns_empty", "unknown", 0),
                ("none_returns_all", None, 3),
            ]
            for label, subject_type, expected_count in cases:
                with self.subTest(label=label):
                    qs = RoleBinding.objects.for_tenant(self.tenant).for_subject(subject_type=subject_type)
                    self.assertEqual(qs.count(), expected_count)
        finally:
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    def test_subject_type_user_with_subject_id(self):
        """Test filtering by subject_type=user and subject_id (principal UUID)."""
        principal = Principal.objects.create(username="test_user", tenant=self.tenant, user_id="user-456")
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            cases = [
                ("match", principal.uuid, 1, {binding_c}),
                ("no_match", uuid.uuid4(), 0, set()),
            ]
            for label, sid, expected_count, expected_set in cases:
                with self.subTest(label=label):
                    qs = RoleBinding.objects.for_tenant(self.tenant).for_subject(subject_type="user", subject_id=sid)
                    self.assertEqual(qs.count(), expected_count)
                    self.assertEqual(set(qs), expected_set)
        finally:
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    def test_subject_id_without_type_searches_both(self):
        """Test that subject_id without subject_type searches groups and principals."""
        principal = Principal.objects.create(username="test_user", tenant=self.tenant, user_id="user-789")
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            # subject_id matching a group
            qs = RoleBinding.objects.for_tenant(self.tenant).for_subject(subject_id=self.group.uuid)
            self.assertEqual(qs.count(), 2)

            # subject_id matching a principal
            qs = RoleBinding.objects.for_tenant(self.tenant).for_subject(subject_id=principal.uuid)
            self.assertEqual(qs.count(), 1)
            self.assertEqual(set(qs), {binding_c})
        finally:
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    def test_subject_type_mismatch_returns_empty(self):
        """Test that subject_type narrows the search to the correct relation table.

        A group UUID passed with subject_type='user' should return nothing (and vice versa),
        because the type constrains which relation table is queried.
        """
        principal = Principal.objects.create(username="mismatch_user", tenant=self.tenant, user_id="user-mismatch")
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            # group UUID with subject_type='user' — searches principal_entries, finds nothing
            qs = RoleBinding.objects.for_tenant(self.tenant).for_subject(
                subject_type="user", subject_id=self.group.uuid
            )
            self.assertEqual(qs.count(), 0)

            # principal UUID with subject_type='group' — searches group_entries, finds nothing
            qs = RoleBinding.objects.for_tenant(self.tenant).for_subject(
                subject_type="group", subject_id=principal.uuid
            )
            self.assertEqual(qs.count(), 0)
        finally:
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    # --- for_granted_subject ---

    def test_granted_subject_group_returns_group_bindings(self):
        """Test that for_granted_subject with type=group returns bindings for that group."""
        qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
            granted_subject_type="group", granted_subject_id=self.group.uuid
        )
        self.assertEqual(set(qs), {self.binding_a, self.binding_b})

    def test_granted_subject_group_no_match(self):
        """Test that for_granted_subject with non-existent group returns empty."""
        qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
            granted_subject_type="group", granted_subject_id=uuid.uuid4()
        )
        self.assertEqual(qs.count(), 0)

    def test_granted_subject_user_returns_direct_and_group_bindings(self):
        """Test that for_granted_subject with type=user returns direct user bindings + group bindings."""
        principal = Principal.objects.create(username="granted_user", tenant=self.tenant, user_id="user-granted")
        self.group.principals.add(principal)
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
                granted_subject_type="user",
                granted_subject_id=principal.uuid,
            )
            self.assertEqual(set(qs), {binding_c, self.binding_a, self.binding_b})
        finally:
            self.group.principals.remove(principal)
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    def test_granted_subject_user_no_groups(self):
        """Test that for_granted_subject with type=user and no groups returns only direct bindings."""
        principal = Principal.objects.create(username="solo_user", tenant=self.tenant, user_id="user-solo")
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
                granted_subject_type="user",
                granted_subject_id=principal.uuid,
            )
            self.assertEqual(set(qs), {binding_c})
        finally:
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    def test_granted_subject_user_no_direct_bindings(self):
        """Test that for_granted_subject with type=user returns group bindings even without direct bindings."""
        principal = Principal.objects.create(username="group_only", tenant=self.tenant, user_id="user-grp")
        self.group.principals.add(principal)

        try:
            qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
                granted_subject_type="user",
                granted_subject_id=principal.uuid,
            )
            self.assertEqual(set(qs), {self.binding_a, self.binding_b})
        finally:
            self.group.principals.remove(principal)
            principal.delete()

    def test_granted_subject_user_lookup_by_user_id(self):
        """Test that for_granted_subject falls back to user_id when uuid doesn't match."""
        principal = Principal.objects.create(username="uid_user", tenant=self.tenant, user_id="user-id-123")
        self.group.principals.add(principal)
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
                granted_subject_type="user",
                granted_subject_id="user-id-123",
            )
            self.assertEqual(set(qs), {binding_c, self.binding_a, self.binding_b})
        finally:
            self.group.principals.remove(principal)
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    def test_granted_subject_nonexistent_user_returns_empty(self):
        """Test that for_granted_subject with non-existent principal returns empty."""
        qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
            granted_subject_type="user", granted_subject_id=str(uuid.uuid4())
        )
        self.assertEqual(qs.count(), 0)

    def test_granted_subject_invalid_type_returns_empty(self):
        """Test that for_granted_subject with unknown type returns empty queryset."""
        qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
            granted_subject_type="service-account", granted_subject_id=uuid.uuid4()
        )
        self.assertEqual(qs.count(), 0)

    def test_granted_subject_without_for_tenant_raises(self):
        """Test that for_granted_subject raises ValueError without prior for_tenant()."""
        with self.assertRaises(ValueError):
            RoleBinding.objects.all().for_granted_subject(
                granted_subject_type="group", granted_subject_id=self.group.uuid
            )

    # --- for_granted_subject with type=principal ---

    def test_granted_subject_principal_with_user_id_returns_direct_and_group_bindings(self):
        """Test that for_granted_subject with type=principal and user_id returns direct + group bindings."""
        principal = Principal.objects.create(username="principal_user", tenant=self.tenant, user_id="ext-user-123")
        self.group.principals.add(principal)
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
                granted_subject_type="principal",
                granted_subject_principal_user_id="ext-user-123",
            )
            self.assertEqual(set(qs), {binding_c, self.binding_a, self.binding_b})
        finally:
            self.group.principals.remove(principal)
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    def test_granted_subject_principal_user_id_no_match_returns_empty(self):
        """Test that for_granted_subject with type=principal and non-existent user_id returns empty."""
        qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
            granted_subject_type="principal",
            granted_subject_principal_user_id="nonexistent-user",
        )
        self.assertEqual(qs.count(), 0)

    def test_granted_subject_principal_without_user_id_returns_empty(self):
        """Test that for_granted_subject with type=principal and no user_id returns nothing."""
        qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
            granted_subject_type="principal",
        )
        self.assertEqual(qs.count(), 0)

    def test_granted_subject_principal_user_id_only_matches_user_id_not_uuid(self):
        """Test that principal type resolves by user_id only, not by UUID."""
        principal = Principal.objects.create(username="uuid_test", tenant=self.tenant, user_id="uid-only")
        role_c = RoleV2.objects.create(name="role_c", tenant=self.tenant)
        binding_c = RoleBinding.objects.create(
            role=role_c, resource_type="workspace", resource_id="res-3", tenant=self.tenant
        )
        RoleBindingPrincipal.objects.create(principal=principal, binding=binding_c, source="default")

        try:
            qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
                granted_subject_type="principal",
                granted_subject_principal_user_id=str(principal.uuid),
            )
            self.assertEqual(qs.count(), 0)

            qs = RoleBinding.objects.for_tenant(self.tenant).for_granted_subject(
                granted_subject_type="principal",
                granted_subject_principal_user_id="uid-only",
            )
            self.assertEqual(set(qs), {binding_c})
        finally:
            RoleBindingPrincipal.objects.filter(binding=binding_c).delete()
            binding_c.delete()
            role_c.delete()
            principal.delete()

    # --- Combined filters ---

    def test_combined_role_and_resource_filter(self):
        """Test role_id + resource filters together."""
        qs = (
            RoleBinding.objects.for_tenant(self.tenant)
            .for_role(self.role_a.uuid)
            .for_resource_filter(resource_id="res-1", resource_type="workspace")
        )
        self.assertEqual(set(qs), {self.binding_a})

    def test_combined_role_and_resource_no_match(self):
        """Test that combining filters that don't intersect returns empty."""
        qs = (
            RoleBinding.objects.for_tenant(self.tenant)
            .for_role(self.role_a.uuid)
            .for_resource_filter(resource_id="res-2", resource_type="workspace")
        )
        self.assertEqual(qs.count(), 0)

    def test_combined_all_filters(self):
        """Test all filters together (role_id + resource + subject)."""
        qs = (
            RoleBinding.objects.for_tenant(self.tenant)
            .for_role(self.role_a.uuid)
            .for_resource_filter(resource_id="res-1", resource_type="workspace")
            .for_subject(subject_type="group", subject_id=self.group.uuid)
        )
        self.assertEqual(set(qs), {self.binding_a})

    # --- Annotation ---

    def test_annotates_role_created(self):
        """Test that role_created annotation is present and correct."""
        qs = RoleBinding.objects.for_tenant(self.tenant)
        for binding, role in [
            (self.binding_a, self.role_a),
            (self.binding_b, self.role_b),
        ]:
            with self.subTest(role=role.name):
                result = qs.get(pk=binding.pk)
                self.assertEqual(result.role_created, role.created)
