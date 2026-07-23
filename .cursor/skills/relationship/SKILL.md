---
name: relationship-query
description: Query relationships in SpiceDB/Kessel via the RBAC relations API
---

# Relationship Query Tool

Use this skill to query relationships stored in SpiceDB/Kessel via the RBAC internal relations API.

## Prerequisites

Before running queries, you MUST:

0. **Check for config.env file**: Verify that `.cursor/skills/config.env` exists. If it does not exist:
   - Create `.cursor/skills/config.env` with STAGE_DOMAIN, PROD_DOMAIN, PROXY
   - **WARNING**: The script will fail if `config.env` is missing or incomplete. Always ensure `config.env` exists before proceeding.

1. **Check if SESSION is set**: If the `SESSION` environment variable is not set, ask the user to get the token from the Turnpike Session by pasting the URL into their browser:
   - For stage: `${STAGE_DOMAIN}/api/turnpike/session/` (domain from `.cursor/skills/config.env`)
   - For prod: `${PROD_DOMAIN}/api/turnpike/session/` (domain from `.cursor/skills/config.env`)
   - Copy the token from the browser response and set it with `export SESSION=<token_value>`
   - Do NOT run queries without a valid SESSION.

2. **Confirm the environment**: If the environment (stage or prod) is not explicitly specified by the user, ask them to confirm which environment they want to query.

### Setting up SESSION

You can obtain the SESSION value using one of these methods:

#### Method 1: Get token from Turnpike Session API (Recommended)

Get the session token by pasting the URL into your browser:

**For stage environment:**
1. Paste this URL into your browser: `${STAGE_DOMAIN}/api/turnpike/session/` (where `STAGE_DOMAIN` is from `.cursor/skills/config.env`, default: `https://internal.console.stage.redhat.com`)
2. Copy the token from the browser response
3. Set it: `export SESSION=<token_from_response>`

**For prod environment:**
1. Paste this URL into your browser: `${PROD_DOMAIN}/api/turnpike/session/` (where `PROD_DOMAIN` is from `.cursor/skills/config.env`, default: `https://internal.console.redhat.com`)
2. Copy the token from the browser response
3. Set it: `export SESSION=<token_from_response>`

#### Method 2: Get session cookie from browser

Alternatively, the session cookie can be obtained from browser cookies when logged into console.stage.redhat.com or console.redhat.com:

1. Log in to the console (stage or prod)
2. Open browser DevTools (F12)
3. Go to Application/Storage tab → Cookies
4. Copy the value of the `session` cookie
5. Set it: `export SESSION=<session_cookie_value>`

## Usage

Run the script at `.cursor/skills/relationship/scripts/relationship.sh`:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh <stage|prod> <read_tuples|lookup_resource> '<JSON_PAYLOAD>'
```

**Important**: Always verify SESSION is set and environment is confirmed before executing queries.

## Workflow

When a user requests a relationship query:

0. **Check for config.env**: Verify `.cursor/skills/config.env` exists
   - If NOT found: Inform the user that `config.env` is required and provide instructions to create it from the example file
   - Do NOT proceed until `config.env` exists

1. **Check SESSION**: Verify if `SESSION` environment variable is set
   - If NOT set: Ask the user to get the token by pasting the Turnpike Session URL into their browser:
     - For stage: `${STAGE_DOMAIN}/api/turnpike/session/` (domain from `.cursor/skills/config.env`)
     - For prod: `${PROD_DOMAIN}/api/turnpike/session/` (domain from `.cursor/skills/config.env`)
   - Ask the user to copy the token from the browser response and set it with `export SESSION=<token_value>`
   - Do NOT proceed with the query until SESSION is set

2. **Confirm Environment**: Determine which environment to query
   - If user specified stage/prod: Use that environment
   - If unclear: Ask the user to confirm (stage or prod)

3. **Execute Query**: Only after config.env exists, SESSION is set, and environment is confirmed, run the relationship query script

## Common Queries

### Group Queries

#### Find all members of a group

Find all principals that are members of a group:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "group",
    "resource_id": "<group_uuid>",
    "relation": "member",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "principal",
      "subject_id": ""
    }
  }
}'
```

#### Check if a principal is a member of a group

Verify if a specific user/principal is a member of a group:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "group",
    "resource_id": "<group_uuid>",
    "relation": "member",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "principal",
      "subject_id": "redhat/<user_id>"
    }
  }
}'
```

**Note**: Use `redhat/<user_id>` format for both stage and prod environments. See Principal Resource ID Format section for details.

### Role Binding Queries

#### Find all role bindings where a group is a subject (read_tuples)

Find all role bindings where a specific group is bound as a subject:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "",
    "relation": "subject",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "group",
      "subject_id": "<group_uuid>"
    }
  }
}'
```

