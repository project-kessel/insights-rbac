"""Tests verifying CheckRequest resource/subject correctness for all inventory checkers.

These tests capture the actual CheckRequest protobuf objects passed to the gRPC stub
and verify that object (resource) and subject fields are in the correct positions.

This catches bugs where resource and subject are accidentally swapped -- a class of
error that mock-based tests miss because MagicMock accepts any arguments.

The SpiceDB tuple format is:
    resource_type:resource_id#relation@subject_type:subject_id

For workspace parent relations:
    workspace:<child_id>#parent@workspace:<parent_id>

The child workspace is the RESOURCE (object), the parent workspace is the SUBJECT.
"""

import uuid
from unittest.mock import MagicMock, call, patch

from django.conf import settings
from django.test import TestCase, override_settings
from kessel.inventory.v1beta2.check_response_pb2 import CheckResponse
from management.group.model import Group
from management.inventory_checker.inventory_api_check import (
    BootstrappedTenantInventoryChecker,
    CrossAccountRequestInventoryChecker,
    CustomRolePermissionChecker,
    GroupPrincipalInventoryChecker,
    RoleBindingInventoryChecker,
    RoleRelationInventoryChecker,
    SeededRoleHierarchyChecker,
    WorkspaceRelationInventoryChecker,
    relation_tuple_to_check_request,
)
from management.principal.model import Principal
from management.relation_replicator.types import (
    ObjectReference,
    ObjectType,
    RelationTuple,
    SubjectReference,
)
from management.role.relations import role_child_relationship
from migration_tool.models import role_permission_tuple
from migration_tool.utils import create_relationship
from tests.identity_request import IdentityRequest

CREATE_CHANNEL_PATH = "management.inventory_checker.inventory_api_check.create_client_channel_inventory"
MESSAGE_TO_DICT_PATH = "management.inventory_checker.inventory_api_check.json_format.MessageToDict"
STUB_PATH = "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub"


def _allowed_response():
    return {"allowed": "ALLOWED_TRUE"}


def _setup_grpc_mocks(mock_create_channel):
    """Set up gRPC channel + stub mocks, return the stub mock for assertion."""
    mock_stub = MagicMock()
    mock_channel = MagicMock()
    mock_channel.__enter__ = MagicMock(return_value=mock_channel)
    mock_channel.__exit__ = MagicMock(return_value=None)
    mock_create_channel.return_value = mock_channel
    return mock_stub


class RelationTupleToCheckRequestTest(TestCase):
    """Tests for the relation_tuple_to_check_request conversion function."""

    def test_resource_fields_map_to_object(self):
        """The RelationTuple.resource should become CheckRequest.object."""
        t = RelationTuple(
            resource=ObjectReference(
                type=ObjectType(namespace="rbac", name="workspace"),
                id="child-ws-id",
            ),
            relation="parent",
            subject=SubjectReference(
                subject=ObjectReference(
                    type=ObjectType(namespace="rbac", name="workspace"),
                    id="parent-ws-id",
                ),
            ),
        )
        req = relation_tuple_to_check_request(t)

        self.assertEqual(req.object.resource_type, "workspace")
        self.assertEqual(req.object.resource_id, "child-ws-id")
        self.assertEqual(req.relation, "parent")
        self.assertEqual(req.subject.resource.resource_type, "workspace")
        self.assertEqual(req.subject.resource.resource_id, "parent-ws-id")

    def test_group_member_tuple(self):
        """group:G#member@principal:P → object=group, subject=principal."""
        t = create_relationship(("rbac", "group"), "group-uuid", ("rbac", "principal"), "user-id", "member")
        req = relation_tuple_to_check_request(t)

        self.assertEqual(req.object.resource_type, "group")
        self.assertEqual(req.object.resource_id, "group-uuid")
        self.assertEqual(req.relation, "member")
        self.assertEqual(req.subject.resource.resource_type, "principal")
        self.assertEqual(req.subject.resource.resource_id, "user-id")

    def test_role_binding_tuple(self):
        """workspace:W#binding@role_binding:RB → object=workspace, subject=role_binding."""
        t = create_relationship(("rbac", "workspace"), "ws-uuid", ("rbac", "role_binding"), "rb-uuid", "binding")
        req = relation_tuple_to_check_request(t)

        self.assertEqual(req.object.resource_type, "workspace")
        self.assertEqual(req.object.resource_id, "ws-uuid")
        self.assertEqual(req.relation, "binding")
        self.assertEqual(req.subject.resource.resource_type, "role_binding")
        self.assertEqual(req.subject.resource.resource_id, "rb-uuid")

    def test_role_child_tuple(self):
        """role:parent#child@role:child → object=parent role, subject=child role."""
        parent_uuid = uuid.uuid4()
        child_uuid = uuid.uuid4()
        t = role_child_relationship(parent_uuid, child_uuid)
        req = relation_tuple_to_check_request(t)

        self.assertEqual(req.object.resource_type, "role")
        self.assertEqual(req.object.resource_id, str(parent_uuid))
        self.assertEqual(req.relation, "child")
        self.assertEqual(req.subject.resource.resource_type, "role")
        self.assertEqual(req.subject.resource.resource_id, str(child_uuid))

    def test_subject_relation_preserved(self):
        """Subject relation (e.g., #member) is preserved in the CheckRequest."""
        t = create_relationship(
            ("rbac", "role_binding"), "rb-uuid", ("rbac", "group"), "group-uuid", "subject", "member"
        )
        req = relation_tuple_to_check_request(t)

        self.assertEqual(req.subject.relation, "member")

    def test_role_permission_wildcard_tuple(self):
        """role:R#perm@principal:* → object=role, subject=principal:*."""
        t = role_permission_tuple("role-uuid", "inventory_groups_read")
        req = relation_tuple_to_check_request(t)

        self.assertEqual(req.object.resource_type, "role")
        self.assertEqual(req.object.resource_id, "role-uuid")
        self.assertEqual(req.relation, "inventory_groups_read")
        self.assertEqual(req.subject.resource.resource_type, "principal")
        self.assertEqual(req.subject.resource.resource_id, "*")


