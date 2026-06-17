# Error Handling Guidelines

## Global Exception Handler

All exceptions pass through `exception_version_handler` (configured in `settings.py` as `EXCEPTION_HANDLER`). It dispatches based on URL path:

- `/api/rbac/v1/...` routes to `custom_exception_handler`
- `/api/rbac/v2/...` routes to `custom_exception_handler_v2`

Both handlers first delegate to DRF's built-in `exception_handler`. If DRF returns `None` (unrecognized exception), each handler checks for project-specific exception types.

## v1 Error Response Format

v1 returns a flat `errors` array. Each entry has `detail`, `status`, and optionally `source`:

```json
{
  "errors": [
    {"detail": "Role name is required", "source": "name", "status": "400"}
  ]
}
```

## v2 Error Response Format (RFC 7807 Problem Details)

v2 uses `application/problem+json` content type. Built by `v2response_error_from_errors()` in `management/utils.py`. Structure:

```json
{
  "status": 400,
  "title": "The request payload contains invalid syntax.",
  "detail": "A role with name 'foo' already exists for this tenant.",
  "errors": [
    {"message": "...", "field": "name"}
  ],
  "instance": "/api/rbac/v2/roles/abc-123/"
}
```

Rules:
- `title` comes from the `PROBLEM_TITLES` dict (keyed by HTTP status code: 400, 401, 403, 404, 409, 500).
- `instance` is included only for PUT/PATCH/DELETE requests.
- `errors` array is included only when field-level errors exist.
- `content_type` must be set to `application/problem+json` on the Response.

## ProblemJSONRenderer

Defined in `api/common/renderers.py`. A thin `JSONRenderer` subclass with `media_type = "application/problem+json"`. Added to `BaseV2ViewSet.renderer_classes` so v2 endpoints can accept `Accept: application/problem+json`.

## Custom Exception Classes

### Shared (`management/exceptions.py`)
- `RequiredFieldError(field_name)` -- missing required field. Stores `field_name`.
- `InvalidFieldError(field, message)` -- invalid field value. Stores `field`.
- `NotFoundError(resource_type, resource_id)` -- resource not found.

### Role v2 (`management/role/v2_exceptions.py`)
Hierarchy rooted at `RoleV2Error`:
- `RoleAlreadyExistsError(name)` -- duplicate role name.
- `PermissionsNotFoundError(missing_permissions)` -- invalid permission strings.
- `RoleDatabaseError(message)` -- unexpected DB error.
- `InvalidRolePermissionsError(message)` -- malformed permission data.
- `RolesNotFoundError(uuids)` -- roles not found (single or bulk).
- `CustomRoleRequiredError(message)` -- operation requires a custom role.

### Authorization (`management/authorization/`)
- `InvalidTokenError` -- invalid JWT token (401).
- `MissingAuthorizationError` -- missing Bearer token (401).
- `UnableMeetPrerequisitesError` -- can't validate token (500).

### Other
- `DualWriteException` (`relation_replicator/relation_replicator.py`) -- wraps replication failures.
- `V1WriteBlockedError` (`tenant_mapping/v2_activation.py`) -- v1 write on v2-activated tenant.
- `InsufficientPrivilegesError` (`group/insufficient_privileges.py`) -- service account privilege check.
- `FieldSelectionValidationError` (`management/utils.py`) -- invalid `?fields=` parameter.
- `GRPCError` (`relation_replicator/relations_api_replicator.py`) -- wrapper for gRPC errors (not an Exception subclass).

## Where to Raise What

### In services (`*_service.py`)
Raise domain exceptions. Never raise DRF exceptions. The service layer must stay framework-agnostic.

```python
# CORRECT -- service raises domain exception
raise NotFoundError("role", role_uuid)
raise RoleAlreadyExistsError(name)

# WRONG -- service raises DRF exception
raise serializers.ValidationError(...)
```

Exception: `workspace/service.py` currently raises `serializers.ValidationError` directly. This is legacy; new code should follow the domain exception pattern.

### In serializers
Catch domain exceptions from services and convert to `serializers.ValidationError` with field attribution:

```python
try:
    return self.service.create(...)
except RequiredFieldError as e:
    raise serializers.ValidationError({e.field_name: str(e)})
except PermissionsNotFoundError as e:
    raise serializers.ValidationError({"permissions": str(e)})
except RoleAlreadyExistsError as e:
    raise serializers.ValidationError({"name": str(e)})
except RoleDatabaseError as e:
    raise serializers.ValidationError({"detail": str(e)})
```

