# Local validation identities and access fixtures

This guide explains how to create local RBAC user records for API validation,
choose V1 or V2 routes, represent org administrators and non-admins, grant V2
permissions through the supported fixture helper, and clean up validation data.

Use it with a running local full stack. The helper writes local RBAC data and
publishes relations through the normal application outbox path. It is not for
stage or production data.

## The four things that are easy to confuse

| Concept | Where it is set | What it controls |
| --- | --- | --- |
| Organization | `org_id` in the fixture and identity header | The RBAC tenant and the organization context for the request |
| API version | Request URL, such as `/api/rbac/v1/...` or `/api/rbac/v2/...` | Which API route handles the request |
| User identity | `username` and `user_id` under each fixture user; tenant `org_id` and `account_id`; matching values in the identity header | Which persisted principal makes the request |
| Org-admin status | `admin` in the fixture and `is_org_admin` in the identity header | Whether the request is treated as an organization administrator |

There is no separate persisted “V1 user” or “V2 user” type. The same user
record can call either API version. Names such as `local-v1-org-admin` and
`local-v2-non-admin` are useful labels for test cases; the URL selects V1 or
V2. Keep API version and identity label independent when testing a matrix.

The `org_id` identifies the organization. “Non-admin” describes one user in
that organization; it does not mean a separate kind of organization.

Some full-stack startup configurations already load these convenience users:
`local-v1-org-admin`, `local-v1-non-org-admin`, `local-v2-org-admin`, and
`local-v2-non-admin`. Check with `list-rbac-users.sh` before creating another
copy. Their names identify intended test personas; they do not by themselves
create V2 role bindings.

## Choose the right local action

### List persisted users and access setup

```bash
scripts/validations/api/actions/list-rbac-users.sh
```

This is a read-only view of tenants, users/principals, V2 roles, and role
bindings in the running RBAC database.

### Generate an identity header for a request

```bash
scripts/validations/api/actions/list-rbac-users.sh generate-user \
  --v1 --admin \
  --org-id local-validation-identities \
  --account-number 10001 \
  --username local-v1-org-admin \
  --user-id local-v1-org-admin-10001
```

Choose `--v1` or `--v2`, `--admin` or `--non-admin`, and optionally set the
organization, account, username, and user ID. The command prints identity JSON,
its Base64 `x-rh-identity` value, and an example `curl` command. `--v1` and
`--v2` only change the example route; they do not create different persisted
user types.

This command generates a header; it does not load a fixture, add a V2 role, or
create a role binding. A request can trigger middleware bootstrap when its
tenant is absent, so use the fixture helper below when a repeatable persisted
user and permission setup is required.

### Create persisted users, groups, V2 roles, and bindings

```bash
scripts/validations/api/actions/apply-rbac-users-config.sh
scripts/validations/api/actions/apply-rbac-users-config.sh --dry-run
scripts/validations/api/actions/apply-rbac-users-config.sh --file /path/to/fixture.yaml
```

The declarative YAML fixture is the supported way to create local users and
V2 access through RBAC services. See the example at
[`scripts/validations/api/actions/rbac-users.yaml`](../scripts/validations/api/actions/rbac-users.yaml).

The helper supports tenant bootstrap, users and principals, groups and
memberships, V2 custom roles, and V2 role bindings. It does not write directly
to SQL, SpiceDB, or Kessel. It does not define a distinct V1 user class and it
does not create legacy V1 role-permission assignments. A V1 non-admin scenario
that needs a legacy V1 permission must use that endpoint’s supported V1 setup
or existing seed/test fixture; do not assume a V2 role binding grants V1 access.

To create user records without assigning any V2 role, a fixture can be as
small as:

```yaml
version: 1
tenants:
  - org_id: local-validation-identities
    account_id: "10001"
    bootstrap: true
    temporary: true
    users:
      - username: local-v1-org-admin
        user_id: local-v1-org-admin-10001
        admin: true
      - username: local-v1-non-org-admin
        user_id: local-v1-non-org-admin-10001
        admin: false
```

