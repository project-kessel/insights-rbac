# insights-rbac

Role-Based Access Control (RBAC) service for [console.redhat.com](https://console.redhat.com). Manages roles, permissions, groups, and workspaces that control user access across the Hybrid Cloud Console platform.

## Overview

insights-rbac is a Django REST Framework microservice that provides two API versions:

- **V1 API** -- stable, widely consumed REST API for managing roles, groups, policies, and permissions
- **V2 API** -- next-generation API with workspace-based access control, RFC 7807 error responses, and Kessel integration for authorization

The service is multi-tenant: every request is scoped to an organization (tenant) via identity headers injected by the platform's authentication gateway.

## Tech Stack

- **Language**: Python 3.12
- **Framework**: Django 5.2 / Django REST Framework
- **Database**: PostgreSQL 16
- **Cache**: Redis
- **Task Queue**: Celery (Redis broker)
- **Authorization**: Kessel (SpiceDB-based), accessed through the Kessel Inventory API (gRPC)
- **Messaging**: Kafka (Debezium CDC outbox pattern)
- **Metrics**: Prometheus

## Quick Start

### Prerequisites

- Python 3.12
- [Pipenv](https://pipenv.pypa.io/)
- Docker / Podman (for PostgreSQL and Redis)

### Option 1: Docker Compose (full stack)

Starts the RBAC server, PostgreSQL, Redis, Celery worker, and Celery beat scheduler:

```bash
make docker-up       # App available at http://localhost:9080
make docker-logs     # Tail all container logs
make docker-down     # Stop and remove containers
```

### Option 2: Local Python (app only)

Run the Django server locally, using Docker only for PostgreSQL:

```bash
pipenv install --dev     # Install dependencies
make start-db            # Start Postgres on port 15432
make run-migrations      # Apply database migrations
make serve               # App available at http://localhost:8000
```

### Option 3: Full Kessel and Host Inventory integration stack

Start RBAC with Kessel Inventory, Kessel Relations, SpiceDB, Kafka, Debezium,
and Host Inventory:

```bash
make docker-local-full-up rbac=local rbac-config=upstream
```

Check that the full-stack containers and published endpoints are healthy:

```bash
make docker-local-full-health
```

The command builds `insights-rbac-local:dev`, uses Docker or Podman Compose,
and resolves the upstream Kessel Inventory and Host Inventory checkouts under
`.local-deps/`. If either checkout is absent, it creates a shallow clone.
Ensure the container VM has enough memory for the full stack.

Each deployment also loads four idempotent local identities in organization
`local-full-stack` / account `10001`: `local-v1-org-admin`,
`local-v1-non-org-admin`, `local-v2-org-admin`, and `local-v2-non-admin`. The
two `*-org-admin` users have
`admin: true`; the other two are non-admin users. Set
`FULL_STACK_LOAD_DEFAULT_USERS=false` to disable this local fixture.

#### Source selection

Select the RBAC, `rbac-config`, Kessel Inventory, and HBI sources directly on
the command line:

| Source | Meaning |
| --- | --- |
| `local` | Current checkout by default; prompts for or accepts a custom local path |
| `upstream` | Latest changes from the hard-coded upstream repository |
| `<GitHub PR URL>` | Source fetched from the specified pull request |
| `<commit SHA>` | Source fetched at the specified commit |

The hard-coded upstream repositories are
[insights-rbac](https://github.com/project-kessel/insights-rbac),
[rbac-config](https://github.com/project-kessel/rbac-config),
[inventory-api](https://github.com/project-kessel/inventory-api), and
[insights-host-inventory](https://github.com/RedHatInsights/insights-host-inventory).

Examples:

```bash
make docker-local-full-up rbac=local rbac-config=upstream
make docker-local-full-up rbac=upstream rbac-config=upstream
make docker-local-full-up rbac=<rbac-commit-sha> rbac-config=<config-commit-sha>
make docker-local-full-up \
  rbac=https://github.com/project-kessel/insights-rbac/pull/3309 \
  rbac-config=https://github.com/project-kessel/rbac-config/pull/123
make docker-local-full-up inventory=local hbi=local
make docker-local-full-up \
  inventory=https://github.com/project-kessel/inventory-api/pull/123 \
  hbi=https://github.com/RedHatInsights/insights-host-inventory/pull/456
```

For `rbac-config=local`, the default sibling checkout at `../rbac-config` is
used. It must contain the stage ConfigMap; the command compiles its stage KSL
schema with `ksl-test-schema-stage` before starting the stack.

When a local source is selected, the command asks for its checkout directory.
Press Enter to use the default. The selected paths are saved in the
user-local configuration file and reused on the next run:

```bash
make docker-local-full-up rbac=local rbac-config=local
```

The saved path file is `$XDG_CONFIG_HOME/insights-rbac/local-stack.env`, or
`~/.config/insights-rbac/local-stack.env` when `XDG_CONFIG_HOME` is not set.

The default local directories for Kessel Inventory and HBI are
`.local-deps/inventory-api` and `.local-deps/insights-host-inventory`.

If the Docker stack is already running, the command rebuilds and recreates the
services affected by the selected sources and prints a deployment summary.
For example, rerunning the local command rebuilds RBAC and the selected local
or fetched service sources.

##### Run the basic V2 API validation

After the stack is healthy, run the basic V2 API lifecycle validation:

```bash
scripts/validations/api/v2-crud.sh
```

The validator requires no identity setup. It exercises every V2 API route:
workspace CRUD/query/move, role CRUD, role-binding create/read/update, and the
read-only principal routes. It temporarily enables V2 writes and adds the
minimum local Kessel authorization tuples, then removes its test resources and
restores the original V2-write setting and Kessel graph. It prints the direct
Kessel tuples for read-only scenarios and verifies the expected persisted tuple
after every write; deletion checks verify that the tuple disappears.

The validator creates a temporary user from an in-memory YAML fixture in
`org_id=11111` with a unique `v2-crud-user-<timestamp>-<pid>` identity. It
deletes that user, its temporary role, and its binding during cleanup. The
workspace RYW validation uses `local-v2-org-admin` by default. Select one of
the four loaded identities with `--user`; the full mapping is listed in the
[local Docker/Podman validation guide](docs/local-docker-validation.md).
The full stack includes HBI, but the current RYW helper validates the RBAC
workspace path only; `--check-hbi` is informational until an HBI assertion is
implemented.

To inspect the organizations, persisted users/principals, V2 roles, and role
bindings in the running RBAC container, run the read-only action:

```bash
scripts/validations/api/actions/list-rbac-users.sh
# or
make docker-local-full-list-users
```

To call the individual read-only API list endpoints, use the scripts below.
They default to `local-v2-org-admin`; the only supported parameter is the
loaded user identity:

```bash
scripts/validations/api/list-v1-roles.sh --user local-v1-org-admin
scripts/validations/api/list-v1-groups.sh --user local-v1-non-org-admin
scripts/validations/api/list-v2-roles.sh --user local-v2-org-admin
scripts/validations/api/list-v2-principals.sh --user local-v2-non-admin
scripts/validations/api/list-v2-workspaces.sh --user local-v2-org-admin
scripts/validations/api/list-v2-role-bindings.sh --user local-v2-org-admin
```

The same action can generate a local admin or non-admin identity for V1 or V2
API testing; see the [local Docker/Podman validation guide](docs/local-docker-validation.md).

To create persisted local users, bootstrap a tenant, and configure groups,
V2 roles, and role bindings, follow the [local user and group management
guide](docs/local-docker-validation.md#manage-local-users-and-groups):

```bash
scripts/validations/api/actions/apply-rbac-users-config.sh
# or
make docker-local-full-apply-users file=/tmp/local-rbac-users.yaml
```

##### Run every local validation

Run every shell validation below `scripts/validations/` in lexical order. The
command stops at the first failure:

```bash
make docker-local-full-validate
```

See [Local Validation Script Guidelines](docs/validation-script-guidelines.md)
when adding a validation for a new feature.

For the complete local Docker/Podman startup and validation workflow, see the
[Local Docker/Podman validation guide](docs/local-docker-validation.md).

Useful endpoints after startup:

| Service | Endpoint |
| --- | --- |
| RBAC API and metrics | http://localhost:9080 and http://localhost:9080/metrics |
| Kessel Relations API | http://localhost:9000 |
| Kessel Inventory API | http://localhost:9081 |
| Kafka Connect | http://localhost:8083 |
| Host Inventory API | http://localhost:8080 |

Verify a workspace create, the RBAC Read-Your-Writes notification, and that
the workspace is visible through Kessel Inventory, which Host Inventory uses
as its workspace source of truth:

```bash
./scripts/validations/api/create-workspace.sh --no-start --check-hbi
```

Stop the full stack and leftover legacy RBAC containers with
`make docker-local-full-down`. This preserves volumes;
pass `--volumes` to `scripts/local_stack/down-full.sh` when a clean HBI data
volume is required. If the RBAC Kafka consumer is unhealthy, restart the stack
with `make docker-local-full-up`; the consumer should report that it acquired a
fencing lock and is listening on `outbox.event.relations-replication-event`.

## Testing

Tests require a running PostgreSQL instance (SQLite is not supported):

```bash
make start-db                                      # Ensure Postgres is running

# Full test suite with coverage
pipenv run tox -e py312

# Fast test suite (no coverage)
pipenv run tox -e py312-fast

# Single test module (dotted path, not file path)
pipenv run tox -e py312-fast -- tests.management.role.test_view
```

See [docs/testing-guidelines.md](docs/testing-guidelines.md) for base classes, v2 test setup, and mocking patterns.

## Linting and Formatting

```bash
pipenv run tox -e lint                          # flake8 + black --check
pipenv run black -t py312 -l 119 rbac tests     # Auto-format
pipenv run pre-commit run --all-files            # Run all pre-commit hooks
```

## Database

```bash
make make-migrations     # Generate migration files
make run-migrations      # Apply migrations
make reinitdb            # Drop, recreate, and migrate
```

Direct access: `psql postgres -U postgres -h localhost -p 15432`

## API Documentation

- V1 API specs: [docs/source/specs/](docs/source/specs/)
- V2 OpenAPI spec: [docs/source/specs/v2/openapi.yaml](docs/source/specs/v2/openapi.yaml)
- V2 TypeSpec source: [docs/source/specs/typespec/main.tsp](docs/source/specs/typespec/main.tsp)
- MCP endpoint (AI agent interface): [docs/MCP.md](docs/MCP.md)

Regenerate the v2 spec from TypeSpec:

```bash
make generate_v2_spec
```

## Environment Variables

Key environment variables (see [docker-compose.yml](docker-compose.yml) for a full reference):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_HOST` | PostgreSQL host | `localhost` |
| `DATABASE_PORT` | PostgreSQL port | `15432` |
| `DATABASE_NAME` | Database name | `postgres` |
| `REDIS_HOST` | Redis host | `rbac_redis` |
| `API_PATH_PREFIX` | API URL prefix | `/api/rbac` |
| `V2_APIS_ENABLED` | Enable v2 API routes | `False` |
| `KAFKA_ENABLED` | Enable Kafka producer/consumer | `False` |
| `DEVELOPMENT` | Development mode flag | `False` |
| `MCP_ENABLED` | Enable MCP endpoint (`/_private/_a2s/mcp/`) | `True` |
| `MCP_WRITE_ENABLED` | Enable MCP write operations | `False` |

## Project Structure

```
rbac/
  api/            # V1 API views, serializers, URLs
  management/     # Core business logic (models, services, views per domain)
  internal/       # Internal/service-to-service API
  core/           # Shared utilities, middleware, error handling
  rbac/           # Django project settings, WSGI, Celery config
  migration_tool/ # V1-to-V2 migration utilities
tests/            # Test suite (mirrors rbac/ structure)
docs/             # Architecture and domain guideline docs
```

## Further Reading

- [CONTRIBUTING.md](CONTRIBUTING.md) -- How to contribute
- [AGENTS.md](AGENTS.md) -- AI agent guidance and codebase conventions
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) -- System architecture and data flow
- [docs/security-guidelines.md](docs/security-guidelines.md) -- Authentication and authorization
- [docs/api-contracts-guidelines.md](docs/api-contracts-guidelines.md) -- API versioning and contracts
- [docs/database-guidelines.md](docs/database-guidelines.md) -- Multi-tenancy, models, migrations
- [docs/integration-guidelines.md](docs/integration-guidelines.md) -- Kessel, Kafka, external services
- [docs/performance-guidelines.md](docs/performance-guidelines.md) -- Caching, query optimization
- [docs/error-handling-guidelines.md](docs/error-handling-guidelines.md) -- Error formats and exceptions
- [docs/testing-guidelines.md](docs/testing-guidelines.md) -- Test runner, base classes, patterns
- [docs/MCP.md](docs/MCP.md) -- MCP endpoint developer guide (protocol, tools, adding tools)
- [docs/MCP-operator-guide.md](docs/MCP-operator-guide.md) -- MCP endpoint operator guide (deployment, config, security)

## License

This project is licensed under the GNU AGPL v3. See [LICENSE](LICENSE) for details.
