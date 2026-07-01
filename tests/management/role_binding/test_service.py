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
"""Tests for the RoleBindingService and Serializer."""

import concurrent.futures
import uuid
from unittest.mock import patch

from api.models import Tenant
from django.test import TestCase, override_settings
from management.principal.model import Principal as PrincipalModel
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import RelationReplicator, ReplicationEventType
from management.tenant_mapping.v2_activation import assert_v1_write_allowed, is_v2_write_activated
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    all_of,
    relation,
    resource,
    resource_type,
    subject,
)

from management.models import Group, Permission, Principal, Role, Workspace
from management.role.v2_model import PlatformRoleV2, RoleV2, SeededRoleV2
from management.role.v2_service import RoleV2Service
from management.exceptions import InvalidFieldError, NotFoundError, RequiredFieldError
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from management.role_binding.serializer import RoleBindingByGroupSerializer, RoleBindingFieldSelection
from management.role_binding.service import CreateBindingRequest, RoleBindingService, API_PRINCIPAL_SOURCE
from management.role_binding.util import parse_resource_type
from management.tenant_mapping.model import TenantMapping
from management.utils import FieldSelectionValidationError
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test


class _ReplicationTracker(RelationReplicator):
    """Records tuples added and removed by replication events for testing.

    Instead of merging into a single set (like InMemoryTuples), this tracks
    the raw add/remove lists so tests can assert the full changeset.
    """

    def __init__(self):
        """Initialize with empty tracking lists."""
        self.tuples_added = []
        self.tuples_removed = []

    def replicate(self, event):
        """Record the tuples from the event."""
        self.tuples_added.extend(event.add)
        self.tuples_removed.extend(event.remove)

    def clear(self):
        """Reset tracking for the next operation."""
        self.tuples_added.clear()
        self.tuples_removed.clear()


class _ReplicationAssertionsMixin:
    """Assertion helpers for testing replication tuple changesets.

    Provides ``assertTuplesAdded`` and ``assertTuplesRemoved`` which
    compare the *full* set of tuples — no more, no fewer.
    """

    tracker: _ReplicationTracker

    @staticmethod
    def _tuple_str(t):
        """Human-readable string for a RelationTuple (for error messages)."""
        subj_rel = f"#{t.subject.relation}" if t.subject.relation else ""
        return (
            f"{t.resource.type.name}:{t.resource.id}"
            f"#{t.relation}"
            f"@{t.subject.subject.type.name}:{t.subject.subject.id}{subj_rel}"
        )

    def _format_tuples(self, tuples):
        """Format a set of tuples as an indented, sorted block."""
        if not tuples:
            return "  (none)"
        return "\n".join(f"  {self._tuple_str(t)}" for t in sorted(tuples, key=self._tuple_str))

    def assertTuplesAdded(self, expected):
        """Assert the exact set of tuples that were added."""
        actual = set(self.tracker.tuples_added)
        expected = set(expected)
        self.assertEqual(
            actual,
            expected,
            f"\nAdded tuples differ."
            f"\nExpected ({len(expected)}):\n{self._format_tuples(expected)}"
            f"\nActual ({len(actual)}):\n{self._format_tuples(actual)}",
        )

    def assertTuplesRemoved(self, expected):
        """Assert the exact set of tuples that were removed."""
        actual = set(self.tracker.tuples_removed)
        expected = set(expected)
        self.assertEqual(
            actual,
            expected,
            f"\nRemoved tuples differ."
            f"\nExpected ({len(expected)}):\n{self._format_tuples(expected)}"
            f"\nActual ({len(actual)}):\n{self._format_tuples(actual)}",
        )


class FieldSelectionTests(TestCase):
    """Tests for RoleBindingFieldSelection dataclass and parsing."""

    def test_parse_returns_none_for_empty_string(self):
        """Test that parse returns None for empty string."""
        result = RoleBindingFieldSelection.parse("")
        self.assertIsNone(result)

    def test_parse_returns_none_for_none(self):
        """Test that parse returns None for None input."""
        result = RoleBindingFieldSelection.parse(None)
        self.assertIsNone(result)

    def test_parse_subject_fields(self):
        """Test parsing subject fields."""
        result = RoleBindingFieldSelection.parse("subject(group.name,group.description)")
        self.assertIsNotNone(result)
        self.assertIn("group.name", result.get_nested("subject"))
        self.assertIn("group.description", result.get_nested("subject"))

    def test_parse_role_fields(self):
        """Test parsing role fields."""
        result = RoleBindingFieldSelection.parse("role(name,id)")
        self.assertIsNotNone(result)
        self.assertIn("name", result.get_nested("role"))
        self.assertIn("id", result.get_nested("role"))

    def test_parse_resource_fields(self):
        """Test parsing resource fields."""
        result = RoleBindingFieldSelection.parse("resource(name,type)")
        self.assertIsNotNone(result)
        self.assertIn("name", result.get_nested("resource"))
        self.assertIn("type", result.get_nested("resource"))

    def test_parse_multiple_objects(self):
        """Test parsing multiple object field selections."""
        result = RoleBindingFieldSelection.parse("subject(group.name),role(name),resource(type)")
        self.assertIsNotNone(result)
        self.assertIn("group.name", result.get_nested("subject"))
        self.assertIn("name", result.get_nested("role"))
        self.assertIn("type", result.get_nested("resource"))

    def test_parse_mixed_nested_object_fields(self):
        """Test parsing multiple nested object fields together."""
        result = RoleBindingFieldSelection.parse("subject(group.name),resource(name,type)")
        self.assertIsNotNone(result)
        self.assertIn("group.name", result.get_nested("subject"))
        self.assertIn("name", result.get_nested("resource"))
        self.assertIn("type", result.get_nested("resource"))

    def test_parse_handles_whitespace(self):
        """Test that parsing handles whitespace correctly."""
        result = RoleBindingFieldSelection.parse(" subject( group.name , group.description ) ")
        self.assertIsNotNone(result)
        self.assertIn("group.name", result.get_nested("subject"))
        self.assertIn("group.description", result.get_nested("subject"))

    def test_split_fields_respects_parentheses(self):
        """Test that field splitting respects parentheses."""
        parts = RoleBindingFieldSelection._split_fields("subject(a,b),role(c)")
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], "subject(a,b)")
        self.assertEqual(parts[1], "role(c)")

    def test_parse_raises_error_for_invalid_subject_field(self):
        """Test that parse raises error for invalid subject field."""
        with self.assertRaises(FieldSelectionValidationError) as context:
            RoleBindingFieldSelection.parse("subject(invalid_field)")
        self.assertIn("invalid_field", str(context.exception))

    def test_parse_raises_error_for_invalid_role_field(self):
        """Test that parse raises error for invalid role field."""
        with self.assertRaises(FieldSelectionValidationError) as context:
            RoleBindingFieldSelection.parse("role(invalid_field)")
        self.assertIn("invalid_field", str(context.exception))

    def test_parse_raises_error_for_invalid_resource_field(self):
        """Test that parse raises error for invalid resource field."""
        with self.assertRaises(FieldSelectionValidationError) as context:
            RoleBindingFieldSelection.parse("resource(invalid_field)")
        self.assertIn("invalid_field", str(context.exception))

    def test_parse_raises_error_for_unknown_object_type(self):
        """Test that parse raises error for unknown object type."""
        with self.assertRaises(FieldSelectionValidationError) as context:
            RoleBindingFieldSelection.parse("unknown(field)")
        self.assertIn("Unknown object type", str(context.exception))

    def test_parse_raises_error_for_invalid_root_field(self):
        """Test that parse raises error for invalid root field."""
        with self.assertRaises(FieldSelectionValidationError) as context:
            RoleBindingFieldSelection.parse("invalid_root_field")
        self.assertIn("Unknown field", str(context.exception))

    def test_parse_user_username_field(self):
        """Test parsing user.username field for user subjects."""
        result = RoleBindingFieldSelection.parse("subject(user.username)")
        self.assertIsNotNone(result)
        self.assertIn("user.username", result.get_nested("subject"))

    def test_parse_user_and_group_fields_together(self):
        """Test parsing both user and group fields (for different subject types)."""
        result = RoleBindingFieldSelection.parse("subject(user.username,group.name)")
        self.assertIsNotNone(result)
        self.assertIn("user.username", result.get_nested("subject"))
        self.assertIn("group.name", result.get_nested("subject"))

    def test_parse_user_field_with_other_objects(self):
        """Test parsing user field with role and resource fields."""
        result = RoleBindingFieldSelection.parse("subject(user.username),role(name),resource(type)")
        self.assertIsNotNone(result)
        self.assertIn("user.username", result.get_nested("subject"))
        self.assertIn("name", result.get_nested("role"))
        self.assertIn("type", result.get_nested("resource"))


