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
- **Authorization**: Kessel Relations (SpiceDB-based, gRPC)
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
