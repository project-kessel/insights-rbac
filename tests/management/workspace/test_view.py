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
"""Test the Audit Logs Model."""

import json
import random
import string

from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django.test.utils import override_settings
from django.urls import clear_url_caches
from importlib import reload
from psycopg2.errors import DeadlockDetected, SerializationFailure
from unittest.mock import patch
from django.urls import reverse
from management.audit_log.model import AuditLog
from management.models import Access, Group, Permission, Policy, Principal, ResourceDefinition, Role, Workspace
from rest_framework import status
from rest_framework.test import APIClient
from management.permissions.workspace_access import TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.workspace.serializer import WorkspaceEventSerializer
from management.workspace.service import WorkspaceService
from migration_tool.in_memory_tuples import (
    all_of,
    InMemoryRelationReplicator,
    InMemoryTuples,
    relation,
    resource,
    subject,
)
from migration_tool.utils import create_relationship
from api.models import Tenant
from rbac import urls
from tests.identity_request import IdentityRequest, TransactionalIdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test


class BasicWorkspaceViewTests:
    def _get_random_name(self, length=10):
        return "".join(random.choices(string.ascii_letters + string.digits, k=length))

    def _setup_access_for_principal(self, username, permission, workspace_id=None, platform_default=False):
        group = Group(name=self._get_random_name(), platform_default=platform_default, tenant=self.tenant)
        group.save()
        role = Role.objects.create(
            name="".join(random.choices(string.ascii_letters + string.digits, k=5)),
            description="A role for a group.",
            tenant=self.tenant,
        )
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        permission, _ = Permission.objects.get_or_create(permission=permission, tenant=public_tenant)
        access = Access.objects.create(permission=permission, role=role, tenant=self.tenant)
        if workspace_id:
            operation = "in" if isinstance(workspace_id, list) else "equal"
            ResourceDefinition.objects.create(
                attributeFilter={
                    "key": "group.id",
                    "operation": operation,
                    "value": workspace_id,
                },
                access=access,
                tenant=self.tenant,
            )

        policy = Policy.objects.create(name=self._get_random_name(), group=group, tenant=self.tenant)
        policy.roles.add(role)
        policy.save()
        group.policies.add(policy)
        group.save()
        if not platform_default:
            principal, _ = Principal.objects.get_or_create(username=username, tenant=self.tenant)
            group.principals.add(principal)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class WorkspaceViewTests(IdentityRequest, BasicWorkspaceViewTests):
    """Test the Workspace view."""

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=10)
    def setUp(self):
        """Set up the workspace model tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        self.service = WorkspaceService()

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

        self.ungrouped_workspace = Workspace.objects.create(
            name="Ungrouped Hosts Workspace",
            description="Ungrouped Hosts Workspace - description",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.UNGROUPED_HOSTS,
        )
        validated_data_standard_ws = {
            "name": "Standard Workspace",
            "description": "Standard Workspace - description",
            "parent_id": self.default_workspace.id,
        }
        self.standard_workspace = self.service.create(validated_data_standard_ws, self.tenant)
        validated_data_standard_sub_ws = {
            "name": "Standard Sub-workspace",
            "description": "Standard Workspace with another standard workspace parent.",
            "parent_id": self.standard_workspace.id,
        }
        self.standard_sub_workspace = self.service.create(validated_data_standard_sub_ws, self.tenant)

    def tearDown(self):
        """Tear down group model tests."""
        from management.utils import PRINCIPAL_CACHE

        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()
        # Clear principal cache to avoid test isolation issues
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class TransactionalWorkspaceViewTests(TransactionalIdentityRequest, BasicWorkspaceViewTests):
    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=10)
    def setUp(self):
        """Set up the workspace model tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        self.service = WorkspaceService()

        with transaction.atomic():
            bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
            self.default_workspace = bootstrap_result.default_workspace
            self.root_workspace = bootstrap_result.root_workspace

        self.ungrouped_workspace = Workspace.objects.create(
            name="Ungrouped Hosts Workspace",
            description="Ungrouped Hosts Workspace - description",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.UNGROUPED_HOSTS,
        )
        validated_data_standard_ws = {
            "name": "Standard Workspace",
            "description": "Standard Workspace - description",
            "parent_id": self.default_workspace.id,
        }
        self.standard_workspace = self.service.create(validated_data_standard_ws, self.tenant)
        validated_data_standard_sub_ws = {
            "name": "Standard Sub-workspace",
            "description": "Standard Workspace with another standard workspace parent.",
            "parent_id": self.standard_workspace.id,
        }
        self.standard_sub_workspace = self.service.create(validated_data_standard_sub_ws, self.tenant)

    def tearDown(self):
        """Tear down workspace model tests."""
        from management.utils import PRINCIPAL_CACHE

        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()
        # Clear principal cache to avoid test isolation issues
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)


