#
# Copyright 2019 Red Hat, Inc.
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
"""Test the internal viewset."""

from abc import abstractmethod
import logging

from django.db.models import Count
from rest_framework import status
from rest_framework.test import APIClient
from django.test import override_settings
from django.urls import reverse
from datetime import timedelta

from django.utils import timezone
from unittest.mock import MagicMock
from unittest.mock import patch
from kessel.relations.v1beta1 import common_pb2
from kessel.relations.v1beta1 import lookup_pb2, check_pb2, relation_tuples_pb2
from kessel.relations.v1beta1 import lookup_pb2_grpc, check_pb2_grpc, relation_tuples_pb2_grpc
from kessel.inventory.v1beta2 import check_response_pb2, allowed_pb2
import pytz
import json
import uuid
from grpc import RpcError
from api.cross_access.model import CrossAccountRequest
from api.models import User, Tenant
from api.utils import reset_imported_tenants
from management.audit_log.model import AuditLog
from management.cache import TenantCache
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.models import BindingMapping, Group, Permission, Policy, Role, Workspace
from management.principal.model import Principal
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import Access, ResourceDefinition
from management.role.relation_api_dual_write_handler import (
    RelationApiDualWriteHandler,
    SeedingRelationApiDualWriteHandler,
)
from management.role.user_source import SourceKey
from management.tenant_mapping.model import TenantMapping
from management.tenant_service.v1 import V1TenantBootstrapService
from management.tenant_service.v2 import V2TenantBootstrapService
from management.workspace.model import Workspace
from migration_tool.in_memory_tuples import (
    all_of,
    InMemoryRelationReplicator,
    InMemoryTuples,
    relation,
    resource,
    subject,
)
from migration_tool.utils import create_relationship
from kessel.inventory.v1beta2 import (
    inventory_service_pb2_grpc,
    reporter_reference_pb2,
    resource_reference_pb2,
    subject_reference_pb2,
    check_response_pb2,
    allowed_pb2,
)
from kessel.inventory.v1beta2.check_request_pb2 import CheckRequest
from tests.identity_request import IdentityRequest
from tests.management.role.test_dual_write import RbacFixture
from tests.rbac.test_middleware import EnvironmentVarGuard
from tests.v2_util import seed_v2_role_from_v1, bootstrap_tenant_for_v2_test


class BaseInternalViewsetTests(IdentityRequest):
    """Base class for testing internal views"""

    _tuples: InMemoryTuples

    @abstractmethod
    def setUp(self):
        """Set up the base internal viewset tests."""
        super().setUp()
        self.client = APIClient()
        self.customer = self.customer_data
        self.internal_request_context = self._create_request_context(self.customer, self.user_data, is_internal=True)

        self.request = self.internal_request_context["request"]
        user = User()
        user.username = self.user_data["username"]
        user.account = self.customer_data["account_id"]
        self.request.user = user

        self.group = Group(name="System Group", system=True, tenant=self.tenant)
        self.group.save()
        self.role = Role.objects.create(
            name="System Role",
            description="A role for a group.",
            system=True,
            tenant=self.tenant,
        )
        self.policy = Policy.objects.create(name="System Policy", group=self.group, tenant=self.tenant)
        self.policy.roles.add(self.role)
        self.policy.save()
        self.group.policies.add(self.policy)
        self.group.save()
        self.public_tenant = Tenant.objects.get(tenant_name="public")
        self._prior_logging_disable_level = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self._tuples = InMemoryTuples()

    @abstractmethod
    def tearDown(self):
        """Tear down base internal viewset tests."""
        logging.disable(self._prior_logging_disable_level)
        # Clear the principal cache to avoid test isolation issues
        from management.utils import PRINCIPAL_CACHE

        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)

        super().tearDown()


