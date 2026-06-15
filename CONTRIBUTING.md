# Contributing to insights-rbac

Thank you for contributing to the RBAC service. This guide covers the workflows, conventions, and expectations for all contributors -- both human and AI.

## Getting Started

1. Clone the repository:

   ```bash
   git clone https://github.com/project-kessel/insights-rbac.git
   cd insights-rbac
   ```

2. Install dependencies:

   ```bash
   pipenv install --dev
   ```

3. Start the database and apply migrations:

   ```bash
   make start-db
   make run-migrations
   ```

4. Install pre-commit hooks:

   ```bash
   pipenv run pre-commit install
   ```

## Branching Strategy

- The `master` branch is the source of truth. All work branches from and merges back into `master`.
- Create feature branches with a descriptive name, ideally prefixed with the JIRA ticket number:

  ```
  feature/RHCLOUD-12345-add-workspace-endpoint
  fix/RHCLOUD-12346-role-cache-invalidation
  ```

- Rebase your branch onto `master` before opening a pull request.

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
type(scope): short description in lowercase
```

**Types**: `fix`, `feat`, `test`, `refactor`, `style`, `docs`, `chore`

**Scopes** (optional): `roles`, `permissions`, `groups`, `workspaces`, `role-bindings`, `deps`, etc.

Examples:

```
feat(workspaces): add recursive child workspace query
fix(roles): prevent duplicate system role creation on seed
test(permissions): add v2 permission filter backend coverage
chore(deps): bump django to 5.2.13
```

- Subject line: lowercase after the colon, imperative mood
- Add a body for non-trivial changes to explain the motivation

## Pull Request Process

1. **Before opening a PR**:
   - Rebase onto latest `master`
   - Run the full test suite: `pipenv run tox -e py312-fast`
   - Run linting: `pipenv run tox -e lint`
   - Fix any formatting issues: `pipenv run black -t py312 -l 119 rbac tests`

2. **PR title format**: `[RHCLOUD-XXXXX] Short description`
   - The bracketed ticket number links to JIRA automatically

3. **PR description** should include:
   - Link to the JIRA ticket
   - Summary of changes and motivation
   - Testing instructions for reviewers
   - Any prerequisites or deployment considerations

4. **Review workflow**:
   - Self-review your code before requesting reviews
   - Ensure all CI pipelines pass (Konflux, pre-commit, Snyk, PlatSec)
   - Address review feedback promptly
   - Squash and merge for small PRs to keep history clean

5. **After merge**: Move the JIRA ticket to **ON QA** status.

## Coding Standards

### General

- **Python 3.12** -- use modern Python features where appropriate
- **Black** for formatting: line length 119, target py312
- **Flake8** for linting: max line length 120
- **Import order**: PyCharm style (`flake8-import-order`), application imports are `rbac` and `api`
- Pre-commit hooks enforce all of the above automatically

### Architecture Patterns

- **Service layer**: Business logic belongs in `service.py` files within each domain module (e.g., `rbac/management/role/service.py`). Never put business logic in views or serializers.
- **Multi-tenancy**: All business models inherit `TenantAwareModel`. Always filter queries by `request.tenant`.
- **V2 views**: Extend `BaseV2ViewSet`. Write operations use `AtomicOperationsMixin` -- override `perform_atomic_create`/`perform_atomic_update`/`perform_atomic_destroy`, not `create`/`update`/`destroy`.
- **Error format**: V2 APIs use RFC 7807 Problem JSON. V1 uses flat `errors` arrays. Do not mix formats.
- **Feature flags**: V2 routes only register when `V2_APIS_ENABLED=True`. Never register v2 routes unconditionally.

For the complete list of patterns and conventions, see [AGENTS.md](AGENTS.md) and the [docs/](docs/) guidelines.

### What to Avoid

- Do not modify v1 API behavior -- it is stable and widely consumed
- Do not put business logic in serializers or views
- Do not expose integer primary keys in APIs -- use UUIDs
- Do not raise DRF exceptions from service layer code -- raise domain exceptions
- Do not skip pre-commit hooks or bypass formatting
- Do not perform outbox writes or external calls outside a transaction boundary

## Testing

Tests run against real PostgreSQL (SQLite is not used). The test runner requires **dotted module paths**, not file paths.

```bash
# Full suite with coverage
pipenv run tox -e py312

# Fast suite (no coverage)
pipenv run tox -e py312-fast

# Single test class
pipenv run tox -e py312-fast -- tests.management.role.test_view.RoleViewsetTests
```

### Test expectations

- Use `IdentityRequest` base class for tests (provides tenant, identity headers, request context)
- For v2 tests: apply `@override_settings(V2_APIS_ENABLED=True)`, reload URLs in `setUp`, bootstrap tenant with `bootstrap_tenant_for_v2_test()`, mock Kessel permission checks
- Always mock Kafka, Kessel Inventory, and the outbox replicator
- Never mock Django ORM queries, serializer validation, or URL routing
- Tests run in parallel with `--failfast` -- clean up objects in `tearDown`
- Assert both return values and side effects

See [docs/testing-guidelines.md](docs/testing-guidelines.md) for full details.

## Database Migrations

- Generate: `make make-migrations`
- Apply: `make run-migrations`
- Reset: `make reinitdb` (drops and recreates)
- New models should use UUID v7 as primary key (`uuid_utils.compat.uuid7`)
- Always test migrations against real PostgreSQL
- Migrations are excluded from linting and coverage

## CI / Pipelines

The repository uses Konflux for CI. Key checks on PRs:

- `ci.ext.devshift.net PR build` (required)
- Konflux build pipeline
- Pre-commit hooks (flake8, black, trailing whitespace, django-upgrade, openapi-spec-validator)
- Snyk security scans
- PlatSec checks

If a Konflux pipeline fails due to infrastructure issues (not code), retry by commenting `/retest` on the PR.

## Documentation

- **README.md** -- Project overview and quick start
- **CONTRIBUTING.md** -- This file
- **AGENTS.md** -- AI agent guidance and codebase conventions
- **docs/ARCHITECTURE.md** -- System architecture and data flow
- **docs/*-guidelines.md** -- Domain-specific guidelines (security, performance, testing, etc.)

When changing behavior covered by existing documentation, update the relevant docs as part of your PR.

## Questions

If you have questions about the codebase or contribution process, reach out to the team via the project's communication channels or open a discussion on the repository.