class RoleBindingServiceTests(IdentityRequest):
    """Tests for RoleBindingService."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create workspace hierarchy
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=self.root_workspace,
        )
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            description="Test workspace description",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        # Create permission and role
        self.permission = Permission.objects.create(
            permission="app:resource:read",
            tenant=self.tenant,
        )

        self.role = RoleV2.objects.create(
            name="test_role",
            tenant=self.tenant,
        )
        self.role.permissions.add(self.permission)

        # Create group with principal
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

        # Create role binding
        self.binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )

        self.binding_group_entry = RoleBindingGroup.objects.create(
            group=self.group,
            binding=self.binding,
        )

        # Create RoleBindingPrincipal for user-type queries
        self.binding_principal_entry = RoleBindingPrincipal.objects.create(
            principal=self.principal,
            binding=self.binding,
            source=API_PRINCIPAL_SOURCE,
        )

        self.service = RoleBindingService(tenant=self.tenant)

    def tearDown(self):
        """Tear down test data."""
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        self.group.principals.clear()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        super().tearDown()

    def test_get_role_bindings_by_subject_returns_groups(self):
        """Test that get_role_bindings_by_subject returns groups."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 1)
        group = queryset.first()
        self.assertEqual(group.name, "test_group")

    def test_get_role_bindings_by_subject_annotates_principal_count(self):
        """Test that groups are annotated with principal count."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        group = queryset.first()
        self.assertEqual(group.principalCount, 1)

    def test_get_role_bindings_by_subject_filters_by_subject_id(self):
        """Test filtering by subject_id."""
        # Create another group with a binding
        other_group = Group.objects.create(
            name="other_group",
            tenant=self.tenant,
        )
        other_role = RoleV2.objects.create(
            name="other_role",
            tenant=self.tenant,
        )
        other_binding = RoleBinding.objects.create(
            role=other_role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=other_group,
            binding=other_binding,
        )

        # Filter by original group's UUID
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "subject_id": str(self.group.uuid),
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().uuid, self.group.uuid)

        # Cleanup
        RoleBindingGroup.objects.filter(binding=other_binding).delete()
        other_binding.delete()
        other_role.delete()
        other_group.delete()

    def test_get_role_bindings_by_subject_filters_by_subject_type_group(self):
        """Test filtering by subject_type='group' returns results."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "subject_type": "group",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 1)

    def test_get_role_bindings_by_subject_filters_by_user_subject_type(self):
        """Test filtering by subject_type='user' returns users."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "subject_type": "user",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        # Should return the principal (user) that has a RoleBindingPrincipal entry
        self.assertEqual(queryset.count(), 1)
        user = queryset.first()
        self.assertEqual(user.username, "testuser")
        self.assertEqual(user.type, Principal.Types.USER)

    def test_get_role_bindings_by_subject_user_type_returns_only_users(self):
        """Test that user subject type only returns users, not service accounts."""
        # Create a service account in the group
        service_account = Principal.objects.create(
            username="service-account-1",
            tenant=self.tenant,
            type=Principal.Types.SERVICE_ACCOUNT,
        )
        self.group.principals.add(service_account)

        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "subject_type": "user",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        # Should only return the user, not the service account
        self.assertEqual(queryset.count(), 1)
        user = queryset.first()
        self.assertEqual(user.type, Principal.Types.USER)

        # Cleanup
        self.group.principals.remove(service_account)
        service_account.delete()

    def test_get_role_bindings_by_subject_user_type_filters_by_subject_id(self):
        """Test filtering user subject type by subject_id."""
        # Create another user in the group
        other_user = Principal.objects.create(
            username="otheruser",
            tenant=self.tenant,
            type=Principal.Types.USER,
        )
        self.group.principals.add(other_user)

        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "subject_type": "user",
            "subject_id": str(self.principal.uuid),
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        # Should only return the filtered user
        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().uuid, self.principal.uuid)

        # Cleanup
        self.group.principals.remove(other_user)
        other_user.delete()

    def test_get_role_bindings_by_subject_user_type_empty_results(self):
        """Test that non-existent resource returns empty queryset for user type."""
        params = {
            "resource_id": "00000000-0000-0000-0000-000000000000",
            "resource_type": "workspace",
            "subject_type": "user",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 0)

    def test_get_role_bindings_by_subject_unsupported_subject_type_returns_empty(self):
        """Test filtering by unsupported subject_type returns empty."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "subject_type": "service-account",  # Not currently supported
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 0)

    def test_get_role_bindings_by_subject_empty_results(self):
        """Test that non-existent resource returns empty queryset."""
        params = {
            "resource_id": "00000000-0000-0000-0000-000000000000",
            "resource_type": "workspace",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 0)

    def test_get_role_bindings_by_subject_for_tenant(self):
        """Test that get_role_bindings_by_subject works with resource_type=tenant."""
        tenant_resource_id = self.tenant.tenant_resource_id()
        tenant_binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="tenant",
            resource_id=tenant_resource_id,
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=self.group,
            binding=tenant_binding,
        )

        params = {
            "resource_id": tenant_resource_id,
            "resource_type": "tenant",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "test_group")

        RoleBindingGroup.objects.filter(binding=tenant_binding).delete()
        tenant_binding.delete()

    def test_get_resource_name_for_workspace(self):
        """Test getting resource name for workspace."""
        name = self.service.get_resource_name(str(self.workspace.id), "workspace")
        self.assertEqual(name, "Test Workspace")

    def test_get_resource_name_for_nonexistent_workspace(self):
        """Test getting resource name for non-existent workspace."""
        name = self.service.get_resource_name("00000000-0000-0000-0000-000000000000", "workspace")
        self.assertIsNone(name)

    def test_get_resource_name_for_unknown_type(self):
        """Test getting resource name for unknown resource type."""
        name = self.service.get_resource_name("some-id", "unknown_type")
        self.assertIsNone(name)

    def test_get_resource_name_for_tenant(self):
        """Test getting resource name for tenant."""
        tenant_resource_id = self.tenant.tenant_resource_id()
        name = self.service.get_resource_name(tenant_resource_id, "tenant")
        self.assertEqual(name, self.tenant.tenant_name)

    def test_get_resource_name_for_tenant_mismatch_returns_none(self):
        """Test getting resource name for tenant with mismatched resource_id returns None."""
        name = self.service.get_resource_name("localhost/other-org-12345", "tenant")
        self.assertIsNone(name)

    def test_build_context(self):
        """Test building serializer context."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }
        context = self.service.build_context(params)

        self.assertEqual(context["resource_id"], str(self.workspace.id))
        self.assertEqual(context["resource_type"], "workspace")
        self.assertEqual(context["resource_name"], "Test Workspace")
        self.assertIsNone(context["field_selection"])

    def test_build_context_with_fields(self):
        """Test building context with field selection (pre-parsed by input serializer)."""
        field_selection = RoleBindingFieldSelection.parse("subject(group.name)")
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "fields": field_selection,
        }
        context = self.service.build_context(params)

        self.assertIsNotNone(context["field_selection"])
        self.assertIn("group.name", context["field_selection"].get_nested("subject"))

    def test_build_context_with_subject_type(self):
        """Test that subject_type is included in context."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "subject_type": "user",
        }
        context = self.service.build_context(params)

        self.assertEqual(context["subject_type"], "user")

    def test_build_context_without_subject_type(self):
        """Test that subject_type is None when not provided."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }
        context = self.service.build_context(params)

        self.assertIsNone(context["subject_type"])

    def test_parse_resource_type_with_namespace(self):
        """Test parsing resource type with namespace prefix."""
        ns, name = parse_resource_type("rbac/workspace")
        self.assertEqual(ns, "rbac")
        self.assertEqual(name, "workspace")

    def test_parse_resource_type_without_namespace(self):
        """Test parsing resource type without namespace defaults to rbac."""
        ns, name = parse_resource_type("workspace")
        self.assertEqual(ns, "rbac")
        self.assertEqual(name, "workspace")

    def test_parse_resource_type_with_custom_namespace(self):
        """Test parsing resource type with custom namespace."""
        ns, name = parse_resource_type("custom/resource")
        self.assertEqual(ns, "custom")
        self.assertEqual(name, "resource")

    def test_parse_resource_type_with_multiple_slashes(self):
        """Test parsing resource type with multiple slashes only splits on first."""
        ns, name = parse_resource_type("ns/path/to/resource")
        self.assertEqual(ns, "ns")
        self.assertEqual(name, "path/to/resource")

    def test_get_role_bindings_by_subject_with_exclude_sources_indirect(self):
        """Test that exclude_sources=indirect returns only direct bindings."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "exclude_sources": "indirect",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 1)
        group = queryset.first()
        self.assertEqual(group.name, "test_group")

    def test_get_role_bindings_by_subject_with_exclude_sources_default(self):
        """Test that exclude_sources defaults to 'none' (falls back to direct without Relations API)."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 1)

    def test_build_base_queryset_with_binding_uuids_includes_inherited(self):
        """Test that _build_base_queryset includes inherited bindings when UUIDs provided."""
        # Create a second group with binding on a different resource (parent workspace)
        parent_group = Group.objects.create(
            name="parent_group",
            tenant=self.tenant,
        )
        parent_binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),  # Parent workspace
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=parent_group,
            binding=parent_binding,
        )

        # Query for child workspace but include parent binding UUID
        binding_uuids = [str(parent_binding.uuid)]
        queryset = self.service._build_base_queryset(str(self.workspace.id), "workspace", binding_uuids)

        # Should include both direct binding group and inherited binding group
        self.assertEqual(queryset.count(), 2)
        group_names = set(queryset.values_list("name", flat=True))
        self.assertIn("test_group", group_names)
        self.assertIn("parent_group", group_names)

        # Cleanup
        RoleBindingGroup.objects.filter(binding=parent_binding).delete()
        parent_binding.delete()
        parent_group.delete()

    def test_build_base_queryset_without_binding_uuids_excludes_inherited(self):
        """Test that _build_base_queryset excludes inherited bindings when no UUIDs."""
        # Create a second group with binding on a different resource (parent workspace)
        parent_group = Group.objects.create(
            name="parent_group",
            tenant=self.tenant,
        )
        parent_binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),  # Parent workspace
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=parent_group,
            binding=parent_binding,
        )

        # Query for child workspace without inherited UUIDs
        queryset = self.service._build_base_queryset(str(self.workspace.id), "workspace", None)

        # Should only include direct binding group
        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "test_group")

        # Cleanup
        RoleBindingGroup.objects.filter(binding=parent_binding).delete()
        parent_binding.delete()
        parent_group.delete()

    def test_get_role_bindings_works_without_tenant_mapping(self):
        """Test that role binding queries work when tenant has no TenantMapping.

        This verifies the lazy default binding creation gracefully skips
        when there's no TenantMapping, allowing existing functionality to work.
        """
        # Verify no TenantMapping exists for this tenant
        self.assertFalse(TenantMapping.objects.filter(tenant=self.tenant).exists())

        # Query should still work and return results
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        # Should return our manually created group with binding
        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "test_group")

    def test_parse_resource_type_with_namespace(self):
        """Test parsing resource type with namespace prefix."""
        ns, name = parse_resource_type("rbac/workspace")
        self.assertEqual(ns, "rbac")
        self.assertEqual(name, "workspace")

    def test_parse_resource_type_without_namespace(self):
        """Test parsing resource type without namespace defaults to rbac."""
        ns, name = parse_resource_type("workspace")
        self.assertEqual(ns, "rbac")
        self.assertEqual(name, "workspace")

    def test_parse_resource_type_with_custom_namespace(self):
        """Test parsing resource type with custom namespace."""
        ns, name = parse_resource_type("custom/resource")
        self.assertEqual(ns, "custom")
        self.assertEqual(name, "resource")

    def test_parse_resource_type_with_multiple_slashes(self):
        """Test parsing resource type with multiple slashes only splits on first."""
        ns, name = parse_resource_type("ns/path/to/resource")
        self.assertEqual(ns, "ns")
        self.assertEqual(name, "path/to/resource")

    def test_get_role_bindings_by_subject_with_exclude_sources_indirect(self):
        """Test that exclude_sources=indirect returns only direct bindings."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "exclude_sources": "indirect",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 1)
        group = queryset.first()
        self.assertEqual(group.name, "test_group")

    def test_get_role_bindings_by_subject_with_exclude_sources_default(self):
        """Test that exclude_sources defaults to 'none' (falls back to direct without Relations API)."""
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }
        queryset = self.service.get_role_bindings_by_subject(params)

        self.assertEqual(queryset.count(), 1)

    def test_build_base_queryset_with_binding_uuids_includes_inherited(self):
        """Test that _build_base_queryset includes inherited bindings when UUIDs provided."""
        # Create a second group with binding on a different resource (parent workspace)
        parent_group = Group.objects.create(
            name="parent_group",
            tenant=self.tenant,
        )
        parent_binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),  # Parent workspace
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=parent_group,
            binding=parent_binding,
        )

        # Query for child workspace but include parent binding UUID
        binding_uuids = [str(parent_binding.uuid)]
        queryset = self.service._build_base_queryset(str(self.workspace.id), "workspace", binding_uuids)

        # Should include both direct binding group and inherited binding group
        self.assertEqual(queryset.count(), 2)
        group_names = set(queryset.values_list("name", flat=True))
        self.assertIn("test_group", group_names)
        self.assertIn("parent_group", group_names)

        # Cleanup
        RoleBindingGroup.objects.filter(binding=parent_binding).delete()
        parent_binding.delete()
        parent_group.delete()

    def test_build_base_queryset_without_binding_uuids_excludes_inherited(self):
        """Test that _build_base_queryset excludes inherited bindings when no UUIDs."""
        # Create a second group with binding on a different resource (parent workspace)
        parent_group = Group.objects.create(
            name="parent_group",
            tenant=self.tenant,
        )
        parent_binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id=str(self.default_workspace.id),  # Parent workspace
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=parent_group,
            binding=parent_binding,
        )

        # Query for child workspace without inherited UUIDs
        queryset = self.service._build_base_queryset(str(self.workspace.id), "workspace", None)

        # Should only include direct binding group
        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.first().name, "test_group")

        # Cleanup
        RoleBindingGroup.objects.filter(binding=parent_binding).delete()
        parent_binding.delete()
        parent_group.delete()

    def test_exclude_principal_different_source(self):
        """Test that a principal with only entries from a different source is not returned."""

        def subjects_for_service(service: RoleBindingService):
            return service.get_role_bindings_by_subject(
                {
                    "resource_type": self.binding.resource_type,
                    "resource_id": self.binding.resource_id,
                    "subject_type": "user",
                }
            )

        self.assertCountEqual([self.principal], subjects_for_service(self.service))

        self.binding_principal_entry.source = "another source"
        self.binding_principal_entry.save()

        self.assertCountEqual([], subjects_for_service(self.service))

        self.assertCountEqual(
            [self.principal],
            subjects_for_service(RoleBindingService(tenant=self.tenant, principal_source="another source")),
        )

    def test_exclude_entries_different_source(self):
        """Test that principal.filtered_bindings includes only entries with the correct source."""

        def check_entries(service: RoleBindingService, entries: list[RoleBindingPrincipal]):
            principals = service.get_role_bindings_by_subject(
                {
                    "resource_type": self.binding.resource_type,
                    "resource_id": self.binding.resource_id,
                    "subject_type": "user",
                }
            )

            self.assertCountEqual([self.principal], principals)
            self.assertCountEqual(entries, principals[0].filtered_bindings)

        check_entries(self.service, [self.binding_principal_entry])

        another_entry = RoleBindingPrincipal.objects.create(
            binding=self.binding, principal=self.principal, source="another source"
        )

        check_entries(self.service, [self.binding_principal_entry])
        check_entries(RoleBindingService(tenant=self.tenant, principal_source="another source"), [another_entry])

    def test_exclude_entries_external_tenant(self):
        """Test that external subjects are retrieved if and only if requested."""
        exclude_service = RoleBindingService(tenant=self.tenant)
        include_service = RoleBindingService(tenant=self.tenant, allow_external_subjects=True)

        ext_tenant = Tenant.objects.create(tenant_name="another tenant", org_id="789")

        def get_groups(service: RoleBindingService):
            return service.get_role_bindings_by_subject(
                {
                    "resource_type": self.binding.resource_type,
                    "resource_id": self.binding.resource_id,
                    "subject_type": "group",
                }
            )

        def get_principals(service: RoleBindingService):
            return service.get_role_bindings_by_subject(
                {
                    "resource_type": self.binding.resource_type,
                    "resource_id": self.binding.resource_id,
                    "subject_type": "user",
                }
            )

        self.assertEqual(1, len(get_groups(exclude_service)))
        self.assertEqual(1, len(get_principals(exclude_service)))

        self.assertEqual(1, len(get_groups(include_service)))
        self.assertEqual(1, len(get_principals(include_service)))

        self.group.tenant = ext_tenant
        self.group.save()

        self.assertEqual(0, len(get_groups(exclude_service)))
        self.assertEqual(1, len(get_groups(include_service)))

        self.principal.tenant = ext_tenant
        self.principal.save()

        self.assertEqual(0, len(get_principals(exclude_service)))
        self.assertEqual(1, len(get_principals(include_service)))

    def test_by_subject_includes_platform_default_groups(self):
        """Test that platform default groups from the public tenant are included in by-subject results.

        Platform default groups (e.g. "Default access") belong to the public tenant but should
        still appear when querying by-subject for a specific tenant's resources.
        Regular (non-default) groups from other tenants must still be excluded.
        """
        public_tenant = Tenant.objects.create(tenant_name="public", org_id="public")

        platform_group = Group.objects.create(
            name="Default access",
            tenant=public_tenant,
            platform_default=True,
            system=True,
        )
        admin_group = Group.objects.create(
            name="Default admin access",
            tenant=public_tenant,
            admin_default=True,
            system=True,
        )
        # A regular group on the public tenant — should NOT be included
        regular_public_group = Group.objects.create(
            name="Regular public group",
            tenant=public_tenant,
            platform_default=False,
            admin_default=False,
            system=False,
        )

        # Create separate roles for each binding (unique constraint: role+resource+tenant)
        platform_role = RoleV2.objects.create(name="platform_role", tenant=self.tenant)
        admin_role = RoleV2.objects.create(name="admin_role", tenant=self.tenant)
        regular_role = RoleV2.objects.create(name="regular_role", tenant=self.tenant)

        # Create role bindings owned by the user's tenant, linked to public tenant groups
        platform_binding = RoleBinding.objects.create(
            role=platform_role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(group=platform_group, binding=platform_binding)

        admin_binding = RoleBinding.objects.create(
            role=admin_role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(group=admin_group, binding=admin_binding)

        regular_binding = RoleBinding.objects.create(
            role=regular_role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(group=regular_public_group, binding=regular_binding)

        groups = self.service.get_role_bindings_by_subject(
            {
                "resource_type": "workspace",
                "resource_id": str(self.workspace.id),
                "subject_type": "group",
            }
        )

        group_names = {g.name for g in groups}
        # Should include the tenant's own group AND both platform default groups
        self.assertIn("test_group", group_names)
        self.assertIn("Default access", group_names)
        self.assertIn("Default admin access", group_names)
        # Regular non-default group from public tenant must be excluded
        self.assertNotIn("Regular public group", group_names)
        self.assertEqual(3, len(groups))


class RoleBindingSerializerTests(IdentityRequest):
    """Tests for RoleBindingByGroupSerializer."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create workspace
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=self.root_workspace,
        )
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            description="Test workspace description",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        # Create role
        self.role = RoleV2.objects.create(
            name="test_role",
            description="Test role description",
            tenant=self.tenant,
        )

        # Create group with principal
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

        # Create role binding
        self.binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(
            group=self.group,
            binding=self.binding,
        )

        # Get annotated group for serializer
        self.service = RoleBindingService(tenant=self.tenant)
        params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }
        queryset = self.service.get_role_bindings_by_subject(params)
        self.annotated_group = queryset.first()

        # Base context for serializer
        self.context = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
            "resource_name": "Test Workspace",
            "field_selection": None,
        }

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        self.group.principals.clear()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        super().tearDown()

    def test_default_response_includes_all_spec_fields(self):
        """Test that default response includes basic required fields.

        Default behavior returns only:
        - subject (id, type)
        - roles (id only)
        - resource (id only)
        """
        serializer = RoleBindingByGroupSerializer(self.annotated_group, context=self.context)
        data = serializer.data
        self.assertIn("subject", data)
        self.assertIn("roles", data)
        self.assertIn("resource", data)

    def test_default_subject_structure_matches_spec(self):
        """Test that subject structure matches default spec.

        Default behavior returns only id and type (no group details).
        """
        serializer = RoleBindingByGroupSerializer(self.annotated_group, context=self.context)
        data = serializer.data

        subject = data["subject"]
        self.assertIn("id", subject)
        self.assertIn("type", subject)
        self.assertEqual(subject["type"], "group")
        # Default behavior: no group details
        self.assertNotIn("group", subject)

    def test_default_roles_structure_matches_spec(self):
        """Test that roles structure matches default spec.

        Default behavior returns only role id.
        """
        serializer = RoleBindingByGroupSerializer(self.annotated_group, context=self.context)
        data = serializer.data

        roles = data["roles"]
        self.assertEqual(len(roles), 1)

        role = roles[0]
        self.assertIn("id", role)
        # Default behavior: no name included
        self.assertNotIn("name", role)
        self.assertNotIn("description", role)
        self.assertNotIn("type", role)
        self.assertNotIn("created", role)
        self.assertNotIn("modified", role)

    def test_default_resource_structure_matches_spec(self):
        """Test that resource structure matches default spec.

        Default behavior returns only resource id.
        """
        serializer = RoleBindingByGroupSerializer(self.annotated_group, context=self.context)
        data = serializer.data

        resource = data["resource"]
        self.assertIn("id", resource)
        # Default behavior: no name or type included
        self.assertNotIn("name", resource)
        self.assertNotIn("type", resource)

    def test_field_selection_filters_subject_fields(self):
        """Test that field selection filters subject group fields.

        subject.type and subject.id are always included. Other fields require explicit request.
        """
        field_selection = RoleBindingFieldSelection.parse("subject(group.name)")
        context = {**self.context, "field_selection": field_selection}

        serializer = RoleBindingByGroupSerializer(self.annotated_group, context=context)
        data = serializer.data

        subject = data["subject"]
        # type and id are always included
        self.assertIn("type", subject)
        self.assertIn("id", subject)

        group = subject["group"]
        self.assertIn("name", group)
        self.assertNotIn("description", group)
        self.assertNotIn("user_count", group)

    def test_field_selection_filters_role_fields(self):
        """Test that field selection filters role fields.

        id is always included, plus explicitly requested fields.
        """
        field_selection = RoleBindingFieldSelection.parse("role(name)")
        context = {**self.context, "field_selection": field_selection}

        serializer = RoleBindingByGroupSerializer(self.annotated_group, context=context)
        data = serializer.data

        role = data["roles"][0]
        # id is always included
        self.assertIn("id", role)
        self.assertIn("name", role)

    def test_field_selection_filters_resource_fields(self):
        """Test that field selection filters resource fields.

        id is always included, plus explicitly requested fields.
        """
        field_selection = RoleBindingFieldSelection.parse("resource(type)")
        context = {**self.context, "field_selection": field_selection}

        serializer = RoleBindingByGroupSerializer(self.annotated_group, context=context)
        data = serializer.data

        resource = data["resource"]
        # id is always included
        self.assertIn("id", resource)
        self.assertIn("type", resource)
        self.assertNotIn("name", resource)

    def test_combined_field_selection(self):
        """Test combined field selection across multiple objects."""
        field_selection = RoleBindingFieldSelection.parse("subject(group.name),role(name),resource(name,type)")
        context = {**self.context, "field_selection": field_selection}

        serializer = RoleBindingByGroupSerializer(self.annotated_group, context=context)
        data = serializer.data

        # Check subject
        self.assertIn("name", data["subject"]["group"])
        self.assertNotIn("description", data["subject"]["group"])

        # Check roles
        self.assertIn("name", data["roles"][0])

        # Check resource
        self.assertIn("name", data["resource"])
        self.assertIn("type", data["resource"])


@override_settings(ATOMIC_RETRY_DISABLED=True)
class BatchCreateRoleBindingTests(IdentityRequest):
    """Tests for RoleBindingService.batch_create method."""

    def setUp(self):
        """Set up test data using services."""
        super().setUp()

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)

        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            description="Test workspace description",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        self.permission1 = Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        self.permission2 = Permission.objects.create(permission="app:resource:write", tenant=self.tenant)

        self.role_service = RoleV2Service()
        self.role1 = self.role_service.create(
            name="role1",
            description="Test role 1",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "read"}],
            tenant=self.tenant,
        )
        self.role2 = self.role_service.create(
            name="role2",
            description="Test role 2",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "write"}],
            tenant=self.tenant,
        )

        self.group = Group.objects.create(
            name="test_group",
            description="Test group description",
            tenant=self.tenant,
        )
        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            user_id="testuser",
            type=Principal.Types.USER,
        )

        self.service = RoleBindingService(tenant=self.tenant)

    def tearDown(self):
        """Tear down test data."""
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        super().tearDown()

    def _make_request(self, role, subject_type, subject_id, resource_id=None):
        """Build a CreateBindingRequest for the workspace."""
        return CreateBindingRequest(
            role_id=str(role.uuid),
            resource_type="workspace",
            resource_id=resource_id or str(self.workspace.id),
            subject_type=subject_type,
            subject_id=str(subject_id),
        )

    def test_batch_create_empty(self):
        with self.assertRaises(RequiredFieldError) as error:
            self.service.batch_create([])

        self.assertEqual(error.exception.field_name, "requests")

    def test_batch_create_for_group(self):
        """Create a single binding with a group subject."""
        results = self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
            ]
        )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["role"], self.role1)
        self.assertEqual(result["subject"], self.group)
        self.assertEqual(result["subject_type"], "group")
        self.assertEqual(result["resource_type"], "workspace")
        self.assertEqual(result["resource_id"], str(self.workspace.id))
        self.assertEqual(result["resource_name"], "Test Workspace")

    def test_batch_create_for_principal(self):
        """Create a single binding with a user subject."""
        results = self.service.batch_create(
            [
                self._make_request(self.role1, "user", self.principal.uuid),
            ]
        )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["role"], self.role1)
        self.assertEqual(result["subject"], self.principal)
        self.assertEqual(result["subject_type"], "user")
        self.assertEqual(result["resource_type"], "workspace")
        self.assertEqual(result["resource_id"], str(self.workspace.id))
        self.assertEqual(result["resource_name"], "Test Workspace")

        self.assertTrue(is_v2_write_activated(self.tenant))

    def test_batch_create_multiple_bindings(self):
        """Create two bindings in one call with different roles and subject types."""
        results = self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
                self._make_request(self.role2, "user", self.principal.uuid),
            ]
        )

        self.assertEqual(len(results), 2)

        self.assertEqual(results[0]["role"], self.role1)
        self.assertEqual(results[0]["subject"], self.group)
        self.assertEqual(results[0]["subject_type"], "group")

        self.assertEqual(results[1]["role"], self.role2)
        self.assertEqual(results[1]["subject"], self.principal)
        self.assertEqual(results[1]["subject_type"], "user")

        self.assertTrue(is_v2_write_activated(self.tenant))

    def test_batch_create_raises_roles_not_found(self):
        """Pass a non-existent role UUID."""
        fake_role_id = str(uuid.uuid4())
        with self.assertRaises(InvalidFieldError) as ctx:
            self.service.batch_create(
                [
                    CreateBindingRequest(
                        role_id=fake_role_id,
                        resource_type="workspace",
                        resource_id=str(self.workspace.id),
                        subject_type="group",
                        subject_id=str(self.group.uuid),
                    ),
                ]
            )

        self.assertEqual(ctx.exception.field, "roles")
        self.assertIn(fake_role_id, str(ctx.exception))

        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_batch_create_raises_subjects_not_found_group(self):
        """Pass a non-existent group UUID."""
        fake_group_id = str(uuid.uuid4())
        with self.assertRaises(NotFoundError) as ctx:
            self.service.batch_create(
                [
                    self._make_request(self.role1, "group", fake_group_id),
                ]
            )

        self.assertEqual(ctx.exception.resource_type, "group")
        self.assertIn(fake_group_id, str(ctx.exception))

        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_batch_create_raises_subjects_not_found_user(self):
        """Pass a non-existent principal UUID."""
        fake_user_id = str(uuid.uuid4())
        with self.assertRaises(NotFoundError) as ctx:
            self.service.batch_create(
                [
                    self._make_request(self.role1, "user", fake_user_id),
                ]
            )

        self.assertEqual(ctx.exception.resource_type, "user")
        self.assertIn(fake_user_id, str(ctx.exception))

        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_batch_create_idempotent_same_subject(self):
        """Granting the same role to the same subject twice is idempotent."""
        self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
            ]
        )
        self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
            ]
        )

        binding = RoleBinding.objects.get(
            role=self.role1,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        self.assertEqual(RoleBindingGroup.objects.filter(binding=binding, group=self.group).count(), 1)

    def test_batch_create_shared_binding_group_and_user(self):
        """Same role+resource reuses one RoleBinding with both subjects linked."""
        self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
                self._make_request(self.role1, "user", self.principal.uuid),
            ]
        )

        bindings = RoleBinding.objects.filter(
            role=self.role1,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        self.assertEqual(bindings.count(), 1)

        binding = bindings.first()
        self.assertTrue(RoleBindingGroup.objects.filter(binding=binding, group=self.group).exists())
        self.assertTrue(RoleBindingPrincipal.objects.filter(binding=binding, principal=self.principal).exists())

    def test_batch_create_reuses_existing_binding(self):
        """A binding from a prior batch is reused when adding a new subject."""
        self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
            ]
        )
        binding_id_before = RoleBinding.objects.get(
            role=self.role1,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
        ).id

        self.service.batch_create(
            [
                self._make_request(self.role1, "user", self.principal.uuid),
            ]
        )

        binding = RoleBinding.objects.get(
            role=self.role1,
            resource_type="workspace",
            resource_id=str(self.workspace.id),
        )
        self.assertEqual(binding.id, binding_id_before)
        self.assertTrue(RoleBindingGroup.objects.filter(binding=binding, group=self.group).exists())
        self.assertTrue(RoleBindingPrincipal.objects.filter(binding=binding, principal=self.principal).exists())

    def test_batch_create_rejects_platform_roles(self):
        """Platform roles are excluded by the assignable filter and reported as not found."""
        seeded = SeededRoleV2.objects.create(name="seeded_child", tenant=self.tenant)
        platform = PlatformRoleV2.objects.create(name="platform_role", tenant=self.tenant)
        platform.children.add(seeded)

        with self.assertRaises(InvalidFieldError) as ctx:
            self.service.batch_create(
                [
                    self._make_request(platform, "group", self.group.uuid),
                ]
            )

        self.assertEqual(ctx.exception.field, "roles")
        self.assertIn(str(platform.uuid), str(ctx.exception))

        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_batch_create_fails_fast_when_one_role_missing(self):
        """Batch with valid role1 and non-existent role2 fails before any DB writes."""
        fake_role_id = str(uuid.uuid4())
        with self.assertRaises(InvalidFieldError) as ctx:
            self.service.batch_create(
                [
                    self._make_request(self.role1, "group", self.group.uuid),
                    CreateBindingRequest(
                        role_id=fake_role_id,
                        resource_type="workspace",
                        resource_id=str(self.workspace.id),
                        subject_type="group",
                        subject_id=str(self.group.uuid),
                    ),
                ]
            )

        self.assertEqual(ctx.exception.field, "roles")
        self.assertIn(fake_role_id, str(ctx.exception))
        self.assertFalse(
            RoleBinding.objects.filter(
                role=self.role1, resource_id=str(self.workspace.id), resource_type="workspace"
            ).exists()
        )

        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_batch_create_fails_fast_when_one_subject_missing(self):
        """Batch with valid group and non-existent group fails before any DB writes."""
        fake_group_id = str(uuid.uuid4())
        with self.assertRaises(NotFoundError) as ctx:
            self.service.batch_create(
                [
                    self._make_request(self.role1, "group", self.group.uuid),
                    self._make_request(self.role2, "group", fake_group_id),
                ]
            )

        self.assertEqual(ctx.exception.resource_type, "group")
        self.assertIn(fake_group_id, str(ctx.exception))
        self.assertFalse(
            RoleBinding.objects.filter(resource_id=str(self.workspace.id), resource_type="workspace").exists()
        )

        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_batch_create_rejects_user_without_user_id(self):
        """A principal without user_id is rejected."""
        unsynced = Principal.objects.create(
            username="unsynced_user",
            tenant=self.tenant,
            user_id=None,
            type=Principal.Types.USER,
        )

        with self.assertRaises(InvalidFieldError):
            self.service.batch_create([self._make_request(self.role1, "user", unsynced.uuid)])

        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_batch_create_shared_binding_two_groups(self):
        """Same role+resource with two different groups shares one RoleBinding."""
        group2 = Group.objects.create(name="group2", tenant=self.tenant)

        self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
                self._make_request(self.role1, "group", group2.uuid),
            ]
        )

        bindings = RoleBinding.objects.filter(
            role=self.role1, resource_type="workspace", resource_id=str(self.workspace.id)
        )
        self.assertEqual(bindings.count(), 1)

        binding = bindings.first()
        self.assertTrue(RoleBindingGroup.objects.filter(binding=binding, group=self.group).exists())
        self.assertTrue(RoleBindingGroup.objects.filter(binding=binding, group=group2).exists())

    def test_batch_create_cross_resource_isolation(self):
        """Bindings on different resources are independent."""
        ws2 = Workspace.objects.create(
            name="Other Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid, resource_id=str(self.workspace.id)),
                self._make_request(self.role1, "group", self.group.uuid, resource_id=str(ws2.id)),
            ]
        )

        ws1_bindings = RoleBinding.objects.filter(resource_id=str(self.workspace.id), resource_type="workspace")
        ws2_bindings = RoleBinding.objects.filter(resource_id=str(ws2.id), resource_type="workspace")

        self.assertEqual(ws1_bindings.count(), 1)
        self.assertEqual(ws2_bindings.count(), 1)
        self.assertNotEqual(ws1_bindings.first().id, ws2_bindings.first().id)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch.object(OutboxReplicator, "replicate")
    def test_batch_create_replicates_to_kessel(self, mock_replicate):
        """Test that batch_create accurately fires a replication event."""
        self.service = RoleBindingService(tenant=self.tenant)
        self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
            ]
        )

        mock_replicate.assert_called_once()
        event = mock_replicate.call_args[0][0]

        self.assertEqual(event.event_type, ReplicationEventType.BATCH_CREATE_ROLE_BINDING)
        self.assertEqual(event.event_info["org_id"], str(self.tenant.org_id))
        self.assertGreaterEqual(len(event.add), 1)

    def test_batch_create_max_items_limit(self):
        """Test creating 100 bindings at once handles max bounds successfully."""
        users = [
            Principal.objects.create(
                username=f"testuser_{i}",
                tenant=self.tenant,
                user_id=f"testuser_{i}",
                type=Principal.Types.USER,
            )
            for i in range(100)
        ]

        requests = [self._make_request(self.role1, "user", user.uuid) for user in users]

        results = self.service.batch_create(requests)
        self.assertEqual(len(results), 100)

        bindings = RoleBinding.objects.filter(
            role=self.role1, resource_type="workspace", resource_id=str(self.workspace.id)
        )
        self.assertEqual(bindings.count(), 1)
        self.assertEqual(RoleBindingPrincipal.objects.filter(binding=bindings.first()).count(), 100)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_replication_tuples_for_new_group_binding(self):
        """New group binding emits role, resource, and subject tuples."""
        store = InMemoryTuples()
        service = RoleBindingService(tenant=self.tenant, replicator=InMemoryRelationReplicator(store))

        service.batch_create([self._make_request(self.role1, "group", self.group.uuid)])

        self.assertEqual(len(store), 3)

        binding = RoleBinding.objects.get(role=self.role1, resource_id=str(self.workspace.id))
        binding_uuid = str(binding.uuid)

        role_tuples = store.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_uuid),
                relation("role"),
                subject("rbac", "role", self.role1.uuid),
            )
        )
        self.assertEqual(len(role_tuples), 1)

        resource_tuples = store.find_tuples(
            all_of(
                resource("rbac", "workspace", str(self.workspace.id)),
                relation("binding"),
                subject("rbac", "role_binding", binding_uuid),
            )
        )
        self.assertEqual(len(resource_tuples), 1)

        subject_tuples = store.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_uuid),
                relation("subject"),
                subject("rbac", "group", self.group.uuid, relation="member"),
            )
        )
        self.assertEqual(len(subject_tuples), 1)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_replication_tuples_for_new_user_binding(self):
        """New user binding emits role, resource, and principal subject tuples."""
        store = InMemoryTuples()
        service = RoleBindingService(tenant=self.tenant, replicator=InMemoryRelationReplicator(store))

        service.batch_create([self._make_request(self.role1, "user", self.principal.uuid)])

        self.assertEqual(len(store), 3)

        binding = RoleBinding.objects.get(role=self.role1, resource_id=str(self.workspace.id))
        self.assertCountEqual([API_PRINCIPAL_SOURCE], [p.source for p in binding.principal_entries.all()])

        binding_uuid = str(binding.uuid)
        principal_resource_id = PrincipalModel.user_id_to_principal_resource_id(self.principal.user_id)

        subject_tuples = store.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_uuid),
                relation("subject"),
                subject("rbac", "principal", principal_resource_id),
            )
        )
        self.assertEqual(len(subject_tuples), 1)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_replication_skips_binding_tuples_for_reused_binding(self):
        """Adding a subject to an existing binding only emits the subject tuple."""
        store = InMemoryTuples()
        service = RoleBindingService(tenant=self.tenant, replicator=InMemoryRelationReplicator(store))

        service.batch_create([self._make_request(self.role1, "group", self.group.uuid)])
        self.assertEqual(len(store), 3)

        group2 = Group.objects.create(name="group2", tenant=self.tenant)
        store_second = InMemoryTuples()
        service_second = RoleBindingService(tenant=self.tenant, replicator=InMemoryRelationReplicator(store_second))

        service_second.batch_create([self._make_request(self.role1, "group", group2.uuid)])

        self.assertEqual(len(store_second), 1, "Reused binding should only emit the new subject tuple")

        binding = RoleBinding.objects.get(role=self.role1, resource_id=str(self.workspace.id))
        subject_tuples = store_second.find_tuples(
            all_of(
                resource("rbac", "role_binding", str(binding.uuid)),
                relation("subject"),
                subject("rbac", "group", group2.uuid, relation="member"),
            )
        )
        self.assertEqual(len(subject_tuples), 1)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=False)
    def test_replication_disabled_uses_noop(self):
        """With replication disabled, no tuples are written."""
        store = InMemoryTuples()
        service = RoleBindingService(tenant=self.tenant, replicator=InMemoryRelationReplicator(store))

        service.batch_create([self._make_request(self.role1, "group", self.group.uuid)])

        self.assertEqual(len(store), 0)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_replication_multiple_subjects_same_binding(self):
        """Two subjects on the same role+resource emit 2 binding + 2 subject tuples."""
        store = InMemoryTuples()
        service = RoleBindingService(tenant=self.tenant, replicator=InMemoryRelationReplicator(store))

        service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
                self._make_request(self.role1, "user", self.principal.uuid),
            ]
        )

        self.assertEqual(len(store), 4, "Expected 2 binding-level + 2 subject tuples")

        binding = RoleBinding.objects.get(role=self.role1, resource_id=str(self.workspace.id))
        binding_uuid = str(binding.uuid)

        role_tuples = store.find_tuples(all_of(resource("rbac", "role_binding", binding_uuid), relation("role")))
        self.assertEqual(len(role_tuples), 1)

        resource_tuples = store.find_tuples(
            all_of(resource("rbac", "workspace", str(self.workspace.id)), relation("binding"))
        )
        self.assertEqual(len(resource_tuples), 1)

        group_subject = store.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_uuid),
                relation("subject"),
                subject("rbac", "group", self.group.uuid, relation="member"),
            )
        )
        self.assertEqual(len(group_subject), 1)

        principal_resource_id = PrincipalModel.user_id_to_principal_resource_id(self.principal.user_id)
        user_subject = store.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_uuid),
                relation("subject"),
                subject("rbac", "principal", principal_resource_id),
            )
        )
        self.assertEqual(len(user_subject), 1)

    def test_batch_create_same_subject_different_roles(self):
        """Same resource+subject with two different roles creates two bindings."""
        results = self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
                self._make_request(self.role2, "group", self.group.uuid),
            ]
        )

        self.assertEqual(len(results), 2)

        bindings = RoleBinding.objects.filter(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        self.assertEqual(bindings.count(), 2)

        roles = set(bindings.values_list("role_id", flat=True))
        self.assertEqual(roles, {self.role1.id, self.role2.id})

        for binding in bindings:
            self.assertTrue(RoleBindingGroup.objects.filter(binding=binding, group=self.group).exists())

    def test_batch_create_different_resources(self):
        """Bindings on different resources in a single batch are independent."""
        ws2 = Workspace.objects.create(
            name="Second Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        results = self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid, resource_id=str(self.workspace.id)),
                self._make_request(self.role1, "group", self.group.uuid, resource_id=str(ws2.id)),
            ]
        )

        self.assertEqual(len(results), 2)

        ws1_bindings = RoleBinding.objects.filter(resource_id=str(self.workspace.id), resource_type="workspace")
        ws2_bindings = RoleBinding.objects.filter(resource_id=str(ws2.id), resource_type="workspace")
        self.assertEqual(ws1_bindings.count(), 1)
        self.assertEqual(ws2_bindings.count(), 1)
        self.assertNotEqual(ws1_bindings.first().id, ws2_bindings.first().id)

    def test_batch_create_partial_overlap(self):
        """Existing binding is a no-op while new binding in the same batch is created."""
        self.service.batch_create([self._make_request(self.role1, "group", self.group.uuid)])
        binding_before = RoleBinding.objects.get(
            role=self.role1, resource_type="workspace", resource_id=str(self.workspace.id)
        )

        self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
                self._make_request(self.role2, "group", self.group.uuid),
            ]
        )

        binding_role1 = RoleBinding.objects.get(
            role=self.role1, resource_type="workspace", resource_id=str(self.workspace.id)
        )
        self.assertEqual(binding_role1.id, binding_before.id)

        self.assertTrue(
            RoleBinding.objects.filter(
                role=self.role2, resource_type="workspace", resource_id=str(self.workspace.id)
            ).exists()
        )

    def test_batch_create_additive_only(self):
        """Creating a new binding does not remove existing bindings for the same subject."""
        self.service.batch_create([self._make_request(self.role1, "group", self.group.uuid)])

        self.service.batch_create([self._make_request(self.role2, "group", self.group.uuid)])

        self.assertTrue(
            RoleBinding.objects.filter(
                role=self.role1, resource_type="workspace", resource_id=str(self.workspace.id)
            ).exists(),
            "Pre-existing role1 binding must not be removed",
        )
        self.assertTrue(
            RoleBinding.objects.filter(
                role=self.role2, resource_type="workspace", resource_id=str(self.workspace.id)
            ).exists(),
        )

    def test_batch_create_duplicate_triples_deduplicated(self):
        """Identical triples within a single batch produce only one binding."""
        results = self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
                self._make_request(self.role1, "group", self.group.uuid),
            ]
        )

        self.assertEqual(len(results), 2, "Results mirror input length")

        bindings = RoleBinding.objects.filter(
            role=self.role1, resource_type="workspace", resource_id=str(self.workspace.id)
        )
        self.assertEqual(bindings.count(), 1)
        self.assertEqual(RoleBindingGroup.objects.filter(binding=bindings.first(), group=self.group).count(), 1)

    def test_batch_create_nonexistent_resource_raises_error(self):
        """A fake workspace UUID is rejected by resource validation."""
        fake_ws_id = str(uuid.uuid4())
        with self.assertRaises(NotFoundError) as ctx:
            self.service.batch_create(
                [
                    CreateBindingRequest(
                        role_id=str(self.role1.uuid),
                        resource_type="workspace",
                        resource_id=fake_ws_id,
                        subject_type="group",
                        subject_id=str(self.group.uuid),
                    ),
                ]
            )

        self.assertEqual(ctx.exception.resource_type, "workspace")
        self.assertIn(fake_ws_id, str(ctx.exception))

        self.assertFalse(RoleBinding.objects.filter(resource_id=fake_ws_id).exists())

    def test_batch_create_non_workspace_resource(self):
        """Bindings can be created for resource types without a local table."""
        app_id = str(uuid.uuid4())
        results = self.service.batch_create(
            [
                CreateBindingRequest(
                    role_id=str(self.role1.uuid),
                    resource_type="application",
                    resource_id=app_id,
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                ),
            ]
        )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["resource_type"], "application")
        self.assertEqual(result["resource_id"], app_id)
        self.assertIsNone(result["resource_name"])

        binding = RoleBinding.objects.get(resource_type="application", resource_id=app_id)
        self.assertEqual(binding.role, self.role1)
        self.assertTrue(RoleBindingGroup.objects.filter(binding=binding, group=self.group).exists())

    def test_batch_create_mixed_resource_types(self):
        """A batch with workspace and non-workspace resources succeeds for both."""
        app_id = str(uuid.uuid4())
        results = self.service.batch_create(
            [
                self._make_request(self.role1, "group", self.group.uuid),
                CreateBindingRequest(
                    role_id=str(self.role2.uuid),
                    resource_type="application",
                    resource_id=app_id,
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                ),
            ]
        )

        self.assertEqual(len(results), 2)

        ws_binding = RoleBinding.objects.get(
            resource_type="workspace", resource_id=str(self.workspace.id), role=self.role1
        )
        self.assertTrue(RoleBindingGroup.objects.filter(binding=ws_binding, group=self.group).exists())

        app_binding = RoleBinding.objects.get(resource_type="application", resource_id=app_id, role=self.role2)
        self.assertTrue(RoleBindingGroup.objects.filter(binding=app_binding, group=self.group).exists())

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_batch_create_non_workspace_resource_replicates(self):
        """Replication tuples use the correct resource type for non-workspace resources."""
        store = InMemoryTuples()
        service = RoleBindingService(tenant=self.tenant, replicator=InMemoryRelationReplicator(store))

        app_id = str(uuid.uuid4())
        service.batch_create(
            [
                CreateBindingRequest(
                    role_id=str(self.role1.uuid),
                    resource_type="application",
                    resource_id=app_id,
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                ),
            ]
        )

        binding = RoleBinding.objects.get(resource_type="application", resource_id=app_id)
        binding_uuid = str(binding.uuid)

        resource_tuples = store.find_tuples(
            all_of(
                resource("rbac", "application", app_id),
                relation("binding"),
                subject("rbac", "role_binding", binding_uuid),
            )
        )
        self.assertEqual(len(resource_tuples), 1)

        role_tuples = store.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_uuid),
                relation("role"),
                subject("rbac", "role", self.role1.uuid),
            )
        )
        self.assertEqual(len(role_tuples), 1)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_batch_create_multiple_source(self):
        """Test creating a role binding for a principal when it already exists with a different source."""
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)

        def create_with(service: RoleBindingService):
            service.batch_create(
                [
                    CreateBindingRequest(
                        role_id=str(self.role1.uuid),
                        resource_type="workspace",
                        resource_id=str(self.workspace.id),
                        subject_type="user",
                        subject_id=str(self.principal.uuid),
                    )
                ]
            )

        def assert_principal_bound():
            self.assertEqual(
                1,
                tuples.count_tuples(
                    all_of(
                        resource_type("rbac", "role_binding"),
                        relation("subject"),
                        subject("rbac", "principal", self.principal.principal_resource_id()),
                    )
                ),
            )

        api_service = RoleBindingService(tenant=self.tenant, replicator=replicator)
        alt_service = RoleBindingService(tenant=self.tenant, replicator=replicator, principal_source="another source")

        create_with(api_service)
        assert_principal_bound()

        tuples.clear()

        create_with(alt_service)

        # We haven't changed whether the principal is actually assigned to the role binding, but we still want to
        # replicate the subject tuple (just in case something went wrong previously).
        assert_principal_bound()

        self.assertCountEqual(
            [API_PRINCIPAL_SOURCE, "another source"],
            [p.source for p in RoleBinding.objects.get(role=self.role1).principal_entries.all()],
        )

    def test_batch_create_external_principal(self):
        """Test that external principals are rejected in batch_create unless enabled."""
        ext_tenant = Tenant.objects.create(tenant_name="external tenant", org_id="34567")
        ext_principal = Principal.objects.create(tenant=ext_tenant, username="some_user", user_id="some_user")

        def do_create_with(service: RoleBindingService):
            service.batch_create(
                [
                    CreateBindingRequest(
                        role_id=str(self.role1.uuid),
                        resource_type="workspace",
                        resource_id=str(self.default_workspace.id),
                        subject_type="user",
                        subject_id=str(ext_principal.uuid),
                    )
                ]
            )

        with self.assertRaises(NotFoundError):
            do_create_with(RoleBindingService(tenant=self.tenant, allow_external_subjects=False))

        try:
            do_create_with(RoleBindingService(tenant=self.tenant, allow_external_subjects=True))
        except NotFoundError as e:
            self.fail(f"Unexpected NotFoundError: {e}")

    def test_batch_create_external_group(self):
        """Test that external groups are rejected in batch_create unless enabled."""
        ext_tenant = Tenant.objects.create(tenant_name="external tenant", org_id="34567")
        ext_group = Group.objects.create(tenant=ext_tenant, name="a group")

        def do_create_with(service: RoleBindingService):
            service.batch_create(
                [
                    CreateBindingRequest(
                        role_id=str(self.role1.uuid),
                        resource_type="workspace",
                        resource_id=str(self.default_workspace.id),
                        subject_type="group",
                        subject_id=str(ext_group.uuid),
                    )
                ]
            )

        with self.assertRaises(NotFoundError):
            do_create_with(RoleBindingService(tenant=self.tenant, allow_external_subjects=False))

        try:
            do_create_with(RoleBindingService(tenant=self.tenant, allow_external_subjects=True))
        except NotFoundError as e:
            self.fail(f"Unexpected NotFoundError: {e}")


@override_settings(ATOMIC_RETRY_DISABLED=True)
class UpdateRoleBindingsForSubjectTests(_ReplicationAssertionsMixin, IdentityRequest):
    """Tests for RoleBindingService.update_role_bindings_for_subject method."""

    def setUp(self):
        """Set up test data using services."""
        super().setUp()

        # Create workspace hierarchy
        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)

        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            description="Test workspace description",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        # Create permissions and roles using RoleV2Service
        self.permission1 = Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        self.permission2 = Permission.objects.create(permission="app:resource:write", tenant=self.tenant)

        self.role_service = RoleV2Service()
        self.role1 = self.role_service.create(
            name="role1",
            description="Test role 1",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "read"}],
            tenant=self.tenant,
        )
        self.role2 = self.role_service.create(
            name="role2",
            description="Test role 2",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "write"}],
            tenant=self.tenant,
        )

        # Create group and principal
        self.group = Group.objects.create(
            name="test_group",
            description="Test group description",
            tenant=self.tenant,
        )
        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            user_id="testuser",
            type=Principal.Types.USER,
        )

        self.tracker = _ReplicationTracker()
        self.service = RoleBindingService(tenant=self.tenant, replicator=self.tracker)

    def tearDown(self):
        """Tear down test data."""
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        Principal.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        super().tearDown()

    def _get_binding(self, role):
        """Get a binding with role eagerly loaded for tuple generation."""
        return RoleBinding.objects.select_related("role").get(
            role=role, resource_id=str(self.workspace.id), resource_type="workspace"
        )

    def test_update_role_bindings_for_group(self):
        """Test updating role bindings for a group."""
        result = self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(self.group.uuid),
            role_ids=[str(self.role1.uuid), str(self.role2.uuid)],
        )

        expected = {
            "subject_type": "group",
            "subject": self.group,
            "resource_type": "workspace",
            "resource_id": str(self.workspace.id),
            "role_uuids": {self.role1.uuid, self.role2.uuid},
        }
        actual = {
            "subject_type": result.subject_type,
            "subject": result.subject,
            "resource_type": result.resource_type,
            "resource_id": result.resource_id,
            "role_uuids": {r.uuid for r in result.roles},
        }
        self.assertEqual(actual, expected)

        self.assertTrue(is_v2_write_activated(self.tenant))

    def test_update_role_bindings_for_principal(self):
        """Test updating role bindings for a principal."""
        result = self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="user",
            subject_id=str(self.principal.uuid),
            role_ids=[str(self.role1.uuid)],
        )

        expected = {
            "subject_type": "user",
            "subject": self.principal,
            "resource_type": "workspace",
            "resource_id": str(self.workspace.id),
            "role_uuids": {self.role1.uuid},
        }
        actual = {
            "subject_type": result.subject_type,
            "subject": result.subject,
            "resource_type": result.resource_type,
            "resource_id": result.resource_id,
            "role_uuids": {r.uuid for r in result.roles},
        }
        self.assertEqual(actual, expected)

        self.assertTrue(is_v2_write_activated(self.tenant))

    def test_update_replaces_existing_bindings(self):
        """Test that update replaces existing bindings."""
        # First update with role1
        self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(self.group.uuid),
            role_ids=[str(self.role1.uuid)],
        )

        # Second update with role2 only
        result = self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(self.group.uuid),
            role_ids=[str(self.role2.uuid)],
        )

        # Should have only role2 now (role1 was replaced)
        expected = {
            "subject_type": "group",
            "subject": self.group,
            "resource_type": "workspace",
            "resource_id": str(self.workspace.id),
            "role_uuids": {self.role2.uuid},
        }
        actual = {
            "subject_type": result.subject_type,
            "subject": result.subject,
            "resource_type": result.resource_type,
            "resource_id": result.resource_id,
            "role_uuids": {r.uuid for r in result.roles},
        }
        self.assertEqual(actual, expected)

        self.assertTrue(is_v2_write_activated(self.tenant))

    def test_update_multiple_sources(self):
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)

        def update_with(service: RoleBindingService, roles: list[RoleV2]):
            service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.workspace.id),
                subject_type="user",
                subject_id=str(self.principal.uuid),
                role_ids=[str(r.uuid) for r in roles],
            )

        api_service = RoleBindingService(tenant=self.tenant, replicator=replicator)
        alt_service = RoleBindingService(tenant=self.tenant, replicator=replicator, principal_source="another source")

        update_with(api_service, [self.role1])
        update_with(alt_service, [self.role1])

        binding = self._get_binding(self.role1)

        def assert_principal_bound(is_bound: bool = True):
            self.assertEqual(
                int(is_bound),
                tuples.count_tuples(
                    all_of(
                        resource("rbac", "role_binding", str(binding.uuid)),
                        relation("subject"),
                        subject("rbac", "principal", self.principal.principal_resource_id()),
                    )
                ),
            )

        def assert_sources(sources: list[str]):
            self.assertEqual(
                sources,
                [p.source for p in binding.principal_entries.all()],
            )

        assert_principal_bound()
        assert_sources([API_PRINCIPAL_SOURCE, "another source"])

        # We can't currently pass an empty list of roles, so just use another role instead.
        # See https://github.com/RedHatInsights/insights-rbac/pull/2629
        update_with(api_service, [self.role2])

        assert_principal_bound()
        assert_sources(["another source"])

        update_with(alt_service, [self.role2])

        assert_principal_bound(False)
        self.assertFalse(RoleBinding.objects.filter(pk=binding.pk).exists())

    def test_update_replicates_tuples_for_group(self):
        """Test that updating role bindings replicates correct tuples for a group."""
        self.tracker.clear()

        self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(self.group.uuid),
            role_ids=[str(self.role1.uuid)],
        )

        binding = self._get_binding(self.role1)
        self.assertTuplesAdded(set(binding.binding_tuples()) | {binding.subject_tuple(self.group)})
        self.assertTuplesRemoved(set())

    def test_update_replicates_tuples_for_principal(self):
        """Test that updating role bindings replicates correct tuples for a principal."""
        self.tracker.clear()

        self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="user",
            subject_id=str(self.principal.uuid),
            role_ids=[str(self.role1.uuid)],
        )

        binding = self._get_binding(self.role1)
        self.assertTuplesAdded(set(binding.binding_tuples()) | {binding.subject_tuple(self.principal)})
        self.assertTuplesRemoved(set())

    def test_update_external_principal(self):
        """Test that updating an external principal is rejected unless enabled."""
        ext_tenant = Tenant.objects.create(tenant_name="a tenant", org_id="34567")
        ext_principal = Principal.objects.create(
            tenant=ext_tenant, username="test_principal", user_id="test_principal"
        )

        def do_update_with(service: RoleBindingService):
            service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.default_workspace.id),
                subject_type="user",
                subject_id=str(ext_principal.uuid),
                role_ids=[str(self.role1.uuid)],
            )

        with self.assertRaises(NotFoundError):
            do_update_with(RoleBindingService(tenant=self.tenant, allow_external_subjects=False))

        try:
            do_update_with(RoleBindingService(tenant=self.tenant, allow_external_subjects=True))
        except NotFoundError as e:
            self.fail(f"Unexpected NotFoundError: {e}")

    def test_update_external_group(self):
        """Test that updating an external group is rejected unless enabled."""
        ext_tenant = Tenant.objects.create(tenant_name="a tenant", org_id="34567")
        ext_group = Group.objects.create(tenant=ext_tenant, name="test_group")

        def do_update_with(service: RoleBindingService):
            service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.default_workspace.id),
                subject_type="group",
                subject_id=str(ext_group.uuid),
                role_ids=[str(self.role1.uuid)],
            )

        with self.assertRaises(NotFoundError):
            do_update_with(RoleBindingService(tenant=self.tenant, allow_external_subjects=False))

        try:
            do_update_with(RoleBindingService(tenant=self.tenant, allow_external_subjects=True))
        except NotFoundError as e:
            self.fail(f"Unexpected NotFoundError: {e}")

    def test_update_raises_not_found_error(self):
        """Test that update raises NotFoundError for non-existent entities."""
        import uuid

        from management.exceptions import NotFoundError

        def make_cases():
            fake_group_id = str(uuid.uuid4())
            fake_principal_id = str(uuid.uuid4())
            fake_workspace_id = str(uuid.uuid4())

            return [
                # Non-existent group
                (
                    "invalid_group",
                    {
                        "resource_type": "workspace",
                        "resource_id": str(self.workspace.id),
                        "subject_type": "group",
                        "subject_id": fake_group_id,
                        "role_ids": [str(self.role1.uuid)],
                    },
                    "group",
                    fake_group_id,
                ),
                # Non-existent principal
                (
                    "invalid_principal",
                    {
                        "resource_type": "workspace",
                        "resource_id": str(self.workspace.id),
                        "subject_type": "user",
                        "subject_id": fake_principal_id,
                        "role_ids": [str(self.role1.uuid)],
                    },
                    "user",
                    fake_principal_id,
                ),
                # Non-existent workspace
                (
                    "invalid_resource",
                    {
                        "resource_type": "workspace",
                        "resource_id": fake_workspace_id,
                        "subject_type": "group",
                        "subject_id": str(self.group.uuid),
                        "role_ids": [str(self.role1.uuid)],
                    },
                    "workspace",
                    fake_workspace_id,
                ),
                # Tenant resource_id does not match tenant
                (
                    "invalid_tenant_resource",
                    {
                        "resource_type": "tenant",
                        "resource_id": "localhost/other-org-12345",
                        "subject_type": "group",
                        "subject_id": str(self.group.uuid),
                        "role_ids": [str(self.role1.uuid)],
                    },
                    "tenant",
                    "localhost/other-org-12345",
                ),
            ]

        for description, params, expected_resource_type, expected_resource_id in make_cases():
            with self.subTest(case=description):
                with self.assertRaises(NotFoundError) as context:
                    self.service.update_role_bindings_for_subject(**params)

                self.assertEqual(context.exception.resource_type, expected_resource_type)
                self.assertEqual(context.exception.resource_id, expected_resource_id)
                self.assertIn(expected_resource_id, str(context.exception))

                self.assertFalse(is_v2_write_activated(self.tenant))

    def test_update_raises_error_for_invalid_role(self):
        """Test that update raises InvalidFieldError for non-existent role."""
        import uuid

        from management.exceptions import InvalidFieldError

        fake_uuid = str(uuid.uuid4())
        with self.assertRaises(InvalidFieldError) as context:
            self.service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.workspace.id),
                subject_type="group",
                subject_id=str(self.group.uuid),
                role_ids=[fake_uuid],
            )

        self.assertEqual(context.exception.field, "roles")
        self.assertIn(fake_uuid, str(context.exception))

        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_update_raises_error_for_unsupported_subject_type(self):
        """Test that update raises UnsupportedSubjectTypeError for invalid subject type."""
        from management.subject import UnsupportedSubjectTypeError

        test_cases = [
            ("invalid_type", "invalid_type"),
            ("empty_string", ""),
        ]

        for description, subject_type in test_cases:
            with self.subTest(case=description):
                with self.assertRaises(UnsupportedSubjectTypeError) as context:
                    self.service.update_role_bindings_for_subject(
                        resource_type="workspace",
                        resource_id=str(self.workspace.id),
                        subject_type=subject_type,
                        subject_id=str(self.group.uuid),
                        role_ids=[str(self.role1.uuid)],
                    )

                self.assertEqual(context.exception.subject_type, subject_type)
                self.assertIn("group", context.exception.supported)
                self.assertIn("user", context.exception.supported)

                self.assertFalse(is_v2_write_activated(self.tenant))

    def test_update_raises_error_for_missing_required_fields(self):
        """Test that update raises RequiredFieldError for missing required fields."""
        from management.exceptions import RequiredFieldError

        test_cases = [
            # Empty resource_type - caught by model validation
            (
                "empty_resource_type",
                {
                    "resource_type": "",
                    "resource_id": str(self.workspace.id),
                    "subject_type": "group",
                    "subject_id": str(self.group.uuid),
                    "role_ids": [str(self.role1.uuid)],
                },
                "resource_type",
            ),
            # Empty resource_id - caught by service validation
            (
                "empty_resource_id",
                {
                    "resource_type": "workspace",
                    "resource_id": "",
                    "subject_type": "group",
                    "subject_id": str(self.group.uuid),
                    "role_ids": [str(self.role1.uuid)],
                },
                "resource_id",
            ),
            # Empty subject_id
            (
                "empty_subject_id",
                {
                    "resource_type": "workspace",
                    "resource_id": str(self.workspace.id),
                    "subject_type": "group",
                    "subject_id": "",
                    "role_ids": [str(self.role1.uuid)],
                },
                "subject_id",
            ),
        ]

        for description, params, expected_field in test_cases:
            with self.subTest(case=description):
                with self.assertRaises(RequiredFieldError) as context:
                    self.service.update_role_bindings_for_subject(**params)

                self.assertEqual(context.exception.field_name, expected_field)
                self.assertFalse(is_v2_write_activated(self.tenant))

    def test_update_rejects_admin_default_group(self):
        """Updating role bindings for the admin default group is not allowed."""
        public_tenant = Tenant.objects.get(tenant_name="public")
        admin_group = Group.objects.create(
            name="Default admin access",
            admin_default=True,
            system=True,
            tenant=public_tenant,
        )

        with self.assertRaises(InvalidFieldError) as context:
            self.service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.workspace.id),
                subject_type="group",
                subject_id=str(admin_group.uuid),
                role_ids=[str(self.role1.uuid)],
            )

        self.assertIn("admin default group", str(context.exception))
        admin_group.delete()

    def test_update_rejects_tenant_scoped_role_on_workspace(self):
        """Binding a tenant-scoped role to a workspace should fail."""
        from management.permission.scope_service import ImplicitResourceService

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=["tenant_app:*:*"],
            root_scope_permissions=[],
        )

        tenant_perm = Permission.objects.create(permission="tenant_app:res:read", tenant=self.tenant)
        tenant_role = RoleV2.objects.create(name="tenant_role", description="Tenant", tenant=self.tenant)
        tenant_role.permissions.add(tenant_perm)

        with patch("management.role_binding.service.default_implicit_resource_service", scope_service):
            with self.assertRaises(InvalidFieldError) as ctx:
                self.service.update_role_bindings_for_subject(
                    resource_type="workspace",
                    resource_id=str(self.workspace.id),
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                    role_ids=[str(tenant_role.uuid)],
                )
            self.assertIn("not scoped", str(ctx.exception))

    def test_update_rejects_default_scoped_role_on_root_workspace(self):
        """Binding a default-scoped role to the root workspace should fail."""
        from management.permission.scope_service import ImplicitResourceService

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=[],
            root_scope_permissions=["root_app:*:*"],
        )

        with patch("management.role_binding.service.default_implicit_resource_service", scope_service):
            with self.assertRaises(InvalidFieldError) as ctx:
                self.service.update_role_bindings_for_subject(
                    resource_type="workspace",
                    resource_id=str(self.root_workspace.id),
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                    role_ids=[str(self.role1.uuid)],
                )
            self.assertIn("not scoped", str(ctx.exception))

    def test_update_rejects_default_scoped_role_on_tenant(self):
        """Binding a default-scoped role to a tenant resource should fail."""
        from management.permission.scope_service import ImplicitResourceService

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=["tenant_app:*:*"],
            root_scope_permissions=[],
        )

        with patch("management.role_binding.service.default_implicit_resource_service", scope_service):
            with self.assertRaises(InvalidFieldError) as ctx:
                self.service.update_role_bindings_for_subject(
                    resource_type="tenant",
                    resource_id=self.tenant.tenant_resource_id(),
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                    role_ids=[str(self.role1.uuid)],
                )
            self.assertIn("not scoped", str(ctx.exception))

    def test_update_allows_correctly_scoped_role_on_root_workspace(self):
        """A root-scoped role should be accepted on the root workspace."""
        from management.permission.scope_service import ImplicitResourceService

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=[],
            root_scope_permissions=["root_app:*:*"],
        )

        root_perm = Permission.objects.create(permission="root_app:res:read", tenant=self.tenant)
        root_role = RoleV2.objects.create(name="root_role", description="Root", tenant=self.tenant)
        root_role.permissions.add(root_perm)

        with patch("management.role_binding.service.default_implicit_resource_service", scope_service):
            result = self.service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.root_workspace.id),
                subject_type="group",
                subject_id=str(self.group.uuid),
                role_ids=[str(root_role.uuid)],
            )
        self.assertEqual({r.uuid for r in result.roles}, {root_role.uuid})

    def test_update_rejects_explicit_default_role_on_standard_workspace(self):
        """Binding an explicit-DEFAULT role to a standard workspace should fail."""
        from management.permission.scope_service import ImplicitResourceService, PermissionScopeCache

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=["rbac:*:*"],
            root_scope_permissions=[],
            default_scope_permissions=["rbac:role_binding:*"],
        )
        scope_cache = PermissionScopeCache(scope_service)

        explicit_perm = Permission.objects.create(permission="rbac:role_binding:read", tenant=self.tenant)
        explicit_role = RoleV2.objects.create(
            name="explicit_default", description="Explicit DEFAULT", tenant=self.tenant
        )
        explicit_role.permissions.add(explicit_perm)

        with (
            patch("management.role_binding.service.default_implicit_resource_service", scope_service),
            patch("management.role_binding.service.permission_scope_cache", scope_cache),
        ):
            with self.assertRaises(InvalidFieldError) as ctx:
                self.service.update_role_bindings_for_subject(
                    resource_type="workspace",
                    resource_id=str(self.workspace.id),
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                    role_ids=[str(explicit_role.uuid)],
                )
            self.assertIn("not scoped", str(ctx.exception))
            self.assertIn("Standard Workspace", str(ctx.exception))

    def test_update_rejects_permissionless_role_on_standard_workspace(self):
        """Binding a permissionless role to a standard workspace should fail."""
        from management.permission.scope_service import ImplicitResourceService, PermissionScopeCache

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=[],
            root_scope_permissions=[],
        )
        scope_cache = PermissionScopeCache(scope_service)

        permissionless_role = RoleV2.objects.create(name="no_perms", description="No permissions", tenant=self.tenant)

        with (
            patch("management.role_binding.service.default_implicit_resource_service", scope_service),
            patch("management.role_binding.service.permission_scope_cache", scope_cache),
        ):
            with self.assertRaises(InvalidFieldError) as ctx:
                self.service.update_role_bindings_for_subject(
                    resource_type="workspace",
                    resource_id=str(self.workspace.id),
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                    role_ids=[str(permissionless_role.uuid)],
                )
            self.assertIn("not scoped", str(ctx.exception))
            self.assertIn("Standard Workspace", str(ctx.exception))

    def test_update_rejects_mixed_default_and_granular_role_on_standard_workspace(self):
        """Binding a role with both explicit-DEFAULT and workspace-granular permissions to a standard workspace should fail."""
        from management.permission.scope_service import ImplicitResourceService, PermissionScopeCache

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=[],
            root_scope_permissions=[],
            default_scope_permissions=["explicit_app:*:*"],
        )
        scope_cache = PermissionScopeCache(scope_service)

        explicit_perm = Permission.objects.create(permission="explicit_app:resource:read", tenant=self.tenant)
        granular_perm = Permission.objects.create(permission="other_app:resource:read", tenant=self.tenant)
        mixed_role = RoleV2.objects.create(
            name="mixed_role", description="Both explicit-DEFAULT and workspace-granular", tenant=self.tenant
        )
        mixed_role.permissions.add(explicit_perm, granular_perm)

        with (
            patch("management.role_binding.service.default_implicit_resource_service", scope_service),
            patch("management.role_binding.service.permission_scope_cache", scope_cache),
        ):
            with self.assertRaises(InvalidFieldError) as ctx:
                self.service.update_role_bindings_for_subject(
                    resource_type="workspace",
                    resource_id=str(self.workspace.id),
                    subject_type="group",
                    subject_id=str(self.group.uuid),
                    role_ids=[str(mixed_role.uuid)],
                )
            self.assertIn("not scoped", str(ctx.exception))
            self.assertIn("Standard Workspace", str(ctx.exception))

    def test_update_allows_workspace_granular_role_on_standard_workspace(self):
        """Binding a workspace-granular (fallback-DEFAULT) role to a standard workspace should succeed."""
        from management.permission.scope_service import ImplicitResourceService, PermissionScopeCache

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=[],
            root_scope_permissions=[],
            default_scope_permissions=["explicit_app:*:*"],
        )
        scope_cache = PermissionScopeCache(scope_service)

        granular_perm = Permission.objects.create(permission="other_app:resource:read", tenant=self.tenant)
        granular_role = RoleV2.objects.create(name="ws_granular", description="Workspace-granular", tenant=self.tenant)
        granular_role.permissions.add(granular_perm)

        with (
            patch("management.role_binding.service.default_implicit_resource_service", scope_service),
            patch("management.role_binding.service.permission_scope_cache", scope_cache),
        ):
            result = self.service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.workspace.id),
                subject_type="group",
                subject_id=str(self.group.uuid),
                role_ids=[str(granular_role.uuid)],
            )
        self.assertEqual({r.uuid for r in result.roles}, {granular_role.uuid})

    def test_update_allows_explicit_default_role_on_default_workspace(self):
        """Binding an explicit-DEFAULT role to the default workspace should succeed."""
        from management.permission.scope_service import ImplicitResourceService, PermissionScopeCache

        scope_service = ImplicitResourceService(
            tenant_scope_permissions=[],
            root_scope_permissions=[],
            default_scope_permissions=["explicit_app:*:*"],
        )
        scope_cache = PermissionScopeCache(scope_service)

        explicit_perm = Permission.objects.create(permission="explicit_app:resource:read", tenant=self.tenant)
        explicit_role = RoleV2.objects.create(
            name="explicit_default", description="Explicit DEFAULT", tenant=self.tenant
        )
        explicit_role.permissions.add(explicit_perm)

        with (
            patch("management.role_binding.service.default_implicit_resource_service", scope_service),
            patch("management.role_binding.service.permission_scope_cache", scope_cache),
        ):
            result = self.service.update_role_bindings_for_subject(
                resource_type="workspace",
                resource_id=str(self.default_workspace.id),
                subject_type="group",
                subject_id=str(self.group.uuid),
                role_ids=[str(explicit_role.uuid)],
            )
        self.assertEqual({r.uuid for r in result.roles}, {explicit_role.uuid})


@override_settings(ATOMIC_RETRY_DISABLED=True)
class ReplaceRoleBindingsTests(_ReplicationAssertionsMixin, IdentityRequest):
    """Tests for RoleBindingService._replace_role_bindings persistence logic.

    Each test verifies both the add and remove side of a PUT operation,
    since update-by-subject is a declarative "make it look like this."
    Replication tuples are asserted in full for every scenario.
    """

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.role1 = RoleV2.objects.create(name="role1", tenant=self.tenant)
        self.role2 = RoleV2.objects.create(name="role2", tenant=self.tenant)
        self.role3 = RoleV2.objects.create(name="role3", tenant=self.tenant)
        self.role4 = RoleV2.objects.create(name="role4", tenant=self.tenant)

        self.group1 = Group.objects.create(name="group1", tenant=self.tenant)
        self.group2 = Group.objects.create(name="group2", tenant=self.tenant)

        self.user1 = Principal.objects.create(tenant=self.tenant, username="user1", user_id="user1")
        self.user2 = Principal.objects.create(tenant=self.tenant, username="user2", user_id="user2")

        self.tracker = _ReplicationTracker()
        self.service = RoleBindingService(tenant=self.tenant, replicator=self.tracker)
        self.ws = "ws-123"

    def tearDown(self):
        """Clean up test data."""
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        RoleV2.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()

    # -- helpers ----------------------------------------------------------

    def _get_binding(self, role, resource_id=None):
        """Get a binding with role eagerly loaded for tuple generation."""
        return RoleBinding.objects.select_related("role").get(
            role=role, resource_id=resource_id or self.ws, resource_type="workspace"
        )

    def _roles_for_principal(self, principal):
        """Return the set of roles a principal is linked to on self.ws."""
        return set(
            RoleBindingPrincipal.objects.filter(
                principal=principal,
                binding__resource_id=self.ws,
                binding__resource_type="workspace",
            ).values_list("binding__role__name", flat=True)
        )

    def _roles_for_group(self, group):
        """Return the set of roles a group is linked to on self.ws."""
        return set(
            RoleBindingGroup.objects.filter(
                group=group,
                binding__resource_id=self.ws,
                binding__resource_type="workspace",
            ).values_list("binding__role__name", flat=True)
        )

    def _binding_exists(self, role):
        """Return whether a RoleBinding(ws, role) row exists."""
        return RoleBinding.objects.filter(role=role, resource_id=self.ws, resource_type="workspace").exists()

    def _binding_subject_count(self, role):
        """Return total subjects (groups + principals) attached to the binding."""
        try:
            binding = RoleBinding.objects.get(role=role, resource_id=self.ws, resource_type="workspace")
        except RoleBinding.DoesNotExist:
            return 0
        return binding.group_entries.count() + binding.principal_entries.count()

    def _update_access(self, subject, roles):
        """Shortcut for calling the service method under test."""
        self.service._replace_role_bindings(
            resource_type="workspace",
            resource_id=self.ws,
            subject=subject,
            roles=roles,
        )

    # -- 1. Fresh user, no prior bindings — adds only ---------------------

    def test_fresh_user_no_prior_bindings(self):
        """User has no bindings on the workspace; PUT adds new ones."""
        # Given: user1 has no bindings on ws
        self.assertEqual(self._roles_for_principal(self.user1), set())
        self.tracker.clear()

        # When: PUT roles=[role3]
        self._update_access(self.user1, [self.role3])

        # Then — added: RoleBinding(ws, role3) created, user1 linked
        self.assertTrue(self._binding_exists(self.role3))
        self.assertEqual(self._roles_for_principal(self.user1), {"role3"})

        # Then — removed: nothing (no prior bindings)
        self.assertEqual(RoleBinding.objects.filter(resource_id=self.ws).count(), 1)

        # Then — replication: new binding (role + resource) + subject
        binding = self._get_binding(self.role3)
        self.assertTuplesAdded(set(binding.binding_tuples()) | {binding.subject_tuple(self.user1)})
        self.assertTuplesRemoved(set())

    # -- 1b. Empty roles removes all bindings -------------------------------

    def test_empty_roles_removes_all_bindings(self):
        """Empty roles list removes all bindings for the subject."""
        # Given: user1 linked to RoleBinding(ws, role1), only subject
        self._update_access(self.user1, [self.role1])
        self.assertTrue(self._binding_exists(self.role1))
        old_binding = self._get_binding(self.role1)
        self.tracker.clear()

        # When: PUT roles=[]
        self._update_access(self.user1, [])

        # Then — removed: user1 unlinked, binding deleted (orphaned)
        self.assertFalse(self._binding_exists(self.role1))
        self.assertEqual(self._roles_for_principal(self.user1), set())

        # Then — replication: old binding fully removed
        self.assertTuplesAdded(set())
        self.assertTuplesRemoved(set(old_binding.binding_tuples()) | {old_binding.subject_tuple(self.user1)})

    # -- 2. Complete replacement, old binding orphaned --------------------

    def test_complete_replacement_orphaned_binding_deleted(self):
        """User is the only subject on the old binding; old binding is deleted."""
        # Given: user1 linked to RoleBinding(ws, role1), only subject
        self._update_access(self.user1, [self.role1])
        self.assertTrue(self._binding_exists(self.role1))
        old_binding = self._get_binding(self.role1)
        self.tracker.clear()

        # When: PUT roles=[role3]
        self._update_access(self.user1, [self.role3])

        # Then — added: RoleBinding(ws, role3) created, user1 linked
        self.assertTrue(self._binding_exists(self.role3))
        self.assertEqual(self._roles_for_principal(self.user1), {"role3"})

        # Then — removed: user1 unlinked from role1, binding deleted (orphaned)
        self.assertFalse(self._binding_exists(self.role1))

        # Then — replication: old binding fully removed, new binding fully added
        new_binding = self._get_binding(self.role3)
        self.assertTuplesAdded(set(new_binding.binding_tuples()) | {new_binding.subject_tuple(self.user1)})
        self.assertTuplesRemoved(set(old_binding.binding_tuples()) | {old_binding.subject_tuple(self.user1)})

    # -- 3. Complete replacement, old binding has other users — kept ------

    def test_complete_replacement_shared_binding_kept(self):
        """Another user is on the old binding; binding survives removal."""
        # Given: user1 and user2 both linked to RoleBinding(ws, role1)
        self._update_access(self.user1, [self.role1])
        self._update_access(self.user2, [self.role1])
        self.assertEqual(self._binding_subject_count(self.role1), 2)
        role1_binding = self._get_binding(self.role1)
        self.tracker.clear()

        # When: PUT roles=[role3] for user1
        self._update_access(self.user1, [self.role3])

        # Then — added: RoleBinding(ws, role3) created, user1 linked
        self.assertTrue(self._binding_exists(self.role3))
        self.assertEqual(self._roles_for_principal(self.user1), {"role3"})

        # Then — removed: user1 unlinked from role1, but binding kept (user2 still on it)
        self.assertTrue(self._binding_exists(self.role1))
        self.assertEqual(self._binding_subject_count(self.role1), 1)
        self.assertEqual(self._roles_for_principal(self.user2), {"role1"})

        # Then — replication: new binding added; only subject unlinked from old (binding kept)
        new_binding = self._get_binding(self.role3)
        self.assertTuplesAdded(set(new_binding.binding_tuples()) | {new_binding.subject_tuple(self.user1)})
        self.assertTuplesRemoved({role1_binding.subject_tuple(self.user1)})

    # -- 4. Partial overlap — keep shared, remove old, add new -----------

    def test_partial_overlap(self):
        """Some roles stay, some are removed, some are added."""
        # Given: user1 linked to role1 and role2 (only subject on both)
        self._update_access(self.user1, [self.role1, self.role2])
        self.assertEqual(self._roles_for_principal(self.user1), {"role1", "role2"})
        old_role1_binding = self._get_binding(self.role1)
        self.tracker.clear()

        # When: PUT roles=[role2, role3]
        self._update_access(self.user1, [self.role2, self.role3])

        # Then — added: RoleBinding(ws, role3) created, user1 linked
        self.assertTrue(self._binding_exists(self.role3))

        # Then — kept: user1 still linked to role2
        self.assertTrue(self._binding_exists(self.role2))
        self.assertEqual(self._roles_for_principal(self.user1), {"role2", "role3"})

        # Then — removed: user1 unlinked from role1, binding deleted (orphaned)
        self.assertFalse(self._binding_exists(self.role1))

        # Then — replication: role3 binding added; role1 orphaned + removed; role2 untouched
        new_role3_binding = self._get_binding(self.role3)
        self.assertTuplesAdded(set(new_role3_binding.binding_tuples()) | {new_role3_binding.subject_tuple(self.user1)})
        self.assertTuplesRemoved(
            set(old_role1_binding.binding_tuples()) | {old_role1_binding.subject_tuple(self.user1)}
        )

    # -- 5. Idempotent — request matches current state exactly -----------

    def test_idempotent_same_roles(self):
        """PUT with the same roles is a true no-op — no DB writes."""
        # Given: user1 linked to role1 and role2
        self._update_access(self.user1, [self.role1, self.role2])
        original_binding_ids = set(RoleBinding.objects.filter(resource_id=self.ws).values_list("id", flat=True))
        original_through_ids = set(
            RoleBindingPrincipal.objects.filter(principal=self.user1, binding__resource_id=self.ws).values_list(
                "id", flat=True
            )
        )
        self.tracker.clear()

        # When: PUT roles=[role1, role2] (same as current)
        self._update_access(self.user1, [self.role1, self.role2])

        # Then — same roles still assigned
        self.assertEqual(self._roles_for_principal(self.user1), {"role1", "role2"})

        # Then — exact same binding rows (IDs unchanged, true no-op)
        current_binding_ids = set(RoleBinding.objects.filter(resource_id=self.ws).values_list("id", flat=True))
        self.assertEqual(original_binding_ids, current_binding_ids)

        # Then — exact same through-table rows (IDs unchanged)
        current_through_ids = set(
            RoleBindingPrincipal.objects.filter(principal=self.user1, binding__resource_id=self.ws).values_list(
                "id", flat=True
            )
        )
        self.assertEqual(original_through_ids, current_through_ids)

        # Then — replication: no tuples written (true no-op)
        self.assertTuplesAdded(set())
        self.assertTuplesRemoved(set())

    # -- 6. Reuse existing binding from another user ---------------------

    def test_reuse_existing_binding_from_another_user(self):
        """New role already has a binding from another user; reuse it."""
        # Given: user2 linked to RoleBinding(ws, role3); user1 linked to role1 (only subject)
        self._update_access(self.user2, [self.role3])
        self._update_access(self.user1, [self.role1])
        role3_binding_id = RoleBinding.objects.get(role=self.role3, resource_id=self.ws).id
        role3_binding = self._get_binding(self.role3)
        old_role1_binding = self._get_binding(self.role1)
        self.tracker.clear()

        # When: PUT roles=[role3] for user1
        self._update_access(self.user1, [self.role3])

        # Then — added: user1 linked to existing RoleBinding(ws, role3), no new binding
        self.assertEqual(self._roles_for_principal(self.user1), {"role3"})
        self.assertEqual(
            RoleBinding.objects.get(role=self.role3, resource_id=self.ws).id,
            role3_binding_id,
        )
        self.assertEqual(self._binding_subject_count(self.role3), 2)

        # Then — removed: user1 unlinked from role1, binding deleted (orphaned)
        self.assertFalse(self._binding_exists(self.role1))

        # Then — user2 still linked to role3
        self.assertEqual(self._roles_for_principal(self.user2), {"role3"})

        # Then — replication: subject linked to existing binding (no binding tuples);
        #         old binding orphaned + deleted
        self.assertTuplesAdded({role3_binding.subject_tuple(self.user1)})
        self.assertTuplesRemoved(
            set(old_role1_binding.binding_tuples()) | {old_role1_binding.subject_tuple(self.user1)}
        )

    # -- 7. Mixed orphan outcomes ----------------------------------------

    def test_mixed_orphan_outcomes(self):
        """Some old bindings are orphaned (deleted), some are not (other user)."""
        # Given: user1 on role1 and role2. user2 also on role1 but NOT role2.
        self._update_access(self.user1, [self.role1, self.role2])
        self._update_access(self.user2, [self.role1])
        role1_binding = self._get_binding(self.role1)
        old_role2_binding = self._get_binding(self.role2)
        self.tracker.clear()

        # When: PUT roles=[role3] for user1
        self._update_access(self.user1, [self.role3])

        # Then — added: RoleBinding(ws, role3) created, user1 linked
        self.assertTrue(self._binding_exists(self.role3))
        self.assertEqual(self._roles_for_principal(self.user1), {"role3"})

        # Then — removed (kept): RoleBinding(ws, role1) kept, user2 still on it
        self.assertTrue(self._binding_exists(self.role1))
        self.assertEqual(self._binding_subject_count(self.role1), 1)
        self.assertEqual(self._roles_for_principal(self.user2), {"role1"})

        # Then — removed (orphaned): RoleBinding(ws, role2) deleted, no one left
        self.assertFalse(self._binding_exists(self.role2))

        # Then — replication:
        #   role3: new binding (role + resource + subject)
        #   role1: subject unlinked only (binding kept for user2)
        #   role2: subject unlinked + binding deleted (orphaned)
        new_role3_binding = self._get_binding(self.role3)
        self.assertTuplesAdded(set(new_role3_binding.binding_tuples()) | {new_role3_binding.subject_tuple(self.user1)})
        self.assertTuplesRemoved(
            # role1: only subject unlink (binding survives)
            {role1_binding.subject_tuple(self.user1)}
            # role2: subject unlink + orphaned binding tuples
            | {old_role2_binding.subject_tuple(self.user1)}
            | set(old_role2_binding.binding_tuples())
        )

    # -- 8. Group on same binding — removing user doesn't orphan ---------

    def test_group_on_same_binding_prevents_orphan(self):
        """A group is also on the binding; removing the user doesn't orphan it."""
        # Given: group1 and user1 both linked to RoleBinding(ws, role1)
        self._update_access(self.group1, [self.role1])
        self._update_access(self.user1, [self.role1])
        self.assertEqual(self._binding_subject_count(self.role1), 2)
        role1_binding = self._get_binding(self.role1)
        self.tracker.clear()

        # When: PUT roles=[role3] for user1
        self._update_access(self.user1, [self.role3])

        # Then — added: RoleBinding(ws, role3) created, user1 linked
        self.assertTrue(self._binding_exists(self.role3))
        self.assertEqual(self._roles_for_principal(self.user1), {"role3"})

        # Then — removed: user1 unlinked from role1, but binding kept (group1 still on it)
        self.assertTrue(self._binding_exists(self.role1))
        self.assertEqual(self._binding_subject_count(self.role1), 1)
        self.assertEqual(self._roles_for_group(self.group1), {"role1"})

        # Then — replication: new binding added; only user1's subject removed from old (binding kept by group)
        new_role3_binding = self._get_binding(self.role3)
        self.assertTuplesAdded(set(new_role3_binding.binding_tuples()) | {new_role3_binding.subject_tuple(self.user1)})
        self.assertTuplesRemoved({role1_binding.subject_tuple(self.user1)})

    # -- 9. Cross-resource isolation -------------------------------------

    def test_cross_resource_isolation(self):
        """Updating bindings on one workspace does not affect another."""
        # Given: user1 has role1 on ws-123 and role2 on ws-456
        self._update_access(self.user1, [self.role1])
        self.service._replace_role_bindings(
            resource_type="workspace",
            resource_id="ws-456",
            subject=self.user1,
            roles=[self.role2],
        )
        old_role1_binding = self._get_binding(self.role1)
        self.tracker.clear()

        # When: PUT roles=[role3] for user1 on ws-123
        self._update_access(self.user1, [self.role3])

        # Then — added: RoleBinding(ws-123, role3) created, user1 linked
        self.assertEqual(self._roles_for_principal(self.user1), {"role3"})

        # Then — removed: RoleBinding(ws-123, role1) deleted
        self.assertFalse(self._binding_exists(self.role1))

        # Then — untouched: user1 still has role2 on ws-456
        ws456_roles = set(
            RoleBindingPrincipal.objects.filter(
                principal=self.user1,
                binding__resource_id="ws-456",
                binding__resource_type="workspace",
            ).values_list("binding__role__name", flat=True)
        )
        self.assertEqual(ws456_roles, {"role2"})

        # Then — replication: only ws-123 bindings affected; ws-456 untouched
        new_role3_binding = self._get_binding(self.role3)
        self.assertTuplesAdded(set(new_role3_binding.binding_tuples()) | {new_role3_binding.subject_tuple(self.user1)})
        self.assertTuplesRemoved(
            set(old_role1_binding.binding_tuples()) | {old_role1_binding.subject_tuple(self.user1)}
        )

    # -- 10. Same flow works for group subject ---------------------------

    def test_same_flow_for_group_subject(self):
        """The full add+remove flow works identically for a group subject."""
        # Given: group1 linked to RoleBinding(ws, role1) (only subject)
        self._update_access(self.group1, [self.role1])
        self.assertTrue(self._binding_exists(self.role1))
        old_role1_binding = self._get_binding(self.role1)
        self.tracker.clear()

        # When: PUT roles=[role3] for group1
        self._update_access(self.group1, [self.role3])

        # Then — added: RoleBinding(ws, role3) created, group1 linked
        self.assertTrue(self._binding_exists(self.role3))
        self.assertEqual(self._roles_for_group(self.group1), {"role3"})

        # Then — removed: group1 unlinked from role1, binding deleted (orphaned)
        self.assertFalse(self._binding_exists(self.role1))

        # Then — replication: new binding added (group subject uses #member); old orphaned + removed
        new_role3_binding = self._get_binding(self.role3)
        self.assertTuplesAdded(
            set(new_role3_binding.binding_tuples()) | {new_role3_binding.subject_tuple(self.group1)}
        )
        self.assertTuplesRemoved(
            set(old_role1_binding.binding_tuples()) | {old_role1_binding.subject_tuple(self.group1)}
        )


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_BOOTSTRAP_TENANT=True, REPLICATION_TO_RELATION_ENABLED=False)
class UpdateRoleBindingsDefaultGroupCustomizationTests(IdentityRequest):
    """Tests for automatic default group customization in update_role_bindings_for_subject."""

    def setUp(self):
        super().setUp()
        from management.policy.model import Policy

        self.public_tenant = Tenant.objects.get(tenant_name="public")

        self.platform_role_v1 = Role.objects.create(
            name="Platform Role",
            platform_default=True,
            system=True,
            tenant=self.public_tenant,
        )

        # The public tenant's "Default access" group (system, shared across all tenants)
        self.default_group = Group.objects.create(
            name="Default access",
            platform_default=True,
            system=True,
            tenant=self.public_tenant,
        )
        Policy.objects.create(
            name=f"System Policy for Group {self.default_group.uuid}",
            system=True,
            group=self.default_group,
            tenant=self.public_tenant,
        )

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        role_service = RoleV2Service()
        self.role1 = role_service.create(
            name="role1",
            description="Test role 1",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "read"}],
            tenant=self.tenant,
        )

        self.service = RoleBindingService(tenant=self.tenant)

    def tearDown(self):
        from management.policy.model import Policy

        RoleBindingGroup.objects.all().delete()
        RoleBindingPrincipal.objects.all().delete()
        RoleBinding.objects.all().delete()
        Policy.objects.all().delete()
        Group.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.public_tenant, platform_default=True).delete()
        Role.objects.filter(tenant=self.public_tenant, platform_default=True).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    @patch("management.group.definer.OutboxReplicator")
    def test_clones_default_group_into_custom(self, mock_replicator_cls):
        """Passing the public default group UUID triggers a clone into a custom default group."""
        mock_replicator_cls.return_value.replicate = lambda event: None

        self.assertFalse(Group.objects.filter(platform_default=True, tenant=self.tenant).exists())

        result = self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(self.default_group.uuid),
            role_ids=[str(self.role1.uuid)],
        )

        # The custom group was created for this tenant
        custom_group = Group.objects.get(platform_default=True, tenant=self.tenant)
        self.assertEqual(custom_group.name, "Custom default access")
        self.assertFalse(custom_group.system)

        # The result references the custom group, not the public default group
        self.assertEqual(result.subject, custom_group)
        self.assertEqual(result.subject.tenant, self.tenant)

        # The role binding was applied to the custom group
        bindings = RoleBinding.objects.filter(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            tenant=self.tenant,
        )
        self.assertTrue(bindings.filter(role=self.role1).exists())
        self.assertTrue(RoleBindingGroup.objects.filter(group=custom_group, binding__role=self.role1).exists())

    @patch("management.group.definer.OutboxReplicator")
    def test_uses_existing_custom_group_if_already_cloned(self, mock_replicator_cls):
        """If a custom default group already exists, use it instead of cloning again."""
        mock_replicator_cls.return_value.replicate = lambda event: None

        existing_custom = Group.objects.create(
            name="Custom default access",
            platform_default=True,
            system=False,
            tenant=self.tenant,
        )

        result = self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(self.default_group.uuid),
            role_ids=[str(self.role1.uuid)],
        )

        # Should use the existing custom group
        self.assertEqual(result.subject, existing_custom)

        # Still only one custom group
        self.assertEqual(Group.objects.filter(platform_default=True, tenant=self.tenant).count(), 1)

    def test_regular_group_is_not_affected(self):
        """A non-default group is not cloned; it proceeds normally."""
        regular_group = Group.objects.create(
            name="Regular Group",
            tenant=self.tenant,
        )

        result = self.service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(regular_group.uuid),
            role_ids=[str(self.role1.uuid)],
        )

        self.assertEqual(result.subject, regular_group)
        self.assertFalse(Group.objects.filter(platform_default=True, tenant=self.tenant).exists())


