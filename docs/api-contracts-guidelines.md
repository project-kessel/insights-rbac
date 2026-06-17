# API Contracts Guidelines

Rules and patterns for maintaining API contract consistency in insights-rbac.

## Dual API Versions

Two API versions coexist. They share models and database but differ in routing, response format, pagination, error handling, and access control.

- **v1** (`/api/rbac/v1/`): Always registered. Group-based RBAC. Uses `DefaultRouter`, `ModelViewSet` mixins, `StandardResultsSetPagination` (limit/offset), and JSON error arrays.
- **v2** (`/api/rbac/v2/`): Conditionally registered via `V2_APIS_ENABLED` setting in `rbac/rbac/urls.py:46`. Workspace-based model. Uses `V2Router` (with custom batch routes), `BaseV2ViewSet`, and RFC 7807 Problem Details errors.

Never register v2 routes unconditionally. The feature flag check in `urls.py` is the gate.

## URL Routing

### v1 (`management/urls.py`)
- `DefaultRouter` registers `GroupViewSet`, `RoleViewSet`, `PermissionViewSet`, `AuditLogViewSet`.
- `PrincipalView` and `AccessView` are plain `APIView` at fixed paths.

### v2 (`management/v2_urls.py`)
- `V2Router` extends `DefaultRouter` with two custom `Route` entries for batch operations:
  - `{prefix}:batchCreate/` maps POST to `batch_create` action.
  - `{prefix}:batchDelete/` maps POST to `bulk_destroy` action.
- Registered viewsets: `WorkspaceViewSet`, `RoleBindingViewSet`, `RoleV2ViewSet`.

When adding a v2 endpoint, register on `V2Router` in `v2_urls.py`. For batch operations, use the existing `:batchCreate` / `:batchDelete` route pattern (colon prefix, camelCase action name).

## ViewSet Patterns

### v1: Manual mixin composition
```python
class RoleViewSet(
    mixins.CreateModelMixin, mixins.DestroyModelMixin,
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin, viewsets.GenericViewSet,
):
```
- Uses `lookup_field = "uuid"` for detail routes.
- Wraps write operations in `transaction.atomic()` inline.
- `get_serializer_class()` switches serializer based on `request.path` and `request.method`.

### v2: `BaseV2ViewSet` (`management/base_viewsets.py`)
Provides the same mixin set as above plus:
- `renderer_classes` includes `ProblemJSONRenderer` for `application/problem+json` content negotiation.
- `pagination_class = V2ResultsSetPagination` by default (offset-based with `limit=-1` support).
- `get_queryset()` auto-filters by `request.tenant` and orders by `name, -modified`.

All new v2 viewsets must inherit `BaseV2ViewSet`.

### AtomicOperationsMixin (`management/v2_mixins.py`)
v2 viewsets that perform writes should also inherit `AtomicOperationsMixin`:
- Wraps `create`, `update`, `destroy` in `pgtransaction.atomic(isolation_level=SERIALIZABLE, retry=self.atomic_retry)` where `atomic_retry=3`.
- Subclasses of `AtomicOperationsMixin` must NOT override `create()`/`update()`/`destroy()` directly. Override `perform_atomic_create()`, `perform_atomic_update()`, `perform_atomic_destroy()` instead. A `__init_subclass__` check enforces this at class definition time.
- Handles `SerializationFailure` (409) and `DeadlockDetected` (500) after retry exhaustion.

## Pagination

### v1: `StandardResultsSetPagination` (limit/offset)
Response envelope:
```json
{"meta": {"count": N, "limit": L, "offset": O}, "links": {"first": "...", "next": "...", "previous": "...", "last": "..."}, "data": [...]}
```
Default limit: 10. Max limit: 1000.

### v2 offset: `V2ResultsSetPagination`
Same envelope as v1. Adds `limit=-1` to disable pagination (returns all results).

### v2 cursor: `V2CursorPagination`
Response envelope (no `count`, no `first`/`last`):
```json
{"meta": {"limit": L}, "links": {"next": "...", "previous": "..."}, "data": [...]}
```
Ordering uses dot-notation field mapping (e.g., `group.name`, `role.modified`). Each model type has its own `FIELD_MAPPING` dict. Invalid `order_by` values raise `ValidationError` with the list of valid fields.

Workspaces use offset pagination (`V2ResultsSetPagination` subclass, `max_limit=3000`). Role bindings and roles use cursor pagination (`V2CursorPagination`).

## Serializer Conventions

### v1 serializers
- Inherit `serializers.ModelSerializer` (often with `SerializerCreateOverrideMixin` which auto-injects `tenant` on create).
- Use `lookup_field = "uuid"` on the viewset, not the serializer.
- Dynamic field inclusion via `get_serializer()` passing a `fields` kwarg (e.g., `RoleDynamicSerializer`).

### v2 serializers: input/output split
v2 endpoints use separate serializers for input validation and output formatting:
- **Input serializers** (`*InputSerializer`, `*RequestSerializer`): Validate query params or request body. Usually `serializers.Serializer` (not `ModelSerializer`). Contain `validate_*` methods. Service layer calls happen in `create()`/`save()`.
- **Output serializers** (`*OutputSerializer`, `*ResponseSerializer`): Format response data. Use `SerializerMethodField` extensively for dynamic field selection.

Example from role bindings:
```
RoleBindingListInputSerializer   -> validates query params
RoleBindingListOutputSerializer  -> formats response with field masking
```

