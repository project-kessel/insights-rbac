import uuid
from typing import Callable
from unittest.mock import patch, MagicMock

import kessel.inventory.v1beta2.check_response_pb2
import kessel.inventory.v1beta2.allowed_pb2
from django.test import TestCase

from api.models import Tenant
from management.inventory_checker.inventory_api_check import ResourceTenantInventoryChecker
from migration_tool.models import V2boundresource


class ResourceTenantInventoryCheckerTest(TestCase):
    _allow_predicate: Callable[[V2boundresource], bool]
    checker: ResourceTenantInventoryChecker

    org_id: str = "test-org"

    workspace_a = V2boundresource(("rbac", "workspace"), "workspace-a")
    workspace_b = V2boundresource(("rbac", "workspace"), "workspace-b")
    workspace_c = V2boundresource(("rbac", "workspace"), "workspace-c")

    tenant_a = V2boundresource(("rbac", "tenant"), Tenant.org_id_to_tenant_resource_id("an-org"))
    tenant_b = V2boundresource(("rbac", "tenant"), Tenant.org_id_to_tenant_resource_id("another-org"))

    def setUp(self):
        super().setUp()
        self.maxDiff = None

        self._allow_predicate = lambda _: False

        def do_check(request):
            self.assertEqual(request.relation, "tenant")
            self.assertEqual(request.subject.resource.reporter.type, "rbac")
            self.assertEqual(request.subject.resource.resource_type, "tenant")
            self.assertEqual(request.subject.resource.resource_id, Tenant.org_id_to_tenant_resource_id(self.org_id))

            is_allowed = self._allow_predicate(
                V2boundresource(
                    (request.object.reporter.type, request.object.resource_type), request.object.resource_id
                )
            )

            return kessel.inventory.v1beta2.check_response_pb2.CheckResponse(
                allowed=(
                    kessel.inventory.v1beta2.allowed_pb2.ALLOWED_TRUE
                    if is_allowed
                    else kessel.inventory.v1beta2.allowed_pb2.ALLOWED_FALSE
                )
            )

        self.check_mock = MagicMock()
        self.check_mock.side_effect = do_check

        stub_mock = MagicMock()
        stub_mock.Check = self.check_mock

        context = self.enterContext(
            patch("kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub")
        )
        context.side_effect = lambda _channel: stub_mock

        self.checker = ResourceTenantInventoryChecker()

    def _do_check(self, resources: list[V2boundresource]) -> dict[V2boundresource, bool]:
        return self.checker.check_resources(org_id=self.org_id, resources=resources)

    def test_disallowed(self):
        self._allow_predicate = lambda _: False

        self.assertEqual(
            self._do_check([self.workspace_a, self.tenant_a]),
            {
                self.workspace_a: False,
                self.tenant_a: False,
            },
        )

    def test_allowed(self):
        self._allow_predicate = lambda _: True

        self.assertEqual(
            self._do_check([self.workspace_a, self.tenant_a]),
            {
                self.workspace_a: True,
                self.tenant_a: True,
            },
        )

    def test_conditional(self):
        self._allow_predicate = lambda r: r in (self.workspace_a, self.workspace_b, self.tenant_a)

        self.assertEqual(
            self._do_check([self.workspace_a, self.workspace_b, self.workspace_c, self.tenant_a, self.tenant_b]),
            {
                self.workspace_a: True,
                self.workspace_b: True,
                self.workspace_c: False,
                self.tenant_a: True,
                self.tenant_b: False,
            },
        )

    def test_duplicates(self):
        self._allow_predicate = lambda r: True

        self.assertEqual(
            self._do_check([self.workspace_a, self.tenant_a] * 10), {self.workspace_a: True, self.tenant_a: True}
        )
        self.assertEqual(self.check_mock.call_count, 2)
