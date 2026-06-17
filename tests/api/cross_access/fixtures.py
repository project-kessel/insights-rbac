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
"""Fixtures for testing cross account requests."""

from api.models import CrossAccountRequest, Tenant
from django.urls import reverse
from django.utils import timezone
from management.models import Role
from rest_framework import status
from rest_framework.test import APIClient

from datetime import timedelta

from management.principal.model import Principal
from management.tenant_service.v2 import V2TenantBootstrapService
from migration_tool.in_memory_tuples import InMemoryRelationReplicator, InMemoryTuples

from tests.identity_request import IdentityRequest
from tests.management.role.test_dual_write import RbacFixture

URL_LIST = reverse("v1_api:cross-list")


class CrossAccountRequestTest(IdentityRequest):
    """Test the cross account request view."""

    def format_date(self, date):
        return date.strftime("%m/%d/%Y")

    def setUp(self):
        """Set up the cross account request for tests."""
        super().setUp()

        self.relations = InMemoryTuples()
        self.replicator = InMemoryRelationReplicator(self.relations)
        self.fixture = RbacFixture(V2TenantBootstrapService(InMemoryRelationReplicator(self.relations)))

        self.ref_time = timezone.now()
        self.account = self.customer_data["account_id"]
        self.org_id = self.customer_data["org_id"]
        self.associate_non_admin_request_context = self._create_request_context(
            self.customer_data, self.user_data, is_org_admin=False, is_internal=True
        )
        self.associate_non_admin_request = self.associate_non_admin_request_context["request"]

        self.not_anemic_customer_data = self._create_customer_data()
        self.not_anemic_account = "21112"
        self.not_anemic_org_id = self.not_anemic_customer_data["org_id"]
        self.not_anemic_customer_data["account_id"] = self.not_anemic_account
        self.not_anemic_customer_data["tenant_name"] = f"acct{self.not_anemic_account}"
        self.associate_not_anemic_request_context = self._create_request_context(
            self.not_anemic_customer_data, self.user_data, is_org_admin=False, is_internal=True
        )
        self.associate_not_anemic_request = self.associate_not_anemic_request_context["request"]
        self.not_anemic_headers = self.associate_not_anemic_request_context["request"].META

        self.associate_admin_request_context = self._create_request_context(
            self.customer_data, self.user_data, is_org_admin=True, is_internal=True
        )
        self.associate_admin_request = self.associate_admin_request_context["request"]

        """
            Create cross account requests 1 to 6: request_1 to request_6
            self.associate_admin_request has user_id 1111111, and org_id = self.org_id
            It would be approver for request_1, request_2, request_5;
            It would be requestor for request_3, request_6
            |  target_org     | user_id | start_date | end_date  |  status  | roles |
            |  self.org_id    | 1111111 |    now     | now+10day | approved |       |
            |  self.org_id    | 2222222 |    now     | now+10day | pending  |       |
            |  another_org_id | 1111111 |    now     | now+10day | approved |       |
            |  self.org_id    | 2222222 |    now     | now+10day | pending  |       |
            |  self.org_id    | 2222222 |    now     | now+10day | expired  |       |
            |  another_org_id | 1111111 |    now     | now_10day | pending  |       |
        """

        self.another_account = "123456"
        self.another_org_id = "54321"

        self.data4create = {
            "target_org": "054321",
            "start_date": self.format_date(self.ref_time),
            "end_date": self.format_date(self.ref_time + timedelta(90)),
            "roles": ["role_1", "role_2"],
        }

        public_tenant = Tenant.objects.get(tenant_name="public")

        tenant_for_target_org = Tenant.objects.create(
            tenant_name=f"org{self.data4create['target_org']}",
            org_id=self.data4create["target_org"],
        )
        tenant_for_target_org.ready = True
        tenant_for_target_org.save()
        self.fixture.bootstrap_tenant(tenant_for_target_org)

        self.role_1 = self.fixture.new_system_role(name="role_1")
        self.role_2 = self.fixture.new_system_role(name="role_2")
        self.role_9 = self.fixture.new_system_role(name="role_9")
        self.role_8 = self.fixture.new_system_role(name="role_8")

        Principal.objects.create(tenant=public_tenant, username="1111111", user_id="1111111")
        Principal.objects.create(tenant=public_tenant, username="2222222", user_id="2222222")

        self.request_1 = CrossAccountRequest.objects.create(
            target_org=self.org_id,
            user_id="1111111",
            end_date=self.ref_time + timedelta(10),
            status="approved",
        )
        self.request_1.roles.add(*(self.role_1, self.role_2))
        self.request_2 = CrossAccountRequest.objects.create(
            target_org=self.org_id,
            user_id="2222222",
            end_date=self.ref_time + timedelta(10),
        )
        self.request_2.roles.add(*(self.role_1, self.role_2))
        self.request_3 = CrossAccountRequest.objects.create(
            target_org=self.another_org_id,
            user_id="1111111",
            end_date=self.ref_time + timedelta(10),
            status="approved",
        )
        self.request_4 = CrossAccountRequest.objects.create(
            target_org=self.org_id,
            user_id="2222222",
            end_date=self.ref_time + timedelta(10),
            status="pending",
        )
        self.request_5 = CrossAccountRequest.objects.create(
            target_org=self.org_id,
            user_id="2222222",
            end_date=self.ref_time + timedelta(10),
            status="expired",
        )
        self.request_6 = CrossAccountRequest.objects.create(
            target_org=self.another_org_id,
            user_id="1111111",
            end_date=self.ref_time + timedelta(10),
            status="pending",
        )
        self.not_anemic_request_1 = CrossAccountRequest.objects.create(
            target_org=self.not_anemic_org_id,
            user_id="1111111",
            end_date=self.ref_time + timedelta(10),
            status="approved",
        )
        # Assume tenant bootstrapped for v2
        self.fixture.bootstrap_tenant(self.tenant)

        not_anemic_tenant = Tenant.objects.create(
            tenant_name=f"acct{self.not_anemic_account}",
            account_id=self.not_anemic_account,
            org_id=self.not_anemic_org_id,
        )

        self.fixture.bootstrap_tenant(not_anemic_tenant)

    def tearDown(self):
        """Tear down cross account request model tests."""
        CrossAccountRequest.objects.all().delete()

    def add_roles_to_request(self, request: CrossAccountRequest, roles: list):
        request.roles.add(*roles)

    def approve_request(self, request: CrossAccountRequest):
        update_data = {"status": "approved"}
        car_uuid = request.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})
        client = APIClient()
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response

    def deny_request(self, request: CrossAccountRequest):
        update_data = {"status": "denied"}
        car_uuid = request.request_id
        url = reverse("v1_api:cross-detail", kwargs={"pk": str(car_uuid)})
        client = APIClient()
        response = client.patch(url, update_data, format="json", **self.associate_admin_request.META)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response
