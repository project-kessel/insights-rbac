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
        |   (data)     |  |(cache)| |(CDC) |   | Relations API |
        +--------------+  +------+ +------+   |   (SpiceDB)   |
                                              +--------------+
```

### Request Flow

1. A user or service makes an HTTP request to `console.redhat.com`.
2. The platform gateway (3scale) authenticates the request and injects an `x-rh-identity` header containing the org ID, user info, and entitlements as base64-encoded JSON.
3. RBAC middleware decodes the identity header, resolves the tenant, and attaches `request.user` and `request.tenant` to the Django request.
4. The view delegates to the service layer for business logic.
5. For v2 APIs, permission checks are performed against Kessel Relations (SpiceDB) via gRPC.
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
  relation_replicator/  # Outbox-based replication to Kessel
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

**V2 API** (`/api/rbac/v2/`): Next-generation workspace-based API. Uses RFC 7807 Problem JSON errors. Routes are gated behind the `V2_APIS_ENABLED` feature flag. Authorization is handled by Kessel Relations instead of local permission checks.

Both API versions share the same underlying models and service layer. The differences are in serializers, views, URL routing, and error format.

## Data Model

### Multi-Tenancy

Every business model inherits `TenantAwareModel`, which adds a non-nullable `ForeignKey` to `Tenant`. All queries are scoped to `request.tenant`. A special "public" tenant holds system-wide platform roles and permissions that are visible to all tenants.

### Core Entities

```
Tenant (org_id)
  |
  +-- Workspace (tree hierarchy, v2)
  |     |
  |     +-- RoleBinding (binds role + groups/principals to workspace, v2)
  |
  +-- Group
  |     |
  |     +-- Principal (users, service accounts)
  |
  +-- Role / RoleV2
  |     |
  |     +-- Permission / ResourceDefinition
  |
  +-- Policy (v1: associates Role + Group)
```

- **Workspace**: Tree structure (parent/child). Each tenant has a root workspace and a default workspace. Depth is configurable via `WORKSPACE_HIERARCHY_DEPTH_LIMIT`.
- **RoleV2**: Typed (custom, seeded, platform) via proxy models. Platform roles are tenant-agnostic (public tenant).
- **RoleBinding**: Associates a role with groups/principals in a specific workspace scope.
- **Policy** (v1 only): Links a role to a group. Being replaced by role bindings in v2.

### Key Relationships

- Roles have permissions. V1 uses `Access` objects with resource definitions. V2 uses `BindingMapping` to define permission sets.
- Groups contain principals. A principal can belong to multiple groups.
- In v2, access is determined by: workspace scope + role binding + group membership.

## External Integrations

### Kessel Relations (SpiceDB)

Authorization engine for v2 APIs. RBAC replicates its data model to Kessel as relationship tuples:

- **Write path**: Service code produces replication events via `RelationReplicator`. In production, `OutboxReplicator` writes to the outbox table, Debezium captures changes via WAL, publishes to Kafka, and the RBAC Kafka consumer writes tuples to Kessel via gRPC.
- **Read path**: V2 permission classes call `CheckForUpdate` (single resource check) or `StreamedListObjects` (list all authorized resources) against Kessel.

### Kafka

- **Producer**: Outbox-based CDC via Debezium (not direct Kafka writes from application code).
- **Consumer**: `RBACKafkaConsumer` reads replication events and applies them to Kessel Relations. Also consumes platform sync and chrome navigation topics.

### Redis

- **Celery broker**: Task queue for async operations (tenant seeding, cross-account cleanup, etc.).
- **Cache**: Permission and access policy caching to reduce database queries.

### Other Services

- **BOP (Backoffice Proxy)**: Principal/user lookup (org admin status, service accounts).
- **IT Service**: Extended user attributes for compliance.
- **UMB (Unified Message Bus)**: STOMP-based messaging for cross-system notifications.
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

- **V1**: Local permission checks based on roles and policies.
- **V2**: Two-layer pattern -- `*AccessPermission` classes for endpoint-level checks (403), `*AccessFilterBackend` for queryset-level filtering. Detail views return 404 for inaccessible resources to prevent existence leakage.

See [security-guidelines.md](security-guidelines.md) for full details.

## Deployment

The service runs on OpenShift via Clowder (Red Hat's application deployment framework):

- **rbac-server**: Gunicorn WSGI server behind 3scale
- **rbac-worker**: Celery worker for async tasks
- **rbac-scheduler**: Celery beat for periodic tasks
- **PostgreSQL 16**: Primary data store
- **Redis**: Cache and Celery broker

Configuration is environment-variable driven. In Clowder-managed environments, database and Kafka connection details are injected automatically via `app-common-python`.
