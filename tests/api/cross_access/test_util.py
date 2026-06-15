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
"""Test the cross access util module."""

from api.cross_access import util
from api.models import CrossAccountRequest, Tenant
from api.cross_access.util import get_cross_principal_name
from django.urls import reverse
from django.utils import timezone
from management.models import Role, Principal
from management.notifications.notification_handlers import EVENT_TYPE_RH_TAM_REQUEST_CREATED
from rest_framework import status
from rest_framework.test import APIClient

from datetime import timedelta
from unittest.mock import patch
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

URL_LIST = reverse("v1_api:cross-list")


@override_settings(PRINCIPAL_USER_DOMAIN="localhost", ATOMIC_RETRY_DISABLED=True)
class CrossAccountRequestUtilTests(CrossAccountRequestTest):
    """Test the cross access util module."""

    def setUp(self):
        """Set up the cross account request for tests."""
        super().setUp()

        self.another_tenant_data = {
            "target_org": self.another_org_id,
            "start_date": self.format_date(self.ref_time),
            "end_date": self.format_date(self.ref_time + timedelta(90)),
            "roles": ["role_1", "role_2"],
        }

        self.another_tenant = Tenant.objects.create(
            tenant_name=f"acct{self.another_account}",
            account_id=self.another_account,
            org_id=self.another_tenant_data["target_org"],
        )
        self.another_tenant.ready = True
        self.another_tenant.save()
        self.fixture.bootstrap_tenant(self.another_tenant)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_expired_cross_account_requests_remove_bindings(self, replicate):
        """Test that there are no bindings after a CAR expires."""

        replicate.side_effect = self.replicator.replicate

        # Modify pending request such that it includes roles
        farmer = self.fixture.new_system_role("Farmer", ["farm:soil:rake"])
        fisher = self.fixture.new_system_role("Fisher", ["stream:fish:catch"])

        # Add roles to request for user 2222222 and approve it.
        self.add_roles_to_request(self.request_4, [farmer, fisher])
        self.approve_request(self.request_4)

        after_expiration = self.request_4.end_date + timedelta(seconds=1)

        with patch("django.utils.timezone.now", return_value=after_expiration):
            util.check_cross_request_expiry()

        # Check that the bindings for the tenant's resources are gone
        # Check that the roles are now bound to the user in the target account (default workspace)
        default_workspace_id = Workspace.objects.default(tenant=self.tenant).id
        default_bindings = self.relations.find_tuples(
            # Tuples for bindings to the default workspace
            all_of(resource("rbac", "workspace", default_workspace_id), relation("binding"))
        )

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

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_expired_cross_account_request_does_not_remove_binding_if_granted_by_another_request(self, replicate):
        replicate.side_effect = self.replicator.replicate

        # Modify pending request such that it includes roles
        farmer = self.fixture.new_system_role("Farmer", ["farm:soil:rake"])
        fisher = self.fixture.new_system_role("Fisher", ["stream:fish:catch"])

        # Add roles to request for user 2222222 and approve it.
        self.add_roles_to_request(self.request_4, [farmer, fisher])
        self.approve_request(self.request_4)

        # Add one of the same roles to another request for the same user and target org and approve
        # but this one expires later
        additional_request = CrossAccountRequest.objects.create(
            target_org=self.request_4.target_org,
            user_id="2222222",
            end_date=self.request_4.end_date + timedelta(days=10),
            status="pending",
        )
        self.add_roles_to_request(additional_request, [farmer])
        self.approve_request(additional_request)

        after_expiration = self.request_4.end_date + timedelta(seconds=1)

        with patch("django.utils.timezone.now", return_value=after_expiration):
            util.check_cross_request_expiry()

        # Check that the bindings for the tenant's resources are gone
        # Check that the roles are now bound to the user in the target account (default workspace)
        default_workspace_id = Workspace.objects.default(tenant=self.tenant).id
        default_bindings = self.relations.find_tuples(
            # Tuples for bindings to the default workspace
            all_of(resource("rbac", "workspace", default_workspace_id), relation("binding"))
        )

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
            1,
            f"Expected remaining cross account binding, found {len(cross_account_bindings)}",
        )

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
    def test_expired_cross_account_request_does_not_remove_bindings_if_granted_by_another_request_multiple_roles(
        self, replicate
    ):
        replicate.side_effect = self.replicator.replicate

        # Modify pending request such that it includes roles
        farmer = self.fixture.new_system_role("Farmer", ["farm:soil:rake"])
        fisher = self.fixture.new_system_role("Fisher", ["stream:fish:catch"])

        # Add roles to request for user 2222222 and approve it.
        self.add_roles_to_request(self.request_4, [farmer, fisher])
        self.approve_request(self.request_4)

        # Add one of the same roles to another request for the same user and target org and approve
        # but this one expires later
        additional_request = CrossAccountRequest.objects.create(
            target_org=self.request_4.target_org,
            user_id="2222222",
            end_date=self.request_4.end_date + timedelta(days=10),
            status="pending",
        )
        self.add_roles_to_request(additional_request, [farmer, fisher])
        self.approve_request(additional_request)

        after_expiration = self.request_4.end_date + timedelta(seconds=1)

        with patch("django.utils.timezone.now", return_value=after_expiration):
            util.check_cross_request_expiry()

        # Check that the bindings for the tenant's resources are gone
        # Check that the roles are now bound to the user in the target account (default workspace)
        default_workspace_id = Workspace.objects.default(tenant=self.tenant).id
        default_bindings = self.relations.find_tuples(
            # Tuples for bindings to the default workspace
            all_of(resource("rbac", "workspace", default_workspace_id), relation("binding"))
        )

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
            2,
            f"Expected 2 cross account binding, found {len(cross_account_bindings)}",
        )

    def tearDown(self):
        """Tear down cross account request model tests."""
