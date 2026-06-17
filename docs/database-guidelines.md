# Database Guidelines

Rules and conventions for database work in insights-rbac. Derived from the actual codebase -- not generic advice.

## Multi-tenancy

Every business model inherits `TenantAwareModel` (defined in `rbac/api/models.py`), which adds a non-nullable `ForeignKey(Tenant, on_delete=CASCADE)`. The only exceptions are join-through models like `RoleBindingGroup`, `RoleBindingPrincipal`, and `ExtTenant`/`ExtRoleRelation` which belong to a tenant implicitly through their parent.

- `BaseV2ViewSet.get_queryset()` automatically filters by `request.tenant`. All v2 querysets must include tenant filtering.
- v1 models use `FilterQuerySet` with `.public_tenant_only()` to distinguish system-wide (public tenant) records from tenant-specific ones.
- The `Tenant` model stores `org_id` (unique, indexed), `account_id`, and a `ready` boolean. A singleton "public" tenant (`tenant_name="public"`) holds system/platform roles and permissions.
- Custom querysets that cross tenant boundaries (e.g., `RoleV2QuerySet.for_tenant`) explicitly include the public tenant: `Q(tenant=tenant) | Q(tenant__tenant_name="public")`.

When writing new queries, never rely on implicit tenant scoping. Always filter explicitly.

## Primary Keys and UUIDs

Two patterns coexist:

1. **v1 models** (Role, Group, Policy, Principal): auto-incrementing `id` PK + separate `uuid = UUIDField(default=uuid4, unique=True)` for external exposure. Never expose the integer PK in APIs.
2. **v2 models** (Workspace, RoleV2, RoleBinding): `id = UUIDField(primary_key=True, default=uuid7)` using time-ordered UUID v7 from `uuid_utils.compat`. New models should follow this pattern.

When adding a new model, use UUID v7 as the primary key:
```python
import uuid_utils.compat as uuid
id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False, unique=True)
```

## Model Conventions

### Required patterns
- All tenant-scoped models: inherit `TenantAwareModel`.
- Timestamps: `created = models.DateTimeField(default=timezone.now)` and `modified = AutoDateTimeField(default=timezone.now)`. The `AutoDateTimeField` (in `management/rbac_fields.py`) auto-sets to `timezone.now()` on every save via `pre_save`.
- Models with custom validation: call `self.full_clean()` in `save()` before `super().save()` to enforce model-level validation (v2 pattern used in Workspace, RoleV2). This ensures constraints and `clean()` logic run even outside serializers.
- Custom managers/querysets: attach as `objects = MyQuerySet.as_manager()` or `objects = MyManager()`.

### Naming
- Table names are auto-generated: `management_workspace`, `management_rolev2`, `api_tenant`.
- Related names should be explicit: `related_name="bindings"`, `related_name="group_entries"`.
- `TextChoices` for type fields: define as inner class `Types` on the model.

### Field types
- Short identifiers: `CharField(max_length=N)` with explicit `max_length`.
- Long text: `TextField` (no max_length at DB level).
- JSON data: `JSONField(default=dict)`.
- Boolean flags: `BooleanField(default=False)` -- always provide a default.

## Proxy Models

v2 uses proxy models for role subtypes: `CustomRoleV2`, `SeededRoleV2`, `PlatformRoleV2` all proxy `RoleV2`. Each has a `TypedRoleV2Manager` that auto-filters by type and a `TypeValidatedRoleV2Mixin` that enforces the type on init and save. Use proxy models when you need type-specific behavior on a single table, not separate tables.

## Constraints and Indexes

### UniqueConstraint patterns
- Tenant-scoped uniqueness: `UniqueConstraint(fields=["name", "tenant"], name="unique role name per tenant")`. Every name-like field that must be unique should be scoped to tenant.
- Conditional uniqueness: `UniqueConstraint(fields=[...], condition=Q(...))` for partial unique indexes. Example: only one root/default workspace per tenant.
- Case-insensitive uniqueness: `UniqueConstraint(Upper("name"), "parent", name="...")`.
- Constraint names: lowercase, descriptive, spaces allowed (Django handles quoting).

### Index patterns
- GIN trigram indexes for case-insensitive text search (`icontains`, `iregex`):
  ```python
  GinIndex(fields=["name"], name="modelname_name_trgm_idx", opclasses=["gin_trgm_ops"])
  ```
  Requires `TrigramExtension()` in the migration.
- Composite indexes on `AuditLog` for common query patterns: `(tenant, created)`, `(tenant, resource_type)`.
- `db_index=True` on fields used in frequent filters: `type`, `org_id`, `service_account_id`, `user_id`.

### CheckConstraint
Used for data integrity beyond what field types provide:
```python
CheckConstraint(condition=~Q(source=""), name="role binding principal has source")
```

## Foreign Key Deletion Rules

- `CASCADE`: default for tenant FK and parent-child ownership (RoleBinding -> RoleV2, Access -> Role).
- `PROTECT`: for relationships where deletion should be blocked (RoleBindingGroup -> Group, Workspace parent -> self). Workspace's self-referencing parent uses `PROTECT` to prevent deleting a workspace that has children.
- `SET_NULL`: only for optional audit references (AuditLog -> Tenant).
- Never use `SET_DEFAULT` or `DO_NOTHING`.

