#
# Copyright 2020 Red Hat, Inc.
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
"""Test the cross account request model."""

from api.models import CrossAccountRequest, Tenant
from api.cross_access.util import get_cross_principal_name
from django.urls import reverse
from django.utils import timezone

from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.models import Group, Role, Principal
from management.notifications.notification_handlers import EVENT_TYPE_RH_TAM_REQUEST_CREATED
from rest_framework import status
from rest_framework.test import APIClient

from datetime import timedelta
from unittest.mock import patch

from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import BindingMapping
from management.workspace.model import Workspace
from migration_tool.in_memory_tuples import (
    all_of,
    one_of,
    relation,
    resource,
    resource_id,
    subject,
    InMemoryRelationReplicator,
)
from tests.api.cross_access.fixtures import CrossAccountRequestTest

from django.test.utils import override_settings
from functools import partial

from tests.util import assert_v1_v2_locally_consistent

URL_LIST = reverse("v1_api:cross-list")


@override_settings(ATOMIC_RETRY_DISABLED=True)
class CrossAccountRequestViewTests(CrossAccountRequestTest):
    """Test the cross account request view."""

    def tearDown(self):
        with self.subTest(msg="tuple consistency"):
            assert_v1_v2_locally_consistent(test=self)

        super().tearDown()

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
                    "account_number": "567890",
                    "user_id": "1111111",
                },
                {
                    "username": "test_user_2",
                    "email": "test_user_2@email.com",
                    "first_name": "user_2",
                    "last_name": "test",
                    "account_number": "123456",
                    "user_id": "2222222",
                },
            ],
        },
    )
    def test_list_requests_query_by_account_success(self, mock_request):
        """Test listing of cross account request based on account number of identity."""
        client = APIClient()
        response = client.get(URL_LIST, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 4)
        email_list = [data.get("email") for data in response.data["data"]]
        self.assertTrue("test_user@email.com" in email_list)
        self.assertTrue("test_user_2@email.com" in email_list)

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
                    "account_number": "567890",
                    "user_id": "1111111",
                },
                {
                    "username": "test_user_2",
                    "email": "test_user_2@email.com",
                    "first_name": "user_2",
                    "last_name": "test",
                    "account_number": "123456",
                    "user_id": "2222222",
                },
            ],
        },
    )
    def test_list_requests_query_by_account_with_status_filter_success(self, mock_request):
        """Test listing of cross account request based on account number of identity and filter status."""
        client = APIClient()

        # Single status filter
        response = client.get(f"{URL_LIST}?status=pending", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)
        self.assertEqual(response.data["data"][0].get("status"), "pending")
        self.assertEqual(response.data["data"][1].get("status"), "pending")

        # Multiple statuses filter
        response = client.get(f"{URL_LIST}?status=approved,expired", **self.headers)
        statuses = [data.get("status") for data in response.data["data"]]
        self.assertTrue("approved" in statuses)
        self.assertTrue("expired" in statuses)
        self.assertTrue("pending" not in statuses)

    def test_list_requests_query_by_account_fail_if_not_admin(self):
        """Test listing cross account request based on account number of identity would fail for non org admin."""
        client = APIClient()
        response = client.get(URL_LIST, **self.associate_non_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["errors"][0]["detail"].code, "permission_denied")

    def test_list_requests_query_by_user_id_success(self):
        """Test listing cross account request based on user id of identity."""
        client = APIClient()
        response = client.get(f"{URL_LIST}?query_by=user_id", **self.associate_non_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 4)
        request_ids = [data.get("request_id") for data in response.data["data"]]
        for request_id in request_ids:
            self.assertTrue(
                request_id
                in [
                    str(self.request_1.request_id),
                    str(self.request_3.request_id),
                    str(self.request_6.request_id),
                    str(self.not_anemic_request_1.request_id),
                ]
            )

    def test_list_requests_query_by_user_id_filter_by_org_id_success(self):
        """Test listing cross account request based on user id of identity."""
        client = APIClient()
        response = client.get(
            f"{URL_LIST}?query_by=user_id&org_id={self.not_anemic_org_id}", **self.associate_not_anemic_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0].get("user_id"), "1111111")
        self.assertEqual(response.data["data"][0].get("target_org"), self.not_anemic_org_id)
        self.assertEqual(response.data["data"][0].get("status"), "approved")

        response = client.get(
            f"{URL_LIST}?query_by=user_id&org_id={self.not_anemic_org_id},{self.another_org_id}",
            **self.associate_not_anemic_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 3)

    def test_list_requests_query_by_user_id_with_combined_filters_success(self):
        """Test listing cross account request based on user id of identity."""
        expired_request = CrossAccountRequest.objects.create(
            target_org="567890",
            user_id="1111111",
            end_date=self.ref_time + timedelta(10),
            status="approved",
        )
        CrossAccountRequest.objects.filter(request_id=expired_request.request_id).update(end_date=timezone.now())

        client = APIClient()
        response = client.get(
            f"{URL_LIST}?query_by=user_id&org_id=098765&approved_only=True", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 0)

        response = client.get(
            f"{URL_LIST}?query_by=user_id&org_id={self.another_org_id}&approved_only=True",
            **self.associate_non_admin_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0].get("user_id"), "1111111")
        self.assertEqual(response.data["data"][0].get("target_org"), self.another_org_id)
        self.assertEqual(response.data["data"][0].get("status"), "approved")

    def test_list_requests_query_by_user_id_fail_if_not_associate(self):
        """Test listing cross account request based on user id of identity would fail for non-associate."""
        client = APIClient()
        response = client.get(f"{URL_LIST}?query_by=user_id", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["errors"][0]["detail"].code, "permission_denied")

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
                    "account_number": "567890",
                    "user_id": "1111111",
                },
            ],
        },
    )
    def test_list_missing_user(self, mock_request):
        """Test listing of cross account request based on account number of identity."""
        client = APIClient()
        response = client.get(URL_LIST, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 4)

        data = response.data["data"]

        user1_cars = [r for r in data if r["request_id"] == str(self.request_1.request_id)]
        user2_cars = [r for r in data if r["request_id"] == str(self.request_2.request_id)]

        self.assertEqual(1, len(user1_cars))
        self.assertEqual(1, len(user2_cars))

        user1_car = user1_cars[0]
        user2_car = user2_cars[0]

        self.assertTrue(user1_car["user_available"])
        self.assertNotIn("user_id", user1_car)
        self.assertEqual("user", user1_car["first_name"])
        self.assertEqual("test", user1_car["last_name"])
        self.assertEqual("test_user@email.com", user1_car["email"])

        self.assertFalse(user2_car["user_available"])
        self.assertEqual("2222222", user2_car["user_id"])
        self.assertNotIn("first_name", user2_car)
        self.assertNotIn("last_name", user2_car)
        self.assertNotIn("email", user2_car)

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
                    "account_number": "567890",
                    "user_id": "1111111",
                }
            ],
        },
    )
    def test_retrieve_request_query_by_account_success(self, mock_request):
        """Test retrieve of cross account request based on account number of identity."""
        client = APIClient()
        response = client.get(f"{URL_LIST}{self.request_1.request_id}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("target_account", response.data)
        self.assertEqual(response.data.get("email"), "test_user@email.com")
        self.assertEqual(len(response.data.get("roles")), 2)

    def test_retrieve_request_query_by_account_fail_if_request_in_another_account(self):
        """Test retrieve cross account request based on account number of identity would fail for non org admin."""
        client = APIClient()
        response = client.get(f"{URL_LIST}{self.request_3.request_id}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_request_query_by_account_fail_if_not_admin(self):
        """Test retrieve cross account request based on account number of identity would fail for non org admin."""
        client = APIClient()
        response = client.get(f"{URL_LIST}{self.request_1.request_id}/", **self.associate_non_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["errors"][0]["detail"].code, "permission_denied")

    def test_retrieve_request_query_by_user_id_success(self):
        """Test retrieve cross account request based on user id of identity."""
        client = APIClient()
        response = client.get(
            f"{URL_LIST}{self.request_1.request_id}/?query_by=user_id", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("target_account", response.data)
        self.assertEqual(response.data.get("email"), None)
        self.assertEqual(response.data.get("user_id"), "1111111")
        self.assertEqual(len(response.data.get("roles")), 2)

    def test_retrieve_request_query_by_user_id_fail_if_request_by_another_associate(self):
        """Test retrieve cross account request based on user id of identity would fail for non-associate."""
        client = APIClient()
        response = client.get(
            f"{URL_LIST}{self.request_2.request_id}/?query_by=user_id", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_request_query_by_user_id_fail_if_not_associate(self):
        """Test retrieve cross account request based on user id of identity would fail for non-associate."""
        client = APIClient()
        response = client.get(f"{URL_LIST}{self.request_1.request_id}/?query_by=user_id", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["errors"][0]["detail"].code, "permission_denied")

    @patch("management.notifications.notification_handlers.notify")
    def test_create_requests_success(self, notify_mock):
        """Test the creation of cross account request success."""
        client = APIClient()
        with self.settings(NOTIFICATIONS_ENABLED=True):
            response = client.post(
                f"{URL_LIST}?", self.data4create, format="json", **self.associate_non_admin_request.META
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("target_account", response.data)
        self.assertEqual(response.data["target_org"], self.data4create["target_org"])
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(response.data["start_date"], self.data4create["start_date"])
        self.assertEqual(response.data["end_date"], self.data4create["end_date"])
        self.assertEqual(len(response.data["roles"]), 2)
        notify_mock.assert_called_once_with(
            EVENT_TYPE_RH_TAM_REQUEST_CREATED,
            {
                "username": self.user_data["username"],
                "request_id": response.data["request_id"],
            },
            self.data4create["target_org"],
        )

    @patch("management.notifications.notification_handlers.notify")
    def test_create_requests_success_with_same_name_or_roles(self, notify_mock):
        """Test the creation of cross account request success."""
        Role.objects.create(name="role_1", system=False, tenant=self.tenant)
        client = APIClient()
        with self.settings(NOTIFICATIONS_ENABLED=True):
            response = client.post(
                f"{URL_LIST}?", self.data4create, format="json", **self.associate_non_admin_request.META
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("target_account", response.data)
        self.assertEqual(response.data["target_org"], self.data4create["target_org"])
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(response.data["start_date"], self.data4create["start_date"])
        self.assertEqual(response.data["end_date"], self.data4create["end_date"])
        self.assertEqual(len(response.data["roles"]), 2)
        notify_mock.assert_called_once_with(
            EVENT_TYPE_RH_TAM_REQUEST_CREATED,
            {
                "username": self.user_data["username"],
                "request_id": response.data["request_id"],
            },
            self.data4create["target_org"],
        )

    @patch("management.notifications.notification_handlers.notify")
    def test_create_requests_ignores_legacy_target_account(self, notify_mock):
        """Test that target_account in request body is silently ignored while target_org is honored."""
        payload = {**self.data4create, "target_account": "legacy-value"}
        client = APIClient()
        with self.settings(NOTIFICATIONS_ENABLED=True):
            response = client.post(f"{URL_LIST}?", payload, format="json", **self.associate_non_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("target_account", response.data)
        self.assertEqual(response.data["target_org"], self.data4create["target_org"])

    def test_create_requests_fail_for_no_account(self):
        """Test the creation of cross account request fails when the account doesn't exist."""

        cross_account_request_with_missing_account = {
            "target_org": "9999",
            "start_date": self.format_date(self.ref_time),
            "end_date": self.format_date(self.ref_time + timedelta(90)),
            "roles": ["role_1", "role_2"],
        }

        client = APIClient()
        response = client.post(
            f"{URL_LIST}?",
            cross_account_request_with_missing_account,
            format="json",
            **self.associate_non_admin_request.META,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = cross_account_request_with_missing_account["target_org"]
        self.assertEqual(response.data.get("errors")[0].get("detail"), f"Org ID '{data}' does not exist.")

    def test_create_requests_towards_their_own_account_fail(self):
        """Test the creation of cross account request towards their own account fails."""
        self.data4create["target_org"] = self.org_id
        client = APIClient()
        response = client.post(
            f"{URL_LIST}?", self.data4create, format="json", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            "Creating a cross access request for your own org id is not allowed.",
        )

    def test_create_requests_fail_for_none_associate(self):
        """Test the creation of cross account request fail for none red hat associate."""
        client = APIClient()
        response = client.post(f"{URL_LIST}?", self.data4create, format="json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_requests_fail_for_over_a_year_period(self):
        """Test the creation of cross account request fail for not supported period."""
        self.data4create["end_date"] = self.format_date(self.ref_time + timedelta(366))
        client = APIClient()
        response = client.post(
            f"{URL_LIST}?", self.data4create, format="json", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"), "Access duration may not be longer than one year."
        )

    def test_create_requests_fail_for_end_date_being_past_value(self):
        """Test the creation of cross account request fail for end_date being past value."""
        self.data4create["end_date"] = self.format_date(self.ref_time + timedelta(-1))
        client = APIClient()
        response = client.post(
            f"{URL_LIST}?", self.data4create, format="json", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"), "Please verify the end dates are not in the past."
        )

    def test_create_requests_fail_for_not_exist_role(self):
        """Test the creation of cross account request fail for not supported period."""
        self.data4create["roles"] = ["role_1", "role_3"]
        client = APIClient()
        response = client.post(
            f"{URL_LIST}?", self.data4create, format="json", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get("errors")[0].get("detail"), "Role 'role_3' does not exist.")

    def test_create_requests_fail_for_not_canned_role(self):
        """Test the creation of cross account request fail for not supported period."""
        self.role_2.system = False
        self.role_2.save()
        self.data4create["roles"] = ["role_1", "role_2"]
        client = APIClient()
        response = client.post(
            f"{URL_LIST}?", self.data4create, format="json", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            "Only system roles may be assigned to a cross-account-request.",
        )

    def test_create_requests_fail_for_over_60_day_start_date(self):
        """Test the creation of cross account request fails when the start date is > 60 days out."""
        self.data4create["start_date"] = self.format_date(self.ref_time + timedelta(61))
        client = APIClient()
        response = client.post(
            f"{URL_LIST}?", self.data4create, format="json", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get("errors")[0].get("detail"), "Start date must be within 60 days of today.")

    def test_update_request_denied_for_approver(self):
        """Test updating an entire CAR denied for approver."""
        self.data4create["target_org"] = self.org_id
        self.data4create["start_date"] = self.format_date(self.ref_time + timedelta(3))
        self.data4create["end_date"] = self.format_date(self.ref_time + timedelta(5))
        self.data4create["roles"] = ["role_8", "role_9"]

        car_uuid = self.request_2.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.put(url, self.data4create, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"), "Only the requestor may update the cross access request."
        )

    def test_patch_custom_role_request_fail(self):
        """Test updating an entire CAR."""
        Role.objects.create(name="role_custom", system=False, tenant=self.tenant)
        data_to_patch = {
            "start_date": self.format_date(self.ref_time + timedelta(3)),
            "end_date": self.format_date(self.ref_time + timedelta(5)),
            "roles": ["role_custom"],
            "status": "cancelled",
        }
        car_uuid = self.request_1.request_id
        self.request_1.target_org = self.another_org_id
        self.request_1.status = "pending"
        self.request_1.save()
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.patch(url, data_to_patch, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            "Role 'role_custom' does not exist.",
        )

    def test_patch_roles_which_doesnt_exist_request_fail(self):
        """Test updating an entire CAR."""
        data_to_patch = {
            "start_date": self.format_date(self.ref_time + timedelta(3)),
            "end_date": self.format_date(self.ref_time + timedelta(5)),
            "roles": ["YYYY", "XXXXX"],
            "status": "cancelled",
        }
        car_uuid = self.request_1.request_id
        self.request_1.target_org = self.another_org_id
        self.request_1.status = "pending"
        self.request_1.save()
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.patch(url, data_to_patch, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get("errors")[0].get("detail"), "Role 'YYYY' does not exist.")

    def test_patch_roles_request_success(self):
        """Test updating an entire CAR."""
        data_to_patch = {
            "start_date": self.format_date(self.ref_time + timedelta(3)),
            "end_date": self.format_date(self.ref_time + timedelta(5)),
            "roles": ["role_8", "role_9"],
            "status": "cancelled",
        }
        car_uuid = self.request_1.request_id
        self.request_1.target_org = self.another_org_id
        self.request_1.status = "pending"
        self.request_1.save()
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.patch(url, data_to_patch, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in data_to_patch:
            if field == "roles":
                for role in response.data.get("roles"):
                    self.assertIn(role.get("display_name"), data_to_patch["roles"])
                continue
            self.assertEqual(data_to_patch.get(field), response.data.get(field))

    def test_update_request_success_for_requestor(self):
        """Test updating an entire CAR."""
        self.data4create["target_org"] = self.another_org_id
        Tenant.objects.create(
            tenant_name=f"acct{self.another_account}", account_id=self.another_account, org_id=self.another_org_id
        )
        self.data4create["start_date"] = self.format_date(self.ref_time + timedelta(3))
        self.data4create["end_date"] = self.format_date(self.ref_time + timedelta(5))
        self.data4create["roles"] = ["role_8", "role_9"]
        self.data4create["status"] = "pending"

        car_uuid = self.request_1.request_id
        self.request_1.target_org = self.another_org_id
        self.request_1.status = "pending"
        self.request_1.save()
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.put(url, self.data4create, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.data4create:
            if field == "roles":
                for role in response.data.get("roles"):
                    self.assertIn(role.get("display_name"), self.data4create["roles"])
                continue
            self.assertEqual(self.data4create.get(field), response.data.get(field))

    def test_update_request_success_for_requestor_when_custom_role_with_same_name_exist(self):
        """Test updating an entire CAR."""
        Role.objects.create(name="role_8", system=False, tenant=self.tenant)
        self.data4create["target_org"] = self.another_org_id
        Tenant.objects.create(
            tenant_name=f"acct{self.another_account}", account_id=self.another_account, org_id=self.another_org_id
        )
        self.data4create["start_date"] = self.format_date(self.ref_time + timedelta(3))
        self.data4create["end_date"] = self.format_date(self.ref_time + timedelta(5))
        self.data4create["roles"] = ["role_8", "role_9"]
        self.data4create["status"] = "pending"

        car_uuid = self.request_1.request_id
        self.request_1.target_org = self.another_org_id
        self.request_1.status = "pending"
        self.request_1.save()
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.put(url, self.data4create, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.data4create:
            if field == "roles":
                for role in response.data.get("roles"):
                    self.assertIn(role.get("display_name"), self.data4create["roles"])
                continue
            self.assertEqual(self.data4create.get(field), response.data.get(field))
        self.assertEqual(len(response.data.get("roles")), 2)

    def test_update_request_fail_acct_for_requestor(self):
        """Test that updating the account of a CAR fails."""
        Tenant.objects.create(
            tenant_name=f"acct{self.another_account}", account_id=self.another_account, org_id=self.another_org_id
        )
        self.data4create["target_org"] = "1000001"

        car_uuid = self.request_6.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": car_uuid})

        client = APIClient()
        response = client.put(url, self.data4create, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get("errors")[0].get("detail"), "Target org must stay the same.")

    def test_update_request_fail_org_for_requestor(self):
        """Test that updating the target org of a CAR fails."""
        self.data4create["target_org"] = "1000001"

        car_uuid = self.request_6.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": car_uuid})

        client = APIClient()
        response = client.put(url, self.data4create, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get("errors")[0].get("detail"), "Target org must stay the same.")

    def test_update_request_expired_for_requestor(self):
        """Test that updating an expired CAR fails."""
        self.data4create["end_date"] = self.format_date(self.ref_time + timedelta(20))

        car_uuid = self.request_3.request_id
        self.request_3.status = "expired"
        self.request_3.save()
        url = reverse("v1_api:cross-detail", kwargs={"pk": car_uuid})

        client = APIClient()
        response = client.put(url, self.data4create, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get("errors")[0].get("detail"), "Only pending requests may be updated.")

    def test_partial_update_denied_for_updating_CAR_belong_to_others(self):
        """Test updating part of a CAR does not have relation with api caller."""
        update_data = {"start_date": self.format_date(self.ref_time + timedelta(2))}

        # request_4's user_id is "2222222", associate_admin_request'user_id is "1111111"
        # request_4's target_org is "54321", associate_admin_request's org is self.org_id
        car_uuid = self.request_4.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_partial_update_success_for_approver(self):
        """Test updating part of a CAR."""
        # request_2's account is "xxxxxx" same as associate_admin_request's account
        car_uuid = self.request_2.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})
        client = APIClient()

        # From pending to approved
        update_data = {"status": "approved"}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("status"), update_data.get("status"))

        test_group = Group.objects.create(tenant=self.tenant, name="test_group")

        dual_write = RelationApiDualWriteGroupHandler(test_group, ReplicationEventType.ASSIGN_ROLE)
        dual_write.generate_relations_reset_roles([self.role_1])
        dual_write.replicate()

        binding_mapping = self.role_1.binding_mappings.first()

        # From approved to denied
        update_data = {"status": "denied"}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        # Assigning the group should have prevented the BindingMapping from being deleted.
        self.assertTrue(BindingMapping.objects.filter(pk=binding_mapping.pk).exists())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("status"), update_data.get("status"))

        # Deined again should be fine
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("status"), update_data.get("status"))

        # From denied to approved
        update_data = {"status": "approved"}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("status"), update_data.get("status"))

        # From approved to cancelled would fail
        update_data = {"status": "cancelled"}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_partial_update_invalid_update_denied_for_approver(self):
        """Test updating part of a CAR fail due to invalid update for approver."""
        # request_2's account is "xxxxxx" same as associate_admin_request's account
        car_uuid = self.request_2.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})
        client = APIClient()

        # fail to update if approver is not admin
        update_data = {"status": "approved"}
        response = client.patch(url, update_data, format="json", **self.associate_non_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # fail to update field other than status
        update_data = {"start_date": self.format_date(self.ref_time + timedelta(2))}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # fail to update fields more than just status
        update_data = {"start_date": self.format_date(self.ref_time + timedelta(2)), "status": "approved"}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # approver fail to update status from pending to cancelled
        update_data = {"status": "cancelled"}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_partial_update_success_for_requestor(self):
        """Test updating part of a CAR."""
        # request_6's user_id is "1111111" same as associate_admin_request's user_id
        car_uuid = self.request_6.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        update_data = {"status": "cancelled"}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("status"), update_data.get("status"))

    def test_partial_update_approved_request_for_requestor(self):
        """Test that updating protected fields of a CAR fails."""
        car_uuid = self.request_3.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()

        # Can not partial update status from approved to other status
        update_data = {"status": "cancelled"}
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_partial_update_expired_for_requestor(self):
        """Test that PATCHing an expired CAR fails."""
        update_data = {"end_date": self.format_date(self.ref_time + timedelta(18))}

        car_uuid = self.request_3.request_id
        self.request_3.status = "expired"
        self.request_3.save()
        url = reverse("v1_api:cross-detail", kwargs={"pk": car_uuid})

        client = APIClient()
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_partial_update_bmi_for_requestor(self):
        """Test that PATCH with extra fields in body fails."""
        update_data = {"start_date": self.format_date(self.ref_time + timedelta(2)), "cup": "cake"}

        car_uuid = self.request_6.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_bad_date_spec_for_requestor(self):
        """Test that PUT with body fields not matching spec fails."""
        self.data4create["target_org"] = self.another_org_id
        Tenant.objects.get_or_create(
            tenant_name=f"acct{self.another_account}", account_id=self.another_account, org_id=self.another_org_id
        )
        self.data4create["end_date"] = 12252021

        car_uuid = self.request_1.request_id
        self.request_1.target_org = self.another_org_id
        self.request_1.status = "pending"
        self.request_1.save()
        url = reverse("v1_api:cross-detail", kwargs={"pk": car_uuid})

        client = APIClient()
        response = client.put(url, self.data4create, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("errors")[0].get("detail"),
            "Datetime has wrong format. Use one of these formats instead: MM/DD/YYYY.",
        )

    def test_partial_update_bad_date_spec_for_requestor(self):
        """Test that PATCH with body fields not matching spec fails."""
        update_data = {"start_date": 12252021}

        car_uuid = self.request_6.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})

        client = APIClient()
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_bad_uuid(self):
        """Test that update endpoints reject invalid UUIDs."""
        bad_uuids = [
            "imateapot!",  # Not even close
            str(self.request_5.request_id).replace("-", ""),  # Malformed but valid UUID
            "7g895hq3-6204-43e6-9g4c-20de5f61e021",  # Non-hex values in UUID
            None,
        ]

        client = APIClient()
        for bad_uuid in bad_uuids:
            url = reverse("v1_api:cross-detail", kwargs={"pk": bad_uuid})
            response = client.put(url, self.data4create, format="json", **self.associate_admin_request.META)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            response = client.patch(url, self.data4create, format="json", **self.associate_admin_request.META)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_principal_on_approval(self):
        """Test that moving a car to approved creates a principal."""
        update_data = {"status": "approved"}
        principal_name = get_cross_principal_name(self.request_2.target_org, self.request_2.user_id)
        car_uuid = self.request_2.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})
        tenant = Tenant.objects.get(org_id=self.request_2.target_org)

        client = APIClient()
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("status"), update_data.get("status"))
        # Principal created in public schema
        princ = Principal.objects.get(username__iexact=principal_name)
        self.assertEqual(princ.username, principal_name)
        self.assertEqual(princ.tenant, tenant)
        self.assertTrue(princ.cross_account)

    def test_cross_account_request_ordering_filter(self):
        """Test ordering filter for request id, created/start/end date."""
        client = APIClient()
        # Sort by Request ID
        response = client.get(
            f"{URL_LIST}?query_by=user_id&order_by=request_id", **self.associate_non_admin_request.META
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 4)
        self.assertTrue(response.data["data"][0].get("request_id") < response.data["data"][1].get("request_id"))

        # Sorting the dates
        ## request_1 is created a little bit earlier than request_6, therefore, the
        ## first should be not_anemic_request_1
        response = client.get(
            f"{URL_LIST}?query_by=user_id&order_by=-created", **self.associate_non_admin_request.META
        )
        self.assertEqual(response.data["data"][0].get("request_id"), str(self.not_anemic_request_1.request_id))

        ## set start_date of request_3 to a day later
        self.request_3.start_date = self.ref_time + timedelta(1)
        self.request_3.save()
        response = client.get(
            f"{URL_LIST}?query_by=user_id&order_by=start_date", **self.associate_non_admin_request.META
        )
        self.assertEqual(response.data["data"][0].get("request_id"), str(self.request_1.request_id))

        ## set start_date of request_3 to 21 days later so its end_date bigger than request_1
        self.request_3.end_date = self.ref_time + timedelta(21)
        self.request_3.save()
        response = client.get(
            f"{URL_LIST}?query_by=user_id&order_by=-end_date", **self.associate_non_admin_request.META
        )
        self.assertEqual(response.data["data"][0].get("request_id"), str(self.request_3.request_id))

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
                    "account_number": "567890",
                    "user_id": "1111111",
                }
            ],
        },
    )
    def test_list_requests_query_by_org_id_success(self, mock_request):
        """Test that cross account request stores org_id."""
        client = APIClient()
        response = client.get(f"{URL_LIST}{self.request_2.request_id}/", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("email"), "test_user@email.com")
        self.assertEqual(response.data.get("target_org"), self.org_id)
        self.assertEqual(len(response.data.get("roles")), 2)

    @override_settings(PRINCIPAL_USER_DOMAIN="localhost")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_approval_replicates_bindings(self, replicate):
        """Test that when cross account access is approved, role bindings are replicated to Relations."""
        replicate.side_effect = self.replicator.replicate

        # Modify pending request such that it includes roles
        farmer = self.fixture.new_system_role("Farmer", ["farm:soil:rake"])
        fisher = self.fixture.new_system_role("Fisher", ["stream:fish:catch"])
        # Should not include this one
        self.fixture.new_system_role("NotGranted", ["app1:resource1:action1"])

        # Add roles to request for user 2222222 and approve it.
        self.add_roles_to_request(self.request_4, [farmer, fisher])

        # generated relations to approve request
        self.approve_request(self.request_4)

        # Check that the roles are now bound to the user in the target account (default workspace)
        default_workspace_id = Workspace.objects.default(tenant=self.tenant).id
        default_bindings = self.relations.find_tuples(
            # Tuples for bindings to the default workspace
            all_of(resource("rbac", "workspace", default_workspace_id), relation("binding"))
        )

        # Of these bindings, look for the ones that are for the user 2222222
        cross_account_bindings, _ = self.relations.find_group_with_tuples(
            # Tuples which are...
            # grouped by resource
            group_by=lambda t: (t.resource.type.namespace, t.resource.type.name, t.resource.id),
            # where the resource is one of the default role bindings...
            group_filter=lambda group: group[0] == "rbac"
            and group[1] == "role_binding"
            and group[2] in {str(binding.subject.subject.id) for binding in default_bindings},
            # and where one of the tuples from that binding has...
            predicates=[
                all_of(
                    # a subject relation
                    relation("subject"),
                    # to the user in the CAR
                    subject("rbac", "principal", "localhost/2222222"),
                ),
                relation("role"),
            ],
        )

        # Assert the roles are correct for these bindings – one per role that was included in the request,
        # and not any not included in the request
        self.assertEqual(len(cross_account_bindings), 2, "Missing bindings for cross access request roles")

        # Collect all the bound roles by iterating over the bindings and getting the subjects of the role relation
        bound_roles = {
            t.subject.subject.id
            for _, tuples in cross_account_bindings.items()
            for t in tuples
            if t.relation == "role"
        }

        # Assert the bindings are to roles with the same ID
        self.assertEqual(
            bound_roles,
            {str(farmer.uuid), str(fisher.uuid)},
            f"Expected roles {farmer.uuid} and {fisher.uuid} but got {bound_roles}",
        )

    @override_settings(PRINCIPAL_USER_DOMAIN="localhost")
    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_approval_and_deny_replicates_bindings(self, replicate):
        """Test that when cross account access is approved, role bindings are replicated to Relations."""
        replicate.side_effect = self.replicator.replicate

        # Modify pending request such that it includes roles
        farmer = self.fixture.new_system_role("Farmer", ["farm:soil:rake"])
        fisher = self.fixture.new_system_role("Fisher", ["stream:fish:catch"])
        # Should not include this one
        self.fixture.new_system_role("NotGranted", ["app1:resource1:action1"])

        # Add roles to request for user 2222222 and approve it.
        self.add_roles_to_request(self.request_4, [farmer, fisher])

        default_workspace_id = Workspace.objects.default(tenant=self.tenant).id
        previous_default_bindings = self.relations.find_tuples(
            # Tuples for bindings to the default workspace
            all_of(resource("rbac", "workspace", default_workspace_id), relation("binding"))
        )

        previous_subject_ids = {str(binding.subject.subject.id) for binding in previous_default_bindings}

        # generated relations to approve request
        self.approve_request(self.request_4)

        # Check that the roles are now bound to the user in the target account (default workspace)
        all_default_bindings = self.relations.find_tuples(
            # Tuples for bindings to the default workspace
            all_of(resource("rbac", "workspace", default_workspace_id), relation("binding"))
        )

        # Collect default bindings which were added by approving request
        default_bindings = []
        for binding in all_default_bindings:
            if str(binding.subject.subject.id) not in previous_subject_ids:
                default_bindings.append(binding)

        # Of these bindings, look for the ones that are for the user 2222222
        cross_account_bindings, _ = self.relations.find_group_with_tuples(
            # Tuples which are...
            # grouped by resource
            group_by=lambda t: (t.resource.type.namespace, t.resource.type.name, t.resource.id),
            # where the resource is one of the default role bindings...
            group_filter=lambda group: group[0] == "rbac"
            and group[1] == "role_binding"
            and group[2] in {str(binding.subject.subject.id) for binding in default_bindings},
            # and where one of the tuples from that binding has...
            predicates=[
                all_of(
                    # a subject relation
                    relation("subject"),
                    # to the user in the CAR
                    subject("rbac", "principal", "localhost/2222222"),
                ),
                relation("role"),
            ],
        )

        # Assert the roles are correct for these bindings – one per role that was included in the request,
        # and not any not included in the request
        self.assertEqual(len(cross_account_bindings), 2, "Missing bindings for cross access request roles")

        # Collect all the bound roles by iterating over the bindings and getting the subjects of the role relation
        bound_roles = {
            t.subject.subject.id
            for _, tuples in cross_account_bindings.items()
            for t in tuples
            if t.relation == "role"
        }

        # Assert the bindings are to roles with the same ID
        self.assertEqual(
            bound_roles,
            {str(farmer.uuid), str(fisher.uuid)},
            f"Expected roles {farmer.uuid} and {fisher.uuid} but got {bound_roles}",
        )

        self.deny_request(self.request_4)

        # Of these bindings, look for the ones that are for the user 2222222
        cross_account_bindings, _ = self.relations.find_group_with_tuples(
            # Tuples which are...
            # grouped by resource
            group_by=lambda t: (t.resource.type.namespace, t.resource.type.name, t.resource.id),
            # where the resource is one of the default role bindings...
            group_filter=lambda group: group[0] == "rbac"
            and group[1] == "role_binding"
            and group[2] in {str(binding.subject.subject.id) for binding in default_bindings},
            # and where one of the tuples from that binding has...
            predicates=[
                all_of(
                    # a subject relation
                    relation("subject"),
                    # to the user in the CAR
                    subject("rbac", "principal", "localhost/2222222"),
                ),
                relation("role"),
            ],
        )

        self.assertEqual(
            len(cross_account_bindings),
            0,
            f"Expected no cross account bindings, found {len(cross_account_bindings)}",
        )

        # Check that the roles are not now bound to the user in the target account (default workspace)
        all_default_bindings = self.relations.find_tuples(
            # Tuples for bindings to the default workspace
            all_of(resource("rbac", "workspace", default_workspace_id), relation("binding"))
        )

        default_bindings = []
        for binding in all_default_bindings:
            if str(binding.subject.subject.id) not in previous_subject_ids:
                default_bindings.append(binding)

        self.assertEqual(len(default_bindings), 0, "Default bindings for cross access request roles still exists")

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
                    "account_number": "567890",
                    "user_id": "1111111",
                },
                {
                    "username": "test_user_2",
                    "email": "test_user_2@email.com",
                    "first_name": "user_2",
                    "last_name": "test",
                    "account_number": "123456",
                    "user_id": "2222222",
                },
            ],
        },
    )
    def test_list_requests_query_limit_offset(self, mock_request):
        """Test limit and offset when listing the cross account requests."""
        client = APIClient()
        default_limit = 10
        default_offset = 0
        # Test that default values for limit and offset are returned for
        # query with no limit and offset
        response = client.get(URL_LIST, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 4)
        self.assertEqual(response.data.get("meta").get("limit"), default_limit)
        self.assertEqual(response.data.get("meta").get("offset"), default_offset)

        # Test that default values for limit and offset are returned for
        # query with invalid limit or offset
        for limit, offset in [("xxx", "xxx"), ("", "")]:
            url = f"{URL_LIST}?limit={limit}&offset={offset}"
            response = client.get(url, **self.headers)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["data"]), 4)
            self.assertEqual(response.data.get("meta").get("limit"), default_limit)
            self.assertEqual(response.data.get("meta").get("offset"), default_offset)

        # Test that correct values for limit and offset are returned for
        # query with specific limit or offset
        url = f"{URL_LIST}?limit={2}&offset={2}"
        response = client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)
        self.assertEqual(response.data.get("meta").get("limit"), 2)
        self.assertEqual(response.data.get("meta").get("offset"), 2)
