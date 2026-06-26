# Migrating External Applications to Kessel RBAC

This guide helps external applications adopt Kessel RBAC for the first time. It covers migration patterns for applications with existing authorization models and those starting from scratch.

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Migration Patterns](#migration-patterns)
   - [Pattern A: Existing Workspace/Grouping Abstractions](#pattern-a-existing-workspacegrouping-abstractions)
   - [Pattern B: No Existing Authorization Model](#pattern-b-no-existing-authorization-model)
4. [Step-by-Step Migration Checklist](#step-by-step-migration-checklist)
5. [Dual-Running Strategies](#dual-running-strategies)
6. [Testing Your Migration](#testing-your-migration)
7. [Common Pitfalls](#common-pitfalls)
8. [Resources](#resources)

---

## Overview

Kessel RBAC provides centralized, workspace-based access control for console.redhat.com applications. Instead of each application implementing its own authorization logic, applications integrate with the RBAC service to:

- **Check permissions** before serving requests (`inventory:hosts:read`)
- **Filter lists** to show only resources the user can access
- **Inherit permissions** through workspace hierarchy
- **Manage access** via role bindings instead of custom logic

### Key Benefits

- **Centralized management**: Org admins control access across all applications from one place
- **Consistent UX**: Users experience the same permission model everywhere
- **Reduced complexity**: No need to build/maintain authorization logic
- **Workspace inheritance**: Permissions cascade down the tree automatically

### Integration Model

```
┌─────────────────────┐
│  Your Application   │
│                     │
│  ┌───────────────┐  │
│  │ Endpoint      │  │
│  │ Handler       │  │
│  └───────┬───────┘  │
│          │          │
│          │ 1. Check permission
│          │    via RBAC API
│          ▼          │
│  ┌───────────────┐  │
│  │ RBAC Client   │──┼──► insights-rbac service
│  │ (HTTP/gRPC)   │  │    ↓
│  └───────────────┘  │    Kessel Relations API
│                     │    (SpiceDB)
│  ┌───────────────┐  │
│  │ Business      │  │
│  │ Logic         │  │
│  └───────────────┘  │
└─────────────────────┘
```

**Flow:**
1. User makes request to your application
2. Your app extracts user identity from `x-rh-identity` header
3. Your app calls RBAC API to check permission (e.g., "Can user read hosts?")
4. RBAC queries Kessel Relations API (SpiceDB)
5. Your app proceeds or returns 403 based on response

---

## Prerequisites

Before migrating, ensure you have:

1. **Identity header integration**: Your application must receive and validate the `x-rh-identity` header injected by the platform gateway (3scale). This header contains:
   - Organization ID (`org_id`)
   - User identity (`identity.user.username` or `identity.service_account.client_id`)
   - Account number (legacy)
   - Entitlements

2. **Permission definitions**: Define your application's permissions in the format `application:resource_type:operation`. Examples:
   - `inventory:hosts:read`
   - `advisor:recommendation_results:write`
   - `cost-management:cost_models:*`

3. **Resource scoping**: Decide whether your resources are:
   - **Data-level** (workspace-scoped): Most application resources (hosts, reports, configs)
   - **Org-level** (organization-wide): Admin functions (create workspaces, manage billing)

4. **Workspace strategy**: Determine how your resources map to workspaces:
   - Will you use existing RBAC workspaces?
   - Do you need custom workspace hierarchies?
   - Should resources inherit parent workspace permissions?

5. **RBAC API access**: Your application needs:
   - Network access to the RBAC service
   - Service-to-service authentication (PSK or identity header)
   - The RBAC API base URL (e.g., `https://console.redhat.com/api/rbac/v2/`)

---

## Migration Patterns

### Pattern A: Existing Workspace/Grouping Abstractions

**Scenario**: Your application already has workspace, project, folder, or team concepts that group resources.

#### Analysis Phase

1. **Map existing abstractions to RBAC workspaces**:
   - If your groupings are hierarchical (folders in folders), they align naturally with RBAC workspaces
   - If they're flat (teams, projects), you can map each to a workspace under a parent

2. **Identify existing permission checks**:
   ```python
   # Example: Your current code
   if not user.can_edit(project):
       return 403
   ```

3. **Map to RBAC permissions**:
   ```
   can_edit(project) → myapp:projects:write permission check
   can_view(project) → myapp:projects:read permission check
   ```

4. **Audit permission granularity**:
   - Are your current checks too coarse? (e.g., only "admin" vs "viewer")
   - Are they too fine-grained? (e.g., permissions per field)
   - RBAC permissions work best at operation level: `read`, `write`, `delete`

#### Implementation Strategy

**Option 1: Map existing groups to RBAC workspaces (recommended)**

Best when your existing groups have clear ownership boundaries.

```python
# Before: Custom authorization table
class Project(models.Model):
    name = models.CharField(max_length=255)
    members = models.ManyToManyField(User, through='ProjectMembership')

class ProjectMembership(models.Model):
    project = models.ForeignKey(Project)
    user = models.ForeignKey(User)
    role = models.CharField(choices=['admin', 'editor', 'viewer'])

# After: Workspace-based via RBAC
class Project(models.Model):
    name = models.CharField(max_length=255)
    workspace_id = models.UUIDField()  # Links to RBAC workspace

# Membership is managed via RBAC role bindings instead
```

**Migration steps:**
1. For each existing group, create an RBAC workspace via `POST /api/rbac/v2/workspaces/`
2. Store the workspace UUID in your existing group/project table
3. For each membership, create a role binding via `POST /api/rbac/v2/role_bindings/`:
   - Map `admin` → `"Custom Admin"` role
   - Map `editor` → `"Custom Editor"` role
   - Map `viewer` → `"Custom Viewer"` role
4. Replace permission checks with RBAC API calls (see [Dual-Running](#dual-running-strategies))

**Option 2: Sync to RBAC asynchronously**

Best when you need to maintain your existing model during transition.

```python
# Keep your existing tables, sync changes to RBAC
class Project(models.Model):
    name = models.CharField(max_length=255)
    workspace_id = models.UUIDField(null=True, blank=True)  # Populated after sync

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Async task: sync to RBAC
        sync_project_to_rbac.delay(self.id)

@celery_app.task
def sync_project_to_rbac(project_id):
    project = Project.objects.get(id=project_id)
    if not project.workspace_id:
        # Create workspace
        response = rbac_client.create_workspace({
            "name": project.name,
            "parent_id": get_root_workspace_id()
        })
        project.workspace_id = response['id']
        project.save(update_fields=['workspace_id'])

    # Sync memberships → role bindings
    for membership in project.memberships.all():
        rbac_client.create_role_binding({
            "role_id": map_role(membership.role),
            "resource_id": str(project.workspace_id),
            "subjects": [{"type": "Principal", "id": membership.user.username}]
        })
```

#### Permission Check Migration

```python
# Before: Custom permission check
def get_project(request, project_id):
    project = Project.objects.get(id=project_id)
    if not request.user.has_project_access(project, 'read'):
        raise PermissionDenied()
    return project

# After: RBAC permission check
def get_project(request, project_id):
    project = Project.objects.get(id=project_id)

    # Check permission via RBAC
    has_access = rbac_client.check_permission(
        principal=request.user.username,
        permission="myapp:projects:read",
        resource_id=str(project.workspace_id)
    )

    if not has_access:
        raise PermissionDenied()
    return project
```

#### List Filtering Migration

For list endpoints, you need to filter results to only resources the user can access.

```python
# Before: Custom filtering
def list_projects(request):
    projects = Project.objects.filter(
        memberships__user=request.user
    )
    return projects

# After: RBAC filtering (Option 1 - fetch accessible workspace IDs)
def list_projects(request):
    # Get all workspace IDs user can access with read permission
    accessible_workspaces = rbac_client.list_accessible_resources(
        principal=request.user.username,
        permission="myapp:projects:read"
    )
    workspace_ids = [r['id'] for r in accessible_workspaces]

    # Filter projects by workspace
    projects = Project.objects.filter(
        workspace_id__in=workspace_ids
    )
    return projects

# After: RBAC filtering (Option 2 - check each, for small lists)
def list_projects(request):
    projects = Project.objects.all()

    # Filter in Python (only viable for small result sets)
    accessible = []
    for project in projects:
        if rbac_client.check_permission(
            principal=request.user.username,
            permission="myapp:projects:read",
            resource_id=str(project.workspace_id)
        ):
            accessible.append(project)
    return accessible
```

**Performance note**: For list endpoints with many results, prefer Option 1 (fetch accessible IDs upfront) to minimize API calls.

---

### Pattern B: No Existing Authorization Model

**Scenario**: Your application currently has no authorization or uses simple admin/non-admin checks.

This is the **simplest migration path** — you're adding authorization from scratch, not migrating an existing model.

#### Analysis Phase

1. **Define your resources**: What objects does your application manage?
   - Examples: hosts, reports, configurations, cost models, policies

2. **Define operations per resource type**:
   ```
   Resource: hosts
   Operations: read, write, delete

   Resource: reports
   Operations: read, write, execute, delete
   ```

3. **Define permission strings**:
   ```
   myapp:hosts:read
   myapp:hosts:write
   myapp:hosts:delete
   myapp:reports:read
   myapp:reports:write
   myapp:reports:execute
   myapp:reports:delete
   ```

4. **Determine workspace mapping**:
   - **Flat model**: All resources in the org's default workspace
   - **Grouped model**: Resources grouped by team/environment/application

#### Implementation Strategy

**Step 1: Decide workspace strategy**

**Option A: Use default workspace (simplest)**
```python
# All resources use the tenant's default workspace
class Host(models.Model):
    name = models.CharField(max_length=255)
    tenant = models.ForeignKey(Tenant)

    @property
    def workspace_id(self):
        # All hosts use default workspace
        return self.tenant.default_workspace_id
```

**Option B: Create workspace hierarchy**
```python
# Resources grouped by environment
class Environment(models.Model):
    name = models.CharField(max_length=255)  # "Production", "Staging"
    tenant = models.ForeignKey(Tenant)
    workspace_id = models.UUIDField()  # RBAC workspace

class Host(models.Model):
    name = models.CharField(max_length=255)
    environment = models.ForeignKey(Environment)

    @property
    def workspace_id(self):
        return self.environment.workspace_id
```

**Step 2: Add permission checks**

```python
# Protect detail endpoints
from myapp.rbac_client import check_permission

def get_host(request, host_id):
    host = Host.objects.get(id=host_id)

    # Check permission
    if not check_permission(
        principal=request.user.username,
        permission="myapp:hosts:read",
        resource_id=str(host.workspace_id)
    ):
        # Return 404 to prevent existence leakage
        raise Http404()

    return host

def update_host(request, host_id):
    host = Host.objects.get(id=host_id)

    # Check write permission
    if not check_permission(
        principal=request.user.username,
        permission="myapp:hosts:write",
        resource_id=str(host.workspace_id)
    ):
        raise Http404()

    # Proceed with update
    host.name = request.data['name']
    host.save()
    return host
```

**Step 3: Filter list endpoints**

```python
def list_hosts(request):
    # Get accessible workspace IDs
    accessible_workspaces = list_accessible_resources(
        principal=request.user.username,
        permission="myapp:hosts:read"
    )
    workspace_ids = [r['id'] for r in accessible_workspaces]

    # Filter hosts
    hosts = Host.objects.filter(
        environment__workspace_id__in=workspace_ids
    )
    return hosts
```

**Step 4: Create default roles**

Work with RBAC team to create seeded roles for your application:

```json
{
  "name": "MyApp Viewer",
  "permissions": [
    "myapp:hosts:read",
    "myapp:reports:read"
  ]
},
{
  "name": "MyApp Editor",
  "permissions": [
    "myapp:hosts:read",
    "myapp:hosts:write",
    "myapp:reports:read",
    "myapp:reports:write",
    "myapp:reports:execute"
  ]
},
{
  "name": "MyApp Admin",
  "permissions": [
    "myapp:*:*"
  ]
}
```

**Step 5: Document for users**

Update your documentation to explain:
- How to grant users access (create role binding in RBAC UI)
- What roles are available
- How workspace inheritance works

---

## Step-by-Step Migration Checklist

### Phase 1: Planning (1-2 weeks)

- [ ] **Define permissions**: List all operations users perform, map to `app:resource:operation` format
- [ ] **Audit current authorization**: Document existing permission checks, identify patterns
- [ ] **Choose migration pattern**: Pattern A (existing groups) or Pattern B (new authorization)
- [ ] **Design workspace mapping**: Decide how your resources map to workspaces
- [ ] **Identify pilot scope**: Choose a small, non-critical feature to migrate first
- [ ] **Set up RBAC client**: Install/configure HTTP client for RBAC API
- [ ] **Get RBAC API credentials**: Obtain PSK or configure service identity

### Phase 2: Development (2-4 weeks)

- [ ] **Implement RBAC client library**:
  ```python
  class RBACClient:
      def check_permission(self, principal, permission, resource_id):
          """Check if principal has permission on resource."""
          pass

      def list_accessible_resources(self, principal, permission):
          """Get all resources principal can access."""
          pass

      def create_workspace(self, name, parent_id):
          """Create a new workspace."""
          pass

      def create_role_binding(self, role_id, resource_id, subjects):
          """Bind a role to subjects on a resource."""
          pass
  ```

- [ ] **Add workspace tracking**:
  - Add `workspace_id` fields to models (if using workspace mapping)
  - Create migration to add column
  - Implement workspace creation logic

- [ ] **Implement dual-running** (see [Dual-Running Strategies](#dual-running-strategies)):
  - Add feature flag: `USE_RBAC_AUTHORIZATION`
  - Implement permission check wrapper that calls both old and new
  - Log discrepancies for investigation

- [ ] **Migrate permission checks**:
  - Replace custom authorization with RBAC calls
  - Update detail endpoints (single resource checks)
  - Update list endpoints (bulk filtering)

- [ ] **Handle edge cases**:
  - Service-to-service calls (use service account identity)
  - Background jobs (use system service account)
  - Public/unauthenticated endpoints (skip RBAC check)

### Phase 3: Testing (1-2 weeks)

- [ ] **Unit tests**: Mock RBAC client, test permission check logic
- [ ] **Integration tests**: Test against real RBAC service in dev environment
- [ ] **Performance testing**: Measure latency impact of RBAC calls
- [ ] **Load testing**: Verify RBAC can handle your expected traffic
- [ ] **Security testing**: Attempt to bypass permission checks, test privilege escalation
- [ ] **User acceptance testing**: Have real users test with role bindings

### Phase 4: Data Migration (1 week)

- [ ] **Migrate workspaces** (if Pattern A):
  ```python
  for project in Project.objects.all():
      workspace = rbac_client.create_workspace(
          name=project.name,
          parent_id=tenant.root_workspace_id
      )
      project.workspace_id = workspace['id']
      project.save()
  ```

- [ ] **Migrate memberships to role bindings**:
  ```python
  for membership in ProjectMembership.objects.all():
      rbac_client.create_role_binding(
          role_id=map_role(membership.role),
          resource_id=str(membership.project.workspace_id),
          subjects=[{"type": "Principal", "id": membership.user.username}]
      )
  ```

- [ ] **Verify data migration**: Compare old and new authorization results
- [ ] **Backfill missing data**: Handle any records that failed migration

### Phase 5: Deployment (1-2 weeks)

- [ ] **Deploy with dual-running enabled**: Deploy to staging/production with feature flag OFF
- [ ] **Enable dual-running in production**: Turn on feature flag, compare results
- [ ] **Monitor for discrepancies**: Alert on any old/new mismatches
- [ ] **Fix discrepancies**: Investigate and resolve any authorization differences
- [ ] **Gradual rollout**: Enable RBAC for increasing % of traffic (10% → 50% → 100%)
- [ ] **Full cutover**: Remove old authorization code, remove feature flag
- [ ] **Deprecate old model**: (Pattern A only) Drop old membership tables after grace period

### Phase 6: Validation (1 week)

- [ ] **Security audit**: Verify no permission bypasses exist
- [ ] **Performance monitoring**: Check p50/p95/p99 latency, error rates
- [ ] **User feedback**: Confirm users can access expected resources
- [ ] **Documentation update**: Update API docs, user guides, runbooks

---

## Dual-Running Strategies

Dual-running allows you to validate RBAC integration without risking production access control. Both old and new authorization systems run in parallel, with the old system enforcing access while the new one is validated.

### Strategy 1: Check Both, Enforce Old (Recommended)

**Best for**: Initial rollout, high-risk applications

```python
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

def check_permission(request, permission, resource):
    """Check permission using dual-running strategy."""

    # Old authorization (enforced)
    old_result = legacy_permission_check(request.user, resource)

    if not settings.RBAC_DUAL_RUNNING_ENABLED:
        # Feature flag off: use old system only
        return old_result

    # New authorization (validated, not enforced)
    try:
        new_result = rbac_client.check_permission(
            principal=request.user.username,
            permission=permission,
            resource_id=str(resource.workspace_id)
        )
    except Exception as e:
        # RBAC API failure: log and fall back to old
        logger.error(f"RBAC check failed: {e}", exc_info=True)
        metrics.increment('rbac.check.error')
        new_result = None

    # Compare results
    if new_result is not None and old_result != new_result:
        logger.warning(
            f"RBAC mismatch: user={request.user.username} "
            f"permission={permission} resource={resource.id} "
            f"old={old_result} new={new_result}"
        )
        metrics.increment('rbac.check.mismatch')

    # Enforce old result
    return old_result
```

**Monitoring**:
- Track `rbac.check.mismatch` metric — should trend toward zero
- Alert on sustained mismatch rate > 1%
- Review logs daily to identify patterns

### Strategy 2: Check Both, Enforce New

**Best for**: After validation period, before removing old system

```python
def check_permission(request, permission, resource):
    """Check permission, enforcing RBAC."""

    # New authorization (enforced)
    new_result = rbac_client.check_permission(
        principal=request.user.username,
        permission=permission,
        resource_id=str(resource.workspace_id)
    )

    if settings.RBAC_DUAL_RUNNING_ENABLED:
        # Also check old system for comparison
        old_result = legacy_permission_check(request.user, resource)

        if old_result != new_result:
            logger.warning(
                f"RBAC enforcement changed access: user={request.user.username} "
                f"permission={permission} resource={resource.id} "
                f"old={old_result} new={new_result}"
            )
            metrics.increment('rbac.enforcement.changed_access')

    # Enforce new result
    return new_result
```

**This phase identifies users who lost or gained access due to migration.**

### Strategy 3: Percentage Rollout

**Best for**: Gradual production deployment

```python
import hashlib

def check_permission(request, permission, resource):
    """Gradually roll out RBAC to percentage of users."""

    # Determine if user is in rollout cohort
    user_hash = int(hashlib.md5(request.user.username.encode()).hexdigest(), 16)
    rollout_pct = settings.RBAC_ROLLOUT_PERCENTAGE  # 0-100
    use_rbac = (user_hash % 100) < rollout_pct

    if use_rbac:
        # User is in RBAC cohort
        result = rbac_client.check_permission(
            principal=request.user.username,
            permission=permission,
            resource_id=str(resource.workspace_id)
        )
        metrics.increment('rbac.check.new')
    else:
        # User still on old system
        result = legacy_permission_check(request.user, resource)
        metrics.increment('rbac.check.old')

    return result
```

**Rollout schedule**:
1. Week 1: 10% of users
2. Week 2: 25% of users
3. Week 3: 50% of users
4. Week 4: 100% of users

**Monitor error rates and latency per cohort** — if RBAC cohort shows elevated errors, pause rollout.

### Caching for Performance

RBAC permission checks add network latency. Cache aggressively:

```python
from django.core.cache import cache

def check_permission_cached(request, permission, resource):
    """Check permission with caching."""

    cache_key = f"rbac:{request.user.username}:{permission}:{resource.workspace_id}"
    result = cache.get(cache_key)

    if result is None:
        result = rbac_client.check_permission(
            principal=request.user.username,
            permission=permission,
            resource_id=str(resource.workspace_id)
        )
        # Cache for 5 minutes
        cache.set(cache_key, result, timeout=300)

    return result
```

**Invalidation**: RBAC changes replicate to Kessel in 100-500ms. A 5-minute cache means up to 5 minutes of stale permissions — acceptable for most use cases. For immediate invalidation, subscribe to RBAC change events via Kafka.

---

## Testing Your Migration

### Unit Testing

Mock the RBAC client to test permission logic:

```python
from unittest.mock import patch, MagicMock

class TestPermissions(TestCase):
    @patch('myapp.rbac_client.check_permission')
    def test_user_can_view_host(self, mock_check):
        # Arrange
        mock_check.return_value = True
        host = Host.objects.create(name="test-host")

        # Act
        response = self.client.get(f'/api/hosts/{host.id}/')

        # Assert
        self.assertEqual(response.status_code, 200)
        mock_check.assert_called_once_with(
            principal='testuser',
            permission='myapp:hosts:read',
            resource_id=str(host.workspace_id)
        )

    @patch('myapp.rbac_client.check_permission')
    def test_user_cannot_view_host(self, mock_check):
        # Arrange
        mock_check.return_value = False
        host = Host.objects.create(name="test-host")

        # Act
        response = self.client.get(f'/api/hosts/{host.id}/')

        # Assert
        self.assertEqual(response.status_code, 404)  # Not 403, to prevent existence leakage
```

### Integration Testing

Test against a real RBAC instance in dev/staging:

```python
class TestRBACIntegration(TestCase):
    def setUp(self):
        # Create test workspace
        self.workspace = rbac_client.create_workspace(
            name="test-workspace",
            parent_id=get_root_workspace_id()
        )

        # Create test role binding
        self.role_binding = rbac_client.create_role_binding(
            role_id=get_viewer_role_id(),
            resource_id=self.workspace['id'],
            subjects=[{"type": "Principal", "id": "testuser"}]
        )

    def tearDown(self):
        # Clean up
        rbac_client.delete_role_binding(self.role_binding['id'])
        rbac_client.delete_workspace(self.workspace['id'])

    def test_permission_check_allows_access(self):
        # Wait for replication (RBAC → Kessel)
        time.sleep(1)

        # Check permission
        result = rbac_client.check_permission(
            principal="testuser",
            permission="myapp:hosts:read",
            resource_id=self.workspace['id']
        )

        self.assertTrue(result)
```

### Performance Testing

Measure latency impact:

```python
import time
import statistics

def benchmark_permission_checks():
    latencies = []

    for i in range(100):
        start = time.time()
        rbac_client.check_permission(
            principal="testuser",
            permission="myapp:hosts:read",
            resource_id=workspace_id
        )
        latencies.append((time.time() - start) * 1000)  # ms

    print(f"p50: {statistics.median(latencies):.2f}ms")
    print(f"p95: {statistics.quantiles(latencies, n=20)[18]:.2f}ms")
    print(f"p99: {statistics.quantiles(latencies, n=100)[98]:.2f}ms")
```

**Target latencies** (RBAC API call):
- p50: < 50ms
- p95: < 100ms
- p99: < 200ms

**If latencies exceed targets**, enable caching (see [Caching for Performance](#caching-for-performance)).

### Security Testing

Attempt to bypass permission checks:

```python
class TestSecurityBypass(TestCase):
    def test_cannot_access_without_permission(self):
        """Verify user without permission cannot access resource."""
        # Create host in workspace user doesn't have access to
        other_workspace = create_workspace_for_other_user()
        host = Host.objects.create(
            name="test-host",
            workspace_id=other_workspace['id']
        )

        # Attempt access
        response = self.client.get(f'/api/hosts/{host.id}/')

        # Should return 404 (not 403, to prevent existence leakage)
        self.assertEqual(response.status_code, 404)

    def test_cannot_list_unauthorized_resources(self):
        """Verify list endpoints don't leak unauthorized resources."""
        # Create hosts in two workspaces
        my_workspace = get_my_workspace()
        other_workspace = get_other_workspace()

        my_host = Host.objects.create(name="my-host", workspace_id=my_workspace['id'])
        other_host = Host.objects.create(name="other-host", workspace_id=other_workspace['id'])

        # List hosts
        response = self.client.get('/api/hosts/')

        # Should only include my_host
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], my_host.id)
```

---

## Common Pitfalls

### 1. Returning 403 Instead of 404

**Problem**: Returning 403 Forbidden for inaccessible resources reveals existence to unauthorized users.

```python
# WRONG: Reveals host exists
def get_host(request, host_id):
    host = Host.objects.get(id=host_id)
    if not check_permission(request, 'myapp:hosts:read', host):
        return HttpResponseForbidden()  # ❌ Reveals host exists
    return host

# CORRECT: Returns 404, prevents existence leakage
def get_host(request, host_id):
    host = Host.objects.get(id=host_id)
    if not check_permission(request, 'myapp:hosts:read', host):
        raise Http404()  # ✅ Hides existence
    return host
```

### 2. Forgetting to Filter List Endpoints

**Problem**: List endpoints return all resources, bypassing permission checks.

```python
# WRONG: Returns all hosts, ignoring permissions
def list_hosts(request):
    return Host.objects.all()  # ❌

# CORRECT: Filters by accessible workspaces
def list_hosts(request):
    accessible_workspaces = rbac_client.list_accessible_resources(
        principal=request.user.username,
        permission='myapp:hosts:read'
    )
    workspace_ids = [r['id'] for r in accessible_workspaces]
    return Host.objects.filter(workspace_id__in=workspace_ids)  # ✅
```

### 3. Not Handling RBAC API Failures

**Problem**: Network errors or RBAC downtime break your application.

```python
# WRONG: Exception bubbles up, breaks request
def check_permission(principal, permission, resource_id):
    return rbac_client.check_permission(principal, permission, resource_id)  # ❌

# CORRECT: Graceful degradation
def check_permission(principal, permission, resource_id):
    try:
        return rbac_client.check_permission(principal, permission, resource_id)
    except requests.exceptions.Timeout:
        logger.error("RBAC timeout, denying access")
        metrics.increment('rbac.timeout')
        return False  # Fail closed
    except Exception as e:
        logger.error(f"RBAC error: {e}", exc_info=True)
        metrics.increment('rbac.error')
        # Option 1: Fail closed (deny access)
        return False
        # Option 2: Fail open (allow access) - only for non-critical resources
        # return True
```

### 4. Caching Too Aggressively

**Problem**: Long cache TTLs mean permission changes don't take effect promptly.

```python
# RISKY: 1-hour cache means revoked access persists for up to 1 hour
cache.set(cache_key, result, timeout=3600)  # ⚠️

# SAFER: 5-minute cache balances performance and staleness
cache.set(cache_key, result, timeout=300)  # ✅
```

**Recommendation**: Cache for 5 minutes by default. For sensitive resources (billing, admin functions), cache for 1 minute or skip caching entirely.

### 5. Hardcoding Workspace IDs

**Problem**: Workspace UUIDs differ across environments (dev, staging, prod).

```python
# WRONG: Hardcoded UUID breaks in other environments
ADMIN_WORKSPACE_ID = "12345678-1234-1234-1234-123456789012"  # ❌

# CORRECT: Fetch dynamically or use environment variable
def get_admin_workspace_id():
    return os.getenv('ADMIN_WORKSPACE_ID') or fetch_from_rbac()  # ✅
```

### 6. Not Testing Cross-Tenant Isolation

**Problem**: Missing `org_id` filter allows cross-tenant data leakage.

```python
# Test that users from org A cannot access org B's resources
def test_cross_tenant_isolation(self):
    # Create host for org A
    org_a_host = Host.objects.create(name="host-a", tenant=org_a)

    # Authenticate as user from org B
    self.client.force_authenticate(user=org_b_user)

    # Attempt to access org A's host
    response = self.client.get(f'/api/hosts/{org_a_host.id}/')

    # Should return 404 (or 403), never 200
    self.assertIn(response.status_code, [403, 404])
```

### 7. Overloading Permission Granularity

**Problem**: Too many fine-grained permissions make roles unusable.

```python
# TOO GRANULAR: 100+ permissions for one resource type
myapp:host:read_name
myapp:host:read_ip
myapp:host:read_os
myapp:host:write_name
myapp:host:write_ip
# ... 95 more

# BETTER: Operation-level permissions
myapp:hosts:read   # Read all host fields
myapp:hosts:write  # Write all host fields
```

**Guideline**: Aim for 3-10 permissions per resource type. Common pattern: `read`, `write`, `delete`, plus occasional special ops like `execute`, `export`.

---

## Resources

### RBAC API Documentation

- **V2 API Spec**: [docs/source/specs/v2/openapi.yaml](../docs/source/specs/v2/openapi.yaml)
- **TypeSpec Source**: [docs/source/specs/typespec/main.tsp](../docs/source/specs/typespec/main.tsp)

### Kessel Documentation

- **Getting Started**: [https://project-kessel.github.io/docs/start-here/getting-started/](https://project-kessel.github.io/docs/start-here/getting-started/)
- **RBAC Concepts**: [https://project-kessel.github.io/docs/building-with-kessel/concepts/rbac/](https://project-kessel.github.io/docs/building-with-kessel/concepts/rbac/)

### insights-rbac Documentation

- **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md)
- **Security Guidelines**: [security-guidelines.md](security-guidelines.md)
- **API Contracts**: [api-contracts-guidelines.md](api-contracts-guidelines.md)
- **Role Bindings Deep Dive**: [role_bindings.md](role_bindings.md)
- **Integration Guidelines**: [integration-guidelines.md](integration-guidelines.md)

### Sample Client Libraries

#### Python (using requests)

```python
import requests
from typing import List, Dict, Optional

class RBACClient:
    def __init__(self, base_url: str, psk: str):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'x-rh-rbac-psk': psk,
            'Content-Type': 'application/json'
        })

    def check_permission(
        self,
        principal: str,
        permission: str,
        resource_id: str
    ) -> bool:
        """Check if principal has permission on resource."""
        response = self.session.post(
            f"{self.base_url}/api/rbac/v2/access/check",
            json={
                "principal": principal,
                "permission": permission,
                "resource_id": resource_id
            },
            timeout=5
        )
        response.raise_for_status()
        return response.json().get('allowed', False)

    def list_accessible_resources(
        self,
        principal: str,
        permission: str
    ) -> List[Dict]:
        """Get all resources principal can access with permission."""
        response = self.session.get(
            f"{self.base_url}/api/rbac/v2/access/resources",
            params={
                "principal": principal,
                "permission": permission
            },
            timeout=10
        )
        response.raise_for_status()
        return response.json().get('data', [])

    def create_workspace(
        self,
        name: str,
        parent_id: Optional[str] = None
    ) -> Dict:
        """Create a new workspace."""
        response = self.session.post(
            f"{self.base_url}/api/rbac/v2/workspaces/",
            json={
                "name": name,
                "parent_id": parent_id
            },
            timeout=5
        )
        response.raise_for_status()
        return response.json()

    def create_role_binding(
        self,
        role_id: str,
        resource_id: str,
        subjects: List[Dict]
    ) -> Dict:
        """Bind a role to subjects on a resource."""
        response = self.session.post(
            f"{self.base_url}/api/rbac/v2/role_bindings/",
            json={
                "role_id": role_id,
                "resource_id": resource_id,
                "subjects": subjects
            },
            timeout=5
        )
        response.raise_for_status()
        return response.json()
```

#### Go (using net/http)

```go
package rbac

import (
    "bytes"
    "encoding/json"
    "fmt"
    "net/http"
    "time"
)

type Client struct {
    BaseURL string
    PSK     string
    HTTP    *http.Client
}

func NewClient(baseURL, psk string) *Client {
    return &Client{
        BaseURL: baseURL,
        PSK:     psk,
        HTTP: &http.Client{
            Timeout: 5 * time.Second,
        },
    }
}

func (c *Client) CheckPermission(principal, permission, resourceID string) (bool, error) {
    payload := map[string]string{
        "principal":   principal,
        "permission":  permission,
        "resource_id": resourceID,
    }
    body, _ := json.Marshal(payload)

    req, _ := http.NewRequest("POST", c.BaseURL+"/api/rbac/v2/access/check", bytes.NewBuffer(body))
    req.Header.Set("x-rh-rbac-psk", c.PSK)
    req.Header.Set("Content-Type", "application/json")

    resp, err := c.HTTP.Do(req)
    if err != nil {
        return false, err
    }
    defer resp.Body.Close()

    if resp.StatusCode != 200 {
        return false, fmt.Errorf("RBAC API returned %d", resp.StatusCode)
    }

    var result struct {
        Allowed bool `json:"allowed"`
    }
    json.NewDecoder(resp.Body).Decode(&result)
    return result.Allowed, nil
}
```

### Support

- **Slack**: `#forum-consoledot-rbac` (internal Red Hat)
- **Email**: rbac-team@redhat.com
- **Issues**: File bugs/questions in the insights-rbac GitHub repo
