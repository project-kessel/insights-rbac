---
name: ephemeral-rbac-diagnose
description: Diagnose RBAC data and Kessel/SpiceDB replication issues in OpenShift ephemeral namespaces (platform role child relationships, principals, permissions). Use when debugging ephemeral envs, crc-eph, missing SpiceDB tuples, empty principals, or Postgres vs Kessel mismatches.
---

# Ephemeral RBAC Diagnosis

Use this skill to investigate RBAC state in an **OpenShift ephemeral namespace** (`ephemeral-*` on `api.crc-eph.r9lp.p1.openshiftapps.com`).

Related skills:
- [zed](../zed/SKILL.md) — direct SpiceDB checks (stage; ephemeral has in-namespace SpiceDB)
- [relationship](../relationship/SKILL.md) — Kessel read_tuples via Turnpike (stage/prod)
- [gabi](../gabi/SKILL.md) — SQL against stage/prod RBAC Postgres (not ephemeral)

## Prerequisites

1. **`oc` logged in** to the ephemeral cluster:
   ```bash
   oc whoami
   ```
   If unauthorized, log in with the user's ephemeral token or OpenShift "Copy login command".

2. **Target namespace** — user must provide `ephemeral-<id>` (e.g. `ephemeral-mvogw1`).

3. **Switch namespace and find the RBAC pod**:
   ```bash
   oc project ephemeral-<id>
   oc get pods | grep rbac-service
   ```
   Use the `rbac-service-*` pod and the `rbac-service` container (not `crcauth`).

## Quick diagnosis script

Run the bundled checker (Postgres vs SpiceDB for platform role children + seed log summary):

```bash
sh .cursor/skills/ephemeral-rbac/scripts/diagnose-platform-role-children.sh ephemeral-<id>
```

Interpret the script output using the sections below.

## Symptom: Platform role → child relationships missing in SpiceDB

### Expected architecture

RBAC maintains role hierarchy in **two places**:

| Layer | Mechanism | Tuple shape |
|-------|-----------|-------------|
| **Postgres** | `RoleV2.children` M2M (`management/role/definer.py` → `_seed_v2_role_from_v1`) | Django only |
| **SpiceDB/Kessel** | `SeedingRelationApiDualWriteHandler.replicate_new_system_role()` via outbox | `rbac/role:<platform_uuid>#child@rbac/role:<seeded_uuid>` |

**Critical:** `platform_role.children.add(v2_role)` updates Postgres only. It does **not** write to the outbox or SpiceDB.

SpiceDB child tuples are emitted only when v1 system role seeding calls `replicate_new_system_role()`, which happens when:
- the v1 role is **newly created**, or
- the v1 role **version changed** (`force_update_relationships`), or
- **`force_create_relationships=True`** is passed to seeds

If seed logs show `No change in system role` for every role, SpiceDB replication is **skipped** even though `_seed_v2_role_from_v1` still updates Django children.

### Manual verification in ephemeral

**1. Django — platform roles and children**

```bash
POD=$(oc get pods -n ephemeral-<id> -o name | grep rbac-service | head -1 | cut -d/ -f2)
oc exec -n ephemeral-<id> "$POD" -c rbac-service -- python rbac/manage.py shell -c "
from management.role.v2_model import RoleV2
for p in RoleV2.objects.filter(type='platform').prefetch_related('children'):
    print(f'{p.name}: django_children={p.children.count()} uuid={p.uuid}')
"
```

**2. SpiceDB — child tuples via in-cluster Kessel**

```bash
oc exec -n ephemeral-<id> "$POD" -c rbac-service -- python rbac/manage.py shell -c "
from management.relation_replicator.relations_api_replicator import RelationsApiReplicator
from management.role.v2_model import RoleV2
replicator = RelationsApiReplicator()
parent = RoleV2.objects.get(name='User default Platform Role')
child = RoleV2.objects.filter(type='seeded').first()
resp = replicator.read_tuples(
    resource_type='role', resource_id=str(parent.uuid), relation='child',
    subject_type='role', subject_id=str(child.uuid),
    resource_namespace='rbac', subject_namespace='rbac')
print(f'SpiceDB child tuples (sample): {len(resp)}')
for p in RoleV2.objects.filter(type='platform'):
    n = len(replicator.read_tuples(
        resource_type='role', resource_id=str(p.uuid), relation='child',
        subject_type='role', subject_id=str(child.uuid),
        resource_namespace='rbac', subject_namespace='rbac'))
    print(f'  {p.name} -> {child.name}: {n}')
"
```

**3. Init seed logs — root cause signal**

```bash
oc logs -n ephemeral-<id> deploy/rbac-service -c rbac-service-init 2>&1 \
  | grep -c 'No change in system role'
oc logs -n ephemeral-<id> deploy/rbac-service -c rbac-service-init 2>&1 \
  | grep -c 'Replicated system role'
```

