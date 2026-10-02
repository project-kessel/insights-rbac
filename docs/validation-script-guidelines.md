# Local Docker/Podman Validation and Script Guidelines

This guide combines local full-stack setup and validation-script guidance. Use
it to prepare identities and data, run checks, and write reusable validation
scripts. The commands use Podman when available; Docker is also supported.

## Write validation scripts

Use a validation script when a feature needs proof against a running local
integration stack: API behavior, background replication, Kessel authorization,
or another service boundary. These scripts complement unit and integration
tests; they are not a replacement for them.

### Location and naming

Place scripts below `scripts/validations/`, grouped by the surface they test:

```text
scripts/validations/
└── api/
    └── v2-crud.sh
```

Use a descriptive, feature-oriented name such as `workspace-move.sh` or
`role-binding-inheritance.sh`. Do not put a pull-request number in a reusable
script name. The all-validation Make command discovers every `*.sh` file
recursively, so no registry needs updating.

### Script contract

- Use Bash and start with `#!/usr/bin/env bash` and `set -euo pipefail`.
- Require an already-running local Docker/Podman stack unless the script starts
  it explicitly. Never target shared, staging, or production environments.
- Provide `--help` and document every optional environment variable, including
  its default.
- Create unique resources using a run identifier. Never rely on or overwrite
  user-created test resources.
- Register `trap cleanup EXIT` before creating data. Cleanup must remove only
  resources, authorization tuples, and settings that the script created.
- If the script temporarily enables a feature gate or V2 write activation,
  remember whether it changed the value and restore only values it enabled.
- Do not print identity headers, tokens, passwords, or other secrets.

### What to validate

Make each scenario explain and prove one observable behavior. Display:

1. The endpoint and input.
2. The expected HTTP status and relevant response field.
3. The reason the scenario matters.
4. A clear `PASS` or `FAIL` result.

For an API endpoint, derive the HTTP method matrix from its registered routes
and allowed-method configuration. Cover every supported method and
representative unsupported methods; check status and relevant response
headers. Include individual query parameters, useful combinations, boundary
values, and malformed or conflicting input. Check response shape and selected
fields as well as status codes. When a cursor is returned, follow it and check
the next page; report when the local data has no next page to exercise.

Keep the API version separate from the identity fixture used by a request. A
user fixture created for one API version can call a route in another version;
the request path determines the API behavior under test. State both the route
and identity in scenario output. When fixtures provide different API-generation
or privilege variants, test each relevant combination and report route version,
identity generation, and administrator status as separate dimensions.

#### User combination matrix

Every generated test scenario must run for all four identity combinations:

| Combination | User fixture | `is_org_admin` |
| --- | --- | --- |
| V1 non-admin | `local-v1-non-org-admin` | no |
| V1 admin | `local-v1-org-admin` | yes |
| V2 non-admin | `local-v2-non-admin` | no |
| V2 admin | `local-v2-org-admin` | yes |

The script must iterate the matrix automatically. For each scenario execution,
print which user is active before any request output. Use colored labels so the
active combination is immediately visible in the terminal:

```text
━━━ Scenario 3: list workspaces ━━━
  ── V1 route (/api/rbac/v1/workspaces/) ──
  ▶ [V1 NON-ADMIN] local-v1-non-org-admin
    GET /api/rbac/v1/workspaces/ → 200 … PASS
  ▶ [V1 ADMIN] local-v1-org-admin
    GET /api/rbac/v1/workspaces/ → 200 … PASS
  ▶ [V2 NON-ADMIN] local-v2-non-admin
    GET /api/rbac/v1/workspaces/ → 200 … PASS
  ▶ [V2 ADMIN] local-v2-org-admin
    GET /api/rbac/v1/workspaces/ → 200 … PASS
  ── V2 route (/api/rbac/v2/workspaces/) ──
  ▶ [V1 NON-ADMIN] local-v1-non-org-admin
    GET /api/rbac/v2/workspaces/ → 200 … PASS
  ▶ [V1 ADMIN] local-v1-org-admin
    GET /api/rbac/v2/workspaces/ → 200 … PASS
  ▶ [V2 NON-ADMIN] local-v2-non-admin
    GET /api/rbac/v2/workspaces/ → 200 … PASS
  ▶ [V2 ADMIN] local-v2-org-admin
    GET /api/rbac/v2/workspaces/ → 200 … PASS
```