## Workspace Hierarchy and Recursive SQL

Workspaces form a tree via `parent = ForeignKey("self", on_delete=PROTECT)`. Four types: `root` (exactly one per tenant, no parent), `default` (one per tenant, parent=root), `standard` (user-created), `ungrouped-hosts` (system).

Recursive queries use `WITH RECURSIVE` CTEs via `RawSQL` or `connection.cursor()`:

```python
# Ancestors -- returns QuerySet (composable)
Workspace.objects.filter(id__in=RawSQL(sql, [self.id, self.id]))

# Max depth -- returns scalar (raw cursor)
with connection.cursor() as cursor:
    cursor.execute(sql, [self.id])
    max_depth = cursor.fetchone()[0]
```

Use `RawSQL` + `filter(id__in=...)` when you need a composable QuerySet. Use raw cursors for scalar aggregates. Always parameterize with `%s` -- never interpolate.

The `WorkspaceManager` provides convenience methods: `.root(tenant=t)`, `.default(tenant=t)`, `.built_in(tenant=t)`, `.standard(tenant=t)`, `.descendant_ids_with_parents(ids, tenant_id)`.

## Debezium Outbox Pattern

The `Outbox` model (`management/debezium/model.py`) implements the transactional outbox pattern for CDC via Debezium. It captures relation changes that must be replicated to Kessel/SpiceDB.

Flow: service layer -> `OutboxReplicator.replicate(event)` -> `Outbox.save(force_insert=True)` -> `Outbox.delete()`.

The record is saved and immediately deleted within the same transaction. Debezium reads the PostgreSQL WAL to capture the insert, so the record does not need to persist. This avoids table bloat.

Key rules:
- Outbox writes must happen inside the same `transaction.atomic()` as the business data mutation. This guarantees atomicity between the data change and the replication event.
- The `aggregatetype` field controls Debezium topic routing: `"relations-replication-event"` or `"workspace"`.
- The `aggregateid` field is the partition key (currently environment-level). All events within a partition are ordered.
- Empty replication events (no adds, no removes) are logged as warnings and skipped.

## Transaction Management

### SERIALIZABLE isolation (v2 write paths)
All v2 mutations use `pgtransaction.atomic(isolation_level=SERIALIZABLE)` via:

1. **`@atomic` decorator** (in `management/atomic_transactions.py`): for service-layer functions.
2. **`@atomic_with_retry(retries=N)`**: same, with automatic retry on serialization failure.
3. **`AtomicOperationsMixin`** (in `management/v2_mixins.py`): for ViewSets. Wraps `create`/`update`/`destroy` in SERIALIZABLE transactions with retry. Override `perform_atomic_create` etc., never override `create` directly.

The mixin catches `SerializationFailure` (returns 409) and `DeadlockDetected` (returns 500).

For tests, set `ATOMIC_RETRY_DISABLED=True` to downgrade to plain `transaction.atomic()` (avoids serialization conflicts in test isolation).

### select_for_update
Used in bulk operations like tenant bootstrapping:
```python
tenants = Tenant.objects.select_for_update().filter(pk__in=pks)
```

### General rule
Never perform outbox writes or external calls outside a transaction boundary. The dual-write pattern requires atomicity between the DB mutation and the outbox insert.

## Custom QuerySets

Place in a separate `queryset.py` file (e.g., `role_binding/queryset.py`, `role/queryset.py`). Attach via `objects = MyQuerySet.as_manager()`.

QuerySet methods should be chainable and return `self`-type querysets. Use method names that read naturally: `.for_tenant()`, `.for_role()`, `.assignable()`, `.orphaned()`.

For domain-specific managers (like `WorkspaceManager`), use a separate manager class that delegates queryset methods.

## Signals

Signals drive cache invalidation (Redis `AccessCache`) and Kafka sync events. They are conditionally connected based on settings:
```python
if settings.ACCESS_CACHE_ENABLED and settings.ACCESS_CACHE_CONNECT_SIGNALS:
    signals.post_save.connect(handler, sender=Model)
```

Signal handlers must check `skip_purging_cache_for_public_tenant(instance.tenant)` to avoid unnecessary cache operations on the public tenant.

## Migration Conventions

- Generated by `makemigrations`, stored in `rbac/management/migrations/` (84+ files) and `rbac/api/migrations/`.
- Migration names: auto-generated is fine; no custom naming convention enforced.
- For PostgreSQL extensions (like `pg_trgm`), use Django's built-in operations: `TrigramExtension()`.
- Data migrations that touch relations use `connection.cursor()` for raw SQL.
- Always test migrations against a real PostgreSQL instance -- SQLite is not used.

## Database Configuration

PostgreSQL 16, configured in `rbac/rbac/database.py`. Single `default` database. SSL mode configurable via `PGSSLMODE` env var (defaults to `prefer`). Clowder-aware: reads connection details from `LoadedConfig` when `CLOWDER_ENABLED=True`.

Local development uses port 15432 (`make start-db`).