#### Find all role bindings where a group is a subject (lookup_resource)

Alternative method to find role bindings for a group:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage lookup_resource '{
  "resource_type": {
    "name": "role_binding",
    "namespace": "rbac"
  },
  "subject": {
    "subject": {
      "type": {
        "name": "group",
        "namespace": "rbac"
      },
      "id": "<group_uuid>"
    }
  },
  "relation": "subject"
}'
```

#### Read all relationships for a specific role binding

Find all relationships for a specific role binding:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "<role_binding_uuid>",
    "relation": "",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "group",
      "subject_id": ""
    }
  }
}'
```

#### Find what role is assigned to a role binding

Find the role associated with a specific role binding:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "<role_binding_uuid>",
    "relation": "role",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "role",
      "subject_id": ""
    }
  }
}'
```

### Principal/User Queries

#### Find all groups a principal belongs to

Find all groups where a specific principal is a member:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "group",
    "resource_id": "",
    "relation": "member",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "principal",
      "subject_id": "redhat/<user_id>"
    }
  }
}'
```

**Note**: Replace `<user_id>` with the numeric user ID. Use `redhat/<user_id>` format for both stage and prod environments. See Principal Resource ID Format section for details.

### Role Binding Relationship Queries

#### Verify group is subject of a role binding

Check if a specific group is bound as a subject to a role binding:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "<role_binding_uuid>",
    "relation": "subject",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "group",
      "subject_id": "<group_uuid>"
    }
  }
}'
```

#### Find role assigned to a role binding

Find what role is assigned to a specific role binding:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "<role_binding_uuid>",
    "relation": "role",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "role",
      "subject_id": ""
    }
  }
}'
```

#### Verify complete access chain

Verify the complete access chain: User → Group → Role Binding → Role → Workspace

**Step 1: Verify user is member of group**
```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "group",
    "resource_id": "<group_uuid>",
    "relation": "member",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "principal",
      "subject_id": "redhat/<user_id>"
    }
  }
}'
```

**Step 2: Verify group is subject of role binding**
```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "<role_binding_uuid>",
    "relation": "subject",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "group",
      "subject_id": "<group_uuid>"
    }
  }
}'
```

**Step 3: Verify role binding has role**
```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "<role_binding_uuid>",
    "relation": "role",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "role",
      "subject_id": ""
    }
  }
}'
```

**Step 4: Check role binding resource (workspace) via database**
Use gabi skill to verify which workspace the role binding is bound to:
```sql
SELECT rb.uuid, rb.resource_type, rb.resource_id, w.name as workspace_name
FROM management_rolebinding rb
LEFT JOIN management_workspace w ON rb.resource_id = w.id::text
WHERE rb.uuid = '<role_binding_uuid>' AND rb.resource_type = 'workspace';
```

## Important Notes

### Principal Resource ID Format

When querying for principals (users), use the following format:

- **Format**: `redhat/<user_id>` (same for both stage and prod environments)
  - Example: For user_id `58872914`, use `redhat/58872914`
  - Service accounts also use `redhat/<service_account_id>` format
- **Note**: The format is `<domain>/<user_id>` where domain comes from `PRINCIPAL_USER_DOMAIN` setting (typically `redhat` for both environments)

**To find a user's `user_id`**:
1. Query the database: `SELECT user_id FROM management_principal WHERE username = '<username>';`
2. Or use the gabi skill to find principals in a group

**To determine the correct format for your environment**:
- Check the `PRINCIPAL_USER_DOMAIN` setting in the application configuration
- Test with a known user_id to verify the format
- The format is typically `<domain>/<user_id>` where domain comes from `PRINCIPAL_USER_DOMAIN`

### Query Result Interpretation

- **Empty results (`"tuples": []` or `"resources": []`)**: No relationships found matching the query
- **Non-empty results**: Relationships exist in SpiceDB
- **Note**: Database and SpiceDB may be out of sync. If a relationship exists in the database but not in SpiceDB, there may be a replication delay.

### Common Query Patterns

1. **Check group membership**: Use `read_tuples` with `relation: "member"` on a group resource
2. **Find role bindings for a group**: Use `read_tuples` or `lookup_resource` with `relation: "subject"` on role_binding resource
3. **Verify access**: Check role bindings → check group membership → verify permissions

## Resource Types

Common resource types in the rbac namespace:
- `role_binding` - Binds roles to subjects (groups/principals)
- `group` - Groups of principals
- `role` - Role definitions
- `workspace` - Workspace resources
- `principal` - Individual users/service accounts

## Relations

Common relations:
- `subject` - Subject of a role binding (group or principal)
- `member` - Member of a group
- `role` - Role assigned to a binding
- `parent` - Parent workspace relationship
- `user_grant` - User grant on a resource (workspace, etc.)

## Complete Workflow: Check User Access to Workspace