@override_settings(
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "management.relation_replicator.outbox_replicator": {
                "level": "INFO",
            },
        },
    },
)
class InternalViewsetTests(BaseInternalViewsetTests):
    """Test the internal viewset."""

    def valid_destructive_time():
        return timezone.now() + timedelta(hours=1)

    def invalid_destructive_time():
        return timezone.now() - timedelta(hours=1)

    def test_delete_tenant_disallowed(self):
        """Test that we cannot delete a tenant when disallowed."""
        response = self.client.delete(f"/_private/api/tenant/{self.tenant.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Destructive operations disallowed.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=invalid_destructive_time())
    def test_delete_tenant_disallowed_with_past_timestamp(self):
        """Test that we cannot delete a tenant when disallowed."""
        response = self.client.delete(f"/_private/api/tenant/{self.tenant.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Destructive operations disallowed.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch.object(Tenant, "delete")
    def test_delete_tenant_allowed_and_unmodified(self, mock):
        """Test that we can delete a tenant when allowed and unmodified."""
        response = self.client.delete(f"/_private/api/tenant/{self.tenant.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch.object(Tenant, "delete")
    def test_delete_tenant_no_schema(self, mock):
        """Test that we can delete a tenant with no schema."""
        public_tenant = Tenant.objects.get(tenant_name="public")
        Group.objects.create(name="Custom Group", tenant=public_tenant)

        tenant_no_schema = Tenant.objects.create(tenant_name="no_schema", org_id="1234")
        response = self.client.delete(f"/_private/api/tenant/{tenant_no_schema.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_delete_tenant_allowed_but_multiple_groups(self):
        """Test that we cannot delete a tenant when allowed but modified."""
        Group.objects.create(name="Custom Group", tenant=self.tenant)

        response = self.client.delete(f"/_private/api/tenant/{self.tenant.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Tenant cannot be deleted.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_delete_tenant_allowed_but_group_is_not_system(self):
        """Test that we cannot delete a tenant when allowed but modified."""
        self.group.system = False
        self.group.save()

        response = self.client.delete(f"/_private/api/tenant/{self.tenant.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Tenant cannot be deleted.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_delete_tenant_allowed_but_role_is_not_system(self):
        """Test that we cannot delete a tenant when allowed but modified."""
        self.role.system = False
        self.role.save()

        response = self.client.delete(f"/_private/api/tenant/{self.tenant.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Tenant cannot be deleted.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_delete_tenant_allowed_but_custom_one_role_is_not_system(self):
        """Test that we cannot delete a tenant when allowed but modified."""
        Role.objects.create(name="Custom Role", tenant=self.tenant)

        response = self.client.delete(f"/_private/api/tenant/{self.tenant.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Tenant cannot be deleted.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch.object(Tenant, "delete")
    def test_delete_tenant_allowed_and_unmodified_no_roles_or_groups(self, mock):
        """Test that we can delete a tenant when allowed and unmodified when there are no roles or groups."""
        Group.objects.all().delete()
        Role.objects.all().delete()
        Policy.objects.all().delete()

        response = self.client.delete(f"/_private/api/tenant/{self.tenant.org_id}/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_list_unmodified_tenants(self):
        """Test that only unmodified tenants are returned"""
        modified_tenant_groups = Tenant.objects.create(tenant_name="acctmodifiedgroups", org_id="1111")
        modified_tenant_roles = Tenant.objects.create(tenant_name="acctmodifiedroles", org_id="2222")
        unmodified_tenant_2 = Tenant.objects.create(tenant_name="acctunmodified2", org_id="3333")

        for t in [modified_tenant_groups, modified_tenant_roles, unmodified_tenant_2]:
            t.ready = True
            t.save()

        Group.objects.create(name="Custom Group", tenant=modified_tenant_groups)

        Role.objects.create(name="Custom Role", tenant=modified_tenant_roles)

        Group.objects.create(name="System Group", system=True, tenant=unmodified_tenant_2)
        Role.objects.create(name="System Role", system=True, tenant=unmodified_tenant_2)

        response = self.client.get(f"/_private/api/tenant/unmodified/", **self.request.META)
        response_data = json.loads(response.content)
        self.assertCountEqual(response_data["unmodified_tenants"], [self.tenant.org_id, unmodified_tenant_2.org_id])

        self.assertEqual(response_data["unmodified_tenants_count"], 2)
        self.assertEqual(response_data["total_tenants_count"], 4)

        # the public schema is created in migrations but excluded from the internal view
        self.assertEqual(Tenant.objects.count(), 5)

        response = self.client.get(f"/_private/api/tenant/unmodified/?limit=2", **self.request.META)
        response_data = json.loads(response.content)
        self.assertEqual(response_data["total_tenants_count"], 2)

        response = self.client.get(f"/_private/api/tenant/unmodified/?limit=2&offset=3", **self.request.META)
        response_data = json.loads(response.content)
        self.assertEqual(response_data["total_tenants_count"], 1)

    @patch("management.tasks.run_migrations_in_worker.delay")
    def test_run_migrations(self, migration_mock):
        """Test that we can trigger migrations."""
        response = self.client.post(f"/_private/api/migrations/run/", **self.request.META)
        migration_mock.assert_called_once()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.content.decode(), "Migrations are running in a background worker.")

    def test_list_migration_progress_without_migration_name(self):
        """Test that we get a 400 when no migration name supplied."""
        url = f"/_private/api/migrations/progress/"
        response = self.client.get(url, **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.content.decode(),
            "Please specify a migration name in the `?migration_name=` param.",
        )

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_defaults(self, seed_mock):
        """Test that we can trigger seeds with defaults."""
        response = self.client.post(f"/_private/api/seeds/run/", **self.request.META)
        seed_mock.assert_called_once_with({})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.content.decode(), "Seeds are running in a background worker.")

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_seed_types(self, seed_mock):
        """Test that we can trigger seeds with seed types."""
        response = self.client.post(f"/_private/api/seeds/run/?seed_types=roles,groups", **self.request.META)
        seed_mock.assert_called_once_with({"roles": True, "groups": True})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.content.decode(), "Seeds are running in a background worker.")

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_force_create(self, seed_mock):
        """Test that we can trigger seeds with force create flag."""
        response = self.client.post(
            f"/_private/api/seeds/run/?seed_types=roles&force_create_relationships=true", **self.request.META
        )
        seed_mock.assert_called_once_with({"roles": True, "force_create_relationships": True})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.content.decode(), "Seeds are running in a background worker.")

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_force_update(self, seed_mock):
        """Test that we can trigger seeds with force update flags."""
        response = self.client.post(
            f"/_private/api/seeds/run/?seed_types=roles&force_update_relationships=true", **self.request.META
        )
        seed_mock.assert_called_once_with({"roles": True, "force_update_relationships": True})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.content.decode(), "Seeds are running in a background worker.")

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_invalid_types(self, seed_mock):
        """Test that we get a 400 when invalid seed types supplied."""
        response = self.client.post(f"/_private/api/seeds/run/?seed_types=foo,bar", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        seed_mock.assert_not_called()
        self.assertEqual(
            response.content.decode(),
            "Valid options for \"seed_types\": ['permissions', 'roles', 'groups'].",
        )

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_invalid_force_flag(self, seed_mock):
        """Test that we get a 400 when invalid force flag supplied."""
        for flag in ["force_create_relationships", "force_update_relationships"]:
            with self.subTest(flag=flag):
                response = self.client.post(f"/_private/api/seeds/run/?{flag}=no", **self.request.META)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                seed_mock.assert_not_called()
                self.assertEqual(
                    response.content.decode(),
                    f"Valid options for \"{flag}\": ['true', 'false'].",
                )

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_both_force_flags(self, seed_mock):
        """Test that we get a 400 when both force flags are true."""
        response = self.client.post(
            f"/_private/api/seeds/run/?force_create_relationships=true&force_update_relationships=true",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        seed_mock.assert_not_called()
        self.assertEqual(
            response.content.decode(),
            "force_create_relationships and force_update_relationships cannot both be set to true.",
        )

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_invalid_options(self, seed_mock):
        """Test that we get a 400 when invalid option supplied."""
        response = self.client.post(f"/_private/api/seeds/run/?foo=true", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        seed_mock.assert_not_called()
        self.assertEqual(
            response.content.decode(),
            "Valid query parameters: ['seed_types', 'force_create_relationships', 'force_update_relationships', 'skip_notifications'].",
        )

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_skip_notifications(self, seed_mock):
        """Test that we can trigger seeds with skip_notifications=true."""
        response = self.client.post(
            f"/_private/api/seeds/run/?seed_types=roles&skip_notifications=true", **self.request.META
        )
        seed_mock.assert_called_once_with({"roles": True, "skip_notifications": True})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @patch("management.tasks.run_seeds_in_worker.delay")
    def test_run_seeds_with_invalid_skip_notifications(self, seed_mock):
        """Test that we get a 400 when an invalid skip_notifications value is supplied."""
        response = self.client.post(f"/_private/api/seeds/run/?skip_notifications=yes", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        seed_mock.assert_not_called()
        self.assertEqual(
            response.content.decode(),
            "Valid options for \"skip_notifications\": ['true', 'false'].",
        )

    @patch("api.tasks.populate_tenant_account_id_in_worker.delay")
    def test_populate_tenant_account_id(self, populate_mock):
        """Test that we can trigger population of account id's for tenants."""
        response = self.client.post("/_private/api/utils/populate_tenant_account_id/", **self.request.META)
        populate_mock.assert_called_once_with()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.content.decode(),
            "Tenant objects account_id values being updated in background worker.",
        )

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_setting_ready_flag_for_tenants(self):
        """Test that we can get the total of not ready tenants and set them to true."""
        Tenant.objects.create(tenant_name="acct_not_ready", org_id="1234")
        response = self.client.get(f"/_private/api/utils/set_tenant_ready/", **self.request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "Total of 1 tenants not set to be ready.")

        response = self.client.post(f"/_private/api/utils/set_tenant_ready/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(f"/_private/api/utils/set_tenant_ready/?max_expected=2", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.content.decode(), "Total of 1 tenants has been updated. 0 tenant with ready flag equal to false."
        )
        self.assertEqual(Tenant.objects.filter(ready=False).count(), 0)

    @patch("api.tasks.populate_tenant_account_id_in_worker.delay")
    def test_populate_tenant_account_id_get_failure(self, populate_mock):
        """Test that we get a bad request for not using POST method."""
        response = self.client.get("/_private/api/utils/populate_tenant_account_id/", **self.request.META)
        populate_mock.assert_not_called()
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(response.content.decode(), 'Invalid method, only "POST" is allowed.')

    @patch("management.principal.proxy.PrincipalProxy.fetch_account_org_mapping")
    def test_populate_tenant_org_id(self, mock_fetch_bop):
        """Test that we can populate tenant org_id by fetching from BOP."""
        # Create test tenants with account_id but no org_id
        tenant1 = Tenant.objects.create(tenant_name="acct111222", account_id="111222", org_id=None)
        tenant2 = Tenant.objects.create(tenant_name="acct333444", account_id="333444", org_id=None)
        tenant3 = Tenant.objects.create(tenant_name="acct555666", account_id="555666", org_id=None)
        tenant4 = Tenant.objects.create(tenant_name="acct777888", account_id="777888", org_id=None)

        # Create workspaces for tenant1 (will be UPDATED, not deleted)
        root_ws_t1 = Workspace.objects.create(name="Root", tenant=tenant1, type=Workspace.Types.ROOT, parent=None)
        default_ws_t1 = Workspace.objects.create(
            name="Default", tenant=tenant1, type=Workspace.Types.DEFAULT, parent=root_ws_t1
        )

        # Create workspaces for tenant2 (will be UPDATED, not deleted)
        root_ws_t2 = Workspace.objects.create(name="Root", tenant=tenant2, type=Workspace.Types.ROOT, parent=None)
        default_ws_t2 = Workspace.objects.create(
            name="Default", tenant=tenant2, type=Workspace.Types.DEFAULT, parent=root_ws_t2
        )

        # Create workspaces for tenant3 (will be deleted due to no BOP mapping)
        # Create a hierarchy: root -> default -> child1 -> grandchild
        root_ws_t3 = Workspace.objects.create(name="Root", tenant=tenant3, type=Workspace.Types.ROOT, parent=None)
        default_ws_t3 = Workspace.objects.create(
            name="Default", tenant=tenant3, type=Workspace.Types.DEFAULT, parent=root_ws_t3
        )
        child_ws_t3 = Workspace.objects.create(
            name="Child", tenant=tenant3, type=Workspace.Types.STANDARD, parent=default_ws_t3
        )
        grandchild_ws_t3 = Workspace.objects.create(
            name="Grandchild", tenant=tenant3, type=Workspace.Types.STANDARD, parent=child_ws_t3
        )

        # Create workspaces for tenant4 (will be deleted due to duplicate org_id)
        root_ws_t4 = Workspace.objects.create(name="Root", tenant=tenant4, type=Workspace.Types.ROOT, parent=None)
        default_ws_t4 = Workspace.objects.create(
            name="Default", tenant=tenant4, type=Workspace.Types.DEFAULT, parent=root_ws_t4
        )

        # Create an existing tenant with org_id that will conflict with tenant4's mapping
        existing_tenant = Tenant.objects.create(tenant_name="acct_existing", account_id="999000", org_id="org-777")

        # Mock BOP response
        mock_fetch_bop.return_value = {
            "111222": "org-111",
            "333444": "org-333",
            "777888": "org-777",  # This conflicts with existing_tenant's org_id
            # 555666 not in mapping to simulate partial results
        }

        response = self.client.post("/_private/api/utils/populate_tenant_org_id/", **self.request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_data = json.loads(response.content.decode())
        self.assertEqual(response_data["message"], "Tenant org_id population completed.")
        self.assertEqual(response_data["tenants_checked"], 4)
        self.assertEqual(response_data["mappings_from_bop"], 3)
        self.assertEqual(response_data["statistics"]["updated"], 2)  # tenant1, tenant2
        self.assertEqual(response_data["statistics"]["deleted_no_mapping"], 1)  # tenant3 deleted (no BOP mapping)
        self.assertEqual(response_data["statistics"]["deleted_duplicate"], 1)  # tenant4 deleted (org-777 exists)
        self.assertEqual(response_data["statistics"]["errors"], 0)

        # Verify BOP was called with correct account_ids
        called_account_ids = mock_fetch_bop.call_args[0][0]
        self.assertIn("111222", called_account_ids)
        self.assertIn("333444", called_account_ids)
        self.assertIn("555666", called_account_ids)
        self.assertIn("777888", called_account_ids)

        # Verify tenants were updated or deleted
        tenant1.refresh_from_db()
        tenant2.refresh_from_db()

        self.assertEqual(tenant1.org_id, "org-111")
        self.assertEqual(tenant2.org_id, "org-333")

        # Verify tenant1's workspaces are still there (tenant was updated, not deleted)
        self.assertTrue(Workspace.objects.filter(id=root_ws_t1.id).exists())
        self.assertTrue(Workspace.objects.filter(id=default_ws_t1.id).exists())

        # Verify tenant2's workspaces are still there (tenant was updated, not deleted)
        self.assertTrue(Workspace.objects.filter(id=root_ws_t2.id).exists())
        self.assertTrue(Workspace.objects.filter(id=default_ws_t2.id).exists())

        # Verify tenant3 was deleted (no mapping in BOP)
        self.assertFalse(Tenant.objects.filter(id=tenant3.id).exists())
        # Verify tenant3's workspaces were also deleted
        self.assertFalse(Workspace.objects.filter(tenant_id=tenant3.id).exists())

        # Verify tenant4 was deleted (duplicate org_id)
        self.assertFalse(Tenant.objects.filter(id=tenant4.id).exists())
        # Verify tenant4's workspaces were also deleted
        self.assertFalse(Workspace.objects.filter(tenant_id=tenant4.id).exists())

        # Verify existing_tenant is still there
        existing_tenant.refresh_from_db()
        self.assertEqual(existing_tenant.org_id, "org-777")

    @patch("management.principal.proxy.PrincipalProxy.fetch_account_org_mapping")
    def test_populate_tenant_org_id_no_bop_mapping(self, mock_fetch_bop):
        """Test that tenants are deleted when BOP returns no mappings."""
        # Create test tenants with account_id but no org_id
        tenant1 = Tenant.objects.create(tenant_name="acct222333", account_id="222333", org_id=None)
        tenant2 = Tenant.objects.create(tenant_name="acct444555", account_id="444555", org_id=None)

        # Create workspaces for tenant1
        root_ws_t1 = Workspace.objects.create(name="Root", tenant=tenant1, type=Workspace.Types.ROOT, parent=None)
        default_ws_t1 = Workspace.objects.create(
            name="Default", tenant=tenant1, type=Workspace.Types.DEFAULT, parent=root_ws_t1
        )

        # Create workspaces for tenant2
        root_ws_t2 = Workspace.objects.create(name="Root", tenant=tenant2, type=Workspace.Types.ROOT, parent=None)

        # Mock BOP response - returns empty dict (no mappings found)
        mock_fetch_bop.return_value = {}

        response = self.client.post("/_private/api/utils/populate_tenant_org_id/", **self.request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify both tenants were deleted
        self.assertFalse(Tenant.objects.filter(id=tenant1.id).exists())
        self.assertFalse(Tenant.objects.filter(id=tenant2.id).exists())

    def test_get_invalid_default_admin_groups(self):
        """Test that we can get invalid groups."""
        invalid_admin_default_group = Group.objects.create(
            admin_default=True,
            system=False,
            tenant=self.tenant,
            name="Invalid Default Admin Group",
        )
        valid_admin_default_group = Group.objects.create(
            admin_default=True,
            system=True,
            tenant=self.public_tenant,
            name="Valid Default Admin Group",
        )
        response = self.client.get(f"/_private/api/utils/invalid_default_admin_groups/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = json.loads(response.content)
        self.assertEqual(response_data["invalid_default_admin_groups_count"], 1)
        self.assertEqual(len(response_data["invalid_default_admin_groups"]), 1)
        self.assertEqual(
            response_data["invalid_default_admin_groups"][0],
            {
                "name": invalid_admin_default_group.name,
                "admin_default": invalid_admin_default_group.admin_default,
                "system": invalid_admin_default_group.system,
                "platform_default": invalid_admin_default_group.platform_default,
                "tenant": invalid_admin_default_group.tenant.id,
            },
        )

    def test_delete_invalid_default_admin_groups_disallowed(self):
        """Test that we cannot delete invalid groups when disallowed."""
        response = self.client.delete(f"/_private/api/utils/invalid_default_admin_groups/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Destructive operations disallowed.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_delete_invalid_default_admin_groups(self):
        """Test that we can delete invalid groups when allowed."""
        invalid_admin_default_group = Group.objects.create(
            admin_default=True,
            system=False,
            tenant=self.tenant,
            name="Invalid Default Admin Group",
        )
        valid_admin_default_group = Group.objects.create(
            admin_default=True,
            system=True,
            tenant=self.public_tenant,
            name="Valid Default Admin Group",
        )
        self.assertEqual(Group.objects.count(), 3)
        response = self.client.delete(f"/_private/api/utils/invalid_default_admin_groups/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Group.objects.count(), 2)
        self.assertEqual(Group.objects.filter(id=valid_admin_default_group.id).exists(), True)
        self.assertEqual(Group.objects.filter(id=invalid_admin_default_group.id).exists(), False)

    def test_get_org_admin_type_not_set(self):
        """Test that we get a bad request for not using type query param."""
        response = self.client.get(f"/_private/api/utils/get_org_admin/123456/", **self.request.META)
        option_key = "type"
        valid_values = ["account_id", "org_id"]
        expected_message = (
            f'Invalid request, must supply the "{option_key}" query parameter; Valid values: {valid_values}.'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), expected_message)

    def test_get_org_admin_bad_type(self):
        """Test that we get a bad request for not using type query param."""
        response = self.client.get(
            f"/_private/api/utils/get_org_admin/123456/?type=foobar",
            **self.request.META,
        )
        option_key = "type"
        valid_values = ["account_id", "org_id"]
        expected_message = f'Valid options for "{option_key}": {valid_values}.'
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), expected_message)

    def test_get_org_admin_post(self):
        """Test that we get a bad request for not using GET method."""
        response = self.client.post(f"/_private/api/utils/get_org_admin/123456/", **self.request.META)
        expected_message = 'Invalid method, only "GET" is allowed.'
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(response.content.decode(), expected_message)

    def test_get_org_admin_bad_connection(self):
        """Test getting the org admin and failing to connect to BOP."""
        response = self.client.get(
            "/_private/api/utils/get_org_admin/123456/?type=account_id",
            **self.request.META,
        )
        expected_message = "Unable to connect for URL"
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn(expected_message, response.content.decode())

    @patch("internal.views.requests")
    def test_get_org_admin_account(self, mock_proxy):
        """Test getting the org admin back with mock proxy using account id."""
        mockresponse = MagicMock()
        mockresponse.status_code = 200
        mockresponse.json.return_value = {
            "userCount": "1",
            "users": [{"username": "test_user"}],
        }
        users = [{"username": "test_user"}]
        mock_proxy.get.return_value = mockresponse
        response = self.client.get(
            "/_private/api/utils/get_org_admin/123456/?type=account_id",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        string_data = response.content.decode()
        dictionary_data = eval(string_data.replace("null", "None"))
        self.assertEqual(users, dictionary_data.get("data"))

    @patch("internal.views.requests")
    def test_get_org_admin_org_id(self, mock_proxy):
        """Test getting the org admin back with mock proxy using org id."""
        mockresponse = MagicMock()
        mockresponse.status_code = 200
        mockresponse.json.return_value = {
            "userCount": "1",
            "users": [{"username": "test_user"}],
        }
        users = [{"username": "test_user"}]
        mock_proxy.get.return_value = mockresponse
        response = self.client.get("/_private/api/utils/get_org_admin/123456/?type=org_id", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        string_data = response.content.decode()
        dictionary_data = eval(string_data.replace("null", "None"))
        self.assertEqual(users, dictionary_data.get("data"))

    @patch("internal.views.requests")
    def test_get_org_admin_bad_proxy_response(self, mock_proxy):
        """Test getting the org admin with bad proxy response."""
        mockresponse = MagicMock()
        mockresponse.status_code = 500
        mockresponse.json.return_value = {"error": "some error"}
        mock_proxy.get.return_value = mockresponse
        response = self.client.get("/_private/api/utils/get_org_admin/123456/?type=org_id", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        string_data = response.content.decode()
        dictionary_data = eval(string_data.replace("null", "None"))
        self.assertEqual(dictionary_data, mockresponse.json.return_value)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=invalid_destructive_time())
    def test_delete_selective_roles_disallowed(self):
        """Test that we cannot delete selective roles when disallowed."""
        response = self.client.delete(f"/_private/api/utils/role/?name={self.role.name}", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Destructive operations disallowed.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_delete_selective_roles(self):
        """Test that we can delete selective roles when allowed and no roles."""
        # No name specified
        response = self.client.delete(f"/_private/api/utils/role/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # No role found
        response = self.client.delete("/_private/api/utils/role/?name=non_exist_name", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Custom roles cannot be deleted
        response = self.client.delete(f"/_private/api/utils/role/?name={self.role.name}", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Delete successful
        self.role.tenant = self.public_tenant
        self.role.save()
        response = self.client.delete(f"/_private/api/utils/role/?name={self.role.name}", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_delete_role_clears_relations(self, replicate):
        """Test that deleting a system role clears its relations."""
        replicator = InMemoryRelationReplicator(self._tuples)
        replicate.side_effect = replicator.replicate
        permissions = ["app1:hosts:read", "inventory:hosts:write"]
        fixture = RbacFixture(V2TenantBootstrapService(replicator))
        fixture.bootstrap_tenant(self.tenant)
        role_system = fixture.new_system_role(name="test_system_role", permissions=permissions)
        dual_write_handler = SeedingRelationApiDualWriteHandler(role_system, replicator=replicator)
        dual_write_handler.replicate_new_system_role()

        # Before removing the role, verify relations exist
        self.assertEqual(
            1,
            self._tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role_system.uuid)),
                    relation("app1_hosts_read"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )
        self.assertEqual(
            1,
            self._tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role_system.uuid)),
                    relation("inventory_hosts_write"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )

        # Delete the role
        response = self.client.delete(f"/_private/api/utils/role/?name={role_system.name}", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # After removing the role, verify relations are cleared
        self.assertEqual(
            0,
            self._tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role_system.uuid)),
                    relation("app1_hosts_read"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )
        self.assertEqual(
            0,
            self._tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role_system.uuid)),
                    relation("inventory_hosts_write"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=invalid_destructive_time())
    def test_delete_selective_permission_disallowed(self):
        """Test that we cannot delete selective permission when disallowed."""
        response = self.client.delete("/_private/api/utils/permission/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Destructive operations disallowed.")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_delete_selective_permission(self):
        """Test that we can delete selective permission when allowed and no permissions."""
        # No permission param specified
        response = self.client.delete("/_private/api/utils/permission/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # No permission found
        response = self.client.delete(
            "/_private/api/utils/permission/?permission=rbac:roles:write",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        Permission.objects.create(permission="rbac:roles:write", tenant=self.tenant)
        response = self.client.delete(
            "/_private/api/utils/permission/?permission=rbac:roles:write",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_delete_permission_clears_relations(self, replicate):
        """Test that we can delete selective permission when allowed and no permissions."""
        replicator = InMemoryRelationReplicator(self._tuples)
        replicate.side_effect = replicator.replicate
        permissions = ["app1:hosts:read", "inventory:hosts:write"]
        fixture = RbacFixture(V2TenantBootstrapService(replicator))
        fixture.bootstrap_tenant(self.tenant)
        role_system = fixture.new_system_role(name="system_role", permissions=permissions)
        dual_write_handler = SeedingRelationApiDualWriteHandler(role_system, replicator=replicator)
        dual_write_handler.replicate_new_system_role()

        ws_2 = Workspace.objects.create(
            tenant=self.tenant, parent=Workspace.objects.default(tenant=self.tenant), name="ws_2"
        )

        role_custom = fixture.new_custom_role(
            name="custom_role",
            tenant=self.tenant,
            resource_access=fixture.workspace_access(
                permissions,
                **{str(ws_2.id): ["app1:hosts:read", "inventory:hosts:write"]},
            ),
        )
        dual_write = RelationApiDualWriteHandler(
            role_custom, ReplicationEventType.CREATE_CUSTOM_ROLE, replicator=replicator
        )
        dual_write.replicate_new_or_updated_role(role_custom)

        # Before removing the permission
        self.assertEqual(
            1,
            self._tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role_system.uuid)),
                    relation("app1_hosts_read"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )
        for binding in role_custom.binding_mappings.all():
            v2_role_id = binding.mappings["role"]["id"]
            self.assertEqual(
                1,
                self._tuples.count_tuples(
                    all_of(
                        resource("rbac", "role", v2_role_id),
                        relation("app1_hosts_read"),
                        subject("rbac", "principal", "*"),
                    )
                ),
            )
        response = self.client.delete(
            "/_private/api/utils/permission/?permission=app1:hosts:read",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(
            0,
            self._tuples.count_tuples(
                all_of(
                    resource("rbac", "role", str(role_system.uuid)),
                    relation("app1_hosts_read"),
                    subject("rbac", "principal", "*"),
                )
            ),
        )
        for binding in role_custom.binding_mappings.all():
            v2_role_id = binding.mappings["role"]["id"]
            self.assertEqual(
                0,
                self._tuples.count_tuples(
                    all_of(
                        resource("rbac", "role", v2_role_id),
                        relation("app1_hosts_read"),
                        subject("rbac", "principal", "*"),
                    )
                ),
            )

    @override_settings(READ_ONLY_API_MODE=True)
    @patch("management.tasks.migrate_data_in_worker.delay")
    def test_run_migrations_of_data(self, migration_mock):
        """Test that we can trigger migrations of data to migrate from V1 to V2."""
        response = self.client.post(
            f"/_private/api/utils/data_migration/?exclude_apps=rbac,costmanagement&orgs=acct00001,acct00002",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        migration_mock.assert_called_once_with(
            {
                "exclude_apps": ["rbac", "costmanagement"],
                "orgs": ["acct00001", "acct00002"],
                "write_relationships": "False",
                "skip_roles": False,
            }
        )
        self.assertEqual(
            response.content.decode(),
            "Data migration from V1 to V2 are running in a background worker.",
        )

        # Without params uses global default
        with self.settings(V2_MIGRATION_APP_EXCLUDE_LIST=["fooapp"]):
            migration_mock.reset_mock()
            response = self.client.post(
                f"/_private/api/utils/data_migration/",
                **self.request.META,
            )
            migration_mock.assert_called_once_with(
                {"exclude_apps": ["fooapp"], "orgs": [], "write_relationships": "False", "skip_roles": False}
            )
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            self.assertEqual(
                response.content.decode(),
                "Data migration from V1 to V2 are running in a background worker.",
            )

        # Without params uses none if no global default
        with self.settings(V2_MIGRATION_APP_EXCLUDE_LIST=[]):
            migration_mock.reset_mock()
            response = self.client.post(
                f"/_private/api/utils/data_migration/",
                **self.request.META,
            )
            migration_mock.assert_called_once_with(
                {"exclude_apps": [], "orgs": [], "write_relationships": "False", "skip_roles": False}
            )
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            self.assertEqual(
                response.content.decode(),
                "Data migration from V1 to V2 are running in a background worker.",
            )

    @patch("management.tasks.migrate_data_in_worker.delay")
    def test_run_migrations_of_data_outbox_replication(self, migration_mock):
        """Test that we can trigger migrations of data to migrate from V1 to V2."""
        response = self.client.post(
            f"/_private/api/utils/data_migration/?exclude_apps=rbac,costmanagement&orgs=acct00001,acct00002"
            "&write_relationships=outbox",
            **self.request.META,
        )
        migration_mock.assert_called_once_with(
            {
                "exclude_apps": ["rbac", "costmanagement"],
                "orgs": ["acct00001", "acct00002"],
                "write_relationships": "outbox",
                "skip_roles": False,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            response.content.decode(),
            "Data migration from V1 to V2 are running in a background worker.",
        )

    def test_list_bindings_by_role(self):
        """Test that we can list bindingmapping by role."""
        response = self.client.get(
            f"/_private/api/utils/bindings/{self.role.uuid}/",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "[]")

        binding_attrs = {
            "resource_id": "123456",
            "resource_type_namespace": "rbac",
            "resource_type_name": "workspace",
            "mappings": {"foo": "bar"},
        }
        # Create a binding mapping
        binding_mapping = BindingMapping.objects.create(
            role=self.role,
            **binding_attrs,
        )
        response = self.client.get(
            f"/_private/api/utils/bindings/{self.role.uuid}/",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        binding_attrs.update({"id": binding_mapping.id, "role": self.role.id, "v2_role": None})
        self.assertEqual(json.loads(response.content.decode()), [binding_attrs])

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_delete_bindings_by_role(self, replicate):
        """Test that we can delete bindingmapping by role."""
        replicator = InMemoryRelationReplicator(self._tuples)
        replicate.side_effect = replicator.replicate
        binding_mapping_id = "abcdefg"
        workspace_id = "123456"
        relations = [
            create_relationship(
                ("rbac", "role_binding"),
                binding_mapping_id,
                ("rbac", "role"),
                str(self.role.uuid),
                "role",
            ),
            create_relationship(
                ("rbac", "workspace"),
                workspace_id,
                ("rbac", "role_binding"),
                binding_mapping_id,
                "binding",
            ),
        ]
        self._tuples.write(relations, [])
        bindings_attrs = [
            {
                "resource_id": workspace_id,
                "resource_type_namespace": "rbac",
                "resource_type_name": "workspace",
                "mappings": {
                    "id": binding_mapping_id,
                    "role": {"is_system": True, "id": str(self.role.uuid), "permissions": []},
                },
            },
            {
                "resource_id": workspace_id,
                "resource_type_namespace": "rbac",
                "resource_type_name": "workspace",
                "mappings": {"foo": "bar"},
            },
        ]
        self._tuples.find_tuples()
        # Create binding mappings
        binding_mappings = BindingMapping.objects.bulk_create(
            [BindingMapping(role=self.role, **binding_attrs) for binding_attrs in bindings_attrs]
        )
        response = self.client.delete(
            f"/_private/api/utils/bindings/{self.role.uuid}/?mappings__role__is_system=True",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        with self.assertRaises(BindingMapping.DoesNotExist):
            binding_mappings[0].refresh_from_db()
        binding_mappings[1].refresh_from_db()
        self.assertEqual(self._tuples.count_tuples(), 0)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_clean_bindings(self, replicate):
        """Test that we can clean bindingmapping."""
        replicator = InMemoryRelationReplicator(self._tuples)
        replicate.side_effect = replicator.replicate
        workspace_id = "123456"
        group_to_remove = Group.objects.create(name="test", tenant=self.tenant)
        group_id_to_remove = str(group_to_remove.uuid)
        # Create binding mappings
        binding_attrs = {
            "resource_id": workspace_id,
            "resource_type_namespace": "rbac",
            "resource_type_name": "workspace",
            "mappings": {
                "role": {"is_system": True, "id": str(self.role.uuid), "permissions": []},
                "groups": [group_id_to_remove, str(self.group.uuid)],
                "users": {},
            },
        }
        binding_mapping = BindingMapping.objects.create(
            role=self.role,
            **binding_attrs,
        )
        binding_mapping_id = str(binding_mapping.id)
        binding_mapping.mappings["id"] = binding_mapping_id
        binding_mapping.save()
        relations = [
            create_relationship(
                ("rbac", "role_binding"),
                binding_mapping_id,
                ("rbac", "role"),
                str(self.role.uuid),
                "role",
            ),
            create_relationship(
                ("rbac", "workspace"),
                workspace_id,
                ("rbac", "role_binding"),
                binding_mapping_id,
                "binding",
            ),
            create_relationship(
                ("rbac", "role_binding"),
                binding_mapping_id,
                ("rbac", "group"),
                group_id_to_remove,
                "subject",
                "member",
            ),
        ]

        self._tuples.write(relations, [])

        # All group still exist
        response = self.client.post(
            f"/_private/api/utils/binding/{binding_mapping_id}/clean/?field=groups",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        group_to_remove.delete()
        response = self.client.post(
            f"/_private/api/utils/binding/{binding_mapping_id}/clean/?field=groups",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            0,
            self._tuples.count_tuples(
                all_of(
                    resource("rbac", "role_binding", binding_mapping_id),
                    relation("subject"),
                    subject("rbac", "group", f"redhat/{group_id_to_remove}", "member"),
                )
            ),
        )
        binding_mapping.refresh_from_db()
        self.assertEqual(binding_mapping.mappings["users"], {})
        self.assertEqual(binding_mapping.mappings["groups"], [str(self.group.uuid)])

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_bootstrapping_tenant(self, replicate):
        """Test that we can bootstrap a tenant."""
        org_id = "12345"

        payload = {"org_ids": [org_id]}
        response = self.client.post(
            f"/_private/api/utils/bootstrap_tenant/",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate
        RbacFixture(V2TenantBootstrapService(replicator))
        tuples.clear()

        tenant = Tenant.objects.create(org_id=org_id)
        with self.assertRaises(Workspace.DoesNotExist) as root_assertion:
            Workspace.objects.root(tenant=tenant)
        self.assertEqual("Workspace matching query does not exist.", str(root_assertion.exception))

        with self.assertRaises(Workspace.DoesNotExist) as default_assertion:
            Workspace.objects.default(tenant=tenant)
        self.assertEqual("Workspace matching query does not exist.", str(default_assertion.exception))

        response = self.client.post(
            f"/_private/api/utils/bootstrap_tenant/",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(Workspace.objects.root(tenant=tenant))
        self.assertIsNotNone(Workspace.objects.default(tenant=tenant))
        self.assertTrue(getattr(tenant, "tenant_mapping"))
        self.assertEqual(len(tuples), 21)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_bootstrapping_multiple_tenants(self, replicate):
        """Test that we can bootstrap a tenant."""
        org_ids = ["12345", "123456", "6789"]

        payload = {"org_ids": org_ids}

        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate
        RbacFixture(V2TenantBootstrapService(replicator))
        tuples.clear()

        for org_id in org_ids:
            tenant = Tenant.objects.create(org_id=org_id)
            with self.assertRaises(Workspace.DoesNotExist) as root_assertion:
                Workspace.objects.root(tenant=tenant)
            self.assertEqual("Workspace matching query does not exist.", str(root_assertion.exception))

            with self.assertRaises(Workspace.DoesNotExist) as default_assertion:
                Workspace.objects.default(tenant=tenant)
            self.assertEqual("Workspace matching query does not exist.", str(default_assertion.exception))

        response = self.client.post(
            f"/_private/api/utils/bootstrap_tenant/",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for org_id in org_ids:
            tenant = Tenant.objects.get(org_id=org_id)
            self.assertIsNotNone(Workspace.objects.root(tenant=tenant))
            self.assertIsNotNone(Workspace.objects.default(tenant=tenant))
            self.assertTrue(getattr(tenant, "tenant_mapping"))
        self.assertEqual(
            len(tuples),
            21 + 21 + 21,
        )  # orgs: 3 for workspaces, (3 for default and 3 for admin default access) for each of 3 scopes

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_bootstrapping_existing_tenant_without_force_does_nothing(self, replicate):
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate
        fixture = RbacFixture(V2TenantBootstrapService(replicator))

        org_id = "12345"
        payload = {"org_ids": [org_id]}
        fixture.new_tenant(org_id)
        tuples.clear()

        response = self.client.post(
            f"/_private/api/utils/bootstrap_tenant/?org_id={org_id}",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(tuples), 0)

        response = self.client.post(
            f"/_private/api/utils/bootstrap_tenant/?org_id={org_id}&force=false",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(tuples), 0)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @override_settings(REPLICATION_TO_RELATION_ENABLED=False)
    def test_force_bootstrapping_tenant(self, replicate):
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate
        fixture = RbacFixture(V2TenantBootstrapService(replicator))

        org_id = "12345"
        payload = {"org_ids": [org_id]}
        fixture.new_tenant(org_id)
        tuples.clear()

        response = self.client.post(
            f"/_private/api/utils/bootstrap_tenant/?org_id={org_id}&force=true",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(tuples), 21)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_cannot_force_bootstrapping_while_replication_enabled(self):
        org_id = "12345"
        payload = {"org_ids": [org_id]}
        response = self.client.post(
            f"/_private/api/utils/bootstrap_tenant/?org_id={org_id}&force=true",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    def test_force_admin_only_bootstrap(self, replicate):
        """Test force_admin_only=true for bootstrap_tenant endpoint.

        Admin default bindings are NOT customizable, so force_admin_only is safe
        even when replication is enabled (unlike force=true).
        """
        from management.group.definer import seed_group

        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate

        # Seed the platform/admin default groups so GlobalPolicyIdService can find them
        seed_group()

        # Test 1: Non-existent tenant returns error
        payload = {"org_ids": ["nonexistent_org"]}
        response = self.client.post(
            "/_private/api/utils/bootstrap_tenant/?force_admin_only=true",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.json()["results"][0]
        self.assertEqual(result["org_id"], "nonexistent_org")
        self.assertIn("error", result)

        # Test 2: Unbootstrapped tenant (no TenantMapping) returns error
        new_tenant = Tenant.objects.create(org_id="unbootstrapped_org")
        payload = {"org_ids": [new_tenant.org_id]}
        response = self.client.post(
            "/_private/api/utils/bootstrap_tenant/?force_admin_only=true",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.json()["results"][0]
        self.assertIn("error", result)
        self.assertIn("TenantMapping not found", result["error"])
        new_tenant.delete()

        # Test 3: Bootstrapped tenant - should succeed and replicate admin bindings
        bootstrap_service = V2TenantBootstrapService(replicator)
        bootstrapped = bootstrap_service.bootstrap_tenant(self.tenant)
        tenant_mapping = self.tenant.tenant_mapping
        tuples.clear()

        payload = {"org_ids": [self.tenant.org_id]}
        response = self.client.post(
            "/_private/api/utils/bootstrap_tenant/?force_admin_only=true",
            data=payload,
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.json()["results"][0]
        self.assertEqual(result["org_id"], self.tenant.org_id)
        self.assertIn("admin_bindings_replicated", result)
        self.assertGreater(result["admin_bindings_replicated"], 0)
        self.assertGreater(len(tuples), 0)

        # Verify tuples include admin group bindings (subject relation with admin group UUID)
        admin_group_uuid = str(tenant_mapping.default_admin_group_uuid)
        found_admin_binding = False
        for t in tuples:
            # RelationTuple has: relation, subject.subject.type.name, subject.subject.id
            if (
                t.relation == "subject"
                and t.subject.subject.type.name == "group"
                and admin_group_uuid in t.subject.subject.id
            ):
                found_admin_binding = True
                break
        self.assertTrue(found_admin_binding, f"No admin binding found for group {admin_group_uuid}")

    def test_listing_migration_resources(self):
        """Test that we can list migration resources."""
        org_id = "12345678"
        bootstrap_service = V2TenantBootstrapService(NoopReplicator())
        bootstrapped_tenant = bootstrap_service.bootstrap_tenant(self.tenant)
        another_bootstrapped_tenant = bootstrap_service.new_bootstrapped_tenant(org_id)
        another_tenant = another_bootstrapped_tenant.tenant
        root_workspaces = [bootstrapped_tenant.root_workspace, another_bootstrapped_tenant.root_workspace]
        default_workspaces = [
            bootstrapped_tenant.default_workspace,
            another_bootstrapped_tenant.default_workspace,
        ]

        # Test limit and offset
        response = self.client.get(
            "/_private/api/utils/migration_resources/?resource=workspace&limit=1",
            **self.request.META,
        )
        workspace_list_1 = json.loads(response.getvalue())
        self.assertEqual(len(workspace_list_1), 1)
        response = self.client.get(
            "/_private/api/utils/migration_resources/?resource=workspace&limit=1&offset=1",
            **self.request.META,
        )
        workspace_list_2 = json.loads(response.getvalue())
        self.assertEqual(len(workspace_list_2), 1)
        self.assertNotEqual(workspace_list_1, workspace_list_2)

        response = self.client.get(
            "/_private/api/utils/migration_resources/?resource=workspace",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        id_set = {str(workspace.id) for workspace in default_workspaces}
        for workspace in root_workspaces:
            id_set.add(str(workspace.id))
        self.assertEqual(set(json.loads(response.getvalue())), id_set)

        response = self.client.get(
            f"/_private/api/utils/migration_resources/?resource=workspace&org_id={org_id}",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(json.loads(response.getvalue())), {str(default_workspaces[1].id), str(root_workspaces[1].id)}
        )

        # Listing tenantmappings
        tenant_mappings = [bootstrapped_tenant.mapping, another_bootstrapped_tenant.mapping]
        response = self.client.get(
            "/_private/api/utils/migration_resources/?resource=mapping",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(json.loads(response.getvalue())), {str(tenant_mapping.id) for tenant_mapping in tenant_mappings}
        )
        response = self.client.get(
            f"/_private/api/utils/migration_resources/?resource=mapping&org_id={org_id}",
            **self.request.META,
        )
        self.assertEqual(json.loads(response.getvalue()), [str(tenant_mappings[1].id)])

        # List bindingmappings
        tenant_role = Role.objects.create(
            name="role",
            tenant=self.tenant,
        )
        another_role = Role.objects.create(
            name="role",
            tenant=another_tenant,
        )
        binding = BindingMapping.objects.create(
            role=tenant_role,
        )
        another_binding = BindingMapping.objects.create(
            role=another_role,
        )
        response = self.client.get(
            "/_private/api/utils/migration_resources/?resource=binding",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(json.loads(response.getvalue())), {str(binding.id), str(another_binding.id)})

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_migration_resource_deletion.delay")
    def test_deleting_migration_resources(self, delay_mock):
        """Test that we can delete migration resources."""
        org_id = "12345678"
        response = self.client.delete(
            f"/_private/api/utils/migration_resources/?resource=workspace&org_id={org_id}",
            **self.request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        delay_mock.assert_called_once_with(
            {
                "resource": "workspace",
                "org_id": org_id,
            }
        )

    def test_reset_imported_tenants_get_does_not_include_sample_tenant_with_tenanted_resources(self):
        response = self.client.get("/_private/api/utils/reset_imported_tenants/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "0 tenants would be deleted")

    def test_reset_imported_tenants_get_does_not_include_tenant_with_principals(self):
        self.fixture = RbacFixture(V1TenantBootstrapService())
        o1 = self.fixture.new_tenant("o1")
        self.fixture.new_principals_in_tenant(["u1"], o1.tenant)
        response = self.client.get("/_private/api/utils/reset_imported_tenants/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "0 tenants would be deleted")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_does_not_delete_public_tenant(self, delay):
        delay.side_effect = lambda args: reset_imported_tenants(**args)
        self.assertTrue(Tenant.objects.filter(tenant_name="public").exists())

        with self.assertLogs("api.utils", level="INFO") as logs:
            response = self.client.delete("/_private/api/utils/reset_imported_tenants/", **self.request.META)

        self.assertIn("Deleted 0 tenants.", logs.output[0])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Tenant.objects.filter(tenant_name="public").exists())

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_removes_tenants_without_tenanted_objects(self, delay):
        delay.side_effect = lambda args: reset_imported_tenants(**args)
        self.fixture = RbacFixture(V1TenantBootstrapService())
        o1 = self.fixture.new_tenant("o1")
        o2 = self.fixture.new_tenant("o2")
        self.fixture.new_tenant("o3")
        self.fixture.new_tenant("o4")
        self.fixture.new_principals_in_tenant(["u1"], o1.tenant)
        self.fixture.new_principals_in_tenant(["u2"], o2.tenant)

        with self.assertLogs("api.utils", level="INFO") as logs:
            response = self.client.delete(
                "/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false", **self.request.META
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Deleted 2 tenants.", logs.output[0])
        self.assertTrue(Tenant.objects.filter(org_id="o1").exists())
        self.assertTrue(Tenant.objects.filter(org_id="o2").exists())
        self.assertFalse(Tenant.objects.filter(org_id="o3").exists())
        self.assertFalse(Tenant.objects.filter(org_id="o4").exists())

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_with_ready_false_flag(self, delay):
        """Test that tenants flagged as ready=false are properly removed."""
        delay.side_effect = lambda args: reset_imported_tenants(**args)
        self.fixture = RbacFixture(V1TenantBootstrapService())
        self.fixture.new_unbootstrapped_tenant("o1")  # Tenant with ready=true
        self.fixture.new_unbootstrapped_tenant("o2")  # Tenant with ready=true

        # Test the query without and with the query param 'only_ready_false_flag=true' (default value)
        for query_param in ["", "?only_ready_false_flag=true"]:
            self.fixture.new_not_ready_tenant("o3")  # Tenant with ready=false
            self.fixture.new_not_ready_tenant("o4")  # Tenant with ready=false

            with self.assertLogs("api.utils", level="INFO") as logs:
                response = self.client.delete(
                    f"/_private/api/utils/reset_imported_tenants/{query_param}", **self.request.META
                )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("Deleted 2 tenants.", logs.output[0])
            self.assertTrue(Tenant.objects.filter(org_id="o1").exists())
            self.assertTrue(Tenant.objects.filter(org_id="o2").exists())
            self.assertFalse(Tenant.objects.filter(org_id="o3").exists())
            self.assertFalse(Tenant.objects.filter(org_id="o4").exists())

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_removes_up_to_limit(self, delay):
        delay.side_effect = lambda args: reset_imported_tenants(**args)

        self.fixture = RbacFixture(V1TenantBootstrapService())
        for i in range(10):
            self.fixture.new_tenant(f"o{i}")

        with self.assertLogs("api.utils", level="INFO") as logs:
            response = self.client.delete(
                "/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false&limit=3", **self.request.META
            )

        self.assertIn("Deleted 3 tenants.", logs.output[0])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # two extra tenants for test tenant and public tenant
        self.assertEqual(Tenant.objects.count(), 9)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_removes_up_to_limit_with_ready_false_flag(self, delay):
        """Test that tenants flagged as ready=false are properly removed up to the specified limit."""
        delay.side_effect = lambda args: reset_imported_tenants(**args)

        self.fixture = RbacFixture(V1TenantBootstrapService())
        for i in range(10):
            self.fixture.new_not_ready_tenant(f"o{i}")

        with self.assertLogs("api.utils", level="INFO") as logs:
            response = self.client.delete("/_private/api/utils/reset_imported_tenants/?limit=3", **self.request.META)

        self.assertIn("Deleted 3 tenants.", logs.output[0])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # two extra tenants for test tenant and public tenant
        self.assertEqual(Tenant.objects.count(), 9)

    def test_reset_imported_tenants_get_counts_all_tenants_to_be_deleted(self):
        self.fixture = RbacFixture(V1TenantBootstrapService())
        o1 = self.fixture.new_tenant("o1")
        o2 = self.fixture.new_tenant("o2")
        self.fixture.new_principals_in_tenant(["u1"], o1.tenant)
        self.fixture.new_principals_in_tenant(["u2"], o2.tenant)

        for i in range(100):
            self.fixture.new_tenant(f"o{i + 3}")

        response = self.client.get(
            "/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false", **self.request.META
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "100 tenants would be deleted")

    def test_reset_imported_tenants_get_counts_all_tenants_to_be_deleted_with_ready_false_flag(self):
        self.fixture = RbacFixture(V1TenantBootstrapService())
        self.fixture.new_tenant("o1")
        self.fixture.new_tenant("o2")

        for i in range(100):
            self.fixture.new_not_ready_tenant(f"o{i + 3}")

        response = self.client.get("/_private/api/utils/reset_imported_tenants/", **self.request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "100 tenants would be deleted")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_no_tenanted_objects_allow_tenant_to_be_deleted(self, delay):
        delay.side_effect = lambda args: reset_imported_tenants(**args)
        self.fixture = RbacFixture(V1TenantBootstrapService())

        for object in [
            (Principal, {"username": "u1"}),
            (TenantMapping, {}),
            (Access, {}),
            (Group, {}),
            (Permission, {"permission": "test:app:foo"}),
            (Policy, {}),
            (ResourceDefinition, {}),
            (Role, {}),
            (AuditLog, {}),
            (Workspace, {"name": "Root", "type": Workspace.Types.ROOT}),
        ]:
            model, kwargs = object
            # Create a new tenant
            t = self.fixture.new_tenant(f"o_{model.__name__}")
            # Create an object that references the tenant
            model.objects.create(tenant=t.tenant, **kwargs)

        # Now create some tenants that don't have any of these
        self.fixture.new_tenant("o_no_objects1")
        self.fixture.new_tenant("o_no_objects2")
        self.fixture.new_tenant("o_no_objects3")
        self.fixture.new_tenant("o_no_objects4")

        with self.assertLogs("api.utils", level="INFO") as logs:
            response = self.client.delete(
                "/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false", **self.request.META
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Deleted 4 tenants.", logs.output[0])
        # two extra tenants for test tenant and public tenant
        self.assertEqual(Tenant.objects.count(), 12)
        self.assertFalse(Tenant.objects.filter(org_id="o_no_objects1").exists())
        self.assertFalse(Tenant.objects.filter(org_id="o_no_objects2").exists())
        self.assertFalse(Tenant.objects.filter(org_id="o_no_objects3").exists())
        self.assertFalse(Tenant.objects.filter(org_id="o_no_objects4").exists())

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_no_tenanted_objects_allow_tenant_to_be_deleted_with_limit(self, delay):
        delay.side_effect = lambda args: reset_imported_tenants(**args)
        self.fixture = RbacFixture(V1TenantBootstrapService())

        for object in [
            (Principal, {"username": "u1"}),
            (TenantMapping, {}),
            (Access, {}),
            (Group, {}),
            (Permission, {"permission": "test:app:foo"}),
            (Policy, {}),
            (ResourceDefinition, {}),
            (Role, {}),
            (AuditLog, {}),
            (Workspace, {"name": "Root", "type": Workspace.Types.ROOT}),
        ]:
            model, kwargs = object
            # Create a new tenant
            t = self.fixture.new_tenant(f"o_{model.__name__}")
            # Create an object that references the tenant
            model.objects.create(tenant=t.tenant, **kwargs)

        # Now create some tenants that don't have any of these
        self.fixture.new_tenant("o_no_objects1")
        self.fixture.new_tenant("o_no_objects2")
        self.fixture.new_tenant("o_no_objects3")
        self.fixture.new_tenant("o_no_objects4")

        with self.assertLogs("api.utils", level="INFO") as logs:
            response = self.client.delete(
                "/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false&limit=1", **self.request.META
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Deleted 1 tenants.", logs.output[0])
        # two extra tenants for test tenant and public tenant
        self.assertEqual(Tenant.objects.count(), 15)
        self.assertFalse(Tenant.objects.filter(org_id="o_no_objects1").exists())
        self.assertTrue(Tenant.objects.filter(org_id="o_no_objects2").exists())
        self.assertTrue(Tenant.objects.filter(org_id="o_no_objects3").exists())
        self.assertTrue(Tenant.objects.filter(org_id="o_no_objects4").exists())

    def test_reset_imported_tenants_get_also_excludes_tenants_with_objects(self):
        self.fixture = RbacFixture(V1TenantBootstrapService())

        for object in [
            (Principal, {"username": "u1"}),
            (TenantMapping, {}),
            (Access, {}),
            (Group, {}),
            (Permission, {"permission": "test:app:foo"}),
            (Policy, {}),
            (ResourceDefinition, {}),
            (Role, {}),
            (AuditLog, {}),
            (Workspace, {"name": "Root", "type": Workspace.Types.ROOT}),
        ]:
            model, kwargs = object
            # Create a new tenant
            t = self.fixture.new_tenant(f"o_{model.__name__}")
            # Create an object that references the tenant
            model.objects.create(tenant=t.tenant, **kwargs)

        # Now create some tenants that don't have any of these
        self.fixture.new_tenant("o_no_objects1")
        self.fixture.new_tenant("o_no_objects2")
        self.fixture.new_tenant("o_no_objects3")
        self.fixture.new_tenant("o_no_objects4")

        response = self.client.get(
            "/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false", **self.request.META
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "4 tenants would be deleted")

    def test_reset_imported_tenants_get_also_excludes_tenants_with_objects_up_to_limit(self):
        self.fixture = RbacFixture(V1TenantBootstrapService())

        for object in [
            (Principal, {"username": "u1"}),
            (TenantMapping, {}),
            (Access, {}),
            (Group, {}),
            (Permission, {"permission": "test:app:foo"}),
            (Policy, {}),
            (ResourceDefinition, {}),
            (Role, {}),
            (AuditLog, {}),
            (Workspace, {"name": "Root", "type": Workspace.Types.ROOT}),
        ]:
            model, kwargs = object
            # Create a new tenant
            t = self.fixture.new_tenant(f"o_{model.__name__}")
            # Create an object that references the tenant
            model.objects.create(tenant=t.tenant, **kwargs)

        # Now create some tenants that don't have any of these
        self.fixture.new_tenant("o_no_objects1")
        self.fixture.new_tenant("o_no_objects2")
        self.fixture.new_tenant("o_no_objects3")
        self.fixture.new_tenant("o_no_objects4")

        response = self.client.get(
            "/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false&limit=1", **self.request.META
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "1 tenants would be deleted")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_reset_imported_tenants_does_nothing_if_zero_limit(self):
        self.fixture = RbacFixture(V1TenantBootstrapService())

        self.fixture.new_tenant("o_no_objects1")
        self.fixture.new_tenant("o_no_objects2")
        self.fixture.new_tenant("o_no_objects3")
        self.fixture.new_tenant("o_no_objects4")

        self.assertEqual(6, Tenant.objects.count())

        response = self.client.delete("/_private/api/utils/reset_imported_tenants/?limit=0", **self.request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(6, Tenant.objects.count())

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_reset_imported_tenants_rejects_invalid_limit(self):
        response = self.client.delete("/_private/api/utils/reset_imported_tenants/?limit=foo", **self.request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_imported_tenants_excludes_get(self):
        # Create some tenants that would be deleted but exclude some
        self.fixture = RbacFixture(V1TenantBootstrapService())

        t1 = self.fixture.new_tenant("o_no_objects1").tenant
        self.fixture.new_tenant("o_no_objects2")
        t3 = self.fixture.new_tenant("o_no_objects3").tenant
        self.fixture.new_tenant("o_no_objects4")

        self.assertEqual(6, Tenant.objects.count())

        response = self.client.get(
            f"/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false&exclude_id={t1.id}&exclude_id={t3.id}",
            **self.request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "2 tenants would be deleted")

    def test_reset_imported_tenants_excludes_get_with_ready_false_flag(self):
        # Create some tenants that would be deleted but exclude some
        self.fixture = RbacFixture(V1TenantBootstrapService())
        t1 = self.fixture.new_not_ready_tenant("o1")
        t2 = self.fixture.new_not_ready_tenant("o2")
        self.fixture.new_not_ready_tenant("o3")
        self.fixture.new_not_ready_tenant("o4")

        self.assertEqual(6, Tenant.objects.count())

        response = self.client.get(
            f"/_private/api/utils/reset_imported_tenants/?exclude_id={t1.id}&exclude_id={t2.id}",
            **self.request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content.decode(), "2 tenants would be deleted")

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_excludes_delete(self, delay):
        delay.side_effect = lambda args: reset_imported_tenants(**args)

        # Create some tenants that would be deleted but exclude some
        self.fixture = RbacFixture(V1TenantBootstrapService())

        t1 = self.fixture.new_tenant("o_no_objects1").tenant
        self.fixture.new_tenant("o_no_objects2")
        t3 = self.fixture.new_tenant("o_no_objects3").tenant
        self.fixture.new_tenant("o_no_objects4")

        self.assertEqual(6, Tenant.objects.count())

        with self.assertLogs("api.utils", level="INFO") as logs:
            response = self.client.delete(
                f"/_private/api/utils/reset_imported_tenants/?only_ready_false_flag=false&exclude_id={t1.id}&exclude_id={t3.id}",
                **self.request.META,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Deleted 2 tenants.", logs.output[0])
        self.assertEqual(4, Tenant.objects.count())

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    @patch("api.tasks.run_reset_imported_tenants.delay")
    def test_reset_imported_tenants_excludes_delete_with_ready_false_flag(self, delay):
        delay.side_effect = lambda args: reset_imported_tenants(**args)

        # Create some tenants that would be deleted but exclude some
        self.fixture = RbacFixture(V1TenantBootstrapService())
        t1 = self.fixture.new_not_ready_tenant("o1")
        t2 = self.fixture.new_not_ready_tenant("o2")
        self.fixture.new_not_ready_tenant("o3")
        self.fixture.new_not_ready_tenant("o4")

        self.assertEqual(6, Tenant.objects.count())

        with self.assertLogs("api.utils", level="INFO") as logs:
            response = self.client.delete(
                f"/_private/api/utils/reset_imported_tenants/?exclude_id={t1.id}&exclude_id={t2.id}",
                **self.request.META,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Deleted 2 tenants.", logs.output[0])
        self.assertEqual(4, Tenant.objects.count())

        self.assertTrue(Tenant.objects.filter(org_id="o1").exists())
        self.assertTrue(Tenant.objects.filter(org_id="o2").exists())
        self.assertFalse(Tenant.objects.filter(org_id="o3").exists())
        self.assertFalse(Tenant.objects.filter(org_id="o4").exists())

    def test_update_system_flag_in_role(self):
        """Test that we can update a role."""
        tenant = Tenant.objects.create(tenant_name="1234", org_id="1234")
        custom_role = Role.objects.create(
            name="role 1", system=True, tenant=tenant, platform_default=False, admin_default=False
        )

        request_body = {
            "system": "false",
        }

        json_request_body = json.dumps(request_body)

        self.assertTrue(custom_role.system)
        self.assertFalse(custom_role.platform_default)
        self.assertFalse(custom_role.admin_default)

        response = self.client.put(
            f"/_private/api/roles/{custom_role.uuid}/",
            json_request_body,
            **self.request.META,
            content_type="application/json",
        )

        custom_role.refresh_from_db()

        self.assertFalse(custom_role.system)
        self.assertFalse(custom_role.platform_default)
        self.assertFalse(custom_role.admin_default)
        self.assertEqual(response.status_code, 200)

    def test_update_platform_default_flag_in_role(self):
        """Test that we can update a role."""
        tenant = Tenant.objects.create(tenant_name="1234", org_id="1234")
        custom_role = Role.objects.create(
            name="role 1", system=False, tenant=tenant, platform_default=True, admin_default=False
        )

        request_body = {
            "platform_default": "false",
        }

        json_request_body = json.dumps(request_body)

        self.assertFalse(custom_role.system)
        self.assertTrue(custom_role.platform_default)
        self.assertFalse(custom_role.admin_default)

        response = self.client.put(
            f"/_private/api/roles/{custom_role.uuid}/",
            json_request_body,
            **self.request.META,
            content_type="application/json",
        )

        custom_role.refresh_from_db()

        self.assertFalse(custom_role.system, False)
        self.assertFalse(custom_role.platform_default, False)
        self.assertFalse(custom_role.admin_default, False)
        self.assertEqual(response.status_code, 200)

    def test_update_admin_default_flag_in_role(self):
        """Test that we can update a role."""
        tenant = Tenant.objects.create(tenant_name="1234", org_id="1234")
        custom_role = Role.objects.create(
            name="role 1", system=False, tenant=tenant, platform_default=False, admin_default=True
        )

        request_body = {
            "admin_default": "false",
        }

        json_request_body = json.dumps(request_body)

        self.assertFalse(custom_role.system)
        self.assertFalse(custom_role.platform_default)
        self.assertTrue(custom_role.admin_default)

        response = self.client.put(
            f"/_private/api/roles/{custom_role.uuid}/",
            json_request_body,
            **self.request.META,
            content_type="application/json",
        )

        custom_role.refresh_from_db()

        self.assertFalse(custom_role.system)
        self.assertFalse(custom_role.platform_default)
        self.assertFalse(custom_role.admin_default)
        self.assertEqual(response.status_code, 200)

    def test_update_role(self):
        """Test that we can update a role."""
        tenant = Tenant.objects.create(tenant_name="1234", org_id="1234")
        custom_role = Role.objects.create(
            name="role 1", system=False, tenant=tenant, platform_default=False, admin_default=False
        )

        request_body = {"admin_default": "true", "system": "true", "platform_default": "true"}

        json_request_body = json.dumps(request_body)

        self.assertFalse(custom_role.system)
        self.assertFalse(custom_role.platform_default)
        self.assertFalse(custom_role.admin_default)

        response = self.client.put(
            f"/_private/api/roles/{custom_role.uuid}/",
            json_request_body,
            **self.request.META,
            content_type="application/json",
        )

        custom_role.refresh_from_db()

        self.assertTrue(custom_role.system)
        self.assertTrue(custom_role.platform_default)
        self.assertTrue(custom_role.admin_default)
        self.assertEqual(response.status_code, 200)

    def test_update_role_fails_disallowed_attributes(self):
        """Test that we can update a role."""
        tenant = Tenant.objects.create(tenant_name="1234", org_id="1234")
        custom_role = Role.objects.create(
            name="role 1", system=False, tenant=tenant, platform_default=False, admin_default=False
        )

        request_body = {"admin_default": "true", "system": "true", "name": "platform_default"}

        json_request_body = json.dumps(request_body)

        self.assertFalse(custom_role.system)
        self.assertFalse(custom_role.platform_default)
        self.assertFalse(custom_role.admin_default)

        response = self.client.put(
            f"/_private/api/roles/{custom_role.uuid}/",
            json_request_body,
            **self.request.META,
            content_type="application/json",
        )

        custom_role.refresh_from_db()

        self.assertFalse(custom_role.system)
        self.assertFalse(custom_role.platform_default)
        self.assertFalse(custom_role.admin_default)
        self.assertEqual(response.status_code, 400)

    def test_fetch_role(self):
        """Test that we can update a role."""
        tenant = Tenant.objects.create(tenant_name="1234", org_id="1234")
        custom_role = Role.objects.create(
            name="role 1", system=False, tenant=tenant, platform_default=False, admin_default=False
        )

        response = self.client.get(
            f"/_private/api/roles/{custom_role.uuid}/",
            **self.request.META,
            content_type="application/json",
        )

        body = json.loads(response.content.decode())
        role = body["role"]
        self.assertFalse(role["system"])
        self.assertFalse(role["platform_default"])
        self.assertFalse(role["admin_default"])
        self.assertEqual(response.status_code, 200)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_update_username_to_lowercase(self):
        """Test that the uppercase username would be updated to lowercase."""
        # Only POST is allowed
        response = self.client.delete(
            f"/_private/api/utils/username_lower/",
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        Principal.objects.bulk_create(
            [
                Principal(username="12345", tenant=self.tenant),
                Principal(username="ABCDE", tenant=self.tenant),
                Principal(username="Xyz", tenant=self.tenant),
                Principal(username="iJkLm", tenant=self.tenant),
                Principal(username="i.J.k@.L.m", tenant=self.tenant),
                Principal(username="user", tenant=self.tenant),
            ]
        )

        response = self.client.get(
            f"/_private/api/utils/username_lower/",
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.content.decode(),
            "Usernames to be updated: ['ABCDE', 'Xyz', 'i.J.k@.L.m', 'iJkLm'] to ['abcde', 'i.j.k@.l.m', 'ijklm', 'xyz']",
        )

        response = self.client.post(
            f"/_private/api/utils/username_lower/",
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        usernames = Principal.objects.values_list("username", flat=True).order_by("username")
        self.assertEqual({"12345", "abcde", "i.j.k@.l.m", "ijklm", "user", "xyz"}, set(usernames))

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@email.com",
                    "first_name": "user",
                    "last_name": "test",
                    "user_id": "u1",
                    "org_id": "12345",
                    "is_active": False,
                }
            ],
        },
    )
    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_delete_principal(self, _):
        """Test that we can delete principal."""
        # No username specified
        response = self.client.delete(f"/_private/api/utils/principal/", **self.request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="12345")
        Principal.objects.bulk_create(
            [
                Principal(username="test_user", tenant=tenant),
                Principal(username="test2", tenant=tenant),
            ]
        )
        # Get usernames of the principals to be deleted
        response = self.client.get(
            "/_private/api/utils/principal/?usernames=test_user,test2&user_type=user", **self.request.META
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.content.decode(),
            "Principals to be deleted: ['test2']",
        )

        # Delete the principals of type user
        response = self.client.delete(
            "/_private/api/utils/principal/?usernames=test_user, test2&user_type=user", **self.request.META
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(Principal.objects.filter(username="test_user").exists())
        self.assertTrue(Principal.objects.filter(username="test2").exists())

        # Delete the principals of type service-account
        Principal.objects.bulk_create(
            [
                Principal(username="sa_1", tenant=tenant, type="service-account"),
                Principal(username="sa_2", tenant=tenant, type="service-account"),
            ]
        )
        response = self.client.delete(
            "/_private/api/utils/principal/?usernames=sa_1,sa_2&user_type=service-account", **self.request.META
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Principal.objects.filter(type="service-account").exists())

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_clean_up_roles_in_cars(self):
        """Test that we can get and clean up cars with custom roles."""
        tenant = Tenant.objects.create(tenant_name="1234", org_id="XXXX")
        custom_role = Role.objects.create(
            name="role 1", system=False, tenant=tenant, platform_default=False, admin_default=False
        )
        system_role = self.role
        car = CrossAccountRequest.objects.create(
            target_org="123456",
            user_id="1111111",
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(10),
            status="approved",
        )
        car.roles.add(*(system_role, custom_role))
        self.assertTrue(system_role.system)
        self.assertTrue(car.roles.filter(id=system_role.id).exists())
        self.assertTrue(car.roles.filter(id=custom_role.id).exists())
        response = self.client.get(
            f"/_private/api/cars/clean/",
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.content.decode(), json.dumps({str(car.request_id): (custom_role.id, custom_role.display_name)})
        )
        custom_role.refresh_from_db()
        system_role.refresh_from_db()

        response = self.client.post(
            f"/_private/api/cars/clean/",
            **self.request.META,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(car.roles.filter(id=system_role.id).exists())
        self.assertFalse(car.roles.filter(id=custom_role.id).exists())


class InternalViewsetUserLookupTests(BaseInternalViewsetTests):
    """Test the /api/utils/user_lookup/ endpoint from internal viewset"""

    def setUp(self):
        """Set up the get user data tests"""
        super().setUp()

        self.API_PATH = "/_private/api/utils/user_lookup/"

    def tearDown(self):
        """Tear down user lookup tests."""
        super().tearDown()
        # Clear the principal cache for the test tenant to avoid test isolation issues
        from management.utils import PRINCIPAL_CACHE

        PRINCIPAL_CACHE.delete_all_principals_for_tenant("12345")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@redhat.com",
                    "is_org_admin": "true",
                    "org_id": "12345",
                }
            ],
        },
    )
    def test_user_lookup_happy_path(self, _):
        # given (a lot of setup)
        # user data we want to query for
        username = "test_user"
        email = "test_user@redhat.com"

        # create a tenant and principal for our user
        tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="12345")
        principal = Principal.objects.create(username=username, tenant=tenant)

        # create platform & admin default groups
        Group.objects.create(
            name="test_group_platform_default",
            tenant=self.public_tenant,
            system=True,
            admin_default=False,
            platform_default=True,
        )
        Group.objects.create(
            name="test_group_admin_default",
            tenant=self.public_tenant,
            system=True,
            admin_default=True,
            platform_default=False,
        )

        # create a test group and add our user to it
        test_group = Group.objects.create(name="test_group", tenant=tenant)
        test_group.principals.add(principal)

        # add some roles to our test group
        test_role1 = Role.objects.create(name="test_role1", tenant=tenant)
        test_role2 = Role.objects.create(name="test_role2", tenant=tenant)
        test_policy = Policy.objects.create(name="test_policy", group=test_group, tenant=tenant)
        test_policy.roles.add(test_role1, test_role2)
        test_group.policies.add(test_policy)

        # and finally add some permissions to our test roles
        test_perm1 = Permission.objects.create(permission="app:res1:*", tenant=tenant)
        test_perm2 = Permission.objects.create(permission="app:res2:*", tenant=tenant)
        Access.objects.create(permission=test_perm1, role=test_role1, tenant=tenant)
        Access.objects.create(permission=test_perm2, role=test_role1, tenant=tenant)

        test_perm3 = Permission.objects.create(permission="app:res3:read", tenant=tenant)
        test_perm4 = Permission.objects.create(permission="app:res3:write", tenant=tenant)
        Access.objects.create(permission=test_perm3, role=test_role2, tenant=tenant)
        Access.objects.create(permission=test_perm4, role=test_role2, tenant=tenant)

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        resp = response.content.decode()
        msg = f"[response from rbac: '{resp}']"

        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK, msg=msg)
        body = json.loads(resp)

        # check top level fields
        self.assertTrue(("username" in body), msg=msg)
        self.assertTrue(("email_address" in body), msg=msg)
        self.assertTrue(("groups" in body), msg=msg)
        self.assertEqual(body["username"], username, msg=msg)
        self.assertEqual(body["email_address"], email, msg=msg)

        resp_groups = body["groups"]
        self.assertIsInstance(resp_groups, list, msg=msg)
        self.assertEqual(len(resp_groups), 3, msg=msg)

        # check all our groups were returned
        group_names = [group["name"] for group in resp_groups]
        self.assertCountEqual(
            group_names, ["test_group", "test_group_platform_default", "test_group_admin_default"], msg=msg
        )

        # check our test group has all its roles
        resp_test_group = [group for group in resp_groups if group["name"] == "test_group"][0]
        self.assertIsNotNone(resp_test_group, msg=msg)

        resp_test_group_roles = resp_test_group["roles"]
        self.assertIsNotNone(resp_test_group_roles, msg=msg)
        self.assertIsInstance(resp_test_group_roles, list, msg=msg)
        self.assertEqual(len(resp_test_group_roles), 2, msg=msg)

        role_names = [role["name"] for role in resp_test_group_roles]
        self.assertCountEqual(role_names, ["test_role1", "test_role2"], msg=msg)

        # and finally check all our roles have all their permissions
        resp_test_group_role1 = resp_test_group_roles[0]
        resp_test_group_role2 = resp_test_group_roles[1]

        self.assertIsInstance(resp_test_group_role1["permissions"], list, msg=msg)
        self.assertEqual(len(resp_test_group_role1["permissions"]), 2)
        self.assertCountEqual(resp_test_group_role1["permissions"], ["app | res1 | *", "app | res2 | *"], msg=msg)

        self.assertIsInstance(resp_test_group_role2["permissions"], list, msg=msg)
        self.assertEqual(len(resp_test_group_role2["permissions"]), 2, msg=msg)
        self.assertCountEqual(
            resp_test_group_role2["permissions"], ["app | res3 | read", "app | res3 | write"], msg=msg
        )

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@redhat.com",
                    "is_org_admin": "true",
                    "org_id": "12345",
                }
            ],
        },
    )
    def test_user_lookup_custom_default_groups(self, _):
        # given (a lot of setup)
        # user data we want to query for
        username = "test_user"
        email = "test_user@redhat.com"

        # create a tenant and principal for our user
        tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="12345")
        principal = Principal.objects.create(username=username, tenant=tenant)

        # create custom platform & admin default groups
        Group.objects.create(
            name="test_group_platform_default_custom",
            tenant=tenant,
            system=True,
            admin_default=False,
            platform_default=True,
        )
        Group.objects.create(
            name="test_group_admin_default_custom",
            tenant=tenant,
            system=True,
            admin_default=True,
            platform_default=False,
        )

        # create public platform & admin default groups
        Group.objects.create(
            name="test_group_platform_default_public",
            tenant=self.public_tenant,
            system=True,
            admin_default=False,
            platform_default=True,
        )
        Group.objects.create(
            name="test_group_admin_default_public",
            tenant=self.public_tenant,
            system=True,
            admin_default=True,
            platform_default=False,
        )

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        resp = response.content.decode()
        msg = f"[response from rbac: '{resp}']"

        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK, msg=msg)
        body = json.loads(resp)

        self.assertTrue(("groups" in body), msg=msg)
        resp_groups = body["groups"]
        self.assertIsInstance(resp_groups, list, msg=msg)
        self.assertEqual(len(resp_groups), 2, msg=msg)

        # the custom groups should be present, not the public ones
        group_names = [group["name"] for group in resp_groups]
        self.assertCountEqual(
            group_names, ["test_group_platform_default_custom", "test_group_admin_default_custom"], msg=msg
        )

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@redhat.com",
                    "is_org_admin": "true",
                    "org_id": "12345",
                }
            ],
        },
    )
    def test_user_lookup_via_email(self, _):
        # given
        username = "test_user"
        email = "test_user@redhat.com"

        # create a tenant and principal for our user
        tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="12345")
        Principal.objects.create(username=username, tenant=tenant)

        # when
        response = self.client.get(f"{self.API_PATH}?email={email}", **self.request.META)

        resp = response.content.decode()
        msg = f"[response from rbac: '{resp}']"

        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK, msg=msg)
        body = json.loads(resp)

        # check top level fields
        self.assertTrue(("username" in body), msg=msg)
        self.assertTrue(("email_address" in body), msg=msg)
        self.assertEqual(body["username"], username, msg=msg)
        self.assertEqual(body["email_address"], email, msg=msg)

    def test_user_lookup_only_get_method_allowed(self):
        # when
        response = self.client.post(f"{self.API_PATH}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("Invalid http method", resp_body["error"])

    def test_user_lookup_no_input_provided(self):
        # when
        response = self.client.get(f"{self.API_PATH}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("you must provide either 'email' or 'username' as query params", resp_body["error"])

    def test_user_lookup_username_invalid(self):
        # given
        username = "   "

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("username contains only whitespace", resp_body["error"])

    def test_user_lookup_email_invalid(self):
        # given
        email = "   "

        # when
        response = self.client.get(f"{self.API_PATH}?email={email}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("email contains only whitespace", resp_body["error"])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 500,
            "errors": [{"connection refused rip"}],
        },
    )
    def test_user_lookup_bop_returns_error(self, _):
        # given
        username = "test_user"

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("unexpected status: '500' returned from bop", resp_body["error"])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [],
        },
    )
    def test_user_lookup_bop_returns_empty_set(self, _):
        # given
        username = "test_user"

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("Not found", resp_body["error"])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "email": "test_user@redhat.com",
                    "is_org_admin": "true",
                    "org_id": "12345",
                }
            ],
        },
    )
    def test_user_lookup_bop_returns_user_without_username(self, _):
        # given
        email = "test_user@redhat.com"

        # when
        response = self.client.get(f"{self.API_PATH}?email={email}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("user found in bop but no username exists", resp_body["error"])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@redhat.com",
                    "org_id": "12345",
                }
            ],
        },
    )
    def test_user_lookup_bop_returns_user_without_is_org_admin(self, _):
        # given
        email = "test_user@redhat.com"

        # create a tenant and principal for our user
        tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="12345")
        Principal.objects.create(username="test_user", tenant=tenant)

        # create platform & admin default groups
        Group.objects.create(
            name="test_group_platform_default",
            tenant=tenant,
            system=True,
            admin_default=False,
            platform_default=True,
        )
        Group.objects.create(
            name="test_group_admin_default",
            tenant=tenant,
            system=True,
            admin_default=True,
            platform_default=False,
        )

        # when
        response = self.client.get(f"{self.API_PATH}?email={email}", **self.request.META)

        # then - in this case it should default is_org_admin to false and continue request
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resp_body = json.loads(response.content.decode())
        self.assertTrue(("groups" in resp_body))

        resp_groups = resp_body["groups"]
        self.assertIsInstance(resp_groups, list)

        group_names = [group["name"] for group in resp_groups]
        self.assertNotIn("test_group_admin_default", group_names)
        self.assertIn("test_group_platform_default", group_names)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@redhat.com",
                    "is_org_admin": "true",
                }
            ],
        },
    )
    def test_user_lookup_bop_returns_user_without_org_id(self, _):
        # given
        username = "test_user"

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("user found in bop but no org_id exists", resp_body["error"])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@redhat.com",
                    "is_org_admin": "false",
                    "org_id": "12345",
                }
            ],
        },
    )
    def test_user_lookup_tenant_does_not_exist_in_rbac(self, _):
        # given
        username = "test_user"

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("failed to query rbac for tenant with org_id: '12345'", resp_body["error"])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@redhat.com",
                    "is_org_admin": "false",
                    "org_id": "12345",
                }
            ],
        },
    )
    @patch(
        "internal.views.get_principal",
        side_effect=Exception("something went wrong"),
    )
    def test_user_lookup_get_principal_raises_exception(self, __, _):
        # given
        username = "test_user"

        Tenant.objects.create(tenant_name="test_tenant", org_id="12345")

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        # then
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

        resp_body = json.loads(response.content.decode())
        self.assertIsNotNone(resp_body["error"])
        self.assertIn("failed to query rbac for user: 'test_user'", resp_body["error"])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "username": "test_user",
                    "email": "test_user@redhat.com",
                    "is_org_admin": "false",
                    "org_id": "12345",
                }
            ],
        },
    )
    def test_user_lookup_user_does_not_exist_in_rbac(self, _):
        # given
        username = "test_user"
        # we don't add principal to rbac db
        tenant = Tenant.objects.create(tenant_name="test_tenant", org_id="12345")
        Group.objects.create(
            name="test_group_platform_default",
            tenant=tenant,
            system=True,
            admin_default=False,
            platform_default=True,
        )

        # when
        response = self.client.get(f"{self.API_PATH}?username={username}", **self.request.META)

        # then
        # only groups that exist should be default
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resp_body = json.loads(response.content.decode())
        self.assertTrue(("groups" in resp_body))

        resp_groups = resp_body["groups"]
        self.assertIsInstance(resp_groups, list)
        self.assertEqual(len(resp_groups), 1)
        self.assertEqual(resp_groups[0]["name"], "test_group_platform_default")


