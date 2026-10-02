# Migrating Existing Applications to Kessel RBAC

This guide covers the practical steps for applications adopting Kessel RBAC for the first time: dual-running strategies for safe migration, a step-by-step checklist, and an API reference for the v2 endpoints you'll use.

For pattern selection (which of the five migration patterns applies to your resource), see the [Migration Pattern Reference](migration-pattern-reference.md). For permissions schema design, see the [Design Permissions guide](https://project-kessel.github.io/docs/building-with-kessel/how-to/design-permissions/). For applications already on RBAC v1, see [Migrate from RBAC v1 to RBAC v2](https://project-kessel.github.io/docs/building-with-kessel/how-to/migrate-from-rbac-v1-to-v2/). For Kessel concepts and architecture, see the [Getting Started tutorial](https://project-kessel.github.io/docs/start-here/getting-started/).

---

## Table of Contents

1. [Key Concepts](#key-concepts)
2. [Dual-Running Strategies](#dual-running-strategies)
3. [Step-by-Step Migration Checklist](#step-by-step-migration-checklist)
4. [API Reference Quick Guide](#api-reference-quick-guide)

---

## Key Concepts

### Workspace Hierarchy

Kessel organizes authorization around a tree of workspaces. Every tenant automatically gets a hierarchy:

```
Tenant (org-wide)
└── Root Workspace         (type: "root")
    └── Default Workspace  (type: "default")
        ├── Standard Workspace A  (type: "standard")
        └── Standard Workspace B  (type: "standard")
```

Permissions **inherit downward**: a role binding on the root workspace grants access to all child workspaces. This hierarchy is defined by the `Workspaces.Workspace` schema in the [V2 OpenAPI spec](../docs/source/specs/v2/openapi.json), which includes the workspace types `root`, `default`, `standard`, and `ungrouped-hosts` (`Workspaces.WorkspaceTypes`).

### Role Bindings

A role binding connects three things: **who** (subject) has **what permissions** (role) on **which resource** (workspace or tenant).

The `RoleBindings.CreateRoleBindingsRequest` schema captures this:

```json
{
  "resource": { "id": "<workspace-or-tenant-id>", "type": "workspace" },
  "subject":  { "id": "<group-or-user-uuid>", "type": "group" },
  "role":     { "id": "<role-uuid>" }
}
```

The `resource.type` field accepts values from the `ResourceType` enum: `workspace` or `tenant`.

### Permission Format

Permissions follow the `Permission` schema — `application:resource_type:operation`:

```json
{ "application": "inventory", "resource_type": "hosts", "operation": "read" }
```

### Authorization Check Types

| Check Type | Use For | Consistency |
|------------|---------|-------------|
| `Check` | Read operations (post-filtering) | Eventually consistent (100-500ms replication window) |
| `CheckForUpdate` | Write operations | Strongly consistent |
| `StreamedListObjects` | Listing accessible resources (pre-filtering) | Eventually consistent |

### Choosing Your Pattern

The [Migration Pattern Reference](migration-pattern-reference.md) defines five patterns based on whether your resource is workspace-aware, its query cardinality, and whether it represents an asset or an org-wide setting. Each pattern specifies both the check level for your application and the binding level RBAC uses — these must match, or permissions silently fail. See the [compatibility constraint](migration-pattern-reference.md#the-compatibility-constraint) for details.

---

## Dual-Running Strategies

During migration, you may need to run your old authorization system alongside Kessel. This section covers strategies for doing so safely.

### Strategy 1: Shadow Mode (Recommended Starting Point)

Run Kessel checks alongside your existing authorization but **only use your existing system for enforcement**. Compare results to validate correctness.

```
┌─────────────────────────────────────────────────┐
│               Incoming Request                   │
│                                                  │
│  ┌──────────────┐    ┌──────────────────────┐   │
│  │  Old Auth     │    │  Kessel Check        │   │
│  │  (enforcing)  │    │  (shadow/logging)    │   │
│  └──────┬───────┘    └──────────┬───────────┘   │
│         │                       │                │
│         ▼                       ▼                │
│    Use this result         Log & compare         │
│                            (alert on mismatch)   │
└─────────────────────────────────────────────────┘
```

Implementation approach:

1. Set up Kessel workspaces, roles, and role bindings per your chosen [pattern](migration-pattern-reference.md)
2. On each request, call both your old auth and Kessel's `Check`
3. Log mismatches — these indicate gaps in your migration data
4. Continue enforcing with old auth only
5. Once mismatch rate drops to zero, proceed to cutover

To review current bindings during shadow mode, use `GET /role-bindings/by-subject/` to verify access is set up correctly. Filter by resource:

```
GET /api/rbac/v2/role-bindings/by-subject/?resource_type=workspace&resource_id=e4277742-b91c-43f1-a185-b827e8574345
```

Response (`RoleBindings.RoleBindingBySubject`):

```json
{
  "meta": { "limit": 10 },
  "links": { "next": null, "previous": null },
  "data": [
    {
      "last_modified": "2024-08-04T12:00:00Z",
      "subject": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "type": "group",
        "group": {
          "name": "Engineering Team",
          "description": "Development and engineering team",
          "user_count": 25
        }
      },
      "roles": [
        {
          "id": "550e8400-e29b-41d4-a716-446655440002",
          "name": "Workspace Admin"
        }
      ],
      "resource": {
        "id": "e4277742-b91c-43f1-a185-b827e8574345",
        "name": "Engineering Workspace",
        "type": "workspace"
      }
    }
  ]
}
```

### Strategy 2: Gradual Cutover by Endpoint

Migrate one endpoint at a time from old auth to Kessel, while keeping the rest on old auth.

```
Endpoint A: ──── Old Auth ────────────────────────────────
Endpoint B: ──── Old Auth ────┬──── Kessel ───────────────
Endpoint C: ──── Old Auth ────────────┬──── Kessel ───────
                              ▲       ▲
                          migrate B  migrate C
```

Steps:

1. Identify your lowest-risk, most well-understood endpoint
2. Run it in shadow mode until you have confidence
3. Switch enforcement to Kessel for that endpoint only
4. Repeat for the next endpoint

This approach limits blast radius — if Kessel's data is wrong, only one endpoint is affected.

### Strategy 3: Feature-Flag Cutover

Use a feature flag to toggle between old and new auth per tenant (organization).

Steps:

1. Gate Kessel enforcement behind a feature flag (per org)
2. Enable shadow mode for all orgs to populate and validate data
3. Enable enforcement for internal/test orgs first
4. Gradually roll out to production orgs
5. Remove the feature flag once all orgs are migrated

### Strategy 4: Read vs Write Split

A pragmatic intermediate approach: switch **read** operations to Kessel first (using `Check` with eventually consistent reads), while keeping **write** operations on old auth (or using `CheckForUpdate` with strong consistency).

This works well because:
- Read operations are higher volume and benefit most from Kessel's caching
- Write operations are lower volume and more safety-critical
- Eventually consistent reads have a 100-500ms replication window, which is acceptable for most read paths
- Strongly consistent writes via `CheckForUpdate` eliminate the replication window for mutations

### Monitoring During Dual-Running

Regardless of strategy, monitor these signals:

| Signal | What It Means |
|--------|---------------|
| Auth decision mismatches | Kessel and old auth disagree — data migration gap |
| Kessel `Check` latency | Should be 10-50ms typical; spikes indicate schema or infrastructure issues |
| Kessel error rate | 5xx responses from Kessel — indicates infrastructure problems |
| False denials | Users blocked by Kessel but allowed by old auth — missing role bindings |
| False grants | Users allowed by Kessel but blocked by old auth — overly broad role bindings |

---

## Step-by-Step Migration Checklist

### Phase 1: Plan

- [ ] **Audit your current authorization model.** Document what permissions exist, how they're checked, and what grouping abstractions (if any) your application uses.
- [ ] **Classify each resource using the [decision tree](migration-pattern-reference.md#decision-tree).** For each resource or permission your application checks, determine which of the five patterns applies. Key questions: Is the resource workspace-aware? What is the query cardinality? Is it an asset or a setting? Is it asset-centric?
- [ ] **Design your permissions schema.** Map your application's operations to the `application:resource_type:operation` format (`Permission` schema). See [Design Permissions](https://project-kessel.github.io/docs/building-with-kessel/how-to/design-permissions/) for guidance.
- [ ] **Define your KSL schema.** Use `@rbac.add_permission()` (new apps) or `@rbac.add_v1_based_permission()` (migrating from RBAC v1) to declare permissions in your `.ksl` file. For [Pattern 2](migration-pattern-reference.md#pattern-2-native-workspace-level-list-workspace-aware-high-cardinality) (workspace-level list), use `@rbac.add_contingent_permission()` to combine host view with app-specific permissions. See [Getting Started](https://project-kessel.github.io/docs/start-here/getting-started/) for the schema compilation workflow.
- [ ] **Coordinate with RBAC on binding levels.** Confirm that the level where RBAC will bind roles matches the level where your application will check. See the [compatibility constraint](migration-pattern-reference.md#the-compatibility-constraint) and [how RBAC migrates access grants](migration-pattern-reference.md#how-rbac-migrates-access-grants).
- [ ] **Choose a dual-running strategy.** See [Dual-Running Strategies](#dual-running-strategies). Shadow mode is recommended as a starting point.

### Phase 2: Set Up

- [ ] **Install the Kessel SDK** for your language (Python, Go, TypeScript, Ruby, or Java). See the [Getting Started guide](https://project-kessel.github.io/docs/start-here/getting-started/) for installation instructions.
- [ ] **Configure your service's OAuth credentials.** Your service needs its own credentials to call the RBAC v2 API for workspace lookups and Kessel for permission checks.
- [ ] **Look up and cache built-in workspace IDs.** Use `GET /api/rbac/v2/workspaces/?type=root` and `GET /api/rbac/v2/workspaces/?type=default` to find the root and default workspace IDs. Cache these — they are immutable. See [looking up built-in workspaces](migration-pattern-reference.md#looking-up-built-in-workspaces).
- [ ] **Create custom roles** via `POST /api/rbac/v2/roles/` if the seeded roles don't cover your permissions. List existing roles with `GET /api/rbac/v2/roles/` to check what's available.
- [ ] **Create workspaces** (Patterns 1/2 only) via `POST /api/rbac/v2/workspaces/` for each of your existing grouping abstractions.

### Phase 3: Migrate Data

- [ ] **Create role bindings** via `POST /api/rbac/v2/role-bindings:batchCreate/` to replicate your current access patterns. Batch up to 100 bindings per request. Ensure each binding targets the correct resource level per your [pattern classification](migration-pattern-reference.md#pattern-summary).
- [ ] **Report resources to Kessel Inventory** (Patterns 1/2 only) using `ReportResource` gRPC calls. Each resource should include a `workspace_id` in its common representation. See [Getting Started](https://project-kessel.github.io/docs/start-here/getting-started/).
- [ ] **Validate role bindings.** Use `GET /api/rbac/v2/role-bindings/by-subject/` filtered by resource to verify bindings match your expectations.
- [ ] **Validate permissions.** Use `Check` calls for a sample of users and resources to confirm Kessel returns the expected allow/deny decisions.
- [ ] **Verify binding-check level alignment.** For each permission, confirm the level where RBAC bound the role matches where your application will check. A mismatch means the permission will silently fail.

### Phase 4: Integrate

- [ ] **Add authorization checks using the correct strategy per pattern:**
  - [Patterns 1, 3, 4](migration-pattern-reference.md): Use `Check` (reads) or `CheckForUpdate` (writes) against the specific resource
  - [Pattern 2](migration-pattern-reference.md#pattern-2-native-workspace-level-list-workspace-aware-high-cardinality): Use `StreamedListObjects` with `object_type = rbac/workspace` for list operations; `Check`/`CheckForUpdate` for detail/write operations
  - [Pattern 5](migration-pattern-reference.md#pattern-5-organization-level-non-asset-centric-settings): Use `Check`/`CheckForUpdate` against the tenant resource
- [ ] **Run in shadow mode.** Execute both old and new auth, log mismatches, resolve discrepancies.
- [ ] **Add error handling.** Follow a fail-closed pattern: if Kessel is unreachable, deny the request. See [Protect an Endpoint](https://project-kessel.github.io/docs/building-with-kessel/how-to/protect-endpoint/).

### Phase 5: Validate

- [ ] **Deploy to ephemeral environment.** Use `insights-service-deployer` to test against a full Kessel stack. See [Migrate from RBAC v1 to v2: Validate in Ephemeral Environment](https://project-kessel.github.io/docs/building-with-kessel/how-to/migrate-from-rbac-v1-to-v2/) for instructions.
- [ ] **Test with local Keycloak** (recommended) or Stage SSO for authentication.
- [ ] **Verify shadow mode mismatch rate is at zero** for at least one full release cycle.
- [ ] **Test edge cases:** users with no roles, users in multiple groups, workspace inheritance across nested workspaces, concurrent role binding updates, permissions at different hierarchy levels.

### Phase 6: Cut Over

- [ ] **Switch enforcement to Kessel** for one endpoint (Strategy 2) or one tenant (Strategy 3).
- [ ] **Monitor for false denials and false grants** using your application's error logging and Kessel's metrics.
- [ ] **Gradually expand scope** until all endpoints and tenants use Kessel.
- [ ] **Remove old authorization code** once all traffic uses Kessel.
- [ ] **Remove the shadow mode comparison code.**

---

## API Reference Quick Guide

All endpoints are relative to `https://console.redhat.com/api/rbac/v2`. Full details are in the [V2 OpenAPI spec](../docs/source/specs/v2/openapi.json).

### Workspaces

| Operation | Method | Path | Request Schema | Response Schema |
|-----------|--------|------|---------------|-----------------|
| List | GET | `/workspaces/` | — | `Workspaces.WorkspaceListResponse` |
| Create | POST | `/workspaces/` | `Workspaces.CreateWorkspaceRequest` | `Workspaces.CreateWorkspaceResponse` |
| Read | GET | `/workspaces/{id}/` | — | `Workspaces.ReadWorkspaceResponse` |
| Update | PUT | `/workspaces/{id}/` | `Workspaces.UpdateWorkspaceRequest` | `Workspaces.UpdateWorkspaceResponse` |
| Patch | PATCH | `/workspaces/{id}/` | `Workspaces.PatchWorkspaceRequest` | `Workspaces.PatchWorkspaceResponse` |
| Delete | DELETE | `/workspaces/{id}/` | — | 204 No Content |
| Move | POST | `/workspaces/{id}/move/` | `Workspaces.MoveWorkspaceRequest` | `Workspaces.MoveWorkspaceResponse` |

Key query parameters for `GET /workspaces/`:
- `type` — filter by workspace type: `root`, `default`, `standard`, `ungrouped-hosts`, or `all` (default)
- `parent_id` — filter by parent workspace UUID (useful for lazy-loading tree structures)
- `with_ancestry` — when `true`, includes ancestor workspaces in the response

### Roles

| Operation | Method | Path | Request Schema | Response Schema |
|-----------|--------|------|---------------|-----------------|
| List | GET | `/roles/` | — | Paginated `Role` list |
| Create | POST | `/roles/` | `Roles.CreateOrUpdateRoleRequest` | `Role` |
| Read | GET | `/roles/{id}/` | — | `Role` |
| Update | PUT | `/roles/{id}/` | `Roles.CreateOrUpdateRoleRequest` | `Role` |
| Batch Delete | POST | `/roles:batchDelete/` | `Roles.BatchDeleteRolesRequest` | 204 No Content |

Key query parameters for `GET /roles/`:
- `name` — filter by name (case-insensitive substring; use `*` for glob patterns)
- `permission` — filter by permission string(s), comma-separated exact match

### Role Bindings

| Operation | Method | Path | Request Schema | Response Schema |
|-----------|--------|------|---------------|-----------------|
| List | GET | `/role-bindings/` | — | Paginated `RoleBindings.RoleBinding` list |
| List by Subject | GET | `/role-bindings/by-subject/` | — | Paginated `RoleBindings.RoleBindingBySubject` list |
| Update by Subject | PUT | `/role-bindings/by-subject/` | `RoleBindings.UpdateRoleBindingsRequest` | `RoleBindings.RoleBindingBySubject` |
| Batch Create | POST | `/role-bindings:batchCreate/` | `RoleBindings.BatchCreateRoleBindingsRequest` | `RoleBindings.BatchCreateRoleBindingsResponse` |

Key query parameters for `GET /role-bindings/`:
- `resource_type`, `resource_id` — filter by the resource the binding applies to
- `resource.tenant.org_id` — filter by tenant org ID (cannot combine with `resource_id`)
- `subject_type`, `subject_id` — filter by the binding's subject (group or user)
- `granted_subject_type`, `granted_subject_id` — filter by the subject effectively granted access (follows group memberships)
- `exclude_sources` — `none` (default), `direct`, or `indirect` (`ExcludeSources` enum)
- `fields` — dynamic field selection using `FieldMask` syntax (e.g., `subject(group.name),roles(name),resource(type)`)

### Principals

| Operation | Method | Path | Response Schema |
|-----------|--------|------|-----------------|
| List | GET | `/principals/` | `PrincipalListResponse` |
| Read | GET | `/principals/{uuid}/` | `Principal` |

### Error Responses

All errors use RFC 7807 Problem JSON with `application/problem+json` content type. Problem type URIs are under `http://project-kessel.org/problems/`:

| Status | Problem Type | Schema |
|--------|-------------|--------|
| 400 | `invalid-request` or `already-exists` | `Problems.Problem400` / `Problems.Problem400AlreadyExists` |
| 401 | `unauthenticated` | `Problems.Problem401` |
| 403 | `insufficient-permission` | `Problems.Problem403` |
| 404 | `not-found` | `Problems.Problem404` |
| 500 | `internal-error` | `Problems.Problem500` |

---

## Further Reading

- [Migration Pattern Reference](migration-pattern-reference.md) — The five migration patterns, decision tree, and request translation tables
- [KSL-016: Migrating host and organization level permissions](https://docs.google.com/document/d/1XnINsHuYeHEi22q_1cS0gUalX-eXl3V19gGf0Wr8NsE/) — ADR defining the five migration patterns
- [Getting Started with Kessel](https://project-kessel.github.io/docs/start-here/getting-started/) — 30-minute hands-on tutorial
- [Understanding Kessel](https://project-kessel.github.io/docs/start-here/understanding-kessel/) — Architectural overview
- [RBAC Concepts](https://project-kessel.github.io/docs/building-with-kessel/concepts/rbac/) — Detailed permission model reference
- [Coming from RBAC v1](https://project-kessel.github.io/docs/building-with-kessel/concepts/coming-from-rbac-v1/) — Conceptual comparison for v1 users
- [Design Permissions](https://project-kessel.github.io/docs/building-with-kessel/how-to/design-permissions/) — Permission schema design guide
- [Protect an Endpoint](https://project-kessel.github.io/docs/building-with-kessel/how-to/protect-endpoint/) — SDK implementation guide with middleware examples
- [Migrate from RBAC v1 to v2](https://project-kessel.github.io/docs/building-with-kessel/how-to/migrate-from-rbac-v1-to-v2/) — For applications already on RBAC v1
