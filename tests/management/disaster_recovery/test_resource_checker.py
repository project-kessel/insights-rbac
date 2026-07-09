"""Tests for disaster recovery resource existence checker."""

from uuid import uuid4

from django.test import TestCase

from api.models import Tenant
from management.disaster_recovery.resource_checker import (
    RESOURCE_TYPE_REGISTRY,
    _strip_domain_prefix,
    check_resources_exist,
)
from management.group.model import Group
from management.principal.model import Principal
from management.relation_replicator.types import (
    ObjectReference,
    ObjectType,
    RelationTuple,
    SubjectReference,
)
from management.role.model import Role
from management.tenant_mapping.model import TenantMapping
from management.workspace.model import Workspace


def _make_tuple(resource_type_name: str, resource_id: str) -> RelationTuple:
    return RelationTuple(
        resource=ObjectReference(
            type=ObjectType(namespace="rbac", name=resource_type_name),
            id=resource_id,
        ),
        relation="member",
        subject=SubjectReference(
            subject=ObjectReference(
                type=ObjectType(namespace="rbac", name="principal"),
                id="test-subject",
            ),
        ),
    )


class StripDomainPrefixTest(TestCase):
    def test_with_prefix(self):
        self.assertEqual(_strip_domain_prefix("localhost/user123"), "user123")

    def test_without_prefix(self):
        self.assertEqual(_strip_domain_prefix("user123"), "user123")

    def test_multiple_slashes(self):
        self.assertEqual(_strip_domain_prefix("some/nested/path"), "path")


class ResourceCheckerTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="test-dr-tenant",
            account_id="test-dr-account",
            org_id="test-dr-org",
            ready=True,
        )
        cls.root_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=cls.tenant,
        )
        cls.default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            tenant=cls.tenant,
            parent=cls.root_ws,
        )

    @classmethod
    def tearDownClass(cls):
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super().tearDownClass()

    def test_workspace_exists(self):
        ws = Workspace.objects.create(
            name="DR Test Workspace",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
        )
        t = _make_tuple("workspace", str(ws.id))
        result = check_resources_exist([t])
        self.assertTrue(result[("workspace", str(ws.id))])
        ws.delete()

    def test_workspace_not_found(self):
        fake_id = str(uuid4())
        t = _make_tuple("workspace", fake_id)
        result = check_resources_exist([t])
        self.assertFalse(result[("workspace", fake_id)])

    def test_role_exists(self):
        role = Role.objects.create(
            name="DR Test Role",
            tenant=self.tenant,
        )
        t = _make_tuple("role", str(role.uuid))
        result = check_resources_exist([t])
        self.assertTrue(result[("role", str(role.uuid))])
        role.delete()

    def test_group_exists(self):
        group = Group.objects.create(
            name="DR Test Group",
            tenant=self.tenant,
        )
        t = _make_tuple("group", str(group.uuid))
        result = check_resources_exist([t])
        self.assertTrue(result[("group", str(group.uuid))])
        group.delete()

    def test_principal_with_domain_prefix(self):
        principal = Principal.objects.create(
            username="dr-test-user",
            user_id="uid123",
            tenant=self.tenant,
        )
        t = _make_tuple("principal", "localhost/uid123")
        result = check_resources_exist([t])
        self.assertTrue(result[("principal", "localhost/uid123")])
        principal.delete()

    def test_principal_not_found(self):
        t = _make_tuple("principal", "localhost/nonexistent-uid")
        result = check_resources_exist([t])
        self.assertFalse(result[("principal", "localhost/nonexistent-uid")])

    def test_tenant_with_domain_prefix(self):
        t = _make_tuple("tenant", f"localhost/{self.tenant.org_id}")
        result = check_resources_exist([t])
        self.assertTrue(result[("tenant", f"localhost/{self.tenant.org_id}")])

    def test_unknown_type_returns_true(self):
        t = _make_tuple("unknown_resource", "some-id")
        result = check_resources_exist([t])
        self.assertTrue(result[("unknown_resource", "some-id")])

    def test_bulk_query_multiple_types(self):
        ws = Workspace.objects.create(
            name="DR Bulk Test",
            type=Workspace.Types.STANDARD,
            tenant=self.tenant,
        )
        group = Group.objects.create(
            name="DR Bulk Group",
            tenant=self.tenant,
        )
        fake_workspace_id = str(uuid4())

        tuples = [
            _make_tuple("workspace", str(ws.id)),
            _make_tuple("workspace", fake_workspace_id),
            _make_tuple("group", str(group.uuid)),
        ]
        result = check_resources_exist(tuples)

        self.assertTrue(result[("workspace", str(ws.id))])
        self.assertFalse(result[("workspace", fake_workspace_id)])
        self.assertTrue(result[("group", str(group.uuid))])

        ws.delete()
        group.delete()

    def test_empty_tuples(self):
        result = check_resources_exist([])
        self.assertEqual(result, {})

    def test_registry_covers_expected_types(self):
        expected = {"workspace", "role", "group", "principal", "role_binding", "tenant"}
        self.assertEqual(set(RESOURCE_TYPE_REGISTRY.keys()), expected)


