# Role Bindings in RBAC: Comprehensive Documentation

## Table of Contents

1. [Overview](#overview)
2. [Core Concepts](#core-concepts)
3. [V1 Perspective: BindingMapping](#v1-perspective-bindingmapping)
4. [V2 Perspective: RoleBinding](#v2-perspective-rolebinding)
5. [V1 vs V2 Comparison](#v1-vs-v2-comparison)
6. [Scopes and Resource Hierarchy](#scopes-and-resource-hierarchy)
7. [SpiceDB Relationship Tuples](#spicedb-relationship-tuples)
8. [Data-Level Permission Examples](#data-level-permission-examples)
9. [Org-Level Permission Examples](#org-level-permission-examples)
10. [Dual-Write Synchronization](#dual-write-synchronization)
11. [Default Role Bindings](#default-role-bindings)
12. [API and Querying](#api-and-querying)

---

## Overview

A **role binding** is the central authorization primitive that answers the question: _"Who has what permissions on which resource?"_ It connects three things together:

```
Subject (who)  +  Role (what permissions)  +  Resource (where)
```

The RBAC system is transitioning from V1 to V2. During this transition, both models coexist and are kept in sync via a dual-write mechanism.

---

## Core Concepts

### Entities Involved

| Entity | Description |
|--------|-------------|
| **Role** | A set of permissions (e.g., `inventory:hosts:read`, `advisor:*:*`) |
| **Subject** | A group or principal (user/service account) that receives permissions |
| **Resource** | The target object — a tenant, workspace, or platform resource |
| **Role Binding** | The link that grants a role's permissions to a subject on a specific resource |

### Permission Format

Permissions follow the pattern `application:resource_type:operation`:

```
inventory:hosts:read
advisor:recommendation_results:write
rbac:workspaces:role_binding_grant
```

Wildcards are supported: `inventory:*:*` grants all inventory permissions.

### Subject Types

- **Group** — a collection of principals; role bindings typically target groups
- **Principal** — an individual user (`type=user`) or service account (`type=service-account`)

---

## V1 Perspective: BindingMapping

The V1 model `BindingMapping` stores V2 role binding data within the V1 schema. It serves as a bridge during the migration period.

**Source:** `rbac/management/role/model.py:150-303`

### Schema

```python
class BindingMapping(models.Model):
    mappings = models.JSONField(default=dict)           # V2 binding data as JSON
    role = models.ForeignKey(Role, ...)                  # FK to V1 Role
    resource_type_namespace = models.CharField(...)      # e.g., "rbac"
    resource_type_name = models.CharField(...)           # e.g., "workspace"
    resource_id = models.CharField(...)                  # e.g., workspace UUID
```

### JSON Structure of `mappings` Field

The `mappings` JSONField contains a serialized `V2rolebinding`:

```json
{
  "id": "019437a2-b8c0-7000-8000-000000000001",
  "role": {
    "id": "canned-rbac-admin-role-uuid",
    "is_system": true,
    "permissions": []
  },
  "groups": [
    "550e8400-e29b-41d4-a716-446655440000"
  ],
  "users": {
    "group:Default access/policy:System Policy, admin access": "user123"
  }
}
```

Key points about the `mappings` JSON:
- `id` — the UUID of the V2 role binding
- `role.id` — the V2 role UUID
- `role.is_system` — system roles store no permissions here (resolved by SpiceDB)
- `role.permissions` — only populated for custom (non-system) roles
- `groups` — list of group UUIDs (can have duplicates from multiple sources)
- `users` — dict of `{source_key: user_id}` pairs, allowing the same user from different sources

### Example: V1 BindingMapping for Admin Access

```
BindingMapping:
  role_id:                42  (V1 Role: "RBAC Administrator")
  resource_type_namespace: "rbac"
  resource_type_name:      "tenant"
  resource_id:             "o_12345"
  mappings: {
    "id": "019437a2-aaaa-7000-8000-000000000001",
    "role": {
      "id": "019437a2-bbbb-7000-8000-admin-role-1",
      "is_system": true,
      "permissions": []
    },
    "groups": ["aaaaaaaa-admin-default-group-uuid"],
    "users": {}
  }
```

### Example: V1 BindingMapping for User with Source Tracking

```
BindingMapping:
  role_id:                 99  (V1 Role: "Inventory Viewer")
  resource_type_namespace: "rbac"
  resource_type_name:      "workspace"
  resource_id:             "550e8400-default-workspace-uuid"
  mappings: {
    "id": "019437a2-cccc-7000-8000-000000000002",
    "role": {
      "id": "019437a2-dddd-7000-8000-viewer-role",
      "is_system": true,
      "permissions": []
    },
    "groups": [
      "550e8400-platform-default-group-uuid"
    ],
    "users": {
      "group:Default access/policy:System Policy, access": "user456",
      "group:Custom Group A/policy:Custom Policy": "user456"
    }
  }
```

In this example, `user456` is bound via two different sources. If one source is removed, the user remains bound because the other source still references them.

### Key V1 Operations

| Method | Description |
|--------|-------------|
| `assign_group_to_bindings(group_uuid)` | Adds a group to the binding (idempotent) |
| `unassign_group(group_uuid)` | Completely removes a group from the binding |
| `assign_user_to_bindings(user_id, source)` | Adds a user under a specific source key |
| `unassign_user_from_bindings(user_id, source)` | Removes a user by source; only removes the SpiceDB relation if no other sources remain |
| `as_tuples()` | Generates SpiceDB relationship tuples |

---

## V2 Perspective: RoleBinding

The V2 model uses proper relational database tables instead of JSON blobs.

**Source:** `rbac/management/role/v2_model.py:175-323`

### Schema

```python
class RoleBinding(TenantAwareModel):
    uuid = models.UUIDField(default=uuid7, unique=True)
    role = models.ForeignKey(RoleV2, ...)           # FK to V2 Role
    resource_type = models.CharField(...)            # e.g., "workspace"
    resource_id = models.CharField(...)              # e.g., workspace UUID
    # tenant inherited from TenantAwareModel

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["role", "resource_type", "resource_id", "tenant"],
                name="unique role binding per role resource pair per tenant",
            )
        ]
```

### Subject Join Tables

**RoleBindingGroup** — connects groups to role bindings:

```python
class RoleBindingGroup(models.Model):
    group = models.ForeignKey(Group, related_name="role_binding_entries")
    binding = models.ForeignKey(RoleBinding, related_name="group_entries")
    # Unique constraint: (group, binding)
```

**RoleBindingPrincipal** — connects principals (users) to role bindings with source tracking:

```python
class RoleBindingPrincipal(models.Model):
    principal = models.ForeignKey(Principal, related_name="role_binding_entries")
    binding = models.ForeignKey(RoleBinding, related_name="principal_entries")
    source = models.CharField(max_length=128, null=False)
    # Unique constraint: (principal, binding, source)
    # Check constraint: source cannot be empty
```

### V2 Role Types

```python
class RoleV2.Types:
    CUSTOM = "custom"      # Created by org admin; can only bind groups, not principals
    SEEDED = "seeded"      # System-provided; has v1_source FK back to V1 Role
    PLATFORM = "platform"  # Aggregate role containing seeded children
```

### Example: V2 RoleBinding for Workspace Access

```
RoleBinding:
  uuid:          "019437a2-cccc-7000-8000-000000000002"
  role_id:       (FK to RoleV2 "Inventory Host Viewer")
  resource_type: "workspace"
  resource_id:   "550e8400-default-workspace-uuid"
  tenant_id:     (FK to Tenant)

  RoleBindingGroup entries:
    - group: "Platform Default Group" (uuid: 550e8400-platform-default-group-uuid)

  RoleBindingPrincipal entries:
    - principal: user456, source: "group:Default access/policy:System Policy, access"
    - principal: user456, source: "group:Custom Group A/policy:Custom Policy"
```

### Key V2 Operations

| Method | Description |
|--------|-------------|
| `update_groups(groups)` | Replaces all group bindings atomically |
| `update_groups_by_uuid(uuids)` | Same, but accepts group UUIDs |
| `update_principals(principals_by_source)` | Replaces all principal bindings atomically |
| `update_principals_by_user_id(user_ids_by_source)` | Same, but accepts user IDs |
| `bound_groups()` | Returns QuerySet of bound groups |
| `bound_principals()` | Returns QuerySet of bound principals |

---

## V1 vs V2 Comparison

| Aspect | V1 (BindingMapping) | V2 (RoleBinding) |
|--------|---------------------|-------------------|
| **Storage** | JSON blob in `mappings` field | Relational tables (RoleBinding, RoleBindingGroup, RoleBindingPrincipal) |
| **Role reference** | FK to V1 `Role` | FK to V2 `RoleV2` |
| **Uniqueness** | Not enforced at DB level | DB constraint: `(role, resource_type, resource_id, tenant)` |
| **Group binding** | List of UUIDs in JSON array | `RoleBindingGroup` join table with unique constraint |
| **User binding** | Dict `{source_key: user_id}` in JSON | `RoleBindingPrincipal` table with `(principal, binding, source)` unique constraint |
| **Source tracking** | Dict key is the source string | Dedicated `source` column on `RoleBindingPrincipal` |
| **Resource namespace** | Stored in `resource_type_namespace` field | Not stored (assumed `"rbac"`) |
| **Custom role principals** | Possible but not validated | Explicitly prevented: "Principal bindings are not supported for custom roles" |
| **Atomicity** | Manual list manipulation + save | `transaction.atomic()` with bulk operations |

---

## Scopes and Resource Hierarchy

Role bindings are attached to resources at different scope levels. Permissions **inherit downward** through the hierarchy.

### Scope Levels

```
Scope.TENANT    → resource_type: "tenant",    resource_id: org_id (e.g., "o_12345")
Scope.ROOT      → resource_type: "workspace", resource_id: root workspace UUID
Scope.DEFAULT   → resource_type: "workspace", resource_id: default workspace UUID
```

### Hierarchy Diagram

```
┌─────────────────────────────────────────────────────┐
│                    TENANT (org)                      │
│           resource: rbac/tenant:o_12345              │
│                                                      │
│  Role bindings here grant permissions across         │
│  the ENTIRE organization.                            │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │            ROOT WORKSPACE                     │   │
│  │    resource: rbac/workspace:<root-ws-uuid>    │   │
│  │                                               │   │
│  │  Role bindings here grant permissions         │   │
│  │  across all workspaces.                       │   │
│  │                                               │   │
│  │  ┌───────────────────────────────────────┐   │   │
│  │  │        DEFAULT WORKSPACE               │   │   │
│  │  │  resource: rbac/workspace:<def-ws-uuid>│   │   │
│  │  │                                        │   │   │
│  │  │  Role bindings here grant permissions  │   │   │
│  │  │  on the default workspace and its      │   │   │
│  │  │  children.                             │   │   │
│  │  │                                        │   │   │
│  │  │  ┌────────────────────────────────┐   │   │   │
│  │  │  │    STANDARD WORKSPACES         │   │   │   │
│  │  │  │    (custom sub-workspaces)     │   │   │   │
│  │  │  └────────────────────────────────┘   │   │   │
│  │  └───────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │            PLATFORM                           │   │
│  │    resource: rbac/platform:<org_id>           │   │
│  │                                               │   │
│  │  Org-level permissions that don't require     │   │
│  │  workspace inheritance (e.g., notifications). │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

### Inheritance in SpiceDB

The SpiceDB schema implements inheritance through `t_parent` relations:

```
// Workspace inherits permissions from parent workspace or tenant
definition rbac/workspace {
    relation t_parent: rbac/workspace | rbac/tenant
    relation t_binding: rbac/role_binding

    permission inventory_host_view =
        t_binding->inventory_host_view    // direct bindings on this workspace
        + t_parent->inventory_host_view   // inherited from parent
}
```

A role binding on the root workspace automatically grants permissions on all child workspaces.

---

## SpiceDB Relationship Tuples

Each role binding generates multiple relationship tuples in SpiceDB.

### Tuple Structure

```
resource_type:resource_id#relation@subject_type:subject_id[#subject_relation]
```

### Tuples Generated by a Single Role Binding

For a role binding that grants the "Inventory Viewer" role to a group on the default workspace:

```
# 1. Bind role to role_binding
rbac/role_binding:binding-uuid-1#role@rbac/role:viewer-role-uuid

# 2. Role has permissions (one tuple per permission, wildcard principal)
rbac/role:viewer-role-uuid#inventory_hosts_read@rbac/principal:*
rbac/role:viewer-role-uuid#inventory_all_read@rbac/principal:*

# 3. Group is a subject of the role binding
rbac/role_binding:binding-uuid-1#subject@rbac/group:group-uuid-1#member

# 4. Direct principal as subject (if any)
rbac/role_binding:binding-uuid-1#subject@rbac/principal:localhost/user123

# 5. Resource has this binding
rbac/workspace:default-ws-uuid#binding@rbac/role_binding:binding-uuid-1
```

### How Permission Checks Work

To check: _"Can user123 view inventory hosts on workspace X?"_

SpiceDB traverses:

```
rbac/workspace:X
  ├─ #binding → rbac/role_binding:binding-uuid-1
  │    ├─ #subject → rbac/group:group-uuid-1#member
  │    │    └─ #member → rbac/principal:localhost/user123  ✓ (user is in group)
  │    └─ #role → rbac/role:viewer-role-uuid
  │         └─ #inventory_host_view → rbac/principal:*    ✓ (role has permission)
  │
  │    Permission = subject & role->inventory_host_view    ✓ ALLOWED
  │
  └─ #parent → rbac/tenant:o_12345
       └─ (also checks bindings at tenant level)
```

---

## Data-Level Permission Examples

Data-level permissions control access to specific data resources (hosts, workspaces) and are **scoped to workspaces**. They require both the permission assignment AND the host being in the workspace.

### Example 1: Inventory Host View on Default Workspace

**Scenario:** Grant the "Engineering" group permission to view hosts in the default workspace.

**V1 (BindingMapping):**

```
BindingMapping:
  role:                    Role(name="Inventory Host Viewer", system=True)
  resource_type_namespace: "rbac"
  resource_type_name:      "workspace"
  resource_id:             "aaaaaaaa-default-ws-uuid"
  mappings: {
    "id": "11111111-binding-uuid",
    "role": {"id": "22222222-viewer-role-uuid", "is_system": true, "permissions": []},
    "groups": ["33333333-engineering-group-uuid"],
    "users": {}
  }
```

**V2 (RoleBinding):**

```
RoleBinding:
  uuid:          "11111111-binding-uuid"
  role:          RoleV2(name="Inventory Host Viewer", type="seeded")
  resource_type: "workspace"
  resource_id:   "aaaaaaaa-default-ws-uuid"
  tenant:        Tenant(org_id="12345")

RoleBindingGroup:
  binding: (above)
  group:   Group(name="Engineering", uuid="33333333-engineering-group-uuid")
```

**SpiceDB Tuples:**

```
rbac/role_binding:11111111-binding-uuid#role@rbac/role:22222222-viewer-role-uuid
rbac/role:22222222-viewer-role-uuid#inventory_hosts_read@rbac/principal:*
rbac/role_binding:11111111-binding-uuid#subject@rbac/group:33333333-engineering-group-uuid#member
rbac/workspace:aaaaaaaa-default-ws-uuid#binding@rbac/role_binding:11111111-binding-uuid
```

**SpiceDB Permission Check:**

```
Can principal "localhost/jsmith" view hosts on workspace "aaaaaaaa-default-ws-uuid"?

1. workspace:aaaaaaaa has binding:11111111 ✓
2. binding:11111111 has role:22222222 with inventory_host_view ✓
3. binding:11111111 has subject group:33333333#member
4. group:33333333 has member principal:localhost/jsmith ✓
5. Result: ALLOWED
```

### Example 2: Patch System Edit Requires Host View (Intersection)

Some data-level permissions require **both** the specific permission AND host view access. This is modeled as an intersection (`&`) in SpiceDB:

```
definition rbac/workspace {
    permission patch_system_edit = inventory_host_view & patch_system_edit_assigned
}
```

**Scenario:** User needs to edit patch systems on hosts. They need:
1. A role binding with `inventory:hosts:read` (for `inventory_host_view`)
2. A role binding with `patch:system:write` (for `patch_system_edit_assigned`)

Both must be satisfied on the same workspace (or inherited from parent).

### Example 3: Host-Level Permission via Workspace Assignment

Hosts are assigned to workspaces. A host's permissions come from its workspace:

```
definition hbi/host {
    relation t_workspace: rbac/workspace

    permission view = t_workspace->inventory_host_view
    permission patch_system_view = view & t_workspace->patch_system_view
}
```

**SpiceDB Tuples for a host:**

```
hbi/host:host-uuid-123#workspace@rbac/workspace:aaaaaaaa-default-ws-uuid
```

To check if a user can view this host, SpiceDB follows:

```
hbi/host:host-uuid-123
  └─ #workspace → rbac/workspace:aaaaaaaa-default-ws-uuid
       └─ (checks inventory_host_view on the workspace, including inherited bindings)
```

---

## Org-Level Permission Examples

Org-level permissions are **not scoped to specific data** and apply across the entire organization. They are bound at the **tenant** or **platform** level.

### Example 4: Notifications Administrator (Tenant-Level)

**Scenario:** Grant the "IT Ops" group full notifications management across the organization.

**V1 (BindingMapping):**

```
BindingMapping:
  role:                    Role(name="Notifications Administrator", system=True)
  resource_type_namespace: "rbac"
  resource_type_name:      "tenant"
  resource_id:             "o_12345"
  mappings: {
    "id": "44444444-binding-uuid",
    "role": {"id": "55555555-notif-admin-role-uuid", "is_system": true, "permissions": []},
    "groups": ["66666666-itops-group-uuid"],
    "users": {}
  }
```

**V2 (RoleBinding):**

```
RoleBinding:
  uuid:          "44444444-binding-uuid"
  role:          RoleV2(name="Notifications Administrator", type="seeded")
  resource_type: "tenant"
  resource_id:   "o_12345"
  tenant:        Tenant(org_id="12345")

RoleBindingGroup:
  binding: (above)
  group:   Group(name="IT Ops", uuid="66666666-itops-group-uuid")
```

**SpiceDB Tuples:**

```
rbac/role_binding:44444444-binding-uuid#role@rbac/role:55555555-notif-admin-role-uuid
rbac/role:55555555-notif-admin-role-uuid#notifications_notifications_read@rbac/principal:*
rbac/role:55555555-notif-admin-role-uuid#notifications_notifications_write@rbac/principal:*
rbac/role:55555555-notif-admin-role-uuid#notifications_events_read@rbac/principal:*
rbac/role_binding:44444444-binding-uuid#subject@rbac/group:66666666-itops-group-uuid#member
rbac/tenant:o_12345#binding@rbac/role_binding:44444444-binding-uuid
```

**Permission resolution at tenant level:**

```
definition rbac/tenant {
    relation t_binding: rbac/role_binding

    permission notifications_notifications_edit =
        t_binding->notifications_notifications_edit
        + t_platform->notifications_notifications_edit
}
```

Tenant-level bindings grant org-wide access regardless of workspace structure.

### Example 5: Platform-Level Permissions

The **platform** resource handles org-level permissions separately from the workspace hierarchy. This is used for services that don't need workspace scoping.

```
definition rbac/platform {
    relation t_binding: rbac/role_binding

    // Pure org-level: just needs the binding
    permission integrations_endpoints_view = t_binding->integrations_endpoints_view

    // Data-dependent: requires host view AND the specific permission
    permission advisor_recommendation_results_view =
        inventory_host_view & advisor_recommendation_results_view_assigned
}
```

**SpiceDB Tuples:**

```
rbac/platform:12345#binding@rbac/role_binding:77777777-platform-binding-uuid
```

The tenant definition connects to the platform:

```
definition rbac/tenant {
    relation t_platform: rbac/platform

    permission integrations_endpoints_view =
        t_binding->integrations_endpoints_view        // from direct tenant bindings
        + t_platform->integrations_endpoints_view     // from platform bindings
}
```

### Example 6: RBAC Administrator (Full Org-Level Access)

**Scenario:** An org admin who can manage workspaces, role bindings, and view all resources.

**V2 (RoleBinding):**

```
RoleBinding:
  uuid:          "88888888-binding-uuid"
  role:          RoleV2(name="RBAC Administrator", type="platform")
  resource_type: "tenant"
  resource_id:   "o_12345"
  tenant:        Tenant(org_id="12345")

RoleBindingGroup:
  binding: (above)
  group:   Group(name="Admin Default", admin_default=True)  # public tenant group
```

The platform role "RBAC Administrator" is a parent role that contains seeded children. The SpiceDB `t_child` relation handles aggregation:

```
definition rbac/role {
    relation t_child: rbac/role

    permission rbac_workspace_view =
        inventory_groups_read + inventory_groups_all
        + inventory_all_read + inventory_all_all
        + all_all_all
        + t_child->rbac_workspace_view   // permissions from child roles
}
```

### Example 7: Subscriptions Viewer (Org-Level, No Data Dependency)

**Scenario:** Grant read-only access to subscription information org-wide.

**V2 (RoleBinding):**

```
RoleBinding:
  uuid:          "99999999-binding-uuid"
  role:          RoleV2(name="Subscriptions Viewer", type="seeded")
  resource_type: "tenant"
  resource_id:   "o_67890"
  tenant:        Tenant(org_id="67890")

RoleBindingGroup:
  binding: (above)
  group:   Group(name="Finance Team", uuid="ffffffff-finance-group-uuid")
```

**Permissions granted (all org-level, no data intersection):**

```
subscriptions_organization_view
subscriptions_product_view
subscriptions_cloud_access_view
subscriptions_manifest_view
subscriptions_report_view
```

---

## Dual-Write Synchronization

During the V1→V2 transition, every change to a `BindingMapping` is mirrored to a `RoleBinding`.

**Source:** `rbac/management/group/relation_api_dual_write_subject_handler.py`

### Sync Flow

```
V1 Operation (e.g., add group to policy)
    │
    ├─► Update BindingMapping (V1 model)
    │     - Modify mappings JSON
    │     - Save to database
    │
    ├─► Sync to RoleBinding (V2 model)
    │     - _sync_binding_mapping_to_role_binding()
    │     - Update RoleBindingGroup entries
    │     - Update RoleBindingPrincipal entries
    │
    └─► Replicate to SpiceDB
          - OutboxReplicator writes to outbox table
          - Async worker sends to SpiceDB
```

### Sync Function

```python
def _sync_binding_mapping_to_role_binding(binding_mapping, role_binding):
    # Assertions: role, resource_type, resource_id must match
    assert role_binding.role.v1_source == binding_mapping.role
    assert role_binding.resource_type == binding_mapping.resource_type_name
    assert role_binding.resource_id == binding_mapping.resource_id

    # Replace groups and principals atomically
    role_binding.update_groups_by_uuid(binding_mapping.mappings["groups"])
    role_binding.update_principals_by_user_id(binding_mapping.mappings["users"].items())
    role_binding.save()
```

### Constraints During Dual-Write

- The binding's ID, role, and resource **cannot change** during an update
- Principal bindings are **not allowed for custom roles** (only system/seeded roles)
- SpiceDB handles deduplication via `TOUCH` operations

---

## Default Role Bindings

Every tenant gets default role bindings created lazily on first access.

**Source:** `rbac/management/role_binding/service.py:308-487`

### Default Binding Types

| Access Type | Group | Description |
|-------------|-------|-------------|
| ADMIN | Admin Default Group (public tenant) | Org admin access |
| USER | Platform Default Group (public tenant) | Standard user access |

### Default Bindings Per Scope

For each scope (TENANT, ROOT, DEFAULT), the system creates:

```
                    TENANT scope          ROOT scope          DEFAULT scope
                    ─────────────         ──────────          ─────────────
ADMIN binding  →    tenant:o_12345       workspace:root-ws   workspace:default-ws
USER binding   →    tenant:o_12345       workspace:root-ws   workspace:default-ws
```

This gives 6 default bindings total (3 scopes x 2 access types).

USER bindings are **skipped** if the tenant has a custom default group (a group with `platform_default=True` belonging to the tenant, not the public tenant).

### Deterministic UUIDs

Default binding UUIDs are deterministic, derived from the `TenantMapping` model. This ensures idempotent creation across restarts and replicas.

---

## API and Querying

### Endpoint

```
GET /api/rbac/v2/role_bindings/by_subject/
```

### Required Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `resource_type` | Type of resource | `workspace` |
| `resource_id` | ID of the resource | `550e8400-...` |

### Optional Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `subject_type` | Filter by subject type | `group` |
| `subject_id` | Filter by specific subject | group UUID |
| `fields` | Dynamic field selection | `subject(group.name),role(name)` |
| `order_by` | Sort order | `role_name`, `-modified` |
| `exclude_sources` | Exclude bindings by source type | `direct`, `indirect`, `none` (default) |

### Field Selection Syntax

```
fields=subject(group.name,group.user_count),role(name,description),resource(type)
```

### Response Format (Grouped by Subject)

```json
{
  "meta": { "count": 2 },
  "data": [
    {
      "subject": {
        "group": {
          "uuid": "33333333-engineering-group-uuid",
          "name": "Engineering",
          "user_count": 15
        }
      },
      "roles": [
        {
          "uuid": "22222222-viewer-role-uuid",
          "name": "Inventory Host Viewer",
          "description": "View inventory hosts"
        },
        {
          "uuid": "55555555-notif-admin-role-uuid",
          "name": "Notifications Administrator",
          "description": "Manage notifications"
        }
      ],
      "resource": {
        "type": "workspace",
        "id": "aaaaaaaa-default-ws-uuid"
      }
    }
  ]
}
```

### Access Control for the API

The role binding API itself is protected by two permission checks:

1. **System User Check** — non-admin service accounts are denied
2. **Kessel Permission Check** — the requesting user must have `role_binding_view` (or `view`) on the specified workspace, checked via the Kessel Inventory API

---

## Database Tables Summary

| Table | Purpose |
|-------|---------|
| `management_bindingmapping` | V1 bridge model; JSON-encoded V2 binding data linked to V1 Role |
| `management_rolebinding` | V2 role binding; links RoleV2 to a resource within a tenant |
| `management_rolebindinggroup` | Join table: RoleBinding ↔ Group |
| `management_rolebindingprincipal` | Join table: RoleBinding ↔ Principal (with source tracking) |
| `management_rolev2` | V2 role definitions (custom, seeded, platform) |
| `management_workspace` | Workspaces forming the resource hierarchy |