This workflow verifies if a user has access to a specific workspace by checking the complete access chain in SpiceDB relationships.

### Prerequisites

1. **Get user_id**: Find the user's `user_id` from the database:
   ```sql
   SELECT user_id, username FROM management_principal WHERE username = '<username>';
   ```
   Or use gabi skill to find principals in a group.

2. **Get workspace_id**: The workspace UUID you want to check access for.

### Step-by-Step Verification

#### Step 1: Find all groups the user belongs to

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "group",
    "resource_id": "",
    "relation": "member",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "principal",
      "subject_id": "redhat/<user_id>"
    }
  }
}'
```

**Expected result**: List of groups where the user is a member. Note the group UUIDs.

#### Step 2: Find role bindings for each group

For each group UUID found in Step 1:

```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "",
    "relation": "subject",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "group",
      "subject_id": "<group_uuid>"
    }
  }
}'
```

**Expected result**: List of role bindings where the group is a subject. Note the role binding UUIDs.

#### Step 3: Check which workspace each role binding is bound to

For each role binding UUID found in Step 2, query the database to find the workspace:

```sql
SELECT rb.uuid as role_binding_uuid, rb.resource_type, rb.resource_id as workspace_id, w.name as workspace_name, rv2.uuid as role_uuid, rv2.name as role_name
FROM management_rolebinding rb
LEFT JOIN management_workspace w ON rb.resource_id = w.id::text
LEFT JOIN management_rolev2 rv2 ON rb.role_id = rv2.id
WHERE rb.uuid = '<role_binding_uuid>' AND rb.resource_type = 'workspace';
```

**Expected result**: Workspace information for each role binding. Check if any `workspace_id` matches your target workspace.

#### Step 4: Verify the complete access chain (optional but recommended)

For the role binding that matches your target workspace, verify each link in the chain:

**4a. Verify user is member of group**:
```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "group",
    "resource_id": "<group_uuid>",
    "relation": "member",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "principal",
      "subject_id": "redhat/<user_id>"
    }
  }
}'
```

**4b. Verify group is subject of role binding**:
```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "<role_binding_uuid>",
    "relation": "subject",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "group",
      "subject_id": "<group_uuid>"
    }
  }
}'
```

**4c. Verify role binding has role**:
```bash
sh .cursor/skills/relationship/scripts/relationship.sh stage read_tuples '{
  "filter": {
    "resource_namespace": "rbac",
    "resource_type": "role_binding",
    "resource_id": "<role_binding_uuid>",
    "relation": "role",
    "subject_filter": {
      "subject_namespace": "rbac",
      "subject_type": "role",
      "subject_id": ""
    }
  }
}'
```

**4d. Check role permissions** (via database):
```sql
SELECT DISTINCT p.permission, p.application, p.resource_type, p.verb
FROM management_permission p
JOIN management_access a ON a.permission_id = p.id
JOIN management_role r ON a.role_id = r.id
JOIN management_rolev2 rv2 ON rv2.v1_source_id = r.id
WHERE rv2.uuid = '<role_uuid>';
```

### Access Chain Summary

If all steps verify successfully, the access chain is:

```
User (redhat/<user_id>)
  → member of → Group (<group_uuid>)
    → subject of → Role Binding (<role_binding_uuid>)
      → role → Role (<role_uuid>)
        → bound to → Workspace (<workspace_id>)
          → grants → Permissions (<permissions>)
```

### Quick Check: Direct Workspace Access Query

To quickly check if a user has ANY access to a workspace, you can:

1. Get user's groups (Step 1 above)
2. For each group, find role bindings bound to the specific workspace:

```sql
SELECT rb.uuid as role_binding_uuid, rb.resource_id as workspace_id, w.name as workspace_name, rv2.name as role_name, g.uuid as group_uuid, g.name as group_name
FROM management_rolebinding rb
JOIN management_rolebindinggroup rbg ON rbg.binding_id = rb.id
JOIN management_group g ON rbg.group_id = g.id
JOIN management_rolev2 rv2 ON rb.role_id = rv2.id
LEFT JOIN management_workspace w ON rb.resource_id = w.id::text
WHERE rb.resource_type = 'workspace'
  AND rb.resource_id = '<workspace_id>'
  AND g.uuid IN (<list_of_group_uuids>);
```

Replace `<list_of_group_uuids>` with comma-separated UUIDs from Step 1.

### Troubleshooting

- **No groups found**: User may not be a member of any groups, or membership hasn't synced to SpiceDB
- **No role bindings found**: Groups may not have any role bindings, or bindings haven't synced to SpiceDB
- **Role binding not bound to workspace**: Group has role bindings but not for the target workspace
- **Database vs SpiceDB mismatch**: Relationships may exist in database but not yet replicated to SpiceDB (check replication status)
