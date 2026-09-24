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
"""Test the GroupV2ViewSet."""

import uuid
from importlib import reload
from unittest.mock import patch

from django.db.models import ProtectedError
from django.test import override_settings
from django.urls import clear_url_caches, reverse
from rest_framework import status
from rest_framework.test import APIClient

from management import v2_urls
from management.audit_log.model import AuditLog
from management.group.model import Group
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.group.v2_service import GroupV2Service
from management.permissions.group_v2_access import GroupV2KesselAccessPermission
from management.policy.model import Policy
from management.principal.model import Principal
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import Role
from management.role.v2_model import CustomRoleV2
from management.role_binding.model import RoleBinding, RoleBindingGroup
from rbac import urls
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test

ACCESS_CHECK_TARGET = "management.permissions.group_v2_access.WorkspaceInventoryAccessChecker.check_resource_access"


@override_settings(V2_APIS_ENABLED=True, V2_EDIT_API_ENABLED=True, ATOMIC_RETRY_DISABLED=True)
class GroupV2ViewTestBase(IdentityRequest):
    """Shared setup for GroupV2ViewSet tests."""

    def setUp(self):
        """Set up test data."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        bootstrap_tenant_for_v2_test(self.tenant)
        self.client = APIClient()

        self.enterContext(
            patch(
                "management.permissions.group_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.mock_check_access = self.enterContext(patch(ACCESS_CHECK_TARGET, return_value=True))
        self.mock_dual_write = self.enterContext(patch("management.group.v2_service.RelationApiDualWriteGroupHandler"))

        self.user_1 = Principal.objects.create(username="user_1", tenant=self.tenant)
        self.user_2 = Principal.objects.create(username="user_2", tenant=self.tenant)
        self.service_account = Principal.objects.create(
            username="service-account-abc",
            service_account_id="abc",
            type=Principal.Types.SERVICE_ACCOUNT,
            tenant=self.tenant,
        )

        self.group_a = Group.objects.create(name="alpha", description="first", tenant=self.tenant)
        self.group_a.principals.add(self.user_1, self.user_2, self.service_account)
        self.group_b = Group.objects.create(name="beta", tenant=self.tenant)
        self.group_b.principals.add(self.user_1)

        self.role_1 = CustomRoleV2.objects.create(name="role_1", tenant=self.tenant)
        self.role_2 = CustomRoleV2.objects.create(name="role_2", tenant=self.tenant)

    def tearDown(self):
        """Tear down test data."""
        clear_url_caches()
        super().tearDown()

    def _bind(self, group, role, resource_id="ws-1"):
        binding = RoleBinding.objects.create(
            role=role, resource_type="workspace", resource_id=resource_id, tenant=self.tenant
        )
        RoleBindingGroup.objects.create(group=group, binding=binding)
        return binding

    def _list_url(self):
        return reverse("v2_management:groups-list")

    def _detail_url(self, group_uuid):
        return reverse("v2_management:groups-detail", kwargs={"uuid": str(group_uuid)})

    def _list(self, **params):
        return self.client.get(self._list_url(), params, **self.headers)

    def _names(self, response):
        return [g["name"] for g in response.json()["data"]]

    def _tenant_group_names(self, response):
        """Return names from the response that belong to groups created by this test (excludes bootstrap groups)."""
        own = set(Group.objects.filter(tenant=self.tenant).values_list("name", flat=True))
        return [n for n in self._names(response) if n in own]


class GroupV2ListViewTest(GroupV2ViewTestBase):
    """Tests for listing groups."""

    def test_list_returns_tenant_groups_with_counts(self):
        """Groups are listed with user-only principal counts and distinct role counts."""
        self._bind(self.group_a, self.role_1, "ws-1")
        self._bind(self.group_a, self.role_1, "ws-2")
        self._bind(self.group_a, self.role_2, "ws-1")

        response = self._list(name="alpha")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(len(data), 1)
        group = data[0]
        self.assertEqual(group["uuid"], str(self.group_a.uuid))
        self.assertEqual(group["name"], "alpha")
        self.assertEqual(group["description"], "first")
        self.assertEqual(group["principal_count"], 2)
        self.assertEqual(group["role_count"], 2)
        self.assertFalse(group["system"])
        self.assertFalse(group["platform_default"])
        self.assertFalse(group["admin_default"])
        self.assertCountEqual(
            group.keys(),
            [
                "uuid",
                "name",
                "description",
                "principal_count",
                "role_count",
                "created",
                "modified",
                "system",
                "platform_default",
                "admin_default",
            ],
        )

    def test_list_uses_offset_pagination(self):
        """The list response uses offset pagination meta and links."""
        response = self._list(limit=1)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["meta"]["count"], Group.objects.filter(tenant=self.tenant).count())
        self.assertIn("next", body["links"])

    def test_list_excludes_other_tenant_groups(self):
        """Groups from other tenants are never returned."""
        other_tenant = self.tenant.__class__.objects.create(tenant_name="other", org_id="other-org")
        Group.objects.create(name="alpha-other", tenant=other_tenant)

        response = self._list(name="alpha")

        self.assertEqual(self._names(response), ["alpha"])

    def test_list_filter_by_name_glob(self):
        """Name filter supports glob patterns."""
        response = self._list(name="b*")

        self.assertEqual(self._names(response), ["beta"])

    def test_list_filter_by_uuid(self):
        """Comma-separated UUIDs filter the list."""
        response = self._list(uuid=f"{self.group_a.uuid},{self.group_b.uuid}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(self._names(response), ["alpha", "beta"])

    def test_list_filter_by_uuid_ignores_empty_values(self):
        """An empty uuid filter is ignored instead of matching nothing."""
        response = self._list(uuid=",")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(self._tenant_group_names(response), ["alpha", "beta"])

    def test_list_filter_by_invalid_uuid(self):
        """An invalid UUID in the filter is rejected."""
        response = self._list(uuid="not-a-uuid")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["errors"][0]["field"], "uuid")

    def test_list_filter_by_flags(self):
        """Boolean flag filters narrow the list; omitted flags do not filter."""
        Group.objects.create(name="sys", system=True, tenant=self.tenant)

        response = self._list(system="true")
        self.assertEqual(self._tenant_group_names(response), ["sys"])

        response = self._list(system="false", name="a")
        self.assertCountEqual(self._tenant_group_names(response), ["alpha", "beta"])

        response = self._list(platform_default="false", admin_default="false")
        self.assertCountEqual(self._tenant_group_names(response), ["alpha", "beta", "sys"])

    def test_list_order_by(self):
        """order_by supports name, modified, principal_count and role_count with '-' for descending."""
        self._bind(self.group_b, self.role_1)

        cases = {
            "name": ["alpha", "beta"],
            "-name": ["beta", "alpha"],
            "principal_count": ["beta", "alpha"],
            "-principal_count": ["alpha", "beta"],
            "role_count": ["alpha", "beta"],
            "-role_count": ["beta", "alpha"],
        }
        for order_by, expected in cases.items():
            with self.subTest(order_by=order_by):
                response = self._list(order_by=order_by, uuid=f"{self.group_a.uuid},{self.group_b.uuid}")
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(self._names(response), expected)

    def test_list_order_by_modified(self):
        """order_by=-modified returns the most recently modified group first."""
        self.group_a.save()

        response = self._list(order_by="-modified", uuid=f"{self.group_a.uuid},{self.group_b.uuid}")

        self.assertEqual(self._names(response), ["alpha", "beta"])

    def test_list_order_by_blank_uses_default(self):
        """A blank order_by falls back to the default name ordering."""
        response = self._list(order_by="", uuid=f"{self.group_b.uuid},{self.group_a.uuid}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._names(response), ["alpha", "beta"])

    def test_list_order_by_invalid(self):
        """Invalid order_by values are rejected with 400."""
        for value in ("uuid", "-", "name,modified", "-principals"):
            with self.subTest(order_by=value):
                response = self._list(order_by=value)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json()["errors"][0]["field"], "order_by")

    def test_list_denied_without_read_permission(self):
        """Listing requires rbac_groups_read."""
        self.mock_check_access.return_value = False

        response = self._list()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.mock_check_access.call_args.kwargs["relation"], "rbac_groups_read")


class GroupV2RetrieveViewTest(GroupV2ViewTestBase):
    """Tests for retrieving a group."""

    def test_retrieve_group(self):
        """A group is returned with its counts."""
        self._bind(self.group_a, self.role_1)

        response = self.client.get(self._detail_url(self.group_a.uuid), **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["uuid"], str(self.group_a.uuid))
        self.assertEqual(data["principal_count"], 2)
        self.assertEqual(data["role_count"], 1)
        self.assertNotIn("principals", data)
        self.assertNotIn("roles", data)

    def test_retrieve_not_found(self):
        """Unknown groups return 404."""
        response = self.client.get(self._detail_url(uuid.uuid4()), **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_other_tenant_group_not_found(self):
        """Groups from other tenants return 404."""
        other_tenant = self.tenant.__class__.objects.create(tenant_name="other", org_id="other-org")
        other_group = Group.objects.create(name="other", tenant=other_tenant)

        response = self.client.get(self._detail_url(other_group.uuid), **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_invalid_uuid(self):
        """A malformed UUID returns 404 rather than a server error."""
        url = self._list_url() + "not-a-uuid/"

        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class GroupV2CreateViewTest(GroupV2ViewTestBase):
    """Tests for creating a group."""

    def _create(self, body):
        return self.client.post(self._list_url(), body, format="json", **self.headers)

    def test_create_group(self):
        """A group is created, audited and returned with zero counts."""
        response = self._create({"name": "gamma", "description": "third"})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        group = Group.objects.get(tenant=self.tenant, name="gamma")
        self.assertEqual(data["uuid"], str(group.uuid))
        self.assertEqual(data["description"], "third")
        self.assertEqual(data["principal_count"], 0)
        self.assertEqual(data["role_count"], 0)
        self.assertFalse(data["system"])

        log = AuditLog.objects.get(resource_type=AuditLog.GROUP_V2, resource_uuid=group.uuid)
        self.assertEqual(log.action, AuditLog.CREATE)
        self.assertEqual(log.description, "Created V2 group: gamma")
        self.assertEqual(log.tenant, self.tenant)

    def test_create_ignores_read_only_flags(self):
        """Protected flags cannot be set through the API."""
        response = self._create({"name": "gamma", "system": True, "platform_default": True, "admin_default": True})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        group = Group.objects.get(tenant=self.tenant, name="gamma")
        self.assertFalse(group.system or group.platform_default or group.admin_default)

    def test_create_duplicate_name(self):
        """A duplicate name returns 400 with the already-exists problem type and nothing is audited."""
        response = self._create({"name": "alpha"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = response.json()
        self.assertEqual(body["type"], "http://project-kessel.org/problems/already-exists")
        self.assertEqual(body["detail"], "A group with name 'alpha' already exists for this tenant.")
        self.assertFalse(AuditLog.objects.filter(resource_type=AuditLog.GROUP_V2).exists())

    def test_create_validation_errors(self):
        """Missing, too long and reserved names are rejected."""
        for body, field in (
            ({}, "name"),
            ({"name": ""}, "name"),
            ({"name": "x" * 151}, "name"),
            ({"name": "Default access"}, "name"),
            ({"name": "ok", "description": "x" * 1001}, "description"),
        ):
            with self.subTest(body=body):
                response = self._create(body)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json()["errors"][0]["field"], field)

    def test_create_requires_write_permission(self):
        """Creating requires rbac_groups_write."""
        self.mock_check_access.return_value = False

        response = self._create({"name": "gamma"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.mock_check_access.call_args.kwargs["relation"], "rbac_groups_write")
        self.assertFalse(Group.objects.filter(tenant=self.tenant, name="gamma").exists())

    @override_settings(V2_EDIT_API_ENABLED=False)
    @patch("feature_flags.FEATURE_FLAGS.is_v2_edit_api_enabled", return_value=False)
    @patch("management.permissions.v2_edit_api_access.is_v2_write_activated", return_value=False)
    def test_create_requires_workspaces_enabled(self, _mock_activated, _mock_flag):
        """Writes are blocked when the org is not using workspaces."""
        response = self._create({"name": "gamma"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("management.group.v2_view.group_obj_change_notification_handler")
    def test_create_sends_notification_on_commit(self, mock_notify):
        """The created-group notification is deferred until the transaction commits."""
        with self.captureOnCommitCallbacks() as callbacks:
            response = self._create({"name": "gamma"})
            mock_notify.assert_not_called()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        group = Group.objects.get(tenant=self.tenant, name="gamma")
        mock_notify.assert_called_once_with(response.wsgi_request.user, group, "created")

    @patch("management.group.v2_view.group_obj_change_notification_handler", side_effect=RuntimeError("kafka down"))
    def test_create_notification_failure_does_not_fail_request(self, mock_notify):
        """A failing notification is logged and does not affect the response."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self._create({"name": "gamma"})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_notify.assert_called_once()