The same records could be named `local-v2-org-admin` and
`local-v2-non-admin`; that label does not change how the persisted user is
stored. Add the exact endpoint permission separately when a V2 route requires
it. An `admin: false` record is a non-admin user in the specified organization.

## Org admins and non-admins

In a user fixture, set `admin: true` for an org admin and `admin: false` for a
non-admin. When you construct the request identity, set the matching
`is_org_admin` value. Keep the username, `user_id`, `org_id`, and account number
in the header aligned with the persisted fixture user and tenant.

An admin flag is identity metadata; it does not universally grant endpoint
permissions. Authorization depends on the endpoint. For example, Group V2
list/retrieve checks the `rbac_groups_read` relation through Kessel for each
request, so an org admin still needs that relation for that endpoint. Conversely,
some V1 checks have an org-admin bypass. Check the current permission class or
endpoint contract instead of inferring access from the admin flag.

For a successful non-admin test, configure its required permission explicitly.
For a deliberate denied case, omit that user's relevant role binding and
assert the endpoint’s documented denial. If a user expected to succeed gets a
403, first check the fixture’s role, resource, subject, and applied binding;
that response alone does not prove the API behavior under test.

## Full example: V1/V2 labels, admin/non-admin, and a V2 reader role

The following fixture creates four users in one local tenant:

| Fixture username | Admin metadata | Typical test label |
| --- | --- | --- |
| `local-v1-org-admin-<run-id>` | `true` | V1-labeled org admin |
| `local-v1-non-org-admin-<run-id>` | `false` | V1-labeled non-admin |
| `local-v2-org-admin-<run-id>` | `true` | V2-labeled org admin |
| `local-v2-non-admin-<run-id>` | `false` | V2-labeled non-admin |

All four are given the same Group V2 read role here so each can make successful
Group V2 list/retrieve requests. Their `local-v1-` or `local-v2-` names do not
restrict which API route they can call. The role is only an example; replace
its permission with the exact least-privilege permission required by the API
under test.

```bash
set -euo pipefail

APPLY_FIXTURE="scripts/validations/api/actions/apply-rbac-users-config.sh"
FIXTURE="$(mktemp "${TMPDIR:-/tmp}/rbac-validation-users.XXXXXX")"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
ORG_ID="local-validation-identities"
ACCOUNT_NUMBER="10001"
ROLE_NAME="local-v2-groups-read-${RUN_ID}"
FIXTURE_APPLY_STARTED=false

cleanup() {
  local result=$?
  trap - EXIT
  if [[ "$FIXTURE_APPLY_STARTED" == true ]]; then
    "$APPLY_FIXTURE" --file "$FIXTURE" --delete || {
      printf 'Fixture cleanup failed; inspect %s and retry --delete.\n' "$FIXTURE" >&2
      result=1
    }
  fi
  rm -f "$FIXTURE"
  exit "$result"
}
trap cleanup EXIT

cat > "$FIXTURE" <<YAML
version: 1
tenants:
  - org_id: ${ORG_ID}
    account_id: "${ACCOUNT_NUMBER}"
    bootstrap: true
    temporary: true
    users:
      - username: local-v1-org-admin-${RUN_ID}
        user_id: local-v1-org-admin-${RUN_ID}
        admin: true
        role_bindings:
          - role: ${ROLE_NAME}
            resource:
              type: tenant
              id: current
      - username: local-v1-non-org-admin-${RUN_ID}
        user_id: local-v1-non-org-admin-${RUN_ID}
        admin: false
        role_bindings:
          - role: ${ROLE_NAME}
            resource:
              type: tenant
              id: current
      - username: local-v2-org-admin-${RUN_ID}
        user_id: local-v2-org-admin-${RUN_ID}
        admin: true
        role_bindings:
          - role: ${ROLE_NAME}
            resource:
              type: tenant
              id: current
      - username: local-v2-non-admin-${RUN_ID}
        user_id: local-v2-non-admin-${RUN_ID}
        admin: false
        role_bindings:
          - role: ${ROLE_NAME}
            resource:
              type: tenant
              id: current
    roles:
      - name: ${ROLE_NAME}
        description: Local Group V2 read validation
        permissions:
          - application: rbac
            resource_type: groups
            operation: read
YAML

"$APPLY_FIXTURE" --file "$FIXTURE" --dry-run
FIXTURE_APPLY_STARTED=true
"$APPLY_FIXTURE" --file "$FIXTURE"
scripts/validations/api/actions/list-rbac-users.sh
```

