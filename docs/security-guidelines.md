# Security Guidelines

Security conventions and patterns for insights-rbac. Rules here are specific to this codebase; generic web security advice is out of scope.

## Authentication Layers

### 1. x-rh-identity Header (primary)

All public API requests are authenticated via a base64-encoded `x-rh-identity` header injected by the API gateway (3scale). The `IdentityHeaderMiddleware` in `rbac/rbac/middleware.py` decodes and validates it.

Rules:
- Never trust raw user input for identity. Always read from the decoded header via `extract_header()` (`rbac/api/serializers.py`).
- `org_id` is mandatory. Middleware returns 400 if absent.
- Service accounts must provide a non-blank `client_id`. Middleware returns 400 otherwise.
- Cross-account requests require both `user.internal == True` and an `@redhat.com` email. Middleware returns 401 if either check fails.

### 2. Pre-Shared Key (S2S via PSK)

Internal service-to-service callers authenticate via `X-RH-RBAC-PSK`, `X-RH-RBAC-ORG-ID`, and `X-RH-RBAC-CLIENT-ID` headers. Validated in `build_user_from_psk()` (`rbac/management/utils.py`).

Rules:
- PSK validation checks both primary and alt-secret from `SERVICE_PSKS` JSON config.
- PSK-authenticated users are set as `system=True, admin=True`. If you add a new PSK client that should not be admin, you must change this default.
- All three headers (PSK, org_id, client_id) must be present, or the auth attempt is skipped entirely.

### 3. ITSSO JWT Token (S2S via Bearer)

Service accounts authenticate via `Authorization: Bearer <token>`. Validated in `ITSSOTokenValidator` (`rbac/management/authorization/token_validator.py`).

Rules:
- Token validation checks issuer, expiry, and optional scope claims against ITSSO JWKS.
- The `user_id` from the token must exist in `SYSTEM_USERS` setting, or auth fails.
- `allow_any_org` in the system user config controls whether the caller can set arbitrary org_id via headers. Token org_id and header org_id must match when `allow_any_org` is False.
- `IT_BYPASS_TOKEN_VALIDATION` returns a mocked user. Never enable in production.

### 4. Internal API Auth

Requests to `/_private/` use `InternalIdentityHeaderMiddleware` (`rbac/internal/middleware.py`).

Rules:
- `/_private/_s2s/` paths use PSK or JWT token auth (same as above), returning 403 on failure.
- Other `/_private/` paths require `x-rh-identity` with identity type `Associate` or `X509`. All other types are rejected.
- A2S paths (`/_private/_a2s/`) are an exception: they route through public `IdentityHeaderMiddleware` auth, not internal auth.

## Authorization Patterns

### Two-layer v2 access control

Every v2 endpoint must have both layers:

1. **Permission class** (`*AccessPermission`): coarse-grained 403 checks. Defined in `permission_classes` on the viewset.
2. **Filter backend** (`*AccessFilterBackend`): queryset-level filtering for list operations. Defined in `filter_backends` on the viewset.

Rules:
- Permission classes check if the user can call the endpoint at all.
- Filter backends narrow the queryset to only authorized objects. For detail views, this produces 404 (not 403) for inaccessible objects, preventing existence leakage.
- Always list the access filter backend first in `filter_backends` so access filtering happens before other filters.

### v1 permission pattern

v1 endpoints use `request.user.access` (preloaded in middleware) to check read/write permissions:
```python
if request.user.admin:
    return True
if request.method in permissions.SAFE_METHODS:
    return bool(request.user.access.get("role", {}).get("read", []))
return bool(request.user.access.get("role", {}).get("write", []))
```

### Kessel integration (v2)

v2 uses Kessel Inventory API for fine-grained checks:
- `CheckForUpdate`: single-resource permission check (detail/create/move operations).
- `StreamedListObjects`: returns all resources a principal can access (list operations).

Rules:
- When Kessel is unreachable, `_call_inventory()` returns a safe default (False for checks, empty set for lookups). Never default to granting access.
- Resource type allowlists are enforced. `RoleBindingKesselAccessPermission.ALLOWED_RESOURCE_TYPES` only allows `{"workspace", "tenant"}`. Unknown types are denied.
- Tenant-level authorization uses `request.user.admin` (org-admin check), not Kessel. This is intentional for the current milestone.

### System user access

System users (S2S) follow a unified decision tree in `check_system_user_access()` (`rbac/management/permissions/system_user_utils.py`):
- Non-system user: continue with normal checks.
- System user without admin: denied.
- System admin: allowed (except move, which requires target validation).

Rules:
- Always use `check_system_user_access()` for system user checks. Do not inline `user.system and user.admin` checks, which creates behavior drift.
- System users bypass Kessel checks entirely. Access is determined solely by the `admin` attribute.

### Feature flag gating

