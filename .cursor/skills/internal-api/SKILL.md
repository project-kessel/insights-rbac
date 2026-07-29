---
name: internal-api
description: Call RBAC internal API endpoints in stage or prod via Turnpike (same routing as the relationship skill). Use when invoking /_private/api/ utils, tenant, relations, inventory, disaster recovery, or integration endpoints.
---

# RBAC Internal API Tool

Call RBAC internal endpoints in **stage** or **prod** through Turnpike. This uses the same auth and routing as the [relationship skill](../relationship/SKILL.md).

## URL mapping

Django registers internal routes under `/_private/` (see `rbac/internal/urls.py`). Turnpike exposes them at:

| Django path | Turnpike URL |
|-------------|--------------|
| `/_private/api/<rest>` | `${STAGE_DOMAIN}/api/rbac/<rest>` |
| `/_private/api/relations/read_tuples/` | `${STAGE_DOMAIN}/api/rbac/relations/read_tuples/` |
| `/_private/api/utils/bootstrap_users_from_user_ids/` | `${STAGE_DOMAIN}/api/rbac/utils/bootstrap_users_from_user_ids/` |

**Not covered by this skill:** `/_private/_s2s/` endpoints (PSK/JWT auth). Use oc exec or service credentials for those.

## Prerequisites

Before calling an endpoint:

0. **Check for config.env** — verify `.cursor/skills/config.env` exists with `STAGE_DOMAIN`, `PROD_DOMAIN`, and `PROXY`.
1. **Check SESSION** — must be set. If missing, ask the user to open the Turnpike session URL in a browser and copy the token:
   - Stage: `${STAGE_DOMAIN}/api/turnpike/session/`
   - Prod: `${PROD_DOMAIN}/api/turnpike/session/`
   - Then: `export SESSION=<token_value>`
2. **Confirm environment** — user must specify `stage` or `prod`.

## Usage

Run `.cursor/skills/internal-api/scripts/internal-api.sh`:

```bash
sh .cursor/skills/internal-api/scripts/internal-api.sh <stage|prod> <METHOD> <path> [query_string] [json_body]
```

- **path** — everything after `api/` in `rbac/internal/urls.py` (e.g. `utils/bootstrap_users_from_user_ids/`, `relations/read_tuples/`)
- **query_string** — optional, without `?` (e.g. `dry_run=true`)
- **json_body** — optional JSON string for POST/PUT/PATCH

If the fourth argument starts with `{` or `[`, it is treated as the JSON body (no query string).

## Workflow

When a user asks to call an internal API:

1. Verify `config.env` exists.
2. Verify `SESSION` is set; if not, give Turnpike session URL instructions and stop.
3. Confirm `stage` or `prod`.
4. Look up the endpoint in `rbac/internal/urls.py` and the view docstring in `rbac/internal/views.py` for method, query params, and body shape.
5. Prefer **dry_run** query params when the endpoint supports them.
6. Run the script and interpret the JSON response.

## Common examples

### Bootstrap users from BOP user IDs

Dry run first:

```bash
sh .cursor/skills/internal-api/scripts/internal-api.sh stage POST \
  utils/bootstrap_users_from_user_ids/ \
  dry_run=true \
  '{"user_ids":["12345678","87654321"]}'
```

Apply:

```bash
sh .cursor/skills/internal-api/scripts/internal-api.sh stage POST \
  utils/bootstrap_users_from_user_ids/ \
  '{"user_ids":["12345678","87654321"]}'
```

### Bootstrap tenant by org ID

By default returns 404 if the Tenant row is missing. Pass `create_missing=true` to create it. Optional `ungrouped_hosts_id` creates the ungrouped-hosts workspace with that UUID.

```bash
# Legacy body (tenant must already exist)
sh .cursor/skills/internal-api/scripts/internal-api.sh stage POST \
  utils/bootstrap_tenant/ \
  '{"org_ids":["1234567"]}'

# Create missing tenant + optional ungrouped-hosts workspace UUID
sh .cursor/skills/internal-api/scripts/internal-api.sh stage POST \
  utils/bootstrap_tenant/ \
  create_missing=true \
  '{"tenants":[{"org_id":"1234567","ungrouped_hosts_id":"0194a1b2-c3d4-7890-abcd-ef1234567890"}]}'
```

### Bootstrap pending tenants

```bash
sh .cursor/skills/internal-api/scripts/internal-api.sh stage GET \
  utils/bootstrap_pending_tenants/
```