@override_settings(ATOMIC_RETRY_DISABLED=True, REPLICATION_TO_RELATION_ENABLED=True)
class PrepareToDeleteGroupV2Tests(_ReplicationAssertionsMixin, IdentityRequest):
    """Tests for the V2 path of prepare_to_delete_group.

    When a tenant has V2 writes activated, prepare_to_delete_group should derive
    removal tuples from V2 RoleBinding objects instead of BindingMapping.
    """

    def setUp(self):
        super().setUp()
        from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        Permission.objects.create(permission="app:resource:read", tenant=self.tenant)
        Permission.objects.create(permission="app:resource:write", tenant=self.tenant)
        role_service = RoleV2Service()
        self.role1 = role_service.create(
            name="role1",
            description="Test role 1",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "read"}],
            tenant=self.tenant,
        )
        self.role2 = role_service.create(
            name="role2",
            description="Test role 2",
            permission_data=[{"application": "app", "resource_type": "resource", "operation": "write"}],
            tenant=self.tenant,
        )

        self.group = Group.objects.create(name="test_group", tenant=self.tenant)

        self.tracker = _ReplicationTracker()
        self.binding_service = RoleBindingService(tenant=self.tenant, replicator=self.tracker)

        # Create role bindings via the V2 service (this also activates V2 writes)
        self.binding_service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(self.group.uuid),
            role_ids=[str(self.role1.uuid), str(self.role2.uuid)],
        )
        self.tracker.clear()

        self.RelationApiDualWriteGroupHandler = RelationApiDualWriteGroupHandler

    def tearDown(self):
        RoleBindingGroup.objects.all().delete()
        RoleBindingPrincipal.objects.all().delete()
        RoleBinding.objects.all().delete()
        Group.objects.filter(tenant=self.tenant).delete()
        RoleV2.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def test_v2_path_removes_binding_tuples(self):
        """When tenant is V2-activated, prepare_to_delete_group uses RoleBinding to compute removal tuples."""
        from management.tenant_mapping.v2_activation import is_v2_write_activated

        self.assertTrue(is_v2_write_activated(self.tenant))

        bindings = list(
            RoleBinding.objects.filter(group_entries__group=self.group, tenant=self.tenant).select_related("role")
        )
        self.assertEqual(len(bindings), 2)

        expected_tuples_to_remove = set()
        for binding in bindings:
            expected_tuples_to_remove.add(binding.subject_tuple(self.group))
            expected_tuples_to_remove.update(binding.binding_tuples())

        dual_write_handler = self.RelationApiDualWriteGroupHandler(
            self.group,
            ReplicationEventType.DELETE_GROUP,
            replicator=self.tracker,
        )

        roles = Role.objects.filter(policies__group=self.group)
        dual_write_handler.prepare_to_delete_group(roles)

        self.assertEqual(set(dual_write_handler.relations_to_remove), expected_tuples_to_remove)

    def test_v2_path_preserves_shared_binding_tuples(self):
        """When another subject shares a binding, only the subject tuple is removed (not binding tuples)."""
        other_group = Group.objects.create(name="other_group", tenant=self.tenant)

        # Link the other group to the same binding via the service
        self.binding_service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="group",
            subject_id=str(other_group.uuid),
            role_ids=[str(self.role1.uuid)],
        )
        self.tracker.clear()

        shared_binding = RoleBinding.objects.select_related("role").get(
            role=self.role1, resource_id=str(self.workspace.id), tenant=self.tenant
        )
        sole_binding = RoleBinding.objects.select_related("role").get(
            role=self.role2, resource_id=str(self.workspace.id), tenant=self.tenant
        )

        dual_write_handler = self.RelationApiDualWriteGroupHandler(
            self.group,
            ReplicationEventType.DELETE_GROUP,
            replicator=self.tracker,
        )

        roles = Role.objects.filter(policies__group=self.group)
        dual_write_handler.prepare_to_delete_group(roles)

        removals = set(dual_write_handler.relations_to_remove)

        # Subject tuples for both bindings are removed
        self.assertIn(shared_binding.subject_tuple(self.group), removals)
        self.assertIn(sole_binding.subject_tuple(self.group), removals)

        # Binding tuples for the shared binding are NOT removed (other_group still uses it)
        for t in shared_binding.binding_tuples():
            self.assertNotIn(t, removals)

        # Binding tuples for the sole binding ARE removed (it will be orphaned)
        for t in sole_binding.binding_tuples():
            self.assertIn(t, removals)

    def test_v2_path_preserves_binding_when_principal_exists(self):
        """When a binding has a principal entry, binding tuples are kept even if the group is the only group."""
        principal = Principal.objects.create(
            username="user_on_binding", tenant=self.tenant, user_id="user_on_binding", type=Principal.Types.USER
        )

        self.binding_service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(self.workspace.id),
            subject_type="user",
            subject_id=str(principal.uuid),
            role_ids=[str(self.role1.uuid)],
        )
        self.tracker.clear()

        binding_with_principal = RoleBinding.objects.select_related("role").get(
            role=self.role1, resource_id=str(self.workspace.id), tenant=self.tenant
        )
        sole_binding = RoleBinding.objects.select_related("role").get(
            role=self.role2, resource_id=str(self.workspace.id), tenant=self.tenant
        )

        dual_write_handler = self.RelationApiDualWriteGroupHandler(
            self.group,
            ReplicationEventType.DELETE_GROUP,
            replicator=self.tracker,
        )

        roles = Role.objects.filter(policies__group=self.group)
        dual_write_handler.prepare_to_delete_group(roles)

        removals = set(dual_write_handler.relations_to_remove)

        # Subject tuple for the group is always removed
        self.assertIn(binding_with_principal.subject_tuple(self.group), removals)
        self.assertIn(sole_binding.subject_tuple(self.group), removals)

        # Binding tuples for the binding with a principal are NOT removed
        for t in binding_with_principal.binding_tuples():
            self.assertNotIn(t, removals)

        # Binding tuples for the sole binding (no other subjects) ARE removed
        for t in sole_binding.binding_tuples():
            self.assertIn(t, removals)

    def test_v2_path_includes_member_relations(self):
        """Non-platform_default group removal also removes member (group→principal) relations."""
        principal = Principal.objects.create(
            username="group_member", tenant=self.tenant, user_id="group_member", type=Principal.Types.USER
        )
        self.group.principals.add(principal)

        dual_write_handler = self.RelationApiDualWriteGroupHandler(
            self.group,
            ReplicationEventType.DELETE_GROUP,
            replicator=self.tracker,
        )

        roles = Role.objects.filter(policies__group=self.group)
        dual_write_handler.prepare_to_delete_group(roles)

        removals = set(dual_write_handler.relations_to_remove)

        member_tuple = self.group.relationship_to_principal(principal)
        self.assertIsNotNone(member_tuple)
        self.assertIn(member_tuple, removals)

    def test_v2_path_no_bindings_only_removes_member_relations(self):
        """Group with no role bindings only produces member relation removals."""
        lonely_group = Group.objects.create(name="lonely_group", tenant=self.tenant)
        principal = Principal.objects.create(
            username="lonely_member", tenant=self.tenant, user_id="lonely_member", type=Principal.Types.USER
        )
        lonely_group.principals.add(principal)

        dual_write_handler = self.RelationApiDualWriteGroupHandler(
            lonely_group,
            ReplicationEventType.DELETE_GROUP,
            replicator=self.tracker,
        )

        roles = Role.objects.filter(policies__group=lonely_group)
        dual_write_handler.prepare_to_delete_group(roles)

        removals = set(dual_write_handler.relations_to_remove)
        member_tuple = lonely_group.relationship_to_principal(principal)
        self.assertIsNotNone(member_tuple)
        self.assertEqual(removals, {member_tuple})

    def test_v2_path_multiple_workspaces(self):
        """Bindings across different workspaces are all collected for removal."""
        other_workspace = Workspace.objects.create(
            name="Other Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        self.binding_service.update_role_bindings_for_subject(
            resource_type="workspace",
            resource_id=str(other_workspace.id),
            subject_type="group",
            subject_id=str(self.group.uuid),
            role_ids=[str(self.role1.uuid)],
        )
        self.tracker.clear()

        all_bindings = list(
            RoleBinding.objects.filter(group_entries__group=self.group, tenant=self.tenant).select_related("role")
        )
        self.assertEqual(len(all_bindings), 3)

        dual_write_handler = self.RelationApiDualWriteGroupHandler(
            self.group,
            ReplicationEventType.DELETE_GROUP,
            replicator=self.tracker,
        )

        roles = Role.objects.filter(policies__group=self.group)
        dual_write_handler.prepare_to_delete_group(roles)

        removals = set(dual_write_handler.relations_to_remove)

        expected = set()
        for binding in all_bindings:
            expected.add(binding.subject_tuple(self.group))
            expected.update(binding.binding_tuples())

        for t in expected:
            self.assertIn(t, removals)

    def test_v2_path_platform_default_adds_default_binding(self):
        """Platform-default group triggers relations_to_add instead of member removals."""
        from management.relation_replicator.types import ObjectReference, ObjectType, RelationTuple, SubjectReference

        sentinel_tuple = RelationTuple(
            resource=ObjectReference(type=ObjectType(namespace="rbac", name="sentinel"), id="default"),
            relation="binding",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="rbac", name="role_binding"), id="default"),
            ),
        )

        self.group.platform_default = True
        self.group.save()

        dual_write_handler = self.RelationApiDualWriteGroupHandler(
            self.group,
            ReplicationEventType.DELETE_GROUP,
            replicator=self.tracker,
        )

        with patch.object(dual_write_handler, "_default_binding", return_value=[sentinel_tuple]):
            roles = Role.objects.filter(policies__group=self.group)
            dual_write_handler.prepare_to_delete_group(roles)

        self.assertIn(sentinel_tuple, dual_write_handler.relations_to_add)

        member_tuples = [
            t
            for t in dual_write_handler.relations_to_remove
            if t.resource.type.name == "group" and t.relation == "member"
        ]
        self.assertEqual(len(member_tuples), 0)