The fixture is marked `temporary: true` so `--delete` is allowed. Cleanup
deletes the custom role and disables the declared users. It does **not** delete
the tenant, and the helper does not delete groups; use a dedicated local
validation tenant and add cleanup for any groups or API resources created by
your test. Reusing `local-validation-identities` avoids creating a new tenant
on every run; the run-specific usernames and role names avoid collisions.

The successful apply prints an `Applied org=... users=... groups=... roles=... bindings=...`
summary. Confirm that the org and counts match the fixture, and allow the
outbox/Kessel relation to replicate before asserting successful requests.

## Build an identity header that matches a fixture user

The header must describe the same persisted user as the fixture. This shell
function mirrors the `generate-user` action and keeps the admin flag as JSON
boolean:

```bash
identity_header() {
  local username="$1"
  local user_id="$2"
  local is_org_admin="$3" # true or false
  local identity_json

  identity_json="$(jq -cn \
    --arg account_number "$ACCOUNT_NUMBER" \
    --arg org_id "$ORG_ID" \
    --arg username "$username" \
    --arg user_id "$user_id" \
    --argjson is_org_admin "$is_org_admin" \
    '{identity:{account_number:$account_number,org_id:$org_id,type:"User",user:{username:$username,email:($username+"@example.com"),is_org_admin:$is_org_admin,user_id:$user_id}}}')"
  printf '%s' "$identity_json" | base64 | tr -d '\n'
}

USERNAME="local-v1-non-org-admin-${RUN_ID}"
USER_ID="$USERNAME"
IDENTITY_HEADER="$(identity_header "$USERNAME" "$USER_ID" false)"
curl -sS -H "x-rh-identity: ${IDENTITY_HEADER}" \
  "http://localhost:9080/api/rbac/v1/principals/"

# The same persisted identity can call a V2 route; the route chooses the API version.
curl -sS -H "x-rh-identity: ${IDENTITY_HEADER}" \
  "http://localhost:9080/api/rbac/v2/groups/"
```

For the org-admin header, pass `true` and use the matching fixture user. For
each of the other three identities, pass that user's exact username and
`user_id`, plus its corresponding `true` or `false` value. Avoid printing the
Base64 header in shared logs because it contains user identity data.

## Route and identity matrix

When a validation needs all four identity profiles, iterate these users
against each relevant registered route. Do not infer the route version from
the user name:

| Identity under test | V1 route example | V2 route example |
| --- | --- | --- |
| V1-labeled org admin | `/api/rbac/v1/...` | `/api/rbac/v2/...` |
| V1-labeled non-admin | `/api/rbac/v1/...` | `/api/rbac/v2/...` |
| V2-labeled org admin | `/api/rbac/v1/...` | `/api/rbac/v2/...` |
| V2-labeled non-admin | `/api/rbac/v1/...` | `/api/rbac/v2/...` |

Expected results can differ by endpoint, admin status, and role binding. Record
the expected result for each combination. If an identity is intentionally
unauthorized, state why and assert the documented denial. Cross-tenant tests
should create or select a second tenant and use that tenant's `org_id` and
user identity; never change only the username while keeping the first tenant's
header.

### Resolve the local V2 route prefix

