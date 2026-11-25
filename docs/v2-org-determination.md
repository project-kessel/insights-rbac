# V2 Org Determination at the `/access` Endpoint

## Overview

The determination of whether an organization has been migrated to V2 (and thus whether it is subject to V2 access control restrictions) uses a **two-layer check** that examines both the application being queried and two independent signals from the database and feature flags.

This document explains how the `/api/rbac/v1/access/` endpoint identifies v2 orgs and enforces access restrictions accordingly. The API prefix is configurable via `API_PATH_PREFIX`.

## The Two Independent Signals

There are two separate signals that can mark an org as "v2-enabled":

### 1. Database Flag: `TenantMapping.v2_write_activated_at`

**Source:** PostgreSQL `management_tenantmapping` table

**What it means:** The org has **actually performed a write via V2 APIs** (roles, role bindings, etc.). Once set, this is **irreversible**.

**Code reference:** `rbac/management/tenant_mapping/v2_activation.py:89`
```python
def is_v2_write_activated(tenant: Tenant):
    """Check if the tenant has been activated for V2 writes (without locking)."""
    mapping = TenantMapping.objects.get(tenant=tenant)
    return mapping.v2_write_activated_at is not None
```

### 2. Unleash Feature Flags

**Source:** Unleash feature flag service, evaluated per `orgId` context

The flag:
- **`hbi.rbac-v2`** — Strict V2 access checks for HBI application specifically

## The Two-Layer Check: `is_v2_access_check_required_for_request`

At the `/access` endpoint, the determination happens in `rbac/management/permissions/v2_edit_api_access.py:40`:

```python
def is_v2_access_check_required_for_request(request, requested_apps: Iterable[str]) -> bool:
    """Check if V2 access check is required for the provided request."""
    # If any requested application has opted in to strict V2 access checks, then we need to check whether the org has
    # strict V2 access checks enabled. Otherwise, we only need to check whether the org has opted in to the V2 write
    # APIs (in which case V2 access checks are *always* required).

    requested_apps = set(requested_apps)

    if not requested_apps.isdisjoint(settings.V2_STRICT_ACCESS_CHECK_FLAG_APPLICATION_NAMES):
        return is_v2_edit_enabled_for_request(request) or FEATURE_FLAGS.is_v2_strict_access_check_enabled(
            request.user.org_id
        )

    return is_v2_edit_enabled_for_request(request)
```

### Layer 1: Strict-Access-Check Applications (e.g., HBI)

If **any** of the requested applications are in `V2_STRICT_ACCESS_CHECK_FLAG_APPLICATION_NAMES`:

**Org is v2-enabled if:**
```
TenantMapping.v2_write_activated_at IS NOT NULL
OR
Unleash flag "hbi.rbac-v2" is enabled for orgId
```

Applications in this list have stricter requirements and opt into V2 access checks independently of general V2 write enablement.

### Layer 2: All Other Applications

For applications not in the strict list:

**Org is v2-enabled if:**
```
TenantMapping.v2_opted_in_at IS NOT NULL
```

This covers the standard V2 write API enablement path.

## Effect on the `/access` Endpoint

When `is_v2_access_check_required_for_request` returns true, the `/access` endpoint calls `validate_v2_application_param` to enforce restrictions.

**Reference:** `rbac/management/access/view.py:201`

### Validation Rules

1. **Required:** The `application=` query parameter must be specified (no empty application list)
   - Returns: HTTP 400 `"V2 orgs must specify an application from the allowed list."`

2. **Allowlist:** All requested applications must be in the V2 migration exclude list
   - The exclude list is retrieved via `v2_role_excluded_applications()`
   - Returns: HTTP 400 with disallowed applications listed if any fail

### Metrics and Logging

When a V2-enabled org's `/access` call is rejected:

```python
v1_access_by_v2_org_total.labels(
    org_id=request.user.org_id,
    application=app_param,
    caller_type="service_account" | "user",
).inc()
```

And a structured log is written with full context: `org_id`, `application`, `caller_type`, `user_id`, `client_id`, `is_org_admin`, `request_id`, `user_agent`.

## Transition States

### V1 Org → V2 Org (One-way door)

