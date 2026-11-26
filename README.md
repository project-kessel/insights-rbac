# insights-rbac

Role-Based Access Control (RBAC) service for [console.redhat.com](https://console.redhat.com). Manages roles, permissions, groups, and workspaces that control user access across the Hybrid Cloud Console platform.

## Overview

insights-rbac is a Django REST Framework microservice that provides two API versions:

- **V1 API** -- stable, widely consumed REST API for managing roles, groups, policies, and permissions
- **V2 API** -- next-generation API with workspace-based access control, RFC 7807 error responses, and Kessel integration for authorization

The service is multi-tenant: every request is scoped to an organization (tenant) via identity headers injected by the platform's authentication gateway.

Find guides and references by audience in the [documentation index](#documentation-index).

## Tech Stack

- **Language**: Python 3.12
- **Framework**: Django 6.0 / Django REST Framework
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
- Docker and Docker Compose for the standard Compose and database targets; the
  full local integration stack also supports Podman
- PostgreSQL development headers when needed to install `psycopg2` from source

### Option 1: Docker Compose (full stack)

Starts the RBAC server, PostgreSQL, Redis, Celery worker, and Celery beat scheduler:

```bash
make docker-up       # App available at http://localhost:9080
make docker-logs     # Tail all container logs
make docker-down     # Stop and remove containers
```

### Option 2: Local Python (app only)

Run the Django server locally with PostgreSQL and Redis available. The database
target below starts PostgreSQL; start Redis separately if needed. Make targets
that run Django read `.env`.

```bash
pipenv install --dev     # Install dependencies
cp .env.example .env     # Configure local settings
```

For the PostgreSQL container started below, edit `.env` and set
`DATABASE_HOST=localhost` and `DATABASE_PORT=15432` before running migrations.
Use `REDIS_HOST=localhost` and `REDIS_PORT=6379` for a host-accessible Redis.
See [Environment Variables](#environment-variables) for mode-specific settings
and switching between modes.

```bash
make start-db            # Start Postgres on port 15432
pipenv run make run-migrations  # Apply database migrations
pipenv run make serve           # App available at http://localhost:8000
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

Run the validation scripts against the healthy stack:

```bash
make docker-local-full-validate
```

The target runs each validation script directly, passing `--no-start` to
`create-workspace.sh`. It also runs `apply-rbac-users-config.sh` with its
bundled fixture; this may create or update tenants, users, groups, custom
roles, and role bindings in local RBAC.

The V2 CRUD validation uses the host `zed` CLI when available, otherwise it
runs the `authzed/zed` container on the selected Kessel network.

When finished, stop the full local stack:

```bash
make docker-local-full-down
```

Starting the stack builds `insights-rbac-local:dev`, uses Docker or Podman
Compose, and resolves the upstream Kessel Inventory and Host Inventory
checkouts under `.local-deps/`. If either checkout is absent, it creates a
shallow clone.
Ensure the container VM has enough memory for the full stack.
If the local RBAC image build cannot reach package mirrors through the default
BuildKit network, set `RBAC_BUILD_NETWORK=host` for the startup command.

To run alongside another local RBAC stack, set an isolated Compose project,
network, data directories, and port offset. Each isolated project gets a
project-specific RBAC image tag unless `RBAC_IMAGE` is set explicitly. For
example:

```bash
export FULL_STACK_SLOT=example
export FULL_STACK_PORT_OFFSET=2000
export FULL_KESSEL_COMPOSE_DIR="${TMPDIR:-/tmp}/insights-rbac-example/development/full-kessel"
export COMPOSE_PROJECT_NAME=example-kessel
export HBI_COMPOSE_PROJECT=example-hbi
export KESSEL_NETWORK_NAME=example-kessel-net
export HBI_PG_DATA="${TMPDIR:-/tmp}/insights-rbac-example/hbi-postgres"
export HBI_MINIO_DATA="${TMPDIR:-/tmp}/insights-rbac-example/hbi-minio"
make docker-local-full-up rbac=local rbac-config=upstream inventory=upstream hbi=upstream
make docker-local-full-health
make docker-local-full-validate
make docker-local-full-down
```

Keep these variables exported for every command; `make` and its child scripts
cannot read shell variables that were only sourced without `export`.

When `FULL_KESSEL_COMPOSE_DIR` is set, startup, health checks, and
validations derive published ports from `FULL_STACK_PORT_OFFSET`; explicit
port variables override those defaults.
`docker-local-full-down` stops the selected full-stack projects and leaves
separate RBAC Compose projects running.

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

Run every shell script below `scripts/validations/` in lexical order, including
scripts that apply local RBAC fixtures. The command stops at the first failure:

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
| Kessel Inventory API | http://localhost:9081 |
| Kafka Connect | http://localhost:8083 |
| Host Inventory API | http://localhost:8080 |

Verify a workspace create, the RBAC Read-Your-Writes notification, and that
the workspace is visible through Kessel Inventory, which Host Inventory uses
as its workspace source of truth:

```bash
./scripts/validations/api/create-workspace.sh --no-start --check-hbi
```

Stop the full stack with `make docker-local-full-down`. This preserves HBI
volumes and, when using an isolated Kessel Compose directory, its Kessel
volumes. Pass `--volumes` to `scripts/local_stack/down-full.sh` to remove
those volumes. If the RBAC Kafka consumer is unhealthy, restart the stack
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

### IQE API tests against the local stack

See [Run IQE RBAC tests locally](docs/local-iqe-tests.md) for setup and full-suite
or filtered runs. From this repository root, after setup and starting the stack:

```bash
scripts/setup_iqe_plugin.sh test
scripts/setup_iqe_plugin.sh filter 'groups'
```

## Linting and Formatting

```bash
pipenv run tox -e lint                          # flake8 + black --check
pipenv run black -t py312 -l 119 rbac tests     # Auto-format
pipenv run pre-commit run --all-files            # Run all pre-commit hooks
```

## Database

```bash
pipenv run make make-migrations  # Generate migration files
pipenv run make run-migrations   # Apply migrations
pipenv run make reinitdb         # Drop, recreate, and migrate
```

Direct access: `psql postgres -U postgres -h localhost -p 15432`

To migrate V1 data to V2 relations, inspect the available options with
`pipenv run python rbac/manage.py migrate_relations --help`.

### Seeds

Permissions, roles, and groups are seeded at startup by default. Set
`PERMISSION_SEEDING_ENABLED`, `ROLE_SEEDING_ENABLED`, or `GROUP_SEEDING_ENABLED`
to `False` to disable each seed. The local definitions are under
[`rbac/management/role/definitions/`](rbac/management/role/definitions/);
deployed default role and group configuration is maintained in
[`rbac-config`](https://github.com/project-kessel/rbac-config).

To seed manually, run `pipenv run python rbac/manage.py seeds`; use `--roles`,
`--groups`, or `--permissions` to select seed types.

### Kafka and Debezium

The full integration stack above includes Kafka and Debezium. Alternatively,
`scripts/debezium/setup_debezium.sh` sets up the standalone CDC stack using
Docker or Podman. It starts PostgreSQL, Kafka, Kafka Connect, and Kafdrop and
configures the connector. Ensure the required ports are available; use
`scripts/debezium/setup_debezium.sh --help` for options and prerequisites.

```bash
scripts/debezium/setup_debezium.sh
scripts/debezium/setup_debezium.sh --consumer     # or --consumer-bg
scripts/debezium/send_test_relations_message.sh
```

See [Kafka consumer documentation](docs/KAFKA_CONSUMER.md) and
[outbound events](docs/kafka-outbound-events.md) for details.

### Feature flags

Local Unleash configuration uses `FEATURE_FLAGS_URL` (default
`http://localhost:4242/api`), `FEATURE_FLAGS_TOKEN`, and
`FEATURE_FLAGS_CACHE_DIR` (default `/tmp/`). In Clowder, the URL and token can
come from the platform configuration. See [Unleash's quick start](https://docs.getunleash.io/quickstart)
and [`rbac/feature_flags.py`](rbac/feature_flags.py) for flag usage and context.

## Authentication and deployment

The gateway supplies `x-rh-identity` for public API requests; local development
can use the development identity middleware. Service-to-service requests may use
PSK or JWT authentication. See [the authentication guide](docs/AUTHENTICATION.md)
for requirements and local examples. For ephemeral deployments, see
[`insights-service-deployer`](https://github.com/project-kessel/insights-service-deployer).

## API Documentation

- REST consumer guide: [docs/API-USAGE.md](docs/API-USAGE.md)
- V1 API specs: [docs/source/specs/](docs/source/specs/)
- V2 OpenAPI spec: [docs/source/specs/v2/openapi.yaml](docs/source/specs/v2/openapi.yaml)
- V2 TypeSpec source: [docs/source/specs/typespec/main.tsp](docs/source/specs/typespec/main.tsp)
- MCP endpoint (AI agent interface): [docs/MCP.md](docs/MCP.md)

Regenerate the v2 spec from TypeSpec:

```bash
make generate_v2_spec
```

## Environment Variables

### Local connection settings by run mode

These are local development examples, not universal application defaults.
Choose addresses from the perspective of the process running Django:

| Variable | Docker Compose server | Local Python using published ports |
|----------|-----------------------|------------------------------------|
| `DATABASE_HOST` | `db` | `localhost` |
| `DATABASE_PORT` | `5432` | `15432` |
| `REDIS_HOST` | `rbac_redis` | `localhost` |
| `REDIS_PORT` | `6379` | `6379` |

The Compose server sets the database address and defaults `REDIS_HOST` to
`rbac_redis`; its worker and scheduler use the Redis service address
`redis:6379` for their broker. Compose publishes PostgreSQL on host port
`15432` and Redis on host port `6379`. The local Python column assumes those
ports are available; use the actual host and port for other instances.

When switching to local Python, update the copied `.env` database host **and**
port before running migrations or starting Django. The Compose name `db` is
not a host address, and `5432` is the container port, not the published port.
When switching back, use the Compose addresses for processes in the Compose
network. Check any `REDIS_HOST` override as well: `localhost` inside a
container refers to that container, while `rbac_redis` is not a host address.
Compose explicitly sets the server's database variables and uses its own Redis
host default; a shell or Compose `.env` value can override `REDIS_HOST`.

### Application defaults

Key application defaults when not otherwise configured (see
[docker-compose.yml](docker-compose.yml), [`rbac/rbac/database.py`](rbac/rbac/database.py),
and [`rbac/rbac/settings.py`](rbac/rbac/settings.py)):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_HOST` | PostgreSQL host | unset |
| `DATABASE_PORT` | PostgreSQL port | unset |
| `DATABASE_NAME` | Database name | unset |
| `REDIS_HOST` | Redis host | `localhost` |
| `REDIS_PORT` | Redis port | `6379` |
| `API_PATH_PREFIX` | API URL prefix | `api/` for routes, `/` for static paths |
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

## Documentation Index

### API Users

- [Authentication guide](docs/AUTHENTICATION.md) -- Authentication methods, API paths, and local examples.
- [V1 OpenAPI](docs/source/specs/openapi.json) -- V1 REST API contract.
- [V2 OpenAPI](docs/source/specs/v2/openapi.yaml) -- Generated V2 REST API contract.
- [V2 OpenAPI JSON](docs/source/specs/v2/openapi.json) -- JSON form of the V2 REST API contract.
- [V2 TypeSpec](docs/source/specs/typespec/main.tsp) -- Source for the V2 API contract.
- [Relations OpenAPI](docs/source/specs/relations-openapi.yml) -- Relations API contract.
- [Role bindings](docs/role_bindings.md) -- V1 and V2 bindings, scopes, synchronization, and queries.
- [MCP endpoint](docs/MCP.md) -- MCP protocol, tools, and extension guide.
- [Kafka outbound events](docs/kafka-outbound-events.md) -- Topics, event formats, and producer behavior.

### Contributors

- [Contributing](CONTRIBUTING.md) -- Contribution workflow, style, and pull requests.
- [Agent guidance](AGENTS.md) -- Repository conventions and domain-specific guidance for AI agents.
- [Claude guidance](CLAUDE.md) -- Claude-specific preferences and the shared agent guidance.
- [Architecture](docs/ARCHITECTURE.md) -- System components, request flow, and integrations.
- [API contracts](docs/api-contracts-guidelines.md) -- API versioning, view patterns, and specifications.
- [Database guidelines](docs/database-guidelines.md) -- Tenancy, models, migrations, and the outbox.
- [Security guidelines](docs/security-guidelines.md) -- Authentication, authorization, and tenant isolation rules.
- [Error handling](docs/error-handling-guidelines.md) -- Error formats, exceptions, and logging conventions.
- [Integration guidelines](docs/integration-guidelines.md) -- Kessel, Kafka, external services, and metrics.
- [Performance guidelines](docs/performance-guidelines.md) -- Caching, query optimization, and transactions.
- [Testing guidelines](docs/testing-guidelines.md) -- Test runner, fixtures, and mocking patterns.
- [Pre-commit setup](docs/pre-commit-setup.md) -- Install and run repository hooks.
- [CodeRabbit review knowledge](docs/coderabbit-rbac-knowledge.md) -- RBAC invariants used in automated reviews.

### Operators

- [Authentication guide](docs/AUTHENTICATION.md) -- Deployment authentication, configuration, and troubleshooting.
- [Kafka consumer](docs/KAFKA_CONSUMER.md) -- Consumer operation, configuration, and monitoring.
- [Kafka outbound events](docs/kafka-outbound-events.md) -- Produced topics and event formats.
- [V2 org determination](docs/v2-org-determination.md) -- How tenant state and flags affect V2 access decisions during rollout.
- [V2 rollout operator guide](docs/V2-ROLLOUT-GUIDE.md) -- Opt-in, bootstrap, backfill, validation, and recovery boundaries.
- [MCP operator guide](docs/MCP-operator-guide.md) -- MCP deployment, security, metrics, and troubleshooting.
- [Kessel workspace view check](docs/kessel_check_rbac_workspace_view.md) -- Tenant workspace-view permission evaluation.
- [Kessel role-binding view check](docs/kessel_check_role_binding_view.md) -- Role-binding permission evaluation and required tuples.
- [Role-binding view diagrams](docs/kessel_check_role_binding_view_diagrams.md) -- Diagrams of Kessel role-binding checks.
- [Ephemeral DR toolkit](docs/ephemeral-dr-toolkit.md) -- Deploy and use disaster recovery tools in an ephemeral cluster.

### Testers

- [Testing guidelines](docs/testing-guidelines.md) -- Run Django tests and use test fixtures and mocks.
- [Local Docker/Podman validation](docs/local-docker-validation.md) -- Validate a local integration stack and manage users and groups.
- [Local validation identities](docs/local-validation-identities.md) -- Choose local identities and access fixtures.
- [Local IQE tests](docs/local-iqe-tests.md) -- Run IQE RBAC tests against the local stack.
- [Validation script guidelines](docs/validation-script-guidelines.md) -- Write and run local validation scripts.
- [MCP scenario validation](docs/mcp-scenario-validation.md) -- Exercise MCP scenarios against real organizations.
- [Ephemeral DR toolkit](docs/ephemeral-dr-toolkit.md) -- Validate disaster recovery workflows in an ephemeral cluster.
- [OCM performance results](tests/performance/README.md) -- Recorded local concurrent and linear test results.

## License

This project is licensed under the GNU AGPL v3. See [LICENSE](LICENSE) for details.