@override_settings(ATOMIC_RETRY_DISABLED=True)
class FixMissingBindingBaseTuplesTests(BaseInternalViewsetTests):
    """Test the fix_missing_binding_base_tuples internal API endpoint."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.url = "/_private/api/utils/fix_missing_binding_base_tuples/"
        self._tuples = InMemoryTuples()

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, ROOT_SCOPE_PERMISSIONS="test:resource:read")
    @patch("management.tasks.fix_missing_binding_base_tuples_in_worker.delay")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_fix_missing_base_tuples(self, mock_replicate, mock_worker):
        """
        Test that API adds missing t_role and t_binding tuples for bindings.

        Setup: Create a binding without base tuples (simulating pre-V2 binding)
        Action: Call fix API
        Verify: Base tuples now exist in Kessel
        """
        # Redirect replication to in-memory tuples for verification
        replicator = InMemoryRelationReplicator(self._tuples)
        mock_replicate.side_effect = replicator.replicate

        # Create a system role
        role = Role.objects.create(name="Test System Role Missing Tuples", system=True, tenant=self.public_tenant)
        perm = Permission.objects.create(
            permission="test:resource:read",
            application="test",
            resource_type="resource",
            verb="read",
            tenant=self.public_tenant,
        )
        Access.objects.create(role=role, permission=perm, tenant=self.public_tenant)

        seed_v2_role_from_v1(role)

        # Bootstrap the tenant to create workspace hierarchy (without replicating, since we don't need the tuples).
        bootstrap_tenant_for_v2_test(self.tenant)

        # Create binding WITHOUT replication (simulating pre-V2 state - missing base tuples)
        RelationApiDualWriteGroupHandler(
            group=self.group, event_type=ReplicationEventType.ASSIGN_ROLE, replicator=NoopReplicator()
        ).generate_relations_reset_roles([role])

        root_ws = Workspace.objects.root(tenant=self.tenant)

        binding = BindingMapping.objects.get(role=role)
        binding_id = binding.mappings["id"]

        # BEFORE FIX: Verify NO tuples exist (simulating missing Kessel relationships)
        self.assertEqual(len(self._tuples), 0, "Should have NO tuples before fix")

        # Simulate worker running synchronously by calling it directly
        from management.tasks import fix_missing_binding_base_tuples_in_worker

        mock_worker.side_effect = lambda **kwargs: fix_missing_binding_base_tuples_in_worker(**kwargs)

        # CALL THE FIX API
        response = self.client.post(f"{self.url}?binding_uuids={binding_id}", **self.request.META)
        self.assertEqual(response.status_code, 202)  # Now returns 202 (Accepted) since it's async

        data = response.json()
        self.assertIn("message", data)
        self.assertEqual(data["binding_uuids"], [binding_id])

        # Verify worker was called
        mock_worker.assert_called_once_with(binding_uuids=[binding_id])

        # AFTER FIX: Verify ALL tuples now exist.

        # Verify t_role tuple was created
        t_role_after = self._tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("role"),
                subject("rbac", "role", str(role.uuid)),
            )
        )
        self.assertEqual(len(t_role_after), 1, "Should have t_role tuple after fix")

        # Verify t_binding tuple was created
        t_binding_after = self._tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", str(root_ws.id)),
                relation("binding"),
                subject("rbac", "role_binding", binding_id),
            )
        )
        self.assertEqual(len(t_binding_after), 1, "Should have t_binding tuple after fix")

        # Verify subject tuple was created
        subject_after = self._tuples.find_tuples(
            all_of(
                resource("rbac", "role_binding", binding_id),
                relation("subject"),
                subject("rbac", "group", str(self.group.uuid), "member"),
            )
        )
        self.assertEqual(len(subject_after), 1, "Should have subject tuple after fix")

        # Total: API replicates ALL tuples (t_role + t_binding + subject = 3 tuples)
        # Note: No permission tuples because system role has empty permissions array
        self.assertEqual(len(self._tuples), 3, "Should have all 3 tuples after fix (t_role + t_binding + subject)")


class CleanInvalidWorkspaceResourceDefinitionsTests(BaseInternalViewsetTests):
    """Test the clean_invalid_workspace_resource_definitions internal API endpoint."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.url = "/_private/api/utils/clean_invalid_workspace_resource_definitions/"

        bootstrap_tenant_for_v2_test(self.tenant)

    @override_settings(REPLICATION_TO_RELATION_ENABLED=True)
    @patch("management.tasks.clean_invalid_workspace_resource_definitions_in_worker.delay")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_clean_removes_invalid_workspace_ids_and_deletes_bindings(self, mock_replicate, mock_worker):
        """
        Test that API removes invalid workspace IDs from resource definitions and deletes bad bindings.

        Setup: Create role with resource definition containing valid and invalid workspace IDs
        Action: Call cleanup API
        Verify: Invalid IDs removed, invalid bindings deleted, valid data preserved
        """
        # Redirect replication to in-memory tuples
        self._tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(self._tuples)
        mock_replicate.side_effect = replicator.replicate

        # Create a custom role
        role = Role.objects.create(name="Test Role Invalid Workspaces", system=False, tenant=self.tenant)
        perm = Permission.objects.create(
            permission="inventory:groups:read",
            application="inventory",
            resource_type="groups",
            verb="read",
            tenant=self.tenant,
        )
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)

        # Create workspace hierarchy (need both root and default for dual write handler)
        root_ws, _ = Workspace.objects.get_or_create(
            tenant=self.tenant, type=Workspace.Types.ROOT, defaults={"name": "Root Workspace"}
        )
        default_ws, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            defaults={"name": "Default Workspace", "parent": root_ws},
        )
        valid_ws = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=root_ws,
            name="Valid Workspace",
        )

        # Create resource definition with one valid and one invalid workspace ID
        fake_ws_id = "95473d62-56ea-4c0c-8945-4f3f6a620669"  # Doesn't exist
        rd = ResourceDefinition.objects.create(
            access=access,
            attributeFilter={
                "key": "group.id",
                "operation": "in",
                "value": [str(valid_ws.id), fake_ws_id],
            },
            tenant=self.tenant,
        )

        # Create bindings for both workspaces (one valid, one invalid)
        valid_binding = BindingMapping.objects.create(
            role=role,
            mappings={
                "id": str(uuid.uuid4()),
                "groups": [],
                "users": {},
                "role": {"id": str(role.uuid), "is_system": False, "permissions": ["inventory_groups_read"]},
            },
            resource_type_namespace="rbac",
            resource_type_name="workspace",
            resource_id=str(valid_ws.id),
        )

        invalid_binding = BindingMapping.objects.create(
            role=role,
            mappings={
                "id": str(uuid.uuid4()),
                "groups": [],
                "users": {},
                "role": {"id": str(role.uuid), "is_system": False, "permissions": ["inventory_groups_read"]},
            },
            resource_type_namespace="rbac",
            resource_type_name="workspace",
            resource_id=fake_ws_id,
        )

        # BEFORE: Verify resource definition has 2 workspace IDs
        self.assertEqual(len(rd.attributeFilter["value"]), 2)

        # BEFORE: Verify 2 bindings exist
        self.assertEqual(BindingMapping.objects.filter(role=role).count(), 2)

        # Simulate worker running synchronously by calling it directly
        from internal.utils import clean_invalid_workspace_resource_definitions as cleanup_function

        mock_worker.side_effect = lambda dry_run=False: cleanup_function(dry_run=dry_run)

        # Call the cleanup API
        response = self.client.post(f"{self.url}", **self.request.META)
        self.assertEqual(response.status_code, 202)  # Async - returns 202

        # Verify worker was called
        mock_worker.assert_called_once()

        # AFTER: Verify resource definition now has only 1 workspace ID (the valid one)
        rd.refresh_from_db()
        self.assertEqual(len(rd.attributeFilter["value"]), 1)
        self.assertEqual(rd.attributeFilter["value"][0], str(valid_ws.id))

        # AFTER: Verify only 1 binding remains (the valid one)
        remaining_bindings = BindingMapping.objects.filter(role=role)
        self.assertEqual(remaining_bindings.count(), 1)
        self.assertEqual(remaining_bindings.first().resource_id, str(valid_ws.id))


