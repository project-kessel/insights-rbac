# RBAC Review Knowledge for CodeRabbit

Domain rules CodeRabbit checks every insights-rbac PR diff against. Each rule is short on purpose: the linked guideline section is the source of truth, and this file only says what to flag. When a rule and its guideline disagree, the guideline wins and this file should be updated.

The file is loaded through `knowledge_base.code_guidelines` in [`.coderabbit.yaml`](../.coderabbit.yaml). The flaggable checks are also condensed into `reviews.path_instructions` there, because path instructions are applied to every matching file in the diff.

## 1. Multi-tenancy invariants

- Every business model inherits `TenantAwareModel`.
- Every queryset over tenant-scoped data is filtered by tenant (`request.tenant`, or an explicit `tenant=` argument in services, Celery tasks and management commands).
- A v2 viewset that overrides `get_queryset()` must call `super().get_queryset()` or filter by tenant explicitly.

Flag: a new `Model.objects.all()`, `.filter(...)` or `.get(...)` on a tenant-scoped model with no tenant constraint; a new model that holds business data but does not inherit `TenantAwareModel`; a `get_queryset()` override that drops tenant filtering; existence checks in permission classes without `tenant=request.tenant`.

Do not flag: join-through models (`RoleBindingGroup`, `RoleBindingPrincipal`, `ExtTenant`, `ExtRoleRelation`), which belong to a tenant through their parent; querysets that intentionally include the public tenant, such as `RoleV2QuerySet.for_tenant` (`Q(tenant=tenant) | Q(tenant__tenant_name="public")`).

