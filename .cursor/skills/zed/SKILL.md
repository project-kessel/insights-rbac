---
name: zed-spicedb-check
description: Run Zed permission checks against SpiceDB in Kessel stage. Use when checking RBAC permissions in SpiceDB, verifying group membership, workspace access for users, or running zed permission check/lookup-resources against stage.
---

# Zed SpiceDB Check (Stage)

Use this skill to run Zed permission checks against SpiceDB in the Kessel stage environment.

## Prerequisites

0. **Check for config.env**: Verify `.cursor/skills/config.env` exists with `ZED_SPICEDB_VAULT_URL` (optional, for vault link).

1. **Get SpiceDB PSK token**: The token is in Vault. Read `ZED_SPICEDB_VAULT_URL` from `.cursor/skills/config.env` for the vault link, or use: [Vault - Kessel Stage SpiceDB PSK](https://vault.devshift.net/ui/vault/secrets/insights/kv/secrets%2Finsights-stage%2Fkessel%2Fspicedb_psk/details?version=2)

2. **Export the token** (do not store in config.env): `export ZED_SPICEDB_PSK="<token_from_vault>"`

3. **oc CLI**: Logged into the stage OpenShift cluster

## Setup (run once per session)

### Step 0: Check if logged in to the cluster

```bash
oc whoami
```

If this fails with "Unauthorized" or "You must be logged in to the server", you need to log in first. The stage cluster login link is in `.cursor/skills/config.env` as `OPENSHIFT_STAGE_CONSOLE`. Open that URL in your browser, click your username (top right) → **Copy login command**, then run the copied command in your terminal.

### Step 1: Login and port-forward

```bash
# Login to stage cluster (if Step 0 showed you are not logged in)
# Get the login command from OPENSHIFT_STAGE_CONSOLE in config.env (browser → Copy login command)

oc project kessel-stage
oc port-forward svc/kessel-relations-spicedb 50051:50051
```

Keep the port-forward running in a separate terminal.

### Step 2: Configure Zed context

Export the PSK and set the Zed context:

```bash
# Export the token (get from Vault - do not commit)
export ZED_SPICEDB_PSK="<token_from_vault>"

zed context set kessel-stage2 localhost:50051 "$ZED_SPICEDB_PSK" --insecure
zed context use kessel-stage2
```

### Step 3: Verify it works

```bash
zed permission check rbac/group:a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d member rbac/principal:redhat/12345678
zed permission lookup-resources rbac/group member rbac/principal:redhat/87654321
```

## Common Commands

### Check if principal is member of group

```bash
zed permission check rbac/group:<group_uuid> member rbac/principal:redhat/<user_id>
```

### Lookup groups a principal is member of

```bash
zed permission lookup-resources rbac/group member rbac/principal:redhat/<user_id>
```

### Check workspace access permission for user

```bash
zed permission check rbac/workspace:<workspace_uuid> view rbac/principal:redhat/<user_id>
```

Example:
```bash
zed permission check rbac/workspace:f7e8d9c0-b1a2-4e3d-8c7b-6a5f4e3d2c1b view rbac/principal:redhat/11223344
```

### Resource ID format

- **Group**: `rbac/group:<uuid>` (e.g. `rbac/group:a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d`)
- **Workspace**: `rbac/workspace:<uuid>` (e.g. `rbac/workspace:f7e8d9c0-b1a2-4e3d-8c7b-6a5f4e3d2c1b`)
- **Principal**: `rbac/principal:redhat/<user_id>` (e.g. `rbac/principal:redhat/12345678`)

## Config Reference

Variables in `.cursor/skills/config.env`:

| Variable | Description |
|----------|-------------|
| `OPENSHIFT_STAGE_CONSOLE` | Stage cluster web console URL — use to get "Copy login command" for `oc login` |
| `ZED_SPICEDB_VAULT_URL` | Vault URL to obtain the SpiceDB PSK token |
| `ZED_SPICEDB_PSK` | The PSK token — **use `export`**, do not store in config.env |