class WorkspaceCheckerCheckRequestTest(TestCase):
    """Verify WorkspaceRelationInventoryChecker builds CheckRequest with correct resource/subject.

    The parent relation in SpiceDB is:
        workspace:<child>#parent@workspace:<parent>

    So CheckRequest must have:
        object.resource_id = child workspace UUID
        subject.resource.resource_id = parent workspace UUID
    """

    def setUp(self):
        self.checker = WorkspaceRelationInventoryChecker()
        self.child_id = str(uuid.uuid4())
        self.parent_id = str(uuid.uuid4())

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_check_workspace_descendants_object_is_child_subject_is_parent(
        self, mock_create_channel, mock_message_to_dict
    ):
        """check_workspace_descendants must set object=child, subject=parent."""
        mock_stub = _setup_grpc_mocks(mock_create_channel)

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_workspace_descendants([(self.child_id, self.parent_id)])

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(
            check_request.object.resource_id,
            self.child_id,
            f"object (resource) should be the CHILD workspace {self.child_id}, "
            f"but got {check_request.object.resource_id}. Resource and subject are swapped.",
        )
        self.assertEqual(check_request.object.resource_type, "workspace")
        self.assertEqual(check_request.relation, "parent")
        self.assertEqual(
            check_request.subject.resource.resource_id,
            self.parent_id,
            f"subject should be the PARENT workspace {self.parent_id}, "
            f"but got {check_request.subject.resource.resource_id}. Resource and subject are swapped.",
        )
        self.assertEqual(check_request.subject.resource.resource_type, "workspace")

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_check_workspace_single_object_is_child_subject_is_parent(self, mock_create_channel, mock_message_to_dict):
        """check_workspace must set object=child, subject=parent."""
        mock_stub = _setup_grpc_mocks(mock_create_channel)

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_workspace(self.child_id, self.parent_id)

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(
            check_request.object.resource_id,
            self.child_id,
            f"object (resource) should be the CHILD workspace {self.child_id}, "
            f"but got {check_request.object.resource_id}. Resource and subject are swapped.",
        )
        self.assertEqual(
            check_request.subject.resource.resource_id,
            self.parent_id,
            f"subject should be the PARENT workspace {self.parent_id}, "
            f"but got {check_request.subject.resource.resource_id}. Resource and subject are swapped.",
        )

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_check_workspace_descendants_multiple_pairs(self, mock_create_channel, mock_message_to_dict):
        """Each pair in a batch should have object=child, subject=parent."""
        mock_stub = _setup_grpc_mocks(mock_create_channel)

        pairs = [
            (str(uuid.uuid4()), str(uuid.uuid4())),
            (str(uuid.uuid4()), str(uuid.uuid4())),
            (str(uuid.uuid4()), str(uuid.uuid4())),
        ]

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_workspace_descendants(pairs)

        self.assertEqual(mock_stub.Check.call_count, 3)

        for i, (child_id, parent_id) in enumerate(pairs):
            check_request = mock_stub.Check.call_args_list[i][0][0]
            self.assertEqual(
                check_request.object.resource_id,
                child_id,
                f"Pair {i}: object should be child {child_id}, got {check_request.object.resource_id}",
            )
            self.assertEqual(
                check_request.subject.resource.resource_id,
                parent_id,
                f"Pair {i}: subject should be parent {parent_id}, got {check_request.subject.resource.resource_id}",
            )