Assign a distinct ANSI color to each combination and keep the mapping
consistent across the entire script run:

```bash
# Example color map (256-color safe)
V1_NON_ADMIN_COLOR="\033[0;33m"   # yellow
V1_ADMIN_COLOR="\033[0;35m"       # magenta
V2_NON_ADMIN_COLOR="\033[0;36m"   # cyan
V2_ADMIN_COLOR="\033[0;32m"       # green
RESET="\033[0m"
```

When a scenario is expected to behave differently for a particular combination
(for example, a non-admin receives `403` while an admin receives `200`), state
the expected status per combination and assert it separately. If a combination
is intentionally skipped for a scenario, print a `SKIP` line with the reason.

When an API endpoint produces Kessel relations (creates, updates, or deletes
SpiceDB tuples), the validation script must include Kessel relation checks for
that endpoint. An HTTP success response alone is not enough. Wait for the
expected direct SpiceDB tuple, assert it exists, and print it. For deletes,
wait until the tuple is absent and report that removal. Use the persisted
`t_*` relation names, for example:

```text
rbac/workspace:<workspace-id> t_binding rbac/role_binding:<binding-id>
rbac/role_binding:<binding-id> t_role rbac/role:<role-id>
rbac/role_binding:<binding-id> t_subject rbac/principal:<domain>/<user-id>
```

Replication is asynchronous. Use a bounded polling loop with a useful failure
message rather than a fixed sleep. Read-only scenarios should print the
relevant existing tuples when available, but must not create state solely to
make a read check pass.

#### Cover HEAD response handling

