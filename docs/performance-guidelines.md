# Performance Guidelines

Performance conventions and patterns specific to insights-rbac.

## Caching Architecture

### Redis Cache Hierarchy

Five cache types share a single `BlockingConnectionPool` (module-level in `management/cache.py`).
The pool's `max_connections` must match `GUNICORN_THREAD_LIMIT` (default 10).

| Cache | Key pattern | Lifetime | Serialization |
|---|---|---|---|
| `TenantCache` | `rbac::tenant::tenant={org_id}` | `ACCESS_CACHE_LIFETIME` (600s) | pickle |
| `AccessCache` | `rbac::policy::tenant={org_id}::user={uuid}` | `ACCESS_CACHE_LIFETIME` (600s) | JSON (hset) |
| `PrincipalCache` | `rbac::principal::{org_id}::{username}` | `PRINCIPAL_CACHE_LIFETIME` (3600s) | pickle |
| `JWKSCache` | `rbac::jwks::response` | `IT_TOKEN_JKWS_CACHE_LIFETIME` (28800s) | JSON |
| `JWTCache` | `rbac::jwt::relations` | `IT_TOKEN_JKWS_CACHE_LIFETIME` (28800s) | string |

### Cache Rules

- **Every `get_cached()` call does a health check ping.** For high-throughput paths (Kafka consumers), use `JWTCacheOptimized` which skips the ping.
- **Signal-driven invalidation** is the primary cache-busting mechanism. Changes to `Role`, `Access`, `ResourceDefinition`, `Policy`, `Group` membership all trigger cache deletes via Django signals. These signals are gated by `ACCESS_CACHE_ENABLED` and `ACCESS_CACHE_CONNECT_SIGNALS`.
- **Platform-default group changes flush the entire tenant's policy cache** (`delete_all_policies_for_tenant`). Non-default changes only flush affected principal UUIDs. Be aware that `scan_iter` with `BATCH_DELETE_SIZE=1000` is used for tenant-wide deletes.
- **PrincipalCache** is used in `management/utils.py:get_principal()`. Always call `cache_principal()` after creating or fetching a principal from the DB to keep the cache warm.
- **Never bypass the cache layer.** The `AccessCache.get_policy` / `save_policy` pattern in `access/view.py` is the reference implementation: check cache first, query DB on miss, write result back to cache.
- **Celery beat runs `run_redis_cache_health` every 30 seconds.** If Redis is unreachable, caching is disabled globally on that worker.

### In-Process Caches

Two singleton caches live in process memory (not Redis):

- `PermissionScopeCache` (`permission/scope_service.py`) -- maps Permission IDs to their Scope enum. Call `invalidate()` after permission seeding.
- `V2RoleExcludedApplicationPermissionIdsCache` (`role/v2_role_scope.py`) -- caches PKs of permissions in excluded applications. Call `invalidate()` after any permission table mutation.

Both are rebuilt lazily on next access after invalidation.

## Query Optimization

### Eager Loading Conventions

**v2 QuerySets** use field-driven eager loading. `RoleV2QuerySet.with_fields(fields)` conditionally applies `select_related`, `prefetch_related`, and annotations based on which response fields are requested. Follow this pattern for new v2 querysets.

```python
# Good: field-driven, only loads what the serializer needs
qs = qs.with_fields(requested_fields)

# Bad: unconditional prefetch of everything
qs = qs.prefetch_related("permissions", "bindings", "tenant")
```

**RoleBindingQuerySet.for_tenant()** is the canonical example of a complex eager-load chain: `select_related("role")`, `prefetch_related("group_entries__group", ...)`, plus `annotate()` for fields that `CursorPagination` needs via `getattr()`.

### Preventing N+1 Queries

- Serializers must not trigger lazy loads. The comment in `role_binding/serializer.py:484` is the contract: `role.children.all()` relies on the service layer's `prefetch_related("role__children")`.
- When adding a new serializer field that traverses a relation, add the corresponding `prefetch_related` in the queryset or service layer, not in the serializer.
- Use `Prefetch` objects with custom querysets for filtered or nested prefetches (see `role_binding/service.py:459-469`).

### Annotations for Pagination

DRF's `CursorPagination` uses `getattr(instance, field)` for cursor positions. Cross-relation lookups (e.g., `role__name`) only work in `.order_by()`, not `getattr()`. Solution: annotate with `F()` expressions:

```python
qs = qs.annotate(
    role_name=F("role__name"),
    role_uuid=F("role__uuid"),
)
```

### Workspace Tree Queries

Ancestor/descendant queries use `WITH RECURSIVE` CTEs via `RawSQL`:
- `Workspace.ancestors()` -- single workspace, returns ancestor chain
- `Workspace.descendants()` -- single workspace, returns subtree
- `WorkspaceManager.descendant_ids_with_parents()` -- batch of workspace IDs, single DB round-trip

