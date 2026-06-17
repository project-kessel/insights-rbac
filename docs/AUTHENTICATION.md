# RBAC Authentication Guide

This document describes all authentication methods for the RBAC service, including public APIs, internal endpoints, and service-to-service communication.

## Table of Contents

- [Overview](#overview)
- [API Versions and Paths](#api-versions-and-paths)
- [Authentication Methods](#authentication-methods)
  - [1. Public API Authentication (x-rh-identity)](#1-public-api-authentication-x-rh-identity)
  - [2. Pre-Shared Key (PSK) Authentication](#2-pre-shared-key-psk-authentication)
  - [3. JWT Token Authentication](#3-jwt-token-authentication)
- [Internal Endpoints](#internal-endpoints)
- [No-Auth Endpoints](#no-auth-endpoints)
- [Local Development Examples](#local-development-examples)
- [Environment Variables](#environment-variables)
- [Troubleshooting](#troubleshooting)

---

## Overview

The RBAC service supports three primary authentication mechanisms:

| Method | Use Case | Headers |
|--------|----------|---------|
| **x-rh-identity** | Public APIs, web clients | `x-rh-identity` (Base64 JSON) |
| **PSK** | Service-to-service calls | `x-rh-rbac-psk`, `x-rh-rbac-org-id`, `x-rh-rbac-client-id` |
| **JWT Token** | Service-to-service calls | `Authorization: Bearer <token>` |

---

## API Versions and Paths

### V1 API (Default)

Base path: `/api/rbac/v1/`

| Endpoint | Description |
|----------|-------------|
| `/api/rbac/v1/roles/` | Role CRUD operations |
| `/api/rbac/v1/groups/` | Group management |
| `/api/rbac/v1/permissions/` | Permission queries |
| `/api/rbac/v1/principals/` | Principal management |
| `/api/rbac/v1/access/` | Access control checks |
| `/api/rbac/v1/policies/` | Policy management |
| `/api/rbac/v1/cross-account-requests/` | Cross-account access requests |
| `/api/rbac/v1/status/` | Health check (no auth required) |
| `/api/rbac/v1/ready/` | Readiness probe (no auth required) |
| `/api/rbac/v1/openapi.json` | OpenAPI specification (no auth required) |

### V2 API (Workspace-centric)

Base path: `/api/rbac/v2/`

Enabled via `V2_APIS_ENABLED=True`. The V2 API uses a workspace-centric model with hierarchical permission management.

| Endpoint | Description |
|----------|-------------|
| `/api/rbac/v2/workspaces/` | Workspace management |
| `/api/rbac/v2/roles/` | Roles with workspace context |
| `/api/rbac/v2/role_bindings/` | Role bindings management |
| `/api/rbac/v2/openapi.json` | V2 OpenAPI specification (no auth required) |

### Internal APIs

Base path: `/_private/`

| Endpoint | Authentication | Description |
|----------|----------------|-------------|
| `/_private/api/v1/integrations/tenant/` | x-rh-identity | Tenant integrations |
| `/_private/_s2s/workspaces/ungrouped/` | PSK or JWT | S2S workspace operations |
| `/_private/api/relations/` | x-rh-identity | Relations API |
| `/_private/api/inventory/` | x-rh-identity | Inventory operations |

---

## Authentication Methods

### 1. Public API Authentication (x-rh-identity)

This is the primary authentication method for public API endpoints. The gateway (3scale/Akamai) provides a Base64-encoded JSON identity header.

#### Header Format

```
Header: x-rh-identity
Value: Base64-encoded JSON
```

#### Identity Structure

**For regular users:**
```json
{
  "identity": {
    "account_number": "10001",
    "org_id": "11111",
    "type": "User",
    "user": {
      "username": "jdoe",
      "email": "jdoe@example.com",
      "is_org_admin": true,
      "is_internal": false,
      "user_id": "12345"
    }
  }
}
```

**For service accounts:**
```json
{
  "identity": {
    "org_id": "11111",
    "type": "ServiceAccount",
    "service_account": {
      "username": "service-account-12345",
      "client_id": "12345-abcde-67890"
    }
  }
}
```

**For internal/associate users:**
```json
{
  "identity": {
    "account_number": "10001",
    "org_id": "11111",
    "type": "Associate",
    "user": {
      "username": "associate_user",
      "email": "associate@redhat.com",
      "is_org_admin": false,
      "is_internal": true,
      "user_id": "99999"
    },
    "internal": {
      "org_id": "target_org_id",
      "cross_access": true
    }
  }
}
```

#### Key Fields

| Field | Required | Description |
|-------|----------|-------------|
| `org_id` | Yes | Organization identifier |
| `account_number` | No | Legacy account number |
| `type` | Yes | `User`, `ServiceAccount`, or `Associate` |
| `user.username` | Yes (for users) | Username |
| `user.is_org_admin` | Yes (for users) | Admin privileges flag |
| `service_account.client_id` | Yes (for service accounts) | Service account client ID |

#### Quick Copy Examples (x-rh-identity)

**Admin User** (org_id: 11111, is_org_admin: true):
```bash
curl http://localhost:8000/api/rbac/v1/roles/ \
  -H "x-rh-identity: eyJpZGVudGl0eSI6IHsiYWNjb3VudF9udW1iZXIiOiAiMTAwMDEiLCAib3JnX2lkIjogIjExMTExIiwgInR5cGUiOiAiVXNlciIsICJ1c2VyIjogeyJ1c2VybmFtZSI6ICJhZG1pbl91c2VyIiwgImVtYWlsIjogImFkbWluQGV4YW1wbGUuY29tIiwgImlzX29yZ19hZG1pbiI6IHRydWUsICJpc19pbnRlcm5hbCI6IGZhbHNlLCAidXNlcl9pZCI6ICIxMjM0NSJ9fX0="
```

<details>
<summary>Decoded JSON</summary>

```json
{"identity": {"account_number": "10001", "org_id": "11111", "type": "User", "user": {"username": "admin_user", "email": "admin@example.com", "is_org_admin": true, "is_internal": false, "user_id": "12345"}}}
```
</details>

**Regular User** (org_id: 11111, is_org_admin: false):
```bash
curl http://localhost:8000/api/rbac/v1/roles/ \
  -H "x-rh-identity: eyJpZGVudGl0eSI6IHsiYWNjb3VudF9udW1iZXIiOiAiMTAwMDEiLCAib3JnX2lkIjogIjExMTExIiwgInR5cGUiOiAiVXNlciIsICJ1c2VyIjogeyJ1c2VybmFtZSI6ICJyZWd1bGFyX3VzZXIiLCAiZW1haWwiOiAidXNlckBleGFtcGxlLmNvbSIsICJpc19vcmdfYWRtaW4iOiBmYWxzZSwgImlzX2ludGVybmFsIjogZmFsc2UsICJ1c2VyX2lkIjogIjY3ODkwIn19fQ=="
```

<details>
<summary>Decoded JSON</summary>

```json
{"identity": {"account_number": "10001", "org_id": "11111", "type": "User", "user": {"username": "regular_user", "email": "user@example.com", "is_org_admin": false, "is_internal": false, "user_id": "67890"}}}
```
</details>

**Service Account** (org_id: 11111, client_id: abc-123-def):
```bash
curl http://localhost:8000/api/rbac/v1/roles/ \
  -H "x-rh-identity: eyJpZGVudGl0eSI6IHsib3JnX2lkIjogIjExMTExIiwgInR5cGUiOiAiU2VydmljZUFjY291bnQiLCAic2VydmljZV9hY2NvdW50IjogeyJ1c2VybmFtZSI6ICJzZXJ2aWNlLWFjY291bnQtMTIzIiwgImNsaWVudF9pZCI6ICJhYmMtMTIzLWRlZiJ9fX0="
```

<details>
<summary>Decoded JSON</summary>

```json
{"identity": {"org_id": "11111", "type": "ServiceAccount", "service_account": {"username": "service-account-123", "client_id": "abc-123-def"}}}
```
</details>

**Associate/Internal User** (cross_access to org_id: 22222):
```bash
curl http://localhost:8000/api/rbac/v1/roles/ \
  -H "x-rh-identity: eyJpZGVudGl0eSI6IHsiYWNjb3VudF9udW1iZXIiOiAiMTAwMDEiLCAib3JnX2lkIjogIjExMTExIiwgInR5cGUiOiAiQXNzb2NpYXRlIiwgInVzZXIiOiB7InVzZXJuYW1lIjogImFzc29jaWF0ZV91c2VyIiwgImVtYWlsIjogImFzc29jaWF0ZUByZWRoYXQuY29tIiwgImlzX29yZ19hZG1pbiI6IGZhbHNlLCAiaXNfaW50ZXJuYWwiOiB0cnVlLCAidXNlcl9pZCI6ICI5OTk5OSJ9LCAiaW50ZXJuYWwiOiB7Im9yZ19pZCI6ICIyMjIyMiIsICJjcm9zc19hY2Nlc3MiOiB0cnVlfX19"
```

<details>
<summary>Decoded JSON</summary>

```json
{"identity": {"account_number": "10001", "org_id": "11111", "type": "Associate", "user": {"username": "associate_user", "email": "associate@redhat.com", "is_org_admin": false, "is_internal": true, "user_id": "99999"}, "internal": {"org_id": "22222", "cross_access": true}}}
```
</details>

---

### 2. Pre-Shared Key (PSK) Authentication

Used for trusted service-to-service communication. The calling service must have a pre-shared key configured.

#### Required Headers

| Header | Description | Example |
|--------|-------------|---------|
| `x-rh-rbac-psk` | Pre-shared key | `my-secret-key-123` |
| `x-rh-rbac-org-id` | Target organization ID | `11111` |
| `x-rh-rbac-client-id` | Service client identifier | `catalog` |

#### Optional Headers

| Header | Description | Example |
|--------|-------------|---------|
| `x-rh-rbac-account` | Legacy account number | `10001` |

#### Server Configuration

PSKs are configured via the `SERVICE_PSKS` environment variable:

```json
{
  "catalog": {
    "secret": "primary-key-here",
    "alt-secret": "backup-key-for-rotation"
  },
  "cost-management": {
    "secret": "another-service-key"
  }
}
```

#### Quick Copy Examples (PSK)

First, start the server with PSK configured:
```bash
SERVICE_PSKS='{"catalog": {"secret": "test-psk-key"}}' make serve
```

**PSK Authentication** (client: catalog, org_id: 11111):
```bash
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "x-rh-rbac-psk: test-psk-key" \
  -H "x-rh-rbac-org-id: 11111" \
  -H "x-rh-rbac-client-id: catalog"
```

**PSK with Account Number** (optional legacy account):
```bash
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "x-rh-rbac-psk: test-psk-key" \
  -H "x-rh-rbac-org-id: 11111" \
  -H "x-rh-rbac-client-id: catalog" \
  -H "x-rh-rbac-account: 10001"
```

---

### 3. JWT Token Authentication

Used for service-to-service calls with Red Hat SSO tokens.

#### Header Format

```
Header: Authorization
Value: Bearer <JWT_TOKEN>
```

#### Token Validation

- Tokens are validated against Red Hat SSO JWKS endpoint
- Required claims: `iss`, `scope`, `sub`, `preferred_username`
- User must be configured in `SYSTEM_USERS` environment variable

#### Server Configuration

```json
{
  "user-id-from-token": {
    "admin": true,
    "is_service_account": true,
    "allow_any_org": false
  }
}
```

#### Quick Copy Examples (JWT)

First, start the server with system users configured:
```bash
SYSTEM_USERS='{"my-service-user": {"admin": true, "is_service_account": true}}' make serve
```

**JWT Authentication** (requires valid SSO token):
```bash
# Get token from SSO first
TOKEN=$(curl -s -X POST \
  "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token" \
  -d "grant_type=client_credentials" \
  -d "client_id=your-client-id" \
  -d "client_secret=your-client-secret" \
  | jq -r '.access_token')

# Use the token
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "x-rh-rbac-org-id: 11111"
```

**Bypass Token Validation** (local testing only):
```bash
# Start server with validation bypass
IT_BYPASS_TOKEN_VALIDATION=True make serve

# Use any token value (will not be validated)
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "Authorization: Bearer fake-token-for-testing" \
  -H "x-rh-rbac-org-id: 11111"
```

---

## Internal Endpoints

Internal endpoints (`/_private/`) use the `InternalIdentityHeaderMiddleware` and have different authentication requirements:

### S2S Endpoints (`/_private/_s2s/`)

Authenticate using PSK or JWT token:
1. First attempts PSK authentication
2. Falls back to JWT token validation
3. Returns 403 if both fail

### Other Internal Endpoints (`/_private/api/`)

Use standard x-rh-identity header with additional validation for internal operations.

---

## No-Auth Endpoints

The following endpoints do not require authentication:

| Endpoint | Description |
|----------|-------------|
| `/api/rbac/v1/status/` | Server health check |
| `/api/rbac/v1/ready/` | Readiness probe |
| `/api/rbac/v1/openapi.json` | V1 OpenAPI specification |
| `/api/rbac/v2/openapi.json` | V2 OpenAPI specification |
| `/metrics` | Prometheus metrics |

---

## Local Development Examples

### Method 1: Development Mode (Automatic Header Injection)

The easiest way to test locally. When `DEVELOPMENT=True`, a mock identity header is automatically injected.

```bash
# Start the server in development mode
DEVELOPMENT=True make serve

# Make requests without any auth headers
curl http://localhost:8000/api/rbac/v1/roles/

# The middleware injects a default user:
# - username: user_dev@foo.com
# - org_id: 11111
# - account: 10001
# - is_org_admin: true
```

**Changing User Type in Development:**

Use the `User-Type` header to simulate different user types:

```bash
# Standard user (default)
curl http://localhost:8000/api/rbac/v1/roles/

# Associate/Internal user
curl http://localhost:8000/api/rbac/v1/roles/ \
  -H "User-Type: associate"

# Internal user
curl http://localhost:8000/api/rbac/v1/roles/ \
  -H "User-Type: internal"
```

---

### Method 2: Manual x-rh-identity Header

Create and send your own identity header.

**Step 1: Generate Base64-encoded header**

```bash
# Using Python
python3 -c "
import base64, json
identity = {
    'identity': {
        'account_number': '10001',
        'org_id': '11111',
        'type': 'User',
        'user': {
            'username': 'test_user',
            'email': 'test@example.com',
            'is_org_admin': True,
            'is_internal': False,
            'user_id': '12345'
        }
    }
}
print(base64.b64encode(json.dumps(identity).encode()).decode())
"
```

**Step 2: Use the header in requests**

```bash
# Store the header value
IDENTITY_HEADER=$(python3 -c "
import base64, json
identity = {'identity': {'account_number': '10001', 'org_id': '11111', 'type': 'User', 'user': {'username': 'test_user', 'email': 'test@example.com', 'is_org_admin': True, 'is_internal': False, 'user_id': '12345'}}}
print(base64.b64encode(json.dumps(identity).encode()).decode())
")

# Make requests with the header
curl http://localhost:8000/api/rbac/v1/roles/ \
  -H "x-rh-identity: $IDENTITY_HEADER"
```

**Examples for different user types:**

```bash
# Non-admin user
python3 -c "
import base64, json
identity = {
    'identity': {
        'org_id': '11111',
        'type': 'User',
        'user': {
            'username': 'regular_user',
            'email': 'user@example.com',
            'is_org_admin': False,
            'is_internal': False,
            'user_id': '67890'
        }
    }
}
print(base64.b64encode(json.dumps(identity).encode()).decode())
"

# Service account
python3 -c "
import base64, json
identity = {
    'identity': {
        'org_id': '11111',
        'type': 'ServiceAccount',
        'service_account': {
            'username': 'service-account-123',
            'client_id': 'abc-123-def'
        }
    }
}
print(base64.b64encode(json.dumps(identity).encode()).decode())
"
```

---

### Method 3: PSK Authentication (S2S Testing)

For testing service-to-service authentication locally.

**Step 1: Start server with PSK configuration**

```bash
# Using environment variable
export SERVICE_PSKS='{"catalog": {"secret": "my-test-key"}}'
make serve

# Or inline
SERVICE_PSKS='{"catalog": {"secret": "my-test-key"}}' make serve
```

**Step 2: Make requests with PSK headers**

```bash
# Successful PSK authentication
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "x-rh-rbac-psk: my-test-key" \
  -H "x-rh-rbac-org-id: 11111" \
  -H "x-rh-rbac-client-id: catalog"

# This should return 403 (wrong key)
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "x-rh-rbac-psk: wrong-key" \
  -H "x-rh-rbac-org-id: 11111" \
  -H "x-rh-rbac-client-id: catalog"
```

**PSK with multiple services:**

```bash
export SERVICE_PSKS='{
  "catalog": {"secret": "catalog-key", "alt-secret": "catalog-backup"},
  "cost-mgmt": {"secret": "cost-key"}
}'
make serve

# Request from catalog service
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "x-rh-rbac-psk: catalog-key" \
  -H "x-rh-rbac-org-id: 11111" \
  -H "x-rh-rbac-client-id: catalog"

# Request from cost-mgmt service
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "x-rh-rbac-psk: cost-key" \
  -H "x-rh-rbac-org-id: 22222" \
  -H "x-rh-rbac-client-id: cost-mgmt"
```

---

### Method 4: JWT Token Authentication (S2S Testing)

For testing JWT-based service-to-service authentication.

**Step 1: Configure system users**

```bash
export SYSTEM_USERS='{
  "system-user-id": {
    "admin": true,
    "is_service_account": true,
    "allow_any_org": false
  }
}'
make serve
```

**Step 2: Obtain a token from Red Hat SSO**

```bash
# Get token from SSO (example)
TOKEN=$(curl -s -X POST \
  "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token" \
  -d "grant_type=client_credentials" \
  -d "client_id=your-client-id" \
  -d "client_secret=your-client-secret" \
  | jq -r '.access_token')
```

**Step 3: Use the token**

```bash
curl http://localhost:8000/_private/_s2s/workspaces/ungrouped/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "x-rh-rbac-org-id: 11111"
```

**Bypass token validation for local testing:**

```bash
# NOT recommended for production
IT_BYPASS_TOKEN_VALIDATION=True make serve
```

---

## Environment Variables

### Authentication Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `DEVELOPMENT` | Enable dev mode with mock identity | `False` |
| `SERVICE_PSKS` | JSON map of service PSKs | `{}` |
| `SYSTEM_USERS` | JSON map of system user configs | `{}` |
| `V2_APIS_ENABLED` | Enable V2 API endpoints | `False` |

### IT/SSO Configuration (JWT Validation)

| Variable | Description | Default |
|----------|-------------|---------|
| `IT_SERVICE_HOST` | SSO host | `localhost` |
| `IT_SERVICE_PORT` | SSO port | `443` |
| `IT_SERVICE_PROTOCOL_SCHEME` | Protocol | `https` |
| `IT_SERVICE_TIMEOUT_SECONDS` | Request timeout | `10` |
| `IT_TOKEN_JKWS_CACHE_LIFETIME` | JWKS cache lifetime (seconds) | `28800` |
| `IT_BYPASS_TOKEN_VALIDATION` | Skip token validation | `False` |

### API Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `API_PATH_PREFIX` | API base path | `/api/rbac` |
| `ALLOW_ANY` | Allow all access (testing) | `False` |

---

## Troubleshooting

### Common Error Responses

**401 Unauthorized - Missing identity header:**
```json
{
  "detail": "Authentication credentials were not provided."
}
```
Solution: Add `x-rh-identity` header with valid Base64-encoded identity.

**400 Bad Request - Missing org_id:**
```json
{
  "code": 400,
  "message": "An org_id must be provided in the identity header."
}
```
Solution: Include `org_id` in the identity JSON.

**400 Bad Request - Missing service account client_id:**
```json
{
  "code": 400,
  "message": "The client ID must be provided for the service account in the x-rh-identity header."
}
```
Solution: Include `client_id` in `service_account` object.

**403 Forbidden - S2S authentication failed:**
```json
{
  "detail": "You do not have permission to perform this action."
}
```
Solution: Verify PSK or JWT token is valid and configured in server.

### Debugging Tips

1. **Enable debug logging:**
   ```bash
   DJANGO_LOG_LEVEL=DEBUG make serve
   ```

2. **Check identity parsing:**
   ```python
   import base64, json
   header = "your-base64-header"
   print(json.dumps(json.loads(base64.b64decode(header)), indent=2))
   ```

3. **Verify PSK configuration:**
   ```bash
   echo $SERVICE_PSKS | python3 -m json.tool
   ```

4. **Test without auth (status endpoint):**
   ```bash
   curl http://localhost:8000/api/rbac/v1/status/
   ```

---

## Key Files Reference

| File | Description |
|------|-------------|
| `rbac/rbac/middleware.py` | Main identity header processing |
| `rbac/rbac/dev_middleware.py` | Development mock header injection |
| `rbac/internal/middleware.py` | Internal/S2S API authentication |
| `rbac/management/utils.py` | PSK & JWT token utilities |
| `rbac/management/authorization/token_validator.py` | JWT validation |
| `rbac/api/models.py` | User object model |
| `rbac/rbac/settings.py` | Configuration & environment variables |