class TenantMappingFallbackTest(TestCase):
    """Test that group/role_binding UUIDs from TenantMapping are found even without model instances."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="test-mapping-tenant",
            account_id="test-mapping-account",
            org_id="test-mapping-org",
            ready=True,
        )
        cls.root_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            type=Workspace.Types.ROOT,
            tenant=cls.tenant,
        )
        cls.default_ws = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            type=Workspace.Types.DEFAULT,
            tenant=cls.tenant,
            parent=cls.root_ws,
        )
        cls.mapping = TenantMapping.objects.create(tenant=cls.tenant)

    @classmethod
    def tearDownClass(cls):
        cls.mapping.delete()
        cls.default_ws.delete()
        cls.root_ws.delete()
        cls.tenant.delete()
        super().tearDownClass()

    def test_group_found_via_tenant_mapping_default_group(self):
        group_uuid = str(self.mapping.default_group_uuid)
        t = _make_tuple("group", group_uuid)
        result = check_resources_exist([t])
        self.assertTrue(result[("group", group_uuid)])

    def test_group_found_via_tenant_mapping_admin_group(self):
        group_uuid = str(self.mapping.default_admin_group_uuid)
        t = _make_tuple("group", group_uuid)
        result = check_resources_exist([t])
        self.assertTrue(result[("group", group_uuid)])

    def test_role_binding_found_via_tenant_mapping(self):
        rb_uuid = str(self.mapping.default_role_binding_uuid)
        t = _make_tuple("role_binding", rb_uuid)
        result = check_resources_exist([t])
        self.assertTrue(result[("role_binding", rb_uuid)])

    def test_role_binding_root_scope_found_via_tenant_mapping(self):
        rb_uuid = str(self.mapping.root_scope_default_role_binding_uuid)
        t = _make_tuple("role_binding", rb_uuid)
        result = check_resources_exist([t])
        self.assertTrue(result[("role_binding", rb_uuid)])

    def test_role_binding_tenant_scope_found_via_tenant_mapping(self):
        rb_uuid = str(self.mapping.tenant_scope_default_role_binding_uuid)
        t = _make_tuple("role_binding", rb_uuid)
        result = check_resources_exist([t])
        self.assertTrue(result[("role_binding", rb_uuid)])

    def test_role_binding_admin_found_via_tenant_mapping(self):
        rb_uuid = str(self.mapping.default_admin_role_binding_uuid)
        t = _make_tuple("role_binding", rb_uuid)
        result = check_resources_exist([t])
        self.assertTrue(result[("role_binding", rb_uuid)])

    def test_group_not_in_mapping_still_returns_false(self):
        fake_uuid = str(uuid4())
        t = _make_tuple("group", fake_uuid)
        result = check_resources_exist([t])
        self.assertFalse(result[("group", fake_uuid)])

    def test_role_binding_not_in_mapping_still_returns_false(self):
        fake_uuid = str(uuid4())
        t = _make_tuple("role_binding", fake_uuid)
        result = check_resources_exist([t])
        self.assertFalse(result[("role_binding", fake_uuid)])

    def test_mixed_real_and_virtual_resources(self):
        """Groups that exist as model instances and groups from TenantMapping both return True."""
        real_group = Group.objects.create(name="Real DR Group", tenant=self.tenant)
        virtual_group_uuid = str(self.mapping.default_group_uuid)
        fake_group_uuid = str(uuid4())

        tuples = [
            _make_tuple("group", str(real_group.uuid)),
            _make_tuple("group", virtual_group_uuid),
            _make_tuple("group", fake_group_uuid),
        ]
        result = check_resources_exist(tuples)

        self.assertTrue(result[("group", str(real_group.uuid))])
        self.assertTrue(result[("group", virtual_group_uuid)])
        self.assertFalse(result[("group", fake_group_uuid)])
        real_group.delete()

    def test_bootstrap_scenario_all_virtual_resources(self):
        """Simulate a full bootstrap event: group, role_binding, workspace all checked."""
        tuples = [
            _make_tuple("workspace", str(self.root_ws.id)),
            _make_tuple("group", str(self.mapping.default_group_uuid)),
            _make_tuple("group", str(self.mapping.default_admin_group_uuid)),
            _make_tuple("role_binding", str(self.mapping.default_role_binding_uuid)),
            _make_tuple("role_binding", str(self.mapping.default_admin_role_binding_uuid)),
            _make_tuple("role_binding", str(self.mapping.root_scope_default_role_binding_uuid)),
            _make_tuple("role_binding", str(self.mapping.root_scope_default_admin_role_binding_uuid)),
            _make_tuple("role_binding", str(self.mapping.tenant_scope_default_role_binding_uuid)),
            _make_tuple("role_binding", str(self.mapping.tenant_scope_default_admin_role_binding_uuid)),
        ]
        result = check_resources_exist(tuples)

        for key, exists in result.items():
            self.assertTrue(exists, f"Expected {key} to exist")
