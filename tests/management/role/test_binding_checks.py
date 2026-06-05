import uuid


from api.models import Tenant
from django.test import TestCase
from management.role.binding_checks import (
    EnumeratedBindingAuthorizationPolicy,
    UnconstrainedBindingAuthorizationPolicy,
)
from migration_tool.models import V2boundresource


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
