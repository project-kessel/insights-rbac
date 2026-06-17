# Testing Guidelines

Rules and patterns for writing tests in insights-rbac. This is a reference for contributors and agents, not a tutorial.

## Running Tests

Django's test runner requires **dotted module paths**, not file paths. Use `tox -r` to match CI (Tekton pipeline).

```bash
# Run all environments (lint + tests + mypy), same as CI
tox -r

# All tests without coverage (fast feedback loop)
tox -r -e py312-fast

# All tests with coverage
tox -r -e py312

# Specific module / class / method
tox -r -e py312-fast -- tests.management.workspace.test_view
tox -r -e py312-fast -- tests.management.workspace.test_view.WorkspaceTestsList
tox -r -e py312-fast -- tests.management.workspace.test_view.WorkspaceTestsList.test_workspace_list_unfiltered

# Profile slowest tests
tox -r -e py312-profile

# Lint + format check
tox -r -e lint
```

Both `py312` and `py312-fast` run with `--failfast`. The `py312` env uses `--parallel 4` (4 workers) and collects branch coverage (omits migrations). The `py312-fast` env uses `--parallel` (auto worker count) and skips coverage.

## Test Base Classes

All test classes that need a tenant and identity header inherit from one of these (in `tests/identity_request.py`):

| Base class | Django class | When to use |
|---|---|---|
| `IdentityRequest` | `TestCase` | Default for most tests |
| `TransactionalIdentityRequest` | `TransactionTestCase` | Tests using `pgtransaction.atomic(retry=...)` or `select_for_update` |

Note: Some test files define their own local `TransactionIdentityRequest` class (without "al") as an alias. Both naming patterns exist in the codebase.

Both apply `@override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat")`.

**What they provide:**
- `cls.tenant` -- a ready Tenant with random `account_id`, `org_id`, `tenant_name`
- `cls.customer_data` -- dict with `account_id`, `tenant_name`, `org_id`
- `cls.user_data` -- dict with unique `username`, `email`, `user_id`
- `cls.headers` -- dict containing the base64-encoded `x-rh-identity` header (org admin by default)
- `cls.request_context` -- dict with a Mock `request` carrying the identity header
- `tearDown` flushes Redis / disables `PRINCIPAL_CACHE` for isolation

**Do not** use plain `TestCase` for tests that need tenant-scoped data -- always use `IdentityRequest`.

## Authentication in Tests

Identity headers are base64-encoded JSON built by `_create_request_context`. Use it to create non-admin or internal users:

```python
# Non-admin user
ctx = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
headers = ctx["request"].META

# Internal/associate user
ctx = self._create_request_context(self.customer_data, self.user_data, is_internal=True)

# Service account (no user_data)
sa_data = self._create_service_account_data()
ctx = self._create_request_context(self.customer_data, service_account_data=sa_data)

# Cross-account
ctx = self._create_request_context(self.customer_data, self.user_data, cross_account=True)
```

Pass headers to API client: `client.post(url, data, format="json", **headers)` or `**self.headers` for org-admin.

## Making API Requests

Use DRF's `APIClient`. Two patterns exist in the codebase:

```python
# Pattern 1: pass headers per request (most common)
client = APIClient()
response = client.get(url, **self.headers)

# Pattern 2: set credentials once on client (used in some v2 tests)
self.client = APIClient()
self.client.credentials(HTTP_X_RH_IDENTITY=self.headers.get("HTTP_X_RH_IDENTITY"))
response = self.client.get(url)
```

Use `reverse()` for URLs:
```python
url = reverse("v2_management:workspace-list")
url = reverse("v2_management:workspace-detail", kwargs={"pk": workspace.id})
url = reverse("v2_management:roles-detail", kwargs={"uuid": str(role.uuid)})
```

## V2 API Tests

V2 endpoints are only registered when `V2_APIS_ENABLED=True`. V2 test classes must:

1. Apply `@override_settings(V2_APIS_ENABLED=True)` at class level
2. Reload URL configuration in `setUp`:
   ```python
   from importlib import reload
   from rbac import urls
   reload(urls)
   clear_url_caches()
   ```
