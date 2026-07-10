# Ephemeral Cluster DR Toolkit

Scripts for testing RBAC disaster recovery reconciliation on bonfire ephemeral clusters.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/ephemeral/deploy-ephemeral.sh` | Deploy RBAC + dependencies with DR-ready configuration |
| `scripts/ephemeral/rbac-dr-toolkit.sh` | DR simulation toolkit (setup → simulate → fix → verify) |
| `scripts/ephemeral/parse-dr-logs.sh` | Live log parser with color-coded output and JSONL export |

## Prerequisites

- `oc` logged into an OpenShift cluster with an active ephemeral namespace
- `curl`, `jq` installed locally
- Bonfire environment deployed (see [Deploying](#deploying))

## Deploying

```bash
./scripts/ephemeral/deploy-ephemeral.sh
```

Deploys RBAC, Kessel Relations, and Host Inventory with DR-specific parameters:
- `DR_RELATIONS_RECONCILE_ENABLED=True` — enables Kessel Relations reconciliation endpoint
- `DR_WORKSPACE_RECONCILE_ENABLED=True` — enables workspace recovery endpoint
- `KAFKA_ENABLED=True` — required for event-based reconciliation
- `V2_EDIT_API_ENABLED=True` — enables v2 workspace/role/role-binding APIs

Default reservation duration is 8 hours. Override with `DURATION=4h ./scripts/ephemeral/deploy-ephemeral.sh`.

## DR Toolkit

The toolkit simulates database restores and verifies that the reconciliation endpoints correctly detect and fix divergence between RBAC and downstream systems.

### Two Reconciliation Paths

| Command | Endpoint | What it reconciles |
|---------|----------|--------------------|
| `--rbac-kessel` | `/_private/api/disaster_recovery/reconcile/` | RBAC DB ↔ Kessel Relations (SpiceDB tuples) |
| `--rbac-hbi` | `/_private/api/disaster_recovery/workspaces/` | RBAC DB ↔ HBI (workspace Kafka events) |

### Phases

Both paths follow the same phase structure:

| Phase | What it does |
|-------|-------------|
| `setup` | Create test resources (workspaces, roles, role bindings) |
| `simulate` | Manipulate DB to create divergence (add/remove/leave records) |
| `pre-check` | Verify divergence exists before reconciliation |
| `dry-run` | Run reconciler with `dry_run=true` (no writes) |
| `fix` | Run reconciler for real (writes corrective events) |
| `manual-fix` | Show the curl command for the user to run manually |
| `post-check` | Verify systems are back in sync |

### Quick Start (all phases at once)

```bash
# Kessel Relations reconciliation — full run
./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel

# Workspace (HBI) reconciliation — full run
./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi

# Fast mode: skip safety checks, auto-confirm prompts
./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel --fast --no-dry-run

# Minimal data: 1 resource per scenario (faster)
./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi --fast --no-dry-run --minimal-data
```

### Step-by-Step Execution

Run individual phases when debugging or demonstrating:

```bash
# Kessel flow
DR_STEP=setup      ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel
DR_STEP=simulate   ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel
DR_STEP=pre-check  ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel
DR_STEP=dry-run    ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel
DR_STEP=fix        ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel
DR_STEP=post-check ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel

