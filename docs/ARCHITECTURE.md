# Architecture

High-level system design for insights-rbac. For domain-specific conventions and rules, see the individual [guideline docs](.).

## System Context

insights-rbac is a microservice within the [console.redhat.com](https://console.redhat.com) platform. It answers the question: "what is this user allowed to do?" Other platform services call RBAC to check permissions before performing operations.

```
                         +---------------------+
                         |  console.redhat.com  |
                         |    (3scale gateway)  |
                         +----------+----------+
                                    |
                            identity header
                                    |
                         +----------v----------+
                         |    insights-rbac     |
                         |  (Django REST API)   |
                         +--+-----+-----+------+
                            |     |     |
               +------------+  +--+--+  +------------+
               |               |     |               |
        +------v------+  +----v-+ +-v----+   +------v-------+
        |  PostgreSQL  |  |Redis | |Kafka |   |    Kessel     |
        |   (data)     |  |(cache)| |(CDC) |   | Inventory API |
        +--------------+  +------+ +------+   |   (SpiceDB)   |
                                              +--------------+
```

### Request Flow

1. A user or service makes an HTTP request to `console.redhat.com`.
2. The platform gateway (3scale) authenticates the request and injects an `x-rh-identity` header containing the org ID, user info, and entitlements as base64-encoded JSON.
3. RBAC middleware decodes the identity header, resolves the tenant, and attaches `request.user` and `request.tenant` to the Django request.
4. The view delegates to the service layer for business logic.
5. Authorization depends on the endpoint and configuration: v2 endpoints can use Kessel Inventory checks, legacy access checks, or both.
6. The response is returned with tenant-scoped data only.

### MCP Endpoint (Agent-to-Service)

The MCP endpoint (`/_private/_a2s/mcp/`) exposes RBAC operations as tools via the [Model Context Protocol](https://modelcontextprotocol.io/) JSON-RPC 2.0 interface, enabling AI agents to discover and invoke RBAC operations.

- **Transport**: StreamableHTTP (JSON-RPC 2.0 over HTTP POST)
- **Auth**: Same `x-rh-identity` header as the public API (routed through `IdentityHeaderMiddleware`, not the internal PSK/JWT auth)
- **Tools**: 54 registered tools (31 read, 23 write) covering roles, groups, permissions, workspaces, role bindings, audit, and cross-account access
- **Write safety**: Disabled by default (`MCP_WRITE_ENABLED`); optional two-phase confirmation with SHA-256 bound tokens
- **Execution**: Synchronous in-process (WSGI thread), delegating to existing Django views via `_clone_request()`

See [MCP.md](MCP.md) for the developer guide and [MCP-operator-guide.md](MCP-operator-guide.md) for deployment and configuration.

## Component Overview

### Application Layers

```
rbac/
  api/              # V1 REST API -- roles, groups, policies, permissions, principals
  management/       # Core domain modules (shared by v1 and v2)
    mcp_views.py    #   MCP endpoint (JSON-RPC, 54 tools, A2S auth)
  internal/         # Internal/service-to-service API (PSK-authenticated)
  core/             # Shared utilities, middleware, JWT, error handling
  rbac/             # Django project config (settings, WSGI, Celery, URL root)
  migration_tool/   # V1-to-V2 data migration utilities
```

### Domain Modules (rbac/management/)

Each domain area is organized as a sub-package with a consistent structure:

```
management/
  role/             # Roles and permissions
  group/            # Groups and group membership
  workspace/        # Workspace hierarchy (v2)
  role_binding/     # Role-to-workspace bindings (v2)
  permission/       # V1 permission objects
  permissions/      # V2 permission checks and filter backends
  policy/           # V1 policies (role + group association)
  principal/        # User/service account principals
  access/           # V1 access checks
  subject/          # Subject evaluation (for access decisions)
  audit_log/        # Audit logging
  authorization/    # Auth middleware and permission classes
  notifications/    # Platform notification integration
  inventory_replicator/ # Outbox-based replication to Kessel
  debezium/         # Debezium CDC outbox model
  tenant_mapping/   # Cross-service tenant resolution
  tenant_service/   # Tenant lifecycle (bootstrap, migration)
  inventory_checker/ # Kessel Inventory integration
  health/           # Liveness and readiness probes
```

Each domain module typically contains:

| File | Responsibility |
|------|---------------|
| `model.py` | Django models (inherit `TenantAwareModel`) |
| `service.py` | Business logic (raises domain exceptions, never DRF exceptions) |
| `view.py` | DRF ViewSets (delegates to service layer) |
| `serializer.py` | Input validation and output formatting |
| `filters.py` | Queryset filtering for list endpoints |
| `definer.py` | Permission definitions and access checks (v2) |

### Dual API Versions

**V1 API** (`/api/rbac/v1/`): Stable, widely consumed. Manages roles, groups, policies, and permissions. Uses flat error arrays. Routes are always registered.

**V2 API** (`/api/rbac/v2/`): Next-generation workspace-based API. Uses RFC 7807 Problem JSON errors. Routes are gated behind the `V2_APIS_ENABLED` feature flag. Authorization varies by endpoint and configuration, as described in the security model below.

The APIs share some domain models, such as groups, principals and permissions, but have distinct role and binding models and version-specific views, serializers and services. Migration and dual-write code links the two representations.

## Data Model

### Multi-Tenancy

Tenant-owned domain entities such as roles, groups, workspaces and role bindings use `TenantAwareModel` to reference a tenant. Queries must respect `request.tenant`; association models such as `RoleBindingGroup` and `RoleBindingPrincipal` do not have their own tenant field and must be scoped through their related objects. A special "public" tenant holds shared system roles and permissions.

### Core Entities

```
V1: Group -- Policy -- Role -- Access -- Permission
                              |
                              +-- ResourceDefinition
V2: RoleV2 -- permissions (many-to-many) -- Permission
       |
       +-- RoleBinding -- resource (workspace or tenant)
               |-- RoleBindingGroup -- Group -- Principal
               +-- RoleBindingPrincipal -- Principal (direct subject)
```

- **Workspace**: Tree structure (parent/child). Each tenant has a root workspace and a default workspace. Depth is configurable via `WORKSPACE_HIERARCHY_DEPTH_LIMIT`.
- **Role** and **RoleV2**: Separate models. `RoleV2` has its own many-to-many `permissions` relation to `Permission`, optional `v1_source` link, and custom, seeded and platform proxy types. Seeded and platform roles belong to the public tenant; custom roles belong to an organization.
- **RoleBinding**: Associates a `RoleV2` with a resource type and ID. `RoleBindingGroup` and `RoleBindingPrincipal` associate group and direct principal subjects respectively; a binding is not limited to a workspace resource.
- **Policy** (v1 only): Links a group to roles. Being replaced by role bindings in v2.

### Key Relationships

- Roles have permissions. V1 uses `Access` objects that link a role to a permission and may have resource definitions. V2 uses `RoleV2.permissions`. `BindingMapping` stores migration/dual-write binding mappings between V1 roles and V2 resources and roles; it does not define V2 permission sets.
- Groups contain principals. A principal can belong to multiple groups.
- V2 grants combine a resource scope and role binding with a group subject (and its members) or a directly bound principal. Workspace inheritance can also affect effective grants.

See [`role/model.py`](../rbac/management/role/model.py), [`role/v2_model.py`](../rbac/management/role/v2_model.py), and [`role_binding/model.py`](../rbac/management/role_binding/model.py) for the model definitions.

## External Integrations

### Kessel (SpiceDB, via the Inventory API)

Kessel evaluates relationships used by v2 authorization. RBAC replicates its data model to Kessel as relationship tuples:

- **Write path**: Service code produces replication events via `InventoryReplicator`. In production, `OutboxReplicator` writes to the outbox table, Debezium captures changes via WAL, publishes to Kafka, and the RBAC Kafka consumer writes tuples to Kessel through the Inventory API (`InventoryApiReplicator`) via gRPC.
- **Read path**: Some v2 permission classes check an individual tenant or resource through Kessel Inventory's `CheckForUpdate`; workspace visibility can use `StreamedListObjects` for lists and `CheckForUpdate` for detail actions. Not every v2 endpoint uses both methods.

### Kafka

- **Producer**: Outbox-based CDC via Debezium (not direct Kafka writes from application code).
- **Consumer**: `RBACKafkaConsumer` reads replication events and applies them to Kessel through the Inventory API. Also consumes platform sync and chrome navigation topics.

### Redis

- **Celery broker**: Task queue for async operations (tenant seeding, cross-account cleanup, etc.).
- **Cache**: Permission and access policy caching to reduce database queries.

### Other Services

- **BOP (Backoffice Proxy)**: Principal/user lookup (org admin status, service accounts).
- **IT Service**: Extended user attributes for compliance.
- **Kafka principal cleanup**: Consumes principal lifecycle events (create/update/disable) from Kafka, with XML bridge support for legacy message formats.
- **Notifications service**: Platform notification delivery.

## Async Processing

Celery handles background tasks with Redis as the broker:

- Tenant bootstrapping and seeding
- Cross-account request expiration
- Workspace hierarchy operations
- Kessel parity checks
- Cache invalidation

The Celery worker and beat scheduler run as separate containers alongside the main application.

## Security Model

Authentication is layered:

1. **Identity header** (`x-rh-identity`): Platform gateway injects user/org context. Decoded by `IdentityHeaderMiddleware`.
2. **Pre-shared keys (PSK)**: Service-to-service calls to internal API endpoints.
3. **JWT**: Kessel service account authentication for gRPC calls.

Authorization differs by API version:

- **V1**: Primarily local permission checks based on roles and policies; some reads have a Kessel fallback for migrated organizations.
- **V2 tenant-wide checks**: Role, group and audit-log permission classes check relations on the tenant through Kessel Inventory; group lists also support a narrowly scoped self-access exception. The principal endpoint uses admin or legacy access with a Kessel fallback for migrated organizations.
- **V2 object-scoped checks**: With `WORKSPACE_ACCESS_CHECK_V2_ENABLED` enabled, `WorkspaceAccessPermission` handles endpoint/create/move checks, while `WorkspaceAccessFilterBackend` filters accessible workspaces. Inaccessible workspace details return 404. When disabled, workspace access falls back to legacy behavior.
- **V2 resource-scoped checks**: Role-binding permissions check the requested workspace or tenant; unscoped reads fall back to tenant authorization. Tenant checks use org-admin status when `KESSEL_TENANT_AUTH_ENABLED` is disabled, or Kessel when enabled. Batch writes check each resource. Role bindings do not use a universal per-object access filter backend.

Authorization failures and filtering are endpoint-specific: an endpoint-level denial can return 403, while workspace detail filtering hides inaccessible objects with 404. Tenant scoping remains necessary regardless of the authorization mechanism.

See [`workspace/filters.py`](../rbac/management/workspace/filters.py) and [`permissions/role_binding_access.py`](../rbac/management/permissions/role_binding_access.py) for these endpoint-specific checks, and [security-guidelines.md](security-guidelines.md) for security conventions.

## Deployment

The service runs on OpenShift via Clowder (Red Hat's application deployment framework):

- **rbac-server**: Gunicorn WSGI server behind 3scale
- **rbac-worker**: Celery worker for async tasks
- **rbac-scheduler**: Celery beat for periodic tasks
- **PostgreSQL 16**: Primary data store
- **Redis**: Cache and Celery broker

Configuration is environment-variable driven. In Clowder-managed environments, database and Kafka connection details are injected automatically via `app-common-python`.