For any validation script that sends a `HEAD` request, generate an offline
regression test that exercises its real request path against a local stub
server. Have the stub return the expected status (such as `401`) and a nonzero
`Content-Length`, but no response body, as required for `HEAD`. Assert that
curl exits successfully, the script reports the expected status, and it does
not fail with curl error 18 ("transfer closed with outstanding read data
remaining").

The test must catch using `--request HEAD` or `-X HEAD` without a
body-suppressing option; use `--head`/`-I` or the equivalent instead. Do not
rely only on checking the script's text for the right curl flag — run the
script against the stub and verify its exit code and output.

A minimal stub server for this purpose:

```python
#!/usr/bin/env python3
"""Stub HTTP server that returns status-only HEAD responses."""
import http.server
import sys

class HeadStubHandler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(int(sys.argv[2]) if len(sys.argv) > 2 else 401)
        self.send_header("Content-Length", "42")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # silence request logs during tests

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    with http.server.HTTPServer(("127.0.0.1", port), HeadStubHandler) as srv:
        srv.handle_request()  # serve exactly one request, then exit
```

The regression test should:

1. Start the stub on a free port.
2. Point the validation script at the stub's URL.
3. Assert the script exits `0` and prints the expected status.
4. Assert curl does not produce error 18.
5. Tear down the stub in a `trap` handler.

### Local authorization

The local development identity may not have the Kessel permissions needed for
V2 role or role-binding writes. A self-contained validator may create a
short-lived local authorization graph only when it:

- uses uniquely named role and role-binding resources;
- grants the minimum relations required by the scenario;
- removes all of those tuples in cleanup; and
- states this temporary access clearly in its output and documentation.

Use the repository's existing local user, role-binding, and authorization
setup when a scenario needs extra access. Grant only the permission and scope
needed by the scenario. Capture whether each exact temporary relationship
already exists, track only relationships the validator adds, and remove those
relationships on every exit path. Verify both the denied and allowed cases,
then confirm denial again after removing temporary access. Do not assume that
an administrator identity bypasses endpoint-specific authorization checks.

Prefer permissions scoped to the resource used in the scenario. For example,
a role bound to the Default Workspace needs a permission with Default/Workspace
scope; a tenant-scoped RBAC permission cannot be used for that binding.

### Output

Output should be readable during a local debugging session. Use the following
canonical scenario format:

```text
━━ SCENARIO X ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<Short description of the scenario>
  Request:  <HTTP method> <path> with <relevant payload or parameters>
  Expected: <HTTP status>; <what should happen>
  Why:      <reason this scenario matters; e.g. authorization rule, replication constraint>
PASS: <METHOD> <url> -> HTTP <status>
PASS: <additional assertion description>
PASS: Kessel relation removed: <tuple that should no longer exist>
PASS: Kessel relation ready: <tuple description>
  rbac/role_binding:<id> t_subject rbac/principal:<domain>/<user-id>
PASS: Kessel relation ready: <another tuple description>
  rbac/role_binding:<id> t_role rbac/role:<role-id>
```

Every scenario block must include:

1. **Header** — `━━ SCENARIO <number> ━━━━…` followed by a description.
2. **Request** — the HTTP method, path, and key payload or query parameters.
3. **Expected** — the expected HTTP status and observable outcome.
4. **Why** — the reason this scenario matters (authorization rule, replication
   constraint, edge case, etc.).
5. **Results** — one `PASS` or `FAIL` line per assertion, including the HTTP
   call and any follow-up checks.
6. **Kessel relation checks** (when the endpoint produces relations) — one
   `PASS`/`FAIL` line per tuple, with the full relation triple printed on the
   next line. Use the `t_*` relation names:

   ```text
   PASS: Kessel relation ready: role binding points to its role
     rbac/role_binding:01a0f225-8bce-7913-89f0-53931a827e87 t_role rbac/role:01a0f225-7728-7671-92cc-3c1f0ae03a53
   PASS: Kessel relation ready: role binding contains the allowed user
     rbac/role_binding:01a0f225-8bce-7913-89f0-53931a827e87 t_subject rbac/principal:redhat/v2-crud-user-1790768910-60777
   ```

Additional output rules:

- Support terminal colors automatically, with a plain-text fallback for CI or
  redirected output. Honor `NO_COLOR`; use `COLOR=always` only when a caller
  wants forced color.
- On failure, print the HTTP response body or the expected relation details
  needed to diagnose the problem.

### Verification and execution

Before adding a script, run its syntax and help checks:

```bash
bash -n scripts/validations/<area>/<script>.sh
scripts/validations/<area>/<script>.sh --help
```

Then run it against the local full stack. Execute one validation directly:

```bash
scripts/validations/api/v2-crud.sh
```

Run all validation scripts in lexical order:

```bash
make docker-local-full-validate
```

Document the validation in the relevant feature PR: the stack command used,
the script command, the scenarios exercised, and any local-only setup it
temporarily performs.

## Run validations on the local full stack

### Start the stack

Select the source for each service with `rbac=`, `rbac-config=`, `inventory=`,
and `hbi=`:

| Source | Meaning |
| --- | --- |
| `local` | Current checkout by default; prompts for or accepts a custom local path |
| `upstream` | Latest changes from the hard-coded upstream repository |
| `<GitHub PR URL>` | Source fetched from the specified pull request |
| `<commit SHA>` | Source fetched at the specified commit |

The default local directories for Kessel Inventory and HBI are
`.local-deps/inventory-api` and `.local-deps/insights-host-inventory`. The
hard-coded upstream repositories are
[insights-rbac](https://github.com/project-kessel/insights-rbac),
[rbac-config](https://github.com/project-kessel/rbac-config),
[inventory-api](https://github.com/project-kessel/inventory-api), and
[insights-host-inventory](https://github.com/RedHatInsights/insights-host-inventory).

| Scenario | Command |
| --- | --- |
| Local RBAC with upstream config | `make docker-local-full-up rbac=local rbac-config=upstream` |
| Local RBAC and local config | `make docker-local-full-up rbac=local rbac-config=local` |
| Latest upstream RBAC and config | `make docker-local-full-up rbac=upstream rbac-config=upstream` |
| RBAC and config commits | `make docker-local-full-up rbac=<rbac-commit-sha> rbac-config=<config-commit-sha>` |
| RBAC PR | `make docker-local-full-up rbac=<rbac-pr-url>` |
| Config PR | `make docker-local-full-up rbac=local rbac-config=<config-pr-url>` |
| RBAC PR and config PR | `make docker-local-full-up rbac=<rbac-pr-url> rbac-config=<config-pr-url>` |
| Local Inventory and HBI | `make docker-local-full-up inventory=local hbi=local` |
| Inventory and HBI commits | `make docker-local-full-up inventory=<inventory-commit-sha> hbi=<hbi-commit-sha>` |
| Inventory and HBI PRs | `make docker-local-full-up inventory=<inventory-pr-url> hbi=<hbi-pr-url>` |

When RBAC is selected from a PR URL, the command creates a temporary worktree,
checks out the PR head, and rebases it onto the current upstream `master`
before building the image. When the PR branch contains merge commits, the
command uses a merge instead of a rebase to preserve manual conflict
resolutions. If the rebase or merge conflicts, the command stops before
starting the stack and leaves the conflicting worktree path in its error output.

For a local source, the command prompts for the checkout directory. Press
Enter to use the default checkout. Defaults are the current repository for
RBAC, `../rbac-config` for `rbac-config`, and the two `.local-deps` directories
for Inventory and HBI. Selected paths are saved in the user-local
configuration file and reused on the next run:

```bash
make docker-local-full-up rbac=local rbac-config=local
```

The saved path file is `$XDG_CONFIG_HOME/insights-rbac/local-stack.env`, or
`~/.config/insights-rbac/local-stack.env` when `XDG_CONFIG_HOME` is not set.

The local `rbac-config` checkout must contain the stage ConfigMap; the command
compiles its stage schema with `ksl-test-schema-stage` before starting the
stack.

#### Running stack behavior

When the stack is already running, the command rebuilds and recreates the
services affected by the selected sources and prints a deployment summary. A
local RBAC and local config deployment rebuilds RBAC, refreshes the selected
schema and role definitions, and rebuilds the upstream Kessel and HBI services.

#### Check stack health

After starting or rebuilding the stack, check all Kessel, RBAC, and HBI
containers together with their published endpoints:

```bash
make docker-local-full-health
```

The command exits non-zero when a required container is stopped or unhealthy,
or when PostgreSQL, Relations, SpiceDB, RBAC, Inventory, or Kafka Connect is
not reachable.

#### Default full-stack identities

`make docker-local-full-up` automatically loads these idempotent users in
organization `local-full-stack` and account `10001`:

| Intended use | Username | User ID | Org admin |
| --- | --- | --- | --- |
| V1 org admin | `local-v1-org-admin` | `local-v1-org-admin-10001` | yes |
| V1 non-org admin | `local-v1-non-org-admin` | `local-v1-non-org-admin-10001` | no |
| V2 org admin | `local-v2-org-admin` | `local-v2-org-admin-10001` | yes |
| V2 non-admin | `local-v2-non-admin` | `local-v2-non-admin-10001` | no |

V1 or V2 is selected by the API request path; the persisted user model is
shared. Inspect these users with:

```bash
make docker-local-full-list-users
```

To disable loading them for a deployment, set
`FULL_STACK_LOAD_DEFAULT_USERS=false`.

#### Individual API list checks

The following scripts call fixed read-only endpoints and print their JSON
responses. They default to `local-v2-org-admin`; only the user can be changed:

| Endpoint | Script |
| --- | --- |
| V1 roles | `scripts/validations/api/list-v1-roles.sh` |
| V1 groups | `scripts/validations/api/list-v1-groups.sh` |
| V2 roles | `scripts/validations/api/list-v2-roles.sh` |
| V2 principals | `scripts/validations/api/list-v2-principals.sh` |
| V2 workspaces | `scripts/validations/api/list-v2-workspaces.sh` |
| V2 role bindings | `scripts/validations/api/list-v2-role-bindings.sh` |

For example:

```bash
scripts/validations/api/list-v2-workspaces.sh --user local-v2-non-admin
```

The scripts use predefined local organization/account and pagination values;
they do not create or modify data.

### Manage local users and groups

Use the declarative fixture action to create local tenants, users/principals,
groups, V2 roles, and role bindings. Start with a copy of the example fixture
so the checked-in defaults remain unchanged:

```bash
cp scripts/validations/api/actions/rbac-users.yaml /tmp/local-rbac-users.yaml
```

Edit the copied YAML:

```yaml
version: 1
tenants:
  - org_id: local-demo
    account_id: "10001"
    bootstrap: true
    users:
      - username: alice
        user_id: alice-local
        admin: false
        groups: [developers]
    groups:
      - name: developers
        description: Local developers
        members: [alice]
```

Validate the fixture without changing the database, then apply it to the
running local stack:

```bash
scripts/validations/api/actions/apply-rbac-users-config.sh \
  --file /tmp/local-rbac-users.yaml --dry-run
scripts/validations/api/actions/apply-rbac-users-config.sh \
  --file /tmp/local-rbac-users.yaml
```

The same operation is available through Make:

```bash
make docker-local-full-apply-users file=/tmp/local-rbac-users.yaml
```

Use `dry-run=true` to validate the fixture without changing the database:

```bash
make docker-local-full-apply-users \
  file=/tmp/local-rbac-users.yaml dry-run=true
```

The action is idempotent for the same tenant, user IDs, group names, role
names, and binding subjects. Use the read-only action to inspect the resulting
users, groups, roles, and bindings:

```bash
scripts/validations/api/actions/list-rbac-users.sh
```

The same read-only listing is available through Make:

```bash
make docker-local-full-list-users
```

It displays tenants, users/principals, groups and memberships, V2 roles and
permissions, and role bindings from the running full-stack RBAC database.

For cleanup, mark every fixture tenant with `temporary: true` and run:

```bash
scripts/validations/api/actions/apply-rbac-users-config.sh \
  --file /tmp/local-rbac-users.yaml --delete
```

Deletion is refused for fixtures that do not explicitly mark their tenants as
temporary.

Wait until the RBAC API is available at `http://localhost:9080`.

### Run all validations

```bash
make docker-local-full-validate
```

The Make target discovers every `*.sh` file below `scripts/validations/` and
runs them in lexical order. The current local checks include:

- workspace creation and read-your-writes pipeline validation;
- V2 workspace, role, principal, and role-binding CRUD validation, including
  persisted Kessel relations and relation cleanup;
- read-only inspection of all RBAC organizations and their persisted users.

#### Identity used by each validation

The scripts print the runtime identity they use. This is the complete local
mapping:

| Script/action | API/version | Organization and account | User | Persistence |
| --- | --- | --- | --- | --- |
| `api/v2-crud.sh` | V2 | `org_id=11111`, `account_id=10001` | Unique temporary `v2-crud-user-<timestamp>-<pid>`, `admin: true` | Created from a temporary YAML fixture and deleted on exit |
| `api/create-workspace.sh` | V2 | `local-full-stack`, account `10001` by default | `local-v2-org-admin` | Uses one of the four default full-stack identities; select with `--user` |
| `api/actions/apply-rbac-users-config.sh` | V2 fixture loader | Values come from `actions/rbac-users.yaml` or `--file` | Values come from each YAML `users` entry | Persists users, groups, roles, and bindings until changed/deleted |
| `api/actions/list-rbac-users.sh` | No API mutation | Reads every tenant in the running RBAC database | Lists persisted principals; `generate-user` defaults to org `11111`, account `10001`, and a generated user ID | `list` is read-only; `generate-user` creates only a header |

`v2-crud.sh` is self-contained: it does not use `user_dev` or any pre-existing
user. Its temporary YAML fixture creates the principal, a temporary V2 access
role, and a tenant-level binding. The script sends that user in
`X-RH-Identity`, runs the V2 CRUD scenarios, removes API/Kessel test data, and
deletes the fixture user. `admin: true` is metadata; the V2 role binding is
what grants the write permissions.

`create-workspace.sh` exercises only V2 workspace creation and the
read-your-writes pipeline. It reuses the running full Kessel stack and does
not start the legacy `rbac_db` container when the full-stack database is
present. Select a loaded identity with `--user`, for example
`--user local-v2-non-admin`.

### Run individual checks

Run the V2 API lifecycle check:

```bash
scripts/validations/api/v2-crud.sh
```

Run workspace creation and read-your-writes directly when the stack is already
running:

```bash
scripts/validations/api/create-workspace.sh --no-start --user local-v2-non-admin
```

This check validates the V2 workspace RYW path. The full stack still starts
HBI and its Inventory API, but the current `test_ryw.py` helper does not assert
HBI replication; `--check-hbi` is therefore informational and prints a warning
instead of passing unsupported arguments to the helper.

List organizations and users/principals from the running RBAC container:

```bash
scripts/validations/api/actions/list-rbac-users.sh
```

The users action is read-only. It uses the container's Django ORM and prints
each organization, account, principal count, username, user ID, principal
type, and UUID. It also lists every V2 role with its permissions and every
role binding with its role, resource, and user/group subjects.

Generate an identity header for a new local test user without changing the
database:

```bash
scripts/validations/api/actions/list-rbac-users.sh generate-user --admin --v2
scripts/validations/api/actions/list-rbac-users.sh generate-user --non-admin --v1
```

The command prints the identity JSON, its base64 `X-RH-Identity` value, and a
ready-to-run `curl` example. Use `--org-id`, `--account-number`, `--username`,
or `--user-id` to override the generated values. This command is deliberately
header-only: it does not create a database user, bootstrap a tenant, or grant
Kessel permissions. The `--admin` flag only changes the identity header's
`is_org_admin` value.

To create real local users and bootstrap their tenant, use the declarative
fixture action:

```bash
scripts/validations/api/actions/apply-rbac-users-config.sh
scripts/validations/api/actions/apply-rbac-users-config.sh --dry-run
scripts/validations/api/actions/apply-rbac-users-config.sh \
  --file /absolute/path/to/rbac-users.yaml
```

For a validation-only fixture, mark the tenant `temporary: true`. The action
can then remove the declared custom roles and users through the normal RBAC
disable path:

```bash
scripts/validations/api/actions/apply-rbac-users-config.sh \
  --file /absolute/path/to/temporary-users.yaml --delete
```

Deletion is refused unless every tenant in the YAML is explicitly marked
temporary, so a normal persistent fixture cannot be removed accidentally.

The example fixture is
[`scripts/validations/api/actions/rbac-users.yaml`](../scripts/validations/api/actions/rbac-users.yaml).
It supports, per tenant:

- `bootstrap: true` to create the tenant and its default workspaces;
- `users` with a username, external user ID, admin metadata, and group membership;
- `groups` with members and group-level role bindings;
- V2 custom `roles` with application/resource/operation permissions; and
- role bindings for users or groups at the user, group, or tenant level.

Applying the same fixture again is safe for the same tenant, user IDs, role
names, and binding subjects. User creation goes through the normal RBAC
bootstrap service, so the persisted principals and their outbox/Kessel
relations are visible to the running API. Authorization still comes from role
bindings; `admin: true` is identity metadata and is not a substitute for a
binding.

To inspect the schema currently loaded in local SpiceDB:

```bash
./scripts/zed_local.sh schema
```

### Output and colors

The users action colors interactive terminal output automatically. Override it
when needed:

```bash
COLOR=always scripts/validations/api/actions/list-rbac-users.sh
COLOR=never scripts/validations/api/actions/list-rbac-users.sh
NO_COLOR=1 scripts/validations/api/actions/list-rbac-users.sh
```

### Stop the stack

```bash
make docker-local-full-down
```

This stops the local full stack and removes leftover containers from the legacy
RBAC Compose project as well. Volumes are kept.