1. **General opt-in:** `v2_opted_in_at` non-null in tenant
   - `/access` endpoint enforces V2 restrictions for all non-excluded apps
   - V2 write APIs are available (gated by `V2WriteRequiresWorkspacesEnabled`)
   - V1 write APIs are blocked (via `V1WriteBlockedWhenWorkspacesEnabled`)

2. **Strict-only opt-in:** Only `hbi.rbac-v2` flag enabled (without tenant being opted-in)
   - `/access` endpoint enforces V2 restrictions **only for strict-access apps** (e.g., HBI)
   - V1 write APIs remain **allowed** — `V1WriteBlockedWhenWorkspacesEnabled` uses `is_v2_edit_enabled_for_request` (general flag), not the strict check
   - V2 write APIs remain **blocked** — `V2WriteRequiresWorkspacesEnabled` also uses the general flag
   - Non-strict apps are unaffected (see `test_not_required` in `tests/management/permissions/test_v2_edit_api_access.py`)

3. **Activation phase:** Org performs first V2 write
   - `TenantMapping.v2_write_activated_at` is set (irreversible)
   - Tenant cannot opt out.

## Key Invariants

1. **Strict access checks are per-app:** The `hbi.rbac-v2` flag only affects HBI access checks; other apps follow the general `platform.rbac.workspaces` flag.
2. **Migration exclude list is gating:** V2-enabled orgs can only query `/access` for applications in the exclude list (apps not yet migrated to V2).

## Configuration References

### Settings

- `V2_STRICT_ACCESS_CHECK_FLAG_APPLICATION_NAMES` — List of apps requiring strict V2 access checks
- `V2_MIGRATION_APP_EXCLUDE_LIST` — Applications allowed for V2-enabled orgs to query via `/access`

### Unleash Flags

| Flag Name | Context | Default | Purpose |
|-----------|---------|---------|---------|
| `hbi.rbac-v2` | `orgId` | False | Enforce strict V2 access checks for HBI application in this org |

## Implementation Files

- **Permission/feature check:** `rbac/management/permissions/v2_edit_api_access.py`
- **V2 write activation/locking:** `rbac/management/tenant_mapping/v2_activation.py`
- **Feature flags:** `rbac/feature_flags.py`
- **/access endpoint:** `rbac/management/access/view.py`
- **Permission classes (write/API blocking):** Various `view.py` files use `V1WriteBlockedWhenWorkspacesEnabled`, `V1ApiBlockedWhenWorkspacesEnabled`, `V2WriteRequiresWorkspacesEnabled` — these all use `is_v2_edit_enabled_for_request` (general flag check), **not** the app-aware `is_v2_access_check_required_for_request` used by the `/access` endpoint

## Debugging

To check if an org is v2-enabled, use the same helper the `/access` endpoint uses.
This accounts for **both** layers — the general `platform.rbac.workspaces` flag (Layer 2)
**and** the strict `hbi.rbac-v2` flag (Layer 1) for applications like HBI:

```python
from management.tenant_mapping.v2_activation import is_v2_opted_in
from management.permissions.v2_edit_api_access import is_v2_access_check_required_for_request
from feature_flags import FEATURE_FLAGS

# Layer 2 (general V2 write flag — all non-strict apps)
is_v2_general = is_v2_opted_in(tenant)

# Layer 1 (strict access check — e.g. HBI)
is_strict_flag = FEATURE_FLAGS.is_v2_strict_access_check_enabled(org_id)
is_v2_strict = is_v2_general or is_strict_flag

# Full check matching the /access endpoint (pass the requested apps):
# is_v2_for_request = is_v2_access_check_required_for_request(request, requested_apps)

print(f"Strict flag  (hbi.rbac-v2): {is_strict_flag}")
print(f"V2 for general apps: {is_v2_general}")
print(f"V2 for strict apps (e.g. HBI): {is_v2_strict}")
```

> **Note:** Checking only the database flag will give a wrong answer for strict-access
> apps like HBI. If `hbi.rbac-v2` is enabled but `platform.rbac.workspaces` is not, the
> `/access` endpoint still applies V2 access restrictions for HBI — but the general check alone would
> report `False`. Always use `is_v2_access_check_required_for_request` or check both flags.

To check metrics:

```bash
# V1 /access calls from v2-enabled orgs (rejected)
curl http://localhost:8000/metrics | grep rbac_v1_access_v2_org_total
```