References: [security: Multi-Tenant Isolation](security-guidelines.md#multi-tenant-isolation), [database: Multi-tenancy](database-guidelines.md#multi-tenancy)

## 2. V2 viewset patterns

- New v2 viewsets inherit `BaseV2ViewSet` (`rbac/management/base_viewsets.py`).
- v2 viewsets that write also inherit `AtomicOperationsMixin` (`rbac/management/v2_mixins.py`).
- Subclasses of `AtomicOperationsMixin` override `perform_atomic_create`, `perform_atomic_update` and `perform_atomic_destroy`. They never override `create`, `update` or `destroy`.

Flag: a v2 viewset built from DRF mixins or `GenericViewSet` instead of `BaseV2ViewSet`; a v2 viewset with write actions but no `AtomicOperationsMixin`; any `def create(`, `def update(` or `def destroy(` on an `AtomicOperationsMixin` subclass. The mixin's `__init_subclass__` rejects these overrides when the class is defined, so a diff that adds one will fail at import time.

Do not flag: `WorkspaceViewSet`, which predates the mixin and handles transactions and concurrency errors itself (`_handle_operational_error`).

References: [api-contracts: v2 BaseV2ViewSet](api-contracts-guidelines.md#v2-basev2viewset-managementbase_viewsetspy), [api-contracts: AtomicOperationsMixin](api-contracts-guidelines.md#atomicoperationsmixin-managementv2_mixinspy)

## 3. Service layer ownership

- Business logic lives in service modules (`service.py` / `*_service.py`), not in views or serializers.
- Services raise plain Python domain exceptions (from `management/exceptions.py` or a domain-specific base such as `RoleV2Error`). They never raise DRF exceptions.
- Serializers catch domain exceptions and convert them to `serializers.ValidationError` with field attribution (`{"field": "message"}`).

Flag: `from rest_framework` imports or `serializers.ValidationError` / `PermissionDenied` / `NotFound` raised in a service; multi-step business logic (queries plus mutations plus replication) added directly in a view or serializer; validation errors raised as bare strings without a field key.

Do not flag: the existing `serializers.ValidationError` in `workspace/service.py`, which is documented legacy. Do flag new code that copies it.

References: [error-handling: Where to Raise What](error-handling-guidelines.md#where-to-raise-what), [error-handling: Rules for New Code](error-handling-guidelines.md#rules-for-new-code)

## 4. Two-layer access control (v2)

- Every v2 viewset declares an `*AccessPermission` class in `permission_classes` (endpoint-level 403).
- Every v2 viewset also needs a `*AccessFilterBackend`, listed first in `filter_backends`, so lists and detail lookups only see objects the caller can access.
- Detail views return 404, not 403, for objects the caller cannot access, so resource IDs cannot be probed.
- When Kessel is unreachable, access checks default to deny.

Flag: a new v2 viewset or action with no access permission class; a new v2 viewset or action with no access filter backend; an access filter backend placed after other filters; a detail path (`retrieve`, `get_object`, or a `detail=True` action) that returns `403` or raises `PermissionDenied` for an object the caller cannot see, instead of 404 from the filtered queryset; an access check that returns `True` on a Kessel error.

Do not flag: endpoint-level 403 from the permission class (for example a non-admin calling a tenant-level write), which is correct.

References: [security: Two-layer v2 access control](security-guidelines.md#two-layer-v2-access-control), [security: Existence Leakage Prevention](security-guidelines.md#existence-leakage-prevention), [security: Kessel integration (v2)](security-guidelines.md#kessel-integration-v2)

## 5. Error format rules

- v2 errors use RFC 7807 Problem JSON, rendered by `ProblemJSONRenderer`. Error responses built by hand in views use `v2response_error_from_errors()`.
- v1 errors use the flat `{"errors": [...]}` array.
- The two formats are never mixed, and v1 behavior is not changed.

Flag: a v2 view returning `Response({"errors": ...})` or `Response({"detail": ...}, status=4xx)` built by hand; a v1 view returning Problem JSON; any change to an existing v1 error shape; a new domain exception that is neither handled by `custom_exception_handler_v2` nor caught in the serializer or view (it would surface as a generic 500).

References: [error-handling: v2 Error Response Format](error-handling-guidelines.md#v2-error-response-format-rfc-7807-problem-details), [error-handling: v1 Error Response Format](error-handling-guidelines.md#v1-error-response-format)

## 6. Feature flag gating

- v2 routes are registered only when `V2_APIS_ENABLED=True`.
- v2 write endpoints also use the `V2WriteRequiresWorkspacesEnabled` permission.
- Dev-only bypasses (`ALLOW_ANY`, `DEVELOPMENT`, `IT_BYPASS_TOKEN_VALIDATION`, `IT_BYPASS_IT_CALLS`, `DEBUG`) are never defaulted to `True` or enabled in deploy config.

Flag: a v2 route registered outside the `V2_APIS_ENABLED` guard; a v2 write action without `V2WriteRequiresWorkspacesEnabled`; a new setting or ClowdApp value that turns on a dev-only bypass.

References: [security: Feature flag gating](security-guidelines.md#feature-flag-gating), [api-contracts: v2 routing](api-contracts-guidelines.md#v2-managementv2_urlspy), [security: Development-Only Features](security-guidelines.md#development-only-features)

## 7. Outbox and transaction constraints

- Outbox writes (`OutboxReplicator.replicate(...)`, dual-write handlers) happen inside the same transaction as the data change they describe.
- External calls (Kessel, BOP, IT service, Kafka) are not placed between a data mutation and its outbox write, and are not made outside a transaction boundary on a write path.
- v2 write paths use SERIALIZABLE isolation through `@atomic`, `@atomic_with_retry`, or `AtomicOperationsMixin`.

Flag: a replication or dual-write call outside `transaction.atomic()` / `@atomic` / `perform_atomic_*`; a write path that commits data and then replicates in a separate transaction; a new v2 mutation with no SERIALIZABLE wrapper.

References: [database: Debezium Outbox Pattern](database-guidelines.md#debezium-outbox-pattern), [database: Transaction Management](database-guidelines.md#transaction-management)

## 8. UUID conventions

- New models use a UUID v7 primary key: `id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False, unique=True)` with `import uuid_utils.compat as uuid`.
- APIs never expose integer primary keys. v1 models expose their `uuid` field.

Flag: a new model with an auto-increment PK or `uuid4` default; a serializer field, URL kwarg, `lookup_field` or response key that exposes `id` / `pk` of a v1 integer-PK model.

References: [database: Primary Keys and UUIDs](database-guidelines.md#primary-keys-and-uuids)

## 9. Authentication layers

There are four distinct trust boundaries, and each has its own entry point:

1. `x-rh-identity` header, decoded by `IdentityHeaderMiddleware`. Identity is read through `extract_header()`, never from request body or query params.
2. Pre-shared key (`X-RH-RBAC-PSK` + org and client headers), validated in `build_user_from_psk()`. PSK users become `system=True, admin=True`.
3. ITSSO JWT bearer token, validated by `ITSSOTokenValidator`. The token's `user_id` must be in `SYSTEM_USERS`.
4. Internal `/_private/` APIs, through `InternalIdentityHeaderMiddleware`. `/_private/_a2s/` is the exception and uses the public middleware.

Flag: identity, `org_id` or admin status taken from user-controlled input without validation by the applicable authentication entry point (x-rh-identity, PSK, or JWT); inline `user.system and user.admin` checks instead of `check_system_user_access()`; a new PSK client that should not be admin but keeps the default; logging of `SERVICE_PSKS`, `SYSTEM_USERS`, tokens or identity headers; changes to middleware order; a new `/_private/` route that skips internal auth.

References: [security: Authentication Layers](security-guidelines.md#authentication-layers), [security: System user access](security-guidelines.md#system-user-access), [security: Secrets and Configuration](security-guidelines.md#secrets-and-configuration)

## Using these rules from another repository

CodeRabbit shares review rules between repositories through `.coderabbit.yaml`, not through markdown files. A repository can inherit this repo's path instructions with:

```yaml
inheritance: true
remote_config:
  repository: "project-kessel/insights-rbac"
  ref: "master"
  path: ".coderabbit.yaml"
```

Most rules above describe insights-rbac internals (`BaseV2ViewSet`, `TenantAwareModel`, the outbox), so they only apply to code that has the same structure. For more detail, the raw file is at `https://raw.githubusercontent.com/project-kessel/insights-rbac/master/docs/coderabbit-rbac-knowledge.md`.
