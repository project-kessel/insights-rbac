# Copyright 2025 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Tests for base viewset for v2 APIs."""

from django.test.utils import override_settings
from django.urls import clear_url_caches, reverse
from importlib import reload
from rest_framework.test import APIClient

from management.base_viewsets import BaseV2ViewSet
from management.models import Workspace
from rbac import urls
from tests.identity_request import IdentityRequest


@override_settings(V2_APIS_ENABLED=True)
class BaseV2ViewSetTest(IdentityRequest):
    """Test the BaseV2ViewSet overrides."""

    def setUp(self):
        self.c = Workspace.objects.create(name="C", type="root", tenant=self.tenant)
        self.a = Workspace.objects.create(name="A", type="default", parent=self.c, tenant=self.tenant)
        self.b1 = Workspace.objects.create(
            name="B1", description="B1", type="standard", parent=self.a, tenant=self.tenant
        )
        self.b2 = Workspace.objects.create(
            name="B2", description="B2", type="standard", parent=self.a, tenant=self.tenant
        )

        reload(urls)
        clear_url_caches()
        super().setUp()
        # Make them admin
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=True)

        request = request_context["request"]
        self.headers = request.META

    def test_renderer_classes(self):
        """Test default renderers."""
        renderers = [klass().__class__.__name__ for klass in BaseV2ViewSet.renderer_classes]
        self.assertCountEqual(renderers, ["JSONRenderer", "ProblemJSONRenderer"])

    def test_base_queryset_default_ordering(self):
        """Test get_queryset default ordering."""
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(url, None, format="json", **self.headers)
        payload = response.data
        ordered_payload_ids = [w["id"] for w in payload["data"]]
        ordered_workspace_ids = [str(self.a.id), str(self.b1.id), str(self.b2.id), str(self.c.id)]
        self.assertEqual(ordered_payload_ids, ordered_workspace_ids)

    def test_base_queryset_ordering_when_modified(self):
        """Test get_queryset default ordering when modified."""
        self.b1.save()  # modify workspace b1
        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(url, None, format="json", **self.headers)
        payload = response.data
        ordered_payload_ids = [w["id"] for w in payload["data"]]
        ordered_workspace_ids = [str(self.a.id), str(self.b1.id), str(self.b2.id), str(self.c.id)]
        self.assertEqual(ordered_payload_ids, ordered_workspace_ids)