class InternalViewsetResourceDefinitionTests(IdentityRequest):
    def setUp(self):
        """Set up the access view tests."""
        super().setUp()
        bootstrap_tenant_for_v2_test(self.tenant)
        self.client = APIClient()
        self.customer = self.customer_data
        self.internal_request_context = self._create_request_context(self.customer, self.user_data, is_internal=True)
        self.internal_request = self.internal_request_context["request"]

        request = self.request_context["request"]
        user = User()
        user.username = self.user_data["username"]
        user.account = self.customer_data["account_id"]
        user.org_id = self.customer_data["org_id"]
        request.user = user
        public_tenant = Tenant.objects.get(tenant_name="public")

        test_tenant_org_id = "100001"

        # we need to delete old test_tenant's that may exist in cache
        TENANTS = TenantCache()
        TENANTS.delete_tenant(test_tenant_org_id)

        # items with test_ prefix have hard coded attributes for new BOP requests
        self.test_tenant = Tenant(
            tenant_name="acct1111111", account_id="1111111", org_id=test_tenant_org_id, ready=True
        )
        self.test_tenant.save()
        self.test_principal = Principal(username="test_user", tenant=self.test_tenant)
        self.test_principal.save()
        self.test_group = Group(name="test_groupA", tenant=self.test_tenant)
        self.test_group.save()
        self.test_group.principals.add(self.test_principal)
        self.test_group.save()
        self.test_permission = Permission.objects.create(permission="app:test_*:test_*", tenant=self.test_tenant)
        Permission.objects.create(permission="app:test_foo:test_bar", tenant=self.test_tenant)
        user_data = {"username": "test_user", "email": "test@gmail.com"}
        request_context = self._create_request_context(
            {"account_id": "1111111", "tenant_name": "acct1111111", "org_id": "100001"}, user_data, is_org_admin=True
        )
        request = request_context["request"]
        self.test_headers = request.META
        test_tenant_root_workspace = Workspace.objects.create(
            name="Test Tenant Root Workspace", type=Workspace.Types.ROOT, tenant=self.test_tenant
        )
        Workspace.objects.create(
            name="Test Tenant Default Workspace",
            type=Workspace.Types.DEFAULT,
            parent=test_tenant_root_workspace,
            tenant=self.test_tenant,
        )

        self.principal = Principal(username=user.username, tenant=self.tenant)
        self.principal.save()
        self.admin_principal = Principal(username="user_admin", tenant=self.tenant)
        self.admin_principal.save()
        self.group = Group(name="groupA", tenant=self.tenant)
        self.group.save()
        self.group.principals.add(self.principal)
        self.group.save()
        self.permission = Permission.objects.create(permission="app:*:*", tenant=self.tenant)
        Permission.objects.create(permission="app:foo:bar", tenant=self.tenant)
        tenant_root_workspace, _ = Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
            defaults={"name": "root", "description": "Root workspace"},
        )
        Workspace.objects.get_or_create(
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            defaults={"name": "Tenant Default Workspace", "parent": tenant_root_workspace},
        )

    def create_role(self, role_name, headers, in_access_data=None):
        """Create a role."""
        access_data = self.access_data
        if in_access_data:
            access_data = in_access_data
        test_data = {"name": role_name, "access": [access_data]}

        # create a role
        url = reverse("v1_management:role-list")
        client = APIClient()
        response = client.post(url, test_data, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response

    def create_policy(self, policy_name, group, roles, tenant):
        """Create a policy."""
        # create a policy
        policy = Policy.objects.create(name=policy_name, tenant=tenant, system=True)
        for role in Role.objects.filter(uuid__in=roles):
            policy.roles.add(role)
        policy.group = Group.objects.get(uuid=group)
        policy.save()

    def create_platform_default_resource(self):
        """Setup default group and role."""
        default_permission = Permission.objects.create(permission="default:*:*", tenant=self.tenant)
        default_role = Role.objects.create(name="default role", platform_default=True, system=True, tenant=self.tenant)
        default_access = Access.objects.create(permission=default_permission, role=default_role, tenant=self.tenant)
        default_policy = Policy.objects.create(name="default policy", system=True, tenant=self.tenant)
        default_policy.roles.add(default_role)
        default_group = Group.objects.create(
            name="default group", system=True, platform_default=True, tenant=self.tenant
        )
        default_group.policies.add(default_policy)

    def create_role_and_permission(self, role_name, permission):
        role = Role.objects.create(name=role_name, tenant=self.tenant)
        assigned_permission = Permission.objects.create(permission=permission, tenant=self.tenant)
        access = Access.objects.create(role=role, permission=assigned_permission, tenant=self.tenant)
        return role

    def test_get_correct_string_resource_definition(self):
        """Test that a string attributeFilter can have the equal operation"""

        role_name = "roleA"

        self.access_data = {
            "permission": "app:*:*",
            "resourceDefinitions": [{"attributeFilter": {"key": "key1.id", "operation": "equal", "value": "value1"}}],
        }

        response = self.create_role(role_name, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_get_incorrect_string_resource_definition(self):
        """Test that a string attributeFilter cannot have the in operation"""
        role = Role.objects.create(name="role_A", tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter={"key": "key1.id", "operation": "in", "value": "value1"},
            tenant=self.tenant,
        )

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"1 resource definitions would be corrected")

    def test_get_correct_list_resource_definition(self):
        """Test that a list attributeFilter can have the in operation"""

        role_name = "roleA"

        self.access_data = {
            "permission": "app:*:*",
            "resourceDefinitions": [
                {"attributeFilter": {"key": "key1.id", "operation": "in", "value": ["value1", "value2"]}}
            ],
        }

        response = self.create_role(role_name, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_get_incorrect_list_resource_definition(self):
        """Test that a list attributeFilter cannot have the equal operation"""
        role = Role.objects.create(name="role_A", tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter={"key": "key1.id", "operation": "equal", "value": ["value1", "value2"]},
            tenant=self.tenant,
        )

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"1 resource definitions would be corrected")

    def test_get_incorrect_resource_definition_with_details(self):
        """Test we can get list of invalid resource definitions with 'detail=true' query param."""
        role = Role.objects.create(name="role_A", tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        attribute_filter_data = [
            {"key": "key1_id", "operation": "equal", "value": ["value1", "value2"]},
            {"key": "key2_id", "operation": "in", "value": "value1, value2"},
            {"key": "key3_id", "operation": "in", "value": "string"},
        ]
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[0],
            tenant=self.tenant,
        )
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[1],
            tenant=self.tenant,
        )
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[2],
            tenant=self.tenant,
        )
        # Send the request without 'detail=true' query param
        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"3 resource definitions would be corrected")

        # Send the request with 'detail=true' query param
        response = self.client.get(
            f"/_private/api/utils/resource_definitions/?detail=true",
            **self.internal_request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 3)
        for rf_from_response in response.json():
            for at in attribute_filter_data:
                if rf_from_response["attributeFilter"]["key"] == at["key"]:
                    operation = rf_from_response["attributeFilter"]["operation"]
                    value = rf_from_response["attributeFilter"]["value"]
                    expected_operation = at["operation"]
                    expected_value = at["value"]
                    self.assertEqual(operation, expected_operation)
                    self.assertEqual(value, expected_value)

    def test_patch_correct_string_resource_definition(self):
        """Test patching a string attributeFilter with the equal operation"""

        role_name = "roleA"

        self.access_data = {
            "permission": "app:*:*",
            "resourceDefinitions": [{"attributeFilter": {"key": "key1.id", "operation": "equal", "value": "value1"}}],
        }

        response = self.create_role(role_name, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 0 bad resource definitions")

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_patch_incorrect_string_resource_definition(self):
        """Test patching a string attributeFilter with the in operation"""
        role = Role.objects.create(name="role_A", tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter={"key": "key1.id", "operation": "in", "value": "value1"},
            tenant=self.tenant,
        )

        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 1 bad resource definitions")

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_patch_correct_list_resource_definition(self):
        """Test patching a list attributeFilter with the in operation"""

        role_name = "roleA"

        self.access_data = {
            "permission": "app:*:*",
            "resourceDefinitions": [
                {"attributeFilter": {"key": "key1.id", "operation": "in", "value": ["value1", "value2"]}}
            ],
        }

        response = self.create_role(role_name, headers=self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 0 bad resource definitions")

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_patch_incorrect_list_resource_definition(self):
        """Test patching a list attributeFilter with the equal operation"""
        role = Role.objects.create(name="role_A", tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:*:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter={"key": "key1.id", "operation": "equal", "value": ["value1", "value2"]},
            tenant=self.tenant,
        )

        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 1 bad resource definitions")

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_patch_invalid_resource_definitions_operation_equal(self):
        """Test patching a attributeFilters operation to a valid operation for 'equal'"""
        role = Role.objects.create(name="role_A", tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter={"key": "key1.id", "operation": "equals", "value": "value1"},
            tenant=self.tenant,
        )

        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 1 bad resource definitions")

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_patch_invalid_resource_definitions_operation_equal_value_int(self):
        """Test patching a attributeFilters operation to a valid operation for 'equal' when value is int"""
        role = Role.objects.create(name="role_A", tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter={"key": "key1.id", "operation": "equals", "value": 12345},
            tenant=self.tenant,
        )

        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 1 bad resource definitions")

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_patch_invalid_resource_definitions_operation_in(self):
        """Test patching a attributeFilters operation to a valid operation for 'in'"""
        role = Role.objects.create(name="role_A", tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter={"key": "key1.id", "operation": "inWrong", "value": ["value1", "value2"]},
            tenant=self.tenant,
        )

        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 1 bad resource definitions")

        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

    def test_patch_incorrect_resource_definition_all(self):
        """Test we can patch all invalid resource definitions."""
        role_name = "role_A"
        role = Role.objects.create(name=role_name, tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        attribute_filter_data = [
            {"key": "key1_id", "operation": "equal", "value": ["value1", "value2"]},
            {"key": "key2_id", "operation": "in", "value": "value1, value2"},
            {"key": "key3_id", "operation": "in", "value": "string"},
        ]
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[0],
            tenant=self.tenant,
        )
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[1],
            tenant=self.tenant,
        )
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[2],
            tenant=self.tenant,
        )

        # Send the GET request to check we have fixable resource definitions
        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"3 resource definitions would be corrected")

        # Send the PATCH request to fix all resource definitions
        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 3 bad resource definitions")

        # Send the GET request to check we don't have fixable resource definitions
        response = self.client.get(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"0 resource definitions would be corrected")

        # Check the resource definitions were fixed as expected
        for rf in ResourceDefinition.objects.filter(access__role__name=role_name):
            if rf.attributeFilter["key"] == "key1_id":
                operation = rf.attributeFilter["operation"]
                self.assertEqual(operation, "in")
            elif rf.attributeFilter["key"] == "key2_id":
                value = rf.attributeFilter["value"]
                self.assertIsInstance(value, list)
                self.assertEqual(len(value), 2)
                self.assertEqual(value[0], "value1")
                self.assertEqual(value[1], "value2")
            elif rf.attributeFilter["key"] == "key3_id":
                operation = rf.attributeFilter["operation"]
                self.assertEqual(operation, "equal")

    def test_patch_incorrect_resource_definition_by_id(self):
        """Test we can patch one invalid resource definitions with 'id' query param."""
        role_name = "role_A"
        role = Role.objects.create(name=role_name, tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        attribute_filter_data = [
            {"key": "key1_id", "operation": "equal", "value": ["value1", "value2"]},
            {"key": "key2_id", "operation": "in", "value": "value1, value2"},
            {"key": "key3_id", "operation": "in", "value": "string"},
        ]
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[0],
            tenant=self.tenant,
        )
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[1],
            tenant=self.tenant,
        )
        ResourceDefinition.objects.create(
            access=access,
            attributeFilter=attribute_filter_data[2],
            tenant=self.tenant,
        )

        # Send the GET request to get details resource definitions ids
        response = self.client.get(
            f"/_private/api/utils/resource_definitions/?detail=true",
            **self.internal_request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        resource_definitions_ids = [rf["id"] for rf in response.json()]

        # Send the PATCH request with 'id' query parameter to fix only 1 resource definition
        for rf_id in resource_definitions_ids:
            response = self.client.patch(
                f"/_private/api/utils/resource_definitions/?id={rf_id}",
                **self.internal_request.META,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.content.decode(), f"Resource definition id = {rf_id} updated.")

        # Send the PATCH request to fix all resource definitions
        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 0 bad resource definitions")

        # Check the resource definitions were fixed as expected
        for rf in ResourceDefinition.objects.filter(access__role__name=role_name):
            if rf.attributeFilter["key"] == "key1_id":
                operation = rf.attributeFilter["operation"]
                self.assertEqual(operation, "in")
            elif rf.attributeFilter["key"] == "key2_id":
                value = rf.attributeFilter["value"]
                self.assertIsInstance(value, list)
                self.assertEqual(len(value), 2)
                self.assertEqual(value[0], "value1")
                self.assertEqual(value[1], "value2")
            elif rf.attributeFilter["key"] == "key3_id":
                operation = rf.attributeFilter["operation"]
                self.assertEqual(operation, "equal")

    def test_patch_hbi_resource_definition(self):
        """Test we can patch one invalid hbi resource definitions."""
        role_name = "role_A"
        role = Role.objects.create(name=role_name, tenant=self.tenant)
        perm = Permission.objects.create(permission="test_app:operation:*", tenant=self.tenant)
        access = Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        attribute_filter_data = [
            {"key": "group.id", "operation": "equal", "value": ["value1", "value2"]},
            {"key": "group.id", "operation": "equal", "value": None},
            {"key": "group.id", "operation": "equal", "value": "string"},
            {"key": "group.id", "operation": "in", "value": {"id": "12345"}},
            {"key": "group.id", "operation": "equal", "value": 2},
            {"key": "group.id", "operation": "in", "value": {}},
        ]
        rfs = ResourceDefinition.objects.bulk_create(
            [
                ResourceDefinition(
                    access=access,
                    attributeFilter=attribute_filter_data[0],
                    tenant=self.tenant,
                ),
                ResourceDefinition(
                    access=access,
                    attributeFilter=attribute_filter_data[1],
                    tenant=self.tenant,
                ),
                ResourceDefinition(
                    access=access,
                    attributeFilter=attribute_filter_data[2],
                    tenant=self.tenant,
                ),
                ResourceDefinition(
                    access=access,
                    attributeFilter=attribute_filter_data[3],
                    tenant=self.tenant,
                ),
                ResourceDefinition(
                    access=access,
                    attributeFilter=attribute_filter_data[4],
                    tenant=self.tenant,
                ),
                ResourceDefinition(
                    access=access,
                    attributeFilter=attribute_filter_data[5],
                    tenant=self.tenant,
                ),
            ]
        )

        # Send the PATCH request to fix all resource definitions
        response = self.client.patch(
            f"/_private/api/utils/resource_definitions/",
            **self.internal_request.META,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"Updated 6 bad resource definitions")

        # Check the resource definitions were fixed as expected
        values = [["value1", "value2"], [None], ["string"], ["12345"], [2], [None]]
        for index, rf in enumerate(rfs):
            rf.refresh_from_db()
            operation = rf.attributeFilter["operation"]
            self.assertEqual(operation, "in")
            value = rf.attributeFilter["value"]
            self.assertEqual(values[index], value)

    def test_bootstrap_pending_tenants(self):
        tenant = Tenant.objects.create(org_id="111", account_id="111")
        Tenant.objects.create(account_id="112")
        response = self.client.get(
            f"/_private/api/utils/bootstrap_pending_tenants/",
            **self.internal_request.META,
        )

        # self.tenant is bootstrapped in setUp; only test_tenant and tenant are pending
        expected_json = {
            "org_ids": sorted([str(self.test_tenant.org_id), str(tenant.org_id)]),
        }

        response_json = json.loads(response.content)
        response_json["org_ids"].sort()

        self.assertEqual(response_json, expected_json)


class InternalS2SViewsetTests(IdentityRequest):

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate_workspace")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, SERVICE_PSKS={"hbi": {"secret": "abc123"}})
    def test_create_ungrouped_workspace(self, replicate, replicate_workspace):
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate
        fixture = RbacFixture(V2TenantBootstrapService(replicator))

        org_id = "12345"
        payload = {"org_ids": [org_id]}
        bootstraped_tenant = fixture.new_tenant(org_id)
        tuples.clear()
        self.service_headers = {
            "HTTP_X_RH_RBAC_PSK": "abc123",
            "HTTP_X_RH_RBAC_CLIENT_ID": "hbi",
            "HTTP_X_RH_RBAC_ORG_ID": org_id,
        }

        self.assertFalse(
            Workspace.objects.filter(tenant=bootstraped_tenant.tenant, type=Workspace.Types.UNGROUPED_HOSTS).exists()
        )
        response = self.client.get(
            f"/_private/_s2s/workspaces/ungrouped/",
            data=payload,
            **self.service_headers,
            content_type="application/json",
        )

        default = Workspace.objects.get(tenant=bootstraped_tenant.tenant, type=Workspace.Types.DEFAULT)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ungrouped_hosts = response.json()
        ungrouped_host_relation = tuples.find_tuples(
            all_of(
                resource("rbac", "workspace", ungrouped_hosts["id"]),
                relation("parent"),
                subject("rbac", "workspace", str(default.id)),
            )
        )
        self.assertEqual(len(ungrouped_host_relation), 1)
        workspace_event = replicate_workspace.call_args[0][0]
        self.assertEqual(workspace_event.org_id, org_id)
        self.assertEqual(workspace_event.event_type, ReplicationEventType.CREATE_WORKSPACE)
        self.assertEqual(workspace_event.workspace["type"], str(Workspace.Types.UNGROUPED_HOSTS))

        # Get existing ungrouped workspace
        response = self.client.get(
            f"/_private/_s2s/workspaces/ungrouped/",
            **self.service_headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ungrouped_hosts.pop("modified")
        payload_get = response.json()
        payload_get.pop("modified")
        self.assertEqual(ungrouped_hosts, payload_get)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate_workspace")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    @override_settings(REPLICATION_TO_RELATION_ENABLED=True, SERVICE_PSKS={"hbi": {"secret": "abc123"}})
    def test_create_ungrouped_workspace_with_new_tenant(self, replicate, replicate_workspace):
        """Test creating ungrouped workspace when tenant doesn't exist yet."""
        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        replicate.side_effect = replicator.replicate

        org_id = "new-org-12345"

        # Ensure tenant doesn't exist
        self.assertFalse(Tenant.objects.filter(org_id=org_id).exists())

        self.service_headers = {
            "HTTP_X_RH_RBAC_PSK": "abc123",
            "HTTP_X_RH_RBAC_CLIENT_ID": "hbi",
            "HTTP_X_RH_RBAC_ORG_ID": org_id,
        }

        # Call the endpoint
        response = self.client.get(
            f"/_private/_s2s/workspaces/ungrouped/",
            **self.service_headers,
            content_type="application/json",
        )

        # Verify successful response
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify tenant was created
        tenant = Tenant.objects.get(org_id=org_id)
        self.assertIsNotNone(tenant)

        # Verify default workspace was created
        default_workspace = Workspace.objects.get(tenant=tenant, type=Workspace.Types.DEFAULT)
        self.assertIsNotNone(default_workspace)

        # Verify ungrouped workspace was created
        ungrouped_workspace = Workspace.objects.get(tenant=tenant, type=Workspace.Types.UNGROUPED_HOSTS)
        self.assertIsNotNone(ungrouped_workspace)
        self.assertEqual(ungrouped_workspace.parent, default_workspace)

        # Verify response content
        ungrouped_hosts_data = response.json()
        self.assertEqual(ungrouped_hosts_data["id"], str(ungrouped_workspace.id))
        self.assertEqual(ungrouped_hosts_data["type"], Workspace.Types.UNGROUPED_HOSTS)
        self.assertEqual(ungrouped_hosts_data["name"], Workspace.SpecialNames.UNGROUPED_HOSTS)


def valid_destructive_time():
    return timezone.now() + timedelta(hours=1)


def invalid_destructive_time():
    return timezone.now() - timedelta(hours=1)


class InternalRelationsViewsetTests(BaseInternalViewsetTests):
    """Test the /_private/api/relations/ endpoints from internal viewset."""

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_lookup_resources(self, mock_create_channel, mock_get_token):
        """Test a request to lookup_resource endpoint returns the correct response."""

        mock_stub = MagicMock()
        mock_response_1 = lookup_pb2.LookupResourcesResponse(
            resource=common_pb2.ObjectReference(type=common_pb2.ObjectType(namespace="rbac", name="group"), id="12345")
        )

        mock_response_2 = lookup_pb2.LookupResourcesResponse(
            resource=common_pb2.ObjectReference(type=common_pb2.ObjectType(namespace="rbac", name="group"), id="67891")
        )

        mock_stub.LookupResources.return_value = [mock_response_1, mock_response_2]

        with patch("internal.views.lookup_pb2_grpc.KesselLookupServiceStub", return_value=mock_stub):

            request_body = {
                "resource_type": {"name": "group", "namespace": "rbac"},
                "relation": "member",
                "subject": {
                    "subject": {
                        "type": {"namespace": "rbac", "name": "principal"},
                        "id": "123adf3-wads1224-ascwqe31-asasu333-333",
                    }
                },
            }

            response = self.client.post(
                f"/_private/api/relations/lookup_resource/",
                request_body,
                format="json",
                **self.request.META,
            )

            response_body = json.loads(response.content)
            resource_1 = response_body["resources"][0]["resource"]
            resource_2 = response_body["resources"][1]["resource"]
            self.assertEqual(response.status_code, 200)
            self.assertIn("resources", response_body)
            self.assertEqual(resource_1["id"], "12345")
            self.assertEqual(resource_1["type"]["namespace"], "rbac")
            self.assertEqual(resource_1["type"]["name"], "group")
            self.assertEqual(resource_2["id"], "67891")
            self.assertEqual(resource_2["type"]["namespace"], "rbac")
            self.assertEqual(resource_2["type"]["name"], "group")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_lookup_resources_empty(self, mock_create_channel, mock_get_token):
        """Test a request to lookup_resource endpoint returns the correct response when no resources are found."""

        mock_stub = MagicMock()

        mock_stub.LookupResources.return_value = []

        with patch("internal.views.lookup_pb2_grpc.KesselLookupServiceStub", return_value=mock_stub):

            request_body = {
                "resource_type": {"name": "group", "namespace": "rbac"},
                "relation": "member",
                "subject": {
                    "subject": {
                        "type": {"namespace": "rbac", "name": "principal"},
                        "id": "123adf3-wads1224-ascwqe31-asasu333-333",
                    }
                },
            }

            response = self.client.post(
                f"/_private/api/relations/lookup_resource/",
                request_body,
                format="json",
                **self.request.META,
            )
            self.assertEqual(response.status_code, 204)

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel", side_effect=RpcError("Simulated GRPC error"))
    def test_lookup_resources_grpc_error(self, mock_channel, mock_token):
        """Test a request to lookup_resource endpoint returns the correct response in case of grpc error."""

        request_body = {
            "resource_type": {"name": "group", "namespace": "rbac"},
            "relation": "member",
            "subject": {
                "subject": {
                    "type": {"namespace": "rbac", "name": "principal"},
                    "id": "123adf3-wads1224-ascwqe31-asasu333-333",
                }
            },
        }

        response = self.client.post(
            f"/_private/api/relations/lookup_resource/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in gRPC call")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch("internal.views.create_client_channel", side_effect=Exception("Simulated internal error"))
    def test_lookup_resources_error(self, mock_channel):
        """Test a request to lookup_resource endpoint returns the correct response in case of an error."""

        request_body = {
            "resource_type": {"name": "group", "namespace": "rbac"},
            "relation": "member",
            "subject": {
                "subject": {
                    "type": {"namespace": "rbac", "name": "principal"},
                    "id": "123adf3-wads1224-ascwqe31-asasu333-333",
                }
            },
        }

        response = self.client.post(
            f"/_private/api/relations/lookup_resource/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in call to lookup resources endpoint")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_lookup_resources_invalid_body(self, mock_create_channel, mock_get_token):
        """Test a request to lookup_resource endpoint returns the correct response in case of input validation failure."""
        request_body = {
            "invalid field": {"name": "group", "namespace": "rbac"},
            "relation": "member",
            "subject": {
                "subject": {
                    "type": {"namespace": "rbac", "name": "principal"},
                    "id": "123adf3-wads1224-ascwqe31-asasu333-333",
                }
            },
        }

        response = self.client.post(
            f"/_private/api/relations/lookup_resource/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response_body["detail"], "Invalid request body provided in request to lookup_resources.")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_check_relation(self, mock_create_channel, mock_get_token):
        """Test a request to check_relation endpoint returns the correct response."""

        mock_stub = MagicMock()
        mock_response_1 = check_pb2.CheckResponse(allowed=True)

        mock_stub.Check.return_value = mock_response_1

        with patch("internal.views.check_pb2_grpc.KesselCheckServiceStub", return_value=mock_stub):

            request_body = {
                "resource": {
                    "type": {"name": "role_binding", "namespace": "rbac"},
                    "id": "1234-5677-234235-234560",
                },
                "relation": "subject",
                "subject": {
                    "relation": "member",
                    "subject": {
                        "type": {"namespace": "rbac", "name": "group"},
                        "id": "9479873-2345643-123523-213253",
                    },
                },
            }

            response = self.client.post(
                f"/_private/api/relations/check_relation/",
                request_body,
                format="json",
                **self.request.META,
            )

            response_body = json.loads(response.content)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response_body["allowed"], True)

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_check_relation_empty(self, mock_create_channel, mock_get_token):
        """Test a request to check_relation endpoint returns the correct response when no relation is found."""

        mock_stub = MagicMock()

        mock_stub.Check.return_value = []

        with patch("internal.views.check_pb2_grpc.KesselCheckServiceStub", return_value=mock_stub):

            request_body = {
                "resource": {
                    "type": {"name": "role_binding", "namespace": "rbac"},
                    "id": "1234-5677-234235-234560",
                },
                "relation": "subject",
                "subject": {
                    "relation": "member",
                    "subject": {
                        "type": {"namespace": "rbac", "name": "group"},
                        "id": "9479873-2345643-123523-213253",
                    },
                },
            }

            response = self.client.post(
                f"/_private/api/relations/check_relation/",
                request_body,
                format="json",
                **self.request.META,
            )
            self.assertEqual(response.status_code, 204)

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel", side_effect=RpcError("Simulated GRPC error"))
    def test_check_relation_grpc_error(self, mock_channel, mock_token):
        """Test a request to check_relation endpoint returns the correct response in case of grpc error."""

        request_body = {
            "resource": {
                "type": {"name": "role_binding", "namespace": "rbac"},
                "id": "1234-5677-234235-234560",
            },
            "relation": "subject",
            "subject": {
                "relation": "member",
                "subject": {
                    "type": {"namespace": "rbac", "name": "group"},
                    "id": "9479873-2345643-123523-213253",
                },
            },
        }

        response = self.client.post(
            f"/_private/api/relations/check_relation/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in gRPC call")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch("internal.views.create_client_channel", side_effect=Exception("Simulated internal error"))
    def test_check_relation_error(self, mock_channel):
        """Test a request to check_relation endpoint returns the correct response in case of an error."""

        request_body = {
            "resource": {
                "type": {"name": "role_binding", "namespace": "rbac"},
                "id": "1234-5677-234235-234560",
            },
            "relation": "subject",
            "subject": {
                "relation": "member",
                "subject": {
                    "type": {"namespace": "rbac", "name": "group"},
                    "id": "9479873-2345643-123523-213253",
                },
            },
        }

        response = self.client.post(
            f"/_private/api/relations/check_relation/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in call to check relation endpoint")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_check_relation_invalid_body(self, mock_create_channel, mock_get_token):
        """Test a request to check_relation endpoint returns the correct response in case of input validation failure."""

        request_body = {
            "invalid_resource": {
                "type": {"name": "role_binding", "namespace": "rbac"},
                "id": "1234-5677-234235-234560",
            },
            "relation": "subject",
            "invalid_subject": {
                "relation": "member",
                "subject": {
                    "type": {"namespace": "rbac", "name": "group"},
                    "id": "9479873-2345643-123523-213253",
                },
            },
        }

        response = self.client.post(
            f"/_private/api/relations/check_relation/",
            request_body,
            format="json",
            **self.request.META,
        )

        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response_body["detail"], "Invalid request body provided in request to check_relation.")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_read_tuples(self, mock_create_channel, mock_get_token):
        """Test a request to read_tuples endpoint returns the correct response."""

        mock_stub = MagicMock()
        mock_response_1 = relation_tuples_pb2.ReadTuplesResponse(
            tuple=common_pb2.Relationship(
                resource=common_pb2.ObjectReference(
                    type=common_pb2.ObjectType(namespace="rbac", name="group"),
                    id="a5d1234-d3307-4123453-a12314sdf-4asdzxccq82",
                ),
                relation="member",
                subject=common_pb2.SubjectReference(
                    subject=common_pb2.ObjectReference(
                        type=common_pb2.ObjectType(namespace="rbac", name="principal"), id="test-213213-principal"
                    )
                ),
            ),
            pagination=common_pb2.ResponsePagination(
                continuation_token="asfhdsfuygsdufkysagdfiwesudfyd192837102937sdiufgsjkahdfd=="
            ),
        )

        mock_stub.ReadTuples.return_value = [mock_response_1]

        with patch("internal.views.relation_tuples_pb2_grpc.KesselTupleServiceStub", return_value=mock_stub):

            request_body = {
                "filter": {
                    "resource_id": "bob_club",
                    "resource_type": "group",
                    "resource_namespace": "rbac",
                    "relation": "member",
                    "subject_filter": {
                        "subject_type": "principal",
                        "subject_namespace": "rbac",
                        "subject_id": "bob",
                    },
                }
            }

            response = self.client.post(
                f"/_private/api/relations/read_tuples/",
                request_body,
                format="json",
                **self.request.META,
            )

            response_body = json.loads(response.content)
            response_tuple = response_body["tuples"][0]["tuple"]
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response_tuple["resource"]["type"]["namespace"], "rbac")
            self.assertEqual(response_tuple["resource"]["type"]["name"], "group")
            self.assertEqual(response_tuple["resource"]["id"], "a5d1234-d3307-4123453-a12314sdf-4asdzxccq82")
            self.assertEqual(response_tuple["relation"], "member")
            self.assertEqual(response_tuple["subject"]["subject"]["type"]["namespace"], "rbac")
            self.assertEqual(response_tuple["subject"]["subject"]["type"]["name"], "principal")
            self.assertEqual(response_tuple["subject"]["subject"]["id"], "test-213213-principal")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_read_tuples_empty(self, mock_create_channel, mock_get_token):
        """Test a request to read_tuples endpoint returns the correct response when no tuples are found."""

        mock_stub = MagicMock()

        mock_stub.ReadTuples.return_value = []

        with patch("internal.views.relation_tuples_pb2_grpc.KesselTupleServiceStub", return_value=mock_stub):

            request_body = {
                "filter": {
                    "resource_id": "bob_club",
                    "resource_type": "group",
                    "resource_namespace": "rbac",
                    "relation": "member",
                    "subject_filter": {
                        "subject_type": "principal",
                        "subject_namespace": "rbac",
                        "subject_id": "bob",
                    },
                }
            }

            response = self.client.post(
                f"/_private/api/relations/read_tuples/",
                request_body,
                format="json",
                **self.request.META,
            )

            self.assertEqual(response.status_code, 204)

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel", side_effect=RpcError("Simulated GRPC error"))
    def test_read_tuples_grpc_error(self, mock_channel, mock_token):
        """Test a request to read_tuples endpoint returns the correct response in case of grpc error."""

        request_body = {
            "filter": {
                "resource_id": "bob_club",
                "resource_type": "group",
                "resource_namespace": "rbac",
                "relation": "member",
                "subject_filter": {
                    "subject_type": "principal",
                    "subject_namespace": "rbac",
                    "subject_id": "bob",
                },
            }
        }

        response = self.client.post(
            f"/_private/api/relations/read_tuples/",
            request_body,
            format="json",
            **self.request.META,
        )

        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in gRPC call")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch("internal.views.create_client_channel", side_effect=Exception("Simulated internal error"))
    def test_read_tuples_error(self, mock_channel):
        """Test a request to read_tuples endpoint returns the correct response in case of an error."""

        request_body = {
            "filter": {
                "resource_id": "bob_club",
                "resource_type": "group",
                "resource_namespace": "rbac",
                "relation": "member",
                "subject_filter": {
                    "subject_type": "principal",
                    "subject_namespace": "rbac",
                    "subject_id": "bob",
                },
            }
        }

        response = self.client.post(
            f"/_private/api/relations/read_tuples/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in call to read tuples endpoint")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_read_tuples_invalid_body(self, mock_create_channel, mock_get_token):
        """Test a request to read_tuples endpoint returns the correct response in case of input validation failure."""

        request_body = {
            "invalid_filter": {
                "resource_id": "bob_club",
                "resource_type": "group",
                "resource_namespace": "rbac",
                "relation": "member",
                "invalid_subject_filter": {
                    "subject_type": "principal",
                    "subject_namespace": "rbac",
                    "subject_id": "bob",
                },
            }
        }

        response = self.client.post(
            f"/_private/api/relations/read_tuples/",
            request_body,
            format="json",
            **self.request.META,
        )

        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response_body["detail"], "Invalid request body provided in request to read_tuples.")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_lookup_subjects(self, mock_create_channel, mock_get_token):
        """Test a request to lookup_subjects endpoint returns the correct response."""

        mock_stub = MagicMock()
        mock_response_1 = lookup_pb2.LookupSubjectsResponse(
            subject=common_pb2.SubjectReference(
                subject=common_pb2.ObjectReference(
                    type=common_pb2.ObjectType(namespace="rbac", name="principal"), id="user-12345"
                )
            )
        )

        mock_response_2 = lookup_pb2.LookupSubjectsResponse(
            subject=common_pb2.SubjectReference(
                subject=common_pb2.ObjectReference(
                    type=common_pb2.ObjectType(namespace="rbac", name="principal"), id="user-67891"
                )
            )
        )

        mock_stub.LookupSubjects.return_value = [mock_response_1, mock_response_2]

        with patch("internal.views.lookup_pb2_grpc.KesselLookupServiceStub", return_value=mock_stub):

            request_body = {
                "resource": {
                    "type": {"namespace": "rbac", "name": "workspace"},
                    "id": "workspace-123",
                },
                "relation": "user_grant",
                "subject_type": {"namespace": "rbac", "name": "principal"},
            }

            response = self.client.post(
                "/_private/api/relations/lookup_subjects/",
                request_body,
                format="json",
                **self.request.META,
            )

            response_body = json.loads(response.content)
            subject_1 = response_body["subjects"][0]["subject"]["subject"]
            subject_2 = response_body["subjects"][1]["subject"]["subject"]
            self.assertEqual(response.status_code, 200)
            self.assertIn("subjects", response_body)
            self.assertEqual(subject_1["id"], "user-12345")
            self.assertEqual(subject_1["type"]["namespace"], "rbac")
            self.assertEqual(subject_1["type"]["name"], "principal")
            self.assertEqual(subject_2["id"], "user-67891")
            self.assertEqual(subject_2["type"]["namespace"], "rbac")
            self.assertEqual(subject_2["type"]["name"], "principal")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_lookup_subjects_with_subject_relation(self, mock_create_channel, mock_get_token):
        """Test a request to lookup_subjects endpoint with optional subject_relation parameter."""

        mock_stub = MagicMock()
        mock_response = lookup_pb2.LookupSubjectsResponse(
            subject=common_pb2.SubjectReference(
                relation="member",
                subject=common_pb2.ObjectReference(
                    type=common_pb2.ObjectType(namespace="rbac", name="group"), id="group-12345"
                ),
            )
        )

        mock_stub.LookupSubjects.return_value = [mock_response]

        with patch("internal.views.lookup_pb2_grpc.KesselLookupServiceStub", return_value=mock_stub):

            request_body = {
                "resource": {
                    "type": {"namespace": "rbac", "name": "role_binding"},
                    "id": "binding-123",
                },
                "relation": "subject",
                "subject_type": {"namespace": "rbac", "name": "group"},
                "subject_relation": "member",
            }

            response = self.client.post(
                "/_private/api/relations/lookup_subjects/",
                request_body,
                format="json",
                **self.request.META,
            )

            response_body = json.loads(response.content)
            self.assertEqual(response.status_code, 200)
            self.assertIn("subjects", response_body)
            subject = response_body["subjects"][0]["subject"]
            self.assertEqual(subject["relation"], "member")
            self.assertEqual(subject["subject"]["id"], "group-12345")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_lookup_subjects_empty(self, mock_create_channel, mock_get_token):
        """Test a request to lookup_subjects endpoint returns the correct response when no subjects are found."""

        mock_stub = MagicMock()

        mock_stub.LookupSubjects.return_value = []

        with patch("internal.views.lookup_pb2_grpc.KesselLookupServiceStub", return_value=mock_stub):

            request_body = {
                "resource": {
                    "type": {"namespace": "rbac", "name": "workspace"},
                    "id": "workspace-123",
                },
                "relation": "user_grant",
                "subject_type": {"namespace": "rbac", "name": "principal"},
            }

            response = self.client.post(
                "/_private/api/relations/lookup_subjects/",
                request_body,
                format="json",
                **self.request.META,
            )
            self.assertEqual(response.status_code, 204)

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel", side_effect=RpcError("Simulated GRPC error"))
    def test_lookup_subjects_grpc_error(self, mock_channel, mock_token):
        """Test a request to lookup_subjects endpoint returns the correct response in case of grpc error."""

        request_body = {
            "resource": {
                "type": {"namespace": "rbac", "name": "workspace"},
                "id": "workspace-123",
            },
            "relation": "user_grant",
            "subject_type": {"namespace": "rbac", "name": "principal"},
        }

        response = self.client.post(
            "/_private/api/relations/lookup_subjects/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in gRPC call")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch("internal.views.create_client_channel", side_effect=Exception("Simulated internal error"))
    def test_lookup_subjects_error(self, mock_channel):
        """Test a request to lookup_subjects endpoint returns the correct response in case of an error."""

        request_body = {
            "resource": {
                "type": {"namespace": "rbac", "name": "workspace"},
                "id": "workspace-123",
            },
            "relation": "user_grant",
            "subject_type": {"namespace": "rbac", "name": "principal"},
        }

        response = self.client.post(
            "/_private/api/relations/lookup_subjects/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in call to lookup subjects endpoint")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("internal.views.create_client_channel")
    def test_lookup_subjects_invalid_body(self, mock_create_channel, mock_get_token):
        """Test a request to lookup_subjects endpoint returns the correct response in case of input validation failure."""
        request_body = {
            "invalid_resource": {
                "type": {"namespace": "rbac", "name": "workspace"},
                "id": "workspace-123",
            },
            "relation": "user_grant",
            "subject_type": {"namespace": "rbac", "name": "principal"},
        }

        response = self.client.post(
            "/_private/api/relations/lookup_subjects/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response_body["detail"], "Invalid request body provided in request to lookup_subjects.")


class InternalInventoryViewsetTests(BaseInternalViewsetTests):
    """Test the /_private/api/inventory/ endpoints from internal viewset."""

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel")
    def test_check_inventory(self, mock_create_channel, mock_get_token):
        """Test a request to check inventory endpoint returns the correct response."""

        mock_stub = MagicMock()
        mock_response_1 = check_response_pb2.CheckResponse(allowed=allowed_pb2.Allowed.ALLOWED_TRUE)

        mock_stub.Check.return_value = mock_response_1

        with patch("internal.views.inventory_service_pb2_grpc.KesselInventoryServiceStub", return_value=mock_stub):

            request_body = {
                "resource": {"resource_id": "bob_club", "resource_type": "group", "reporter": {"type": "rbac"}},
                "relation": "member",
                "subject": {
                    "resource": {"resource_id": "bob", "resource_type": "principal", "reporter": {"type": "rbac"}}
                },
            }

            response = self.client.post(
                f"/_private/api/inventory/check/",
                request_body,
                format="json",
                **self.request.META,
            )

            response_body = json.loads(response.content)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response_body["allowed"], True)

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel", side_effect=RpcError("Simulated GRPC error"))
    def test_check_inventory_grpc_error(self, mock_channel, mock_token):
        """Test a request to check inventory endpoint returns the correct response in case of grpc error."""

        request_body = {
            "resource": {"resource_id": "bob_club", "resource_type": "group", "reporter": {"type": "rbac"}},
            "relation": "member",
            "subject": {
                "resource": {"resource_id": "bob", "resource_type": "principal", "reporter": {"type": "rbac"}}
            },
        }

        response = self.client.post(
            f"/_private/api/inventory/check/",
            request_body,
            format="json",
            **self.request.META,
        )

        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in gRPC call")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch("internal.utils.create_client_channel", side_effect=Exception("Simulated internal error"))
    def test_check_inventory_error(self, mock_channel):
        """Test a request to check inventory endpoint returns the correct response in case of an error."""

        request_body = {
            "resource": {"resource_id": "bob_club", "resource_type": "group", "reporter": {"type": "rbac"}},
            "relation": "member",
            "subject": {
                "resource": {"resource_id": "bob", "resource_type": "principal", "reporter": {"type": "rbac"}}
            },
        }

        response = self.client.post(
            f"/_private/api/inventory/check/",
            request_body,
            format="json",
            **self.request.META,
        )
        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Error occurred in call to check inventory endpoint")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel")
    def test_check_inventory_invalid_body(self, mock_create_channel, mock_get_token):
        """Test a request to check inventory endpoint returns the correct response in case of input validation failure."""

        request_body = {
            "invalid_field": {"resource_id": "bob_club", "resource_type": "group", "reporter": {"type": "rbac"}},
            "relation": "member",
            "subject": {
                "resource": {"resource_id": "bob", "resource_type": "principal", "reporter": {"type": "rbac"}}
            },
        }

        response = self.client.post(
            f"/_private/api/inventory/check/",
            request_body,
            format="json",
            **self.request.META,
        )

        response_body = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response_body["detail"], "Invalid request body provided in request to check inventory.")


