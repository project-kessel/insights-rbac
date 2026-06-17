---
name: local-dev
description: How to run unit tests, linting, and type checking for local development
---

# Local Development Commands

Use this skill to run unit tests, linting, and type checking for the insights-rbac project.

## Running Unit Tests

### Using Tox (Recommended)

**Run tests with coverage:**
```bash
tox -e py312
```

**Run tests without coverage (faster):**
```bash
tox -e py312-fast
```

**Run tests and show slowest tests:**
```bash
tox -e py312-profile
```

**Run a specific test:**
```bash
tox -e py312 -- tests.module.path.TestClass.test_method
```

**Run a specific test with keepdb (faster, reuses existing test database):**
```bash
tox -e py312 -- tests.module.path.TestClass.test_method --keepdb
```

**Run all tests in a test file:**
```bash
tox -e py312 -- tests.module.path
```

**Run all tests in a test class:**
```bash
tox -e py312 -- tests.module.path.TestClass
```

### Using Make

**Run Django tests directly:**
```bash
make unittest
```

**Run fast tests (without coverage):**
```bash
make unittest-fast
```

**Run tests with profiling:**
```bash
make unittest-profile
```

## Running Linting

### Using Tox

**Run linting checks (flake8 and black):**
```bash
tox -e lint
```

### Using Make

**Run linting:**
```bash
make lint
```

**Format code (fix linting errors):**
```bash
make format
```

## Type Checking

### Using Tox

**Run type checking with mypy:**
```bash
tox -e mypy
```

### Using Make

**Run type checking:**
```bash
make typecheck
```

## Quick Reference

| Task | Tox Command | Make Command |
|------|-------------|--------------|
| Run tests with coverage | `tox -e py312` | `make unittest` |
| Run tests (fast, no coverage) | `tox -e py312-fast` | `make unittest-fast` |
| Profile slow tests | `tox -e py312-profile` | `make unittest-profile` |
| Run specific test | `tox -e py312 -- tests.module.path.TestClass.test_method` | N/A |
| Run specific test (keepdb) | `tox -e py312 -- tests.module.path.TestClass.test_method --keepdb` | N/A |
| Run linting | `tox -e lint` | `make lint` |
| Format code | N/A | `make format` |
| Type checking | `tox -e mypy` | `make typecheck` |

## Notes

- The `tox` commands automatically provision the pipenv environment before running tests
- Use `make format` to automatically fix formatting issues found by black
- The `py312-fast` environment runs tests in parallel with `--failfast` flag for faster feedback
- All test environments require a running PostgreSQL database (typically via `docker-compose up -d db`)