### Field selection (`?fields=`)
v2 endpoints support a `fields` query parameter for response field masking. The parsing infrastructure lives in `management/utils.py`:
- `FieldSelection` base class with `VALID_ROOT_FIELDS` and `VALID_NESTED_FIELDS` class vars.
- Subclass per endpoint (e.g., `RoleBindingFieldSelection`, `RoleFieldSelection`).
- Syntax: `subject(group.name,id),role(name),last_modified`.

For roles, field selection uses simple set-based masking (pop fields from `self.fields` in `__init__`). For role bindings, the `RoleBindingFieldMaskingMixin` provides shared `_build_subject_data`, `_build_role_data`, `_build_resource_data` helpers.

When adding fields parameters: subclass `FieldSelection`, define valid fields, add a `validate_fields` method to the input serializer.

### NUL byte sanitization
v2 input serializers strip `\x00` bytes in `to_internal_value()`. Follow this pattern for any new v2 input serializer.

### Dotted query param remapping
Some query params use dots (e.g., `resource.tenant.org_id`). Remap in `to_internal_value()` via a `DOTTED_PARAM_MAP` dict to underscore field names. See `RoleBindingListInputSerializer`.

## Error Response Formats

### v1 errors
```json
{"errors": [{"detail": "...", "source": "field_name", "status": "400"}]}
```
Dispatched by `custom_exception_handler()` in `api/common/exception_handler.py`.

### v2 errors: RFC 7807 Problem Details
```json
{"status": 400, "title": "The request payload contains invalid syntax.", "detail": "...", "errors": [{"message": "...", "field": "..."}]}
```
Dispatched by `custom_exception_handler_v2()`. Content type: `application/problem+json`.

The version-routing handler `exception_version_handler` checks the request path for `/v2/` to select the correct handler. This is configured globally in `REST_FRAMEWORK["EXCEPTION_HANDLER"]`.

### Domain exceptions (`management/exceptions.py`)
- `NotFoundError(resource_type, resource_id)` -- converted to 404 Problem Details.
- `InvalidFieldError(field, message)` -- converted to 400 Problem Details.
- `RequiredFieldError(field_name)` -- converted to 400 Problem Details.

Raise these from service layer code; the global exception handler formats them. Do NOT catch and re-raise as `serializers.ValidationError` in views -- let them propagate.

### Problem titles mapping
Defined in `management/utils.py:PROBLEM_TITLES`. Standard titles for 400, 401, 403, 404, 409, 500. Do not invent custom titles outside this mapping.

## Content Negotiation

- v1: Only `JSONRenderer` (configured globally in `REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]`).
- v2: `JSONRenderer` + `ProblemJSONRenderer` (added via `BaseV2ViewSet.renderer_classes`). The `ProblemJSONRenderer` accepts `application/problem+json` in the `Accept` header but renders standard JSON.

## OpenAPI Spec

### Source of truth
v2 spec is generated from TypeSpec at `docs/source/specs/typespec/main.tsp`. Output goes to `docs/source/specs/v2/openapi.json` and `openapi.yaml`.

### Serving
- v1: `GET /api/rbac/v1/openapi.json` serves `docs/source/specs/openapi.json`.
- v2: `GET /api/rbac/v2/openapi.json` serves `docs/source/specs/v2/openapi.json`.

Both are static file reads, not auto-generated from DRF.

### Regenerating
```bash
make generate_v2_spec   # Requires TypeSpec installed in docs/source/specs/typespec/
```

When changing v2 API contracts, update `main.tsp` first, regenerate the spec, then implement. The TypeSpec file is the contract; the code must match it.

### TypeSpec conventions in `main.tsp`
- Reusable models: `ListResponse<Item>`, `ItemResponse<Item, StatusCode>`, `ProblemDetails<Status>`.
- Pagination models: `CursorPaginationMeta/Links` (for roles, role bindings) and `OffsetPaginationMeta/Links` (for workspaces).
- `FieldMask`, `Limit`, `Cursor`, `Offset`, `OrderBy` are custom scalars with documentation.
- Batch operations use `:batchCreate/` and `:batchDelete/` route suffixes (colon prefix).
- All error responses use `Problems.CommonProblems` (401, 403, 500) plus specific `Problem400`/`Problem404`.

## Multi-tenancy in Serializers

- v1: `SerializerCreateOverrideMixin.create()` injects `tenant=self.context["request"].tenant` into `Model.objects.create()`. v1 serializers for tenant-aware models should inherit this mixin.
- v2: Service layer handles tenant. Serializers call `self.context["request"].tenant` and pass to service methods.

Never rely on implicit tenant. Always explicitly pass or filter by tenant.

## Access Control Integration

v2 viewsets declare two layers:
1. `permission_classes` tuple -- endpoint-level permission checks (e.g., `WorkspaceAccessPermission`, `RoleBindingKesselAccessPermission`).
2. `filter_backends` -- queryset filtering (e.g., `WorkspaceAccessFilterBackend`).

The permission class order matters. `V2WriteRequiresWorkspacesEnabled` is a guard that blocks writes when workspaces feature is disabled; it goes last. `RoleBindingSystemUserAccessPermission` goes first to allow system-to-system calls to bypass Kessel checks.

## Service Layer Pattern

v2 business logic lives in `service.py` files, not views or serializers:
- Views validate input, call service, format output.
- Serializers parse/validate data, delegate creation to service via `create()`/`save()`.
- Services handle validation, database operations, relation replication, event publishing.

Keep views thin. Services are the transaction boundary (or `AtomicOperationsMixin` wraps them).