Rules:
- v2 routes only register when `V2_APIS_ENABLED=True` (`rbac/rbac/urls.py`).
- v2 writes require the org to have workspaces enabled (`V2WriteRequiresWorkspacesEnabled`). v1 writes are blocked for orgs with workspaces enabled (`V1WriteBlockedWhenWorkspacesEnabled`).
- `ALLOW_ANY` env var bypasses all permission checks. It must never be set in production.

## Multi-Tenant Isolation

### TenantAwareModel

All business models inherit `TenantAwareModel`, which adds a `ForeignKey(Tenant)`.

Rules:
- `BaseV2ViewSet.get_queryset()` automatically filters by `request.tenant`. If you override `get_queryset()`, either call `super()` or explicitly filter by tenant (e.g., `RoleV2.objects.for_tenant(self.request.tenant)`).
- When querying models outside of a viewset (services, management commands), always filter by tenant explicitly. Never return cross-tenant data.
- Target workspace existence checks in permission classes must include `tenant=request.tenant` (see `WorkspaceAccessPermission._check_move_target_exists_v1`).

### Tenant resolution

- Tenant is resolved from `request.user.org_id` via cache (`TenantCache`) or DB lookup.
- System users without a tenant get 404 (no lazy bootstrap for system users).
- `request.tenant` is set by middleware and available to all views.

## Input Validation

### Query parameter sanitization

Use `clean_query_param()` (`rbac/management/utils.py`) for all user-supplied query parameters:
```python
value = clean_query_param(request.query_params.get("name"), "name")
```
This strips whitespace-only values and rejects NUL characters (`\x00`).

Rules:
- NUL bytes in query params can cause PostgreSQL errors or bypass string matching. Always sanitize.
- Role binding access checks strip `\x00` from `resource_id` and `resource_type` inline (see `_parse_query_resource`). Prefer using `clean_query_param` for consistency.

### UUID validation

- Use `validate_uuid()` or `is_valid_uuid()` from `rbac/management/utils.py` before passing user-supplied UUIDs to queries.
- Use `UUIDStringField` (in `rbac/management/utils.py`) in serializers for strict UUID format validation (hyphenated hex only).

### Serializer field constraints

- Always set `max_length` on `CharField` fields. Workspace name and description are capped at 255.
- Use `read_only=True` on fields that should never be set by the client (id, created, modified, type).

### Reserved names

`validate_group_name()` rejects "Custom Default Access" and "Default Access" as group names.

## Secrets and Configuration

Rules:
- `DJANGO_SECRET_KEY` has a hardcoded fallback for development. In production, it must be set via environment variable.
- `SERVICE_PSKS` and `SYSTEM_USERS` are JSON environment variables. Never log their contents.
- Never commit `.env`, `.envrc`, or credential files. The `.gitignore` should exclude them.
- gRPC channels use `grpc.insecure_channel()` in development/Clowder but `grpc.ssl_channel_credentials()` in production. The switch is in `create_client_channel()`, `create_client_channel_relation()`, and `create_client_channel_inventory()` functions.

## Development-Only Features

These features are guarded by environment variables and must never be enabled in production:

| Setting | Effect | Guard |
|---|---|---|
| `DEVELOPMENT=True` | Injects mock identity header via `DevelopmentIdentityHeaderMiddleware` | Only added to middleware stack when True |
| `IT_BYPASS_TOKEN_VALIDATION=True` | Skips JWT validation, returns mocked user | Returns hardcoded mock values |
| `ALLOW_ANY=True` | All permission classes return True | Checked in each permission class |
| `IT_BYPASS_IT_CALLS=True` | Skips calls to IT service for principal validation | Skips BOP verification |
| `DEBUG=True` | Django debug mode | Should always be False in production |

## Middleware Order

The middleware stack in `settings.py` includes several security-critical components. The key security middleware are (in their relative order):
1. `DisableCSRF` -- CSRF is disabled because the API gateway handles authentication via identity headers, not cookies.
2. `IdentityHeaderMiddleware` -- parses identity for public API paths.
3. `InternalIdentityHeaderMiddleware` -- parses identity for `/_private/` paths.
4. `ReadOnlyApiMiddleware` -- blocks writes when read-only mode is enabled (except internal endpoints).

`DevelopmentIdentityHeaderMiddleware` is inserted before `IdentityHeaderMiddleware` only when `DEVELOPMENT=True`.

## Existence Leakage Prevention

Rules:
- v2 detail endpoints must return 404 (not 403) for objects the user cannot access. This prevents attackers from discovering valid resource IDs.
- `WorkspaceObjectAccessMixin` ensures `get_object()` returns 404 when the object is filtered out by the access filter backend.
- The `WorkspaceAccessFilterBackend` filters the queryset before DRF's `get_object()` runs, so inaccessible objects appear as "not found."

## Cross-Account Requests

- Only internal Red Hat users (`is_internal=True`, `@redhat.com` email) can create cross-account requests.
- Cross-account principals are scoped to system roles only (`system=True`).
- The username for cross-account sessions is formatted as `{org_id}-{user_id}` to prevent collision with regular usernames.
