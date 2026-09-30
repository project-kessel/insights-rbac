# Inventory Migration Verification Endpoint

Internal, read-only endpoint for validating that a tenant's RBAC data was correctly
migrated to Kessel Inventory.

## Endpoint

```
GET /_private/api/inventory/verify_migration/<org_id>/
```

Authenticated via the internal identity header (`InternalIdentityHeaderMiddleware`).

## Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `role_limit` | int | 25 | Max custom roles to check |
| `binding_limit` | int | 25 | Max role bindings to check |
| `group_limit` | int | 25 | Max groups to check |
| `car_limit` | int | 25 | Max cross-account requests to check |
| `workspace_limit` | int | 25 | Max workspace pairs to check |
| `dry_run` | bool | false | Validate tuple generation without gRPC calls |

All limits must be non-negative integers (400 on invalid).

## Response Shape

```json
{
  "org_id": "12345",
  "dry_run": false,
  "overall_status": "pass|fail",
  "failed_checks": ["bootstrap", "roles", ...],
  "summary": {
    "sections_checked": 8,
    "sections_passed": 8,
    "sections_failed": 0
  },
  "checks": {
    "bootstrap": { ... },
    "workspaces": { ... },
    "roles": { ... },
    "bindings": { ... },
    "group_principals": { ... },
    "role_permissions": { ... },
    "cross_account_requests": { ... },
    "pipeline_health": { ... }
  }
}
```

## Sections Checked

### bootstrap
Validates the tenant's root, default, and ungrouped workspace relations exist in
Inventory. In dry-run mode, reports the workspace IDs without checking.

### workspaces
Validates parent/descendant relations for child workspaces. Each pair generates
one gRPC `Check` call. Capped by `workspace_limit`.

### roles
For each non-system (custom) role, replays the dual-write handler against an
in-memory replicator inside a rolled-back transaction to produce the expected
relation tuples, then verifies each tuple exists in Inventory. In dry-run mode,
validates tuples via protobuf round-trip without gRPC calls.

### bindings
Validates role binding tuples (`resource#binding`, `binding#role`,
`binding#subject`) via `RoleBinding.all_tuples()`. Capped by `binding_limit`.

### group_principals
Validates group-to-principal membership relations. Capped by `group_limit`.

### role_permissions
Validates `CustomRoleV2` permission tuples. Reports both V2 role UUID and V1
source UUID for cross-referencing with the `roles` section.

### cross_account_requests
Validates approved cross-account request relations by replaying the CAR
dual-write handler. Capped by `car_limit`.

### pipeline_health
Tenant-agnostic liveness check of the async replication pipeline:
- **Debezium connector**: HTTP GET to `KAFKA_CONNECT_URL` for connector status.
  Requires `KAFKA_CONNECT_URL` env var.
- **Replication slots**: Queries `pg_replication_slots` for active pgoutput slots.

Always runs in both dry-run and live modes (no gRPC calls involved).

## Safety Properties

- **Read-only**: The role and cross-account request handler-replay sections run
  inside `transaction.atomic()` with `set_rollback(True)` to prevent writes.
  The remaining sections (bootstrap, workspaces, group principals, role
  permissions) use plain read queries. The pipeline health check sends an
  HTTP GET to the Debezium connector and a `SELECT` against
  `pg_replication_slots`. Nothing is persisted.
- **Per-item isolation**: A single failing item (e.g. transient gRPC error) is
  recorded for that item only; remaining items in the section continue.
- **Audit logged**: Every call is logged as `VERIFY_INVENTORY_MIGRATION` admin
  action (SEC-MON-REQ-1 / EOI-3).

## Dry-Run Mode

When `dry_run=true`:
- Roles, bindings, group principals, role permissions, and CARs validate their
  generated tuples via protobuf round-trip (`as_message` -> validation) without
  making any gRPC calls.
- Bootstrap and workspaces report their resource IDs/pairs but do not verify
  against Inventory (their checkers fuse tuple generation with the gRPC check).
- `pipeline_health` still runs (it never calls Inventory).
- A section fails if any generated tuple fails round-trip validation.

## Configuration

| Env Var | Purpose |
|---------|---------|
| `KAFKA_CONNECT_URL` | Kafka Connect REST API base URL for Debezium health check. Optional. |
| `REPLICATION_TO_RELATION_ENABLED` | When false, role dual-write handlers produce no tuples; roles are reported as `"verified": false` rather than falsely passing. |