OpenAPI `paths` are relative to the service prefix. For Group V2, the RBAC
router registers `groups` in `rbac/management/v2_urls.py`, producing the
`/groups/` collection route. `rbac/rbac/urls.py` mounts that router below
`API_PATH_PREFIX` plus `v2/`. The prefix is configurable: its source default is
`api/`, while `docker-compose.local.yml` sets `/api/rbac` for the local stack.
Therefore the local Group V2 collection URL is
`http://localhost:9080/api/rbac/v2/groups/`. If the local stack overrides
`API_PATH_PREFIX`, use that effective value instead of the default example.

## V1 access versus V2 role bindings

The fixture helper creates V2 custom roles with permission entries shaped like
`application`, `resource_type`, and `operation`, then applies role bindings for
users or groups. It does not create V1 legacy role-permission assignments.

### Convert a permission class into fixture access

Follow this trace for the exact route and action under test. Do not infer a
fixture permission from the endpoint name or split a relation string on
underscores: relation names can be aliases, and permission components can
contain underscores.

1. Find the registered view and action in the URL router and viewset. Read its
   `permission_classes` and any method or action-specific permission logic.
   Account for every class in the tuple: all of them must allow the request.
2. Trace `has_permission` and, when present, `has_object_permission` for the
   request method and action. Record the actual outcome for org admins, V1
   legacy users, and V2 users; note feature-flag branches, bypasses, and
   fail-closed defaults. A permission class may require more than one grant.
3. Record the exact authorization check: relation or legacy permission,
   resource type and resource ID, and the principal used by the check. Follow
   helper calls until those values are resolved; do not stop at the view's
   imported permission class name.
4. For a V2 custom role, find the corresponding permission definition and
   conversion in the current RBAC source. Fixture role entries use
   `application`, `resource_type`, and `operation`. `PermissionValue.from_v2_dict`
   maps `operation` to the stored verb, and `Permission.v2_string()` turns the
   stored permission into the relation name used in V2 role relationships.
   `RoleV2Service._validate_and_resolve_permissions()` resolves the triple
   against permissions already defined in RBAC; an invented triple will not
   create a usable permission. Confirm the exact triple against the class,
   permission definitions, role seeds/configuration, or an existing known-good
   role. Do not reverse-engineer an arbitrary relation name by splitting it.
5. Bind that role to the test user at the resource the permission class checks.
   A correct permission on the wrong resource scope does not authorize the
   request. Keep denied and cross-tenant users without a matching binding.
6. Apply the fixture with `apply-rbac-users-config.sh`, check its org and
   binding counts, and wait for or verify the effective relationship before
   treating an expected-success request as an authorization test. A successful
   fixture command alone does not prove that Kessel can authorize the request.

Record the derivation in the generated scenario, for example:
`GroupV2KesselAccessPermission._get_relation(list, GET)` requires
`rbac_groups_read` on the tenant. Then confirm that `rbac:groups:read` exists
in the active RBAC permission catalog and that the active Kessel schema can
resolve `rbac_groups_read` before creating a role with that permission.

### Group V2 mapping and required catalog entries

The current `rbac/management/permissions/group_v2_access.py` implementation
defines `RESOURCE_TYPE = "tenant"` and checks the request tenant's
`tenant_resource_id()`. Its action/method selection is:

| Group V2 action and method | Required Kessel relation | Exact fixture role permission | Binding resource |
| --- | --- | --- | --- |
| `list` or `retrieve` | `rbac_groups_read` | `application: rbac`, `resource_type: groups`, `operation: read` | `type: tenant`, `id: current` |
| `principals` with `GET`, `HEAD`, or `OPTIONS` | `rbac_groups_read` | `application: rbac`, `resource_type: groups`, `operation: read` | `type: tenant`, `id: current` |
| All other actions or methods, including group or membership writes | `rbac_groups_write` | `application: rbac`, `resource_type: groups`, `operation: write` | `type: tenant`, `id: current` |

