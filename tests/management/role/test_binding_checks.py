import uuid
from typing import Callable, Iterable
from unittest.mock import patch

from api.models import Tenant
from django.test import TestCase, override_settings
from management.relation_replicator.types import ObjectType
from management.role.binding_checks import (
    UnconstrainedBindingAuthorizationPolicy,
    EnumeratedBindingAuthorizationPolicy,
    check_binding_resources,
    BindingCheckResult,
    UnauthorizedResourceError,
    commit_binding_policy,
)
from management.role.resource_definitions import ParsedAttributeFilter
from management.workspace.model import Workspace
from migration_tool.models import V2boundresource
from tests.v2_util import bootstrap_tenant_for_v2_test


class BindingAuthorizationPolicyTest(TestCase):
    def test_unconstrained(self):
        policy = UnconstrainedBindingAuthorizationPolicy()
        self.assertTrue(policy.can_bind_to(V2boundresource(("rbac", "workspace"), str(uuid.uuid4()))))

    def test_enumerated(self):
        workspace_a = V2boundresource(("rbac", "workspace"), str(uuid.uuid4()))
        workspace_b = V2boundresource(("rbac", "workspace"), str(uuid.uuid4()))
        workspace_c = V2boundresource(("rbac", "workspace"), str(uuid.uuid4()))
        tenant_a = V2boundresource(("rbac", "tenant"), Tenant.org_id_to_tenant_resource_id("some_org"))
        tenant_b = V2boundresource(("rbac", "tenant"), Tenant.org_id_to_tenant_resource_id("another_org"))

        policy = EnumeratedBindingAuthorizationPolicy([workspace_a, workspace_b, tenant_a])

        self.assertTrue(policy.can_bind_to(workspace_a))
        self.assertTrue(policy.can_bind_to(workspace_b))
        self.assertFalse(policy.can_bind_to(workspace_c))
        self.assertTrue(policy.can_bind_to(tenant_a))
        self.assertFalse(policy.can_bind_to(tenant_b))


_workspace_type = ObjectType("rbac", "workspace")


@override_settings(SKIP_RESOURCE_BINDING_CHECKS=False)
class CheckBindingResourcesTest(TestCase):
    _allow_predicate: Callable[[V2boundresource], bool]
    tenant: Tenant

    def setUp(self):
        super().setUp()

        self._allow_predicate = lambda _: False
        self.tenant = Tenant.objects.create(tenant_name="test-org", org_id="test-org")
        bootstrap_tenant_for_v2_test(self.tenant)

        def do_check(org_id: str, resources: Iterable[V2boundresource]):
            self.assertEqual(org_id, self.tenant.org_id)

            return {r: self._allow_predicate(r) for r in set(resources)}

        ctx = self.enterContext(
            patch("management.inventory_checker.inventory_api_check.ResourceTenantInventoryChecker.check_resources")
        )
        ctx.side_effect = do_check

    def _access_for(self, attribute_filters: list[dict]) -> list[dict]:
        return [
            {
                "permission": "rbac:*:*",
                "resourceDefinitions": list(attribute_filters),
            }
        ]

    def _do_check_for(self, filters: list[ParsedAttributeFilter]) -> BindingCheckResult:
        return check_binding_resources(org_id=self.tenant.org_id, filters=filters)

    def test_empty(self):
        self._allow_predicate = lambda _: False

        result = self._do_check_for([])
        self.assertEqual(BindingCheckResult(named_resources=frozenset(), ungrouped_hosts_workspace=False), result)

    def test_workspace_allowed(self):
        workspace = V2boundresource(("rbac", "workspace"), str(uuid.uuid4()))
        self._allow_predicate = lambda r: r == workspace

        result = self._do_check_for(
            [ParsedAttributeFilter(resource_type=_workspace_type, valid_ids={workspace.resource_id})]
        )

        self.assertEqual(
            BindingCheckResult(named_resources=frozenset({workspace}), ungrouped_hosts_workspace=False), result
        )

    def test_ungrouped_hosts_workspace(self):
        workspace = V2boundresource(("rbac", "workspace"), str(uuid.uuid4()))
        self._allow_predicate = lambda r: r == workspace

        result = self._do_check_for(
            [ParsedAttributeFilter(resource_type=_workspace_type, valid_ids={workspace.resource_id, None})]
        )

        self.assertEqual(
            BindingCheckResult(named_resources=frozenset({workspace}), ungrouped_hosts_workspace=True), result
        )

    def test_disallowed(self):
        workspace = V2boundresource(("rbac", "workspace"), str(uuid.uuid4()))
        self._allow_predicate = lambda _: False

        with self.assertRaises(UnauthorizedResourceError) as ctx:
            self._do_check_for(
                [ParsedAttributeFilter(resource_type=_workspace_type, valid_ids={workspace.resource_id})]
            )

        self.assertIn(str(workspace), str(ctx.exception))

    def test_non_workspace_null(self):
        self._allow_predicate = lambda _: False

        with self.assertRaises(TypeError) as ctx:
            self._do_check_for([ParsedAttributeFilter(resource_type=ObjectType("rbac", "tenant"), valid_ids={None})])

        self.assertEqual("None is only a valid ID for workspaces", str(ctx.exception))


class CommitBindingPolicyTest(TestCase):
    def setUp(self):
        super().setUp()
        self.maxDiff = None

        self.tenant = Tenant.objects.create(tenant_name="a tenant", org_id="test_tenant")

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

    def _do_commit_policy(self, check_result: BindingCheckResult):
        return commit_binding_policy(self.tenant, check_result)

    def _built_in_resources(self) -> set[V2boundresource]:
        return {
            V2boundresource(("rbac", "tenant"), self.tenant.tenant_resource_id()),
            V2boundresource(("rbac", "workspace"), str(self.default_workspace.id)),
            V2boundresource(("rbac", "workspace"), str(self.root_workspace.id)),
        }

    def test_empty(self):
        self.assertEqual(
            self._do_commit_policy(BindingCheckResult(named_resources=frozenset(), ungrouped_hosts_workspace=False)),
            EnumeratedBindingAuthorizationPolicy(self._built_in_resources()),
        )

    def test_named_resource(self):
        host = V2boundresource(("hbi", "host"), str(uuid.uuid4()))

        self.assertEqual(
            self._do_commit_policy(
                BindingCheckResult(named_resources=frozenset({host}), ungrouped_hosts_workspace=False)
            ),
            EnumeratedBindingAuthorizationPolicy({*self._built_in_resources(), host}),
        )

    def test_ungrouped_hosts(self):
        host = V2boundresource(("hbi", "host"), str(uuid.uuid4()))

        policy = self._do_commit_policy(
            BindingCheckResult(named_resources=frozenset({host}), ungrouped_hosts_workspace=True)
        )

        # An ungrouped hosts workspace should have been created, since it was requested.
        ungrouped_hosts_ws = Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.UNGROUPED_HOSTS).get()

        self.assertEqual(
            policy,
            EnumeratedBindingAuthorizationPolicy(
                {
                    *self._built_in_resources(),
                    V2boundresource(("rbac", "workspace"), str(ungrouped_hosts_ws.id)),
                    host,
                }
            ),
        )