### User lookup

```bash
sh .cursor/skills/internal-api/scripts/internal-api.sh stage GET \
  utils/user_lookup/ \
  username=jdoe
```

### Relations API

Same endpoints as the relationship skill; use either skill:

```bash
sh .cursor/skills/internal-api/scripts/internal-api.sh stage POST \
  relations/read_tuples/ \
  '{"filter": {"resource_namespace": "rbac", "resource_type": "group", "resource_id": "<group_uuid>", "relation": "member", "subject_filter": {"subject_namespace": "rbac", "subject_type": "principal", "subject_id": ""}}}'
```

### Disaster recovery

```bash
sh .cursor/skills/internal-api/scripts/internal-api.sh stage POST \
  disaster_recovery/reconcile/ \
  '{"org_ids":["1234567"], "dry_run": true}'
```

## Endpoint index

Paths below are the **script `path` argument** (after `/api/rbac/`). See `rbac/internal/views.py` for parameters and behavior.

### Tenant

| Path | Methods |
|------|---------|
| `tenant/unmodified/` | GET |
| `tenant/` | GET |
| `tenant/<org_id>/` | GET, DELETE |

### Utils

| Path | Notes |
|------|-------|
| `utils/sync_schemas/` | POST |
| `utils/set_tenant_ready/` | GET, POST |
| `utils/populate_tenant_account_id/` | POST |
| `utils/populate_tenant_org_id/` | POST |
| `utils/invalid_default_admin_groups/` | GET, DELETE |
| `utils/get_org_admin/<org_or_account>/` | GET |
| `utils/user_lookup/` | GET |
| `utils/bootstrap_tenant/` | POST |
| `utils/bootstrap_pending_tenants/` | GET |
| `utils/bootstrap_users_from_user_ids/` | POST (`?dry_run=true`) |
| `utils/fetch_replication_data/` | GET |
| `utils/kessel_parity_check/` | POST |
| `utils/replicate_default_workspaces/` | POST |
| `utils/replicate_updated_workspaces/` | POST |
| `utils/recompute_tenant_role_bindings/<org_id>/` | POST |
| `utils/migrate_role_scope_if_changed/<role_uuid>/` | POST |
| `utils/cleanup_tenant_orphan_bindings/<org_id>/` | POST |
| `utils/rebuild_tenant_workspace_relations/<org_id>/` | POST |
| `utils/kafka_test_message/` | GET |

Many other utils endpoints exist for migrations, cleanup, and destructive operations — check `rbac/internal/urls.py` before running them in prod.

### Relations

| Path |
|------|
| `relations/lookup_resource/` |
| `relations/lookup_subjects/` |
| `relations/check_relation/` |
| `relations/read_tuples/` |

### Inventory

| Path |
|------|
| `inventory/bootstrap_tenants/<org_id>/` |
| `inventory/group_assignments/<group_uuid>/` |
| `inventory/check_workspace/<workspace_uuid>/` |
| `inventory/check_role/<role_uuid>/` |
| `inventory/check_cross_account_request/<request_id>/` |
| `inventory/check/` |

### Disaster recovery

| Path |
|------|
| `disaster_recovery/workspaces/` |
| `disaster_recovery/reconcile/` |

### Integrations (v1)

| Path |
|------|
| `v1/integrations/tenant/` |
| `v1/integrations/tenant/<org_id>/roles/` |
| `v1/integrations/tenant/<org_id>/groups/` |
| `v1/integrations/tenant/<org_id>/groups/<uuid>/roles/` |
| `v1/integrations/tenant/<org_id>/groups/<uuid>/principals/` |
| `v1/integrations/tenant/<org_id>/principal/<principals>/groups/` |
| `v1/integrations/tenant/<org_id>/principal/<principals>/groups/<uuid>/roles/` |

### Other

| Path | Notes |
|------|-------|
| `migrations/run/` | POST |
| `migrations/progress/` | GET |
| `seeds/run/` | POST |
| `cars/expire/` | POST |
| `cars/clean/` | GET, POST |

## Related skills

- [relationship](../relationship/SKILL.md) — relations API with pre-built query examples
- [gabi](../gabi/SKILL.md) — SQL against RBAC Postgres
- [ephemeral-rbac](../ephemeral-rbac/SKILL.md) — internal API from inside ephemeral pods (no Turnpike)