The table describes the permission class's intended mapping. The current
repository's `rbac/management/role/permissions/` directory contains only
`inventory.json` and `approval.json`; it does not seed `rbac:groups:read` or
`rbac:groups:write`. A fixture role using the table therefore fails with
`PermissionsNotFoundError` in the local stack checked on 2026-10-01. The
checked-in Inventory schema copy at `.local-deps/inventory-api/deploy/schema.zed`
does not show those relations, while the **running** SpiceDB schema checked on
that date did contain `rbac_groups_read` and `t_rbac_groups_read`. Inspect the
active schema rather than assuming the checked-in copy is what the stack loaded.
`inventory:groups:read` is a different permission and does not authorize
`GroupV2KesselAccessPermission`.

Before applying a Group V2 fixture, check the active permission catalog and
Kessel schema. If either mapping is absent, make the validator fail with that
specific prerequisite error and record the missing RBAC/Kessel contract in the
report. Keep the generated script complete so the run captures the real
dependency failure and can diagnose or repair it. Do not seed a permission by
direct SQL or create a Kessel relationship with `zed relationship touch`.
For example, a read-only catalog check in the RBAC container is:

```bash
podman exec full-kessel-rbac-server-1 python /opt/rbac/rbac/manage.py shell -c \
  'from management.models import Permission; print(Permission.objects.filter(permission="rbac:groups:read").exists())'
```

Check the active Kessel schema with `zed schema read` using the local stack's
documented endpoint and token. Require the `rbac_groups_read` permission on
the tenant resource and its corresponding role relation; a source file alone
does not prove that the running SpiceDB instance loaded the schema. The
validator should print which prerequisite is missing and exit nonzero before
it creates temporary users.

Once both catalog and schema entries exist, the fixture permission's V2 string
is `rbac_groups_read` or `rbac_groups_write`, matching the relation constants
checked by that class.
The write grant is separate; do not include it in a read-only validator just
to make setup pass. This class has no org-admin bypass, so the admin metadata
does not replace the required relation.

After the catalog and schema prerequisites are supplied, a non-admin user who
should list groups needs this role and tenant binding in the fixture (use a
unique role name for each validation):

```yaml
version: 1
tenants:
  - org_id: local-validation-identities
    account_id: "10001"
    bootstrap: true
    temporary: true
    users:
      - username: local-v2-non-admin-example
        user_id: local-v2-non-admin-example
        admin: false
        role_bindings:
          - role: local-v2-groups-read-example
            resource:
              type: tenant
              id: current
    roles:
      - name: local-v2-groups-read-example
        description: Read-only access for the Group V2 validation
        permissions:
          - application: rbac
            resource_type: groups
            operation: read
```

For another V2 endpoint, apply the same trace and derive its exact permission
and resource scope from the class and the code that translates that permission
into a V2 role relationship. A role bound to a workspace does not automatically
grant access to a tenant resource or vice versa. If the current source and
fixture contract do not establish the mapping, report the specific missing
symbol or conversion; do not guess.

For a V1 non-admin endpoint that uses legacy RBAC permissions, configure those
permissions through the endpoint's supported V1 API or existing seed/test
fixture. Do not conclude that a V1 user is authorized because it exists, is
marked as an org admin without the endpoint's required permission, belongs to
a group, or has a V2 role binding.

## Inspect and clean up

List the current fixture state:

```bash
scripts/validations/api/actions/list-rbac-users.sh
```

Delete fixture roles and disable its users using the same YAML file:

```bash
scripts/validations/api/actions/apply-rbac-users-config.sh \
  --file /path/to/fixture.yaml --delete
```

Deletion is refused unless every tenant in that file has `temporary: true`.
The action removes named custom roles and disables declared users through the
normal RBAC services. It leaves tenants, groups, and group memberships in the
database. Delete resources created by the validation through their supported
API cleanup paths.

The helper uses `full-kessel-rbac-server-1` by default and auto-detects Docker
or Podman. Override the container with `RBAC_SERVER_CONTAINER` or the runtime
with `CONTAINER_RUNTIME` when needed. The local stack must be running before
applying fixtures.
