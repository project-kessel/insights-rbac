# Manage Access with RBAC

This guide shows how to use Kessel RBAC to manage access control in your application. You'll learn how to:

- **Model your authorization** using RBAC schema extensions
- **Create custom roles** and grant them to users via role bindings
- **Check permissions** at runtime using Kessel Relations API
- **Leverage workspace hierarchy** for permission inheritance

This is a practical guide focused on common patterns. For migration strategies, see the [Step-by-Step Migration Checklist](#step-by-step-migration-checklist) at the end.

**Note on Code Examples**: Examples use Python/Django and SpiceDB schema language. The concepts apply to any technology stack - adapt to your language and framework.

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Modeling Authorization with Schema Extensions](#modeling-authorization-with-schema-extensions)
   - [RBAC Schema Extension Decorators](#rbac-schema-extension-decorators)
   - [Testing Your Schema in SpiceDB Playground](#testing-your-schema-in-spicedb-playground)
4. [Creating Custom Roles (V2 API)](#creating-custom-roles-v2-api)
5. [Managing Role Bindings (V2 API)](#managing-role-bindings-v2-api)
6. [Workspace Hierarchy and Permission Inheritance](#workspace-hierarchy-and-permission-inheritance)
7. [Common Access Patterns](#common-access-patterns)
   - [Pattern 1: Org-Wide Settings](#pattern-1-org-wide-settings-organization-level-permissions)
   - [Pattern 2: Asset-Level Access](#pattern-2-asset-level-access-direct-resource-permissions)
   - [Pattern 3: Workspace-Scoped Roles](#pattern-3-workspace-scoped-roles-most-common)
   - [Pattern 4: Hierarchical Resources](#pattern-4-hierarchical-resources-with-contingent-permissions)
   - [Pattern 5: Hybrid Role + Attribute-Based](#pattern-5-role-based--attribute-based-access-hybrid)
8. [Integrating Permission Checks](#integrating-permission-checks)
9. [Common Pitfalls](#common-pitfalls)
10. [Resources](#resources)
11. [Appendix: Migration Guide](#appendix-migration-guide) _(Optional - for teams migrating existing authorization)_

---

## Overview

Kessel RBAC provides centralized, workspace-based access control for console.redhat.com applications. Instead of each application implementing its own authorization logic, applications integrate with the RBAC service to:

- **Model authorization** using schema extensions (define your resources and permissions)
- **Create custom roles** with application-specific permissions
- **Manage access** via role bindings that grant roles to users/groups on workspaces
- **Check permissions** at runtime using Kessel Relations API
- **Inherit permissions** through workspace hierarchy automatically

### Key Benefits

- **Centralized management**: Org admins control access across all applications from one place
- **Consistent UX**: Users experience the same permission model everywhere
- **Reduced complexity**: No need to build/maintain authorization logic
- **Workspace inheritance**: Permissions cascade down the tree automatically
- **Flexible modeling**: Schema extensions let you define resources and relationships specific to your application

### Integration Model

```
┌─────────────────────┐
│  Your Application   │
│                     │
│  ┌───────────────┐  │
│  │ Endpoint      │  │
│  │ Handler       │  │
│  └───────┬───────┘  │
│          │          │
│          │ 1. Check permission
│          │    via RBAC API
│          ▼          │
│  ┌───────────────┐  │
│  │ RBAC Client   │──┼──► insights-rbac service
│  │ (HTTP/gRPC)   │  │    ↓
│  └───────────────┘  │    Kessel Relations API
│                     │    (SpiceDB)
│  ┌───────────────┐  │
│  │ Business      │  │
│  │ Logic         │  │
│  └───────────────┘  │
└─────────────────────┘
```

**Flow:**
1. User makes request to your application
2. Your app extracts user identity from `x-rh-identity` header
3. Your app checks permission via Kessel Relations API (e.g., "Can user read hosts?")
   - **Note**: The RBAC v2 API manages workspaces, roles, and role bindings, but does not provide direct permission check endpoints. For runtime permission checks, applications should integrate with Kessel Relations API (gRPC) directly, or query role bindings via the RBAC API and implement authorization logic.
4. Kessel Relations evaluates the permission against stored tuples (SpiceDB)
5. Your app proceeds or returns 403 based on response

---

## Prerequisites

Before migrating, ensure you have:

1. **Identity header integration**: Your application must receive and validate the `x-rh-identity` header injected by the platform gateway (3scale). This header contains:
   - Organization ID (`org_id`)
   - User identity (`identity.user.username` or `identity.service_account.client_id`)
   - Account number (legacy)
   - Entitlements

2. **Permission definitions**: Define your application's permissions in the format `application:resource_type:operation`. Examples:
   - `inventory:hosts:read`
   - `advisor:recommendation_results:write`
   - `cost-management:cost_models:*`

3. **Resource scoping**: Decide whether your resources are:
   - **Data-level** (workspace-scoped): Most application resources (hosts, reports, configs)
   - **Org-level** (organization-wide): Admin functions (create workspaces, manage billing)

4. **Workspace strategy**: Determine how your resources map to workspaces:
   - Will you use existing RBAC workspaces?
   - Do you need custom workspace hierarchies?
   - Should resources inherit parent workspace permissions?

5. **RBAC API access**: Your application needs:
   - Network access to the RBAC service
   - Service-to-service authentication (PSK or identity header)
   - The RBAC API base URL (e.g., `https://console.redhat.com/api/rbac/v2/`)

---

## Modeling Authorization with Schema Extensions

Schema extensions are the **core mechanism** for modeling your application's authorization in Kessel RBAC. They define:

1. **Resource types** your application manages (hosts, reports, clusters, etc.)
2. **Relations** between resources and subjects (user, group, workspace)
3. **Permissions** derived from those relations using RBAC decorators

### Understanding the Schema Model

Kessel RBAC uses a relationship-based authorization model (Google Zanzibar / SpiceDB). Authorization is expressed as relationship tuples:

```
(subject, relation, resource)
```

Examples:
- `(user:alice, viewer, workspace:prod)` - Alice is a viewer of the prod workspace
- `(group:sre-team, admin, workspace:infrastructure)` - The SRE team are admins of the infrastructure workspace

**Testing your schemas**: Use the [SpiceDB Playground](https://play.authzed.com/) to test schema definitions and permission checks interactively before deploying them.

### Defining Your Application's Schema Extension

Schema extensions are written in SpiceDB schema language and registered with Kessel.

#### Example: Basic Application Schema (myapp/host and myapp/cluster)

```spicedb
// Define your application's resource types
definition myapp/host {
    // Relations define who can do what
    relation workspace: workspace
    relation viewer: user | group#member
    relation editor: user | group#member

    // Permissions use RBAC decorators to compose access rules
    permission read = viewer + editor + workspace->viewer + workspace->editor
    permission write = editor + workspace->editor
    permission delete = editor + workspace->editor
}

definition myapp/cluster {
    relation workspace: workspace
    relation admin: user | group#member
    relation viewer: user | group#member

    permission read = viewer + admin + workspace->viewer + workspace->admin
    permission write = admin + workspace->admin
    permission delete = admin + workspace->admin
}
```

**Key concepts:**

- **`relation workspace`**: Links the resource to a workspace (enables inheritance)
- **`relation viewer/editor/admin`**: Direct grants to users or groups
- **`permission read`**: Computed permission that checks both direct grants AND workspace inheritance (`workspace->viewer`)
- **`user | group#member`**: Union type - accepts either users or group members

### RBAC Schema Extensions (KSL Decorators)

The RBAC system uses **Kessel Schema Language (KSL)** extensions (decorators) for advanced permission modeling. These are defined in `/rbac-config/configs/prod/schemas/src/rbac.ksl` and used across all application schemas.

#### 1. `@rbac.add_v1_based_permission` - V1 Compatibility Decorator

Adds a permission that bridges V1 (group-based) and V2 (role binding-based) authorization. This decorator allows gradual migration from V1 to V2 while maintaining backward compatibility.

**Syntax**:
```ksl
@rbac.add_v1_based_permission(app:'<app>', resource:'<resource>', verb:'<verb>', v2_perm:'<permission_name>');
```

**Parameters**:
- `app` - Application name (e.g., `'advisor'`, `'inventory'`)
- `resource` - Resource type (e.g., `'hosts'`, `'recommendations'`)
- `verb` - Operation (e.g., `'read'`, `'write'`, `'delete'`)
- `v2_perm` - V2 permission name (e.g., `'advisor_recommendation_results_view'`)

**When to use**: During V1→V2 migration when you need to support both:
- V1 users with `app:resource:verb` permissions via groups
- V2 users with role bindings granting `v2_perm`

**Real example from `advisor.ksl`**:

```ksl
version 0.1
namespace advisor

import rbac

@rbac.add_v1_based_permission(
    app:'advisor',
    resource:'recommendation_results',
    verb:'read',
    v2_perm:'advisor_recommendation_results_view_assigned'
);
```

**What this generates** (in compiled SpiceDB schema):

```spicedb
definition rbac/role {
    // Wildcard relations for V1 compatibility
    private relation advisor_all_all: [bool]
    private relation advisor_recommendation_results_all: [bool]
    private relation advisor_all_read: [bool]
    private relation advisor_recommendation_results_read: [bool]

    // V2 permission inherits from all V1 wildcards
    permission advisor_recommendation_results_view_assigned =
        advisor_recommendation_results_read +      // V1: advisor:recommendation_results:read
        advisor_recommendation_results_all +       // V1: advisor:recommendation_results:*
        advisor_all_read +                         // V1: advisor:*:read
        advisor_all_all +                          // V1: advisor:*:*
        all_all_all +                              // V1: *:*:*
        child.advisor_recommendation_results_view_assigned  // Parent role inheritance
}

definition rbac/role_binding {
    // Role binding requires BOTH subject match AND role permission
    permission advisor_recommendation_results_view_assigned =
        subject and role.advisor_recommendation_results_view_assigned
}

definition rbac/workspace {
    // Workspace permission inherits from bindings and parent workspaces
    permission advisor_recommendation_results_view_assigned =
        binding.advisor_recommendation_results_view_assigned +
        parent.advisor_recommendation_results_view_assigned
}

definition rbac/platform {
    // Platform (org-level) permission from bindings
    permission advisor_recommendation_results_view_assigned =
        binding.advisor_recommendation_results_view_assigned
}

definition rbac/tenant {
    // Tenant permission from bindings or platform
    permission advisor_recommendation_results_view_assigned =
        binding.advisor_recommendation_results_view_assigned +
        platform.advisor_recommendation_results_view_assigned
}
```

**How it works**:
1. **V1 users**: Have `advisor:recommendation_results:read` in a V1 group → permission granted via wildcard relations
2. **V2 users**: Have role binding with `advisor_recommendation_results_view_assigned` → permission granted via role binding
3. Both paths work simultaneously during migration

**Usage in application code**:

```python
# Check permission via Kessel Relations API
allowed = kessel.check(
    subject=f"user:{user_uuid}",
    relation="advisor_recommendation_results_view_assigned",
    resource={"type": "rbac/workspace", "id": str(workspace_id)}
)
# Returns true if user has EITHER:
# - V1 group with advisor:recommendation_results:read, OR
# - V2 role binding with advisor_recommendation_results_view_assigned
```

#### 2. `@rbac.add_contingent_permission` - Composite Permission Decorator

Adds a permission that requires BOTH a first permission AND a second permission (intersection). This is used for data-dependent permissions where access to data requires host visibility plus specific operation permission.

**Syntax**:
```ksl
@rbac.add_contingent_permission(first: '<perm1>', second: '<perm2>', contingent: '<composite_perm>');
```

**Parameters**:
- `first` - First required permission (usually `'inventory_host_view'`)
- `second` - Second required permission (the operation-specific permission)
- `contingent` - Name of the composite permission

**When to use**: When accessing application data requires:
1. **Host visibility** (`inventory_host_view`) - user can see the hosts, AND
2. **Operation permission** (e.g., `advisor_recommendation_results_view_assigned`) - user can perform the operation

This prevents users from seeing data about hosts they don't have access to.

**Real example from `advisor.ksl`**:

```ksl
version 0.1
namespace advisor

import rbac

// First, define the V1-based permission (the "second" permission)
@rbac.add_v1_based_permission(
    app:'advisor',
    resource:'recommendation_results',
    verb:'read',
    v2_perm:'advisor_recommendation_results_view_assigned'
);

// Then, create the contingent permission requiring BOTH host view AND the assigned permission
@rbac.add_contingent_permission(
    first: 'inventory_host_view',
    second: 'advisor_recommendation_results_view_assigned',
    contingent: 'advisor_recommendation_results_view'
);
```

**What this generates** (in compiled SpiceDB schema):

```spicedb
definition rbac/workspace {
    // Contingent permission requires BOTH first AND second
    permission advisor_recommendation_results_view =
        inventory_host_view and advisor_recommendation_results_view_assigned
}

definition rbac/platform {
    permission advisor_recommendation_results_view =
        inventory_host_view and advisor_recommendation_results_view_assigned
}

definition rbac/tenant {
    permission advisor_recommendation_results_view =
        inventory_host_view and advisor_recommendation_results_view_assigned
}
```

**How it works**:
1. User must have `inventory_host_view` (can see hosts in the workspace)
2. **AND** user must have `advisor_recommendation_results_view_assigned` (assigned the advisor permission)
3. Only if BOTH are true, `advisor_recommendation_results_view` is granted

**Common pattern - three permission layers**:

```ksl
// Layer 1: V1-based permission (operation)
@rbac.add_v1_based_permission(app:'advisor', resource:'recommendation_results', verb:'read',
                               v2_perm:'advisor_recommendation_results_view_assigned');

// Layer 2: Contingent permission (operation + host view)
@rbac.add_contingent_permission(first: 'inventory_host_view',
                                 second: 'advisor_recommendation_results_view_assigned',
                                 contingent: 'advisor_recommendation_results_view');

// Layer 3: Expose to hosts (make checkable at host level)
@hbi.expose_host_permission(v2_perm: 'advisor_recommendation_results_view',
                            host_perm: 'advisor_recommendation_results_view');
```

**Usage in application code**:

```python
# Check if user can view advisor recommendation results
allowed = kessel.check(
    subject=f"user:{user_uuid}",
    relation="advisor_recommendation_results_view",
    resource={"type": "rbac/workspace", "id": str(workspace_id)}
)
# Returns true ONLY if user has BOTH:
# 1. inventory_host_view on the workspace (can see hosts), AND
# 2. advisor_recommendation_results_view_assigned (has the advisor permission)
```

**Real-world examples** (from production schemas):

```ksl
// Vulnerability - requires host view + vulnerability permission
@rbac.add_contingent_permission(
    first: 'inventory_host_view',
    second: 'vulnerability_vulnerability_results_view_assigned',
    contingent: 'vulnerability_vulnerability_results_view'
);

// Patch - requires host view + patch permission
@rbac.add_contingent_permission(
    first: 'inventory_host_view',
    second: 'patch_system_view_assigned',
    contingent: 'patch_system_view'
);

// Malware - requires host view + malware permission
@rbac.add_contingent_permission(
    first:'inventory_host_view',
    second:'malware_malware_view_assigned',
    contingent:'malware_malware_view'
);

// Compliance - requires host view + compliance permission
@rbac.add_contingent_permission(
    first: 'inventory_host_view',
    second: 'compliance_system_view_assigned',
    contingent: 'compliance_system_view'
);
```

#### 3. `@rbac.add_unified_permission` - Unified V1/V2 Permission

Adds a permission where V1 and V2 use the **same permission name** (in `app_resource_verb` format). This avoids naming conflicts during migration.

**Syntax**:
```ksl
@rbac.add_unified_permission(app:'<app>', resource:'<resource>', verb:'<verb>');
```

**Parameters**:
- `app` - Application name
- `resource` - Resource type
- `verb` - Operation

**When to use**: When the V1 permission name matches the desired V2 permission name exactly (e.g., `rbac_roles_read` in both V1 and V2).

**Real example from `rbac.ksl`**:

```ksl
@add_unified_permission(app:'rbac', resource:'roles', verb:'read')
@add_unified_permission(app:'rbac', resource:'roles', verb:'write')
@add_unified_permission(app:'rbac', resource:'groups', verb:'read')
@add_unified_permission(app:'rbac', resource:'groups', verb:'write')
```

**What this generates**:

```spicedb
definition rbac/role {
    // Direct writable relation (supports both V1 assignment and V2 wildcards)
    permission rbac_roles_read = [bool] or rbac_roles_all or rbac_all_read or rbac_all_all or all_all_all or child.rbac_roles_read
}
```

**Difference from `@rbac.add_v1_based_permission`**:
- **Unified**: Permission name is the same in V1 and V2 (`rbac_roles_read`)
- **V1-based**: V1 uses different name (`rbac:roles:read`) than V2 (`rbac_roles_view`)

#### 4. `@rbac.add_v1only_permission` - V1-Only Permission

Adds a permission that exists ONLY in V1 and has no V2 equivalent. Used for deprecated permissions during migration.

**Syntax**:
```ksl
@rbac.add_v1only_permission(perm:'<permission_name>');
```

**When to use**: When a V1 permission is being deprecated and won't exist in V2, but must remain functional during migration.

**Real examples from production schemas**:

```ksl
// Compliance - V1-only permissions
@rbac.add_v1only_permission(perm:'compliance_policy_update');
@rbac.add_v1only_permission(perm:'compliance_report_delete');

// Patch - V1-only permission
@rbac.add_v1only_permission(perm:'patch_template_write');

// Playbook Dispatcher - V1-only permissions
@rbac.add_v1only_permission(perm:'playbook_dispatcher_run_read');
@rbac.add_v1only_permission(perm:'playbook_dispatcher_run_write');
```

**What this generates**:

```spicedb
definition rbac/role {
    // Writable relation for V1 assignment only
    private relation compliance_policy_update: [bool]
}
```

**Why use this**: Prevents breaking V1 users while signaling the permission won't be in V2.

---

### SpiceDB Schema Patterns (Non-Decorator)

In addition to KSL decorators, the schemas use standard SpiceDB language features:

#### Pattern A: Hierarchical Inheritance via `parent` relation

Built-in SpiceDB operators and relation patterns used by the decorators.

**Union (`+`)**: Permission granted if ANY condition is true
```spicedb
permission view = binding.view + parent.view
```

**Intersection (`&`)**: Permission granted if ALL conditions are true
```spicedb
permission contingent_perm = first_perm and second_perm
```

**Arrow (`->`)**: Follow a relation to another resource
```spicedb
permission view = workspace->inventory_host_view
```

**Inheritance**: Permissions flow down the workspace tree via `parent` relation
```spicedb
definition rbac/workspace {
    relation parent: [AtMostOne workspace]
    relation binding: [Any role_binding] or parent.binding
}
```

These patterns are used by the KSL decorators documented above.

### Example Schema: Cross-Referencing Platform Schemas

The RBAC system itself uses these patterns. Here are simplified versions of the core schemas:

#### Core RBAC Schema (rbac.ksl equivalent)

```spicedb
// Core workspace definition
definition rbac/workspace {
    // Hierarchical parent relation
    relation t_parent: rbac/workspace

    // Direct role grants
    relation viewer: user | group#member
    relation editor: user | group#member
    relation admin: user | group#member

    // Permissions inherit up the tree
    permission workspace_read = viewer + editor + admin + t_parent->workspace_read
    permission workspace_write = editor + admin + t_parent->workspace_write
    permission workspace_admin = admin + t_parent->workspace_admin

    // Role binding management permissions
    permission role_binding_grant = admin + t_parent->role_binding_grant
    permission role_binding_revoke = admin + t_parent->role_binding_revoke
}

// Tenant (organization) definition
definition rbac/tenant {
    relation org_admin: user | group#member
    relation default_workspace: rbac/workspace

    permission tenant_admin = org_admin
    permission workspace_create = org_admin
}
```

#### Application Schema Extending RBAC (kessel.ksl equivalent)

```spicedb
// Application-specific resource
definition inventory/host {
    relation workspace: rbac/workspace
    relation direct_viewer: user | group#member
    relation direct_editor: user | group#member

    // Inherit from workspace roles
    permission read = direct_viewer + direct_editor + workspace->viewer + workspace->editor + workspace->admin
    permission write = direct_editor + workspace->editor + workspace->admin
    permission delete = workspace->admin

    // Add V1 backward compatibility
    @rbac.add_v1_based_permission(permission_string="inventory:hosts:read")
    permission v1_read = read

    @rbac.add_v1_based_permission(permission_string="inventory:hosts:write")
    permission v1_write = write
}

// Multi-level resource hierarchy
definition inventory/host_group {
    relation workspace: rbac/workspace
    relation owner: user | group#member

    permission read = owner + workspace->viewer + workspace->admin
    permission write = owner + workspace->admin
}

definition inventory/host_in_group {
    relation host_group: inventory/host_group
    relation workspace: rbac/workspace

    // Direct workspace permissions
    permission read = workspace->viewer + workspace->admin

    // Contingent permission from host_group
    @rbac.add_contingent_permission(
        contingent_resource_type="inventory/host_group",
        contingent_permission="read"
    )
    permission group_read = read

    permission write = workspace->admin

    @rbac.add_contingent_permission(
        contingent_resource_type="inventory/host_group",
        contingent_permission="write"
    )
    permission group_write = write
}
```

**Key patterns demonstrated:**

1. **Workspace inheritance**: All resources link to `rbac/workspace` and inherit viewer/editor/admin roles
2. **Hierarchical workspaces**: `t_parent` relation enables multi-level workspace trees
3. **V1 compatibility**: `@rbac.add_v1_based_permission` bridges V1 and V2
4. **Container-based access**: `@rbac.add_contingent_permission` grants access via parent resources

### Testing Your Schema in SpiceDB Playground

Before deploying your schema, test it interactively:

1. **Go to**: [https://play.authzed.com/](https://play.authzed.com/)
2. **Paste your schema** in the left panel
3. **Add sample relationships**:
   ```
   // Create workspace hierarchy
   rbac/workspace:prod#t_parent@rbac/workspace:root

   // Grant alice viewer role on root workspace
   rbac/workspace:root#viewer@user:alice

   // Create host in prod workspace
   inventory/host:web-1#workspace@rbac/workspace:prod
   ```
4. **Test permissions**:
   ```
   // Check if alice can read web-1 (should be true via inheritance)
   inventory/host:web-1#read@user:alice
   ```
5. **Validate**: The playground shows the evaluation path and whether the check passes

**Common testing scenarios:**

- ✅ User with role on parent workspace can access child resources
- ✅ User with role on child workspace cannot access parent resources (no upward inheritance)
- ✅ Contingent permissions grant access via related resources
- ✅ V1-based permissions grant access to users in V1 groups

### Permission Inheritance Through Workspaces

The `workspace->viewer` syntax enables permission inheritance. If a user has `viewer` role on workspace `prod`, they automatically get `read` permission on all resources with `relation workspace: workspace` pointing to `prod`.

**Hierarchy example:**

```
Root Workspace (org-123)
├── Production (workspace:prod)
│   ├── host:web-server-1
│   └── host:web-server-2
└── Staging (workspace:staging)
    └── host:test-server-1
```

If Alice has `viewer` role binding on `workspace:prod`:
- ✅ Can read `host:web-server-1` (inherits via `workspace->viewer`)
- ✅ Can read `host:web-server-2` (inherits via `workspace->viewer`)
- ❌ Cannot read `host:test-server-1` (different workspace)

### Registering Your Schema Extension

Work with the RBAC team to register your schema extension in Kessel. The schema is deployed as part of the Kessel Relations API configuration.

**Steps:**

1. **Design your schema**: Define resource types, relations, and permissions
2. **Document your permissions**: Create a mapping of operations to permissions (e.g., `GET /hosts/:id` requires `myapp/host#read`)
3. **Submit schema for review**: RBAC team validates the schema
4. **Schema deployment**: RBAC team deploys the schema to Kessel Relations API
5. **Create default roles**: Define seeded roles that grant your permissions (next section)

### Best Practices for Schema Design

**DO:**
- ✅ Include `relation workspace: workspace` on all workspace-scoped resources
- ✅ Use workspace inheritance for permission checks (`workspace->viewer`)
- ✅ Define 3-5 permission levels per resource type (read, write, delete, execute, admin)
- ✅ Use union types for relations (`user | group#member`) to support both direct and group-based grants
- ✅ Model hierarchical resources with parent relations (e.g., `relation cluster: myapp/cluster` on a host)

**DON'T:**
- ❌ Create overly granular permissions (per-field access)
- ❌ Hardcode user IDs in schema definitions
- ❌ Skip workspace inheritance (breaks the workspace hierarchy model)
- ❌ Model application-specific business logic in schema (e.g., "can only edit hosts on Tuesdays")

---

## Creating Custom Roles (V2 API)

Roles define collections of permissions. RBAC V2 provides seeded platform roles (Org Admin, Workspace Admin, Workspace Viewer), but applications should create custom roles for application-specific permissions.

### Role Structure (V2)

```json
{
  "id": "uuid-v7",
  "name": "MyApp Editor",
  "display_name": "MyApp Editor",
  "description": "Can view and edit MyApp resources",
  "permissions": [
    "myapp:hosts:read",
    "myapp:hosts:write",
    "myapp:clusters:read"
  ],
  "created": "2024-01-15T10:30:00Z",
  "modified": "2024-01-15T10:30:00Z"
}
```

### Creating Roles via V2 API

**Endpoint**: `POST /api/rbac/v2/roles/`

**Request:**

```bash
curl -X POST https://console.redhat.com/api/rbac/v2/roles/ \
  -H "x-rh-identity: $IDENTITY_HEADER" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "MyApp Editor",
    "display_name": "MyApp Editor",
    "description": "Can view and edit MyApp resources",
    "permissions": [
      "myapp:hosts:read",
      "myapp:hosts:write",
      "myapp:clusters:read"
    ]
  }'
```

**Response:**

```json
{
  "id": "01933b2e-f5c0-7890-b2c3-0242ac120002",
  "name": "MyApp Editor",
  "display_name": "MyApp Editor",
  "description": "Can view and edit MyApp resources",
  "permissions": [
    "myapp:hosts:read",
    "myapp:hosts:write",
    "myapp:clusters:read"
  ],
  "created": "2024-01-15T10:30:00Z",
  "modified": "2024-01-15T10:30:00Z"
}
```

### Permission Format

Permissions follow the format: `application:resource_type:operation`

**Examples:**

- `inventory:hosts:read` - Read hosts in Inventory
- `advisor:recommendations:write` - Write (accept/dismiss) recommendations in Advisor
- `cost-management:cost_models:*` - All operations on cost models
- `myapp:*:*` - All operations on all MyApp resources (admin wildcard)

### Seeded vs Custom Roles

**Seeded roles** are created during system initialization and available to all tenants:

- `Org Admin` - Organization-wide admin (manages workspaces, users)
- `Workspace Admin` - Workspace-level admin (manages role bindings in workspace)
- `Workspace Viewer` - Workspace-level read access

**Custom roles** are created by applications for application-specific permissions:

- `Inventory Viewer` - Read-only access to Inventory
- `Advisor Editor` - View and manage Advisor recommendations
- `Cost Management Admin` - Full control over Cost Management resources

### Role Naming Conventions

- Use title case: `MyApp Editor`, not `myapp-editor`
- Include application name for clarity: `Inventory Viewer`, not just `Viewer`
- Use standard suffixes:
  - `Viewer` - Read-only access
  - `Editor` - Read and write (but not delete)
  - `Admin` - Full control (create, read, write, delete)
  - `Operator` - Execute operations (e.g., run reports, trigger jobs)

### Example: Complete Role Set for an Application

```json
[
  {
    "name": "MyApp Viewer",
    "display_name": "MyApp Viewer",
    "description": "Read-only access to all MyApp resources",
    "permissions": [
      "myapp:hosts:read",
      "myapp:clusters:read",
      "myapp:reports:read"
    ]
  },
  {
    "name": "MyApp Editor",
    "display_name": "MyApp Editor",
    "description": "View and edit MyApp resources",
    "permissions": [
      "myapp:hosts:read",
      "myapp:hosts:write",
      "myapp:clusters:read",
      "myapp:clusters:write",
      "myapp:reports:read",
      "myapp:reports:write"
    ]
  },
  {
    "name": "MyApp Admin",
    "display_name": "MyApp Admin",
    "description": "Full administrative access to MyApp",
    "permissions": [
      "myapp:*:*"
    ]
  },
  {
    "name": "MyApp Report Operator",
    "display_name": "MyApp Report Operator",
    "description": "Can view and execute reports",
    "permissions": [
      "myapp:reports:read",
      "myapp:reports:execute"
    ]
  }
]
```

### Updating Roles

**Endpoint**: `PATCH /api/rbac/v2/roles/{role_id}/`

```bash
curl -X PATCH https://console.redhat.com/api/rbac/v2/roles/01933b2e-f5c0-7890-b2c3-0242ac120002/ \
  -H "x-rh-identity: $IDENTITY_HEADER" \
  -H "Content-Type: application/json" \
  -d '{
    "permissions": [
      "myapp:hosts:read",
      "myapp:hosts:write",
      "myapp:clusters:read",
      "myapp:clusters:write"
    ]
  }'
```

**Note**: Updating a role's permissions immediately affects all role bindings using that role. Changes replicate to Kessel Relations API within 100-500ms.

---

## Managing Role Bindings (V2 API)

Role bindings grant roles to subjects (users or groups) on resources (workspaces). They are the core mechanism for granting access in RBAC.

### Role Binding Structure (V2)

```json
{
  "resource": {
    "id": "01933b2e-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "type": "workspace",
    "name": "Production"
  },
  "subject": {
    "id": "user-uuid-or-group-uuid",
    "type": "user"  // or "group"
  },
  "role": {
    "id": "01933b2e-yyyy-yyyy-yyyy-yyyyyyyyyyyy",
    "name": "MyApp Editor"
  },
  "sources": [
    {
      "resource_id": "01933b2e-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "resource_type": "workspace"
    }
  ]
}
```

### Creating Role Bindings: Batch Create (V2)

**V2 API only supports batch creation** - you must use the `:batchCreate` endpoint even for a single binding.

**Endpoint**: `POST /api/rbac/v2/role-bindings:batchCreate/`

**Request (single binding):**

```bash
curl -X POST https://console.redhat.com/api/rbac/v2/role-bindings:batchCreate/ \
  -H "x-rh-identity: $IDENTITY_HEADER" \
  -H "Content-Type: application/json" \
  -d '{
    "requests": [
      {
        "resource": {
          "id": "01933b2e-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
          "type": "workspace"
        },
        "subject": {
          "id": "user-alice-uuid",
          "type": "user"
        },
        "role": {
          "id": "01933b2e-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        }
      }
    ]
  }'
```

**Request (multiple bindings):**

```bash
curl -X POST https://console.redhat.com/api/rbac/v2/role-bindings:batchCreate/ \
  -H "x-rh-identity: $IDENTITY_HEADER" \
  -H "Content-Type: application/json" \
  -d '{
    "requests": [
      {
        "resource": {"id": "workspace-prod-uuid", "type": "workspace"},
        "subject": {"id": "user-alice-uuid", "type": "user"},
        "role": {"id": "myapp-editor-role-uuid"}
      },
      {
        "resource": {"id": "workspace-prod-uuid", "type": "workspace"},
        "subject": {"id": "group-sre-team-uuid", "type": "group"},
        "role": {"id": "myapp-admin-role-uuid"}
      },
      {
        "resource": {"id": "workspace-staging-uuid", "type": "workspace"},
        "subject": {"id": "user-bob-uuid", "type": "user"},
        "role": {"id": "myapp-viewer-role-uuid"}
      }
    ]
  }'
```

**Response:**

```json
{
  "role_bindings": [
    {
      "resource": {
        "id": "workspace-prod-uuid",
        "type": "workspace",
        "name": "Production"
      },
      "subject": {
        "id": "user-alice-uuid",
        "type": "user"
      },
      "role": {
        "id": "myapp-editor-role-uuid",
        "name": "MyApp Editor"
      },
      "sources": [
        {
          "resource_id": "workspace-prod-uuid",
          "resource_type": "workspace"
        }
      ]
    },
    // ... other created bindings
  ]
}
```

**Limits:**

- Maximum 100 bindings per batch request
- For larger migrations, batch in groups of 100

### Querying Role Bindings

**Get bindings for a subject:**

```bash
# By subject (returns all bindings for a user or group)
GET /api/rbac/v2/role-bindings/by-subject/?subject_id={user_or_group_uuid}
```

**Response:**

```json
{
  "data": [
    {
      "resource": {
        "id": "workspace-prod-uuid",
        "type": "workspace",
        "name": "Production"
      },
      "roles": [
        {
          "id": "myapp-editor-role-uuid",
          "name": "MyApp Editor",
          "permissions": ["myapp:hosts:read", "myapp:hosts:write"]
        }
      ],
      "sources": [
        {
          "resource_id": "workspace-prod-uuid",
          "resource_type": "workspace"
        }
      ]
    }
  ]
}
```

**Get bindings for a resource:**

```bash
# By resource (returns all bindings on a workspace)
GET /api/rbac/v2/role-bindings/by-resource/?resource_id={workspace_uuid}&resource_type=workspace
```

### Deleting Role Bindings

**V2 API does not yet provide a DELETE endpoint for role bindings.** Work with the RBAC team to remove bindings during the migration period.

**Workaround**: Use the V1 API for deletion during migration:

```bash
# V1 DELETE (temporary workaround)
DELETE /api/rbac/v1/roles/{role_uuid}/groups/{group_uuid}/
```

### Role Binding Replication

Role bindings replicate from RBAC to Kessel Relations API asynchronously:

- **Latency**: 100-500ms typical, up to 1-2 seconds under load
- **Consistency**: Eventually consistent (not immediate)
- **Implications**: After creating a role binding, permission checks may return false for a brief window

**Testing pattern:**

```python
# Create role binding
rbac_client.batch_create_role_bindings([binding])

# Wait for replication (integration tests only)
time.sleep(1)

# Now permission check will succeed
assert rbac_client.check_permission(user, permission, resource) == True
```

**Production pattern**: Accept that newly granted permissions may not work immediately. Users should refresh or retry after a few seconds if access is denied.

---

## Workspace Hierarchy and Permission Inheritance

Workspaces form a tree hierarchy within an organization. Permissions granted on a parent workspace automatically apply to all descendant workspaces.

### Workspace Structure

```
Root Workspace (org-abc123)
├── Engineering (workspace:eng)
│   ├── Backend (workspace:backend)
│   │   ├── API Team (workspace:api-team)
│   │   └── Database Team (workspace:db-team)
│   └── Frontend (workspace:frontend)
└── Operations (workspace:ops)
    ├── Production (workspace:prod)
    └── Staging (workspace:staging)
```

### Permission Inheritance Rules

**Direct grant** (role binding on a specific workspace):

```json
{
  "resource": {"id": "workspace:backend", "type": "workspace"},
  "subject": {"id": "user:alice", "type": "user"},
  "role": {"id": "myapp-viewer-role"}
}
```

Alice gets `myapp:*:read` on:
- ✅ `workspace:backend` (direct)
- ✅ `workspace:api-team` (child of backend)
- ✅ `workspace:db-team` (child of backend)
- ❌ `workspace:eng` (parent - no upward inheritance)
- ❌ `workspace:frontend` (sibling - no lateral inheritance)

**Inheritance is transitive**: If Alice has a role on `workspace:eng`, she inherits it on all descendants (`backend`, `api-team`, `db-team`, `frontend`).

### Creating Workspaces with Parents

**Endpoint**: `POST /api/rbac/v2/workspaces/`

```bash
curl -X POST https://console.redhat.com/api/rbac/v2/workspaces/ \
  -H "x-rh-identity: $IDENTITY_HEADER" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "API Team",
    "description": "Backend API team workspace",
    "parent_id": "workspace-backend-uuid"
  }'
```

**Response:**

```json
{
  "id": "01933b2e-cccc-cccc-cccc-cccccccccccc",
  "name": "API Team",
  "description": "Backend API team workspace",
  "parent_id": "workspace-backend-uuid",
  "type": "workspace",
  "created": "2024-01-15T10:30:00Z",
  "modified": "2024-01-15T10:30:00Z"
}
```

### Inheritance in Schema Definitions

Your schema extension must include `workspace->viewer` syntax to enable inheritance:

```spicedb
definition myapp/host {
    relation workspace: workspace
    relation viewer: user | group#member

    // Permission inherits from workspace relation
    permission read = viewer + workspace->viewer
    //                         ^^^^^^^^^^^^^^^^^ inheritance
}
```

Without `workspace->viewer`, permissions are only direct grants (no inheritance).

### Best Practices

- ✅ **Grant broad roles at high levels**: Give "Viewer" on root workspace to all employees
- ✅ **Grant specific roles at lower levels**: Give "Editor" on team-specific workspaces
- ✅ **Use hierarchy to model organizational structure**: Teams, departments, environments
- ❌ **Don't create flat structures**: You lose the benefit of inheritance
- ❌ **Don't grant overly broad permissions at root**: Every user would inherit them

---

## Common Access Patterns

This section demonstrates common authorization patterns using RBAC schema extensions, roles, and role bindings.

### Pattern 1: Org-Wide Settings (Organization-Level Permissions)

**Use case**: Global settings that apply to the entire organization, not specific workspaces.

**Schema**:

```spicedb
definition myapp/org_settings {
    relation tenant: rbac/tenant
    relation admin: user | group#member

    // Only org admins can manage settings
    permission read = admin + tenant->tenant_admin
    permission write = admin + tenant->tenant_admin
}
```

**Role definition**:

```json
{
  "name": "MyApp Org Admin",
  "display_name": "MyApp Organization Administrator",
  "description": "Can manage organization-wide MyApp settings",
  "permissions": [
    "myapp:org_settings:read",
    "myapp:org_settings:write"
  ]
}
```

**Role binding** (grant at tenant level):

```python
# Grant org admin access at the tenant level
binding_request = {
    "resource": {
        "id": f"rbac/tenant:{org_id}",
        "type": "tenant"
    },
    "subject": {
        "id": str(admin_user.uuid),
        "type": "user"
    },
    "role": {
        "id": org_admin_role_id
    }
}

response = requests.post(
    f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
    json={"requests": [binding_request]},
    headers={"x-rh-identity": identity_header}
)
```

**Permission check**:

```python
# Check if user can write org settings
allowed = kessel.check(
    subject=f"user:{user_uuid}",
    relation="write",
    resource={"type": "myapp/org_settings", "id": f"org:{org_id}"}
)
```

**When to use**: Billing settings, organization profile, feature flags, integrations that span all workspaces.

---

### Pattern 2: Asset-Level Access (Direct Resource Permissions)

**Use case**: Grant access to specific resources without workspace inheritance.

**Schema**:

```spicedb
definition myapp/confidential_report {
    relation workspace: rbac/workspace
    relation owner: user
    relation shared_with: user | group#member

    // Only owner and explicitly shared users can access
    permission read = owner + shared_with
    permission write = owner
    permission share = owner

    // No workspace inheritance for confidential resources
}
```

**Granting access** (via application logic, not role bindings):

```python
# Owner creates a confidential report
report = ConfidentialReport.objects.create(
    name="Q4 Financial Forecast",
    owner_id=user_uuid,
    workspace_id=workspace_id
)

# Write relationship tuple directly to Kessel
kessel.write_relationships([
    {
        "resource": {"type": "myapp/confidential_report", "id": str(report.id)},
        "relation": "owner",
        "subject": {"type": "user", "id": str(user_uuid)}
    }
])

# Share with specific user
kessel.write_relationships([
    {
        "resource": {"type": "myapp/confidential_report", "id": str(report.id)},
        "relation": "shared_with",
        "subject": {"type": "user", "id": str(other_user_uuid)}
    }
])
```

**Permission check**:

```python
# Check if user can read this specific report
allowed = kessel.check(
    subject=f"user:{user_uuid}",
    relation="read",
    resource={"type": "myapp/confidential_report", "id": str(report.id)}
)
```

**When to use**: Confidential documents, personal resources, assets requiring explicit sharing (not inherited from workspace roles).

---

### Pattern 3: Workspace-Scoped Roles (Most Common)

**Use case**: Standard workspace-based access where permissions inherit down the workspace tree.

**Schema**:

```spicedb
definition myapp/configuration {
    relation workspace: rbac/workspace
    relation editor: user | group#member

    // Inherit from workspace roles
    permission read = editor + workspace->viewer + workspace->editor + workspace->admin
    permission write = editor + workspace->editor + workspace->admin
    permission delete = workspace->admin
}
```

**Role definition**:

```json
{
  "name": "MyApp Configuration Editor",
  "display_name": "MyApp Configuration Editor",
  "description": "Can view and edit configurations in assigned workspaces",
  "permissions": [
    "myapp:configurations:read",
    "myapp:configurations:write"
  ]
}
```

**Role binding** (grant at workspace):

```python
# Grant configuration editor role on the "Production" workspace
binding_request = {
    "resource": {
        "id": production_workspace_id,
        "type": "workspace"
    },
    "subject": {
        "id": str(user_uuid),
        "type": "user"
    },
    "role": {
        "id": config_editor_role_id
    }
}

response = requests.post(
    f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
    json={"requests": [binding_request]},
    headers={"x-rh-identity": identity_header}
)
```

**Permission check**:

```python
# Check if user can write configuration
allowed = kessel.check(
    subject=f"user:{user_uuid}",
    relation="write",
    resource={"type": "myapp/configuration", "id": str(config.id)}
)
# Returns true if:
# - User has editor role binding on config's workspace (or any ancestor workspace)
# - User has direct editor relation on this config resource
```

**Inheritance example**:

```
Workspace Hierarchy:
  Root (org-123)
  └── Engineering
      ├── Backend
      │   └── API Team  ← User has "Config Editor" role here
      └── Frontend

Configurations:
  - config-1 (workspace: API Team)     ← User CAN write (direct)
  - config-2 (workspace: Backend)      ← User CANNOT write (parent, no upward inheritance)
  - config-3 (workspace: Frontend)     ← User CANNOT write (sibling)
  - config-4 (workspace: Root)         ← User CANNOT write (ancestor)
```

**When to use**: Most application resources - hosts, reports, policies, configurations, clusters, etc.

---

### Pattern 4: Hierarchical Resources with Contingent Permissions

**Use case**: Access to parent resource should grant access to child resources.

**Schema**:

```spicedb
definition myapp/project {
    relation workspace: rbac/workspace
    relation owner: user | group#member
    relation member: user | group#member

    permission read = owner + member + workspace->viewer + workspace->admin
    permission write = owner + workspace->admin
}

definition myapp/task {
    relation project: myapp/project
    relation workspace: rbac/workspace
    relation assignee: user

    // Direct workspace permissions
    permission read = assignee + workspace->viewer + workspace->admin
    permission write = assignee + workspace->editor + workspace->admin

    // Contingent: project members can read tasks
    @rbac.add_contingent_permission(
        contingent_resource_type="myapp/project",
        contingent_permission="read"
    )
    permission project_read = read

    // Contingent: project owners can write tasks
    @rbac.add_contingent_permission(
        contingent_resource_type="myapp/project",
        contingent_permission="write"
    )
    permission project_write = write
}
```

**Setting up relationships**:

```python
# Create project
project = Project.objects.create(name="New Feature", workspace_id=workspace_id)

# Grant user as project owner (via Kessel relationship tuple)
kessel.write_relationships([
    {
        "resource": {"type": "myapp/project", "id": str(project.id)},
        "relation": "owner",
        "subject": {"type": "user", "id": str(owner_user_uuid)}
    }
])

# Create task in project
task = Task.objects.create(
    name="Implement API endpoint",
    project_id=project.id,
    workspace_id=workspace_id
)

# Link task to project (enables contingent permissions)
kessel.write_relationships([
    {
        "resource": {"type": "myapp/task", "id": str(task.id)},
        "relation": "project",
        "subject": {"type": "myapp/project", "id": str(project.id)}
    }
])
```

**Permission check**:

```python
# Check if project owner can write task (via contingent permission)
allowed = kessel.check(
    subject=f"user:{owner_user_uuid}",
    relation="project_write",
    resource={"type": "myapp/task", "id": str(task.id)}
)
# Returns true because:
# 1. User is owner of the project
# 2. Task has a "project" relation to the project
# 3. The @rbac.add_contingent_permission decorator grants project_write to project owners
```

**When to use**: Projects/tasks, folders/files, clusters/nodes, containers/items - any parent/child resource hierarchy.

---

### Pattern 5: Role-Based + Attribute-Based Access (Hybrid)

**Use case**: Combine role-based access with resource attributes (e.g., only edit your own resources).

**Schema**:

```spicedb
definition myapp/user_profile {
    relation workspace: rbac/workspace
    relation profile_owner: user
    relation hr_admin: user | group#member

    // Profile owner can always read/write their own profile
    permission read = profile_owner + hr_admin + workspace->admin
    permission write = profile_owner + hr_admin + workspace->admin

    // Only HR admins can delete profiles
    permission delete = hr_admin + workspace->admin
}
```

**Role definition** (HR Admin):

```json
{
  "name": "HR Administrator",
  "display_name": "HR Administrator",
  "description": "Can manage all user profiles",
  "permissions": [
    "myapp:user_profiles:read",
    "myapp:user_profiles:write",
    "myapp:user_profiles:delete"
  ]
}
```

**Setting up access**:

```python
# Create user profile
profile = UserProfile.objects.create(
    user_id=user_uuid,
    workspace_id=workspace_id
)

# Grant owner relation (attribute-based)
kessel.write_relationships([
    {
        "resource": {"type": "myapp/user_profile", "id": str(profile.id)},
        "relation": "profile_owner",
        "subject": {"type": "user", "id": str(user_uuid)}
    }
])

# Grant HR admin role binding (role-based)
binding_request = {
    "resource": {"id": workspace_id, "type": "workspace"},
    "subject": {"id": str(hr_admin_uuid), "type": "user"},
    "role": {"id": hr_admin_role_id}
}

requests.post(
    f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
    json={"requests": [binding_request]},
    headers={"x-rh-identity": identity_header}
)
```

**Permission checks**:

```python
# Regular user editing their own profile
allowed = kessel.check(
    subject=f"user:{user_uuid}",
    relation="write",
    resource={"type": "myapp/user_profile", "id": str(profile.id)}
)
# Returns true (profile_owner relation)

# HR admin editing someone else's profile
allowed = kessel.check(
    subject=f"user:{hr_admin_uuid}",
    relation="write",
    resource={"type": "myapp/user_profile", "id": str(other_profile.id)}
)
# Returns true (HR admin role binding + workspace->admin inheritance)

# Regular user trying to delete their own profile
allowed = kessel.check(
    subject=f"user:{user_uuid}",
    relation="delete",
    resource={"type": "myapp/user_profile", "id": str(profile.id)}
)
# Returns false (only hr_admin can delete)
```

**When to use**: User-owned resources (profiles, API keys, dashboards), resources requiring both ownership and admin override.

---

## Integrating Permission Checks

Once you have schema extensions, roles, and role bindings configured, integrate permission checks into your application endpoints.

### Permission Check via Kessel Relations API

The RBAC V2 API **does not provide a direct permission check endpoint**. Applications must integrate with **Kessel Relations API** (gRPC) for runtime checks.

**Kessel Relations API - Check endpoint:**

```protobuf
service CheckService {
  rpc Check(CheckRequest) returns (CheckResponse);
}

message CheckRequest {
  string subject = 1;        // "user:alice-uuid"
  string relation = 2;       // "read" or "write"
  ObjectReference resource = 3;  // {type: "myapp/host", id: "host-123"}
}

message CheckResponse {
  bool allowed = 1;
}
```

**Example: Python client using Kessel SDK**

```python
from kessel import KesselClient

kessel = KesselClient(
    host="kessel-relations.example.com:8080",
    auth_token=get_service_token()
)

# Check if user can read a host
allowed = kessel.check(
    subject=f"user:{user_uuid}",
    relation="read",
    resource={"type": "myapp/host", "id": str(host.id)}
)

if not allowed:
    raise Http404()  # Return 404, not 403, to prevent existence leakage
```

### Alternative: Query Role Bindings (RBAC V2 API)

If you cannot integrate with Kessel Relations API immediately, you can query role bindings and implement authorization logic:

**Step 1: Query user's role bindings**

```python
response = requests.get(
    f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings/by-subject/",
    params={"subject_id": user_uuid},
    headers={"x-rh-identity": identity_header}
)
role_bindings = response.json()['data']
```

**Step 2: Extract permissions and check**

```python
def has_permission(user_uuid, permission, resource_workspace_id):
    """Check if user has permission on resource's workspace."""
    role_bindings = get_role_bindings_for_user(user_uuid)

    for rb in role_bindings:
        # Check if binding is on the resource's workspace (or ancestor)
        if is_workspace_ancestor(rb['resource']['id'], resource_workspace_id):
            # Check if any role grants the permission
            for role in rb['roles']:
                if permission in role['permissions'] or '*:*:*' in role['permissions']:
                    return True
    return False
```

**Limitation**: This approach does NOT support schema-defined permission inheritance (`workspace->viewer`). Only Kessel Relations API evaluates the schema correctly.

### Protecting Endpoints

**Detail endpoint (single resource):**

```python
def get_host(request, host_id):
    host = Host.objects.get(id=host_id)

    # Check permission via Kessel
    allowed = kessel.check(
        subject=f"user:{request.user.uuid}",
        relation="read",
        resource={"type": "myapp/host", "id": str(host.id)}
    )

    if not allowed:
        raise Http404()  # Not 403 - prevents existence leakage

    return JsonResponse(serialize_host(host))
```

**List endpoint (multiple resources):**

```python
def list_hosts(request):
    # Option 1: Get accessible workspace IDs from RBAC API
    role_bindings = get_role_bindings_for_user(request.user.uuid)
    accessible_workspace_ids = extract_workspace_ids(role_bindings)

    # Filter hosts by accessible workspaces
    hosts = Host.objects.filter(workspace_id__in=accessible_workspace_ids)

    return JsonResponse([serialize_host(h) for h in hosts])
```

**Write endpoint (create/update/delete):**

```python
def update_host(request, host_id):
    host = Host.objects.get(id=host_id)

    # Check write permission
    allowed = kessel.check(
        subject=f"user:{request.user.uuid}",
        relation="write",
        resource={"type": "myapp/host", "id": str(host.id)}
    )

    if not allowed:
        raise Http404()

    # Proceed with update
    host.name = request.data['name']
    host.save()
    return JsonResponse(serialize_host(host))
```

### Caching Permission Checks

Permission checks add network latency (10-50ms per check). Cache aggressively:

```python
from django.core.cache import cache

def check_permission_cached(subject, relation, resource):
    cache_key = f"kessel:{subject}:{relation}:{resource['type']}:{resource['id']}"
    result = cache.get(cache_key)

    if result is None:
        result = kessel.check(subject, relation, resource)
        # Cache for 5 minutes
        cache.set(cache_key, result, timeout=300)

    return result
```

**Cache invalidation**: RBAC changes replicate to Kessel in 100-500ms. A 5-minute cache means up to 5 minutes of stale permissions. For most applications this is acceptable. For sensitive operations (admin functions, billing), use shorter TTL (60s) or skip caching.

---

## Common Pitfalls

### 1. Returning 403 Instead of 404

**Problem**: Returning 403 Forbidden for inaccessible resources reveals existence to unauthorized users.

```python
# WRONG: Reveals host exists
def get_host(request, host_id):
    host = Host.objects.get(id=host_id)
    if not check_permission(request, 'myapp:hosts:read', host):
        return HttpResponseForbidden()  # ❌ Reveals host exists
    return host

# CORRECT: Returns 404, prevents existence leakage
def get_host(request, host_id):
    host = Host.objects.get(id=host_id)
    if not check_permission(request, 'myapp:hosts:read', host):
        raise Http404()  # ✅ Hides existence
    return host
```

### 2. Forgetting to Filter List Endpoints

**Problem**: List endpoints return all resources, bypassing permission checks.

```python
# WRONG: Returns all hosts, ignoring permissions
def list_hosts(request):
    return Host.objects.all()  # ❌

# CORRECT: Filters by accessible workspaces
def list_hosts(request):
    accessible_workspaces = get_accessible_workspaces(user_uuid, 'myapp:hosts:read')
    return Host.objects.filter(workspace_id__in=accessible_workspaces)  # ✅
```

### 3. Not Handling RBAC API Failures

**Problem**: Network errors or RBAC downtime break your application.

```python
# WRONG: Exception bubbles up, breaks request
def check_permission(principal, permission, resource_id):
    return rbac_client.check_permission(principal, permission, resource_id)  # ❌

# CORRECT: Graceful degradation
def check_permission(principal, permission, resource_id):
    try:
        return kessel.check(principal, permission, resource_id)
    except requests.exceptions.Timeout:
        logger.error("Kessel timeout, denying access")
        metrics.increment('kessel.timeout')
        return False  # Fail closed
    except Exception as e:
        logger.error(f"Kessel error: {e}", exc_info=True)
        metrics.increment('kessel.error')
        return False  # Fail closed for security
```

### 4. Caching Too Aggressively

**Problem**: Long cache TTLs mean permission changes don't take effect promptly.

```python
# RISKY: 1-hour cache means revoked access persists for up to 1 hour
cache.set(cache_key, result, timeout=3600)  # ⚠️

# SAFER: 5-minute cache balances performance and staleness
cache.set(cache_key, result, timeout=300)  # ✅
```

**Recommendation**: Cache for 5 minutes by default. For sensitive resources (billing, admin functions), cache for 1 minute or skip caching entirely.

### 5. Hardcoding Workspace IDs

**Problem**: Workspace UUIDs differ across environments (dev, staging, prod).

```python
# WRONG: Hardcoded UUID breaks in other environments
ADMIN_WORKSPACE_ID = "12345678-1234-1234-1234-123456789012"  # ❌

# CORRECT: Fetch dynamically or use environment variable
def get_admin_workspace_id():
    return os.getenv('ADMIN_WORKSPACE_ID') or fetch_from_rbac()  # ✅
```

### 6. Not Testing Cross-Tenant Isolation

**Problem**: Missing `org_id` filter allows cross-tenant data leakage.

```python
# Test that users from org A cannot access org B's resources
def test_cross_tenant_isolation(self):
    # Create host for org A
    org_a_host = Host.objects.create(name="host-a", tenant=org_a)

    # Authenticate as user from org B
    self.client.force_authenticate(user=org_b_user)

    # Attempt to access org A's host
    response = self.client.get(f'/api/hosts/{org_a_host.id}/')

    # Should return 404 (or 403), never 200
    self.assertIn(response.status_code, [403, 404])
```

### 7. Overloading Permission Granularity

**Problem**: Too many fine-grained permissions make roles unusable.

```python
# TOO GRANULAR: 100+ permissions for one resource type
myapp:host:read_name
myapp:host:read_ip
myapp:host:read_os
myapp:host:write_name
myapp:host:write_ip
# ... 95 more

# BETTER: Operation-level permissions
myapp:hosts:read   # Read all host fields
myapp:hosts:write  # Write all host fields
```

**Guideline**: Aim for 3-10 permissions per resource type. Common pattern: `read`, `write`, `delete`, plus occasional special ops like `execute`, `export`.

### 8. Forgetting Workspace Relations in Schema

**Problem**: Resources don't inherit workspace permissions.

```spicedb
// WRONG: No workspace relation
definition myapp/host {
    relation viewer: user | group#member
    permission read = viewer  // ❌ No inheritance
}

// CORRECT: Include workspace relation
definition myapp/host {
    relation workspace: rbac/workspace
    relation viewer: user | group#member
    permission read = viewer + workspace->viewer  // ✅ Inherits from workspace
}
```

### 9. Not Accounting for Replication Delay

**Problem**: Permission checks immediately after creating role bindings may fail.

```python
# Create role binding
rbac_client.batch_create_role_bindings([binding])

# Immediately check permission (may fail - replication takes 100-500ms)
allowed = kessel.check(user, permission, resource)  # ❌ Might return false

# Better: Accept eventual consistency
rbac_client.batch_create_role_bindings([binding])
# Permission will work within 500ms - acceptable for most UX flows
# Users can refresh if they get 403 initially
```

**For tests only**: Add `time.sleep(1)` after creating bindings to wait for replication.

---

## Resources

### RBAC API Documentation

- **V2 API Spec**: [docs/source/specs/v2/openapi.yaml](source/specs/v2/openapi.yaml)
- **TypeSpec Source**: [docs/source/specs/typespec/main.tsp](source/specs/typespec/main.tsp)
- **Role Bindings Deep Dive**: [role_bindings.md](role_bindings.md)

### Kessel Documentation

#### Getting Started
- **Quick Start Tutorial** (~30 min): [https://project-kessel.github.io/docs/start-here/getting-started/](https://project-kessel.github.io/docs/start-here/getting-started/)
- **Understanding Kessel**: [https://project-kessel.github.io/docs/start-here/understanding-kessel/](https://project-kessel.github.io/docs/start-here/understanding-kessel/)
- **Run with Docker**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/run-with-docker/](https://project-kessel.github.io/docs/building-with-kessel/how-to/run-with-docker/)

#### Concepts
- **RBAC Concepts**: [https://project-kessel.github.io/docs/building-with-kessel/concepts/rbac/](https://project-kessel.github.io/docs/building-with-kessel/concepts/rbac/)
- **Consistency Model**: [https://project-kessel.github.io/docs/building-with-kessel/concepts/consistency/](https://project-kessel.github.io/docs/building-with-kessel/concepts/consistency/)

#### How-To Guides
- **Design Permissions**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/design-permissions/](https://project-kessel.github.io/docs/building-with-kessel/how-to/design-permissions/)
- **Protect Endpoints**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/protect-endpoint/](https://project-kessel.github.io/docs/building-with-kessel/how-to/protect-endpoint/)
- **RBAC Management**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/rbac/](https://project-kessel.github.io/docs/building-with-kessel/how-to/rbac/)
- **SDK Authentication**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/authenticate-with-sdks/](https://project-kessel.github.io/docs/building-with-kessel/how-to/authenticate-with-sdks/)

#### API References
- **gRPC Inventory API**: [https://buf.build/project-kessel/inventory-api/docs/main:kessel.inventory.v1beta2](https://buf.build/project-kessel/inventory-api/docs/main:kessel.inventory.v1beta2)
- **HTTP API Reference**: [https://project-kessel.github.io/docs/building-with-kessel/reference/http-api/](https://project-kessel.github.io/docs/building-with-kessel/reference/http-api/)
- **Error Codes**: [https://project-kessel.github.io/docs/building-with-kessel/reference/error-codes/](https://project-kessel.github.io/docs/building-with-kessel/reference/error-codes/)

### SpiceDB Resources

- **SpiceDB Playground**: [https://play.authzed.com/](https://play.authzed.com/) - Interactive schema testing
- **SpiceDB Documentation**: [https://authzed.com/docs](https://authzed.com/docs)
- **Schema Language Guide**: [https://authzed.com/docs/reference/schema-lang](https://authzed.com/docs/reference/schema-lang)

### insights-rbac Documentation

- **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md)
- **Security Guidelines**: [security-guidelines.md](security-guidelines.md)
- **API Contracts**: [api-contracts-guidelines.md](api-contracts-guidelines.md)
- **Integration Guidelines**: [integration-guidelines.md](integration-guidelines.md)

### Support

- **Slack**: `#forum-consoledot-rbac` (internal Red Hat)
- **Email**: rbac-team@redhat.com
- **Issues**: File bugs/questions in the insights-rbac GitHub repo

---

## Appendix: Migration Guide

**Note**: This section is for teams migrating existing authorization systems to RBAC. If you're implementing authorization for the first time, you can skip this section and use the patterns described earlier in the guide.

### When to Migrate vs Start Fresh

- **Migrate**: You have an existing authorization system with groups, roles, or permissions that users depend on
- **Start fresh**: You're adding authorization for the first time, or your current system is simple (admin vs non-admin)

### Migration Pattern: Existing Workspace/Grouping Abstractions

**Scenario**: Your application already has workspace, project, folder, or team concepts that group resources.

**Goal**: Map existing groups to RBAC workspaces and convert memberships to role bindings without disrupting user access.

#### Analysis Phase

1. **Map existing abstractions to RBAC workspaces**:
   - If your groupings are hierarchical (folders in folders), they align naturally with RBAC workspaces
   - If they're flat (teams, projects), you can map each to a workspace under a parent

2. **Identify existing permission checks**:
   ```python
   # Example: Your current code
   if not user.can_edit(project):
       return 403
   ```

3. **Map to RBAC permissions**:
   ```
   can_edit(project) → myapp:projects:write permission check
   can_view(project) → myapp:projects:read permission check
   ```

4. **Audit permission granularity**:
   - Are your current checks too coarse? (e.g., only "admin" vs "viewer")
   - Are they too fine-grained? (e.g., permissions per field)
   - RBAC permissions work best at operation level: `read`, `write`, `delete`

#### Implementation Strategy

**Option 1: Map existing groups to RBAC workspaces (recommended)**

Best when your existing groups have clear ownership boundaries.

```python
# Before: Custom authorization table
class Project(models.Model):
    name = models.CharField(max_length=255)
    members = models.ManyToManyField(User, through='ProjectMembership')

class ProjectMembership(models.Model):
    project = models.ForeignKey(Project)
    user = models.ForeignKey(User)
    role = models.CharField(choices=['admin', 'editor', 'viewer'])

# After: Workspace-based via RBAC V2
class Project(models.Model):
    name = models.CharField(max_length=255)
    workspace_id = models.UUIDField()  # Links to RBAC workspace

# Membership is managed via RBAC role bindings instead
```

**Migration steps:**

**Step 1: Define schema extension**

Work with RBAC team to register your schema:

```spicedb
definition myapp/project {
    relation workspace: workspace
    relation viewer: user | group#member
    relation editor: user | group#member
    relation admin: user | group#member

    permission read = viewer + editor + admin + workspace->viewer + workspace->editor + workspace->admin
    permission write = editor + admin + workspace->editor + workspace->admin
    permission delete = admin + workspace->admin
}
```

**Step 2: Create custom roles (V2 API)**

```python
import requests

# Create roles for your application
roles = [
    {
        "name": "MyApp Viewer",
        "display_name": "MyApp Viewer",
        "description": "Read-only access to projects",
        "permissions": ["myapp:projects:read"]
    },
    {
        "name": "MyApp Editor",
        "display_name": "MyApp Editor",
        "description": "View and edit projects",
        "permissions": ["myapp:projects:read", "myapp:projects:write"]
    },
    {
        "name": "MyApp Admin",
        "display_name": "MyApp Admin",
        "description": "Full access to projects",
        "permissions": ["myapp:projects:*"]
    }
]

role_id_map = {}
for role_def in roles:
    response = requests.post(
        f"{RBAC_BASE_URL}/api/rbac/v2/roles/",
        json=role_def,
        headers={"x-rh-identity": identity_header}
    )
    role = response.json()
    role_id_map[role_def['name']] = role['id']
```

**Step 3: Create workspaces for existing projects**

```python
for project in Project.objects.all():
    response = requests.post(
        f"{RBAC_BASE_URL}/api/rbac/v2/workspaces/",
        json={
            "name": project.name,
            "description": f"Workspace for {project.name}",
            "parent_id": get_root_workspace_id()  # Link to org root
        },
        headers={"x-rh-identity": identity_header}
    )
    workspace = response.json()
    project.workspace_id = workspace['id']
    project.save()
```

**Step 4: Migrate memberships to role bindings (V2 batch API)**

```python
# Map old roles to new role UUIDs
ROLE_MAPPING = {
    'admin': role_id_map['MyApp Admin'],
    'editor': role_id_map['MyApp Editor'],
    'viewer': role_id_map['MyApp Viewer']
}

# Batch create role bindings (max 100 per request)
binding_requests = []

for membership in ProjectMembership.objects.all():
    binding_requests.append({
        "resource": {
            "id": str(membership.project.workspace_id),
            "type": "workspace"
        },
        "subject": {
            "id": str(membership.user.uuid),
            "type": "user"
        },
        "role": {
            "id": ROLE_MAPPING[membership.role]
        }
    })

    # Batch in groups of 100
    if len(binding_requests) >= 100:
        requests.post(
            f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
            json={"requests": binding_requests},
            headers={"x-rh-identity": identity_header}
        )
        binding_requests = []

# Create remaining bindings
if binding_requests:
    requests.post(
        f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
        json={"requests": binding_requests},
        headers={"x-rh-identity": identity_header}
    )
```

**Step 5: Replace permission checks with Kessel Relations API**

See [Integrating Permission Checks](#integrating-permission-checks) for implementation details.

**Option 2: Sync to RBAC asynchronously**

Best when you need to maintain your existing model during transition.

```python
# Keep your existing tables, sync changes to RBAC V2
class Project(models.Model):
    name = models.CharField(max_length=255)
    workspace_id = models.UUIDField(null=True, blank=True)  # Populated after sync

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Async task: sync to RBAC
        sync_project_to_rbac.delay(self.id)

@celery_app.task
def sync_project_to_rbac(project_id):
    project = Project.objects.get(id=project_id)

    if not project.workspace_id:
        # Create workspace via V2 API
        response = requests.post(
            f"{RBAC_BASE_URL}/api/rbac/v2/workspaces/",
            json={
                "name": project.name,
                "description": f"Auto-synced workspace for {project.name}",
                "parent_id": get_root_workspace_id()
            },
            headers={"x-rh-identity": get_service_identity()}
        )
        workspace = response.json()
        project.workspace_id = workspace['id']
        project.save(update_fields=['workspace_id'])

    # Sync memberships → role bindings (V2 batch API)
    existing_bindings = get_existing_bindings(project.workspace_id)
    role_binding_requests = []

    for membership in project.memberships.all():
        # Only create if binding doesn't exist
        binding_key = (str(membership.user.uuid), ROLE_MAPPING[membership.role])
        if binding_key not in existing_bindings:
            role_binding_requests.append({
                "resource": {"id": str(project.workspace_id), "type": "workspace"},
                "subject": {"id": str(membership.user.uuid), "type": "user"},
                "role": {"id": ROLE_MAPPING[membership.role]}
            })

    # Batch create in groups of 100
    for i in range(0, len(role_binding_requests), 100):
        batch = role_binding_requests[i:i+100]
        requests.post(
            f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
            json={"requests": batch},
            headers={"x-rh-identity": get_service_identity()}
        )

def get_existing_bindings(workspace_id):
    """Query existing bindings to avoid duplicates."""
    response = requests.get(
        f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings/by-resource/",
        params={"resource_id": workspace_id, "resource_type": "workspace"},
        headers={"x-rh-identity": get_service_identity()}
    )
    bindings = response.json().get('data', [])
    # Return set of (subject_id, role_id) tuples
    return {(b['subject']['id'], b['role']['id']) for b in bindings}
```

#### Permission Check Migration

**Note**: The examples below use a conceptual `rbac_client` wrapper. The RBAC v2 API does not provide direct permission check endpoints. Applications must either:
1. Integrate with **Kessel Relations API** (gRPC) for runtime permission checks (recommended)
2. Query role bindings via `/api/rbac/v2/role-bindings/by-subject/` and implement authorization logic

See the [Sample Client Libraries](#sample-client-libraries) section for implementation details.

```python
# Before: Custom permission check
def get_project(request, project_id):
    project = Project.objects.get(id=project_id)
    if not request.user.has_project_access(project, 'read'):
        raise PermissionDenied()
    return project

# After: RBAC permission check (conceptual - requires Kessel Relations API integration)
def get_project(request, project_id):
    project = Project.objects.get(id=project_id)

    # Check permission via Kessel Relations API or role binding query
    has_access = rbac_client.check_permission(
        principal=request.user.username,
        permission="myapp:projects:read",
        resource_id=str(project.workspace_id)
    )

    if not has_access:
        raise PermissionDenied()
    return project
```

#### List Filtering Migration

For list endpoints, you need to filter results to only resources the user can access.

**Note**: These examples use a conceptual `rbac_client.list_accessible_resources()` method. You must implement this by querying `/api/rbac/v2/role-bindings/by-subject/?subject={principal}` to get the user's role bindings and extracting workspace IDs from the results. See the [Sample Client Libraries](#sample-client-libraries) section for implementation.

```python
# Before: Custom filtering
def list_projects(request):
    projects = Project.objects.filter(
        memberships__user=request.user
    )
    return projects

# After: RBAC filtering (Option 1 - fetch accessible workspace IDs)
def list_projects(request):
    # Get all workspace IDs user can access with read permission
    # Implementation: queries /api/rbac/v2/role-bindings/by-subject/
    accessible_workspaces = rbac_client.list_accessible_resources(
        principal=request.user.username,
        permission="myapp:projects:read"
    )
    workspace_ids = [r['id'] for r in accessible_workspaces]

    # Filter projects by workspace
    projects = Project.objects.filter(
        workspace_id__in=workspace_ids
    )
    return projects

# After: RBAC filtering (Option 2 - check each via Kessel, for small lists)
def list_projects(request):
    projects = Project.objects.all()

    # Filter in Python (only viable for small result sets)
    # Each check requires Kessel Relations API call
    accessible = []
    for project in projects:
        if rbac_client.check_permission(
            principal=request.user.username,
            permission="myapp:projects:read",
            resource_id=str(project.workspace_id)
        ):
            accessible.append(project)
    return accessible
```

**Performance note**: For list endpoints with many results, prefer Option 1 (fetch accessible IDs upfront) to minimize API calls.

---

### Pattern B: No Existing Authorization Model

**Scenario**: Your application currently has no authorization or uses simple admin/non-admin checks.

This is the **simplest migration path** — you're adding authorization from scratch using RBAC V2 and schema extensions.

#### Analysis Phase

**Step 1: Define your resources and operations**

What objects does your application manage, and what operations can users perform?

**Example application:**

```
Resource: hosts
Operations: read, write, delete

Resource: reports
Operations: read, write, execute, delete

Resource: configurations
Operations: read, write
```

**Step 2: Design schema extension**

Model your resources and permissions in SpiceDB schema language:

```spicedb
definition myapp/host {
    relation workspace: workspace
    relation viewer: user | group#member
    relation editor: user | group#member

    permission read = viewer + editor + workspace->viewer + workspace->editor
    permission write = editor + workspace->editor
    permission delete = editor + workspace->editor
}

definition myapp/report {
    relation workspace: workspace
    relation viewer: user | group#member
    relation operator: user | group#member
    relation editor: user | group#member

    permission read = viewer + operator + editor + workspace->viewer + workspace->operator + workspace->editor
    permission write = editor + workspace->editor
    permission execute = operator + editor + workspace->operator + workspace->editor
    permission delete = editor + workspace->editor
}

definition myapp/configuration {
    relation workspace: workspace
    relation viewer: user | group#member
    relation admin: user | group#member

    permission read = viewer + admin + workspace->viewer + workspace->admin
    permission write = admin + workspace->admin
}
```

**Key design decisions:**

- All resources have `relation workspace: workspace` for inheritance
- Permissions include `workspace->viewer` to inherit from workspace role bindings
- Different resource types have different relation sets (reports have `operator`, configs have `admin`)

**Step 3: Define permission strings**

Map schema permissions to API permission strings:

```
myapp:hosts:read       → myapp/host#read
myapp:hosts:write      → myapp/host#write
myapp:hosts:delete     → myapp/host#delete
myapp:reports:read     → myapp/report#read
myapp:reports:write    → myapp/report#write
myapp:reports:execute  → myapp/report#execute
myapp:reports:delete   → myapp/report#delete
```

**Step 4: Determine workspace mapping**

- **Flat model**: All resources in the org's default workspace
- **Grouped model**: Resources grouped by team/environment/application (recommended)

#### Implementation Strategy

**Step 1: Register schema extension**

Work with RBAC team to deploy your schema extension to Kessel Relations API. Provide:

- Schema definition (SpiceDB format)
- Permission mapping documentation
- Example resources and expected permission results

**Step 2: Create custom roles (V2 API)**

Define roles that grant your application's permissions:

```python
import requests

RBAC_BASE_URL = "https://console.redhat.com"

roles_to_create = [
    {
        "name": "MyApp Viewer",
        "display_name": "MyApp Viewer",
        "description": "Read-only access to all MyApp resources",
        "permissions": [
            "myapp:hosts:read",
            "myapp:reports:read",
            "myapp:configurations:read"
        ]
    },
    {
        "name": "MyApp Operator",
        "display_name": "MyApp Operator",
        "description": "Can view and execute reports",
        "permissions": [
            "myapp:hosts:read",
            "myapp:reports:read",
            "myapp:reports:execute",
            "myapp:configurations:read"
        ]
    },
    {
        "name": "MyApp Editor",
        "display_name": "MyApp Editor",
        "description": "Can view and edit hosts and reports",
        "permissions": [
            "myapp:hosts:read",
            "myapp:hosts:write",
            "myapp:reports:read",
            "myapp:reports:write",
            "myapp:reports:execute",
            "myapp:configurations:read"
        ]
    },
    {
        "name": "MyApp Admin",
        "display_name": "MyApp Admin",
        "description": "Full administrative access to MyApp",
        "permissions": [
            "myapp:*:*"
        ]
    }
]

created_roles = {}
for role_def in roles_to_create:
    response = requests.post(
        f"{RBAC_BASE_URL}/api/rbac/v2/roles/",
        json=role_def,
        headers={"x-rh-identity": identity_header}
    )
    role = response.json()
    created_roles[role['name']] = role['id']
    print(f"Created role: {role['name']} ({role['id']})")
```

**Step 3: Decide workspace strategy**

**Option A: Use default workspace (simplest)**

All resources use the organization's root workspace.

```python
# All resources use the tenant's default workspace
class Host(models.Model):
    name = models.CharField(max_length=255)
    tenant = models.ForeignKey(Tenant)

    @property
    def workspace_id(self):
        # All hosts use default workspace
        return self.tenant.default_workspace_id
```

**Option B: Create workspace hierarchy (recommended)**

Resources grouped by environment, team, or application area.

```python
# Resources grouped by environment
class Environment(models.Model):
    name = models.CharField(max_length=255)  # "Production", "Staging"
    tenant = models.ForeignKey(Tenant)
    workspace_id = models.UUIDField()  # RBAC workspace

    def save(self, *args, **kwargs):
        if not self.workspace_id:
            # Create workspace via V2 API
            response = requests.post(
                f"{RBAC_BASE_URL}/api/rbac/v2/workspaces/",
                json={
                    "name": self.name,
                    "description": f"{self.name} environment workspace",
                    "parent_id": self.tenant.root_workspace_id
                },
                headers={"x-rh-identity": get_service_identity()}
            )
            self.workspace_id = response.json()['id']
        super().save(*args, **kwargs)

class Host(models.Model):
    name = models.CharField(max_length=255)
    environment = models.ForeignKey(Environment)

    @property
    def workspace_id(self):
        return self.environment.workspace_id
```

**Step 4: Integrate Kessel Relations API for permission checks**

Install Kessel SDK and integrate permission checks:

```python
from kessel import KesselClient
from django.http import Http404, JsonResponse

# Initialize Kessel client
kessel = KesselClient(
    host=settings.KESSEL_RELATIONS_HOST,
    auth_token=get_service_token()
)

def get_host(request, host_id):
    """Detail endpoint - check permission via Kessel."""
    host = Host.objects.get(id=host_id)

    # Check permission via Kessel Relations API
    allowed = kessel.check(
        subject=f"user:{request.user.uuid}",
        relation="read",  # Maps to myapp/host#read in schema
        resource={"type": "myapp/host", "id": str(host.id)}
    )

    if not allowed:
        # Return 404 to prevent existence leakage
        raise Http404()

    return JsonResponse({
        "id": str(host.id),
        "name": host.name,
        "environment": host.environment.name
    })

def update_host(request, host_id):
    """Update endpoint - check write permission."""
    host = Host.objects.get(id=host_id)

    # Check write permission
    allowed = kessel.check(
        subject=f"user:{request.user.uuid}",
        relation="write",
        resource={"type": "myapp/host", "id": str(host.id)}
    )

    if not allowed:
        raise Http404()

    # Proceed with update
    host.name = request.data.get('name', host.name)
    host.save()

    return JsonResponse({
        "id": str(host.id),
        "name": host.name
    })

def delete_host(request, host_id):
    """Delete endpoint - check delete permission."""
    host = Host.objects.get(id=host_id)

    allowed = kessel.check(
        subject=f"user:{request.user.uuid}",
        relation="delete",
        resource={"type": "myapp/host", "id": str(host.id)}
    )

    if not allowed:
        raise Http404()

    host.delete()
    return JsonResponse({"status": "deleted"})
```

**Step 5: Filter list endpoints**

For list endpoints, query the user's role bindings to get accessible workspace IDs:

```python
def list_hosts(request):
    """List endpoint - filter by accessible workspaces."""

    # Query user's role bindings via RBAC V2 API
    response = requests.get(
        f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings/by-subject/",
        params={"subject_id": str(request.user.uuid)},
        headers={"x-rh-identity": request.META['HTTP_X_RH_IDENTITY']}
    )
    role_bindings = response.json().get('data', [])

    # Extract workspace IDs where user has read permission
    accessible_workspace_ids = []
    for rb in role_bindings:
        # Check if any role grants myapp:hosts:read
        for role in rb['roles']:
            if 'myapp:hosts:read' in role['permissions'] or 'myapp:*:*' in role['permissions']:
                accessible_workspace_ids.append(rb['resource']['id'])
                break

    # Filter hosts by accessible workspaces
    hosts = Host.objects.filter(
        environment__workspace_id__in=accessible_workspace_ids
    )

    return JsonResponse({
        "data": [
            {"id": str(h.id), "name": h.name, "environment": h.environment.name}
            for h in hosts
        ]
    })
```

**Performance optimization**: Cache accessible workspace IDs for the request:

```python
from functools import lru_cache

@lru_cache(maxsize=128)
def get_accessible_workspaces_for_user(user_uuid, permission):
    """Get workspace IDs where user has permission (cached)."""
    response = requests.get(
        f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings/by-subject/",
        params={"subject_id": str(user_uuid)},
        headers={"x-rh-identity": get_service_identity()}
    )
    role_bindings = response.json().get('data', [])

    accessible = []
    for rb in role_bindings:
        for role in rb['roles']:
            if permission in role['permissions'] or '*:*:*' in role['permissions']:
                accessible.append(rb['resource']['id'])
                break

    return accessible

def list_hosts(request):
    workspace_ids = get_accessible_workspaces_for_user(
        request.user.uuid,
        'myapp:hosts:read'
    )
    hosts = Host.objects.filter(environment__workspace_id__in=workspace_ids)
    return JsonResponse({"data": [serialize_host(h) for h in hosts]})
```

**Step 6: Grant initial access via role bindings**

Create role bindings to grant users access. For initial setup, grant broad access at the root workspace:

```python
# Grant org admin full access at root workspace
binding_requests = [
    {
        "resource": {
            "id": tenant.root_workspace_id,
            "type": "workspace"
        },
        "subject": {
            "id": str(admin_user.uuid),
            "type": "user"
        },
        "role": {
            "id": created_roles['MyApp Admin']
        }
    }
]

response = requests.post(
    f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
    json={"requests": binding_requests},
    headers={"x-rh-identity": get_service_identity()}
)

print(f"Created {len(response.json()['role_bindings'])} role bindings")
```

**Step 7: Test permission checks**

Verify that permission checks work correctly:

```python
# Test: Admin can read hosts
allowed = kessel.check(
    subject=f"user:{admin_user.uuid}",
    relation="read",
    resource={"type": "myapp/host", "id": str(host.id)}
)
assert allowed == True

# Test: Regular user cannot read hosts (no role binding)
allowed = kessel.check(
    subject=f"user:{regular_user.uuid}",
    relation="read",
    resource={"type": "myapp/host", "id": str(host.id)}
)
assert allowed == False
```

**Step 8: Document for users**

Update your documentation to explain:

- **How to grant users access**: Org admins create role bindings via RBAC UI or API
- **Available roles**: `MyApp Viewer`, `MyApp Operator`, `MyApp Editor`, `MyApp Admin`
- **Workspace inheritance**: Permissions granted at parent workspaces automatically apply to child workspaces
- **Permission model**: What each role can do (read vs write vs execute vs admin)

**Example user documentation:**

```markdown
## Granting Access to MyApp

To grant a user access to MyApp resources:

1. Navigate to **Settings > Access Management** in console.redhat.com
2. Select the workspace where you want to grant access
3. Click **Add role binding**
4. Choose the user or group
5. Select a role:
   - **MyApp Viewer**: Read-only access to all resources
   - **MyApp Operator**: Can view and execute reports
   - **MyApp Editor**: Can view, create, and edit resources
   - **MyApp Admin**: Full administrative access
6. Click **Create**

The user will have access to all MyApp resources in that workspace and any child workspaces.
```

---

## Step-by-Step Migration Checklist

### Phase 1: Planning (1-2 weeks)

- [ ] **Define permissions**: List all operations users perform, map to `app:resource:operation` format
- [ ] **Audit current authorization**: Document existing permission checks, identify patterns
- [ ] **Choose migration pattern**: Pattern A (existing groups) or Pattern B (new authorization)
- [ ] **Design workspace mapping**: Decide how your resources map to workspaces
- [ ] **Identify pilot scope**: Choose a small, non-critical feature to migrate first
- [ ] **Set up RBAC client**: Install/configure HTTP client for RBAC API
- [ ] **Get RBAC API credentials**: Obtain PSK or configure service identity

### Phase 2: Development (2-4 weeks)

- [ ] **Design and register schema extension**:
  - Define resource types and relations in SpiceDB schema language
  - Document permission mapping (API operations → schema permissions)
  - Submit schema to RBAC team for review and deployment
  - Validate schema in dev environment

- [ ] **Create custom roles via V2 API**:
  ```python
  # Create application-specific roles
  roles = [
      {
          "name": "MyApp Viewer",
          "display_name": "MyApp Viewer",
          "description": "Read-only access",
          "permissions": ["myapp:*:read"]
      },
      {
          "name": "MyApp Editor",
          "display_name": "MyApp Editor",
          "description": "Read and write access",
          "permissions": ["myapp:*:read", "myapp:*:write"]
      }
  ]

  for role_def in roles:
      response = requests.post(
          f"{RBAC_BASE_URL}/api/rbac/v2/roles/",
          json=role_def,
          headers={"x-rh-identity": identity_header}
      )
  ```

- [ ] **Integrate Kessel Relations API client**:
  ```python
  from kessel import KesselClient

  kessel = KesselClient(
      host=settings.KESSEL_RELATIONS_HOST,
      auth_token=get_service_token()
  )

  def check_permission(user_uuid, relation, resource_type, resource_id):
      """Check permission via Kessel Relations API."""
      return kessel.check(
          subject=f"user:{user_uuid}",
          relation=relation,  # "read", "write", "delete"
          resource={"type": resource_type, "id": resource_id}
      )
  ```

- [ ] **Implement RBAC V2 client utilities**:
  ```python
  class RBACV2Client:
      def create_workspace(self, name, parent_id, description=None):
          """Create workspace via POST /api/rbac/v2/workspaces/"""
          return requests.post(
              f"{RBAC_BASE_URL}/api/rbac/v2/workspaces/",
              json={"name": name, "description": description, "parent_id": parent_id},
              headers={"x-rh-identity": self.identity_header}
          ).json()

      def batch_create_role_bindings(self, binding_requests):
          """
          Create role bindings via POST /api/rbac/v2/role-bindings:batchCreate/
          Maximum 100 bindings per request.
          """
          return requests.post(
              f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
              json={"requests": binding_requests},
              headers={"x-rh-identity": self.identity_header}
          ).json()

      def get_user_role_bindings(self, user_uuid):
          """Get all role bindings for a user."""
          return requests.get(
              f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings/by-subject/",
              params={"subject_id": user_uuid},
              headers={"x-rh-identity": self.identity_header}
          ).json()
  ```

- [ ] **Add workspace tracking**:
  - Add `workspace_id` fields to models (if using workspace mapping)
  - Create migration to add column
  - Implement workspace creation logic

- [ ] **Implement dual-running** (see [Dual-Running Strategies](#dual-running-strategies)):
  - Add feature flag: `USE_RBAC_AUTHORIZATION`
  - Implement permission check wrapper that calls both old and new
  - Log discrepancies for investigation

- [ ] **Migrate permission checks**:
  - Replace custom authorization with RBAC calls
  - Update detail endpoints (single resource checks)
  - Update list endpoints (bulk filtering)

- [ ] **Handle edge cases**:
  - Service-to-service calls (use service account identity)
  - Background jobs (use system service account)
  - Public/unauthenticated endpoints (skip RBAC check)

### Phase 3: Testing (1-2 weeks)

- [ ] **Unit tests**: Mock RBAC client, test permission check logic
- [ ] **Integration tests**: Test against real RBAC service in dev environment
- [ ] **Performance testing**: Measure latency impact of RBAC calls
- [ ] **Load testing**: Verify RBAC can handle your expected traffic
- [ ] **Security testing**: Attempt to bypass permission checks, test privilege escalation
- [ ] **User acceptance testing**: Have real users test with role bindings

### Phase 4: Data Migration (1 week)

- [ ] **Migrate workspaces** (if Pattern A):
  ```python
  for project in Project.objects.all():
      workspace = rbac_client.create_workspace(
          name=project.name,
          parent_id=tenant.root_workspace_id
      )
      project.workspace_id = workspace['id']
      project.save()
  ```

- [ ] **Migrate memberships to role bindings (V2 batch API)**:
  ```python
  # Map old roles to new V2 role UUIDs
  ROLE_MAPPING = {
      'admin': myapp_admin_role_id,
      'editor': myapp_editor_role_id,
      'viewer': myapp_viewer_role_id
  }

  # Batch create role bindings (V2 API requires batch format, max 100)
  role_binding_requests = []

  for membership in ProjectMembership.objects.all():
      role_binding_requests.append({
          "resource": {
              "id": str(membership.project.workspace_id),
              "type": "workspace"
          },
          "subject": {
              "id": str(membership.user.uuid),
              "type": "user"
          },
          "role": {
              "id": ROLE_MAPPING[membership.role]
          }
      })

      # Batch in groups of 100 (API limit)
      if len(role_binding_requests) >= 100:
          response = requests.post(
              f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
              json={"requests": role_binding_requests},
              headers={"x-rh-identity": get_service_identity()}
          )
          print(f"Created {len(response.json()['role_bindings'])} bindings")
          role_binding_requests = []

  # Create remaining bindings
  if role_binding_requests:
      response = requests.post(
          f"{RBAC_BASE_URL}/api/rbac/v2/role-bindings:batchCreate/",
          json={"requests": role_binding_requests},
          headers={"x-rh-identity": get_service_identity()}
      )
      print(f"Created {len(response.json()['role_bindings'])} bindings")
  ```

- [ ] **Verify data migration**: Compare old and new authorization results
- [ ] **Backfill missing data**: Handle any records that failed migration

### Phase 5: Deployment (1-2 weeks)

- [ ] **Deploy with dual-running enabled**: Deploy to staging/production with feature flag OFF
- [ ] **Enable dual-running in production**: Turn on feature flag, compare results
- [ ] **Monitor for discrepancies**: Alert on any old/new mismatches
- [ ] **Fix discrepancies**: Investigate and resolve any authorization differences
- [ ] **Gradual rollout**: Enable RBAC for increasing % of traffic (10% → 50% → 100%)
- [ ] **Full cutover**: Remove old authorization code, remove feature flag
- [ ] **Deprecate old model**: (Pattern A only) Drop old membership tables after grace period

### Phase 6: Validation (1 week)

- [ ] **Security audit**: Verify no permission bypasses exist
- [ ] **Performance monitoring**: Check p50/p95/p99 latency, error rates
- [ ] **User feedback**: Confirm users can access expected resources
- [ ] **Documentation update**: Update API docs, user guides, runbooks

---

## Dual-Running Strategies

Dual-running allows you to validate RBAC integration without risking production access control. Both old and new authorization systems run in parallel, with the old system enforcing access while the new one is validated.

### Strategy 1: Check Both, Enforce Old (Recommended)

**Best for**: Initial rollout, high-risk applications

```python
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

def check_permission(request, permission, resource):
    """Check permission using dual-running strategy."""

    # Old authorization (enforced)
    old_result = legacy_permission_check(request.user, resource)

    if not settings.RBAC_DUAL_RUNNING_ENABLED:
        # Feature flag off: use old system only
        return old_result

    # New authorization (validated, not enforced)
    try:
        new_result = rbac_client.check_permission(
            principal=request.user.username,
            permission=permission,
            resource_id=str(resource.workspace_id)
        )
    except Exception as e:
        # RBAC API failure: log and fall back to old
        logger.error(f"RBAC check failed: {e}", exc_info=True)
        metrics.increment('rbac.check.error')
        new_result = None

    # Compare results
    if new_result is not None and old_result != new_result:
        logger.warning(
            f"RBAC mismatch: user={request.user.username} "
            f"permission={permission} resource={resource.id} "
            f"old={old_result} new={new_result}"
        )
        metrics.increment('rbac.check.mismatch')

    # Enforce old result
    return old_result
```

**Monitoring**:
- Track `rbac.check.mismatch` metric — should trend toward zero
- Alert on sustained mismatch rate > 1%
- Review logs daily to identify patterns

### Strategy 2: Check Both, Enforce New

**Best for**: After validation period, before removing old system

```python
def check_permission(request, permission, resource):
    """Check permission, enforcing RBAC."""

    # New authorization (enforced)
    new_result = rbac_client.check_permission(
        principal=request.user.username,
        permission=permission,
        resource_id=str(resource.workspace_id)
    )

    if settings.RBAC_DUAL_RUNNING_ENABLED:
        # Also check old system for comparison
        old_result = legacy_permission_check(request.user, resource)

        if old_result != new_result:
            logger.warning(
                f"RBAC enforcement changed access: user={request.user.username} "
                f"permission={permission} resource={resource.id} "
                f"old={old_result} new={new_result}"
            )
            metrics.increment('rbac.enforcement.changed_access')

    # Enforce new result
    return new_result
```

**This phase identifies users who lost or gained access due to migration.**

### Strategy 3: Percentage Rollout

**Best for**: Gradual production deployment

```python
import hashlib

def check_permission(request, permission, resource):
    """Gradually roll out RBAC to percentage of users."""

    # Determine if user is in rollout cohort
    user_hash = int(hashlib.md5(request.user.username.encode()).hexdigest(), 16)
    rollout_pct = settings.RBAC_ROLLOUT_PERCENTAGE  # 0-100
    use_rbac = (user_hash % 100) < rollout_pct

    if use_rbac:
        # User is in RBAC cohort
        result = rbac_client.check_permission(
            principal=request.user.username,
            permission=permission,
            resource_id=str(resource.workspace_id)
        )
        metrics.increment('rbac.check.new')
    else:
        # User still on old system
        result = legacy_permission_check(request.user, resource)
        metrics.increment('rbac.check.old')

    return result
```

**Rollout schedule**:
1. Week 1: 10% of users
2. Week 2: 25% of users
3. Week 3: 50% of users
4. Week 4: 100% of users

**Monitor error rates and latency per cohort** — if RBAC cohort shows elevated errors, pause rollout.

### Caching for Performance

RBAC permission checks add network latency. Cache aggressively:

```python
from django.core.cache import cache

def check_permission_cached(request, permission, resource):
    """Check permission with caching."""

    cache_key = f"rbac:{request.user.username}:{permission}:{resource.workspace_id}"
    result = cache.get(cache_key)

    if result is None:
        result = rbac_client.check_permission(
            principal=request.user.username,
            permission=permission,
            resource_id=str(resource.workspace_id)
        )
        # Cache for 5 minutes
        cache.set(cache_key, result, timeout=300)

    return result
```

**Invalidation**: RBAC changes replicate to Kessel in 100-500ms. A 5-minute cache means up to 5 minutes of stale permissions — acceptable for most use cases. For immediate invalidation, subscribe to RBAC change events via Kafka.

---

## Testing Your Migration

### Unit Testing

Mock the RBAC client to test permission logic:

```python
from unittest.mock import patch, MagicMock

class TestPermissions(TestCase):
    @patch('myapp.rbac_client.check_permission')
    def test_user_can_view_host(self, mock_check):
        # Arrange
        mock_check.return_value = True
        host = Host.objects.create(name="test-host")

        # Act
        response = self.client.get(f'/api/hosts/{host.id}/')

        # Assert
        self.assertEqual(response.status_code, 200)
        mock_check.assert_called_once_with(
            principal='testuser',
            permission='myapp:hosts:read',
            resource_id=str(host.workspace_id)
        )

    @patch('myapp.rbac_client.check_permission')
    def test_user_cannot_view_host(self, mock_check):
        # Arrange
        mock_check.return_value = False
        host = Host.objects.create(name="test-host")

        # Act
        response = self.client.get(f'/api/hosts/{host.id}/')

        # Assert
        self.assertEqual(response.status_code, 404)  # Not 403, to prevent existence leakage
```

### Integration Testing

Test against a real RBAC instance in dev/staging:

```python
class TestRBACIntegration(TestCase):
    def setUp(self):
        # Create test workspace
        self.workspace = rbac_client.create_workspace(
            name="test-workspace",
            description="Integration test workspace",
            parent_id=get_root_workspace_id()
        )

        # Create test role binding (using batch create API)
        result = rbac_client.batch_create_role_bindings([{
            "resource": {"id": self.workspace['id'], "type": "workspace"},
            "subject": {"id": get_test_user_uuid(), "type": "user"},
            "role": {"id": get_viewer_role_id()}
        }])
        self.role_binding = result['role_bindings'][0]

    def tearDown(self):
        # Clean up
        # Note: v2 API provides DELETE endpoints for cleanup
        rbac_client.delete_workspace(self.workspace['id'])

    def test_permission_check_allows_access(self):
        # Wait for replication (RBAC → Kessel)
        time.sleep(1)

        # Check permission
        result = rbac_client.check_permission(
            principal="testuser",
            permission="myapp:hosts:read",
            resource_id=self.workspace['id']
        )

        self.assertTrue(result)
```

### Performance Testing

Measure latency impact:

```python
import time
import statistics

def benchmark_permission_checks():
    latencies = []

    for i in range(100):
        start = time.time()
        rbac_client.check_permission(
            principal="testuser",
            permission="myapp:hosts:read",
            resource_id=workspace_id
        )
        latencies.append((time.time() - start) * 1000)  # ms

    print(f"p50: {statistics.median(latencies):.2f}ms")
    print(f"p95: {statistics.quantiles(latencies, n=20)[18]:.2f}ms")
    print(f"p99: {statistics.quantiles(latencies, n=100)[98]:.2f}ms")
```

**Target latencies** (RBAC API call):
- p50: < 50ms
- p95: < 100ms
- p99: < 200ms

**If latencies exceed targets**, enable caching (see [Caching for Performance](#caching-for-performance)).

### Security Testing

Attempt to bypass permission checks:

```python
class TestSecurityBypass(TestCase):
    def test_cannot_access_without_permission(self):
        """Verify user without permission cannot access resource."""
        # Create host in workspace user doesn't have access to
        other_workspace = create_workspace_for_other_user()
        host = Host.objects.create(
            name="test-host",
            workspace_id=other_workspace['id']
        )

        # Attempt access
        response = self.client.get(f'/api/hosts/{host.id}/')

        # Should return 404 (not 403, to prevent existence leakage)
        self.assertEqual(response.status_code, 404)

    def test_cannot_list_unauthorized_resources(self):
        """Verify list endpoints don't leak unauthorized resources."""
        # Create hosts in two workspaces
        my_workspace = get_my_workspace()
        other_workspace = get_other_workspace()

        my_host = Host.objects.create(name="my-host", workspace_id=my_workspace['id'])
        other_host = Host.objects.create(name="other-host", workspace_id=other_workspace['id'])

        # List hosts
        response = self.client.get('/api/hosts/')

        # Should only include my_host
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], my_host.id)
```

---

## Common Pitfalls

### 1. Returning 403 Instead of 404

**Problem**: Returning 403 Forbidden for inaccessible resources reveals existence to unauthorized users.

```python
# WRONG: Reveals host exists
def get_host(request, host_id):
    host = Host.objects.get(id=host_id)
    if not check_permission(request, 'myapp:hosts:read', host):
        return HttpResponseForbidden()  # ❌ Reveals host exists
    return host

# CORRECT: Returns 404, prevents existence leakage
def get_host(request, host_id):
    host = Host.objects.get(id=host_id)
    if not check_permission(request, 'myapp:hosts:read', host):
        raise Http404()  # ✅ Hides existence
    return host
```

### 2. Forgetting to Filter List Endpoints

**Problem**: List endpoints return all resources, bypassing permission checks.

```python
# WRONG: Returns all hosts, ignoring permissions
def list_hosts(request):
    return Host.objects.all()  # ❌

# CORRECT: Filters by accessible workspaces
def list_hosts(request):
    accessible_workspaces = rbac_client.list_accessible_resources(
        principal=request.user.username,
        permission='myapp:hosts:read'
    )
    workspace_ids = [r['id'] for r in accessible_workspaces]
    return Host.objects.filter(workspace_id__in=workspace_ids)  # ✅
```

### 3. Not Handling RBAC API Failures

**Problem**: Network errors or RBAC downtime break your application.

```python
# WRONG: Exception bubbles up, breaks request
def check_permission(principal, permission, resource_id):
    return rbac_client.check_permission(principal, permission, resource_id)  # ❌

# CORRECT: Graceful degradation
def check_permission(principal, permission, resource_id):
    try:
        return rbac_client.check_permission(principal, permission, resource_id)
    except requests.exceptions.Timeout:
        logger.error("RBAC timeout, denying access")
        metrics.increment('rbac.timeout')
        return False  # Fail closed
    except Exception as e:
        logger.error(f"RBAC error: {e}", exc_info=True)
        metrics.increment('rbac.error')
        # Option 1: Fail closed (deny access)
        return False
        # Option 2: Fail open (allow access) - only for non-critical resources
        # return True
```

### 4. Caching Too Aggressively

**Problem**: Long cache TTLs mean permission changes don't take effect promptly.

```python
# RISKY: 1-hour cache means revoked access persists for up to 1 hour
cache.set(cache_key, result, timeout=3600)  # ⚠️

# SAFER: 5-minute cache balances performance and staleness
cache.set(cache_key, result, timeout=300)  # ✅
```

**Recommendation**: Cache for 5 minutes by default. For sensitive resources (billing, admin functions), cache for 1 minute or skip caching entirely.

### 5. Hardcoding Workspace IDs

**Problem**: Workspace UUIDs differ across environments (dev, staging, prod).

```python
# WRONG: Hardcoded UUID breaks in other environments
ADMIN_WORKSPACE_ID = "12345678-1234-1234-1234-123456789012"  # ❌

# CORRECT: Fetch dynamically or use environment variable
def get_admin_workspace_id():
    return os.getenv('ADMIN_WORKSPACE_ID') or fetch_from_rbac()  # ✅
```

### 6. Not Testing Cross-Tenant Isolation

**Problem**: Missing `org_id` filter allows cross-tenant data leakage.

```python
# Test that users from org A cannot access org B's resources
def test_cross_tenant_isolation(self):
    # Create host for org A
    org_a_host = Host.objects.create(name="host-a", tenant=org_a)

    # Authenticate as user from org B
    self.client.force_authenticate(user=org_b_user)

    # Attempt to access org A's host
    response = self.client.get(f'/api/hosts/{org_a_host.id}/')

    # Should return 404 (or 403), never 200
    self.assertIn(response.status_code, [403, 404])
```

### 7. Overloading Permission Granularity

**Problem**: Too many fine-grained permissions make roles unusable.

```python
# TOO GRANULAR: 100+ permissions for one resource type
myapp:host:read_name
myapp:host:read_ip
myapp:host:read_os
myapp:host:write_name
myapp:host:write_ip
# ... 95 more

# BETTER: Operation-level permissions
myapp:hosts:read   # Read all host fields
myapp:hosts:write  # Write all host fields
```

**Guideline**: Aim for 3-10 permissions per resource type. Common pattern: `read`, `write`, `delete`, plus occasional special ops like `execute`, `export`.

---

## Resources

### RBAC API Documentation

- **V2 API Spec**: [docs/source/specs/v2/openapi.yaml](source/specs/v2/openapi.yaml)
- **TypeSpec Source**: [docs/source/specs/typespec/main.tsp](source/specs/typespec/main.tsp)

### Kessel Documentation

#### Getting Started
- **Quick Start Tutorial** (~30 min): [https://project-kessel.github.io/docs/start-here/getting-started/](https://project-kessel.github.io/docs/start-here/getting-started/) - End-to-end walkthrough of resource definitions, inventory reporting, and permission checking
- **Understanding Kessel**: [https://project-kessel.github.io/docs/start-here/understanding-kessel/](https://project-kessel.github.io/docs/start-here/understanding-kessel/)
- **Run with Docker**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/run-with-docker/](https://project-kessel.github.io/docs/building-with-kessel/how-to/run-with-docker/)

#### Concepts
- **RBAC Concepts**: [https://project-kessel.github.io/docs/building-with-kessel/concepts/rbac/](https://project-kessel.github.io/docs/building-with-kessel/concepts/rbac/)
- **Consistency Model**: [https://project-kessel.github.io/docs/building-with-kessel/concepts/consistency/](https://project-kessel.github.io/docs/building-with-kessel/concepts/consistency/) - Understanding 100-500ms replication windows and `CheckForUpdate`

#### How-To Guides
- **Design Permissions**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/design-permissions/](https://project-kessel.github.io/docs/building-with-kessel/how-to/design-permissions/)
- **Protect Endpoints**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/protect-endpoint/](https://project-kessel.github.io/docs/building-with-kessel/how-to/protect-endpoint/)
- **RBAC Management**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/rbac/](https://project-kessel.github.io/docs/building-with-kessel/how-to/rbac/)
- **SDK Authentication**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/authenticate-with-sdks/](https://project-kessel.github.io/docs/building-with-kessel/how-to/authenticate-with-sdks/)
- **Migrate from RBAC v1 to v2**: [https://project-kessel.github.io/docs/building-with-kessel/how-to/migrate-from-rbac-v1-to-v2/](https://project-kessel.github.io/docs/building-with-kessel/how-to/migrate-from-rbac-v1-to-v2/)

#### API References
- **gRPC Inventory API**: [https://buf.build/project-kessel/inventory-api/docs/main:kessel.inventory.v1beta2](https://buf.build/project-kessel/inventory-api/docs/main:kessel.inventory.v1beta2)
- **HTTP API Reference**: [https://project-kessel.github.io/docs/building-with-kessel/reference/http-api/](https://project-kessel.github.io/docs/building-with-kessel/reference/http-api/)
- **Error Codes**: [https://project-kessel.github.io/docs/building-with-kessel/reference/error-codes/](https://project-kessel.github.io/docs/building-with-kessel/reference/error-codes/)

### insights-rbac Documentation

- **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md)
- **Security Guidelines**: [security-guidelines.md](security-guidelines.md)
- **API Contracts**: [api-contracts-guidelines.md](api-contracts-guidelines.md)
- **Role Bindings Deep Dive**: [role_bindings.md](role_bindings.md)
- **Integration Guidelines**: [integration-guidelines.md](integration-guidelines.md)

### Sample Client Libraries

**Important**: These sample implementations demonstrate how to interact with the RBAC v2 API. Note that:
- `create_workspace()` uses the actual v2 API endpoint: `POST /api/rbac/v2/workspaces/`
- `batch_create_role_bindings()` uses the actual v2 API endpoint: `POST /api/rbac/v2/role-bindings:batchCreate/` (v2 only supports batch creation, max 100 per request)
- `check_permission()` requires **Kessel Relations API integration** (not provided by RBAC v2 API)
- `list_accessible_resources()` is implemented by querying `/api/rbac/v2/role-bindings/by-subject/` and extracting resource IDs

#### Python (using requests)

```python
import requests
from typing import List, Dict, Optional

class RBACClient:
    def __init__(self, base_url: str, psk: str, org_id: str, client_id: str):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'x-rh-rbac-psk': psk,
            'x-rh-rbac-org-id': org_id,
            'x-rh-rbac-client-id': client_id,
            'Content-Type': 'application/json'
        })

    def check_permission(
        self,
        principal: str,
        permission: str,
        resource_id: str
    ) -> bool:
        """
        Check if principal has permission on resource.

        Note: V2 API doesn't provide a direct access check endpoint.
        Applications should use Kessel Relations API directly for permission checks,
        or implement their own authorization layer based on role bindings.

        For production use, integrate with Kessel Relations API (gRPC):
        https://project-kessel.github.io/docs/building-with-kessel/
        """
        # This is a placeholder - implement using Kessel Relations API
        raise NotImplementedError("Use Kessel Relations API for permission checks")

    def list_accessible_resources(
        self,
        principal: str,
        permission: str
    ) -> List[Dict]:
        """
        Get all resources principal can access with permission.

        Note: V2 API doesn't provide a direct resource listing by permission.
        Applications should query role bindings for the principal and determine
        accessible workspaces from the results.

        Example: GET /api/rbac/v2/role-bindings/by-subject/?subject={principal}
        """
        # Query role bindings for this principal
        response = self.session.get(
            f"{self.base_url}/api/rbac/v2/role-bindings/by-subject/",
            params={"subject_id": principal},  # Use subject_id parameter
            timeout=10
        )
        response.raise_for_status()
        # Extract workspace IDs from role bindings
        # Response structure: {data: [{resource: {id, type, name}, roles: [...], ...}]}
        role_bindings = response.json().get('data', [])
        return [{'id': rb['resource']['id'], 'type': rb['resource']['type']} for rb in role_bindings]

    def create_workspace(
        self,
        name: str,
        parent_id: Optional[str] = None,
        description: Optional[str] = None
    ) -> Dict:
        """
        Create a new workspace.

        API: POST /api/rbac/v2/workspaces/
        Request: {name (required), description (optional), parent_id (optional)}
        Response: {id, name, description, parent_id, type, created, modified}
        """
        payload = {"name": name}
        if description:
            payload["description"] = description
        if parent_id:
            payload["parent_id"] = parent_id

        response = self.session.post(
            f"{self.base_url}/api/rbac/v2/workspaces/",
            json=payload,
            timeout=5
        )
        response.raise_for_status()
        return response.json()

    def batch_create_role_bindings(
        self,
        binding_requests: List[Dict]
    ) -> Dict:
        """
        Create multiple role bindings in a single request.

        API: POST /api/rbac/v2/role-bindings:batchCreate/
        Request: {requests: [{resource: {id, type}, subject: {id, type}, role: {id}}]}
        Response: {role_bindings: [{resource, subject, role, sources}]}
        Maximum 100 bindings per request.
        """
        response = self.session.post(
            f"{self.base_url}/api/rbac/v2/role-bindings:batchCreate/",
            json={"requests": binding_requests},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
```

#### Go (using net/http)

```go
package rbac

import (
    "bytes"
    "encoding/json"
    "fmt"
    "net/http"
    "time"
)

type Client struct {
    BaseURL  string
    PSK      string
    OrgID    string
    ClientID string
    HTTP     *http.Client
}

func NewClient(baseURL, psk, orgID, clientID string) *Client {
    return &Client{
        BaseURL:  baseURL,
        PSK:      psk,
        OrgID:    orgID,
        ClientID: clientID,
        HTTP: &http.Client{
            Timeout: 5 * time.Second,
        },
    }
}

// CheckPermission checks if a principal has permission on a resource.
// Note: V2 API doesn't provide a direct access check endpoint.
// For production use, integrate with Kessel Relations API (gRPC).
// This example queries role bindings to determine access.
func (c *Client) CheckPermission(principal, permission, resourceID string) (bool, error) {
    // Query role bindings for this principal
    url := fmt.Sprintf("%s/api/rbac/v2/role-bindings/by-subject/?subject_id=%s", c.BaseURL, principal)
    req, _ := http.NewRequest("GET", url, nil)
    req.Header.Set("x-rh-rbac-psk", c.PSK)
    req.Header.Set("x-rh-rbac-org-id", c.OrgID)
    req.Header.Set("x-rh-rbac-client-id", c.ClientID)
    req.Header.Set("Content-Type", "application/json")

    resp, err := c.HTTP.Do(req)
    if err != nil {
        return false, err
    }
    defer resp.Body.Close()

    if resp.StatusCode != 200 {
        return false, fmt.Errorf("RBAC API returned %d", resp.StatusCode)
    }

    var result struct {
        Data []struct {
            Resource struct {
                ID   string `json:"id"`
                Type string `json:"type"`
            } `json:"resource"`
            Roles []struct {
                ID          string   `json:"id"`
                Name        string   `json:"name"`
                Permissions []string `json:"permissions"`
            } `json:"roles"`
        } `json:"data"`
    }
    json.NewDecoder(resp.Body).Decode(&result)

    // Check if any role binding grants the permission on the resource
    for _, rb := range result.Data {
        if rb.Resource.ID == resourceID {
            for _, role := range rb.Roles {
                for _, perm := range role.Permissions {
                    if perm == permission || perm == "*:*:*" {
                        return true, nil
                    }
                }
            }
        }
    }
    return false, nil
}
```

### Support

- **Slack**: `#forum-consoledot-rbac` (internal Red Hat)
- **Email**: rbac-team@redhat.com
- **Issues**: File bugs/questions in the insights-rbac GitHub repo
