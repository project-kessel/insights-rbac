# RBAC MCP Endpoint — Operator Guide

This document provides comprehensive operator-facing documentation for the RBAC Model Context Protocol (MCP) endpoint: what it does, how to deploy it, how to configure it, what permissions and credentials it requires, security considerations, observability, and usage examples.

For developer-facing documentation (adding tools, protocol details, code structure), see [MCP.md](MCP.md).

---

## Table of Contents

- [Purpose](#purpose)
- [Architecture Overview](#architecture-overview)
- [Installation & Dependencies](#installation--dependencies)
- [Configuration Reference](#configuration-reference)
  - [MCP Settings](#mcp-settings)
  - [Related Infrastructure Settings](#related-infrastructure-settings)
- [Deployment](#deployment)
  - [Enabling the MCP Endpoint](#enabling-the-mcp-endpoint)
  - [Write Operations](#write-operations)
  - [Infrastructure Requirements](#infrastructure-requirements)
- [Authentication & Authorization](#authentication--authorization)
  - [Identity Header Authentication](#identity-header-authentication)
  - [A2S Routing](#a2s-routing)
  - [Tool-Level Authorization](#tool-level-authorization)
  - [Write Confirmation Protocol](#write-confirmation-protocol)
- [Security Considerations](#security-considerations)
  - [Network Access Control](#network-access-control)
  - [Write Mode Security](#write-mode-security)
  - [Data Exposure](#data-exposure)
  - [Denial of Service Protection](#denial-of-service-protection)
- [Available Tools](#available-tools)
  - [Read-Only Tools](#read-only-tools)
  - [Investigation & Audit Tools](#investigation--audit-tools)
  - [Write Tools](#write-tools)
- [Observability](#observability)
  - [Prometheus Metrics](#prometheus-metrics)
  - [Logging](#logging)
  - [Request Tracing](#request-tracing)
- [Usage Examples](#usage-examples)
  - [Connectivity Check](#connectivity-check)
  - [Full MCP Session](#full-mcp-session)
  - [Authenticated Tool Call](#authenticated-tool-call)
  - [Write Operation with Confirmation](#write-operation-with-confirmation)
  - [Configuring an MCP Client](#configuring-an-mcp-client)
- [Troubleshooting](#troubleshooting)
- [Reference](#reference)

---

## Purpose

The MCP endpoint enables AI agents (Claude, ChatGPT, custom assistants) to discover and invoke RBAC operations via the standard [Model Context Protocol](https://modelcontextprotocol.io/) JSON-RPC 2.0 interface. It exposes 50+ tools covering:

- **User & group management** — list principals, groups, memberships
- **Role & permission queries** — search roles, check permissions, list access
- **Audit & investigation** — audit logs, recent changes, user access investigation, cross-account access analysis
- **Workspace operations** (V2) — list/manage workspaces, role bindings
- **Write operations** (optional) — create/update/delete groups, roles, role bindings, workspaces

The endpoint delegates all operations to existing Django views, ensuring that MCP tools respect the same business logic, tenant isolation, and permission checks as the REST API.

---

## Architecture Overview

```
MCP Client (AI Agent)
    │
    │  JSON-RPC 2.0 over HTTP POST
    ▼
┌─────────────────────────────────────────────────────┐
│  /_private/_a2s/mcp/                                │
│                                                      │
│  IdentityHeaderMiddleware                            │
│  (A2S paths use public x-rh-identity auth)           │
├─────────────────────────────────────────────────────┤
│  MCPView.post()                                      │
│  ├─ initialize        → protocol handshake           │
│  ├─ tools/list        → tool catalog (JSON schemas)  │
│  └─ tools/call        → tool execution               │
│       ├─ Auth check (org_id, Kessel/V1 perms)        │
│       ├─ Write confirmation (if enabled)             │
│       ├─ _clone_request() → existing Django views    │
│       └─ Timeout enforcement (ThreadPoolExecutor)    │
└─────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────┐  ┌────────────────┐
│ PostgreSQL (RBAC data)   │  │ Redis (cache,  │
│                          │  │ confirmations) │
└──────────────────────────┘  └────────────────┘
```

**Key design decisions:**

| Decision | Rationale |
|----------|-----------|
| Synchronous WSGI execution | Tools access the Django ORM directly; async dispatch would trigger `SynchronousOnlyOperation` |
| Delegation to existing views | Reuses all existing permission checks, serializers, and business logic |
| A2S path prefix | Separates agent auth (public identity header) from internal auth (PSK/JWT) |
| FastMCP for schema only | Uses the Anthropic MCP SDK for JSON schema generation but not for request dispatch |

---

## Installation & Dependencies

The MCP endpoint is part of the standard RBAC application — no separate installation is needed. The only additional Python dependency is:

```
mcp >= 1.26.0
```

This is the [Anthropic MCP Python SDK](https://pypi.org/project/mcp/), used for tool registration and JSON schema generation.

**Infrastructure dependencies** (shared with the main RBAC application):

| Component | Purpose for MCP |
|-----------|-----------------|
| PostgreSQL | All RBAC data (roles, groups, permissions, audit logs) |
| Redis | Write confirmation token storage, tool description overrides |
| Kessel Inventory API | V2 workspace-scoped authorization checks (if V2 enabled) |

No additional infrastructure is required beyond what the RBAC service already uses.

---

## Configuration Reference

### MCP Settings

All MCP settings are read from environment variables with sensible defaults. The MCP endpoint works with zero configuration — it is enabled by default in read-only mode.

| Environment Variable | Type | Default | Description |
|---------------------|------|---------|-------------|
| `MCP_ENABLED` | bool | `True` | Master switch for the MCP endpoint. When `False`, the `/_private/_a2s/mcp/` path is not registered and returns 404. |
| `MCP_TOOL_TIMEOUT_SECONDS` | int | `30` | Maximum execution time (seconds) for any single tool call. Tools that exceed this are terminated and return a JSON-RPC error. Set to `0` to disable timeouts (not recommended in production). |
| `MCP_TOOL_MAX_WORKERS` | int | `10` | Size of the thread pool used for tool execution with timeout enforcement. Each concurrent tool call uses one thread. |
| `MCP_WRITE_ENABLED` | bool | `False` | Enable write operations (create, update, delete). When `False`, write tools are listed as `[DISABLED]` in tool discovery and return an error if called. |
| `MCP_WRITE_CONFIRMATION` | bool | `True` | Require two-phase confirmation for write operations. Only effective when `MCP_WRITE_ENABLED=True`. When `True`, write tools return a preview and confirmation token on first call; the operation only executes when the token is sent back with the same arguments. |
| `MCP_WRITE_CONFIRMATION_TTL` | int | `300` | Time-to-live (seconds) for write confirmation tokens. Tokens expire after this period and must be re-issued. |

### Related Infrastructure Settings

These existing RBAC settings affect MCP behavior:

| Environment Variable | Default | MCP Impact |
|---------------------|---------|------------|
| `A2S_PATH_PREFIX` | `/_private/_a2s/` | URL prefix for the MCP endpoint. Change only if you need a different path for routing. |
| `V2_APIS_ENABLED` | `False` | When `True`, V2-specific tools (workspaces, role bindings, V2 roles) are available. When `False`, V2 tools are hidden from tool discovery and rejected if called. |
| `DEVELOPMENT` | `False` | When `True`, the development middleware auto-injects a test identity header, allowing unauthenticated MCP calls for local testing. **Never enable in production.** |
| `REDIS_URL` / `REDIS_HOST` / `REDIS_PORT` | varies | Redis connection for write confirmation tokens (stored via Django's cache framework) and tool description overrides. |

---

## Deployment

### Enabling the MCP Endpoint

The MCP endpoint is **enabled by default** (`MCP_ENABLED=True`) in read-only mode. No configuration is needed for basic deployment. To explicitly disable it:

```bash
MCP_ENABLED=False
```

When disabled:
- The `/_private/_a2s/mcp/` URL is not registered
- A2S path detection returns `False`, so requests fall through to standard `/_private/` internal auth and return 404
- No performance overhead — disabled at the URL routing level

### Write Operations

Write operations are **disabled by default** and must be explicitly enabled:

```bash
# Enable write tools
MCP_WRITE_ENABLED=True

# Optional: disable write confirmation (not recommended)
# MCP_WRITE_CONFIRMATION=False
```

When write mode is enabled with confirmation (the default), the deployment requires:
- **Redis** — for storing confirmation tokens (uses Django's cache backend)
- All existing RBAC infrastructure (PostgreSQL, Kessel if V2)

**Recommended deployment progression:**

1. **Stage 1 — Read-only** (default): Deploy with no MCP configuration changes. Agents can query RBAC data but cannot modify it.
2. **Stage 2 — Write with confirmation**: Set `MCP_WRITE_ENABLED=True`. All writes require two-phase confirmation with human approval.
3. **Stage 3 — Write without confirmation** (use caution): Set `MCP_WRITE_ENABLED=True` and `MCP_WRITE_CONFIRMATION=False`. Writes execute immediately. Only appropriate for fully trusted, non-interactive automation.

### Infrastructure Requirements

| Component | Required for | Notes |
|-----------|-------------|-------|
| PostgreSQL | All operations | Same database as the main RBAC service |
| Redis | Write confirmations, description overrides | Uses the shared Redis from Django cache config. Read-only mode works without Redis (description overrides gracefully degrade). |
| Kessel Inventory API | V2 tool authorization | Only needed when `V2_APIS_ENABLED=True`. Tools that require Kessel permissions fail closed if Kessel is unreachable. |
| BOP (Billing & Org Platform) | `list_principals` | Principal data is fetched from BOP via the existing proxy. If BOP is unreachable, `list_principals` returns an error. |

---

## Authentication & Authorization

### Identity Header Authentication

The MCP endpoint uses the same `x-rh-identity` header as the public RBAC REST API. This is a base64-encoded JSON object containing the user's identity, organization, and entitlements. It is typically injected by the platform's authentication proxy (3Scale / Turnpike).

```
x-rh-identity: eyJpZGVudGl0eSI6eyJ0eXBlIjoiVXNlciIsIm9yZ19pZCI6IjEyMzQ1Njc4IiwidXNlciI6eyJ1c2VybmFtZSI6ImpvaG4uZG9lIn19fQ==
```

Decoded:
```json
{
  "identity": {
    "type": "User",
    "org_id": "12345678",
    "user": {
      "username": "john.doe"
    }
  }
}
```

### A2S Routing

The `/_private/_a2s/` prefix creates a special authentication routing seam:

| Path Pattern | Auth Method | Used By |
|-------------|-------------|---------|
| `/api/rbac/v1/*` | `x-rh-identity` (public) | REST API consumers |
| `/_private/*` (except `_a2s`) | PSK / JWT (internal) | Service-to-service calls |
| `/_private/_a2s/*` | `x-rh-identity` (public) | MCP / agent-to-service calls |

This means:
- MCP requests go through the same `IdentityHeaderMiddleware` as public API requests
- No PSK or service account tokens are needed — the calling agent must present a valid user identity
- Unauthenticated requests are allowed through (for tools like `hello` that don't require auth), but auth-required tools will reject them with a JSON-RPC error

### Tool-Level Authorization

Authorization is enforced at multiple layers:

| Layer | Check | Failure Response |
|-------|-------|-----------------|
| **Tool auth** | `requires_auth=True` tools require valid `org_id` in identity header | JSON-RPC error `-32000` ("Authentication required") |
| **V1 permissions** | Some tools check the user's RBAC access dict for `(resource_type, verb)` tuples | JSON-RPC error `-32003` ("Permission denied") |
| **Kessel relations** (V2) | V2 tools check Kessel Inventory API for workspace-scoped relations | JSON-RPC error `-32003` ("Permission denied") |
| **Org admin bypass** | Org admins (`admin: true` in identity) bypass V1 permission checks | Automatic grant |
| **View-level checks** | Delegated Django views run their own permission checks | HTTP error wrapped in JSON-RPC response |
| **Tenant isolation** | All queries are scoped to `request.tenant` (derived from `org_id`) | Data filtered at the ORM level |

### Write Confirmation Protocol

When `MCP_WRITE_ENABLED=True` and `MCP_WRITE_CONFIRMATION=True` (the default), all write operations use a two-phase confirmation protocol:

```
Phase 1: Agent calls write tool → Server returns preview + confirmation_token
Phase 2: Agent shows preview to user → User approves → Agent calls tool again with token
```

**Token properties:**
- Generated as UUID4 hex string
- Stored in Django cache (Redis) with configurable TTL (default: 300 seconds)
- Bound to: tool name, argument hash (SHA-256), and org_id
- Single-use: consumed on successful validation
- Tamper-resistant: changing arguments after token issuance invalidates it

**Failure modes:**
- Expired token → "Confirmation token is invalid or expired"
- Wrong tool → "Token was issued for 'X', not 'Y'"
- Changed arguments → "Arguments have changed since the confirmation was issued"
- Wrong org → "Token was issued for a different organization"

---

## Security Considerations

### Network Access Control

The MCP endpoint lives under `/_private/_a2s/` — a path prefix that should be **restricted to internal networks**. Even though it uses the same identity header as public APIs, it is designed for agent-to-service communication within the platform, not direct external access.

**Recommendations:**
- Route `/_private/_a2s/mcp/` only from trusted internal networks (service mesh, internal load balancer)
- Apply the same network policies as other `/_private/` endpoints
- Do not expose `/_private/_a2s/mcp/` through public-facing ingress or 3Scale
- If using a service mesh, require mTLS for MCP traffic

### Write Mode Security

Write operations are **disabled by default** for defense in depth. When enabling writes:

| Risk | Mitigation |
|------|------------|
| Unauthorized modifications | Write confirmation protocol requires explicit user approval for each operation |
| Agent hallucination/mistakes | Two-phase confirmation lets users review the exact action before execution |
| Replay attacks | Confirmation tokens are single-use and expire after `MCP_WRITE_CONFIRMATION_TTL` seconds |
| Argument tampering | Token is bound to SHA-256 hash of tool name + arguments; any change invalidates it |
| Cross-org token use | Token is bound to the issuing org_id |
| Bulk damage | Write tools delegate to existing views, which enforce their own permission checks and rate limits |

**If you disable write confirmation** (`MCP_WRITE_CONFIRMATION=False`):
- Write tools execute immediately without user approval
- Only appropriate for fully automated, trusted pipelines
- Ensure the calling agent's identity has only the minimum necessary permissions
- Monitor `rbac_mcp_tool_call_total` metrics closely for unexpected write patterns

### Data Exposure

- All tool responses respect tenant isolation — data from other organizations is never returned
- Investigation tools (e.g., `investigate_user_access`, `audit_redhat_access`) may return detailed permission and audit data. Ensure the calling agent handles this data appropriately.
- The `tools/list` response includes tool names and input schemas but no tenant-specific data
- Tool description overrides stored in Redis (`mcp:desc:*` keys) are shared across all organizations

### Denial of Service Protection

| Protection | Mechanism |
|-----------|-----------|
| Tool execution timeout | `MCP_TOOL_TIMEOUT_SECONDS` (default 30s) prevents hanging tools from blocking resources |
| Thread pool limit | `MCP_TOOL_MAX_WORKERS` (default 10) caps concurrent tool executions per WSGI worker |
| Existing rate limits | Delegated views inherit the RBAC service's existing rate limiting |
| No batch requests | JSON-RPC batch requests are explicitly rejected |
| No SSE streaming | GET requests return 405 — no long-lived connections |

---

## Available Tools

Tools are organized by category and gated by API version and write mode.

### Read-Only Tools

These tools are always available when the MCP endpoint is enabled.

| Tool | Auth | Description |
|------|------|-------------|
| `hello` | No | Connectivity check. Echoes a message with server UTC timestamp. |
| `get_status` | Yes | Server status (API version, platform info). |
| `list_principals` | Yes | List users in the authenticated organization. Supports pagination, filtering, sorting. |
| `list_groups` | Yes | List custom groups with pagination. |
| `get_group` | Yes | Get a single group by UUID. |
| `list_group_principals` | Yes | List users within a specific group. |
| `list_group_roles` | Yes | List roles assigned to a group (V1). |
| `search_roles` | Yes | Search roles by name, display name, or permission. Auto-detects V1/V2. |
| `get_role` | Yes | Get role details by UUID. Auto-detects V1/V2. |
| `check_role_permissions` | Yes | Pre-flight analysis of what a role grants. |
| `list_role_access` | Yes | List permission entries granted by a V1 role. |
| `list_permissions` | Yes | List available RBAC permissions. |
| `list_permission_options` | Yes | Distinct values for permission fields. |
| `list_access` | Yes | Effective permissions for the authenticated user (V1). |
| `check_user_permission` | Yes | Check if a specific user has a specific permission. |
| `list_cross_account_requests` | Yes | List TAM/cross-account access requests. |
| `get_cross_account_request` | Yes | Get a single cross-account request by ID. |
| `list_audit_logs` | Yes | Query the RBAC audit log with date/action/resource filters. |

**V2-only tools** (require `V2_APIS_ENABLED=True`):

| Tool | Auth | Description |
|------|------|-------------|
| `list_workspaces` | Yes | List workspaces in the organization. |
| `get_workspace` | Yes | Get a single workspace by ID. |
| `list_role_bindings` | Yes | List role bindings. |
| `list_role_bindings_by_subject` | Yes | List role bindings for a specific subject. |

### Investigation & Audit Tools

These are higher-level tools that combine data from multiple sources for analysis:

| Tool | Description |
|------|-------------|
| `get_rbac_recent_changes` | Summarize recent RBAC changes from the audit log. |
| `investigate_group_changes` | Analyze modifications to a specific group over time. |
| `investigate_user_access` | Deep analysis of a user's effective permissions, group memberships, and roles. |
| `investigate_tam_access` | Cross-account (TAM) access investigation for a specific user. |
| `audit_redhat_access` | Full cross-account access audit across the organization. |
| `get_user_state` | Internal view of a user's current permissions and group memberships. |

### Write Tools

These tools require `MCP_WRITE_ENABLED=True`. When write confirmation is enabled (default), each tool requires a two-phase call.

**Create operations:**

| Tool | API Version | Description |
|------|-------------|-------------|
| `create_group` | V1/V2 | Create a new custom group. |
| `add_principals_to_group` | V1/V2 | Add users to a group. |
| `add_roles_to_group` | V1 | Assign roles to a group. |
| `create_role_v1` | V1 | Create a custom role with permissions. |
| `create_role` | V2 | Create a V2 role. |
| `create_role_bindings` | V2 | Create role bindings (batch). |
| `create_workspace` | V2 | Create a workspace. |
| `create_cross_account_request` | V1/V2 | Create a cross-account access request. |

**Update operations:**

| Tool | API Version | Description |
|------|-------------|-------------|
| `update_group` | V1/V2 | Update group name/description. |
| `update_role_v1` | V1 | Full replacement of a V1 role. |
| `patch_role_v1` | V1 | Partial update of a V1 role. |
| `update_role` | V2 | Full replacement of a V2 role. |
| `update_role_binding` | V2 | Replace role bindings for a subject. |
| `update_workspace` | V2 | Update workspace properties. |
| `move_workspace` | V2 | Move a workspace in the hierarchy. |
| `update_cross_account_request` | V1/V2 | Full update of a cross-account request. |
| `patch_cross_account_request` | V1/V2 | Partial update of a cross-account request. |

**Delete operations:**

| Tool | API Version | Description |
|------|-------------|-------------|
| `delete_group` | V1/V2 | Permanently delete a group. |
| `remove_principals_from_group` | V1/V2 | Remove users from a group. |
| `remove_roles_from_group` | V1 | Remove roles from a group. |
| `delete_role_v1` | V1 | Permanently delete a V1 role. |
| `bulk_delete_roles` | V2 | Bulk delete V2 roles by ID. |
| `delete_workspace` | V2 | Permanently delete a workspace. |

---

## Observability

### Prometheus Metrics

The MCP endpoint exports two Prometheus metrics (the `hello` tool is excluded from metrics to avoid noise):

**`rbac_mcp_tool_call_total`** (Counter)

Total number of MCP tool calls, labeled by `tool` and `status`.

| Status Label | Meaning |
|-------------|---------|
| `success` | Tool executed successfully |
| `auth_error` | Tool rejected due to missing authentication |
| `permission_denied` | Tool rejected due to insufficient permissions |
| `timeout` | Tool execution exceeded `MCP_TOOL_TIMEOUT_SECONDS` |
| `error` | Unhandled exception during tool execution |
| `invalid_params` | Invalid parameters passed to the tool |
| `confirmation_pending` | Write tool returned a confirmation token (first phase) |
| `confirmation_failed` | Write tool confirmation token was invalid or expired |

**`rbac_mcp_tool_call_duration_seconds`** (Histogram)

Duration of MCP tool calls in seconds, labeled by `tool`. Only recorded for successful completions and errors (not for auth rejections or confirmation phases).

**Example PromQL queries:**

```promql
# Error rate by tool (last 5 minutes)
rate(rbac_mcp_tool_call_total{status!="success"}[5m])

# P95 tool execution latency
histogram_quantile(0.95, rate(rbac_mcp_tool_call_duration_seconds_bucket[5m]))

# Write operations per hour
increase(rbac_mcp_tool_call_total{status="success", tool=~"create_.*|update_.*|delete_.*|patch_.*|add_.*|remove_.*|bulk_.*|move_.*"}[1h])

# Timeout rate
rate(rbac_mcp_tool_call_total{status="timeout"}[5m]) / rate(rbac_mcp_tool_call_total[5m])
```

### Logging

The MCP endpoint logs to the `management.mcp_views` logger at the following levels:

| Level | Events |
|-------|--------|
| `INFO` | Every JSON-RPC method dispatch (`mcp: method=<method>, org_id=<org>`), tool calls with argument keys, tool completion with duration, initialization with client info |
| `WARNING` | Parse errors, unknown methods, unknown tools, auth rejections, permission denials, confirmation failures |
| `ERROR` | Tool execution timeouts, unhandled exceptions |
| `DEBUG` | Redis unavailability for description overrides (non-fatal) |

Log entries include `org_id` and `req_id` for correlation.

### Request Tracing

The MCP endpoint forwards tracing headers from the original request to delegated view calls:

- `X-Request-ID`
- `X-RH-Insights-Request-ID`

This ensures that MCP tool calls appear in distributed traces alongside the originating request.

---

## Usage Examples

### Connectivity Check

Verify the MCP endpoint is running (no authentication required):

```bash
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 1,
    "params": {
      "name": "hello",
      "arguments": {"message": "ping"}
    }
  }' | python3 -m json.tool
```

Expected response:
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"response\": \"RBAC received your message: 'ping'\", \"date\": \"2025-06-01 12:00:00 UTC\"}"
      }
    ],
    "isError": false
  },
  "id": 1
}
```

### Full MCP Session

A complete MCP session follows the initialize → discover → call → terminate lifecycle:

```bash
# 1. Initialize session
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "ops-agent", "version": "1.0"}
    }
  }' | python3 -m json.tool

# 2. Send initialized notification
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}'
# Returns HTTP 202

# 3. Discover tools
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}' | python3 -m json.tool

# 4. Call a tool (see examples below)

# 5. End session
curl -X DELETE http://localhost:8000/_private/_a2s/mcp/
```

### Authenticated Tool Call

Calling a tool that requires authentication:

```bash
# Base64-encode the identity
IDENTITY=$(echo -n '{"identity":{"type":"User","org_id":"12345678","user":{"username":"admin","is_org_admin":true}}}' | base64)

curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -H "x-rh-identity: ${IDENTITY}" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 3,
    "params": {
      "name": "list_principals",
      "arguments": {"limit": 5, "sort_order": "desc"}
    }
  }' | python3 -m json.tool
```

### Write Operation with Confirmation

When `MCP_WRITE_ENABLED=True` and `MCP_WRITE_CONFIRMATION=True`:

```bash
IDENTITY=$(echo -n '{"identity":{"type":"User","org_id":"12345678","user":{"username":"admin","is_org_admin":true}}}' | base64)

# Phase 1: Request the write — server returns preview + token
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -H "x-rh-identity: ${IDENTITY}" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 10,
    "params": {
      "name": "create_group",
      "arguments": {"name": "Engineering Team", "description": "Backend engineers"}
    }
  }' | python3 -m json.tool
```

Response (Phase 1):
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"confirmation_required\": true, \"message\": \"This will create a new group 'Engineering Team'.\", \"confirmation_token\": \"a1b2c3d4e5f6...\"}"
      }
    ],
    "isError": false
  },
  "id": 10
}
```

```bash
# Phase 2: Confirm with the token (arguments must match exactly)
curl -s -X POST http://localhost:8000/_private/_a2s/mcp/ \
  -H "Content-Type: application/json" \
  -H "x-rh-identity: ${IDENTITY}" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 11,
    "params": {
      "name": "create_group",
      "arguments": {
        "name": "Engineering Team",
        "description": "Backend engineers",
        "confirmation_token": "<token-from-phase-1>"
      }
    }
  }' | python3 -m json.tool
```

### Configuring an MCP Client

To connect an MCP-compatible AI agent (e.g., Claude Desktop, Claude Code, or a custom agent) to the RBAC MCP endpoint, configure it as a StreamableHTTP MCP server:

```json
{
  "mcpServers": {
    "rbac": {
      "type": "streamable-http",
      "url": "https://rbac.example.com/_private/_a2s/mcp/",
      "headers": {
        "x-rh-identity": "<base64-encoded-identity>"
      }
    }
  }
}
```

Replace the URL and identity header with values appropriate for your environment.

---

## Troubleshooting

| Symptom | Possible Cause | Resolution |
|---------|---------------|------------|
| 404 on `/_private/_a2s/mcp/` | `MCP_ENABLED=False` or the MCP URL is not registered | Set `MCP_ENABLED=True` and restart the application |
| JSON-RPC `-32000` "Authentication required" | Missing or invalid `x-rh-identity` header | Provide a valid base64-encoded identity header with `org_id` |
| JSON-RPC `-32003` "Permission denied" | User lacks RBAC permissions or Kessel relations for the tool | Verify the user has the required role/permission. For V2, check Kessel relation grants. |
| JSON-RPC `-32602` "Write mode is disabled" | `MCP_WRITE_ENABLED=False` (default) | Set `MCP_WRITE_ENABLED=True` if write operations are needed |
| JSON-RPC `-32602` "requires V2 APIs" | `V2_APIS_ENABLED=False` and a V2 tool was called | Set `V2_APIS_ENABLED=True` if V2 tools are needed |
| JSON-RPC `-32603` "Tool execution timed out" | Tool took longer than `MCP_TOOL_TIMEOUT_SECONDS` | Increase the timeout, investigate slow dependencies (BOP, Kessel, PostgreSQL) |
| Confirmation token expired | More than `MCP_WRITE_CONFIRMATION_TTL` seconds between phases | Increase the TTL or complete the confirmation more quickly |
| `tools/list` returns empty or missing tools | V2 tools hidden because `V2_APIS_ENABLED=False`; write tools show `[DISABLED]` | Check feature flags match the expected tool set |
| Redis errors in logs | Redis unavailable for description overrides or confirmation tokens | Ensure Redis is running and accessible. Read-only mode degrades gracefully; write confirmations require Redis. |
| HTTP 405 on GET | Client attempted SSE streaming | Use POST for all MCP protocol messages. SSE is not supported in WSGI mode. |

---

## Reference

### JSON-RPC Error Codes

| Code | Meaning |
|------|---------|
| `-32700` | Parse error (malformed JSON) |
| `-32600` | Invalid request (missing `jsonrpc: "2.0"`, batch requests, non-string method) |
| `-32601` | Method not found (unknown JSON-RPC method) |
| `-32602` | Invalid params (unknown tool, missing arguments, disabled tool, V2 not enabled) |
| `-32603` | Internal error (unhandled exception, tool timeout) |
| `-32000` | Authentication required (auth-required tool called without valid identity) |
| `-32003` | Permission denied (insufficient RBAC permissions or Kessel relations) |

### Key Files

| File | Description |
|------|-------------|
| `rbac/management/mcp_views.py` | MCP endpoint implementation (MCPView, tools, JSON-RPC handling) |
| `rbac/management/mcp_urls.py` | URL routing for MCP endpoint |
| `rbac/rbac/a2s.py` | `is_a2s_path()` helper shared by both middleware classes |
| `rbac/rbac/middleware.py` | A2S bypass in `IdentityHeaderMiddleware` |
| `rbac/internal/middleware.py` | A2S bypass in `InternalIdentityHeaderMiddleware` |
| `rbac/rbac/settings.py` | All MCP configuration settings |
| `rbac/rbac/urls.py` | Top-level URL routing (conditional MCP mount) |
| `tests/management/test_mcp_views.py` | Comprehensive test suite |
| `docs/MCP.md` | Developer-facing documentation (protocol, adding tools) |

### Protocol Specification

The MCP endpoint implements [MCP StreamableHTTP transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http) (protocol version `2025-03-26`). Server capabilities:

```json
{
  "tools": {"listChanged": false}
}
```

The `Mcp-Session-Id` response header is set on `initialize` for session tracking.