3. Bootstrap the tenant for v2 if testing write operations:
   ```python
   from tests.v2_util import bootstrap_tenant_for_v2_test
   bootstrap_tenant_for_v2_test(self.tenant)
   ```
4. Mock Kessel permission checks (v2 endpoints use Kessel for authz):
   ```python
   self.enterContext(patch(
       "management.permissions.role_v2_access.get_kessel_principal_id",
       return_value="localhost/test-user-id",
   ))
   self.enterContext(patch(
       "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
       return_value=True,
   ))
   ```

For tests that exercise the Kessel permission layer itself, mock `inventory_client`:
```python
@patch("management.permissions.workspace_inventory_access.inventory_client")
def test_access_denied(self, mock_inventory_client):
    self._setup_kessel_mock(mock_inventory_client, allowed_pb2.Allowed.ALLOWED_FALSE)
```

## Workspace Test Setup

Most workspace tests create the full hierarchy in `setUp`:

```python
self.root_workspace = Workspace.objects.create(name="Root Workspace", tenant=self.tenant, type=Workspace.Types.ROOT)
self.default_workspace = Workspace.objects.create(tenant=self.tenant, type=Workspace.Types.DEFAULT, parent=self.root_workspace, ...)
self.ungrouped_workspace = Workspace.objects.create(tenant=self.tenant, type=Workspace.Types.UNGROUPED_HOSTS, parent=self.default_workspace, ...)
```

**Teardown** must null out parent refs before deleting (due to `PROTECT` on FK):
```python
def tearDown(self):
    Workspace.objects.update(parent=None)
    Workspace.objects.all().delete()
```

## Relation Replication Testing

Use `InMemoryTuples` and `InMemoryRelationReplicator` from `migration_tool.in_memory_tuples` to capture and assert SpiceDB relation tuples without a running Kessel:

```python
from migration_tool.in_memory_tuples import InMemoryTuples, InMemoryRelationReplicator

self.tuples = InMemoryTuples()
self.replicator = InMemoryRelationReplicator(self.tuples)
```

For tests that only need the replicator signature but don't inspect tuples, use `NoopReplicator`.

Patch `OutboxReplicator.replicate` and redirect to in-memory:
```python
@patch("management.relation_replicator.outbox_replicator.OutboxReplicator.replicate")
def test_something(self, mock_replicate):
    mock_replicate.side_effect = self.in_memory_replicator.replicate
```

Assert tuples with helpers from `tests/v2_util.py`:
- `bootstrap_tenant_for_v2_test(tenant, tuples=None)` -- bootstraps tenant for v2 with optional tuple capture
- `make_read_tuples_mock(tuples)` -- returns a callable matching the `read_tuples_from_kessel` signature

Tuple consistency assertions from `tests/util/`:
- `assert_v1_v2_tuples_fully_consistent(test, tuples)` -- full v1/v2 invariant check
- `assert_v2_tuples_consistent(test, tuples)` -- v2-only tuple consistency

## RbacFixture

`RbacFixture` (defined in `tests/management/role/test_dual_write.py`) creates complete RBAC data graphs for integration tests:

```python
from tests.management.role.test_dual_write import RbacFixture

fixture = RbacFixture(V2TenantBootstrapService(InMemoryRelationReplicator(tuples)))
bootstrapped = fixture.new_tenant("org123")
role = fixture.new_system_role("my-role", permissions=["app:resource:read"])
custom = fixture.new_custom_role("my-custom", resource_access=[(["app:res:write"], {})], tenant=tenant)
```

## Mocking Patterns

**Always mock:**
- Kafka: `MOCK_KAFKA=True` is set in tox.ini `setenv`. For tests asserting Kafka messages, patch `core.kafka.RBACProducer.send_kafka_message` and use `copy_call_args` from `tests/core/test_kafka.py` to capture deepcopied arguments.
- Kessel Inventory API: patch `inventory_client` or `WorkspaceInventoryAccessChecker.check_resource_access`
- Outbox replicator: patch `OutboxReplicator.replicate` when testing tuple output