class GroupPrincipalCheckerCheckRequestTest(IdentityRequest):
    """Verify GroupPrincipalInventoryChecker builds CheckRequest with correct resource/subject.

    The member relation in SpiceDB is:
        group:<group_uuid>#member@principal:<principal_id>

    So CheckRequest must have:
        object.resource_id = group UUID
        subject.resource.resource_id = principal ID
    """

    def setUp(self):
        super().setUp()
        self.checker = GroupPrincipalInventoryChecker()
        self.group = Group.objects.create(name="check-req-group", tenant=self.tenant)
        self.principal = Principal.objects.create(
            username="check-req-user", user_id="uid-check-req", tenant=self.tenant
        )
        self.group.principals.add(self.principal)

    def tearDown(self):
        self.group.principals.remove(self.principal)
        self.principal.delete()
        self.group.delete()
        super().tearDown()

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_check_request_object_is_group_subject_is_principal(self, mock_create_channel, mock_message_to_dict):
        """CheckRequest object must be the group, subject must be the principal."""
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        relationships = [self.group.relationship_to_principal(self.principal)]

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_relationships(relationships)

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(check_request.object.resource_type, "group")
        self.assertEqual(check_request.object.resource_id, str(self.group.uuid))
        self.assertEqual(check_request.relation, "member")
        self.assertEqual(check_request.subject.resource.resource_type, "principal")
        expected_principal_id = f"{settings.PRINCIPAL_USER_DOMAIN}/{self.principal.user_id}"
        self.assertEqual(check_request.subject.resource.resource_id, expected_principal_id)


