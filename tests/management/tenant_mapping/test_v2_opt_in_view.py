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
import contextlib
from typing import Optional
from unittest.mock import ANY

from django.test import override_settings
from django.urls import reverse
from management.tenant_mapping.model import TenantMapping
from management.tenant_mapping.v2_activation import is_v2_opted_in, set_v2_opt_in_state
from management.tenant_mapping.v2_opt_in_view import (
    rbac_v2_optin_eligibility_requests_total,
    rbac_v2_optin_status_requests_total,
    rbac_v2_optin_total,
)
from prometheus_client import Counter
from rest_framework import status
from rest_framework.test import APIClient
from tests.identity_request import IdentityRequest
from tests.management.role.test_dual_write import RbacFixture
from tests.v2_util import bootstrap_tenant_for_v2_test


@override_settings(ATOMIC_RETRY_DISABLED=True)
@override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["ineligible", "bad"])
class OptInViewSetTest(IdentityRequest):
    def setUp(self):
        super().setUp()

        self.status_url = reverse("v1_management:opt-in")
        self.eligibility_url = reverse("v1_management:opt-in-eligibility")

        self.non_admin_headers = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)[
            "request"
        ].META

        self.client = APIClient()
        self.fixture = RbacFixture()

        bootstrap_tenant_for_v2_test(self.tenant)

    def _debootstrap(self):
        TenantMapping.objects.filter(tenant=self.tenant).delete()

    def _send_opt_in_request(self, headers: Optional[dict] = None, body: Optional[dict] = None):
        if headers is None:
            headers = self.headers

        if body is None:
            body = {"v2_opted_in": True}

        return self.client.patch(self.status_url, body, content_type="application/json", **headers)

    def _get_eligibility(self, headers: Optional[dict] = None):
        if headers is None:
            headers = self.headers

        return self.client.get(self.eligibility_url, **headers)

    def _assert_status(self, opted_in: bool):
        metric_result = "opted-in" if opted_in else "not-opted-in"

        with self._assert_increment(rbac_v2_optin_status_requests_total, result=metric_result):
            response = self.client.get(self.status_url, **self.non_admin_headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"v2_opted_in": opted_in})
        self.assertEqual(response.headers["Cache-Control"], "max-age=120, private")

    def _opt_in_and_assert_success(self):
        with self._assert_increment(rbac_v2_optin_total, result="success"):
            response = self._send_opt_in_request()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"v2_opted_in": True})

    @contextlib.contextmanager
    def _assert_increment(self, metric: Counter, **labels: str):
        labelled = metric.labels(**labels)
        labelled.reset()

        self.assertEqual(0, list(labelled.collect())[0].samples[0].value)
        yield
        self.assertEqual(1, list(labelled.collect())[0].samples[0].value)

    def test_unbootstrapped_status(self):
        self._debootstrap()
        self._assert_status(False)

    def test_bootstrapped_initial_status(self):
        self._assert_status(False)

    def test_opted_in_status(self):
        set_v2_opt_in_state(self.tenant, True)
        self._assert_status(True)

    def test_non_admin_opt_in_prohibited(self):
        response = self._send_opt_in_request(headers=self.non_admin_headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unbootstrapped_opt_in(self):
        self._debootstrap()

        with self._assert_increment(rbac_v2_optin_total, result="ineligible"):
            response = self._send_opt_in_request()

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(
            response.data,
            {"eligible": False, "bootstrapped": False, "ineligible_custom_roles": [], "ineligible_groups": []},
        )

    def test_opt_in_eligible(self):
        group = self.fixture.new_group("a group", self.tenant, ["p1"])[0]
        system_role = self.fixture.new_system_role("system role", ["eligible:*:*"])
        custom_role = self.fixture.new_custom_role(
            "custom role", self.fixture.workspace_access(["eligible:*:*"]), self.tenant
        )

        self.fixture.add_role_to_group(system_role, group)
        self.fixture.add_role_to_group(custom_role, group)

        self._opt_in_and_assert_success()
        self.assertTrue(is_v2_opted_in(self.tenant))

    def test_opt_in_idempotent(self):
        self._opt_in_and_assert_success()
        self._opt_in_and_assert_success()
        self.assertTrue(is_v2_opted_in(self.tenant))

    def _assert_ineligible_response(self, request_fn, status_code: int):
        group = self.fixture.new_group("a group", self.tenant, ["p1"])[0]
        system_role = self.fixture.new_system_role("system role", ["ineligible:*:*", "bad:*:*"])
        custom_role = self.fixture.new_custom_role(
            "custom role", self.fixture.workspace_access(["ineligible:*:*"]), self.tenant
        )

        self.fixture.add_role_to_group(system_role, group)
        self.fixture.add_role_to_group(custom_role, group)

        response = request_fn()

        self.assertEqual(response.status_code, status_code)
        self.assertEqual(response.data["eligible"], False)
        self.assertEqual(response.data["bootstrapped"], True)

        self.assertEqual(
            response.data["ineligible_groups"],
            [
                {
                    "name": group.name,
                    "uuid": str(group.uuid),
                    "ineligible_system_roles": [
                        {
                            "name": system_role.name,
                            "uuid": str(system_role.uuid),
                            "ineligible_applications": ANY,
                        }
                    ],
                }
            ],
        )

        self.assertCountEqual(
            ["ineligible", "bad"],
            response.data["ineligible_groups"][0]["ineligible_system_roles"][0]["ineligible_applications"],
        )

        self.assertEqual(
            response.data["ineligible_custom_roles"],
            [{"name": custom_role.name, "uuid": str(custom_role.uuid), "ineligible_applications": ["ineligible"]}],
        )

        return response

    def test_opt_in_ineligible(self):
        self._assert_ineligible_response(self._send_opt_in_request, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self._assert_status(False)

    def test_opt_in_empty_body(self):
        with self._assert_increment(rbac_v2_optin_total, result="noop-request"):
            response = self._send_opt_in_request(body={})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"v2_opted_in": False})

        self._assert_status(False)

    def test_opt_out_prohibited(self):
        with self._assert_increment(rbac_v2_optin_total, result="invalid-request"):
            response = self._send_opt_in_request(body={"v2_opted_in": False})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("v2_opted_in, if present, can only be set to true", response.content.decode())

        self._assert_status(False)

    def test_opt_in_null_prohibited(self):
        with self._assert_increment(rbac_v2_optin_total, result="invalid-request"):
            response = self._send_opt_in_request(body={"v2_opted_in": None})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("v2_opted_in, if present, can only be set to true", response.content.decode())

        self._assert_status(False)

    def test_eligibility_non_admin_prohibited(self):
        response = self._get_eligibility(headers=self.non_admin_headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_eligibility_eligible(self):
        with self._assert_increment(rbac_v2_optin_eligibility_requests_total, result="eligible"):
            response = self._get_eligibility()

        self.assertEqual(response.data, {"eligible": True})
        self.assertNotIn("Cache-Control", response.headers)

    def test_eligibility_opted_in(self):
        self._opt_in_and_assert_success()

        with self._assert_increment(rbac_v2_optin_eligibility_requests_total, result="eligible"):
            response = self._get_eligibility()

        self.assertEqual(response.data, {"eligible": True})
        self.assertNotIn("Cache-Control", response.headers)

    def test_eligibility_ineligible(self):
        with self._assert_increment(rbac_v2_optin_eligibility_requests_total, result="ineligible"):
            response = self._assert_ineligible_response(self._get_eligibility, status.HTTP_200_OK)

        self._assert_status(False)
        self.assertNotIn("Cache-Control", response.headers)