**Never mock:**
- Django ORM queries (test against real PostgreSQL)
- Serializer validation logic
- URL routing and middleware (use `APIClient` for integration-level coverage)

**Settings overrides** (use `@override_settings` on class or method):
- `V2_APIS_ENABLED=True` -- enable v2 URL routes
- `V2_EDIT_API_ENABLED=True` -- enable v2 write endpoints
- `ATOMIC_RETRY_DISABLED=True` -- disable `pgtransaction.atomic` retries (avoids nested transaction errors in `TestCase`)
- `WORKSPACE_HIERARCHY_DEPTH_LIMIT=N` -- control depth limit for workspace tests
- `REPLICATION_TO_RELATION_ENABLED=True` -- enable relation replication (already set by base classes)

Use `self.enterContext(patch(...))` for patches that should last the entire test method (cleaner than `@patch` when multiple patches are needed in `setUp`).

## Test Mixins

Share setup and helpers across test classes using mixins:

```python
class RoleBindingAccessTestMixin:
    """Mixin providing common setup for role binding access tests."""
    def setUp(self):
        reload(urls)
        clear_url_caches()
        super().setUp()
        # ... create workspace hierarchy ...

class RoleBindingAccessIntegrationTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    pass
```

For assertion helpers, use mixins like `_ReplicationAssertionsMixin` which provides `assertTuplesAdded`/`assertTuplesRemoved`.

## Test Naming

- Test files: `test_<thing>.py` (e.g., `test_view.py`, `test_model.py`, `test_serializer.py`, `test_service.py`)
- Test classes: `<Thing>Tests` or `<Thing>Test` (e.g., `WorkspaceModelTests`, `RoleBindingListViewSetTest`)
- Test methods: `test_<action>_<scenario>` (e.g., `test_create_workspace`, `test_delete_workspace_unauthorized`, `test_list_roles_with_name_filter`)
- Every test method must have a docstring describing what it verifies

## Directory Structure

Tests mirror the source layout. Place test files in the corresponding `tests/` subdirectory:

```
rbac/management/workspace/view.py     -> tests/management/workspace/test_view.py
rbac/management/workspace/model.py    -> tests/management/workspace/test_model.py
rbac/management/workspace/service.py  -> tests/management/workspace/test_service.py
rbac/management/role/v2_service.py    -> tests/management/role/test_v2_service.py
```

Shared test utilities go in `tests/v2_util.py` (v2 bootstrapping), `tests/util/` (tuple invariants), or domain-specific fixture files (e.g., `tests/management/authorization/token_fixtures.py`).

## Coverage

Coverage is configured in `.coveragerc`:
- Branch coverage enabled
- Migrations excluded
- Report sorted by coverage percentage, skips fully-covered and empty files
- Lines excluded: `pragma: no cover`, `__repr__`, `raise NotImplementedError`, `if __name__`

## Performance Tests

Located in `tests/performance/`. These are **not** part of the normal test suite -- they are standalone scripts run against a database with pre-seeded data (1000 tenants, 10 groups/tenant). They use `APIClient` directly and measure request timing. Do not include them in regular test runs.

## Key Rules

1. Always call `super().setUp()` and `super().tearDown()` -- the base classes manage tenant lifecycle and cache cleanup
2. Clean up created objects in `tearDown` -- tests run in parallel and share the database
3. Use `@override_settings(ATOMIC_RETRY_DISABLED=True)` when using `TestCase` with code that calls `pgtransaction.atomic(retry=...)`; otherwise use `TransactionalIdentityRequest`
4. For v2 endpoints, always reload URLs and clear caches in `setUp`
5. Create permissions in the test tenant or `public` tenant as appropriate -- system roles go to `public`, custom roles go to the test tenant
6. Use `format="json"` in `APIClient.post`/`put`/`patch` calls
7. Assert both status code and response body structure -- check `content-type` for v2 (`application/json` or `application/problem+json`)
