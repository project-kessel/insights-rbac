# V1-to-V2 rollout operator guide

This guide describes the RBAC-side sequence for a selected organization. Coordinate application
readiness, permissions, and the change window before opt-in; do not use a production write as a
probe. Paths below assume the default `/api/rbac/` prefix. Private endpoints require authorized
internal access; see [authentication](AUTHENTICATION.md). The [API guide](API-USAGE.md) describes
client authentication and pagination.

## Gates and one-way boundary

| Signal | Effect |
| --- | --- |
| `V2_APIS_ENABLED` | Deployment setting, default false; registers V2 routes. A 404 alone does not prove the setting is off. |
| `v2_opted_in_at` on `TenantMapping` | General per-tenant opt-in. `V2WriteRequiresWorkspacesEnabled` is a **permission class**, not a configurable flag: it permits safe methods but requires opt-in for writes on the role, group and role-binding views, in addition to their authorization checks. Some V1 writes or APIs are blocked at opt-in. Workspace operations have their own checks. |
| `v2_write_activated_at` | **Irreversible:** V2 write activation prevents subsequent opt-out and V1 writes. Atomic V2 write paths set it inside the transaction; a failed transaction does not commit it. Do not assume a nominally harmless write leaves the tenant reversible. |
| `hbi.rbac-v2` | Per-org strict access flag. If a request includes an application in `V2_STRICT_ACCESS_CHECK_FLAG_APPLICATION_NAMES`, the V1 `/access` restrictions apply when either this flag or general opt-in is true. Otherwise they depend on general opt-in only. The restriction applies to the whole request, not just the strict application. |

For restricted `/access` requests, specify `application`; all requested applications must be in
`V2_MIGRATION_APP_EXCLUDE_LIST` or the request returns 400. Strict-only enablement does not itself
grant V2 writes or block V1 writes. The workspace access-check toggle and its
`WORKSPACE_ACCESS_CHECK_V2_ENABLED` fallback select a separate workspace authorization path; they
are not the tenant opt-in state. See [org determination](v2-org-determination.md) and
[`v2_edit_api_access.py`](../rbac/management/permissions/v2_edit_api_access.py) for the actual checks.
In particular, use `v2_opted_in_at`, not `v2_write_activated_at` alone, for the general check.

## Prepare and opt in

1. Confirm V2 routes are enabled on the deployment, Kessel/Relations and replication are healthy,
   and the application owners have accounted for V1 access restrictions. Record the target org and
   baseline roles, groups, access and workspace results. See [scenario validation](mcp-scenario-validation.md)
   for discovery and representative readonly scenarios; use an approved test org and identities.
2. Check `GET /api/rbac/v1/tenant/opt-in/` and, as org admin,
   `GET /api/rbac/v1/tenant/opt-in/eligibility/`. Status responses carry a private 120-second cache
   directive. Eligibility reports `bootstrapped`, ineligible custom roles and groups with ineligible
   system roles when applicable. Resolve reported blockers with the relevant owners and recheck;
   the eligibility check uses the configured migration exclude list.
3. If the mapping is missing, use the authorized internal
   `POST /_private/api/utils/bootstrap_tenant/` with body `{"org_ids":["<org-id>"]}`. By default
   missing tenants return 404; do not use `create_missing` without explicit authorization. Bootstrap
   creates the tenant mapping, root and default workspaces and replication events. It does not opt
   the tenant in. Do not use `force=true` as a routine retry: it is rejected while relation
   replication is enabled due to custom-default-group race risk. The endpoint also supports
   `force_admin_only=true` for re-replicating admin defaults; see
   [`bootstrap_tenant`](../rbac/internal/views.py) before using it.
4. After approval and a successful eligibility check, an org admin may
   `PATCH /api/rbac/v1/tenant/opt-in/` with `{"v2_opted_in":true}`. The endpoint rechecks
   eligibility; 422 contains the ineligibility report. Public opt-in accepts true only. It creates
   default bindings in the same transaction on new opt-in; missing seeded platform roles can leave
   creation incomplete. Opt-in changes API behavior immediately even before write activation.
5. Confirm the state and default bindings, then run the checks below before allowing approved V2
   writes. **First committed V2 write activation is a one-way boundary.** Record its time and
   communicate that V1 write recovery is no longer available.