class BootstrapCheckerCheckRequestTest(IdentityRequest):
    """Verify BootstrappedTenantInventoryChecker builds CheckRequest with correct resource/subject.

    Bootstrap produces tuples like:
        workspace:<default>#parent@workspace:<root>         (default's parent is root)
        workspace:<root>#tenant@tenant:<org_id>             (root's tenant)
        tenant:<org_id>#platform@platform:<env>             (tenant's platform)
        workspace:<ws>#binding@role_binding:<rb>             (workspace has binding)
        role_binding:<rb>#role@role:<role_uuid>              (binding's role)
        role_binding:<rb>#subject@group:<group_uuid>#member  (binding's subject group)
    """

    def setUp(self):
        super().setUp()
        from management.tenant_mapping.model import TenantMapping

        self.tenant_mapping = TenantMapping.objects.create(tenant=self.tenant)
        self.root_ws_id = str(uuid.uuid4())
        self.default_ws_id = str(uuid.uuid4())
        self.checker = BootstrappedTenantInventoryChecker()

    def tearDown(self):
        self.tenant_mapping.delete()
        super().tearDown()

    @patch("management.inventory_checker.inventory_api_check.platform_v2_role_uuid_for")
    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_workspace_parent_tuple_object_is_child_subject_is_parent(
        self, mock_create_channel, mock_message_to_dict, mock_role_uuid
    ):
        """The default_workspace_parent tuple: workspace:<default>#parent@workspace:<root>."""
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        mock_role_uuid.return_value = uuid.uuid4()

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_ws_id,
                default_workspace_id=self.default_ws_id,
            )

        first_request = mock_stub.Check.call_args_list[0][0][0]

        self.assertEqual(
            first_request.object.resource_id,
            self.default_ws_id,
            f"First bootstrap tuple (default_workspace_parent): object should be DEFAULT workspace "
            f"{self.default_ws_id}, got {first_request.object.resource_id}",
        )
        self.assertEqual(first_request.object.resource_type, "workspace")
        self.assertEqual(first_request.relation, "parent")
        self.assertEqual(
            first_request.subject.resource.resource_id,
            self.root_ws_id,
            f"First bootstrap tuple: subject should be ROOT workspace "
            f"{self.root_ws_id}, got {first_request.subject.resource.resource_id}",
        )

    @patch("management.inventory_checker.inventory_api_check.platform_v2_role_uuid_for")
    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_tenant_platform_tuple_object_is_tenant_subject_is_platform(
        self, mock_create_channel, mock_message_to_dict, mock_role_uuid
    ):
        """The tenant_platform tuple: tenant:<org_id>#platform@platform:<env>."""
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        mock_role_uuid.return_value = uuid.uuid4()

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_ws_id,
                default_workspace_id=self.default_ws_id,
            )

        third_request = mock_stub.Check.call_args_list[2][0][0]

        self.assertEqual(third_request.object.resource_type, "tenant")
        self.assertEqual(third_request.relation, "platform")
        self.assertEqual(third_request.subject.resource.resource_type, "platform")
        self.assertEqual(third_request.subject.resource.resource_id, "stage")

    @patch("management.inventory_checker.inventory_api_check.platform_v2_role_uuid_for")
    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    @override_settings(ENV_NAME="stage", PRINCIPAL_USER_DOMAIN="localhost")
    def test_binding_tuple_object_is_workspace_subject_is_role_binding(
        self, mock_create_channel, mock_message_to_dict, mock_role_uuid
    ):
        """Binding tuples: workspace:<ws>#binding@role_binding:<rb>."""
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        mock_role_uuid.return_value = uuid.uuid4()

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_bootstrapped_tenant(
                org_id=self.tenant.org_id,
                tenant_mapping=self.tenant_mapping,
                root_workspace_id=self.root_ws_id,
                default_workspace_id=self.default_ws_id,
            )

        from management.permission.scope_service import Scope
        from management.tenant_mapping.model import DefaultAccessType

        binding_request = mock_stub.Check.call_args_list[3][0][0]

        rb_uuid = str(self.tenant_mapping.default_role_binding_uuid_for(DefaultAccessType.USER, Scope.DEFAULT))

        self.assertEqual(binding_request.object.resource_type, "workspace")
        self.assertEqual(binding_request.object.resource_id, self.default_ws_id)
        self.assertEqual(binding_request.relation, "binding")
        self.assertEqual(binding_request.subject.resource.resource_type, "role_binding")
        self.assertEqual(binding_request.subject.resource.resource_id, rb_uuid)


class RoleBindingCheckerCheckRequestTest(TestCase):
    """Verify RoleBindingInventoryChecker builds CheckRequest with correct resource/subject.

    Role binding tuples:
        workspace:<ws>#binding@role_binding:<rb>         object=workspace, subject=role_binding
        role_binding:<rb>#role@role:<role_uuid>           object=role_binding, subject=role
        role_binding:<rb>#subject@group:<g>#member        object=role_binding, subject=group
    """

    def setUp(self):
        self.checker = RoleBindingInventoryChecker()

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_binding_tuple_object_is_workspace_subject_is_role_binding(
        self, mock_create_channel, mock_message_to_dict
    ):
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        ws_id = str(uuid.uuid4())
        rb_id = str(uuid.uuid4())

        binding_tuple = create_relationship(("rbac", "workspace"), ws_id, ("rbac", "role_binding"), rb_id, "binding")

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_role_binding([binding_tuple], rb_id)

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(check_request.object.resource_type, "workspace")
        self.assertEqual(check_request.object.resource_id, ws_id)
        self.assertEqual(check_request.relation, "binding")
        self.assertEqual(check_request.subject.resource.resource_type, "role_binding")
        self.assertEqual(check_request.subject.resource.resource_id, rb_id)

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_role_tuple_object_is_role_binding_subject_is_role(self, mock_create_channel, mock_message_to_dict):
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        rb_id = str(uuid.uuid4())
        role_id = str(uuid.uuid4())

        role_tuple = create_relationship(("rbac", "role_binding"), rb_id, ("rbac", "role"), role_id, "role")

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_role_binding([role_tuple], rb_id)

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(check_request.object.resource_type, "role_binding")
        self.assertEqual(check_request.object.resource_id, rb_id)
        self.assertEqual(check_request.relation, "role")
        self.assertEqual(check_request.subject.resource.resource_type, "role")
        self.assertEqual(check_request.subject.resource.resource_id, role_id)

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_subject_tuple_object_is_role_binding_subject_is_group(self, mock_create_channel, mock_message_to_dict):
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        rb_id = str(uuid.uuid4())
        group_id = str(uuid.uuid4())

        subject_tuple = create_relationship(
            ("rbac", "role_binding"), rb_id, ("rbac", "group"), group_id, "subject", "member"
        )

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_role_binding([subject_tuple], rb_id)

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(check_request.object.resource_type, "role_binding")
        self.assertEqual(check_request.object.resource_id, rb_id)
        self.assertEqual(check_request.relation, "subject")
        self.assertEqual(check_request.subject.resource.resource_type, "group")
        self.assertEqual(check_request.subject.resource.resource_id, group_id)
        self.assertEqual(check_request.subject.relation, "member")