Typical mismatch pattern:
- `No change in system role`: **~62** (all roles already in Postgres)
- `Replicated system role`: **0**
- Django children: **>0**
- SpiceDB child tuples: **0**

**Why ephemeral hits this:** Postgres often persists across redeploys (existing role rows), while SpiceDB is **fresh per env**. Normal `seeds` is idempotent for Postgres but does not backfill Kessel.

### Fix

Replicate all system-role relationships (including `child` tuples) to the outbox:

```bash
oc exec -n ephemeral-<id> deploy/rbac-service -c rbac-service -- \
  python rbac/manage.py seeds --roles --force-create-relationships
```

Wait for Kafka/Debezium to process outbox events, then re-run the SpiceDB check above.

Optional env checks on the pod:
```bash
oc exec -n ephemeral-<id> deploy/rbac-service -c rbac-service -- python -c "
import os
print('REPLICATION_TO_RELATION_ENABLED=', os.environ.get('REPLICATION_TO_RELATION_ENABLED'))
print('RELATION_API_SERVER=', os.environ.get('RELATION_API_SERVER'))
"
```

## Symptom: Principal / user_id / permissions in ephemeral

### No UMB in ephemeral

UMB (`principal_cleanup_via_umb`) does not run. `user_id` on `Principal` is **not** set by normal API calls when the tenant already exists.

Paths that **persist** `user_id`:
- First request that **bootstraps a new tenant** (`IdentityHeaderMiddleware` → `update_user(upsert=True)`) with `user_id` in `x-rh-identity`
- `POST /v1/groups/{uuid}/principals/` if BOP returns `user_id`
- Manual: `V2TenantBootstrapService.update_user()` in Django shell

`get_principal()` creates principals **without** `user_id`.

### List principals in ephemeral

```bash
POD=$(oc get pods -n ephemeral-<id> -o name | grep rbac-service | head -1 | cut -d/ -f2)
oc exec -n ephemeral-<id> "$POD" -c rbac-service -- python rbac/manage.py shell -c "
from management.principal.model import Principal
print(f'Total principals: {Principal.objects.count()}')
for p in Principal.objects.select_related('tenant').order_by('username'):
    print(f'{p.username} | user_id={p.user_id} | org={p.tenant.org_id}')
"
```

Empty principal count usually means **no user has hit RBAC yet** in that env (only public tenant seeded).

### Check effective permissions for a user

```bash
oc exec -n ephemeral-<id> "$POD" -c rbac-service -- python rbac/manage.py shell -c "
from management.principal.model import Principal
from management.utils import groups_for_principal, access_for_principal
p = Principal.objects.get(username__iexact='<username>')
t = p.tenant
print('groups:', [g.name for g in groups_for_principal(p, t, is_org_admin=False)])
access = access_for_principal(p, t, application='subscriptions', is_org_admin=False)
print('subscriptions:', sorted({a.permission.permission for a in access}))
"
```

### Custom default group (empty default permissions)

Audit logs on the tenant show org admin removing all roles from `Custom default access`:

```bash
oc exec -n ephemeral-<id> "$POD" -c rbac-service -- python rbac/manage.py shell -c "
from management.audit_log.model import AuditLog
from api.models import Tenant
t = Tenant.objects.get(org_id='<org_id>')
for l in AuditLog.objects.filter(tenant=t).order_by('created')[:25]:
    print(l.created, l.principal_username, l.action, l.description[:120])
"
```

## Diagnosis report template

Use this when reporting findings to the user:

```markdown
## Ephemeral RBAC diagnosis: `ephemeral-<id>`

### Platform role hierarchy
- Django platform roles: N (all should have children)
- Django total child links: N
- SpiceDB child tuples (sample): N — **expected 1+ if replicated**
- Init seed: `No change in system role` = N, `Replicated system role` = N

**Verdict:** [Postgres-only / replicated OK / SpiceDB empty]

### Principals (if requested)
- Total principals: N
- [username]: user_id=..., org=..., subscriptions perms=...

### Recommended fix
- [ ] `seeds --roles --force-create-relationships` (if SpiceDB missing)
- [ ] User login / `update_user()` (if user_id missing)
```

## Code references

- Django children M2M: `rbac/management/role/definer.py` (`_seed_v2_role_from_v1`)
- SpiceDB child tuples: `rbac/management/role/relation_api_dual_write_handler.py` (`_check_create_admin_platform_relation`)
- Tuple shape: `rbac/management/role/relations.py` (`role_child_relationship`)
- Seeds force flag: `rbac/management/management/commands/seeds.py` (`--force-create-relationships`)
- Skip replication on unchanged roles: `rbac/management/role/definer.py` (`_make_role`, `No change in system role` branch)