### In views
Views handle infrastructure-level errors that serializers/services shouldn't know about:

- `OperationalError` (serialization failures, deadlocks) -- return 503 or 500 with retry guidance.
- `TimeoutError` -- return 500.
- `ValidationError` (Django core, from model `.save()`) -- flatten with `flatten_validation_error()` and re-raise as `serializers.ValidationError`.
- `RolesNotFoundError` for bulk operations -- build response directly with `v2response_error_from_errors()`.

## AtomicOperationsMixin (v2 Views)

The `AtomicOperationsMixin` in `management/v2_mixins.py` wraps create/update/destroy in SERIALIZABLE transactions with automatic retry (3 attempts). Error handling:

- `SerializationFailure` after retries exhausted: 409 Conflict.
- `DeadlockDetected` after retries exhausted: 500 Internal Server Error.
- Unrecognized `OperationalError`: re-raised with `raise` (no return value).

Subclasses must NOT override `create`/`update`/`destroy`. Override `perform_atomic_create`/`perform_atomic_update`/`perform_atomic_destroy` instead.

## Django ValidationError vs DRF ValidationError

Two different classes with the same name. The codebase uses both:

- `django.core.exceptions.ValidationError` -- raised by model `.save()` and `.full_clean()`. Has `message_dict` and `error_dict` attributes.
- `rest_framework.serializers.ValidationError` -- raised in serializers and views. Handled by DRF's exception handler. Has `detail` attribute.

Pattern: catch Django `ValidationError` in views, convert to DRF `serializers.ValidationError`:

```python
except ValidationError as e:
    for field, error_message in flatten_validation_error(e):
        if "unique_workspace_name_per_parent" in error_message:
            message = "A workspace with the same name already exists."
            break
    raise serializers.ValidationError(message)
```

`flatten_validation_error()` in `management/utils.py` normalizes Django's `ValidationError` variants into `(field, message)` tuples.

## IntegrityError Handling

`IntegrityError` is NOT handled by DRF's default handler. Both v1 and v2 custom handlers catch it explicitly:

- v1: wraps in `{"errors": [{"detail": ..., "source": basename, "status": "400"}]}`.
- v2: wraps in Problem JSON via `v2response_error_from_errors()`.

In services, prefer catching `IntegrityError` and raising a domain exception:

```python
except IntegrityError as e:
    if "unique" in str(e).lower():
        raise RoleAlreadyExistsError(name)
    raise RoleDatabaseError()
```

## Logging Conventions

- `logger.exception(...)` -- for unexpected errors (includes traceback). Use in catch blocks for DB errors, replication failures.
- `logger.error(...)` -- for known error conditions without traceback (e.g., missing org_id on tenant, gRPC failures).
- `logger.warning(...)` -- for degraded but recoverable situations (missing data, skipped operations, fallback paths).

Pattern in views for concurrency errors:

```python
logger.exception("SerializationFailure in %s operation", operation_name)
```

Pattern in services for expected domain errors: no logging needed (the caller decides).

## Rules for New Code

1. **New v2 domain errors**: subclass the relevant base (`RoleV2Error`, etc.) or use the shared exceptions in `management/exceptions.py`.
2. **Always include field attribution** in validation errors: `raise serializers.ValidationError({"field_name": "message"})`, not bare strings.
3. **Never let domain exceptions leak to the HTTP layer unhandled.** If a new exception type isn't caught by the global handler, DRF returns a generic 500. Either add it to `custom_exception_handler_v2` or catch it in the serializer/view.
4. **v2 error responses must use Problem JSON.** Use `v2response_error_from_errors()` when building error responses manually in views.
5. **Concurrency errors** in v2 views: use `AtomicOperationsMixin`. In workspace views: use the `_handle_operational_error` pattern.
6. **Do not catch exceptions you cannot handle.** Use bare `raise` (not `raise e`) to preserve tracebacks. Return `None` from error handlers to signal "not my problem."
7. **Service layer exceptions must be plain Python** (not DRF). This keeps services testable without HTTP infrastructure.
8. **Use `raise_validation_error(source, message)`** from `management/utils.py` for simple v1 validation errors in query parameter parsing.