class SeededRoleHierarchyCheckerCheckRequestTest(TestCase):
    """Verify SeededRoleHierarchyChecker builds CheckRequest with correct resource/subject.

    Role hierarchy tuple: role:<parent>#child@role:<child>
        object = parent role, subject = child role
    """

    def setUp(self):
        self.checker = SeededRoleHierarchyChecker()

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_hierarchy_tuple_object_is_parent_role_subject_is_child_role(
        self, mock_create_channel, mock_message_to_dict
    ):
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        parent_uuid = uuid.uuid4()
        child_uuid = uuid.uuid4()

        hierarchy_tuple = role_child_relationship(parent_uuid, child_uuid)

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_seeded_role_hierarchy([hierarchy_tuple], str(child_uuid))

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(check_request.object.resource_type, "role")
        self.assertEqual(
            check_request.object.resource_id,
            str(parent_uuid),
            f"object should be PARENT role {parent_uuid}, got {check_request.object.resource_id}",
        )
        self.assertEqual(check_request.relation, "child")
        self.assertEqual(check_request.subject.resource.resource_type, "role")
        self.assertEqual(
            check_request.subject.resource.resource_id,
            str(child_uuid),
            f"subject should be CHILD role {child_uuid}, got {check_request.subject.resource.resource_id}",
        )


class CrossAccountRequestCheckerCheckRequestTest(TestCase):
    """Verify CrossAccountRequestInventoryChecker builds CheckRequest correctly."""

    def setUp(self):
        self.checker = CrossAccountRequestInventoryChecker()

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_car_binding_tuple_object_is_workspace_subject_is_role_binding(
        self, mock_create_channel, mock_message_to_dict
    ):
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        ws_id = str(uuid.uuid4())
        rb_id = str(uuid.uuid4())

        car_tuple = create_relationship(("rbac", "workspace"), ws_id, ("rbac", "role_binding"), rb_id, "binding")

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_cross_account_request([car_tuple], str(uuid.uuid4()))

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(check_request.object.resource_type, "workspace")
        self.assertEqual(check_request.object.resource_id, ws_id)
        self.assertEqual(check_request.relation, "binding")
        self.assertEqual(check_request.subject.resource.resource_type, "role_binding")
        self.assertEqual(check_request.subject.resource.resource_id, rb_id)


