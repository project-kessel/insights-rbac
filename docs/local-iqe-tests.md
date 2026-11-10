# Run IQE RBAC tests locally

Use [`scripts/setup_iqe_plugin.sh`](../scripts/setup_iqe_plugin.sh) from the
`insights-rbac` repository root to install and run the IQE RBAC plugin against
the local API at `http://localhost:9080`.

The launcher uses the local `smoke` environment by default.

## 1. Set up the IQE environment

You need:

- Python 3.12 available as `python3`.
- Git, `curl`, `wget`, and `uv` on your `PATH`.
- Access to the configured IQE package index.
- An IQE RBAC plugin checkout. By default, setup expects it at
  `$HOME/Projects/iqe-rbac-plugin`; you can configure another location.
- A running Docker or Podman engine for the local stack.

Run:

```bash
scripts/setup_iqe_plugin.sh setup
```

Setup recreates `$HOME/Venvs/iqe-rbac-plugin`, installs IQE and its dependencies,
and installs the plugin checkout in editable mode. To use a checkout at another
path, set `IQE_RBAC_PLUGIN_DIR` for setup:

```bash
IQE_RBAC_PLUGIN_DIR="$HOME/src/iqe-rbac-plugin" scripts/setup_iqe_plugin.sh setup
```

If not set, `IQE_RBAC_PLUGIN_DIR` defaults to
`$HOME/Projects/iqe-rbac-plugin`. Setup reports an error if the checkout is
missing from the configured path.

Run setup when installing or rebuilding the environment. Subsequent test runs
activate the virtualenv automatically.

## 2. Start the local RBAC stack

With a local `rbac-config` checkout at `../rbac-config`, run:

```bash
make docker-local-full-up rbac=local rbac-config=local
make docker-local-full-health
```

The startup command prompts for local checkout paths and remembers your
selection. To use upstream configuration instead of a local `rbac-config`
checkout, start with:

```bash
make docker-local-full-up rbac=local rbac-config=upstream
```

See the [local stack guide](validation-script-guidelines.md#run-validations-on-the-local-full-stack)
for stack prerequisites and source selection.

## 3. Run the suite

```bash
scripts/setup_iqe_plugin.sh test
```

The launcher selects the local smoke environment, configures the API clients
to use localhost, disables Vault loading, and raises the test process's soft
open-file limit to at least 1,024 for concurrency tests. Smoke authentication
uses configured identity headers.

Runs stop on the first failure because `PYTEST_ADDOPTS` defaults to `-x`.
To continue after failures:

```bash
PYTEST_ADDOPTS='--maxfail=0' scripts/setup_iqe_plugin.sh test
```

This command selects the entire plugin suite. Individual tests can still be
skipped when the smoke environment lacks their required services, credentials,
users, or supported scenarios. A passing local run covers the tests that ran;
skipped tests require a suitable environment.

## 4. Run tests with a filter

Run tests matching `groups`:

```bash
scripts/setup_iqe_plugin.sh filter 'groups'
```

Run a specific test by name:

```bash
scripts/setup_iqe_plugin.sh filter 'test_concurrent_group_modification_multi_user'
```

Combine conditions:

```bash
scripts/setup_iqe_plugin.sh filter 'groups and not service_account'
```

The filter is passed to pytest's `-k` option. It matches test names, parent
names, and parameter names, so `groups` can also select workspace tests whose
parameters contain `groups`. Quote the expression, especially when it contains
spaces. Tests excluded by the filter are reported as `deselected`.

## Add or change tests

Edit or add `test_*.py` files under:

```text
$IQE_RBAC_PLUGIN_DIR/iqe_rbac/tests/
```

The default `IQE_RBAC_PLUGIN_DIR` is `$HOME/Projects/iqe-rbac-plugin`. If you
configured a custom checkout with the `IQE_RBAC_PLUGIN_DIR` environment
variable, use that path instead.

The editable installation picks up these changes without rerunning setup.
Run the new test by name with `filter`, then run the suite as needed. Changes
to the RBAC service itself require rebuilding the local stack with the startup
command above.

## Troubleshooting

### Local RBAC API is not reachable

Start the stack and check its health with the commands in step 2. The test
launcher expects RBAC on port 9080.

### `iqe: command not found`

Run `scripts/setup_iqe_plugin.sh setup` to install IQE. The launcher activates
its virtualenv for each test run. For direct IQE commands in your shell, first
activate it yourself:

```bash
source "$HOME/Venvs/iqe-rbac-plugin/bin/activate"
```

### Inspect failures or skipped tests

Request a full traceback and skip reasons:

```bash
PYTEST_ADDOPTS='-x --tb=long -rs' scripts/setup_iqe_plugin.sh test
```

The same options work with `filter`:

```bash
PYTEST_ADDOPTS='-x --tb=long -rs' scripts/setup_iqe_plugin.sh filter 'groups'
```

If the launcher reports that the hard open-file limit is below 1,024, raise
that limit in the environment launching the script. The launcher raises the
soft limit automatically when the hard limit permits it.

## Stop the stack

```bash
make docker-local-full-down
```

This preserves the stack's data volumes.