## Default-binding backfill and dual write

Existing opted-in tenants may need the default-binding backfill. Start with selected orgs using
`POST /_private/api/utils/backfill_default_role_bindings/?org_ids=<org-id>`; omit `org_ids` for all
opted-in tenants. `batch_size` is optional, defaults to 500 and must be positive. Alternatively,
the management command `backfill_default_role_bindings` requires `--org-ids` (comma-separated)
or `--all`; the deployed job is [here](../deploy/backfill_default_role_bindings.yml).

Review `tenants_processed`, `already_complete`, `tenants_backfilled`, `incomplete_orgs`,
`failed_orgs`, and `aborted`. The internal API returns 422 for failures/incomplete tenants and 500
if aborted; it aborts after 10 consecutive failures or incomplete tenants. Check missing workspaces,
platform roles and logs, fix the cause, then rerun: completed tenants are skipped. A complete
**unfiltered** run with zero errors and incomplete tenants is required before removing the lazy
creation path; a staged run cannot establish that. A custom default group intentionally replaces
the USER default bindings. A successful scoped run can process zero tenants for an unknown or
not-opted-in org; check `tenants_processed` against the intended scope. See
[default bindings](role_bindings.md#default-role-bindings).

Eligible V1 dual-write paths update `BindingMapping`, synchronize corresponding migrated V2
`RoleBinding` subjects, and enqueue relationship replication via the outbox. This is not a hook on
every mapping save, nor a promise of reverse V2-to-V1 synchronization. DB commit and downstream
delivery are distinct: wait for replication and verify access rather than assuming an enqueue proves
Kessel parity. Default-binding backfill is not a general migration of every V1 role or binding; see
[dual-write synchronization](role_bindings.md#dual-write-synchronization).

## Validate and monitor

- Recheck opt-in and eligibility, including strict flag targeting for the actual requested apps.
  Test expected `/access` allowlisted requests and expected rejection of non-allowlisted requests.
- Use authorized `POST /_private/api/inventory/bootstrap_tenants/<org_id>/`; inspect
  `bootstrapped_correct` and `relations_checked`, not just HTTP success. This checks bootstrap
  relations, not every role binding. Check root/default workspace visibility and representative
  V2 role-binding queries with the appropriate identity and resource scope; follow pagination.
- Compare representative group membership, role grants and effective access against the recorded
  baseline, including expected denials. The [MCP scenarios](mcp-scenario-validation.md) cover
  discovery, conflicting group access, role inspection, and 403 diagnosis; record tool calls,
  results and gaps. They are scenario tests, not an automated migration parity guarantee. Do not
  approve suggested writes as part of readonly validation.
- Watch `rbac_v2_optin_total` and `rbac_v2_optin_eligibility_requests_total` by `result`, and
  `rbac_v2_optin_status_requests_total` / `rbac_v2_optin_status_latency_seconds` for status health.
  Watch `rbac_v2_api_requests_total` by endpoint, method and status class and
  `rbac_v2_api_request_duration_seconds` by endpoint and method for errors and latency.
  `rbac_api_migration_requests_total` tracks V1/V2 traffic by version, client ID, user agent and
  method; it is not a count of migrated tenants. `rbac_v1_access_v2_org_total` counts rejected
  V1 `/access` requests from V2-restricted orgs. Correlate with authorization and replication logs;
  see [integration metrics](integration-guidelines.md#12-prometheus-metrics-conventions).

## Recovery

- Before write activation, authorized operators can use
  `PATCH /_private/api/utils/tenant_v2_opt_in/<org_id>/` with `{"v2_opted_in":false}` and verify
  the returned state. The public API cannot opt out. Opt-out does **not** delete default bindings.
  Strict-flag restrictions may still apply independently; coordinate flag changes with their owner.
- After write activation, internal opt-out returns 422. Disabling routes or a feature flag does not
  clear activation or restore V1 writes. Stop rollout, preserve evidence and escalate for an
  incident-specific recovery plan; this repository documents no supported general V2-to-V1 rollback.
- For incomplete backfill or bootstrap/replication errors, investigate the reported orgs and logs,
  restore prerequisites and repeat the scoped check/backfill. Avoid force-replication or destructive
  repair without a separately reviewed procedure. A successful DB operation alone does not establish
  downstream consistency.