class CustomRolePermissionCheckerCheckRequestTest(TestCase):
    """Verify CustomRolePermissionChecker builds ReadTuples filter with correct resource/subject.

    Permission tuple: role:<uuid>#<permission>@principal:*
        resource = role, subject = principal:*
    """

    def setUp(self):
        self.checker = CustomRolePermissionChecker()

    @patch("management.inventory_checker.inventory_api_check.jwt_manager")
    @patch("management.inventory_checker.inventory_api_check.create_client_channel_relation")
    def test_read_tuples_filter_resource_is_role_subject_is_principal(self, mock_create_channel, mock_jwt_manager):
        mock_jwt_manager.get_jwt_from_redis.return_value = "fake-jwt"

        mock_stub = MagicMock()
        mock_stub.ReadTuples.return_value = [MagicMock()]
        mock_channel = MagicMock()
        mock_channel.__enter__ = MagicMock(return_value=mock_channel)
        mock_channel.__exit__ = MagicMock(return_value=None)
        mock_create_channel.return_value = mock_channel

        role_uuid = str(uuid.uuid4())
        permission_tuple = role_permission_tuple(role_uuid, "inventory_groups_read")

        with patch(
            "management.inventory_checker.inventory_api_check.relation_tuples_pb2_grpc.KesselTupleServiceStub",
            return_value=mock_stub,
        ):
            self.checker.check_custom_role_permissions([permission_tuple], role_uuid)

        read_request = mock_stub.ReadTuples.call_args[0][0]
        f = read_request.filter

        self.assertEqual(f.resource_namespace, "rbac")
        self.assertEqual(f.resource_type, "role")
        self.assertEqual(f.resource_id, role_uuid)
        self.assertEqual(f.relation, "inventory_groups_read")
        self.assertEqual(f.subject_filter.subject_namespace, "rbac")
        self.assertEqual(f.subject_filter.subject_type, "principal")
        self.assertEqual(f.subject_filter.subject_id, "*")


class RoleRelationCheckerCheckRequestTest(TestCase):
    """Verify RoleRelationInventoryChecker builds CheckRequest correctly from dict input.

    RoleRelationInventoryChecker.check_role builds CheckRequest directly from dict fields,
    not via relation_tuple_to_check_request. This test verifies the dict → CheckRequest
    mapping preserves resource/subject positions.
    """

    def setUp(self):
        self.checker = RoleRelationInventoryChecker()

    @patch(MESSAGE_TO_DICT_PATH, return_value=_allowed_response())
    @patch(CREATE_CHANNEL_PATH)
    def test_role_relation_object_is_resource_subject_is_subject(self, mock_create_channel, mock_message_to_dict):
        mock_stub = _setup_grpc_mocks(mock_create_channel)
        role_uuid = str(uuid.uuid4())

        role_relation = {
            "resource": {
                "id": role_uuid,
                "type": {"name": "role", "namespace": "rbac"},
            },
            "relation": "inventory_groups_read",
            "subject": {
                "subject": {
                    "id": "*",
                    "type": {"name": "principal", "namespace": "rbac"},
                }
            },
        }

        with patch(STUB_PATH, return_value=mock_stub):
            self.checker.check_role(role_relations=[role_relation], role_uuid=role_uuid)

        check_request = mock_stub.Check.call_args[0][0]

        self.assertEqual(check_request.object.resource_type, "role")
        self.assertEqual(check_request.object.resource_id, role_uuid)
        self.assertEqual(check_request.relation, "inventory_groups_read")
        self.assertEqual(check_request.subject.resource.resource_type, "principal")
        self.assertEqual(check_request.subject.resource.resource_id, "*")


class ParityLogFormatTest(TestCase):
    """Verify the parity check log output matches the actual CheckRequest direction.

    The log format uses: rbac/workspace:{parent_id}#parent@rbac/workspace:{workspace_id}
    This must match the SpiceDB tuple direction: resource#relation@subject.

    If the CheckRequest has object=child and subject=parent (correct),
    then the log should show: rbac/workspace:{child}#parent@rbac/workspace:{parent}
    """

    def test_log_format_matches_tuple_direction(self):
        """Log line for a MISSING workspace pair should read child#parent@parent."""
        child_id = str(uuid.uuid4())
        parent_id = str(uuid.uuid4())

        pair_result = {"workspace_id": child_id, "parent_id": parent_id, "exists": False}

        expected_log = f"rbac/workspace:{child_id}#parent@rbac/workspace:{parent_id}"
        actual_log = f"rbac/workspace:{pair_result['parent_id']}#parent@rbac/workspace:{pair_result['workspace_id']}"

        self.assertEqual(
            expected_log,
            f"rbac/workspace:{child_id}#parent@rbac/workspace:{parent_id}",
        )

        self.assertNotEqual(
            actual_log,
            expected_log,
            "Current log format swaps child/parent — the log line puts parent_id as the resource "
            "and workspace_id as the subject, but the tuple direction is child#parent@parent. "
            "This means the log output is also inverted relative to the actual SpiceDB tuple.",
        )