Always use `.only("name", "id", "parent_id")` when serializing ancestors (see `workspace/serializer.py:97`).

### values_list for ID Collections

Use `values_list("id", flat=True)` or `values_list("uuid", flat=True)` when you only need IDs for filtering. This avoids hydrating full model instances.

## Transaction Management

### SERIALIZABLE Isolation (v2 APIs)

All v2 write operations use PostgreSQL `SERIALIZABLE` isolation via `pgtransaction`. Three helpers in `management/atomic_transactions.py`:

- `@atomic` -- decorator, SERIALIZABLE, no retry
- `@atomic_with_retry(retries=N)` -- decorator, SERIALIZABLE, auto-retry on serialization failure
- `atomic_block()` -- context manager, SERIALIZABLE

The `AtomicOperationsMixin` in `v2_mixins.py` wraps `create`/`update`/`destroy` with SERIALIZABLE + 3 retries + concurrency error handling (409 for `SerializationFailure`, 500 for `DeadlockDetected`). Override `perform_atomic_create` etc., never override `create` directly.

### select_for_update Patterns

- **Always pair `select_for_update()` with `transaction.atomic()`.** The codebase uses `select_for_update(of=["self"])` to lock only the target table in joins.
- **Dual-write handlers require the role to be locked** before constructing the handler (`role/relation_api_dual_write_handler.py:289`).
- Chain `select_for_update().select_related("tenant")` to avoid extra queries inside the locked section.

### Test Isolation

Set `ATOMIC_RETRY_DISABLED=True` in test settings to skip `pgtransaction` wrappers (which conflict with Django's test transaction rollback). The `is_atomic_disabled()` check falls back to plain `transaction.atomic()`.

## Celery Tasks

### Beat Schedule

| Task | Schedule | Purpose |
|---|---|---|
| `cross_account_cleanup` | Daily at midnight | Expire cross-account requests |
| `run_redis_cache_health` | Every 30 seconds | Toggle caching on Redis failure |
| `principal_cleanup_via_umb` | Every 60 seconds (if UMB enabled) | Process principal events from UMB |
| `principal_cleanup` | Every 7 days (if UMB disabled) | Clean stale principals via BOP |

### Task Guidelines

- All tasks are `@shared_task` (not bound to the app instance) for testability.
- Heavy data operations (migration, orphan cleanup) accept kwargs to control scope (e.g., `tenant_limit`, `binding_uuids`, `dry_run`).
- Never do cache writes inside Celery tasks that also modify the DB -- signals handle cache invalidation automatically.

## Pagination

### v1: LimitOffsetPagination

`StandardResultsSetPagination` -- default limit 10, max 1000. Provides `first`/`next`/`previous`/`last` links.

### v2: Dual Strategy

- **`V2ResultsSetPagination`** (LimitOffset) for workspaces and simple lists. Supports `limit=-1` to disable pagination (fetches count first).
- **`V2CursorPagination`** for role-bindings and roles. Better for large datasets -- no COUNT query. Default page size 10, max 1000. Dynamic ordering via `order_by` query param with dot notation (`role.name`, `group.modified`).

When using `limit=-1`, `V2ResultsSetPagination` calls `queryset.count()` to set `default_limit`. This is an extra query -- acceptable for small datasets but avoid for large ones.

## Gunicorn Configuration

```
workers = POD_CPU_LIMIT * GUNICORN_WORKER_MULTIPLIER (default 2)
threads = GUNICORN_THREAD_LIMIT (default 10)
```

The Redis `BlockingConnectionPool.max_connections` (default 10) should match `threads`. If you increase thread count, increase `REDIS_MAX_CONNECTIONS` accordingly.

Redis socket timeouts are aggressive: `REDIS_SOCKET_CONNECT_TIMEOUT=0.1s`, `REDIS_SOCKET_TIMEOUT=0.1s`. This ensures a Redis outage doesn't block request threads, but means transient network blips will trigger cache misses.

## Database Indexes

- Workspace and RoleV2 `name` fields have GIN trigram indexes (`gin_trgm_ops`) for case-insensitive substring search.
- `Principal.type`, `Principal.service_account_id`, `Principal.user_id`, `Workspace.type`, `RoleV2.type` all have `db_index=True`.
- When adding new filters to v2 list endpoints, check whether a database index supports the query. Add indexes in a migration if the filter will be used in production list operations.

## Performance Testing

The `tests/performance/` directory contains OCM integration sync benchmarks. Tests create 1000 tenants x 10 groups x 10 roles x 10 principals and measure endpoint throughput both synchronously and with `ThreadPoolExecutor(max_workers=10)`. These are not part of the regular test suite -- run via `run_ocm_performance_in_worker` Celery task.