# HBI workspace flow
DR_STEP=setup      ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi
DR_STEP=simulate   ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi
DR_STEP=manual-fix ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi
DR_STEP=post-check ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi
```

### Manual Fix

The `manual-fix` step prints the full `oc exec ... curl` command with all parameters filled in and waits for you to run it yourself. Useful for demos and learning what the reconciliation endpoint expects.

```bash
DR_STEP=manual-fix ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi
```

### Three Reconciliation Scenarios

Both `--rbac-kessel` and `--rbac-hbi` test three scenarios that correspond to the reconciler truth table:

| Scenario | Kafka event | DB state after simulate | Reconciler action |
|----------|------------|------------------------|-------------------|
| Corrective DELETE | `create` | NOT in DB | Writes DELETE (orphaned downstream) |
| Corrective CREATE | `delete` | In DB | Writes CREATE (missing downstream) |
| SKIP | `create` | In DB | No action (consistent) |

### Flags

| Flag | Effect |
|------|--------|
| `--fast` | Skip safety checks, sync waits, auto-confirm prompts |
| `--no-dry-run` | Skip the dry-run phase, proceed directly to real fix |
| `--minimal-data` | Use 1 resource per scenario instead of 2 |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DR_STEP` | `all` | Phase to run: `setup\|simulate\|pre-check\|dry-run\|fix\|manual-fix\|post-check\|cleanup\|state\|all` |
| `DR_STATE_FILE` | `~/.cache/rbac-dr-state.env` | State file path (stores IDs, timestamps between phases) |
| `DR_KESSEL_ORG_ID` | auto-resolved | Org ID for Kessel scenario |
| `DR_HBI_ORG_ID` | auto-resolved | Org ID for HBI scenario |
| `DR_BUFFER_SECONDS` | `300` | Kessel reconcile window (seconds) |
| `DR_BUFFER_MINUTES` | `5` | Workspace recovery window (minutes) |
| `DR_DRY_RUN` | `false` | Run reconciler without writing corrective events |

### Utility Commands

```bash
# List workspaces via public v2 API
./scripts/ephemeral/rbac-dr-toolkit.sh --workspaces

# Bootstrap a tenant
./scripts/ephemeral/rbac-dr-toolkit.sh --bootstrap-tenant <ORG_ID>

# Create a workspace
./scripts/ephemeral/rbac-dr-toolkit.sh --create-workspace "My Workspace"

# Delete a workspace directly from DB
./scripts/ephemeral/rbac-dr-toolkit.sh --delete-workspace-db <UUID>

# Show saved state (timestamps, workspace IDs, task IDs)
./scripts/ephemeral/rbac-dr-toolkit.sh --dr-state

# Tail worker/server logs
./scripts/ephemeral/rbac-dr-toolkit.sh --watch-worker
./scripts/ephemeral/rbac-dr-toolkit.sh --watch-server

# Show internal auth header
./scripts/ephemeral/rbac-dr-toolkit.sh --debug-psk
```

## Log Parser

Prettifies RBAC pod logs with color-coded levels, DR event formatting, and inline JSON printing.

### Usage

```bash
# Stream from a pod
oc logs <pod> -f | ./scripts/ephemeral/parse-dr-logs.sh

# Stream all pods matching a pattern
./scripts/ephemeral/parse-dr-logs.sh --pods rbac-worker

# Save structured JSONL alongside pretty output
./scripts/ephemeral/parse-dr-logs.sh --pods rbac-kafka-consumer --json dr-logs.jsonl

# Parse a saved log file
./scripts/ephemeral/parse-dr-logs.sh logfile.txt

# Disable colors (for piping)
./scripts/ephemeral/parse-dr-logs.sh --no-color logfile.txt
```

### What it Parses

- Celery worker logs (`[timestamp: LEVEL/Worker] message`)
- App logger logs (`[timestamp] LEVEL [module]: message`)
- ECS JSON logs (`{"@timestamp":"...","log.level":"info",...}`)
- Django/gunicorn plain text, warnings, tracebacks
- Truncated JSON (accumulates lines until valid JSON is complete)

### JSONL Export

With `--json <file>`, every parsed log line is written as a JSON object for later analysis:

```bash
# Export then query
./scripts/ephemeral/parse-dr-logs.sh --pods rbac-worker --json worker.jsonl
jq 'select(.level == "ERROR")' worker.jsonl
```

## State File

The toolkit persists state between phases in `~/.cache/rbac-dr-state.env` (plain key=value format). This allows running phases independently:

```bash
# Run setup, then come back later for simulate
DR_STEP=setup ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi
# ... time passes ...
DR_STEP=simulate ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi
```

The state file auto-clears when the ephemeral namespace changes (detected on script startup). View current state with `--dr-state`.
