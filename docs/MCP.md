# RBAC MCP Endpoint

This document describes the Model Context Protocol (MCP) endpoint for the RBAC service, which enables AI agents to discover and invoke RBAC operations via the standard MCP JSON-RPC 2.0 protocol.

For operator-facing documentation (deployment, configuration, security, all environment variables), see [MCP-operator-guide.md](MCP-operator-guide.md).

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Endpoint](#endpoint)
- [Authentication](#authentication)
- [Protocol](#protocol)
  - [Initialize](#initialize)
  - [List Tools](#list-tools)
  - [Call Tool](#call-tool)
  - [Notifications](#notifications)
  - [Session Termination](#session-termination)
- [Available Tools](#available-tools)
  - [System](#system-2-tools)
  - [Principals](#principals-1-tool)
  - [Permissions](#permissions-2-tools)
  - [Access](#access-1-tool)
  - [Roles](#roles-11-tools)
  - [Groups](#groups-11-tools)
  - [Cross-Account / TAM](#cross-account--tam-7-tools)
  - [Workspaces](#workspaces-6-tools)
  - [Role Bindings](#role-bindings-4-tools)
  - [Audit / Investigation](#audit--investigation-5-tools)
  - [User State](#user-state-4-tools)
- [Write Tools and Confirmation Flow](#write-tools-and-confirmation-flow)
- [API Version Gating](#api-version-gating)
- [Adding New Tools](#adding-new-tools)
- [Local Development](#local-development)
- [Tool Execution Timeout](#tool-execution-timeout)
- [Error Handling](#error-handling)
- [Key Files Reference](#key-files-reference)

---

## Overview

The MCP endpoint exposes RBAC operations as tools that any MCP-compatible client can discover and invoke. It implements the [MCP StreamableHTTP transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http) over a single HTTP POST endpoint.

| Property | Value |
|----------|-------|
| **Path** | `/_private/_a2s/mcp/` |
| **Transport** | StreamableHTTP (JSON-RPC 2.0 over HTTP POST) |
| **Auth** | `x-rh-identity` header (same as public API) |
| **Runtime** | Synchronous WSGI (Django) |
| **Tools** | 54 registered (31 read, 23 write) |
| **Tests** | 419 tests in `tests/management/test_mcp_views.py` |

---

## Architecture

```
MCP Client (AI Agent)
    |
    |  JSON-RPC 2.0 over HTTP POST
    v
+----------------------------------------------+
|  IdentityHeaderMiddleware                    |
|  (A2S paths use public auth, not PSK/JWT)    |
+----------------------+-----------------------+
                       v
+----------------------------------------------+
|  MCPView.post()                              |
|  +-- _parse_jsonrpc()  -> JsonRpcRequest     |
|  +-- initialize        -> protocol handshake |
|  +-- tools/list        -> _get_tools()       |
|  +-- tools/call        -> _TOOL_CONFIG       |
|       +-- Write gating (MCP_WRITE_ENABLED)   |
|       +-- Confirmation flow (SHA-256 token)  |
|       +-- Kessel/V1 permission check         |
|       +-- Prometheus metrics recording       |
|       +-- Tool execution (sync, in-thread)   |
|            +-- View delegation via           |
|            |   _clone_request() -> ViewSet   |
|            +-- In-process ORM for            |
|                orchestration tools           |
+----------------------------------------------+
```

**Key design decisions:**

- **Synchronous execution** -- Tools run in the WSGI request thread to avoid Django's `SynchronousOnlyOperation` when accessing the ORM. FastMCP is used only for JSON schema generation (`tools/list`), not for dispatching tool calls.
- **A2S routing** -- The endpoint lives under `/_private/_a2s/` (agent-to-service), which uses the same `x-rh-identity` auth as public APIs instead of the internal PSK/JWT auth normally applied to `/_private/` paths.
- **`@register_tool` decorator** -- Registers each tool with both FastMCP (for schema) and `_TOOL_CONFIG` (for sync execution) in one declaration.
- **Redis-backed description overrides** -- Tool descriptions can be dynamically overridden via Redis keys (`mcp:desc:<tool_name>`) without code changes.
- **Prometheus metrics** -- Every tool call records `rbac_mcp_tool_call_total` (counter with tool name and status) and `rbac_mcp_tool_call_duration_seconds` (histogram).
- **Dual auth** -- V2 orgs use Kessel relation checks (`required_relation`), V1 orgs use permission tuple checks (`v1_permission`), org admins always pass.

---

## Endpoint

```
POST /_private/_a2s/mcp/
```

All MCP protocol messages are sent as HTTP POST requests to this single endpoint. The JSON-RPC `method` field determines the operation.

| HTTP Method | Behavior |
|-------------|----------|
| `POST` | Handle MCP JSON-RPC requests |
| `GET` | Returns 405 (SSE streaming not supported in WSGI) |
| `DELETE` | Returns 200 (session termination acknowledgement) |

---

## Authentication

The MCP endpoint uses the same `x-rh-identity` header as the public RBAC API. See [AUTHENTICATION.md](AUTHENTICATION.md) for full details.

**Key points:**

- Tools marked `requires_auth=True` (e.g. `list_principals`) require a valid identity header with an `org_id`.
- Tools without auth requirements (e.g. `hello`) work without any identity header.
- Unauthenticated requests to auth-required tools receive a JSON-RPC error (`-32000 Authentication required`), not an HTTP 401.

---

## Protocol

The endpoint implements the MCP JSON-RPC 2.0 protocol. All requests must include:

```json
{
  "jsonrpc": "2.0",
  "method": "<method-name>",
  "id": <request-id>,
  "params": {}
}
```

### Initialize

Start a new MCP session. Returns protocol version and server capabilities.

**Request:**
```bash
curl -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "my-agent", "version": "1.0"}
    }
  }'
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "protocolVersion": "2025-03-26",
    "capabilities": {
      "tools": {"listChanged": false}
    },
    "serverInfo": {
      "name": "RBAC",
      "version": "1.0.0"
    }
  },
  "id": 1
}
```

The response includes an `Mcp-Session-Id` header for session tracking.

### List Tools

Discover available tools and their JSON Schema input definitions.

**Request:**
```bash
curl -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}'
```

**Response:** Returns an array of tool objects, each with `name`, `description`, and `inputSchema` (JSON Schema for the tool's parameters). Write tools that are disabled (when `MCP_WRITE_ENABLED=False`) are annotated with `[DISABLED]` in their description.

### Call Tool

Invoke a tool by name with arguments.

**Request (unauthenticated tool):**
```bash
curl -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 3,
    "params": {
      "name": "hello",
      "arguments": {"message": "Hi from my agent!"}
    }
  }'
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"response\": \"RBAC received your message: 'Hi from my agent!'\", \"date\": \"2025-01-15 10:30:00 UTC\"}"
      }
    ],
    "isError": false
  },
  "id": 3
}
```

**Request (authenticated tool):**
```bash
curl -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -H "x-rh-identity: <base64-encoded-identity>" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 4,
    "params": {
      "name": "list_principals",
      "arguments": {"limit": 5, "sort_order": "desc"}
    }
  }'
```

### Notifications

JSON-RPC requests without an `id` field are treated as notifications. The server acknowledges with HTTP 202 (no response body).

```bash
curl -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}'
# Returns HTTP 202
```

### Session Termination

```bash
curl -X DELETE http://localhost:8000/_private/_a2s/mcp/
# Returns HTTP 200
```

---

## Available Tools

54 tools organized into 11 categories. Each tool listing includes its parameters, API version, auth requirements, and the underlying endpoint it calls.

**Legend:**

- **API Version**: `common` = works in both V1 and V2 orgs; `unified` = auto-detects V1/V2; `v1` = V1 only; `v2` = V2 only; `unversioned` = no auth required.
- **Write**: Tools marked as write require `MCP_WRITE_ENABLED=True` and use the two-phase confirmation flow.
- **Permission**: Kessel relation (V2 orgs) / V1 permission tuple. Org admins always bypass.

### System (2 tools)

#### hello

Say hello to the RBAC service. Returns your message echoed back along with the current server date in UTC. Use this to verify MCP connectivity.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | string | `"Hello, World!"` | Message to send |

**API Version:** unversioned
**Auth:** None required
**Calls:** In-process (no API call)

**Example request:**
```json
{"method": "tools/call", "params": {"name": "hello", "arguments": {"message": "Testing!"}}}
```

**Returns:** `{response, date}` -- echoed message and server UTC timestamp.

---

#### get_status

Get RBAC server status including API version, commit hash, server address, platform info, Python version, and loaded modules.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| _(none)_ | | | |

**API Version:** unversioned
**Auth:** None required
**Calls:** `GET /api/v1/status/`

**Example request:**
```json
{"method": "tools/call", "params": {"name": "get_status", "arguments": {}}}
```

**Returns:** Server status object with API version, commit, platform info, and modules.

---

### Principals (1 tool)

#### list_principals

List principals (users) for the authenticated organization. Supports pagination, sorting, and filtering by status. Set `usernames` (comma-separated) to look up specific users. Set `name` to search by display name (cannot be used with `usernames`).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `10` | Max results to return |
| `offset` | integer | `0` | Pagination offset |
| `sort_order` | string | `"asc"` | Sort order (`asc` or `desc`) |
| `status` | string | `"enabled"` | Filter by status (`enabled`/`disabled`/`all`) |
| `username_only` | string | `"false"` | Return only usernames |
| `usernames` | string | `""` | Comma-separated usernames for exact lookup |
| `match_criteria` | string | `""` | `exact` (default) or `partial` for username matching |
| `name` | string | `""` | Search by display name (case-insensitive substring) |

**API Version:** common
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/principals/` (or in-process BOP proxy for name filtering)

**Example request:**
```json
{"method": "tools/call", "params": {"name": "list_principals", "arguments": {"limit": 5, "name": "John Smith"}}}
```

**Returns:** `{meta: {count}, links, data: [{username, email, first_name, last_name, is_org_admin, ...}]}`.

**Caveats:**
- Shows only users provisioned in this org -- does not include cross-account (TAM) users or service accounts from other identity providers.
- `is_org_admin` reflects the current state from the identity provider, not a historical snapshot.

---

### Permissions (2 tools)

#### list_permissions

List permissions available in RBAC. Each permission has the format `application:resource_type:verb`. Filter by application, resource_type, or verb. Supports pagination and ordering.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `application` | string | `""` | Filter by application |
| `resource_type` | string | `""` | Filter by resource type |
| `verb` | string | `""` | Filter by verb |
| `limit` | integer | `10` | Max results to return |
| `offset` | integer | `0` | Pagination offset |
| `order_by` | string | `""` | Order by `permission` or `-permission` |

**API Version:** common
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/permissions/`

**Example request:**
```json
{"method": "tools/call", "params": {"name": "list_permissions", "arguments": {"application": "cost-management", "limit": 20}}}
```

**Returns:** `{meta: {count}, links, data: [{application, resource_type, verb, permission}]}`.

**Caveats:**
- Permissions exist independently of roles. A permission appearing here does not mean any role grants it -- use `search_roles(permission='...')` to find roles that include a specific permission.
- Wildcard permissions (`*`) are expanded at access-check time, not in this listing.

---

#### list_permission_options

Get distinct values for a permission field. Use this to discover what applications, resource_types, or verbs exist.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `field` | string | _(required)_ | One of: `application`, `resource_type`, `verb` |
| `application` | string | `""` | Filter by application (comma-separated for multiple) |
| `resource_type` | string | `""` | Filter by resource type |
| `verb` | string | `""` | Filter by verb |
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |

**API Version:** common
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/permissions/options/`

**Example request:**
```json
{"method": "tools/call", "params": {"name": "list_permission_options", "arguments": {"field": "application"}}}
```

**Returns:** `{meta: {count}, links, data: ['value1', 'value2', ...]}`.

**Caveats:**
- Returns values from the permission registry, not from what is actively assigned.
- Application names are identifiers (e.g., `cost-management`), not display names.

---

### Access (1 tool)

#### list_access

List access permissions for a principal (V1 API only). By default shows access for the currently authenticated user.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `application` | string | _(required)_ | Application to filter by |
| `username` | string | `""` | Query another user's access (requires org admin) |
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `order_by` | string | `""` | Order by `application`, `resource_type`, or `verb` |
| `status` | string | `"enabled"` | Filter by status |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/access/`

**Example request:**
```json
{"method": "tools/call", "params": {"name": "list_access", "arguments": {"application": "cost-management"}}}
```

**Returns:** `{meta: {count}, links, data: [{permission, resourceDefinitions: [...]}]}`.

**Caveats:**
- V1-only endpoint. For V2 organizations, use `check_user_permission` or `list_role_bindings` instead.
- Shows flattened effective permissions but not which role or group granted each one. Use `list_group_roles` + `list_role_access` to trace the full chain.

---

### Roles (11 tools)

#### search_roles

Search and filter roles by name, permission, application, or other criteria. Automatically detects V1/V2 and routes accordingly.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `name` | string | `""` | Filter by role name (V2 supports `*` wildcards) |
| `display_name` | string | `""` | Filter by display name (V1 only) |
| `permission` | string | `""` | Filter by permission (comma-separated) |
| `application` | string | `""` | Filter by application |
| `system` | string | `""` | Filter by system role (`true`/`false`, V1 only) |
| `resource_type` | string | `""` | Filter by resource type (V2 only) |
| `order_by` | string | `""` | Order by `name`, `modified`, etc. |
| `username` | string | `""` | Filter by user (V1 only; ignored in V2) |

**API Version:** unified
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` (Kessel) / `role:read` (V1)
**Calls:** V1: `GET /api/v1/roles/` | V2: `GET /api/v2/roles/`

**Example request:**
```json
{"method": "tools/call", "params": {"name": "search_roles", "arguments": {"permission": "cost-management:cost_model:read"}}}
```

**Returns:** `{meta: {count}, links, data: [{uuid, name, description, ...}], org_version: 'v1'|'v2'}`.

**Caveats:**
- The permission filter does not account for ResourceDefinition filters that may narrow effective scope.
- The `username` filter is V1-only. For V2, it is ignored and returned in `ignored_filters`.

---

#### get_role

Get details of a specific role by UUID, including its permissions. Auto-detects V1/V2.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `role_uuid` | string | _(required)_ | UUID of the role |

**API Version:** unified
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `role:read`
**Calls:** V1: `GET /api/v1/roles/{uuid}/` + `GET /api/v1/roles/{uuid}/access/` | V2: `GET /api/v2/roles/{uuid}/`

**Returns:** `{uuid, name, description, permissions: [...], org_version}`.

---

#### check_role_permissions

Pre-flight check for a custom role: analyze what permissions it grants before assigning it to users. Only checks custom roles, not system/seeded roles.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `role_name` | string | _(required)_ | Name of the custom role |
| `include_available_permissions` | boolean | `false` | Also list permissions the role does NOT include |

**API Version:** unified
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `role:read`
**Calls:** In-process (direct ORM queries on Role/RoleV2 + Permission models)

**Returns:** `{role, permissions: {summary, by_application, expanded_permissions, verbs_included, verbs_not_included}, coverage_analysis, recommendations, org_version}`.

**Caveats:**
- Permission names are naming conventions only -- RBAC cannot confirm what UI elements or API endpoints they control.
- ResourceDefinition filters are stored but enforced by the consuming application, not RBAC.

---

#### list_role_access

List access permissions granted by a specific role (V1 API).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `role_uuid` | string | _(required)_ | UUID of the role |
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/roles/{uuid}/access/`

**Returns:** `{meta: {count}, links, data: [{permission, resourceDefinitions: [...]}]}`.

**Caveats:**
- Shows what the role grants in isolation. A user's effective access is the union of all roles across all groups.
- No workspace or scope concept in V1.

---

#### create_role_v1

Create a custom role (V1 API).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | _(required)_ | Role name |
| `display_name` | string | `""` | Display name |
| `description` | string | `""` | Description |
| `access` | list[object] | _(required)_ | List of permission objects (`{permission: 'app:resource:verb'}`) |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v1/roles/`

**Returns:** Created role object with uuid, name, and access list.

---

#### create_role

Create a custom role (V2 API).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | _(required)_ | Role name |
| `description` | string | `""` | Description |
| `permissions` | list[object] | _(required)_ | List of permission objects (`{application, resource_type, operation}`) |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v2/roles/`

**Returns:** Created role object with uuid, name, and permissions list.

---

#### update_role_v1

Update a custom role (V1 API, full replacement). Replaces the entire role including permissions.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `role_uuid` | string | _(required)_ | UUID of the role |
| `name` | string | _(required)_ | Role name |
| `display_name` | string | `""` | Display name |
| `description` | string | `""` | Description |
| `access` | list[object] | _(required)_ | List of permission objects |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `PUT /api/v1/roles/{uuid}/`

**Returns:** Updated role object.

---

#### patch_role_v1

Partially update a custom role (V1 API). Updates only the provided metadata fields. Does NOT update permissions -- use `update_role_v1` for that.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `role_uuid` | string | _(required)_ | UUID of the role |
| `name` | string | `""` | New name |
| `display_name` | string | `""` | New display name |
| `description` | string | `""` | New description |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `PATCH /api/v1/roles/{uuid}/`

**Returns:** Updated role object.

---

#### update_role

Update a custom role (V2 API, full replacement). Replaces the entire role including permissions.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `role_uuid` | string | _(required)_ | UUID of the role |
| `name` | string | _(required)_ | Role name |
| `description` | string | `""` | Description |
| `permissions` | list[object] | _(required)_ | List of permission objects (`{application, resource_type, operation}`) |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `PUT /api/v2/roles/{uuid}/`

**Returns:** Updated role object.

---

#### delete_role_v1

DESTRUCTIVE: Permanently delete a custom role (V1 API). All permissions and group assignments for this role are removed. System roles cannot be deleted.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `role_uuid` | string | _(required)_ | UUID of the role to delete |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `DELETE /api/v1/roles/{uuid}/`

**Returns:** `{status: 'deleted'}`.

---

#### bulk_delete_roles

DESTRUCTIVE: Permanently delete one or more roles in a single atomic operation (V2 API). All role bindings referencing the deleted roles are removed. If any UUID is not found, the entire operation fails.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ids` | list[string] | _(required)_ | List of role UUID strings |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v2/roles/:batchDelete`

**Returns:** `{status: 'deleted'}`.

---

### Groups (11 tools)

#### list_groups

List groups for the authenticated organization. Groups are collections of principals that can be assigned roles via policies.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `name` | string | `""` | Filter by name (partial match) |
| `username` | string | `""` | Groups a specific user belongs to |
| `role_names` | string | `""` | Groups with a specific role assigned |
| `order_by` | string | `""` | Order by `name`, `modified`, `principalCount`, `policyCount` |

**API Version:** common
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/groups/`

**Returns:** `{meta: {count}, links, data: [{uuid, name, description, principalCount, ...}]}`.

---

#### get_group

Get details of a specific group by UUID.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | _(required)_ | UUID of the group |

**API Version:** common
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/groups/{uuid}/`

**Returns:** `{uuid, name, description, principalCount, policyCount, roleCount, ...}`.

**Caveats:**
- The `Default access` group is a system group that all users belong to implicitly.
- Group membership changes are not versioned. Use `list_audit_logs` for change history.

---

#### list_group_principals

List principals (users) that are members of a specific group.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | _(required)_ | UUID of the group |
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `principal_type` | string | `""` | Filter by principal type |

**API Version:** common
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/groups/{uuid}/principals/`

**Returns:** `{meta: {count}, links, data: [{username, email, first_name, last_name, ...}]}`.

---

#### list_group_roles

List roles assigned to a specific group. Provide either `group_uuid` or `group_name` (case-insensitive).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | `""` | UUID of the group |
| `group_name` | string | `""` | Name of the group (case-insensitive lookup) |
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `order_by` | string | `""` | Order by `name`, `display_name`, `modified`, `policyCount` |
| `role_name` | string | `""` | Filter by role name |
| `role_description` | string | `""` | Filter by role description |
| `role_display_name` | string | `""` | Filter by display name |
| `role_system` | string | `""` | Filter by system role |
| `exclude` | string | `"false"` | `true` to list roles NOT in the group |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/groups/{uuid}/roles/`

**Returns:** `{meta: {count}, links, data: [{uuid, name, description, system, ...}]}`.

**Caveats:**
- Non-org-admin users cannot modify groups that contain roles with RBAC write permissions.

---

#### create_group

Create a new custom group. Works for both V1 and V2 organizations.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | _(required)_ | Group name |
| `description` | string | `""` | Description |

**API Version:** common
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v1/groups/`

**Returns:** Created group object with uuid, name, description.

---

#### update_group

Update a custom group (full replacement). System groups cannot be modified.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | `""` | UUID of the group |
| `group_name` | string | `""` | Name of the group (case-insensitive) |
| `name` | string | _(required)_ | New name |
| `description` | string | `""` | New description |

**API Version:** common
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `PUT /api/v1/groups/{uuid}/`

**Returns:** Updated group object.

---

#### add_principals_to_group

Add one or more principals (users) to a group. Works for both V1 and V2 organizations.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | `""` | UUID of the group |
| `group_name` | string | `""` | Name of the group (case-insensitive) |
| `principals` | list[string] | _(required)_ | List of usernames to add |

**API Version:** common
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v1/groups/{uuid}/principals/`

**Returns:** `{principals: [{username}], ...}`.

---

#### add_roles_to_group

Assign one or more roles to a group. V1 only -- use `create_role_bindings` for V2.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | `""` | UUID of the group |
| `group_name` | string | `""` | Name of the group (case-insensitive) |
| `roles` | list[string] | _(required)_ | List of role UUIDs to assign |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v1/groups/{uuid}/roles/`

**Returns:** Updated group-roles mapping.

---

#### delete_group

DESTRUCTIVE: Permanently delete a custom group. All role assignments and principal memberships are removed. System groups cannot be deleted.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | `""` | UUID of the group |
| `group_name` | string | `""` | Name of the group (case-insensitive) |

**API Version:** common
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `DELETE /api/v1/groups/{uuid}/`

**Returns:** `{status: 'deleted'}`.

---

#### remove_principals_from_group

DESTRUCTIVE: Remove one or more principals (users) from a group. At least one of `usernames` or `service_accounts` is required.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | `""` | UUID of the group |
| `group_name` | string | `""` | Name of the group (case-insensitive) |
| `usernames` | string | `""` | Comma-separated usernames |
| `service_accounts` | string | `""` | Comma-separated service account client IDs |

**API Version:** common
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `DELETE /api/v1/groups/{uuid}/principals/`

**Returns:** `{status: 'deleted'}`.

---

#### remove_roles_from_group

DESTRUCTIVE: Remove one or more roles from a group. The role-group association is deleted (the role itself is NOT deleted). V1 only -- use `update_role_binding` for V2.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | `""` | UUID of the group |
| `group_name` | string | `""` | Name of the group (case-insensitive) |
| `roles` | string | _(required)_ | Comma-separated role UUIDs |

**API Version:** v1
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `DELETE /api/v1/groups/{uuid}/roles/`

**Returns:** `{status: 'deleted'}`.

---

### Cross-Account / TAM (7 tools)

#### list_cross_account_requests

List cross-account access requests. These allow users from one org (e.g. Red Hat TAMs) to request temporary access to another org's resources.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `query_by` | string | `""` | `target_org` (requests INTO your org) or `user_id` (requests you made) |
| `status` | string | `""` | Filter: `pending`/`approved`/`denied`/`expired`/`cancelled` |
| `org_id` | string | `""` | Filter by org ID |
| `approved_only` | string | `""` | Filter for approved only |
| `order_by` | string | `""` | Order by `request_id`, `start_date`, `end_date`, `created`, `status` |

**API Version:** common
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/cross-account-requests/`

**Returns:** `{meta: {count}, links, data: [{request_id, target_org, status, start_date, end_date, ...}]}`.

**Caveats:**
- Approved requests have a time window -- check `end_date` against current date.
- Cross-account activity is not tracked in RBAC audit logs.
- Only org admins can approve or deny requests.

---

#### get_cross_account_request

Get details of a specific cross-account access request by its ID.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `request_id` | string | _(required)_ | ID of the request |

**API Version:** common
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v1/cross-account-requests/{request_id}/`

**Returns:** `{request_id, target_org, status, start_date, end_date, created, roles, ...}`.

---

#### investigate_tam_access

Investigate TAM (Technical Account Manager) cross-account access. Use when a TAM reports they cannot access a feature. Fetches approved requests, shows roles/permissions, and identifies permission gaps.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `requester_name` | string | `""` | Filter by requester name (case-insensitive substring) |
| `requester_email` | string | `""` | Filter by requester email |
| `status` | string | `"approved"` | Filter by status |
| `required_permission` | string | `""` | Check if this specific permission is granted |
| `limit` | integer | `20` | Max results |

**API Version:** common
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `role:read`
**Calls:** In-process (queries CrossAccountRequest + BOP proxy + Role/Access/Permission models)

**Returns:** `{requests: [{request_id, status, days_remaining, requester_info, roles: [{name, permissions}]}], analysis}`.

---

#### audit_redhat_access

Audit all Red Hat cross-account access into your organization. Returns a complete inventory of who has access, their roles/permissions, expiration dates, and RBAC changes they've made.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_inactive` | boolean | `false` | Include expired or pending requests |
| `audit_days` | integer | `30` | How far back to look in audit logs |
| `limit` | integer | `50` | Max results |

**API Version:** common
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `role:read`
**Calls:** In-process (queries CrossAccountRequest + AuditLog + BOP proxy)

**Returns:** `{active_access: [{user_info, roles, permissions, audit_activity}], summary: {total_users, expiring_soon, unused_access}}`.

---

#### create_cross_account_request

Create a cross-account access request for temporary access to another org's resources.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target_org` | string | _(required)_ | Org ID to request access to |
| `start_date` | string | _(required)_ | Start date (`YYYY-MM-DD`) |
| `end_date` | string | _(required)_ | End date (`YYYY-MM-DD`) |
| `roles` | list[string] | _(required)_ | List of role UUIDs |

**API Version:** common
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v1/cross-account-requests/`

**Returns:** Created request with request_id, status, dates.

---

#### update_cross_account_request

Update a cross-account access request (full replacement).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `request_id` | string | _(required)_ | ID of the request |
| `target_org` | string | _(required)_ | Target org ID |
| `start_date` | string | _(required)_ | Start date (`MM/DD/YYYY`) |
| `end_date` | string | _(required)_ | End date (`MM/DD/YYYY`) |
| `roles` | list[string] | _(required)_ | List of role display name strings |

**API Version:** common
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `PUT /api/v1/cross-account-requests/{id}/`

**Returns:** Updated request object.

**Caveats:**
- Date format differs from `create_cross_account_request`: this endpoint requires `MM/DD/YYYY`, while create uses `YYYY-MM-DD`. This is an asymmetry in the underlying API serializers.
- `roles` here takes role display name strings, not UUIDs (unlike create which takes UUIDs).

---

#### patch_cross_account_request

Partially update a cross-account access request (status change). Used to approve, deny, or cancel a request.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `request_id` | string | _(required)_ | ID of the request |
| `status` | string | _(required)_ | One of: `pending`, `approved`, `denied`, `cancelled`, `expired` |

**API Version:** common
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `PATCH /api/v1/cross-account-requests/{id}/`

**Returns:** Updated request object.

---

### Workspaces (6 tools)

#### list_workspaces

List workspaces for the authenticated organization (V2 API). Workspaces are hierarchical containers used to scope role bindings.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `order_by` | string | `""` | Order by `name`, `created`, `modified`, `type` |

**API Version:** common (calls V2 endpoint; requires V2 routes to be registered)
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v2/workspaces/`

**Returns:** `{meta: {count}, links, data: [{uuid, name, description, type, parent_id, created, ...}]}`.

---

#### get_workspace

Get details of a specific workspace by UUID (V2 API). Optionally includes the full ancestry chain.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace_uuid` | string | _(required)_ | UUID of the workspace |
| `include_ancestry` | boolean | `false` | Include full ancestry chain |

**API Version:** common (calls V2 endpoint; requires V2 routes to be registered)
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v2/workspaces/{uuid}/`

**Returns:** `{uuid, name, description, type, parent_id, created, modified, ...}`.

---

#### create_workspace

Create a workspace (V2 API). Workspaces are hierarchical containers for scoping role bindings.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | _(required)_ | Workspace name |
| `description` | string | `""` | Description |
| `parent_id` | string | `""` | UUID of parent workspace |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v2/workspaces/`

**Returns:** Created workspace object with uuid, name, type, parent_id.

---

#### update_workspace

Update a workspace (V2 API, full replacement). Root and ungrouped-hosts workspaces cannot be modified.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace_id` | string | _(required)_ | UUID of the workspace (named `workspace_id` in code; same format as `workspace_uuid` in other workspace tools) |
| `name` | string | _(required)_ | New name |
| `description` | string | `""` | New description |
| `parent_id` | string | `""` | Required for standard workspaces |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `PUT /api/v2/workspaces/{uuid}/`

**Returns:** Updated workspace object.

---

#### move_workspace

Move a workspace to a new parent (V2 API). Root and ungrouped-hosts workspaces cannot be moved.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace_id` | string | _(required)_ | UUID of the workspace (named `workspace_id` in code; same format as `workspace_uuid` in other workspace tools) |
| `parent_id` | string | _(required)_ | UUID of the new parent workspace |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v2/workspaces/{uuid}/move/`

**Returns:** Moved workspace object.

---

#### delete_workspace

DESTRUCTIVE: Permanently delete a workspace (V2 API). All role bindings scoped to this workspace are removed. Only STANDARD workspaces can be deleted. Cannot delete a workspace that has children.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace_uuid` | string | _(required)_ | UUID of the workspace |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `DELETE /api/v2/workspaces/{uuid}/`

**Returns:** `{status: 'deleted'}`.

---

### Role Bindings (4 tools)

#### list_role_bindings

List role bindings for the authenticated organization (V2 API). A role binding assigns a role to a subject (user/group) within a resource scope (e.g. workspace).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `role_id` | string | `""` | Filter by role UUID |
| `resource_type` | string | `""` | Filter by resource type |
| `resource_id` | string | `""` | Filter by resource UUID |
| `subject_type` | string | `""` | Filter by subject type |
| `subject_id` | string | `""` | Filter by subject UUID |
| `granted_subject_type` | string | `""` | Filter by granted subject type (e.g. `principal`) |
| `granted_subject_id` | string | `""` | Filter by granted subject UUID |
| `granted_subject_principal_user_id` | string | `""` | Filter by username (effective bindings including group membership) |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v2/role-bindings/`

**Returns:** `{meta: {count}, links, data: [{uuid, role, resource, subject, ...}]}`.

---

#### list_role_bindings_by_subject

List role bindings grouped by subject (V2 API). Shows which roles each subject has within a specific resource.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `resource_id` | string | _(required)_ | Resource UUID |
| `resource_type` | string | _(required)_ | Resource type (e.g. `workspace`) |
| `subject_type` | string | `""` | Filter by subject type |
| `subject_id` | string | `""` | Filter by subject UUID |
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Calls:** `GET /api/v2/role-bindings/by-subject/`

**Returns:** `{meta: {count}, links, data: [{subject, roles: [...], ...}]}`.

---

#### create_role_bindings

Create role bindings (V2 API). Assigns roles to subjects within resource scopes.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `bindings` | list[object] | _(required)_ | List of binding objects (each with `role`, `resource`, `subject`) |

Each binding needs: `role` (UUID string), `resource` (object with `type` and `id`), `subject` (object with `type` and `id` -- type is `principal` or `group`).

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `POST /api/v2/role-bindings/:batchCreate`

**Returns:** List of created role binding objects.

---

#### update_role_binding

Update role bindings for a specific subject on a resource (V2 API). Sets the exact list of roles -- any existing bindings not in the list are removed.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `resource_id` | string | _(required)_ | Resource UUID |
| `resource_type` | string | `"workspace"` | Resource type |
| `subject_id` | string | _(required)_ | Subject UUID |
| `subject_type` | string | _(required)_ | `principal` or `group` |
| `roles` | list[object] | _(required)_ | List of objects with `id` key (role UUIDs) |

**API Version:** v2
**Auth:** `x-rh-identity` required
**Write:** Yes (uses confirmation flow)
**Calls:** `PUT /api/v2/role-bindings/by-subject/`

**Returns:** Updated role binding state.

---

### Audit / Investigation (5 tools)

#### list_audit_logs

List audit log entries recording RBAC changes for the authenticated organization. Each entry records who changed what (principal, resource_type, action).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `10` | Max results |
| `offset` | integer | `0` | Pagination offset |
| `order_by` | string | `"-created"` | Order by `created`, `principal_username`, `resource_type`, `action` |
| `principal_username` | string | `""` | Filter by who made the change |
| `resource_type` | string | `""` | Filter: `group`/`role`/`role_v2`/`user`/`permission`/`workspace`/`role_binding` |
| `action` | string | `""` | Filter: `add`/`edit`/`delete`/`create`/`remove` |
| `group_name` | string | `""` | Filter by group name (substring in description) |
| `role_name` | string | `""` | Filter by role name (substring in description) |
| `include_authorization` | boolean | `false` | Include the role/permission that authorized each action |

**API Version:** common
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `admin:only`
**Calls:** In-process (queries AuditLog model directly)

**Returns:** `{meta: {count}, data: [{principal_username, action, resource_type, description, created, source, authorized_by?}]}`.

**Caveats:**
- Does NOT capture IP addresses, session IDs, login/logout events, or before/after state diffs.
- No server-side date range filter. Time-bounded queries require client-side pagination.
- Cross-account request approval/denial events may not appear here.

---

#### get_rbac_recent_changes

Get a summary of recent RBAC changes grouped by resource type, action, and actor.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | integer | `7` | How far back to look (1-30) |

**API Version:** common
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `admin:only`
**Calls:** In-process (queries AuditLog model)

**Returns:** `{summary: {days_reviewed, total_changes, unique_actors}, by_resource_type, by_action, by_actor, recent_changes}`.

---

#### investigate_group_changes

Investigate who changed a specific group. Returns audit log entries with authorization context for each actor.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_name` | string | _(required)_ | Name of the group |
| `role_name` | string | `""` | Filter by role name in change description |
| `action` | string | `""` | Filter by action type |
| `limit` | integer | `20` | Max entries (1-100) |
| `include_authorization` | boolean | `true` | Show what role/permission authorized each action |

**API Version:** common
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `group:read`
**Calls:** In-process (queries Group + AuditLog + BOP proxy + Access models)

**Returns:** `{group: {uuid, name, current_roles}, audit_entries: [{actor, action, description, created, authorized_by}], summary}`.

---

#### audit_group_for_dissolution

Audit a group before dissolving it. Shows all members, roles/permissions they'd lose, and identifies who would be left stranded (losing all non-default access).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_uuid` | string | `""` | UUID of the group |
| `group_name` | string | `""` | Name of the group (case-insensitive) |

**API Version:** common
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `group:read`
**Calls:** In-process (queries Group, Principal, Policy, Role, Access models)

**Returns:** `{group, members: [{username, type, other_groups, access_impact, is_stranded}], roles, analysis: {stranded_users, stranded_service_accounts, warnings}}`.

---

#### investigate_user_access

Investigate why a user has or lacks expected permissions, especially when they belong to multiple groups. Auto-detects V1/V2.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `username` | string | _(required)_ | Username or display name |
| `application` | string | `""` | Application to investigate |
| `expected_permission` | string | `""` | Expected permission (e.g. `write`) |
| `expected_verb` | string | `""` | Expected verb |

**API Version:** common
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `principal:read`
**Calls:** In-process (V1: Group/Policy/Role/Access ORM + `GET /api/v1/access/`; V2: RoleBinding/RoleBindingPrincipal/RoleBindingGroup ORM)

**Returns:** V1: `{user, org_version, groups: [{roles: [{permissions}]}], effective_access, analysis, permission_sources}`. V2: `{user, org_version, groups, role_bindings: [{role: {permissions}}], effective_access, analysis}`.

**Caveats:**
- Org admins bypass all RBAC checks; this tool returns only explicitly assigned permissions.
- In V1, roles are assigned to groups. In V2, roles are bound to subjects via role bindings.
- Permission names are naming conventions only.

---

### User State (4 tools)

#### check_user_permission

Check whether a specific user has a specific permission. Returns true/false with matched permission details. Auto-detects V1/V2.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `username` | string | _(required)_ | Username to check |
| `permission` | string | _(required)_ | Permission to check (`application:resource_type:verb`) |

**API Version:** unified
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `principal:read`
**Calls:** V1: `GET /api/v1/access/?username=X&application=Y` | V2: role-bindings -> roles resolution (in-process)

**Returns:** `{allowed: bool, username, permission, matched_permission, role_name, role_uuid, org_version}`.

**Caveats:**
- Org admins bypass all RBAC checks; this tool returns only explicitly assigned permissions.
- ResourceDefinition filters are enforced by the consuming application, not RBAC.

---

#### get_user_state

Get comprehensive state for a specific user in one call. Returns groups, roles, permissions, and audit activity. Auto-detects V1/V2.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `username` | string | _(required)_ | Username or display name |
| `include_group_roles` | boolean | `true` | Include roles for each group |
| `include_permissions` | boolean | `true` | Include all permissions |
| `audit_log_limit` | integer | `10` | Max audit log entries per group (1-100) |

**API Version:** unified
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `principal:read`
**Calls:** In-process (queries Group, AuditLog, Access/RoleBinding models; auto-detects V1/V2)

**Returns:** `{username, org_version, groups: [{name, roles, recent_activity}], access, user_actions: {total_count, by_group, by_type, recent}, summary, hints}`.

**Caveats:**
- Org admins have implicit full access not reflected in the returned data.

---

#### lookup_person

Alias for `get_user_state` that surfaces contractor/vendor/consultant/temp/intern terminology. Returns identical data.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `username` | string | _(required)_ | Username or display name |
| `include_group_roles` | boolean | `true` | Include roles for each group |
| `include_permissions` | boolean | `true` | Include all permissions |
| `audit_log_limit` | integer | `10` | Max audit log entries per group (1-100) |

**API Version:** unified
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `principal:read`
**Calls:** Delegates to `get_user_state`

**Returns:** Same as `get_user_state`.

---

#### guide_user_access_delegation

Check if a user can be delegated user access management without Org Admin privileges. Finds the "User Access administrator" system role and checks whether the user already has it.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `username` | string | _(required)_ | Username or display name |

**API Version:** unified
**Auth:** `x-rh-identity` required
**Permission:** `rbac_roles_read` / `role:read`
**Calls:** In-process (uses `search_roles`, `list_groups`, `list_principals` internally; queries RoleBinding/Group models for V2)

**Returns:** `{org_version, user_info, role_info, user_already_has_role, existing_assignments}`.

---

## Write Tools and Confirmation Flow

All 23 write tools are gated by a four-level security model:

1. **`MCP_WRITE_ENABLED` flag** (global kill switch) -- when `False`, write tools are visible in `tools/list` but annotated `[DISABLED]` and reject all calls.
2. **API version gating** -- V1-only tools are blocked for V2 organizations and vice versa.
3. **Two-phase confirmation** (`MCP_WRITE_CONFIRMATION`, default `True`) -- first call returns a human-readable preview and a single-use confirmation token. Second call with matching token executes the operation.
4. **Django view permission checks** -- standard RBAC permission enforcement.

### Confirmation token details

- Tokens are SHA-256 hashes bound to: tool name + arguments + org_id.
- Stored in Django cache (Redis in production, LocMemCache in tests).
- TTL: 300 seconds.
- Single-use: consumed on successful execution.
- The `confirmation_token` parameter is stripped from arguments before dispatch so tools never see it.

### Write tool example (two-phase)

**Phase 1 -- Preview:**
```json
{"method": "tools/call", "params": {"name": "create_group", "arguments": {"name": "Engineering"}}}
```
Response includes a preview of the operation and a `confirmation_token`.

**Phase 2 -- Execute:**
```json
{"method": "tools/call", "params": {"name": "create_group", "arguments": {"name": "Engineering", "confirmation_token": "<token>"}}}
```
Executes the operation and returns the created group.

---

## API Version Gating

Tools declare their API version compatibility via the `api_version` parameter on `@register_tool`:

| API Version | Behavior |
|-------------|----------|
| `UNVERSIONED` | No auth required, always visible |
| `COMMON` | Works for both V1 and V2 organizations |
| `UNIFIED` | Auto-detects V1/V2 via `is_v2_write_activated()` and routes accordingly |
| `V1` | V1-only; blocked for V2 organizations with guidance to use V2 equivalent |
| `V2` | V2-only; hidden when `V2_APIS_ENABLED=False` |

Tools with `api_version=ApiVersion.UNIFIED` (e.g., `search_roles`, `get_role`, `check_user_permission`) provide the best experience by automatically detecting the organization's version and using the appropriate API path.

---

## Adding New Tools

Use the `@register_tool` decorator to add new tools. It handles both FastMCP schema registration and sync execution configuration automatically.

### Basic tool (no auth)

```python
@register_tool(description="Describe what the tool does")
def my_tool(param1: str, param2: int = 10) -> str:
    """Implement the tool logic."""
    result = do_something(param1, param2)
    return json.dumps(result)
```

### Authenticated tool (receives Django request)

```python
@register_tool(description="Tool that needs user context", requires_auth=True)
def my_auth_tool(request: HttpRequest, *, limit: int = 10) -> str:
    """Tool that delegates to an existing Django view."""
    path = reverse("v1_management:some-view")
    view_request = _clone_request(request, path, data={"limit": str(limit)})
    response = _some_view(view_request)
    return json.dumps(response.data, default=str)
```

### Write tool (with confirmation flow)

```python
@register_tool(
    description="Create something",
    requires_auth=True,
    write=True,
    api_version=ApiVersion.V1,
)
def create_something(request: HttpRequest, *, name: str) -> str:
    """Write tool with automatic confirmation gating."""
    body = {"name": name}
    path = reverse("v1_management:something-list")
    return _call_view_write(request, _something_create_view, path, body)
```

### Decorator parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `description` | str | _(required)_ | Shown in `tools/list` (overridable via Redis) |
| `requires_auth` | bool | `False` | Requires `x-rh-identity` header |
| `api_version` | ApiVersion | `COMMON` | Version gating behavior |
| `write` | bool | `False` | Gated by `MCP_WRITE_ENABLED` + confirmation flow |
| `required_relation` | str | `""` | Kessel relation check for V2 orgs |
| `required_resource_type` | str | `""` | Kessel resource type for V2 orgs |
| `v1_permission` | tuple | `()` | V1 permission fallback (e.g., `("role", "read")`) |
| `caveats` | str | `""` | Appended as separate content block in tool results |

### How it works

1. The decorator inspects the function signature. If the first parameter is `request`, it creates a schema-only wrapper (without `request`) for FastMCP, so the JSON schema only includes user-facing parameters.
2. `requires_auth=True` means the tool will reject calls without a valid identity header.
3. `_clone_request()` copies auth context (user, tenant, identity header) and tracing headers (`X-Request-ID`, `X-RH-Insights-Request-ID`) from the MCP request to the internal view request.
4. Tool functions must return a `str`. Use `json.dumps()` for structured output.

---

## Local Development

### Start the server

```bash
# With development mode (auto identity injection)
DEVELOPMENT=True make serve

# Or with manual identity header
make serve
```

### Full session example

```bash
# 1. Initialize
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{}}}' | python3 -m json.tool

# 2. List available tools
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":2,"params":{}}' | python3 -m json.tool

# 3. Call hello (no auth needed)
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","id":3,"params":{"name":"hello","arguments":{"message":"Hi!"}}}' | python3 -m json.tool

# 4. Call list_principals (needs auth -- use dev mode or provide x-rh-identity)
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","id":4,"params":{"name":"list_principals","arguments":{"limit":3}}}' | python3 -m json.tool

# 5. End session
curl -X DELETE http://localhost:8000/_private/_a2s/mcp/
```

### Run tests

```bash
# MCP tests only
tox -e py312 -- tests.management.test_mcp_views

# Full test suite
tox -r
```

---

## Tool Execution Timeout

All tool calls are subject to a configurable timeout to prevent slow or hanging dependencies from blocking MCP requests indefinitely.

| Setting | Default | Description |
|---------|---------|-------------|
| `MCP_TOOL_TIMEOUT_SECONDS` | `30` | Maximum seconds a tool may execute before being terminated |
| `MCP_TOOL_MAX_WORKERS` | `10` | Thread pool size for concurrent tool execution |

When a tool exceeds the timeout, the MCP server returns a JSON-RPC error:

```json
{
  "jsonrpc": "2.0",
  "error": {"code": -32603, "message": "Tool execution timed out after 30s"},
  "id": 3
}
```

Timeouts are recorded in the `rbac_mcp_tool_call_total` Prometheus metric with `status="timeout"`.

The timeout uses a module-level `concurrent.futures.ThreadPoolExecutor` (reused across requests) which is safe in multi-threaded WSGI containers (unlike `signal.alarm` which only works on the main thread).

---

## Error Handling

All errors follow the JSON-RPC 2.0 error response format:

```json
{
  "jsonrpc": "2.0",
  "error": {"code": -32602, "message": "Unknown tool: foo"},
  "id": 3
}
```

### Error codes

| Code | Meaning | Example |
|------|---------|---------|
| `-32700` | Parse error | Malformed JSON body |
| `-32600` | Invalid Request | Missing `jsonrpc: "2.0"`, batch requests, non-string method |
| `-32601` | Method not found | Unknown JSON-RPC method (not `initialize`/`tools/list`/`tools/call`) |
| `-32602` | Invalid params | Unknown tool name, missing `arguments`, invalid argument types |
| `-32603` | Internal error | Unhandled exception in tool execution, or tool execution timed out |
| `-32000` | Authentication required | Auth-required tool called without valid identity |

---

## Key Files Reference

| File | Description |
|------|-------------|
| `rbac/management/mcp_views.py` | MCP endpoint implementation: `MCPView`, 54 tools, JSON-RPC handling, `@register_tool` decorator |
| `rbac/management/mcp_urls.py` | URL routing for MCP endpoint |
| `rbac/rbac/a2s.py` | `is_a2s_path()` helper shared by both middleware classes |
| `rbac/rbac/middleware.py` | A2S bypass in `IdentityHeaderMiddleware` |
| `rbac/internal/middleware.py` | A2S bypass in `InternalIdentityHeaderMiddleware` |
| `rbac/rbac/settings.py` | `A2S_PATH_PREFIX`, `CORS_EXPOSE_HEADERS`, `MCP_TOOL_TIMEOUT_SECONDS`, `MCP_TOOL_MAX_WORKERS`, `MCP_WRITE_ENABLED`, `MCP_WRITE_CONFIRMATION` settings |
| `rbac/rbac/urls.py` | Top-level URL routing for `/_private/_a2s/` |
| `tests/management/test_mcp_views.py` | Comprehensive test suite (419 tests) |