@override_settings(
    ATOMIC_RETRY_DISABLED=True,
    V2_APIS_ENABLED=True,
    WORKSPACE_HIERARCHY_DEPTH_LIMIT=100,
    WORKSPACE_RESTRICT_DEFAULT_PEERS=False,
)
class WorkspaceTestsCreateUpdateDelete(TransactionalWorkspaceViewTests):
    """Tests for create/update/delete workspaces."""

    def setUp(self):
        super().setUp()
        self.tuples = InMemoryTuples()
        self.in_memory_replicator = InMemoryRelationReplicator(self.tuples)

        # Patch get_queryset to not use select_for_update during tests
        # SERIALIZABLE isolation level already provides necessary locking
        from unittest.mock import patch
        from management.workspace.view import WorkspaceViewSet

        original_get_queryset = WorkspaceViewSet.get_queryset

        def get_queryset_without_lock(self):
            # Return queryset without select_for_update to avoid transaction requirement during permission checks
            from management.base_viewsets import BaseV2ViewSet

            return BaseV2ViewSet.get_queryset(self)

        self.patcher = patch.object(WorkspaceViewSet, "get_queryset", get_queryset_without_lock)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        super().tearDown()

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate_workspace")
    def test_create_workspace(self, replicate_workspace, replicate):
        """Test for creating a workspace."""
        replicate.side_effect = self.in_memory_replicator.replicate
        workspace = {"name": "New Workspace", "description": "Workspace", "parent_id": self.default_workspace.id}

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data
        self.assertEqual(data.get("name"), "New Workspace")
        self.assertNotEqual(data.get("id"), "")
        self.assertIsNotNone(data.get("id"))
        self.assertNotEqual(data.get("created"), "")
        self.assertNotEqual(data.get("modified"), "")
        self.assertEqual(data.get("description"), "Workspace")
        self.assertEqual(data.get("type"), "standard")
        self.assertEqual(response.get("content-type"), "application/json")
        tuples = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", data.get("id")),
                relation("parent"),
                subject("rbac", "workspace", str(self.default_workspace.id)),
            )
        )
        self.assertEqual(len(tuples), 1)
        workspace_event = replicate_workspace.call_args[0][0]
        self.assertEqual(workspace_event.account_number, self.tenant.account_id)
        self.assertEqual(workspace_event.org_id, self.tenant.org_id)
        self.assertEqual(workspace_event.event_type, ReplicationEventType.CREATE_WORKSPACE)
        data.pop("description")
        data.pop("parent_id")
        self.assertEqual(workspace_event.workspace, data)

    def test_create_workspace_against_root(self):
        """Test for creating a workspace against the root."""
        workspace = {"name": "Root Peer", "description": "Workspace", "parent_id": self.root_workspace.id}

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data
        self.assertEqual(data.get("name"), "Root Peer")
        self.assertNotEqual(data.get("id"), "")
        self.assertIsNotNone(data.get("id"))
        self.assertNotEqual(data.get("created"), "")
        self.assertNotEqual(data.get("modified"), "")
        self.assertEqual(data.get("description"), "Workspace")
        self.assertEqual(data.get("type"), "standard")
        self.assertEqual(response.get("content-type"), "application/json")

    def test_create_workspace_assign_parent_id(self):
        """Test for creating a workspace without parent id."""
        workspace = {
            "name": "New Workspace",
            "description": "Workspace",
        }

        url = reverse("v2_management:workspace-list")
        client = APIClient()

        response = client.post(url, workspace, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data

        self.assertEqual(data.get("name"), "New Workspace")
        self.assertNotEqual(data.get("id"), "")
        self.assertNotEqual(data.get("parent_id"), data.get("id"))
        self.assertIsNotNone(data.get("id"))
        self.assertNotEqual(data.get("created"), "")
        self.assertNotEqual(data.get("modified"), "")
        self.assertEqual(data.get("description"), "Workspace")
        self.assertEqual(data.get("type"), "standard")
        self.assertEqual(response.get("content-type"), "application/json")

    def test_create_workspace_empty_body(self):
        """Test for creating a workspace."""
        workspace = {}

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        status_code = response.data.get("status")
        detail = response.data.get("detail")
        self.assertIsNotNone(detail)
        self.assertEqual(detail, "This field is required.")

        self.assertEqual(status_code, 400)
        self.assertEqual(response.get("content-type"), "application/problem+json")

    def test_create_workspace_unauthorized(self):
        """Test for creating a workspace."""
        workspace = {}

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        status_code = response.data.get("status")
        detail = response.data.get("detail")
        self.assertEqual(detail, "You do not have permission to perform this action.")
        self.assertEqual(status_code, 403)
        self.assertEqual(response.get("content-type"), "application/problem+json")

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_create_workspace_authorized_through_custom_role(self, send_kafka_message):
        """Test for creating a workspace."""
        workspace = {
            "name": "New Workspace",
            "description": "Workspace",
        }

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META
        self._setup_access_for_principal(self.user_data["username"], "inventory:groups:read")

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self._setup_access_for_principal(self.user_data["username"], "inventory:groups:write")
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], workspace["name"])

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_create_workspace_authorized_through_platform_default_access(self, send_kafka_message):
        """Test for creating a workspace."""
        workspace = {
            "name": "New Workspace",
            "description": "Workspace",
        }

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META
        self._setup_access_for_principal(self.user_data["username"], "inventory:groups:read", platform_default=True)

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self._setup_access_for_principal(self.user_data["username"], "inventory:groups:write", platform_default=True)
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], workspace["name"])

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_create_workspace_authorized_through_platform_default_access_with_wildcard(self, send_kafka_message):
        """Test for creating a workspace."""
        workspace = {
            "name": "New Workspace",
            "description": "Workspace",
        }

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META

        self._setup_access_for_principal(self.user_data["username"], "inventory:*:write", platform_default=True)
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], workspace["name"])

        # Make sure "inventory:*:*" also works
        Group.objects.get(platform_default=True).delete()
        Workspace.objects.get(id=response.data["id"]).delete()
        response = client.post(url, workspace, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self._setup_access_for_principal(self.user_data["username"], "inventory:*:*", platform_default=True)
        response = client.post(url, workspace, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], workspace["name"])

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_create_workspace_authorized_with_default_workspace_permission_only(self, send_kafka_message):
        """Test for creating a workspace with only Default Workspace permission."""
        workspace = {
            "name": "New Workspace Default Only",
            "description": "Workspace created with Default Workspace permission only",
        }

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META

        # Set up access for Default Workspace only (not Root Workspace)
        self._setup_access_for_principal(
            self.user_data["username"], "inventory:groups:write", workspace_id=str(self.default_workspace.id)
        )

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)

        # This should now succeed with our permission fix
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], workspace["name"])
        self.assertEqual(response.data["parent_id"], str(self.default_workspace.id))

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_create_workspace_authorized_with_default_workspace_read_only_fails(self, send_kafka_message):
        """Test that creating a workspace with only Default Workspace read permission fails."""
        workspace = {
            "name": "New Workspace Read Only",
            "description": "Workspace creation should fail with read-only permission",
        }

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META

        # Set up read-only access for Default Workspace
        self._setup_access_for_principal(
            self.user_data["username"], "inventory:groups:read", workspace_id=str(self.default_workspace.id)
        )

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)

        # This should still fail because we only have read permission
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_create_workspace_authorized_with_root_and_default_workspace_permissions(self, send_kafka_message):
        """Test for creating a workspace with both Root and Default Workspace permissions."""
        workspace = {
            "name": "New Workspace Both Permissions",
            "description": "Workspace created with both Root and Default permissions",
        }

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META

        # Set up access for both Root and Default Workspace
        self._setup_access_for_principal(
            self.user_data["username"],
            "inventory:groups:write",
            workspace_id=[str(self.root_workspace.id), str(self.default_workspace.id)],
        )

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **headers)

        # This should continue to work as before
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], workspace["name"])
        self.assertEqual(response.data["parent_id"], str(self.default_workspace.id))

    def test_duplicate_create_workspace(self):
        """Test that creating a duplicate workspace within same parent is not allowed."""
        workspace_data = {
            "name": "New Workspace",
            "description": "New Workspace - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.standard_workspace.id,
        }

        Workspace.objects.create(**workspace_data)

        test_data = {"name": "New Workspace", "parent_id": self.standard_workspace.id}

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        resp_body = json.loads(response.content.decode())
        self.assertEqual(resp_body.get("detail"), "Can't create workspace with same name within same parent workspace")

    @override_settings(WORKSPACE_ORG_CREATION_LIMIT=4)
    def test_create_workspaces_exceed_limit(self):
        """
        Test that when creating workspaces if the limit exceeds the organization's workspace limit
        the correct response is returned.
        """
        workspace_names = ["Workspace A", "Workspace B", "Workspace C", "Workspace D"]

        for name in workspace_names:
            workspace_data = {
                "name": name,
                "description": "New Workspace - description",
                "tenant_id": self.tenant.id,
                "parent_id": self.standard_workspace.id,
            }

            Workspace.objects.create(**workspace_data)

        test_data = {"name": "New Workspace", "parent_id": self.standard_workspace.id}

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        resp_body = json.loads(response.content.decode())
        self.assertEqual(
            resp_body.get("detail"),
            "Workspace limit reached (6/4); please free up capacity by deleting empty workspaces"
            " or consolidating those with similar users and role bindings.",
        )

    @override_settings(WORKSPACE_ORG_CREATION_LIMIT=9)
    def test_create_workspaces_not_exceed_limit(self):
        """
        Test that when creating workspaces if the limit does not exceed the organization's workspace limit
        the correct response is returned.
        """
        workspace_names = ["Workspace A", "Workspace B", "Workspace C", "Workspace D"]

        for name in workspace_names:
            workspace_data = {
                "name": name,
                "description": "New Workspace - description",
                "tenant_id": self.tenant.id,
                "parent_id": self.standard_workspace.id,
            }

            Workspace.objects.create(**workspace_data)

        test_data = {"name": "New Workspace", "parent_id": self.standard_workspace.id}

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, test_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], test_data["name"])

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=5)
    def test_create_workspaces_exceed_hierarchy_depth_limit(self):
        """
        Test that creating workspaces succeeds when within the hierarchy depth limit.

        Current hierarchy: root (0) -> default (1) -> standard (2) -> standard_sub (3)
        Creating at depth 4 should succeed with limit of 5.
        """
        workspace_data = {
            "name": "Level 4 Workspace",
            "description": "Workspace created at depth 4 within the limit.",
            "parent_id": self.standard_sub_workspace.id,
        }
        client = APIClient()
        url = reverse("v2_management:workspace-list")
        response = client.post(url, workspace_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data
        self.assertEqual(data.get("name"), "Level 4 Workspace")
        self.assertEqual(data.get("type"), "standard")
        self.assertEqual(data.get("parent_id"), str(self.standard_sub_workspace.id))

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=3)
    def test_create_workspace_fails_when_exceeding_hierarchy_depth_limit(self):
        """
        Test that creating workspaces fails when the hierarchy depth limit is exceeded.

        Current hierarchy: root (0) -> default (1) -> standard (2) -> standard_sub (3)
        Attempting to create at depth 4 should fail with limit of 3.
        """
        workspace_data = {
            "name": "Too Deep Workspace",
            "description": "Workspace that exceeds the hierarchy depth limit.",
            "parent_id": self.standard_sub_workspace.id,
        }
        client = APIClient()
        url = reverse("v2_management:workspace-list")
        response = client.post(url, workspace_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        resp_body = json.loads(response.content.decode())
        self.assertEqual(resp_body.get("detail"), "Workspaces may only nest 3 levels deep.")

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=5, WORKSPACE_RESTRICT_DEFAULT_PEERS=False)
    def test_create_deep_workspace_chain_under_root(self):
        """
        Test creating a deep chain of standard workspaces under root workspace.
        Expected hierarchy:
        root (0) -> standard1 (1) -> standard2 (2) -> standard3 (3) -> standard4 (4) -> standard5 (5)
        """
        client = APIClient()
        url = reverse("v2_management:workspace-list")

        # Create the chain of workspaces
        current_parent_id = self.root_workspace.id
        workspace_ids = []

        for i in range(1, 6):  # Create 5 standard workspaces (depth 1-5)
            workspace_data = {
                "name": f"Standard Workspace Level {i}",
                "description": f"Standard workspace at depth level {i}",
                "parent_id": current_parent_id,
            }

            response = client.post(url, workspace_data, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            data = response.data
            self.assertEqual(data.get("name"), f"Standard Workspace Level {i}")
            self.assertEqual(data.get("type"), "standard")
            self.assertEqual(data.get("parent_id"), str(current_parent_id))
            self.assertIsNotNone(data.get("id"))

            # Store workspace ID and set it as parent for next iteration
            workspace_id = data.get("id")
            workspace_ids.append(workspace_id)
            current_parent_id = workspace_id

        # Verify all 5 workspaces were created successfully
        self.assertEqual(len(workspace_ids), 5)

        # Verify the hierarchy structure by checking each workspace's parent
        for i, workspace_id in enumerate(workspace_ids):
            workspace = Workspace.objects.get(id=workspace_id)
            self.assertEqual(workspace.type, Workspace.Types.STANDARD)
            self.assertEqual(workspace.name, f"Standard Workspace Level {i + 1}")

            if i == 0:
                # First workspace should have root as parent
                self.assertEqual(workspace.parent_id, self.root_workspace.id)
            else:
                # Each subsequent workspace should have the previous one as parent
                self.assertEqual(str(workspace.parent_id), workspace_ids[i - 1])

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=5, WORKSPACE_RESTRICT_DEFAULT_PEERS=False)
    def test_create_standard_chain_under_default_workspace(self):
        """
        Test creating a chain of standard workspaces under existing default workspace.
        Expected hierarchy:
        root (0) -> default (1) -> standard1 (2) -> standard2 (3) -> standard3 (4) -> standard4 (5)
        """
        client = APIClient()
        url = reverse("v2_management:workspace-list")

        # Create the chain of workspaces
        current_parent_id = self.default_workspace.id
        workspace_ids = []

        for i in range(1, 5):  # Create 4 standard workspaces (depth 2-5)
            workspace_data = {
                "name": f"Standard Chain {i}",
                "description": f"Standard workspace #{i} in chain under default",
                "parent_id": current_parent_id,
            }

            response = client.post(url, workspace_data, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            data = response.data
            self.assertEqual(data.get("name"), f"Standard Chain {i}")
            self.assertEqual(data.get("type"), "standard")
            self.assertEqual(data.get("parent_id"), str(current_parent_id))

            # Store the created workspace ID for next iteration
            workspace_id = data.get("id")
            workspace_ids.append(workspace_id)
            current_parent_id = workspace_id

        # Verify the complete chain exists and has correct hierarchy
        for i, workspace_id in enumerate(workspace_ids):
            workspace = Workspace.objects.get(id=workspace_id)
            self.assertEqual(workspace.name, f"Standard Chain {i + 1}")
            self.assertEqual(workspace.type, "standard")

            if i == 0:
                # First workspace should have default as parent
                self.assertEqual(workspace.parent_id, self.default_workspace.id)
            else:
                # Each subsequent workspace should have the previous one as parent
                self.assertEqual(str(workspace.parent_id), workspace_ids[i - 1])

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=5, WORKSPACE_RESTRICT_DEFAULT_PEERS=False)
    def test_create_standard_chain_under_ungrouped_workspace(self):
        """
        Test creating a chain of standard workspaces under existing ungrouped workspace.
        Expected hierarchy:
        root (0) -> default (1) -> ungrouped (2) -> standard1 (3) -> standard2 (4) -> standard3 (5)
        """
        client = APIClient()
        url = reverse("v2_management:workspace-list")

        # Create the chain of workspaces
        current_parent_id = self.ungrouped_workspace.id
        workspace_ids = []

        for i in range(1, 4):  # Create 3 standard workspaces (depth 3-5)
            workspace_data = {
                "name": f"Standard Under Ungrouped {i}",
                "description": f"Standard workspace #{i} in chain under ungrouped",
                "parent_id": current_parent_id,
            }

            response = client.post(url, workspace_data, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            data = response.data
            self.assertEqual(data.get("name"), f"Standard Under Ungrouped {i}")
            self.assertEqual(data.get("type"), "standard")
            self.assertEqual(data.get("parent_id"), str(current_parent_id))

            # Store the created workspace ID for next iteration
            workspace_id = data.get("id")
            workspace_ids.append(workspace_id)
            current_parent_id = workspace_id

        # Verify the complete chain exists and has correct hierarchy
        for i, workspace_id in enumerate(workspace_ids):
            workspace = Workspace.objects.get(id=workspace_id)
            self.assertEqual(workspace.name, f"Standard Under Ungrouped {i + 1}")
            self.assertEqual(workspace.type, "standard")

            if i == 0:
                # First workspace should have ungrouped as parent
                self.assertEqual(workspace.parent_id, self.ungrouped_workspace.id)
            else:
                # Each subsequent workspace should have the previous one as parent
                self.assertEqual(str(workspace.parent_id), workspace_ids[i - 1])

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate_workspace")
    def test_update_workspace(self, replicate_workspace, replicate):
        """Test for updating a workspace."""
        replicate.side_effect = self.in_memory_replicator.replicate
        workspace_data = {
            "name": "New Workspace",
            "description": "New Workspace - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.standard_workspace.id,
        }

        workspace = Workspace.objects.create(**workspace_data)

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()

        workspace_data["name"] = "Updated name"
        workspace_data["description"] = "Updated description"
        workspace_data["parent_id"] = workspace.parent_id
        response = client.put(url, workspace_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data.get("name"), "Updated name")
        self.assertNotEqual(data.get("id"), "")
        self.assertIsNotNone(data.get("id"))
        self.assertNotEqual(data.get("created"), "")
        self.assertNotEqual(data.get("modified"), "")
        self.assertEqual(data.get("type"), "standard")
        self.assertEqual(data.get("description"), "Updated description")

        update_workspace = Workspace.objects.filter(id=workspace.id).first()
        self.assertEqual(update_workspace.name, "Updated name")
        self.assertEqual(update_workspace.description, "Updated description")
        self.assertEqual(response.get("content-type"), "application/json")

        self.assertEqual(len(self.tuples), 0)
        self.in_memory_replicator = InMemoryRelationReplicator(self.tuples)
        workspace_event = replicate_workspace.call_args[0][0]
        self.assertEqual(workspace_event.account_number, self.tenant.account_id)
        self.assertEqual(workspace_event.org_id, self.tenant.org_id)
        self.assertEqual(workspace_event.event_type, ReplicationEventType.UPDATE_WORKSPACE)
        data.pop("description")
        data.pop("parent_id")
        self.assertEqual(workspace_event.workspace, data)

    def test_partial_update_workspace_with_put_method(self):
        """Test for updating a workspace."""
        workspace_data = {
            "name": "New Workspace",
            "description": "New Workspace - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.standard_workspace.id,
        }

        workspace = Workspace.objects.create(**workspace_data)

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()

        workspace_request_data = {"name": "Newer Workspace", "parent_id": workspace.parent_id}

        response = client.put(url, workspace_request_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        status_code = data.get("status")
        self.assertEqual(data.get("name"), "Newer Workspace")
        self.assertEqual(data.get("description"), workspace_data["description"])
        self.assertEqual(data.get("parent_id"), str(workspace_data["parent_id"]))

    def test_partial_update_empty(self):
        """Test for updating a workspace with empty body."""
        workspace_data = {
            "name": "New Workspace",
            "description": "New Workspace - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.root_workspace.id,
        }

        workspace = Workspace.objects.create(**workspace_data)

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()
        response = client.patch(url, {}, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.get("content-type"), "application/json")
        data = response.data
        self.assertEqual(data.get("name"), "New Workspace")
        self.assertEqual(data.get("description"), "New Workspace - description")
        self.assertEqual(data.get("id"), str(workspace.id))
        self.assertNotEqual(data.get("created"), "")
        self.assertNotEqual(data.get("modified"), "")
        self.assertEqual(data.get("type"), "standard")

        update_workspace = Workspace.objects.filter(id=workspace.id).first()
        self.assertEqual(update_workspace.name, "New Workspace")

    def test_update_workspace_update_parent_id(self):
        """Test for updating a workspace's parent_id."""
        workspace_data = {
            "name": "New Workspace",
            "description": "New Workspace - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.standard_workspace.id,
        }

        workspace = Workspace.objects.create(**workspace_data)

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()

        workspace_request_data = {"name": "New Workspace", "parent_id": self.default_workspace.id}

        response = client.put(url, workspace_request_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        status_code = response.data.get("status")
        detail = response.data.get("detail")
        self.assertIsNotNone(detail)
        self.assertEqual(detail, "Can't update the 'parent_id' on a workspace directly")
        self.assertEqual(status_code, 400)
        self.assertEqual(response.get("content-type"), "application/problem+json")

    def test_partial_update_workspace(self):
        """Test for updating a workspace."""
        workspace_data = {
            "name": "New Workspace",
            "description": "New Workspace - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.root_workspace.id,
        }

        workspace = Workspace.objects.create(**workspace_data)

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()

        workspace_data = {"name": "Updated name"}
        response = client.patch(url, workspace_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data.get("name"), "Updated name")
        self.assertNotEqual(data.get("id"), "")
        self.assertIsNotNone(data.get("id"))
        self.assertNotEqual(data.get("created"), "")
        self.assertNotEqual(data.get("modified"), "")
        self.assertEqual(data.get("type"), "standard")

        update_workspace = Workspace.objects.filter(id=workspace.id).first()
        self.assertEqual(update_workspace.name, "Updated name")
        self.assertEqual(response.get("content-type"), "application/json")

    def test_partial_update_workspace_same_tenant_parent_id(self):
        """Test for updating a workspace with a parent that's the same as the original object."""
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()

        workspace_data = {"parent_id": self.standard_workspace.parent_id}
        response = client.patch(url, workspace_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_partial_update_workspace_wrong_tenant_parent_id(self):
        """Test for updating a workspace with a parent not the same as the original object."""
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()

        workspace_data = {"parent_id": self.root_workspace.id}
        response = client.patch(url, workspace_data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Can't update the 'parent_id' on a workspace directly")

    def test_update_workspace_empty_body(self):
        """Test for updating a workspace with empty body"""
        workspace = {}

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        response = client.put(url, workspace, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        status_code = response.data.get("status")
        detail = response.data.get("detail")
        instance = response.data.get("instance")
        self.assertIsNotNone(detail)
        self.assertEqual(detail, "This field is required.")
        self.assertEqual(status_code, 400)
        self.assertEqual(instance, url)
        self.assertEqual(response.get("content-type"), "application/problem+json")

    def test_update_duplicate_workspace(self):
        workspace_data_for_update = {
            "name": "New Duplicate Workspace for Update",
            "description": "New Duplicate Workspace - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.standard_workspace.id,
        }

        workspace_for_update = Workspace.objects.create(**workspace_data_for_update)

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace_for_update.id})
        client = APIClient()

        workspace_data_for_put = {
            "name": "New Duplicate Workspace",
            "description": "New Duplicate Workspace - description",
            "parent_id": self.standard_workspace.id,
        }

        response = client.put(url, workspace_data_for_put, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.get("content-type"), "application/json")

    def test_update_workspace_unauthorized(self):
        workspace = {}

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        response = client.put(url, workspace, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        status_code = response.data.get("status")
        detail = response.data.get("detail")

        self.assertEqual(detail, "You do not have permission to perform this action.")
        self.assertEqual(status_code, 403)
        self.assertEqual(response.get("content-type"), "application/problem+json")

    def test_update_root_workspace_parent_id_fail(self):
        """Test we cannot update parent id for root workspace."""
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.root_workspace.id})

        # Test with not existing uuid, with root workspace's own id, with existing workspace id
        invalid_id = "a5b82afe-a74a-4d4d-98e5-f49ea78e5910"
        root_id = self.root_workspace.id
        workspace_data = {
            "name": "Workspace name",
            "description": "Workspace description",
            "tenant_id": self.tenant.id,
            "parent_id": self.standard_workspace.id,
        }
        standard_workspace = Workspace.objects.create(**workspace_data)
        standard_id = standard_workspace.id

        for ws_id in invalid_id, root_id, standard_id:
            test_data = {"name": "New Workspace Name", "parent_id": ws_id}
            response = client.put(url, test_data, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            resp_body = json.loads(response.content.decode())
            self.assertEqual(resp_body.get("detail"), "The root workspace cannot be updated.")

    def test_update_root_workspace_name_description_fail(self):
        """Test we cannot update name and/or description for root workspace."""
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.root_workspace.id})

        # Test data cases:
        #    - name and description update,
        #    - only name update
        #    - only description update (name without change but must be present because the field is required)
        test_data_cases = [
            {"name": "New name", "description": "New description"},
            {"name": "New name"},
            {"name": self.root_workspace.name, "description": "New description"},
        ]

        for test_data in test_data_cases:
            for method in ("put", "patch"):  # try to update via PUT and PATCH endpoint
                response = getattr(client, method)(url, test_data, format="json", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                resp_body = json.loads(response.content.decode())
                self.assertEqual(resp_body.get("detail"), "The root workspace cannot be updated.")

    def test_update_ungrouped_workspace_parent_id_fail(self):
        """Test we cannot update parent id for ungrouped workspace."""
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.ungrouped_workspace.id})

        # Test with not existing uuid, with ungrouped workspace's own id, with existing workspace id
        invalid_id = "a5b82afe-a74a-4d4d-98e5-f49ea78e5910"
        ungrouped_id = self.ungrouped_workspace.id
        workspace_data = {
            "name": "Workspace name",
            "description": "Workspace description",
            "tenant_id": self.tenant.id,
            "parent_id": self.standard_workspace.id,
        }
        standard_workspace = Workspace.objects.create(**workspace_data)
        standard_id = standard_workspace.id

        for ws_id in invalid_id, ungrouped_id, standard_id:
            test_data = {"name": "New Workspace Name", "parent_id": ws_id}
            response = client.put(url, test_data, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            resp_body = json.loads(response.content.decode())
            self.assertEqual(resp_body.get("detail"), "The ungrouped-hosts workspace cannot be updated.")

    def test_update_ungrouped_workspace_name_description_fail(self):
        """Test we cannot update name and/or description for ungrouped workspace."""
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.ungrouped_workspace.id})

        # Test data cases:
        #    - name and description update,
        #    - only name update
        #    - only description update (name without change but must be present because the field is required)
        test_data_cases = [
            {"name": "New name", "description": "New description"},
            {"name": "New name"},
            {"name": self.root_workspace.name, "description": "New description"},
        ]

        for test_data in test_data_cases:
            for method in ("put", "patch"):  # try to update via PUT and PATCH endpoint
                response = getattr(client, method)(url, test_data, format="json", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                resp_body = json.loads(response.content.decode())
                self.assertEqual(resp_body.get("detail"), "The ungrouped-hosts workspace cannot be updated.")

    def test_update_default_workspace_parent_id_fail(self):
        """Test we cannot update parent id for default workspace."""
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.default_workspace.id})

        # Test with not existing uuid, default workspace's id, existing workspace id
        invalid_id = "a5b82afe-a74a-4d4d-98e5-f49ea78e5910"
        default_id = self.default_workspace.id
        workspace_data = {
            "name": "Workspace name",
            "description": "Workspace description",
            "tenant_id": self.tenant.id,
            "parent_id": self.standard_workspace.id,
        }
        standard_workspace = Workspace.objects.create(**workspace_data)
        standard_id = standard_workspace.id

        for ws_id in invalid_id, default_id, standard_id:
            test_data = {"name": "New Workspace Name", "parent_id": ws_id}
            response = client.put(url, test_data, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            resp_body = json.loads(response.content.decode())
            self.assertEqual(resp_body.get("detail"), "Can't update the 'parent_id' on a workspace directly")

    def test_update_default_workspace_name_description(self):
        """Test we can update name and/or description for default workspace."""
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.default_workspace.id})

        root_parent_id = str(self.default_workspace.parent_id)

        # Test data cases:
        #    - name and description update,
        #    - only name update
        #    - only description update (name without change but must be present because the field is required)
        # For PUT, parent_id (= root workspace id) is required per the spec.
        # For PATCH, parent_id is optional.
        put_test_data_cases = [
            {"name": "New name", "description": "New description", "parent_id": root_parent_id},
            {"name": "New name", "description": self.default_workspace.description, "parent_id": root_parent_id},
            {"name": self.default_workspace.name, "description": "New description", "parent_id": root_parent_id},
        ]
        patch_test_data_cases = [
            {"name": "New name", "description": "New description"},
            {"name": "New name", "description": self.default_workspace.description},
            {"name": self.default_workspace.name, "description": "New description"},
        ]

        for test_data in put_test_data_cases:
            response = client.put(url, test_data, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            resp_body = response.json()
            self.assertEqual(resp_body.get("id"), str(self.default_workspace.id))
            self.assertEqual(resp_body.get("name"), test_data["name"])
            self.assertEqual(resp_body.get("description"), test_data["description"])
            self.assertEqual(resp_body.get("type"), Workspace.Types.DEFAULT)
            self.assertEqual(resp_body.get("parent_id"), root_parent_id)

            # After test set the default values back
            self.default_workspace.name = "Default"
            self.default_workspace.description = "Default description"

        for test_data in patch_test_data_cases:
            response = client.patch(url, test_data, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            resp_body = response.json()
            self.assertEqual(resp_body.get("id"), str(self.default_workspace.id))
            self.assertEqual(resp_body.get("name"), test_data["name"])
            self.assertEqual(resp_body.get("description"), test_data["description"])
            self.assertEqual(resp_body.get("type"), Workspace.Types.DEFAULT)
            self.assertEqual(resp_body.get("parent_id"), root_parent_id)

            # After test set the default values back
            self.default_workspace.name = "Default"
            self.default_workspace.description = "Default description"

    def test_update_standard_workspace_put_without_parent_id_fails(self):
        """Test that PUT to a standard workspace without parent_id returns 400."""
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})

        response = client.put(url, {"name": "New Name"}, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        resp_body = response.json()
        self.assertIn("parent_id", str(resp_body))

    def test_update_default_workspace_put_with_root_parent_id_success(self):
        """Test that PUT to the default workspace with the root workspace's id as parent_id succeeds.

        The spec requires parent_id to match the current value. The default workspace's parent
        is the root workspace, so passing root workspace's id should succeed.
        Previously this failed with 'Sub-workspaces may only be created under the default workspace.'
        """
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.default_workspace.id})

        request_data = {
            "name": self.default_workspace.name,
            "description": self.default_workspace.description,
            "parent_id": str(self.default_workspace.parent_id),
        }

        response = client.put(url, request_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        resp_body = response.json()
        self.assertEqual(resp_body.get("type"), Workspace.Types.DEFAULT)
        self.assertEqual(resp_body.get("parent_id"), str(self.default_workspace.parent_id))

    def test_update_workspace_existing_name_fail(self):
        """Test the workspace name update (PUT) fail for already existing "name" under same parent."""
        wsA = Workspace.objects.create(
            name="Workspace A", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )
        wsB = Workspace.objects.create(
            name="Workspace B", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )

        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": wsA.id})
        workspace_request_data = {"name": wsB.name, "parent_id": wsA.parent_id}

        response = client.put(url, workspace_request_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_message = response.json()

        self.assertEqual(response_message.get("status"), status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response_message.get("detail"), f"A workspace with the name '{wsB.name}' already exists under same parent."
        )
        self.assertEqual(response_message.get("instance"), f"/api/rbac/v2/workspaces/{wsA.id}/")

    def test_partial_update_workspace_existing_name_fail(self):
        """Test the workspace name update (PATCH) fail for already existing "name" under same parent."""
        wsA = Workspace.objects.create(
            name="Workspace A", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )
        wsB = Workspace.objects.create(
            name="Workspace B", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )

        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": wsA.id})
        workspace_request_data = {"name": wsB.name}

        response = client.patch(url, workspace_request_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_message = response.json()

        self.assertEqual(response_message.get("status"), status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response_message.get("detail"), f"A workspace with the name '{wsB.name}' already exists under same parent."
        )
        self.assertEqual(response_message.get("instance"), f"/api/rbac/v2/workspaces/{wsA.id}/")

    def test_update_workspace_existing_name_success(self):
        """
        Test the workspace name update (PUT) success for already existing "name"
        under same tenant but with different parent.
        """

        # workspaces structure:
        # self.default_workspace (default) -> Workspace A (standard) -> Workspace AA (standard)
        #                                  -> Workspace B (standard)
        wsA = Workspace.objects.create(
            name="Workspace A", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )
        wsAA = Workspace.objects.create(
            name="Workspace AA", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=wsA
        )
        wsB = Workspace.objects.create(
            name="Workspace B", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )

        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": wsAA.id})
        workspace_request_data = {"name": wsB.name, "parent_id": wsAA.parent_id}
        # expected structure after update:
        # self.default_workspace (default) -> Workspace A (standard) -> Workspace B (standard)
        #                                  -> Workspace B (standard)

        response = client.put(url, workspace_request_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_message = response.json()
        self.assertEqual(response_message.get("name"), wsB.name)
        self.assertEqual(response_message.get("parent_id"), str(wsA.id))

    def test_partial_update_workspace_existing_name_success(self):
        """
        Test the workspace name update (PATCH) success for already existing "name"
        under same tenant but with different parent.
        """

        # workspaces structure:
        # self.default_workspace (default) -> Workspace A (standard) -> Workspace AA (standard)
        #                                  -> Workspace B (standard)
        wsA = Workspace.objects.create(
            name="Workspace A", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )
        wsAA = Workspace.objects.create(
            name="Workspace AA", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=wsA
        )
        wsB = Workspace.objects.create(
            name="Workspace B", type=Workspace.Types.STANDARD, tenant=self.tenant, parent=self.default_workspace
        )

        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": wsAA.id})
        workspace_request_data = {"name": wsB.name}
        # expected structure after update:
        # self.default_workspace (default) -> Workspace A (standard) -> Workspace B (standard)
        #                                  -> Workspace B (standard)

        response = client.patch(url, workspace_request_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_message = response.json()
        self.assertEqual(response_message.get("name"), wsB.name)
        self.assertEqual(response_message.get("parent_id"), str(wsA.id))

    def test_create_workspace_invalid_name_characters(self):
        """Test that create rejects names with disallowed characters."""
        client = APIClient()
        url = reverse("v2_management:workspace-list")
        for invalid_name in ["ws@name", "ws#name", "ws!name", "ws.name", "ws/name", "ws+name"]:
            response = client.post(
                url,
                {"name": invalid_name, "parent_id": self.default_workspace.id},
                format="json",
                **self.headers,
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, f"Expected 400 for '{invalid_name}'")

    def test_create_workspace_valid_name_characters(self):
        """Test that create accepts names with allowed characters."""
        client = APIClient()
        url = reverse("v2_management:workspace-list")
        valid_names = ["My Workspace", "ws-name", "ws_name", "Workspace 123", "simple"]
        for valid_name in valid_names:
            response = client.post(
                url,
                {"name": valid_name, "parent_id": self.default_workspace.id},
                format="json",
                **self.headers,
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, f"Expected 201 for '{valid_name}'")

    def test_update_workspace_invalid_name_characters(self):
        """Test that PUT rejects a new name with disallowed characters."""
        workspace = Workspace.objects.create(
            name="Valid Name",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.STANDARD,
        )
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        response = client.put(
            url,
            {"name": "invalid@name", "parent_id": workspace.parent_id},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_partial_update_workspace_invalid_name_characters(self):
        """Test that PATCH rejects a new name with disallowed characters."""
        workspace = Workspace.objects.create(
            name="Valid Name",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.STANDARD,
        )
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        response = client.patch(url, {"name": "bad!name"}, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_workspace_legacy_name_unchanged(self):
        """Test that PUT with an unchanged legacy name (with disallowed chars) succeeds."""
        workspace = Workspace.objects.create(
            name="legacy@name",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.STANDARD,
        )
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        response = client.put(
            url,
            {"name": "legacy@name", "description": "updated desc", "parent_id": workspace.parent_id},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "legacy@name")

    def test_partial_update_workspace_legacy_name_not_sent(self):
        """Test that PATCH without name field on a workspace with legacy name succeeds."""
        workspace = Workspace.objects.create(
            name="legacy@name",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.STANDARD,
        )
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        response = client.patch(url, {"description": "updated desc"}, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "legacy@name")

    def test_update_workspace_legacy_name_changed_to_invalid(self):
        """Test that changing a legacy name to a different invalid name is rejected."""
        workspace = Workspace.objects.create(
            name="legacy@name",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.STANDARD,
        )
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        response = client.patch(url, {"name": "other@invalid"}, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_workspace_invalid_uuid(self):
        """Test that update fails with 400 when workspace id is not a valid UUID."""
        client = APIClient()
        invalid_uuid = "invalid-uuid"
        url = f"/api/rbac/v2/workspaces/{invalid_uuid}/"
        workspace_data = {"name": "Test"}

        response = client.put(url, workspace_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))

    def test_update_workspace_invalid_uuid_with_parent_id_in_body(self):
        """Test that update with parent_id in body fails with 400 when workspace id is not a valid UUID.

        This specifically tests the move detection code path in permission_from_request
        which queries the database to compare parent IDs.
        """
        client = APIClient()
        invalid_uuid = "id"  # The literal string "id" as seen in the Sentry error
        url = f"/api/rbac/v2/workspaces/{invalid_uuid}/"
        workspace_data = {
            "name": "Test Workspace",
            "parent_id": str(self.default_workspace.id),
            "description": "Test description",
        }

        response = client.put(url, workspace_data, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))

    def test_retrieve_workspace_invalid_uuid(self):
        """Test that retrieve fails with 400 when workspace id is not a valid UUID."""
        client = APIClient()
        invalid_uuid = "invalid-uuid"
        url = f"/api/rbac/v2/workspaces/{invalid_uuid}/"

        response = client.get(url, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))

    def test_delete_workspace_invalid_uuid(self):
        """Test that delete fails with 400 when workspace id is not a valid UUID."""
        client = APIClient()
        invalid_uuid = "invalid-uuid"
        url = f"/api/rbac/v2/workspaces/{invalid_uuid}/"

        response = client.delete(url, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))

    @override_settings(WORKSPACE_RESTRICT_DEFAULT_PEERS=False)
    def test_edit_workspace_disregard_type(self):
        """Test for creating a workspace."""
        root = Workspace.objects.get(tenant=self.tenant, type=Workspace.Types.ROOT)

        workspace = {
            "name": "New Workspace",
            "description": "Workspace",
            "parent_id": str(root.id),
            "type": Workspace.Types.UNGROUPED_HOSTS,
        }

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # Create disregards 'type'
        response = client.post(url, workspace, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data.get("type"), Workspace.Types.STANDARD)

        workspace["type"] = Workspace.Types.DEFAULT
        workspace["name"] = "New Workspace 2"
        response = client.post(url, workspace, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data.get("type"), Workspace.Types.STANDARD)

        # Update disregards 'type'
        workspace["type"] = Workspace.Types.STANDARD
        workspace["name"] = "New Workspace 3"
        created_workspace = Workspace.objects.create(**workspace, tenant=self.tenant)
        url = reverse("v2_management:workspace-detail", kwargs={"pk": created_workspace.id})
        client = APIClient()
        workspace["type"] = Workspace.Types.UNGROUPED_HOSTS
        response = client.put(url, workspace, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("type"), Workspace.Types.STANDARD)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate_workspace")
    def test_delete_workspace(self, replicate_workspace, replicate):
        replicate.side_effect = self.in_memory_replicator.replicate
        workspace_data = {
            "name": "Workspace for delete",
            "description": "Workspace for delete - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.root_workspace.id,
        }
        workspace = Workspace.objects.create(**workspace_data)
        relationship = create_relationship(
            ("rbac", "workspace"),
            str(workspace.id),
            ("rbac", "workspace"),
            str(self.root_workspace.id),
            "parent",
        )
        self.tuples.write([relationship], [])
        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()
        test_headers = self.headers.copy()
        test_headers["HTTP_ACCEPT"] = "application/problem+json"
        response = client.delete(url, None, format="json", **test_headers)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response.headers.get("content-type"), None)
        deleted_workspace = Workspace.objects.filter(id=workspace.id).first()
        self.assertIsNone(deleted_workspace)

        self.assertEqual(len(self.tuples), 0)
        self.in_memory_replicator = InMemoryRelationReplicator(self.tuples)
        workspace_event = replicate_workspace.call_args[0][0]
        self.assertEqual(workspace_event.account_number, self.tenant.account_id)
        self.assertEqual(workspace_event.org_id, self.tenant.org_id)
        self.assertEqual(workspace_event.event_type, ReplicationEventType.DELETE_WORKSPACE)
        deleted_workspace = WorkspaceEventSerializer(workspace).data
        self.assertEqual(workspace_event.workspace["id"], deleted_workspace["id"])

    def test_delete_workspace_not_found(self):
        # Use a valid UUID format that doesn't exist in the database
        non_existent_uuid = "00000000-0000-0000-0000-000000000000"
        url = reverse("v2_management:workspace-detail", kwargs={"pk": non_existent_uuid})
        client = APIClient()
        response = client.delete(url, None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        status_code = response.data.get("status")
        detail = response.data.get("detail")
        self.assertEqual(detail, "No Workspace matches the given query.")
        self.assertEqual(status_code, 404)
        self.assertEqual(response.get("content-type"), "application/problem+json")

    def test_delete_workspace_unauthorized(self):
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        response = client.delete(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        status_code = response.data.get("status")
        detail = response.data.get("detail")
        self.assertEqual(detail, "You do not have permission to perform this action.")
        self.assertEqual(status_code, 403)
        self.assertEqual(response.get("content-type"), "application/problem+json")

    def test_delete_workspace_with_dependencies(self):
        workspace_data = {
            "name": "Workspace for delete",
            "description": "Workspace for delete - description",
            "tenant_id": self.tenant.id,
            "parent_id": self.root_workspace.id,
        }
        workspace = Workspace.objects.create(**workspace_data)
        dependent = Workspace.objects.create(
            name="Dependent Workspace",
            tenant=self.tenant,
            type="standard",
            parent_id=workspace.id,
        )
        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()
        test_headers = self.headers.copy()
        test_headers["HTTP_ACCEPT"] = "application/problem+json"
        response = client.delete(url, None, format="json", **test_headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = response.data.get("detail")
        self.assertEqual(detail, "Unable to delete due to workspace dependencies")

    def test_delete_workspace_with_non_standard_types(self):
        """Test the non-standard (root, default, ungrouped-hosts) workspaces cannot be deleted."""
        client = APIClient()
        # Root workspace can't be deleted
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.root_workspace.id})
        test_headers = self.headers.copy()
        test_headers["HTTP_ACCEPT"] = "application/problem+json"
        response = client.delete(url, None, format="json", **test_headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = response.data.get("detail")
        self.assertEqual(detail, "Unable to delete root workspace")

        # Default workspace can't be deleted
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.default_workspace.id})
        response = client.delete(url, None, format="json", **test_headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = response.data.get("detail")
        self.assertEqual(detail, "Unable to delete default workspace")

        # Ungrouped workspace can't be deleted
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.ungrouped_workspace.id})
        response = client.delete(url, None, format="json", **test_headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = response.data.get("detail")
        self.assertEqual(detail, "Unable to delete ungrouped-hosts workspace")

    @patch("management.base_viewsets.BaseV2ViewSet.create")
    def test_create_retry_success_after_serialization_failure(self, mock_super_create):
        """
        Test that create operation succeeds after SerializationFailure on first attempt.

        The retry=3 parameter in @pgtransaction.atomic should automatically retry
        when SerializationFailure occurs. This test verifies that after an initial
        failure, the operation succeeds on retry.
        """
        # Mock to fail once with SerializationFailure, then succeed
        # Note: Django wraps psycopg2 errors in OperationalError
        serialization_error = OperationalError("could not serialize access")
        serialization_error.__cause__ = SerializationFailure("could not serialize access due to concurrent update")

        from rest_framework.response import Response

        success_response = Response(
            {
                "id": str(uuid4()),
                "name": "Test Workspace",
                "parent_id": str(self.default_workspace.id),
            },
            status=status.HTTP_201_CREATED,
        )

        # First call raises error, second call succeeds
        mock_super_create.side_effect = [serialization_error, success_response]

        # Execute
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace", "parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify success after retry
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "Test Workspace")

        # Verify the method was called twice (initial + 1 retry)
        self.assertEqual(mock_super_create.call_count, 2)

    @patch("management.base_viewsets.BaseV2ViewSet.create")
    def test_create_retry_exhausted_after_three_failures(self, mock_super_create):
        """
        Test that create operation returns 503 SERVICE UNAVAILABLE after all retry attempts fail.

        The retry=3 parameter means: 1 initial attempt + 3 retries = 4 total attempts.
        If all attempts fail with SerializationFailure, the exception should propagate
        and be caught by the create() method, returning a 503 response with Retry-After header.
        """
        # Mock to always fail with SerializationFailure
        serialization_error = OperationalError("could not serialize access")
        serialization_error.__cause__ = SerializationFailure("could not serialize access due to concurrent update")

        mock_super_create.side_effect = serialization_error

        # Execute
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace", "parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify returns 503 SERVICE UNAVAILABLE after all retries exhausted
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("temporarily unable to handle this request", str(response.data["detail"]))
        self.assertEqual(response["Retry-After"], "1")

        # Verify the method was called 4 times (1 initial + 3 retries)
        self.assertEqual(mock_super_create.call_count, 4)

    @patch("management.base_viewsets.BaseV2ViewSet.create")
    def test_create_retry_success_on_third_attempt(self, mock_super_create):
        """
        Test that create succeeds on the 4th and final attempt (3rd retry).

        This verifies the retry mechanism continues trying until success
        or all attempts are exhausted.
        """
        serialization_error = OperationalError("could not serialize access")
        serialization_error.__cause__ = SerializationFailure("could not serialize access due to concurrent update")

        from rest_framework.response import Response

        success_response = Response(
            {
                "id": str(uuid4()),
                "name": "Test Workspace",
                "parent_id": str(self.default_workspace.id),
            },
            status=status.HTTP_201_CREATED,
        )

        # Fail 3 times, succeed on 4th attempt (final retry)
        mock_super_create.side_effect = [
            serialization_error,
            serialization_error,
            serialization_error,
            success_response,
        ]

        # Execute
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace", "parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify success on 4th attempt (3rd retry)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "Test Workspace")

        # Verify the method was called 4 times before succeeding
        self.assertEqual(mock_super_create.call_count, 4)

    @patch("management.base_viewsets.BaseV2ViewSet.create")
    def test_create_retry_deadlock_detected(self, mock_super_create):
        """
        Test that DeadlockDetected errors are also retried by the retry mechanism.

        According to django-pgtransaction documentation, the retry parameter
        handles both SerializationFailure and DeadlockDetected errors.
        """
        # Mock to fail with DeadlockDetected, then succeed
        deadlock_error = OperationalError("deadlock detected")
        deadlock_error.__cause__ = DeadlockDetected("deadlock detected")

        from rest_framework.response import Response

        success_response = Response(
            {
                "id": str(uuid4()),
                "name": "Test Workspace",
                "parent_id": str(self.default_workspace.id),
            },
            status=status.HTTP_201_CREATED,
        )

        mock_super_create.side_effect = [deadlock_error, success_response]

        # Execute
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace", "parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify success after retry
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify retry happened
        self.assertEqual(mock_super_create.call_count, 2)

    @patch("management.base_viewsets.BaseV2ViewSet.create")
    def test_create_retry_deadlock_exhausted(self, mock_super_create):
        """
        Test that DeadlockDetected returns 500 error after all retries exhausted.

        Unlike SerializationFailure (409), DeadlockDetected should return 500
        as it indicates a more serious internal server error.
        The retry=3 parameter means 1 initial + 3 retries = 4 total attempts.
        """
        deadlock_error = OperationalError("deadlock detected")
        deadlock_error.__cause__ = DeadlockDetected("deadlock detected")

        mock_super_create.side_effect = deadlock_error

        # Execute
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace", "parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify returns 500 INTERNAL SERVER ERROR for deadlock
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("Internal server error", str(response.data["detail"]))

        # Verify all retry attempts were made (1 initial + 3 retries = 4 total)
        self.assertEqual(mock_super_create.call_count, 4)

    @patch("management.base_viewsets.BaseV2ViewSet.create")
    def test_create_no_retry_on_validation_error(self, mock_super_create):
        """
        Test that ValidationError does not trigger retries.

        Only SerializationFailure and DeadlockDetected should be retried.
        Other errors like ValidationError should fail immediately without retry.
        """
        # Mock to raise ValidationError
        validation_error = ValidationError("Validation failed")
        mock_super_create.side_effect = validation_error

        # Execute
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace", "parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify failure without retry (ValidationError is handled by DRF)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify method was called only once (no retries for ValidationError)
        self.assertEqual(mock_super_create.call_count, 1)

    @patch(
        "management.workspace.relation_api_dual_write_workspace_handler.RelationApiDualWriteWorkspaceHandler._replicate"
    )
    def test_create_dual_write_operational_error_propagates_for_retry(self, mock_replicate):
        """
        Test that OperationalError from dual write handler propagates through for pgtransaction retry.

        Previously, _replicate() wrapped all exceptions in DualWriteException, which prevented
        pgtransaction from recognizing the error as retryable. After the fix, OperationalError
        (wrapping SerializationFailure) should propagate directly, allowing pgtransaction to retry.
        When retries are exhausted, the view returns 503 with Retry-After header.
        """
        serialization_error = OperationalError("could not serialize access")
        serialization_error.__cause__ = SerializationFailure("could not serialize access due to concurrent update")

        mock_replicate.side_effect = serialization_error

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace DW", "parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # OperationalError propagates through _replicate -> pgtransaction retries -> exhausted -> 503
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("temporarily unable to handle this request", str(response.data["detail"]))
        self.assertEqual(response["Retry-After"], "1")

    @patch(
        "management.workspace.relation_api_dual_write_workspace_handler.RelationApiDualWriteWorkspaceHandler._replicate"
    )
    def test_create_dual_write_non_db_exception_raises_dual_write_error(self, mock_replicate):
        """
        Test that non-database exceptions from _replicate() are still wrapped in DualWriteException.

        Only OperationalError (serialization/deadlock) should propagate for retry.
        Other exceptions should remain wrapped in DualWriteException and bubble up.
        """
        from management.relation_replicator.relation_replicator import DualWriteException

        mock_replicate.side_effect = DualWriteException(ValueError("some other error"))

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace DW2", "parent_id": str(self.default_workspace.id)}

        # Non-DB DualWriteException bubbles up as unhandled (not caught by the view)
        with self.assertRaises(DualWriteException):
            client.post(url, data, format="json", **self.headers)

    @patch("management.base_viewsets.BaseV2ViewSet.create")
    def test_create_bare_operational_error_propagates(self, mock_super_create):
        """
        Test that OperationalError without __cause__ re-raises as unhandled.

        If OperationalError has no __cause__ (e.g., connection error, not serialization),
        it should propagate as an unhandled exception rather than being silently swallowed.
        """
        bare_error = OperationalError("connection reset by peer")

        mock_super_create.side_effect = bare_error

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        data = {"name": "Test Workspace Bare", "parent_id": str(self.default_workspace.id)}

        with self.assertRaises(OperationalError):
            client.post(url, data, format="json", **self.headers)


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=True, WORKSPACE_HIERARCHY_DEPTH_LIMIT=5)
class WorkspaceMove(TransactionalWorkspaceViewTests):
    """Tests for move workspace."""

    def setUp(self):
        """Set up workspace access check tests."""
        super().setUp()
        self.tuples = InMemoryTuples()
        self.in_memory_replicator = InMemoryRelationReplicator(self.tuples)

        # Create a unique test workspace that won't conflict with existing names
        self.test_workspace = Workspace.objects.create(
            name="Test Workspace for Move",
            description="Test workspace for move operations",
            tenant=self.tenant,
            parent=self.standard_workspace,
            type=Workspace.Types.STANDARD,
        )

        # Create test users
        self.user_with_access = {"username": "user_with_access", "email": "user_with_access@example.com"}
        self.user_without_access = {"username": "user_without_access", "email": "user_without_access@example.com"}

        # Set up access for user_with_access to have write permissions on default_workspace
        with patch("core.kafka.RBACProducer.send_kafka_message"):
            self._setup_access_for_principal(
                self.user_with_access["username"], "inventory:groups:write", str(self.default_workspace.id)
            )

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_success_move_workspace(self, replicate):
        replicate.side_effect = self.in_memory_replicator.replicate

        validated_data_source_ws = {
            "name": "Workspace Source",
            "parent_id": self.default_workspace.id,
        }
        source_workspace = self.service.create(validated_data_source_ws, self.tenant)

        validated_data_target_ws = {
            "name": "Workspace Target",
            "parent_id": self.default_workspace.id,
        }
        target_workspace = self.service.create(validated_data_target_ws, self.tenant)

        tuples_source_to_default = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(source_workspace.id)),
                relation("parent"),
                subject("rbac", "workspace", str(self.default_workspace.id)),
            )
        )
        self.assertEqual(len(tuples_source_to_default), 1)

        tuples_target_to_default = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(target_workspace.id)),
                relation("parent"),
                subject("rbac", "workspace", str(self.default_workspace.id)),
            )
        )
        self.assertEqual(len(tuples_target_to_default), 1)

        url = reverse("v2_management:workspace-move", kwargs={"pk": source_workspace.id})

        client = APIClient()

        workspace_data_for_move = {
            "parent_id": target_workspace.id,
        }

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.get("content-type"), "application/json")
        self.assertEqual(response.data.get("id"), str(source_workspace.id))
        self.assertEqual(response.data.get("parent_id"), str(target_workspace.id))

        tuples_source_to_target = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(source_workspace.id)),
                relation("parent"),
                subject("rbac", "workspace", str(target_workspace.id)),
            )
        )
        self.assertEqual(len(tuples_source_to_target), 1)

        tuples_source_to_default = self.tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(source_workspace.id)),
                relation("parent"),
                subject("rbac", "workspace", str(self.default_workspace.id)),
            )
        )
        self.assertEqual(len(tuples_source_to_default), 0)

    def test_move_not_existing_workspace(self):
        """Test you cannot move not existing workspace."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": str(uuid4())})
        client = APIClient()
        workspace_data_for_move = {"parent_id": self.standard_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_body = response.json()
        self.assertEqual(response_body.get("detail"), "No Workspace matches the given query.")

    def test_move_with_invalid_uuid_parent_id(self):
        """Test you cannot move a workspace when invalid uuid is provided as a parent id."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        invalid_uuid = "invalid_uuid"
        workspace_data_for_move = {"parent_id": invalid_uuid}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_body = response.json()
        self.assertEqual(response_body.get("detail"), f"{invalid_uuid} is not a valid UUID.")

    def test_move_under_itself(self):
        """Test you cannot move a workspace under itself."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        workspace_data_for_move = {"parent_id": self.standard_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_body = response.json()
        self.assertEqual(response_body.get("detail"), "The parent_id and id values must not be the same.")

    def test_move_root_workspace(self):
        """Test you cannot move a root workspace."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.root_workspace.id})
        client = APIClient()
        workspace_data_for_move = {"parent_id": self.standard_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_body = response.json()
        self.assertEqual(response_body.get("detail"), "Cannot move non-standard workspace.")

    def test_move_default_workspace(self):
        """Test you cannot move a default workspace."""
        standard_workspace_1 = self.service.create(
            {"name": "W-1", "parent_id": self.default_workspace.id}, self.tenant
        )
        standard_workspace_1_1 = self.service.create(
            {"name": "W-1-1", "parent_id": standard_workspace_1.id}, self.tenant
        )
        standard_workspace_1_1_1 = self.service.create(
            {"name": "W-1-1-1", "parent_id": standard_workspace_1_1.id}, self.tenant
        )
        standard_workspace_1_2 = self.service.create(
            {"name": "W-1-2", "parent_id": standard_workspace_1.id}, self.tenant
        )

        # move ws 1_1_1 under 1_2
        url = reverse("v2_management:workspace-move", kwargs={"pk": standard_workspace_1_1_1.id})
        client = APIClient()
        workspace_data_for_move = {"parent_id": standard_workspace_1_2.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_body = response.json()

        self.assertEqual(response_body["id"], str(standard_workspace_1_1_1.id))
        self.assertEqual(response_body["parent_id"], str(standard_workspace_1_2.id))
        self.assertEqual(
            response_body["parent_id"], str(Workspace.objects.get(id=standard_workspace_1_1_1.id).parent_id)
        )

    def test_move_ungrouped_workspace(self):
        """Test you cannot move a ungrouped workspace."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.ungrouped_workspace.id})
        client = APIClient()
        workspace_data_for_move = {"parent_id": self.standard_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_body = response.json()
        self.assertEqual(response_body.get("detail"), "Cannot move non-standard workspace.")

    def test_move_parent_id_not_exists(self):
        """Test you cannot move a workspace under not existing parent."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        parent_id = str(uuid4())
        workspace_data_for_move = {"parent_id": parent_id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response_body = response.json()
        self.assertEqual(response_body.get("detail"), TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE)

    def test_move_parent_with_empty_parent_id(self):
        """Test you cannot move a workspace when empty string is provided as a parent id."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        workspace_data_for_move = {"parent_id": ""}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_body = response.json()
        self.assertEqual(response_body.get("detail"), "The 'parent_id' field is required.")

    def test_move_parent_without_request_body(self):
        """Test you cannot move a workspace without request body (without parent_id)."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()

        response = client.post(url, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_body = response.json()
        self.assertEqual(response_body.get("detail"), "The 'parent_id' field is required.")

    @override_settings(WORKSPACE_RESTRICT_DEFAULT_PEERS=True)
    def test_move_under_root_workspace_fail(self):
        """Test you cannot move a workspace under a root workspace with WORKSPACE_RESTRICT_DEFAULT_PEERS=True."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        workspace_data_for_move = {"parent_id": self.root_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json().get("detail"), "Sub-workspaces may only be created under the default workspace."
        )

    @override_settings(WORKSPACE_RESTRICT_DEFAULT_PEERS=False)
    def test_move_under_root_workspace_success(self):
        """Test you can move a workspace under a root workspace with WORKSPACE_RESTRICT_DEFAULT_PEERS=False."""
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        workspace_data_for_move = {"parent_id": self.root_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("id"), str(self.standard_workspace.id))
        self.assertEqual(response.data.get("parent_id"), str(self.root_workspace.id))

    def test_move_under_default_workspace(self):
        """Test you can move a workspace under a default workspace."""
        # The test workspace belongs under another standard workspace
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_sub_workspace.id})
        response = client.get(url, format="json", **self.headers)
        parent_id = response.data.get("parent_id")
        parent_workspace = Workspace.objects.get(id=parent_id)
        self.assertEqual(parent_workspace.type, Workspace.Types.STANDARD)

        # Move the workspace under default workspace
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_sub_workspace.id})
        workspace_data_for_move = {"parent_id": self.default_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("id"), str(self.standard_sub_workspace.id))
        self.assertEqual(response.data.get("parent_id"), str(self.default_workspace.id))

    def test_move_under_ungrouped_workspace(self):
        """Test you can move a workspace under an ungrouped workspace."""
        # The test workspace belongs under another standard workspace
        client = APIClient()
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_sub_workspace.id})
        response = client.get(url, format="json", **self.headers)
        parent_id = response.data.get("parent_id")
        parent_workspace = Workspace.objects.get(id=parent_id)
        self.assertEqual(parent_workspace.type, Workspace.Types.STANDARD)

        # Move the workspace under ungrouped workspace
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.standard_sub_workspace.id})
        workspace_data_for_move = {"parent_id": self.ungrouped_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("id"), str(self.standard_sub_workspace.id))
        self.assertEqual(response.data.get("parent_id"), str(self.ungrouped_workspace.id))

    def test_move_under_standard_workspace(self):
        """Test you can move a workspace under a standard workspace."""
        # Create new workspace under default workspace
        validated_data = {"name": "New Workspace", "description": "Workspace"}
        workspace = self.service.create(validated_data, self.tenant)
        parent_workspace = Workspace.objects.get(id=workspace.parent_id)
        self.assertEqual(parent_workspace.type, Workspace.Types.DEFAULT)

        # Move the workspace under 'self.standard_workspace'
        client = APIClient()
        url = reverse("v2_management:workspace-move", kwargs={"pk": workspace.id})
        workspace_data_for_move = {"parent_id": self.standard_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("id"), str(workspace.id))
        self.assertEqual(response.data.get("parent_id"), str(self.standard_workspace.id))

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=3)
    def test_move_enforce_hierarchy_depth(self):
        """
        Test to enforce the hierarchy depth for moving workspace.

        Initial workspace hierarchy from setUp() method
        root -> default -> standard -> standard
                        -> ungrouped

        Test adds new standard workspace under default workspace and then
        tries to move it under most right standard workspace

        Desired workspace hierarchy
        root -> default -> standard -> standard -> standard
                        -> ungrouped

        It is expected that the move will be declined due to exceeding the maximum allowed hierarchy depth.
        """
        # Create new workspace under default workspace
        validated_data = {"name": "New Workspace", "description": "Workspace"}
        workspace = self.service.create(validated_data, self.tenant)
        parent_workspace = Workspace.objects.get(id=workspace.parent_id)
        self.assertEqual(parent_workspace.type, Workspace.Types.DEFAULT)

        # Move the workspace
        client = APIClient()
        url = reverse("v2_management:workspace-move", kwargs={"pk": workspace.id})
        workspace_data_for_move = {"parent_id": self.standard_sub_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json().get("detail"), "Workspaces may only nest 3 levels deep.")

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=5)
    def test_move_enforce_hierarchy_depth_with_descendants_success(self):
        """
        Test to enforce the hierarchy depth for workspace descendants.

        Initial workspace hierarchy from setUp() method
        root -> default -> standard A -> standard B
                        -> ungrouped

        Test adds new standard workspace with one child under default workspace and then
        tries to move it under standard A workspace

        Desired workspace hierarchy
        root -> default -> standard A -> standard B
                                      -> new standard C -> new standard D
                        -> ungrouped

        It is expected that the move will be successful.
        """
        # Create new workspace under default workspace
        validated_data_ws_C = {"name": "New Workspace C", "description": "Workspace C"}
        workspace_C = self.service.create(validated_data_ws_C, self.tenant)
        parent_workspace = Workspace.objects.get(id=workspace_C.parent_id)
        self.assertEqual(parent_workspace.type, Workspace.Types.DEFAULT)

        # Create a child workspace
        validated_data_ws_D = {"name": "New Workspace D", "description": "Workspace D", "parent_id": workspace_C.id}
        workspace_D = self.service.create(validated_data_ws_D, self.tenant)
        parent_workspace = Workspace.objects.get(id=workspace_D.parent_id)
        self.assertEqual(parent_workspace.type, Workspace.Types.STANDARD)

        # Move the workspace with descendant
        client = APIClient()
        url = reverse("v2_management:workspace-move", kwargs={"pk": workspace_C.id})
        workspace_data_for_move = {"parent_id": self.standard_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json().get("id"), str(workspace_C.id))
        self.assertEqual(response.json().get("parent_id"), str(self.standard_workspace.id))

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=3)
    def test_move_enforce_hierarchy_depth_with_descendants_fail(self):
        """
        Test to enforce the hierarchy depth for workspace descendants.

        Initial workspace hierarchy from setUp() method
        root -> default -> standard A -> standard B
                        -> ungrouped

        Test adds new standard workspace with one child under default workspace and then
        tries to move it under standard A workspace

        Desired workspace hierarchy
        root -> default -> standard A -> standard B
                                      -> new standard C -> new standard D
                        -> ungrouped

        It is expected that the move will be declined due to exceeding the maximum allowed hierarchy depth.
        """
        # Create new workspace under default workspace
        validated_data_ws_C = {"name": "New Workspace C", "description": "Workspace C"}
        workspace_C = self.service.create(validated_data_ws_C, self.tenant)
        parent_workspace = Workspace.objects.get(id=workspace_C.parent_id)
        self.assertEqual(parent_workspace.type, Workspace.Types.DEFAULT)

        # Create a child workspace
        validated_data_ws_D = {"name": "New Workspace D", "description": "Workspace D", "parent_id": workspace_C.id}
        workspace_D = self.service.create(validated_data_ws_D, self.tenant)
        parent_workspace = Workspace.objects.get(id=workspace_D.parent_id)
        self.assertEqual(parent_workspace.type, Workspace.Types.STANDARD)

        # Move the workspace with descendant
        client = APIClient()
        url = reverse("v2_management:workspace-move", kwargs={"pk": workspace_C.id})
        workspace_data_for_move = {"parent_id": self.standard_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json().get("detail"), "Cannot move workspace: resulting hierarchy depth (4) exceeds limit (3)."
        )

    @override_settings(WORKSPACE_HIERARCHY_DEPTH_LIMIT=100)
    def test_move_under_descendant(self):
        """Test you cannot move a workspace under own descendant."""
        # Create tree of 5 standard workspaces under the default workspace
        parent_id = None
        workspaces = {}

        for name in "ABCDE":
            validated_data = {
                "name": f"Workspace {name}",
                "description": f"Workspace {name}",
            }
            if parent_id:
                validated_data["parent_id"] = parent_id
            workspace = self.service.create(validated_data, self.tenant)
            workspaces[name] = workspace
            parent_id = workspace.id

        # Check that 4 descendants were created
        descendants = workspaces["A"].descendants()
        self.assertEqual(len(descendants), 4)
        self.assertEqual(workspaces["A"].get_max_descendant_depth(), 4)

        # Try to move the top workspace under all its descendants
        client = APIClient()
        for descendant in descendants:
            url = reverse("v2_management:workspace-move", kwargs={"pk": workspaces["A"].id})
            workspace_data_for_move = {"parent_id": descendant.id}

            response = client.post(url, workspace_data_for_move, format="json", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            response_body = response.json()
            self.assertEqual(response_body.get("detail"), "Cannot move workspace under one of its own descendants.")

    def test_move_with_duplicate_name_under_target_parent(self):
        """
        Test that a workspace cannot be moved under a parent
        if another workspace with the same name already exists there.
        """
        # Create workspace structure
        # root -> default -> Standard Workspace -> Test Workspace
        #                 -> Test Workspace
        name = "Test Workspace"
        self.standard_sub_workspace.name = name
        self.standard_sub_workspace.save()

        validated_data = {"name": name, "description": f"{name} description"}
        test_workspace = self.service.create(validated_data, self.tenant)

        # Try to move the 'Test Workspace' under the 'Standard Workspace'
        client = APIClient()
        url = reverse("v2_management:workspace-move", kwargs={"pk": test_workspace.id})
        workspace_data_for_move = {"parent_id": self.standard_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_body = response.json()
        self.assertEqual(
            response_body.get("detail"), "A workspace with the same name already exists under the target parent."
        )

    def test_move_under_target_parent_with_same_name(self):
        """
        Test that a workspace can be moved under parent with same name."""
        # Create workspace structure
        # root -> default -> Standard Workspace -> Test Workspace
        #                 -> Test Workspace
        name = "Test Workspace"
        self.standard_sub_workspace.name = name
        self.standard_sub_workspace.save()

        validated_data = {"name": name, "description": f"{name} description"}
        test_workspace = self.service.create(validated_data, self.tenant)

        # Try to move the 'Test Workspace' under the 'Test Workspace'
        client = APIClient()
        url = reverse("v2_management:workspace-move", kwargs={"pk": test_workspace.id})
        workspace_data_for_move = {"parent_id": self.standard_sub_workspace.id}

        response = client.post(url, workspace_data_for_move, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("id"), str(test_workspace.id))
        self.assertEqual(response.data.get("parent_id"), str(self.standard_sub_workspace.id))

    def test_move_with_write_access_allowed(self):
        """Test that move succeeds when user has write access to target workspace."""
        # Create request context for user with access
        request_context = self._create_request_context(self.customer_data, self.user_with_access, is_org_admin=False)
        headers = request_context["request"].META

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **headers)

        # Verify
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.data
        self.assertEqual(response_data["id"], str(self.test_workspace.id))
        self.assertEqual(response_data["parent_id"], str(self.default_workspace.id))

    def test_move_with_write_access_denied(self):
        """Test that move fails when user lacks write access to target workspace."""
        # Create request context for user without access
        request_context = self._create_request_context(
            self.customer_data, self.user_without_access, is_org_admin=False
        )
        headers = request_context["request"].META

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **headers)

        # Verify - Should get 403 from WorkspaceAccessPermission class
        # before our custom validation can return 400
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("You do not have permission to perform this action", str(response.data))

    def test_move_with_admin_access(self):
        """Test that move succeeds when user is org admin (bypasses access check)."""
        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.data
        self.assertEqual(response_data["id"], str(self.test_workspace.id))
        self.assertEqual(response_data["parent_id"], str(self.default_workspace.id))

    def test_move_missing_parent_id(self):
        """Test that move fails when parent_id is missing."""
        request_context = self._create_request_context(self.customer_data, self.user_with_access, is_org_admin=False)
        headers = request_context["request"].META

        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {}
        response = client.post(url, data, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("The 'parent_id' field is required", str(response.data))

    def test_move_invalid_parent_id_uuid(self):
        """Test that move fails when parent_id is not a valid UUID."""
        request_context = self._create_request_context(self.customer_data, self.user_with_access, is_org_admin=False)
        headers = request_context["request"].META

        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": "invalid-uuid"}
        response = client.post(url, data, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_move_with_read_only_access(self, send_kafka_message):
        """Test that move fails when user only has read access to target workspace."""
        # Create user with only read access
        read_only_user = {"username": "read_only_user", "email": "read_only@example.com"}
        self._setup_access_for_principal(
            read_only_user["username"],
            "inventory:groups:read",  # Only read permission, not write
            str(self.default_workspace.id),
        )

        request_context = self._create_request_context(self.customer_data, read_only_user, is_org_admin=False)
        headers = request_context["request"].META

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **headers)

        # Verify - Should get 403 from WorkspaceAccessPermission class
        # since user only has read access but move requires write
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("You do not have permission to perform this action", str(response.data))

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_move_with_wildcard_permission(self, send_kafka_message):
        """Test that move succeeds when user has wildcard (*) permission on target workspace."""
        # Create user with wildcard permission
        wildcard_user = {"username": "wildcard_user", "email": "wildcard@example.com"}
        self._setup_access_for_principal(
            wildcard_user["username"],
            "inventory:groups:*",  # Wildcard permission includes write
            str(self.default_workspace.id),
        )

        request_context = self._create_request_context(self.customer_data, wildcard_user, is_org_admin=False)
        headers = request_context["request"].META

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **headers)

        # Verify
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.data
        self.assertEqual(response_data["id"], str(self.test_workspace.id))
        self.assertEqual(response_data["parent_id"], str(self.default_workspace.id))

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_move_with_source_access_but_no_target_access_unique(self, send_kafka_message):
        """Test that move fails with PermissionDenied when user has write access to source workspace but not target workspace.

        This specifically tests our inner _check_target_workspace_write_access method.
        """
        # Create a user with access to the source workspace but not target
        source_access_user = {"username": "source_access_user", "email": "source_access_user@example.com"}
        # Give this user write access to the source workspace's parent (standard_workspace)
        self._setup_access_for_principal(
            source_access_user["username"], "inventory:groups:write", str(self.standard_workspace.id)
        )

        # Create a unique target workspace that the user doesn't have access to
        unique_suffix = str(uuid4())[:8]
        target_workspace = Workspace.objects.create(
            name=f"Target Workspace No Access {unique_suffix}",
            description="Target workspace user has no access to",
            tenant=self.tenant,
            parent=self.default_workspace,  # Put target under default_workspace (no access for source_access_user)
            type=Workspace.Types.STANDARD,
        )

        # Use the user who has access to source but not target
        request_context = self._create_request_context(self.customer_data, source_access_user, is_org_admin=False)
        headers = request_context["request"].META

        # Execute: try to move test_workspace to target_workspace (user has no access to target)
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(target_workspace.id)}
        response = client.post(url, data, format="json", **headers)

        # Verify - Should get 403 from our permission class's target workspace check
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # The error message should come from our PermissionDenied exception
        self.assertIn(TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE, str(response.data))

    @patch("management.workspace.serializer.WorkspaceSerializer.move")
    def test_move_retry_success_after_serialization_failure(self, mock_serializer_move):
        """
        Test that move operation succeeds after SerializationFailure on first attempt.

        The retry=3 parameter in @pgtransaction.atomic should automatically retry
        when SerializationFailure occurs. This test verifies that after an initial
        failure, the operation succeeds on retry.
        """
        # Mock to fail once with SerializationFailure, then succeed
        # Note: Django wraps psycopg2 errors in OperationalError
        serialization_error = OperationalError("could not serialize access")
        serialization_error.__cause__ = SerializationFailure("could not serialize access due to concurrent update")

        success_response = {
            "id": str(self.test_workspace.id),
            "name": self.test_workspace.name,
            "parent_id": str(self.default_workspace.id),
        }

        # First call raises error, second call succeeds
        mock_serializer_move.side_effect = [serialization_error, success_response]

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify success after retry
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(self.test_workspace.id))
        self.assertEqual(response.data["parent_id"], str(self.default_workspace.id))

        # Verify the method was called twice (initial + 1 retry)
        self.assertEqual(mock_serializer_move.call_count, 2)

    @patch("management.workspace.serializer.WorkspaceSerializer.move")
    def test_move_retry_exhausted_after_three_failures(self, mock_serializer_move):
        """
        Test that move operation returns 503 SERVICE UNAVAILABLE after all retry attempts fail.

        The retry=3 parameter means: 1 initial attempt + 3 retries = 4 total attempts.
        If all attempts fail with SerializationFailure, the exception should propagate
        and be caught by the move() method, returning a 503 response with Retry-After header.
        """
        # Mock to always fail with SerializationFailure
        serialization_error = OperationalError("could not serialize access")
        serialization_error.__cause__ = SerializationFailure("could not serialize access due to concurrent update")

        mock_serializer_move.side_effect = serialization_error

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify returns 503 SERVICE UNAVAILABLE after all retries exhausted
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("temporarily unable to handle this request", str(response.data["detail"]))
        self.assertEqual(response["Retry-After"], "1")

        # Verify the method was called 4 times (1 initial + 3 retries)
        self.assertEqual(mock_serializer_move.call_count, 4)

    @patch("management.workspace.serializer.WorkspaceSerializer.move")
    def test_move_retry_success_on_third_attempt(self, mock_serializer_move):
        """
        Test that move succeeds on the 4th and final attempt (3rd retry).

        This verifies the retry mechanism continues trying until success
        or all attempts are exhausted.
        """
        serialization_error = OperationalError("could not serialize access")
        serialization_error.__cause__ = SerializationFailure("could not serialize access due to concurrent update")

        success_response = {
            "id": str(self.test_workspace.id),
            "name": self.test_workspace.name,
            "parent_id": str(self.default_workspace.id),
        }

        # Fail 3 times, succeed on 4th attempt (final retry)
        mock_serializer_move.side_effect = [
            serialization_error,
            serialization_error,
            serialization_error,
            success_response,
        ]

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify success on 4th attempt (3rd retry)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(self.test_workspace.id))

        # Verify the method was called 4 times before succeeding
        self.assertEqual(mock_serializer_move.call_count, 4)

    @patch("management.workspace.serializer.WorkspaceSerializer.move")
    def test_move_retry_deadlock_detected(self, mock_serializer_move):
        """
        Test that DeadlockDetected errors are also retried by the retry mechanism.

        According to django-pgtransaction documentation, the retry parameter
        handles both SerializationFailure and DeadlockDetected errors.
        """
        # Mock to fail with DeadlockDetected, then succeed
        deadlock_error = OperationalError("deadlock detected")
        deadlock_error.__cause__ = DeadlockDetected("deadlock detected")

        success_response = {
            "id": str(self.test_workspace.id),
            "name": self.test_workspace.name,
            "parent_id": str(self.default_workspace.id),
        }

        mock_serializer_move.side_effect = [deadlock_error, success_response]

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify success after retry
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify retry happened
        self.assertEqual(mock_serializer_move.call_count, 2)

    @patch("management.workspace.serializer.WorkspaceSerializer.move")
    def test_move_retry_deadlock_exhausted(self, mock_serializer_move):
        """
        Test that DeadlockDetected returns 500 error after all retries exhausted.

        Unlike SerializationFailure (409), DeadlockDetected should return 500
        as it indicates a more serious internal server error.
        The retry=3 parameter means 1 initial + 3 retries = 4 total attempts.
        """
        deadlock_error = OperationalError("deadlock detected")
        deadlock_error.__cause__ = DeadlockDetected("deadlock detected")

        mock_serializer_move.side_effect = deadlock_error

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify returns 500 INTERNAL SERVER ERROR for deadlock
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("Internal server error", str(response.data["detail"]))

        # Verify all retry attempts were made (1 initial + 3 retries = 4 total)
        self.assertEqual(mock_serializer_move.call_count, 4)

    @patch("management.workspace.serializer.WorkspaceSerializer.move")
    def test_move_no_retry_on_validation_error(self, mock_serializer_move):
        """
        Test that ValidationError does not trigger retries.

        Only SerializationFailure and DeadlockDetected should be retried.
        Other errors like ValidationError should fail immediately without retry.
        """

        # Mock to raise ValidationError
        validation_error = ValidationError("Validation failed")
        mock_serializer_move.side_effect = validation_error

        # Execute
        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Verify it fails immediately with 400 BAD REQUEST
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify the method was only called once (no retries for ValidationError)
        self.assertEqual(mock_serializer_move.call_count, 1)

    @patch(
        "management.workspace.relation_api_dual_write_workspace_handler.RelationApiDualWriteWorkspaceHandler._replicate"
    )
    def test_move_dual_write_operational_error_propagates_for_retry(self, mock_replicate):
        """
        Test that OperationalError from dual write handler propagates through for pgtransaction retry on move.

        Same fix as create path: _replicate() now re-raises OperationalError instead of wrapping
        in DualWriteException, allowing pgtransaction to retry. When retries are exhausted, returns 503.
        """
        serialization_error = OperationalError("could not serialize access")
        serialization_error.__cause__ = SerializationFailure("could not serialize access due to concurrent update")

        mock_replicate.side_effect = serialization_error

        url = reverse("v2_management:workspace-move", kwargs={"pk": self.test_workspace.id})
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # OperationalError propagates through _replicate -> pgtransaction retries -> exhausted -> 503
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("temporarily unable to handle this request", str(response.data["detail"]))
        self.assertEqual(response["Retry-After"], "1")


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=True)
class WorkspaceTestsList(WorkspaceViewTests):
    """Tests for listing workspaces."""

    def assertSuccessfulList(self, response, payload):
        """Common list success assertions."""
        self.assertIsInstance(payload.get("data"), list)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.get("content-type"), "application/json")
        for keyname in ["meta", "links", "data"]:
            self.assertIn(keyname, payload)
        for keyname in ["name", "id", "parent_id", "description", "type"]:
            self.assertIn(keyname, payload.get("data")[0])

    def assertType(self, payload, expected_type):
        """Ensure the correct type on data."""
        for ws in payload.get("data"):
            self.assertEqual(ws["type"], expected_type)

    def test_workspace_list_unfiltered(self):
        """List workspaces unfiltered."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(url, None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_all(self):
        """List workspaces type=all."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=all", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_standard(self):
        """List workspaces type=standard."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=standard", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertNotEqual(Workspace.objects.count(), Workspace.objects.filter(type="standard").count())
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.filter(type="standard").count())
        self.assertType(payload, "standard")

    def test_workspace_list_root(self):
        """List workspaces type=root."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=root", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["id"], str(self.root_workspace.id))
        self.assertType(payload, "root")

    def test_workspace_list_default(self):
        """List workspaces type=default."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=default", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["id"], str(self.default_workspace.id))
        self.assertType(payload, "default")

    def test_workspace_list_multiple_types(self):
        """List workspaces with comma-separated type values."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=standard,ungrouped-hosts", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        expected_count = Workspace.objects.filter(type__in=["standard", "ungrouped-hosts"]).count()
        self.assertEqual(payload.get("meta").get("count"), expected_count)
        returned_types = {ws["type"] for ws in payload.get("data")}
        self.assertTrue(returned_types.issubset({"standard", "ungrouped-hosts"}))
        self.assertIn(str(self.standard_workspace.id), [ws["id"] for ws in payload.get("data")])
        self.assertIn(str(self.ungrouped_workspace.id), [ws["id"] for ws in payload.get("data")])

    def test_workspace_list_multiple_types_with_whitespace_and_case(self):
        """List workspaces with whitespace and mixed case in comma-separated type values."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=STANDARD, ungrouped-hosts", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        expected_count = Workspace.objects.filter(type__in=["standard", "ungrouped-hosts"]).count()
        self.assertEqual(payload.get("meta").get("count"), expected_count)
        returned_types = {ws["type"] for ws in payload.get("data")}
        self.assertTrue(returned_types.issubset({"standard", "ungrouped-hosts"}))

    def test_workspace_list_multiple_types_with_all(self):
        """List workspaces with type=standard,all returns all types (all takes precedence)."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=standard,all", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_multiple_types_with_all_different_position_and_case(self):
        """List workspaces with type=ALL,standard returns all types (all takes precedence, case-insensitive)."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=ALL,standard", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_multiple_types_invalid_value(self):
        """List workspaces with an invalid type in comma-separated values returns a structured 400 error."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=standard,invalid", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsInstance(response.data, dict)
        self.assertIn("detail", response.data)
        message = str(response.data["detail"])
        self.assertIn("invalid", message)
        self.assertIn("allowed", message.lower())

    def test_workspace_list_multiple_types_root_default(self):
        """List workspaces with type=root,default returns only root and default workspaces."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=root,default", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        expected_count = Workspace.objects.filter(type__in=[Workspace.Types.ROOT, Workspace.Types.DEFAULT]).count()
        self.assertEqual(payload.get("meta").get("count"), expected_count)
        returned_ids = [ws["id"] for ws in payload.get("data")]
        self.assertIn(str(self.root_workspace.id), returned_ids)
        self.assertIn(str(self.default_workspace.id), returned_ids)

    def test_workspace_list_empty_type_returns_all(self):
        """List workspaces with ?type= (empty string) returns all workspaces (not zero)."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_type_nul_byte_returns_400(self):
        """List workspaces with NUL byte in type param returns 400."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=standard\x00evil", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_workspace_list_filter_by_ids_with_multiple_types(self):
        """Test filtering workspaces by ids with multiple comma-separated types."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        ids = f"{self.standard_workspace.id},{self.ungrouped_workspace.id},{self.root_workspace.id}"
        response = client.get(f"{url}?ids={ids}&type=standard,ungrouped-hosts", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        # Only standard and ungrouped-hosts workspaces should be returned
        self.assertEqual(payload.get("meta").get("count"), 2)
        returned_ids = [ws["id"] for ws in payload.get("data")]
        self.assertIn(str(self.standard_workspace.id), returned_ids)
        self.assertIn(str(self.ungrouped_workspace.id), returned_ids)
        self.assertNotIn(str(self.root_workspace.id), returned_ids)

    def test_workspace_list_queryset_by_tenant(self):
        """List workspaces only for the request tenant."""
        tenant = Tenant.objects.create(tenant_name="Tenant 2")
        t2_root_workspace = Workspace.objects.create(name="Tenant 2 Root", type="root", tenant=tenant)

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=root", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["id"], str(self.root_workspace.id))
        self.assertType(payload, "root")

    def test_workspace_list_filter_by_name_substring(self):
        """Test that name filter without wildcards does case-insensitive substring match."""
        ws_name_1 = "Sales Team Alpha"
        ws_name_2 = "Sales Team Beta"
        ws_name_3 = "Engineering Squad"
        Workspace.objects.bulk_create(
            [
                Workspace(
                    name=ws_name_1,
                    tenant=self.tenant,
                    type="standard",
                    parent_id=self.default_workspace.id,
                ),
                Workspace(
                    name=ws_name_2,
                    tenant=self.tenant,
                    type="standard",
                    parent_id=self.default_workspace.id,
                ),
                Workspace(
                    name=ws_name_3,
                    tenant=self.tenant,
                    type="standard",
                    parent_id=self.default_workspace.id,
                ),
            ]
        )

        url = reverse("v2_management:workspace-list")
        client = APIClient()

        # Full name "Sales Team Alpha" returns only that workspace
        response = client.get(f"{url}?name=Sales Team Alpha", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["name"], ws_name_1)

        # Partial string "Sales" matches both Sales workspaces (substring semantics)
        response = client.get(f"{url}?name=Sales", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 2)

        # Full name match for "Engineering Squad"
        response = client.get(f"{url}?name=Engineering Squad", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["name"], ws_name_3)

    def test_workspace_list_filter_by_name_wildcard(self):
        """Test that name filter with * wildcards does glob pattern matching."""
        ws_name_1 = "Sales Team Alpha"
        ws_name_2 = "Sales Team Beta"
        ws_name_3 = "Engineering Squad"
        Workspace.objects.bulk_create(
            [
                Workspace(
                    name=ws_name_1,
                    tenant=self.tenant,
                    type="standard",
                    parent_id=self.default_workspace.id,
                ),
                Workspace(
                    name=ws_name_2,
                    tenant=self.tenant,
                    type="standard",
                    parent_id=self.default_workspace.id,
                ),
                Workspace(
                    name=ws_name_3,
                    tenant=self.tenant,
                    type="standard",
                    parent_id=self.default_workspace.id,
                ),
            ]
        )

        url = reverse("v2_management:workspace-list")
        client = APIClient()

        # Substring wildcard *Sales* matches both Sales Team workspaces
        response = client.get(f"{url}?name=*Sales*", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 2)

        # Prefix wildcard Sales* matches both Sales Team workspaces
        response = client.get(f"{url}?name=Sales*", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 2)

        # Suffix wildcard *Alpha matches only Sales Team Alpha
        response = client.get(f"{url}?name=*Alpha", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["name"], ws_name_1)

        # Substring wildcard *Team* matches both Sales Team workspaces
        response = client.get(f"{url}?name=*Team*", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 2)

        # Complex pattern Sales*Alpha matches only Sales Team Alpha
        response = client.get(f"{url}?name=Sales*Alpha", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["name"], ws_name_1)

    def test_workspace_list_filter_by_name_wildcard_no_match(self):
        """Test that a wildcard pattern matching nothing returns empty list."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?name=zzz*", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])

    def test_workspace_list_filter_by_name_wildcard_star_returns_all(self):
        """Test that name=* returns all workspaces (no filter applied)."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        total_count = Workspace.objects.count()

        response = client.get(f"{url}?name=*", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), total_count)

    def test_workspace_list_filter_by_name_is_case_insensitive(self):
        """Test that name filter is case-insensitive for both substring and wildcard matching."""
        Workspace.objects.create(
            name="Sales Team Alpha",
            tenant=self.tenant,
            type="standard",
            parent_id=self.default_workspace.id,
        )

        url = reverse("v2_management:workspace-list")
        client = APIClient()

        # Lowercase substring match
        response = client.get(f"{url}?name=sales team alpha", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["name"], "Sales Team Alpha")

        # Uppercase substring match
        response = client.get(f"{url}?name=SALES TEAM ALPHA", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["name"], "Sales Team Alpha")

        # Case-insensitive wildcard
        response = client.get(f"{url}?name=*sales*", None, format="json", **self.headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["name"], "Sales Team Alpha")

    def test_workspace_list_filter_by_name_empty_string(self):
        """Test that filtering by empty name string returns all workspaces."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?name=", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Empty name filter should return all workspaces (same as no filter)
        self.assertIn("data", response.data)

    def test_workspace_list_filter_by_name_whitespace_only(self):
        """Test that filtering by whitespace-only name string returns all workspaces."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?name=   ", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Whitespace-only name filter should return all workspaces (same as no filter)
        self.assertIn("data", response.data)

    def test_workspace_list_filter_by_name_with_nul_character(self):
        """Test that filtering by name containing NUL character returns a validation error."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # Simulate a NUL character in the name parameter
        response = client.get(f"{url}?name=test\x00name", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.get("content-type"), "application/problem+json")
        self.assertIn("name", str(response.data))
        self.assertIn("invalid characters", str(response.data))

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_workspace_list_authorization_platform_default(self, send_kafka_message):
        """List workspaces authorization."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        request = request_context["request"]
        headers = request.META

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=all", None, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self._setup_access_for_principal(self.user_data["username"], "inventory:groups:read", platform_default=True)
        response = client.get(f"{url}?type=all", None, format="json", **headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_workspace_list_authorization_platform_default_with_wildcard(self, send_kafka_message):
        """List workspaces authorization with wildcard permission."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        request = request_context["request"]
        headers = request.META

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?type=all", None, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self._setup_access_for_principal(self.user_data["username"], "inventory:*:read", platform_default=True)
        response = client.get(f"{url}?type=all", None, format="json", **headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

        # Make sure "inventory:*:*" also works
        Group.objects.get(platform_default=True).delete()
        response = client.get(f"{url}?type=all", None, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self._setup_access_for_principal(self.user_data["username"], "inventory:*:*", platform_default=True)
        response = client.get(f"{url}?type=all", None, format="json", **headers)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_workspace_list_authorization_custom_role(self, send_kafka_message):
        """List workspaces authorization."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        request = request_context["request"]
        headers = request.META
        Workspace.objects.create(
            name="Another Standard Workspace",
            tenant=self.tenant,
            type="standard",
            parent_id=self.default_workspace.id,
        )

        url = reverse("v2_management:workspace-list")
        client = APIClient()

        self._setup_access_for_principal(
            self.user_data["username"], "inventory:groups:read", workspace_id=str(uuid4())
        )
        response = client.get(f"{url}?type=all", None, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Will include ancestors of the workspace: root->default->standard_workspace, but not the another standard ws
        self._setup_access_for_principal(
            self.user_data["username"], "inventory:groups:read", workspace_id=str(self.standard_workspace.id)
        )
        response = client.get(f"{url}?type=all", None, format="json", **headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(len(payload.get("data")), 4)

        # Account for ungrouped and new standard workspace not having access
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count() - 2)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_workspace_list_no_limit(self, send_kafka_message):
        """Test that when limit == -1, we return all records"""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        request = request_context["request"]
        headers = request.META
        workspaces = [
            Workspace(
                name=f"Workspace {n}",
                tenant=self.tenant,
                type="standard",
                parent=self.default_workspace,
            )
            for n in range(1, 21)
        ]
        Workspace.objects.bulk_create(workspaces)

        url = reverse("v2_management:workspace-list")
        client = APIClient()

        self._setup_access_for_principal(self.user_data["username"], "inventory:groups:read")

        response = client.get(f"{url}?type=all", None, format="json", **headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(len(payload.get("data")), 10)
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

        response = client.get(f"{url}?type=all&limit=-1", None, format="json", **headers)
        payload = response.data
        self.assertSuccessfulList(response, payload)
        self.assertEqual(len(payload.get("data")), Workspace.objects.count())
        self.assertEqual(payload.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_filter_by_single_id(self):
        """Test filtering workspaces by a single id."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?ids={self.standard_workspace.id}", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["id"], str(self.standard_workspace.id))

    def test_workspace_list_filter_by_multiple_ids(self):
        """Test filtering workspaces by multiple comma-separated ids."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # Use two standard workspaces since ids filter defaults to type=standard
        ids = f"{self.standard_workspace.id},{self.standard_sub_workspace.id}"
        response = client.get(f"{url}?ids={ids}", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 2)
        returned_ids = [ws["id"] for ws in payload.get("data")]
        self.assertIn(str(self.standard_workspace.id), returned_ids)
        self.assertIn(str(self.standard_sub_workspace.id), returned_ids)

    def test_workspace_list_filter_by_ids_empty_string(self):
        """Test that filtering by empty ids string returns all workspaces."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?ids=", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Empty ids filter should return all workspaces (same as no filter)
        self.assertIn("data", response.data)
        self.assertEqual(response.data.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_filter_by_invalid_ids(self):
        """Test that filtering by invalid UUID returns a validation error."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?ids=invalid-uuid", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))

    def test_workspace_list_filter_by_multiple_ids_with_invalid(self):
        """Test that filtering by ids with one invalid UUID returns a validation error."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        ids = f"{self.standard_workspace.id},invalid-uuid"
        response = client.get(f"{url}?ids={ids}", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))

    def test_workspace_list_filter_by_nonexistent_ids(self):
        """Test filtering by a valid UUID that doesn't exist returns empty results."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        non_existent_uuid = "00000000-0000-0000-0000-000000000000"
        response = client.get(f"{url}?ids={non_existent_uuid}", None, format="json", **self.headers)
        payload = response.data

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(payload.get("meta").get("count"), 0)
        self.assertEqual(len(payload.get("data")), 0)

    def test_workspace_list_filter_by_ids_with_type_all(self):
        """Test filtering workspaces by ids with type=all returns all types."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        ids = f"{self.standard_workspace.id},{self.default_workspace.id},{self.root_workspace.id}"
        response = client.get(f"{url}?ids={ids}&type=all", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        # All three workspaces should be returned when type=all is specified
        self.assertEqual(payload.get("meta").get("count"), 3)
        returned_ids = [ws["id"] for ws in payload.get("data")]
        self.assertIn(str(self.standard_workspace.id), returned_ids)
        self.assertIn(str(self.default_workspace.id), returned_ids)
        self.assertIn(str(self.root_workspace.id), returned_ids)

    def test_workspace_list_filter_by_ids_defaults_to_standard(self):
        """Test that ids filter defaults to type=standard when type not specified."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # Include workspaces of different types
        ids = f"{self.standard_workspace.id},{self.default_workspace.id},{self.root_workspace.id}"
        response = client.get(f"{url}?ids={ids}", None, format="json", **self.headers)
        payload = response.data

        self.assertSuccessfulList(response, payload)
        # Only the standard workspace should be returned (default type filter)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["id"], str(self.standard_workspace.id))
        self.assertEqual(payload.get("data")[0]["type"], "standard")

    def test_workspace_list_filter_by_parent_id(self):
        """Test filtering workspaces by parent_id returns only direct children."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # Filter for children of default_workspace — should return standard workspaces under it
        response = client.get(
            f"{url}?parent_id={self.default_workspace.id}&type=all", None, format="json", **self.headers
        )
        payload = response.data

        self.assertSuccessfulList(response, payload)
        # default_workspace has: standard_workspace, ungrouped_workspace as direct children
        self.assertEqual(payload.get("meta").get("count"), 2)
        returned_ids = [ws["id"] for ws in payload.get("data")]
        self.assertIn(str(self.standard_workspace.id), returned_ids)
        self.assertIn(str(self.ungrouped_workspace.id), returned_ids)

    def test_workspace_list_filter_by_parent_id_non_root_parent(self):
        """Test that parent_id works for intermediate hierarchy nodes, not just root/default."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # standard_sub_workspace is a child of standard_workspace
        response = client.get(
            f"{url}?parent_id={self.standard_workspace.id}&type=all", None, format="json", **self.headers
        )
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["id"], str(self.standard_sub_workspace.id))

    def test_workspace_list_filter_by_parent_id_returns_only_direct_children(self):
        """Test that parent_id filter returns only direct children, not deeper descendants."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # standard_sub_workspace is a child of standard_workspace, NOT of default_workspace
        response = client.get(
            f"{url}?parent_id={self.default_workspace.id}&type=standard", None, format="json", **self.headers
        )
        payload = response.data

        self.assertSuccessfulList(response, payload)
        returned_ids = [ws["id"] for ws in payload.get("data")]
        # standard_sub_workspace should NOT appear — it's a grandchild, not a direct child
        self.assertNotIn(str(self.standard_sub_workspace.id), returned_ids)
        self.assertIn(str(self.standard_workspace.id), returned_ids)
        # ungrouped_workspace is a direct child of default but has type=ungrouped-hosts, not standard
        self.assertNotIn(str(self.ungrouped_workspace.id), returned_ids)

    def test_workspace_list_filter_by_parent_id_with_name(self):
        """Test combining parent_id and name filters."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(
            f"{url}?parent_id={self.default_workspace.id}&name=Standard Workspace&type=all",
            None,
            format="json",
            **self.headers,
        )
        payload = response.data

        self.assertSuccessfulList(response, payload)
        self.assertEqual(payload.get("meta").get("count"), 1)
        self.assertEqual(payload.get("data")[0]["id"], str(self.standard_workspace.id))

    def test_workspace_list_filter_by_parent_id_with_ids(self):
        """Test combining parent_id with ids returns only workspaces matching both filters."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # standard_sub_workspace is a child of standard_workspace, NOT of default_workspace
        # so it should be excluded by the parent_id filter even though it's in the ids list
        ids_param = f"{self.standard_workspace.id},{self.standard_sub_workspace.id}"
        response = client.get(
            f"{url}?parent_id={self.default_workspace.id}&ids={ids_param}&type=all",
            None,
            format="json",
            **self.headers,
        )
        payload = response.data

        self.assertSuccessfulList(response, payload)
        returned_ids = {ws["id"] for ws in payload.get("data")}
        # Only standard_workspace is both in the ids list AND a child of default_workspace
        self.assertIn(str(self.standard_workspace.id), returned_ids)
        self.assertNotIn(str(self.standard_sub_workspace.id), returned_ids)

    def test_workspace_list_filter_by_parent_id_empty_string(self):
        """Test that filtering by empty parent_id returns all workspaces."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?parent_id=", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.data)
        self.assertEqual(response.data.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_filter_by_parent_id_whitespace_only(self):
        """Test that filtering by whitespace-only parent_id returns all workspaces."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?parent_id=   ", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.data)
        self.assertEqual(response.data.get("meta").get("count"), Workspace.objects.count())

    def test_workspace_list_filter_by_parent_id_invalid_uuid(self):
        """Test that filtering by invalid parent_id UUID returns a validation error."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?parent_id=not-a-uuid", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Assert on error structure rather than exact message text
        error_fields = [e["field"] for e in response.data.get("errors", [])]
        self.assertIn("parent_id", error_fields)

    def test_workspace_list_filter_by_parent_id_nonexistent(self):
        """Test that filtering by a valid but non-existent parent_id returns empty results."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        non_existent_uuid = "00000000-0000-0000-0000-000000000000"
        response = client.get(f"{url}?parent_id={non_existent_uuid}", None, format="json", **self.headers)
        payload = response.data

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(payload.get("meta").get("count"), 0)
        self.assertEqual(len(payload.get("data")), 0)

    def test_workspace_list_filter_by_parent_id_with_nul_character(self):
        """Test that filtering by parent_id containing NUL character returns a validation error."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(f"{url}?parent_id=test\x00uuid", None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.get("content-type"), "application/problem+json")
        self.assertIn("parent_id", str(response.data))
        self.assertIn("invalid characters", str(response.data))

    def test_workspace_list_filter_by_parent_id_cross_tenant(self):
        """Test that parent_id from another tenant returns no results and does not leak current-tenant workspaces."""
        other_tenant = Tenant.objects.create(tenant_name="Other Tenant")
        other_root = Workspace.objects.create(name="Other Root", type="root", tenant=other_tenant)

        # Sanity check: current tenant has workspaces
        current_tenant_workspaces = Workspace.objects.filter(tenant=self.tenant)
        self.assertTrue(current_tenant_workspaces.exists())

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        # Try to filter by a parent_id that belongs to a different tenant
        response = client.get(f"{url}?parent_id={other_root.id}", None, format="json", **self.headers)
        payload = response.data

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(payload.get("meta").get("count"), 0)

        # Verify no current-tenant workspaces leaked into the response
        returned_ids = {ws["id"] for ws in payload.get("data")}
        self.assertNotIn(str(self.default_workspace.id), returned_ids)


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=True)
class WorkspaceTestsDetail(WorkspaceViewTests):
    """Tests for get workspace detail."""

    def test_get_workspace(self):
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data.get("name"), "Standard Workspace")
        self.assertEqual(data.get("description"), "Standard Workspace - description")
        self.assertNotEqual(data.get("id"), "")
        self.assertIsNotNone(data.get("id"))
        self.assertNotEqual(data.get("created"), "")
        self.assertNotEqual(data.get("modified"), "")
        self.assertEqual(response.get("content-type"), "application/json")
        self.assertEqual(data.get("ancestry"), None)
        self.assertEqual(data.get("type"), "standard")
        self.assertEqual(response.get("content-type"), "application/json")

    def test_get_workspace_with_ancestry(self):
        base_url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        url = f"{base_url}?include_ancestry=true"
        client = APIClient()
        response = client.get(url, None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data.get("name"), "Standard Workspace")
        self.assertEqual(data.get("description"), "Standard Workspace - description")
        self.assertNotEqual(data.get("id"), "")
        self.assertIsNotNone(data.get("id"))
        self.assertNotEqual(data.get("created"), "")
        self.assertNotEqual(data.get("modified"), "")
        self.assertCountEqual(
            data.get("ancestry"),
            [
                {"name": self.root_workspace.name, "id": str(self.root_workspace.id), "parent_id": None},
                {
                    "name": self.default_workspace.name,
                    "id": str(self.default_workspace.id),
                    "parent_id": str(self.root_workspace.id),
                },
            ],
        )
        self.assertEqual(data.get("type"), "standard")
        self.assertEqual(response.get("content-type"), "application/json")

    def test_get_workspace_not_found(self):
        # Use a valid UUID format that doesn't exist in the database
        non_existent_uuid = "00000000-0000-0000-0000-000000000000"
        url = reverse("v2_management:workspace-detail", kwargs={"pk": non_existent_uuid})
        client = APIClient()
        response = client.get(url, None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        status_code = response.data.get("status")
        detail = response.data.get("detail")

        self.assertEqual(detail, "No Workspace matches the given query.")
        self.assertEqual(status_code, 404)
        self.assertEqual(response.get("content-type"), "application/problem+json")

    def test_get_workspace_unauthorized(self):
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        status_code = response.data.get("status")
        detail = response.data.get("detail")

        self.assertEqual(detail, "You do not have permission to perform this action.")
        self.assertEqual(status_code, 403)
        self.assertEqual(response.get("content-type"), "application/problem+json")

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.root_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_get_workspace_authorized_through_custom_role(self, send_kafka_message):
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META
        self._setup_access_for_principal(self.user_data["username"], "inventory:groups:read")

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Can also get the ancestor
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.root_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_get_workspace_authorized_through_custom_role_with_resourcedef(self, send_kafka_message):
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META
        # Assign permission of non target workspace
        self._setup_access_for_principal(
            self.user_data["username"], "inventory:groups:read", workspace_id=str(uuid4())
        )

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Assign permission of target workspace
        self._setup_access_for_principal(
            self.user_data["username"], "inventory:groups:read", workspace_id=[str(self.standard_workspace.id)]
        )
        response = client.get(url, None, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Can also get the ancestor
        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.root_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Can't access another workspace within the same ancestor
        another_ws = Workspace.objects.create(
            name="Another Standard Workspace",
            tenant=self.tenant,
            type="standard",
            parent_id=self.default_workspace.id,
        )
        url = reverse("v2_management:workspace-detail", kwargs={"pk": another_ws.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    def test_get_workspace_authorized_through_platform_default_access(self, send_kafka_message):
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        request = request_context["request"]
        headers = request.META
        self._setup_access_for_principal(self.user_data["username"], "inventory:groups:read", platform_default=True)

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.standard_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        url = reverse("v2_management:workspace-detail", kwargs={"pk": self.root_workspace.id})
        client = APIClient()
        response = client.get(url, None, format="json", **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=False)
class WorkspaceViewTestsV2Disabled(WorkspaceViewTests):
    def test_get_workspace_list(self):
        """Test for accessing v2 APIs which should be disabled by default."""
        url = "/api/rbac/v2/workspaces/"
        client = APIClient()
        response = client.get(url, None, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_settings(ATOMIC_RETRY_DISABLED=True, WORKSPACE_HIERARCHY_DEPTH_LIMIT=2, V2_APIS_ENABLED=True)
class WorkspaceViewTestsWithHierarchyLimit(TransactionalWorkspaceViewTests):
    """Test workspace hierarchy limits."""

    def test_create_nested_workspace_valid(self):
        """Test creating a workspace with valid depth."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        workspace = {"name": "New Workspace", "description": "Workspace", "parent_id": self.default_workspace.id}
        response = client.post(url, workspace, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_nested_workspace_invalid(self):
        """Test creating a workspace with invalid depth."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        workspace = {"name": "New Workspace", "description": "Workspace", "parent_id": self.standard_workspace.id}
        response = client.post(url, workspace, format="json", **self.headers)

        status_code = response.data.get("status")
        detail = response.data.get("detail")
        self.assertEqual(detail, f"Workspaces may only nest {settings.WORKSPACE_HIERARCHY_DEPTH_LIMIT} levels deep.")

        self.assertEqual(status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.get("content-type"), "application/problem+json")


@override_settings(ATOMIC_RETRY_DISABLED=True, WORKSPACE_RESTRICT_DEFAULT_PEERS=True, V2_APIS_ENABLED=True)
class WorkspaceViewTestsWithPeerRestrictions(TransactionalWorkspaceViewTests):
    """Test workspace peer restrictions."""

    def test_create_nested_workspace_against_root(self):
        """Test creating a workspace with root as the parent."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        workspace = {"name": "New Workspace", "description": "Workspace", "parent_id": self.root_workspace.id}
        response = client.post(url, workspace, format="json", **self.headers)

        status_code = response.data.get("status")
        detail = response.data.get("detail")
        self.assertEqual(detail, "Sub-workspaces may only be created under the default workspace.")

        self.assertEqual(status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.get("content-type"), "application/problem+json")


@override_settings(V2_APIS_ENABLED=True, WORKSPACE_HIERARCHY_DEPTH_LIMIT=100, WORKSPACE_RESTRICT_DEFAULT_PEERS=False)
class WorkspaceAuditLogTests(TransactionalWorkspaceViewTests):
    """Tests for workspace audit logging."""

    def setUp(self):
        super().setUp()
        self.tuples = InMemoryTuples()
        self.in_memory_replicator = InMemoryRelationReplicator(self.tuples)

        from management.workspace.view import WorkspaceViewSet

        def get_queryset_without_lock(self):
            from management.base_viewsets import BaseV2ViewSet

            return BaseV2ViewSet.get_queryset(self)

        self.patcher = patch.object(WorkspaceViewSet, "get_queryset", get_queryset_without_lock)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        super().tearDown()

    def _assert_audit_log(self, action: str, description: str):
        client = APIClient()
        audit_logs = client.get("/api/rbac/v1/auditlogs/", **self.headers).data["data"]

        self.assertIn(
            {
                "action": action,
                "description": description,
                "resource_type": AuditLog.WORKSPACE,
                "principal_username": self.user_data["username"],
            },
            [
                {k: log[k] for k in ["action", "description", "resource_type", "principal_username"]}
                for log in audit_logs
            ],
        )

    def test_create_workspace_audit_log(self):
        """Test that creating a workspace creates an audit log entry."""
        workspace = {"name": "Audit Log Workspace", "description": "Test", "parent_id": self.default_workspace.id}

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.post(url, workspace, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self._assert_audit_log(action=AuditLog.CREATE, description="Created workspace: Audit Log Workspace")

    def test_update_workspace_audit_log(self):
        """Test that updating a workspace creates an audit log entry."""
        workspace = Workspace.objects.create(
            name="Original Name",
            description="Original Desc",
            tenant=self.tenant,
            parent=self.standard_workspace,
        )

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()
        data = {"name": "Updated Name", "description": "Updated Desc", "parent_id": workspace.parent_id}
        response = client.put(url, data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(
            action=AuditLog.EDIT,
            description=f"workspace {workspace.name}:\nEdited name\nEdited description",
        )

    def test_update_workspace_name_only_audit_log(self):
        """Test that updating only the name is reflected in the audit log."""
        workspace = Workspace.objects.create(
            name="Name Only",
            description="Keep This",
            tenant=self.tenant,
            parent=self.standard_workspace,
        )

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()
        data = {"name": "New Name", "description": "Keep This", "parent_id": workspace.parent_id}
        response = client.put(url, data, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(
            action=AuditLog.EDIT,
            description=f"workspace {workspace.name}:\nEdited name",
        )

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate_workspace")
    def test_delete_workspace_audit_log(self, replicate_workspace, replicate):
        """Test that deleting a workspace creates an audit log entry."""
        replicate.side_effect = self.in_memory_replicator.replicate
        workspace = Workspace.objects.create(
            name="Workspace To Delete",
            description="Will be deleted",
            tenant=self.tenant,
            parent=self.root_workspace,
        )
        relationship = create_relationship(
            ("rbac", "workspace"),
            str(workspace.id),
            ("rbac", "workspace"),
            str(self.root_workspace.id),
            "parent",
        )
        self.tuples.write([relationship], [])

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()
        response = client.delete(url, None, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self._assert_audit_log(action=AuditLog.DELETE, description="Deleted workspace: Workspace To Delete")

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_move_workspace_audit_log(self, replicate):
        """Test that moving a workspace creates an audit log entry."""
        replicate.side_effect = self.in_memory_replicator.replicate

        source_workspace = self.service.create(
            {"name": "Source WS", "parent_id": self.default_workspace.id}, self.tenant
        )
        target_workspace = self.service.create(
            {"name": "Target WS", "parent_id": self.default_workspace.id}, self.tenant
        )

        url = reverse("v2_management:workspace-move", kwargs={"pk": source_workspace.id})
        client = APIClient()
        response = client.post(url, {"parent_id": target_workspace.id}, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(
            action=AuditLog.EDIT,
            description="Moved workspace: Source WS to parent Target WS",
        )

    def test_partial_update_workspace_audit_log(self):
        """Test that a PATCH with only description does not log 'Edited name'."""
        workspace = Workspace.objects.create(
            name="Partial Update WS",
            description="Old Desc",
            tenant=self.tenant,
            parent=self.standard_workspace,
        )

        url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
        client = APIClient()
        response = client.patch(url, {"description": "New Desc"}, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_audit_log(
            action=AuditLog.EDIT,
            description=f"workspace {workspace.name}:\nEdited description",
        )