class GroupV2UpdateViewTest(GroupV2ViewTestBase):
    """Tests for updating a group."""

    def _update(self, group, body):
        return self.client.put(self._detail_url(group.uuid), body, format="json", **self.headers)

    def test_update_group(self):
        """Name and description are updated and the edit is audited."""
        self._bind(self.group_a, self.role_1)

        response = self._update(self.group_a, {"name": "alpha-renamed", "description": "changed"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["name"], "alpha-renamed")
        self.assertEqual(data["description"], "changed")
        self.assertEqual(data["principal_count"], 2)
        self.assertEqual(data["role_count"], 1)
        self.group_a.refresh_from_db()
        self.assertEqual(self.group_a.name, "alpha-renamed")

        log = AuditLog.objects.get(resource_type=AuditLog.GROUP_V2, resource_uuid=self.group_a.uuid)
        self.assertEqual(log.action, AuditLog.EDIT)
        self.assertEqual(log.description, "V2 group alpha:\nEdited name\nEdited description")

    def test_update_without_description_audits_cleared_description(self):
        """PUT without description clears it, and the audit entry records the change."""
        response = self._update(self.group_a, {"name": "alpha"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.json()["description"])
        log = AuditLog.objects.get(resource_type=AuditLog.GROUP_V2, resource_uuid=self.group_a.uuid)
        self.assertEqual(log.description, "V2 group alpha:\nEdited description")

    def test_update_null_description_on_group_without_description(self):
        """A null description on a group without one is not reported as an edit."""
        response = self._update(self.group_b, {"name": "beta", "description": None})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        log = AuditLog.objects.get(resource_type=AuditLog.GROUP_V2, resource_uuid=self.group_b.uuid)
        self.assertEqual(log.description, "V2 group beta")

    @patch("management.group.v2_view.group_obj_change_notification_handler")
    def test_update_sends_notification_on_commit(self, mock_notify):
        """The updated-group notification is sent after commit."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self._update(self.group_a, {"name": "alpha-renamed"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.group_a.refresh_from_db()
        mock_notify.assert_called_once_with(response.wsgi_request.user, self.group_a, "updated")

    def test_update_system_group_rejected(self):
        """System groups cannot be updated."""
        group = Group.objects.create(name="sys", system=True, tenant=self.tenant)

        response = self._update(group, {"name": "renamed"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["detail"], "Groups with system=true may not be updated.")
        group.refresh_from_db()
        self.assertEqual(group.name, "sys")
        self.assertFalse(AuditLog.objects.filter(resource_type=AuditLog.GROUP_V2).exists())

    def test_update_platform_and_admin_default_groups_allowed(self):
        """Non-system platform_default and admin_default groups allow name/description updates."""
        for flag in ("platform_default", "admin_default"):
            with self.subTest(flag=flag):
                group = Group.objects.create(name=f"{flag}-group", tenant=self.tenant, **{flag: True})

                response = self._update(group, {"name": f"{flag}-renamed", "description": "d"})

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                group.refresh_from_db()
                self.assertEqual(group.name, f"{flag}-renamed")
                self.assertTrue(getattr(group, flag))

    def test_update_duplicate_name(self):
        """Renaming to an existing name returns 400 already-exists and rolls back the audit entry."""
        response = self._update(self.group_a, {"name": "beta"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["type"], "http://project-kessel.org/problems/already-exists")
        self.group_a.refresh_from_db()
        self.assertEqual(self.group_a.name, "alpha")
        self.assertFalse(AuditLog.objects.filter(resource_type=AuditLog.GROUP_V2).exists())

    def test_update_not_found(self):
        """Updating an unknown group returns 404."""
        response = self.client.put(self._detail_url(uuid.uuid4()), {"name": "x"}, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_requires_write_permission(self):
        """Updating requires rbac_groups_write."""
        self.mock_check_access.return_value = False

        response = self._update(self.group_a, {"name": "renamed"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.mock_check_access.call_args.kwargs["relation"], "rbac_groups_write")

    def test_patch_not_allowed(self):
        """PATCH is not part of the contract."""
        response = self.client.patch(
            self._detail_url(self.group_a.uuid), {"name": "renamed"}, format="json", **self.headers
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class GroupV2DestroyViewTest(GroupV2ViewTestBase):
    """Tests for deleting a group."""

    def _delete(self, group_uuid):
        return self.client.delete(self._detail_url(group_uuid), **self.headers)

    def test_delete_group(self):
        """A custom group without role bindings is deleted, replicated and audited."""
        group_uuid = self.group_a.uuid

        response = self._delete(group_uuid)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Group.objects.filter(uuid=group_uuid).exists())

        self.mock_dual_write.assert_called_once()
        self.assertEqual(self.mock_dual_write.call_args.args[1], ReplicationEventType.DELETE_GROUP)
        removed = self.mock_dual_write.return_value.replicate_removed_principals.call_args.args[0]
        self.assertCountEqual(removed, [self.user_1, self.user_2, self.service_account])

        log = AuditLog.objects.get(resource_type=AuditLog.GROUP_V2, resource_uuid=group_uuid)
        self.assertEqual(log.action, AuditLog.DELETE)
        self.assertEqual(log.description, "Deleted V2 group: alpha")

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_delete_group_replicates_member_removal(self, mock_save_event):
        """Deleting a group writes removal of its user member tuples to the outbox."""
        self.user_1.user_id = "1111"
        self.user_1.save()
        self.user_2.user_id = "2222"
        self.user_2.save()
        group_uuid = str(self.group_a.uuid)

        with patch("management.group.v2_service.RelationApiDualWriteGroupHandler", RelationApiDualWriteGroupHandler):
            response = self._delete(group_uuid)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        event = mock_save_event.call_args.args[0]
        self.assertEqual(event["relations_to_add"], [])
        removed = {
            (r["resource"]["id"], r["relation"], r["subject"]["subject"]["id"]) for r in event["relations_to_remove"]
        }
        self.assertEqual(
            removed,
            {(group_uuid, "member", "redhat/1111"), (group_uuid, "member", "redhat/2222")},
        )

    def test_delete_protected_groups_rejected(self):
        """System, platform_default and admin_default groups return 400."""
        for flag in ("system", "platform_default", "admin_default"):
            with self.subTest(flag=flag):
                group = Group.objects.create(name=f"{flag}-group", tenant=self.tenant, **{flag: True})

                response = self._delete(group.uuid)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json()["detail"], f"Groups with {flag}=true may not be deleted.")
                self.assertTrue(Group.objects.filter(uuid=group.uuid).exists())
        self.mock_dual_write.assert_not_called()

    def test_delete_group_with_role_bindings_conflict(self):
        """A group referenced by role bindings returns 409 and is kept."""
        self._bind(self.group_a, self.role_1, "ws-1")
        self._bind(self.group_a, self.role_2, "ws-1")

        response = self._delete(self.group_a.uuid)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        body = response.json()
        self.assertEqual(body["status"], 409)
        self.assertEqual(
            body["detail"],
            "Group is referenced by 2 active role binding(s). "
            "Remove the group from all role bindings before deleting it.",
        )
        self.assertTrue(Group.objects.filter(uuid=self.group_a.uuid).exists())
        self.mock_dual_write.assert_not_called()
        self.assertFalse(AuditLog.objects.filter(resource_type=AuditLog.GROUP_V2).exists())

    def test_delete_group_with_legacy_policy_role_conflict(self):
        """A group referenced only by a legacy v1 Policy/Role assignment returns 409 and is kept.

        role_binding_entries only covers v2 RoleBindingGroup rows; tenants still on v1-era
        Policy-based role assignments would otherwise pass that guard and be deleted while
        leaving orphaned SpiceDB tuples behind.
        """
        legacy_role = Role.objects.create(name="legacy_role", tenant=self.tenant)
        policy = Policy.objects.create(name="legacy_policy", group=self.group_a, tenant=self.tenant)
        policy.roles.add(legacy_role)

        response = self._delete(self.group_a.uuid)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        body = response.json()
        self.assertEqual(body["status"], 409)
        self.assertEqual(
            body["detail"],
            "Group is referenced by 1 active role binding(s). "
            "Remove the group from all role bindings before deleting it.",
        )
        self.assertTrue(Group.objects.filter(uuid=self.group_a.uuid).exists())
        self.mock_dual_write.assert_not_called()
        self.assertFalse(AuditLog.objects.filter(resource_type=AuditLog.GROUP_V2).exists())

    def test_delete_protected_error_race_maps_to_conflict(self):
        """A ProtectedError raised by the ORM (binding added concurrently) still maps to 409.

        The dual-write handler is constructed before group.delete() (matching V1 ordering), so
        construction itself is not a signal of success -- replication must not have run.
        """
        with patch.object(Group, "delete", side_effect=ProtectedError("protected", {object()})):
            response = self._delete(self.group_a.uuid)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.mock_dual_write.return_value.replicate_removed_principals.assert_not_called()

    @patch("management.group.v2_view.group_obj_change_notification_handler")
    def test_delete_sends_notification_on_commit(self, mock_notify):
        """The deleted-group notification is sent after commit."""
        group_uuid = self.group_a.uuid

        with self.captureOnCommitCallbacks(execute=True):
            response = self._delete(group_uuid)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        mock_notify.assert_called_once()
        user, group, operation = mock_notify.call_args.args
        self.assertEqual((user, group.uuid, operation), (response.wsgi_request.user, group_uuid, "deleted"))

    def test_delete_not_found(self):
        """Deleting an unknown group returns 404."""
        response = self._delete(uuid.uuid4())

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_requires_write_permission(self):
        """Deleting requires rbac_groups_write."""
        self.mock_check_access.return_value = False

        response = self._delete(self.group_a.uuid)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.mock_check_access.call_args.kwargs["relation"], "rbac_groups_write")
        self.assertTrue(Group.objects.filter(uuid=self.group_a.uuid).exists())


class GroupV2ServiceQueryTest(GroupV2ViewTestBase):
    """Query-count checks for the annotated queryset."""

    def test_list_counts_do_not_cause_n_plus_one(self):
        """Counts come from annotations, so the list query count does not grow with the number of groups."""
        for i in range(5):
            group = Group.objects.create(name=f"extra-{i}", tenant=self.tenant)
            group.principals.add(self.user_1)
            self._bind(group, self.role_1, f"ws-{i}")

        service = GroupV2Service(tenant=self.tenant)
        with self.assertNumQueries(1):
            groups = list(service.list({}))
        self.assertTrue(all(hasattr(g, "principal_count_annotation") for g in groups))


class GroupV2AccessPermissionTest(IdentityRequest):
    """Tests for GroupV2KesselAccessPermission relation selection."""

    def test_relation_for_actions(self):
        """Write actions require rbac_groups_write, everything else rbac_groups_read."""
        permission = GroupV2KesselAccessPermission()
        for action, relation in (
            ("list", "rbac_groups_read"),
            ("retrieve", "rbac_groups_read"),
            ("create", "rbac_groups_write"),
            ("update", "rbac_groups_write"),
            ("destroy", "rbac_groups_write"),
        ):
            with self.subTest(action=action):
                view = type("View", (), {"action": action})()
                self.assertEqual(permission._get_relation(view), relation)


class GroupV2RouteGatingTest(IdentityRequest):
    """Tests for V2 route registration."""

    @override_settings(V2_APIS_ENABLED=False)
    def test_route_not_registered_when_v2_disabled(self):
        """The groups route is only served when V2 APIs are enabled."""
        reload(urls)
        clear_url_caches()
        try:
            response = self.client.get("/api/rbac/v2/groups/", **self.headers)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        finally:
            clear_url_caches()

    def test_groups_registered_in_v2_router(self):
        """The v2 router exposes the groups list and detail routes."""
        names = {pattern.name for pattern in v2_urls.ROUTER.urls}
        self.assertIn("groups-list", names)
        self.assertIn("groups-detail", names)