class InternalInventoryViewsetTests(BaseInternalViewsetTests):
    """Test the /_private/api/inventory/ endpoints from internal viewset."""

    def setUp(self):
        super().setUp()

        bootstrap_result = bootstrap_tenant_for_v2_test(tenant=self.tenant)

        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel")
    @patch("management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships")
    def test_inventory_group_assignments(self, mock_check_relationships, mock_create_channel, mock_get_token):
        """Test a request to check group assignments of inventory returns correct response."""
        group_uuid = str(self.group.uuid)

        mock_stub = MagicMock()
        mock_stub.Check.return_value = check_response_pb2.CheckResponse(allowed=allowed_pb2.Allowed.ALLOWED_TRUE)

        mock_check_relationships.return_value = {
            "group_uuid": group_uuid,
            "principal_relations": [{"id": "213072139", "relation_exists": True}],
        }

        with patch(
            "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            response = self.client.get(
                f"/_private/api/inventory/group_assignments/{group_uuid}/",
                format="json",
                **self.request.META,
            )

        # Parse and validate response
        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertEqual(response_body["group_uuid"], group_uuid)
        self.assertIn("principal_relations", response_body)
        self.assertEqual(response_body["principal_relations"], [{"id": "213072139", "relation_exists": True}])

    @patch(
        "management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships",
        side_effect=RpcError("Simulated GRPC error"),
    )
    def test_inventory_group_assignments_grpc_error(self, mock_check_relationships):
        """Test the expected grpc error is returned in cases of grpc error for group assignments check"""
        group_uuid = str(self.group.uuid)

        response = self.client.get(
            f"/_private/api/inventory/group_assignments/{group_uuid}/",
            format="json",
            **self.request.META,
        )
        self.assertEqual(response.status_code, 400)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "gRPC error occurred during inventory group assignment check")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch(
        "management.inventory_checker.inventory_api_check.GroupPrincipalInventoryChecker.check_relationships",
        side_effect=Exception("Simulated internal error"),
    )
    def test_inventory_group_assignments_error(self, mock_check_relationships):
        """Test the expected error is returned in cases of unexpected error for group assignments check"""
        group_uuid = str(self.group.uuid)

        response = self.client.get(
            f"/_private/api/inventory/group_assignments/{group_uuid}/",
            format="json",
            **self.request.META,
        )
        self.assertEqual(response.status_code, 500)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Unexpected error during inventory group assignment check")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel")
    @patch(
        "management.inventory_checker.inventory_api_check.BootstrappedTenantInventoryChecker.check_bootstrapped_tenant"
    )
    def test_inventory_bootstrapped_tenants(self, mock_check_bootstrapped_tenant, mock_create_channel, mock_get_token):
        """Test a request to check bootstrapped tenants on inventory returns correct response."""
        # Mock returns tuple of (bool, list of check dicts)
        mock_relations_checked = [
            {
                "name": "default_workspace_parent",
                "check": "rbac/workspace:ws-123#parent@rbac/workspace:ws-456",
                "exists": True,
            },
            {
                "name": "root_workspace_parent",
                "check": "rbac/workspace:ws-456#parent@rbac/tenant:localhost/org",
                "exists": True,
            },
        ]
        mock_check_bootstrapped_tenant.return_value = (True, mock_relations_checked)

        response = self.client.get(
            f"/_private/api/inventory/bootstrap_tenants/{self.tenant.org_id}/",
            format="json",
            **self.request.META,
        )

        # Parse and validate response
        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertEqual(response_body["org_id"], str(self.tenant.org_id))
        self.assertTrue(response_body["bootstrapped_correct"])
        self.assertIn("relations_checked", response_body)
        self.assertEqual(response_body["relations_checked"], mock_relations_checked)

    @patch(
        "management.inventory_checker.inventory_api_check.BootstrappedTenantInventoryChecker.check_bootstrapped_tenant",
        side_effect=RpcError("Simulated GRPC error"),
    )
    def test_inventory_bootstrapped_tenants_grpc_error(self, mock_check_relationships):
        """Test the expected grpc error is returned in cases of grpc error for bootstrapped tenant check"""
        # Create the required objects for this test
        response = self.client.get(
            f"/_private/api/inventory/bootstrap_tenants/{self.tenant.org_id}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 400)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "gRPC error occurred during inventory bootstrapped tenant check")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch(
        "management.inventory_checker.inventory_api_check.BootstrappedTenantInventoryChecker.check_bootstrapped_tenant",
        side_effect=Exception("Simulated internal error"),
    )
    def test_inventory_bootstrapped_tenants_error(self, mock_check_relationships):
        """Test the expected error is returned in cases of generic error for bootstrapped tenant check"""
        # Create the required objects for this test
        response = self.client.get(
            f"/_private/api/inventory/bootstrap_tenants/{self.tenant.org_id}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 500)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Unexpected error during inventory bootstrapped tenant check")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel")
    @patch("management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace")
    @patch("internal.views.check_workspace_relation")
    def test_inventory_workspace_relation(
        self, mock_workspace_view, mock_check_workspace_relation, mock_create_channel, mock_get_token
    ):
        """Test a request to check workspace relation on inventory returns correct response."""
        # Create the required objects for this test
        self.test_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            name="Test Workspace",
            description="Default Description",
            parent_id=self.default_workspace.id,
        )
        mock_stub = MagicMock()
        mock_stub.Check.return_value = "true"

        mock_check_workspace_relation.return_value = mock_stub.Check.return_value
        mock_workspace_view.return_value = {
            "org_id": self.test_workspace.tenant.org_id,
            "workspace_id": self.test_workspace.id,
            "workspace_parent_id": self.test_workspace.parent.id,
            "workspace_relation_correct": mock_stub.Check.return_value,
        }
        with patch(
            "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            response = self.client.get(
                f"/_private/api/inventory/check_workspace/{str(self.test_workspace.id)}/",
                format="json",
                **self.request.META,
            )

        # Parse and validate response
        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertEqual(response_body["org_id"], self.test_workspace.tenant.org_id)
        self.assertEqual(response_body["workspace_id"], str(self.test_workspace.id))
        self.assertEqual(response_body["workspace_parent_id"], str(self.test_workspace.parent.id))
        self.assertEqual(response_body["workspace_relation_correct"], "true")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel")
    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants"
    )
    @patch("internal.views.check_workspace_relation")
    def test_inventory_workspace_descendants_relation(
        self, mock_workspace_view, mock_check_workspace_relation, mock_create_channel, mock_get_token
    ):
        """Test a request to check workspace relation descendants on inventory returns correct response."""
        self.test_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            name="Test Workspace",
            description="Default Description",
            parent_id=self.default_workspace.id,
        )
        # Create a descendant of test_workspace
        self.descendant_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            name="Descendant Workspace",
            description="Descendant Description",
            parent_id=self.default_workspace.id,
        )
        mock_stub = MagicMock()
        mock_stub.Check.return_value = "true"

        mock_check_workspace_relation.return_value = mock_stub.Check.return_value
        mock_workspace_view.return_value = (
            {
                "org_id": self.root_workspace.tenant.org_id,
                "workspace_id": str(self.root_workspace.id),
                "workspace_relation_correct": mock_stub.Check.return_value,
            },
        )

        with patch(
            "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            response = self.client.get(
                f"/_private/api/inventory/check_workspace/{str(self.root_workspace.id)}/?descendants=true",
                format="json",
                **self.request.META,
            )

        # Parse and validate response
        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertIsInstance(response_body, dict)

        # Check response
        self.assertEqual(response_body["org_id"], self.root_workspace.tenant.org_id)
        self.assertEqual(response_body["workspace_id"], str(self.root_workspace.id))
        self.assertEqual(response_body["workspace_descendants_correct"], "true")

    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace",
        side_effect=RpcError("Simulated GRPC error"),
    )
    def test_inventory_workspace_relation_grpc_error(self, mock_check_workspace):
        """Test the expected grpc error is returned in cases of grpc error for workspace relation check"""
        # Create the required objects for this test
        self.test_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            name="Test Workspace",
            description="Default Description",
            parent_id=self.default_workspace.id,
        )

        response = self.client.get(
            f"/_private/api/inventory/check_workspace/{str(self.test_workspace.id)}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 400)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "gRPC error occurred during inventory workspace relation check")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace",
        side_effect=Exception("Simulated internal error"),
    )
    def test_inventory_workspace_relation_error(self, mock_check_workspace):
        """Test the expected error is returned in cases of generic error for workspace relation check"""
        # Create the required objects for this test
        self.test_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            name="Test Workspace",
            description="Default Description",
            parent_id=self.default_workspace.id,
        )

        response = self.client.get(
            f"/_private/api/inventory/check_workspace/{str(self.test_workspace.id)}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 500)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Unexpected error during inventory workspace relation check")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace",
        side_effect=Exception("Simulated internal error"),
    )
    def test_inventory_workspace_root_error(self, mock_check_workspace):
        """Test the expected error is returned in cases of a root workspace uuid being provided for workspace relation check"""
        # Create the required objects for this test
        self.test_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            name="Test Workspace",
            description="Default Description",
            parent_id=self.default_workspace.id,
        )

        response = self.client.get(
            f"/_private/api/inventory/check_workspace/{str(self.root_workspace.id)}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 400)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertEqual(
            response_body["detail"],
            "Root workspace provided — this is not a valid input as it does not have a parent workspace. Request skipped.",
        )

    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants",
        side_effect=RpcError("Simulated GRPC error"),
    )
    def test_inventory_workspace_descendants_relation_grpc_error(self, mock_check_workspace):
        """Test the expected grpc error is returned in cases of grpc error for workspace relation descendants check"""
        # Create the required objects for this test
        self.test_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            name="Test Workspace",
            description="Default Description",
            parent_id=self.default_workspace.id,
        )

        response = self.client.get(
            f"/_private/api/inventory/check_workspace/{str(self.root_workspace.id)}/?descendants=true",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 400)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(
            response_body["detail"], "gRPC error occurred during inventory workspace descendants relation check"
        )
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch(
        "management.inventory_checker.inventory_api_check.WorkspaceRelationInventoryChecker.check_workspace_descendants",
        side_effect=Exception("Simulated internal error"),
    )
    def test_inventory_workspace_descendants_relation_error(self, mock_check_workspace):
        """Test the expected error is returned in cases of generic error for workspace relation descendants check"""
        # Create the required objects for this test
        self.test_workspace = Workspace.objects.create(
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            name="Test Workspace",
            description="Default Description",
            parent_id=self.default_workspace.id,
        )

        response = self.client.get(
            f"/_private/api/inventory/check_workspace/{str(self.root_workspace.id)}/?descendants=true",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 500)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(
            response_body["detail"], "Unexpected error during inventory workspace descendants relation check"
        )
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel")
    @patch("management.inventory_checker.inventory_api_check.RoleRelationInventoryChecker.check_role")
    @patch("internal.views.check_role")
    def test_inventory_check_role(self, mock_bootstrapped_view, mock_check_role, mock_create_channel, mock_get_token):
        """Test a request to check role endpoint on inventory returns correct response."""
        mock_stub = MagicMock()
        mock_stub.Check.return_value = True

        mock_check_role.return_value = mock_stub.Check.return_value
        mock_bootstrapped_view.return_value = {
            "V2_role_checks": {
                "v1_role_uuid": self.role.uuid,
                "v1_role_name": self.role.name,
                "V2_role_relations_correct": mock_check_role.return_value,
            }
        }

        # Create this so that we can check that BindingMappings aren't accidentally created. (A previous implementation
        # incorrectly used RelationApiDualWriteHandler for system roles.)
        Access.objects.create(
            tenant=self.public_tenant,
            role=self.role,
            permission=Permission.objects.create(
                tenant=self.public_tenant,
                permission="app:resource:verb",
            ),
        )

        BindingMapping.objects.all().delete()

        with patch(
            "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            response = self.client.get(
                f"/_private/api/inventory/check_role/{self.role.uuid}/",
                format="json",
                **self.request.META,
            )

        # Parse and validate response
        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertIn("V2_role_checks", response_body)
        self.assertEqual(response_body["V2_role_checks"]["v1_role_uuid"], str(self.role.uuid))
        self.assertEqual(response_body["V2_role_checks"]["v1_role_name"], str(self.role.name))
        self.assertEqual(response_body["V2_role_checks"]["V2_role_relations_correct"], True)

        self.assertEqual(BindingMapping.objects.all().count(), 0)

    @patch("internal.jwt_utils.JWTProvider.get_jwt_token", return_value={"access_token": "mocked_valid_token"})
    @patch("management.utils.create_client_channel")
    @patch("management.inventory_checker.inventory_api_check.RoleRelationInventoryChecker.check_role")
    @patch("internal.views.check_role")
    def test_inventory_check_custom_role(
        self, mock_bootstrapped_view, mock_check_role, mock_create_channel, mock_get_token
    ):
        """Test a request to check role endpoint on inventory returns correct response."""
        custom_role = Role.objects.create(
            tenant=self.tenant,
            name="Custom role",
        )

        mock_stub = MagicMock()
        mock_stub.Check.return_value = True

        mock_check_role.return_value = mock_stub.Check.return_value
        mock_bootstrapped_view.return_value = {
            "V2_role_checks": {
                "v1_role_uuid": custom_role.uuid,
                "v1_role_name": custom_role.name,
                "V2_role_relations_correct": mock_check_role.return_value,
            }
        }

        # Create this so that we can check that BindingMappings aren't accidentally created.
        Access.objects.create(
            tenant=self.tenant,
            role=custom_role,
            permission=Permission.objects.create(
                tenant=self.public_tenant,
                permission="app:resource:verb",
            ),
        )

        BindingMapping.objects.all().delete()

        with patch(
            "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            response = self.client.get(
                f"/_private/api/inventory/check_role/{custom_role.uuid}/",
                format="json",
                **self.request.META,
            )

        # Parse and validate response
        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertIn("V2_role_checks", response_body)
        self.assertEqual(response_body["V2_role_checks"]["v1_role_uuid"], str(custom_role.uuid))
        self.assertEqual(response_body["V2_role_checks"]["v1_role_name"], str(custom_role.name))
        self.assertEqual(response_body["V2_role_checks"]["V2_role_relations_correct"], True)

        self.assertEqual(BindingMapping.objects.all().count(), 0)

    @patch(
        "management.inventory_checker.inventory_api_check.RoleRelationInventoryChecker.check_role",
        side_effect=RpcError("Simulated GRPC error"),
    )
    def test_inventory_check_role_grpc_error(self, mock_check_role):
        """Test a request to check role endpoint on inventory returns correct response in case of grpc error."""
        with patch(
            "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub"
        ):
            response = self.client.get(
                f"/_private/api/inventory/check_role/{self.role.uuid}/",
                format="json",
                **self.request.META,
            )

        # Parse and validate response
        self.assertEqual(response.status_code, 400)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "gRPC error occurred during inventory role relation check")
        self.assertEqual(response_body["error"], "Simulated GRPC error")

    @patch(
        "management.inventory_checker.inventory_api_check.RoleRelationInventoryChecker.check_role",
        side_effect=Exception("Simulated internal error"),
    )
    def test_inventory_check_role_error(self, mock_check_role):
        """Test a request to check role endpoint on inventory returns correct response in case of generic error."""
        with patch(
            "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub"
        ):
            response = self.client.get(
                f"/_private/api/inventory/check_role/{self.role.uuid}/",
                format="json",
                **self.request.META,
            )

        # Parse and validate response
        self.assertEqual(response.status_code, 500)
        response_body = response.json()

        self.assertIn("detail", response_body)
        self.assertIn("error", response_body)
        self.assertEqual(response_body["detail"], "Unexpected error occurred during inventory role relation check")
        self.assertEqual(response_body["error"], "Simulated internal error")

    @patch(
        "internal.views.RelationApiDualWriteCrossAccessHandler",
    )
    @patch(
        "management.inventory_checker.inventory_api_check"
        ".CrossAccountRequestInventoryChecker.check_cross_account_request"
    )
    def test_inventory_check_cross_account_request(self, mock_check_car, mock_handler_cls):
        """Test check_cross_account_request returns correct response when all relations exist."""
        car = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="12345",
            status="approved",
            end_date=timezone.now() + timedelta(days=30),
        )
        car.roles.add(self.role)

        mock_handler = MagicMock()
        mock_handler_cls.return_value = mock_handler
        mock_check_car.return_value = True

        response = self.client.get(
            f"/_private/api/inventory/check_cross_account_request/{car.request_id}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertIn("cross_account_request_checks", response_body)
        checks = response_body["cross_account_request_checks"]
        self.assertEqual(checks["request_id"], str(car.request_id))
        self.assertEqual(checks["target_org"], self.tenant.org_id)
        self.assertEqual(checks["user_id"], "12345")
        self.assertEqual(checks["status"], "approved")
        self.assertTrue(checks["relations_correct"])
        self.assertEqual(len(checks["roles"]), 1)

    @patch(
        "internal.views.RelationApiDualWriteCrossAccessHandler",
    )
    @patch(
        "management.inventory_checker.inventory_api_check"
        ".CrossAccountRequestInventoryChecker.check_cross_account_request"
    )
    def test_inventory_check_cross_account_request_missing_relations(self, mock_check_car, mock_handler_cls):
        """Test check_cross_account_request returns False when relations are missing."""
        car = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="12345",
            status="approved",
            end_date=timezone.now() + timedelta(days=30),
        )
        car.roles.add(self.role)

        mock_handler = MagicMock()
        mock_handler_cls.return_value = mock_handler
        mock_check_car.return_value = False

        response = self.client.get(
            f"/_private/api/inventory/check_cross_account_request/{car.request_id}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertFalse(response_body["cross_account_request_checks"]["relations_correct"])

    def test_inventory_check_cross_account_request_not_approved(self):
        """Test check_cross_account_request returns 400 for non-approved CAR."""
        car = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="12345",
            status="pending",
            end_date=timezone.now() + timedelta(days=30),
        )

        response = self.client.get(
            f"/_private/api/inventory/check_cross_account_request/{car.request_id}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 400)
        response_body = response.json()
        self.assertIn("not approved", response_body["detail"])

    def test_inventory_check_cross_account_request_no_roles(self):
        """Test check_cross_account_request returns True when CAR has no roles."""
        car = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="12345",
            status="approved",
            end_date=timezone.now() + timedelta(days=30),
        )

        response = self.client.get(
            f"/_private/api/inventory/check_cross_account_request/{car.request_id}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        checks = response_body["cross_account_request_checks"]
        self.assertTrue(checks["relations_correct"])
        self.assertEqual(checks["roles"], [])

    def test_inventory_check_cross_account_request_not_found(self):
        """Test check_cross_account_request returns 404 for nonexistent request."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        response = self.client.get(
            f"/_private/api/inventory/check_cross_account_request/{fake_uuid}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 404)

    @patch(
        "internal.views.RelationApiDualWriteCrossAccessHandler",
        side_effect=RpcError("Simulated gRPC error"),
    )
    def test_inventory_check_cross_account_request_grpc_error(self, mock_handler_cls):
        """Test check_cross_account_request returns 400 on gRPC error."""
        car = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="12345",
            status="approved",
            end_date=timezone.now() + timedelta(days=30),
        )
        car.roles.add(self.role)

        response = self.client.get(
            f"/_private/api/inventory/check_cross_account_request/{car.request_id}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 400)
        response_body = response.json()
        self.assertIn("gRPC error", response_body["detail"])

    @patch(
        "internal.views.RelationApiDualWriteCrossAccessHandler",
        side_effect=Exception("Simulated internal error"),
    )
    def test_inventory_check_cross_account_request_unexpected_error(self, mock_handler_cls):
        """Test check_cross_account_request returns 500 on unexpected error."""
        car = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="12345",
            status="approved",
            end_date=timezone.now() + timedelta(days=30),
        )
        car.roles.add(self.role)

        response = self.client.get(
            f"/_private/api/inventory/check_cross_account_request/{car.request_id}/",
            format="json",
            **self.request.META,
        )

        self.assertEqual(response.status_code, 500)
        response_body = response.json()
        self.assertIn("Unexpected error", response_body["detail"])
