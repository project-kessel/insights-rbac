# RBAC REST API consumer guide

This guide uses the externally routed `/api/rbac/` prefix; deployments may use a
different `API_PATH_PREFIX` or gateway mapping. V1 is always registered; V2
routes require the server setting `V2_APIS_ENABLED=True`. Consult the
[V1 spec](source/specs/openapi.json) and [V2 spec](source/specs/v2/openapi.yaml),
but check the deployed version: a spec alone does not enable a route or grant
access. Examples below are HTTP messages and command templates, not credentials. Replace every
`<...>` with a value obtained for your deployment and URL-encode query values.
Send `Accept: application/json` and, for JSON bodies, `Content-Type: application/json`.
The `curl` templates are illustrative, not executed here. Use HTTPS and your
organization's secret-handling mechanism; avoid placing real secrets in shell
history, process arguments, logs, or shared scripts.

## Authentication and authorization

Public V1 and V2 requests normally arrive through the platform gateway, which
authenticates the caller and injects `x-rh-identity` (base64-encoded identity
JSON). The identity supplies the organization context. A service account's
identity also needs a nonblank `client_id`. Do not fabricate an identity or
trust a client-supplied identity header at a public ingress; base64 is not
authentication. Use the platform's supported authentication flow.

Trusted service-to-service callers may instead use a configured PSK:

```http
X-RH-RBAC-PSK: <provisioned-secret>
X-RH-RBAC-ORG-ID: <target-org-id>
X-RH-RBAC-CLIENT-ID: <configured-client-id>
```

All three are required for a PSK attempt; a valid PSK represents a system admin.
Alternatively, a configured system user can send `Authorization: Bearer
<valid-token>` and, when applicable, `X-RH-RBAC-ORG-ID: <org-id>`. The server
validates the token and requires the user in `SYSTEM_USERS`; organization override
requires `allow_any_org`. These are privileged integration mechanisms, not
replacement credentials for ordinary clients. Public middleware can fall back to
PSK or system-user JWT when identity parsing fails. Private `/_private/_s2s/`
routes use PSK or JWT; other private routes have separate restrictions. See
[authentication](AUTHENTICATION.md) and [security guidelines](security-guidelines.md).

For a trusted backend or local test path that accepts the identity header directly,
the following shows its placement. **Do not send a fabricated identity to the
public gateway**: use its supported login and let it inject the header instead.

```text
curl --header 'x-rh-identity: <trusted-gateway-provided-identity>' \
  'https://<trusted-rbac-host>/api/rbac/v1/roles/?limit=10&offset=0'
```

For provisioned system integrations on a trusted path, these illustrate the
alternative header sets. The target tenant must exist and the system caller must
be authorized for the requested operation:

```text
curl --header 'X-RH-RBAC-PSK: <provisioned-secret>' \
  --header 'X-RH-RBAC-ORG-ID: <org-id>' \
  --header 'X-RH-RBAC-CLIENT-ID: <client-id>' \
  'https://<trusted-rbac-host>/api/rbac/v1/roles/?limit=10&offset=0'

curl --header 'Authorization: Bearer <valid-system-user-token>' \
  --header 'X-RH-RBAC-ORG-ID: <org-id>' \
  'https://<trusted-rbac-host>/api/rbac/v1/roles/?limit=10&offset=0'
```

Authentication does not imply authorization. V1 access depends on the caller's
permissions; V2 checks depend on the endpoint and resource, including Kessel
relations. Workspace lists are filtered and an inaccessible workspace detail can
be 404. Role and group access is tenant-wide; role-binding checks are scoped to
the requested resource. Unscoped binding reads use a tenant check. Tenant binding
checks use org-admin status or Kessel, depending on server configuration.

## Check V2 availability and opt-in

These are distinct checks:

1. Ask the deployment operator whether `V2_APIS_ENABLED` is set. A missing V2
   route can also be a routing issue, so a 404 alone does not prove the setting.
2. An authenticated caller can inspect its tenant's state:

   ```http
   GET /api/rbac/v1/tenant/opt-in/
   ```

   A 200 response is `{"v2_opted_in": true}` or `{"v2_opted_in": false}`.
   This endpoint uses a private, 120-second cache directive; allow for staleness.
   Org admins can also `GET /api/rbac/v1/tenant/opt-in/eligibility/`, returning
   `{"eligible": true}` or an ineligibility report. Opt-in is an administrative
   operation, not a harmless probe: `PATCH` to the opt-in URL with
   `{"v2_opted_in": true}` checks eligibility and can return 422. Opt-in is
   publicly one-way: this API does not accept false or null. Internal opt-out
   is possible before V2 write activation, but not after it. Do not opt in
   without organizational approval.
3. V2 role, group and binding writes require tenant opt-in as well as their
   respective permissions. Workspace operations have their own permission checks.
   Some V1 writes are blocked after opt-in. Strict application access flags and
   opt-in are separate; see [V2 org determination](v2-org-determination.md).
   No general public endpoint here reports every server feature flag.

## Pagination

Follow `links.next` until it is `null`, preserving the server-provided URL and
query parameters. Do not construct offsets for cursor endpoints. For example,
an empty offset page has the following shape (`<url>` denotes a returned link):

```json
{"meta":{"count":0,"limit":10,"offset":0},"links":{"first":"<url>","next":null,"previous":null,"last":"<url>"},"data":[]}
```

V1 lists and V2 workspace, group, principal and permission lists use offset
pagination. Workspace pages default to 10 and cap at 3000; common V1 and other
V2 offset pages default to 10 and cap at 1000. For example:
`GET /api/rbac/v2/workspaces/?limit=10&offset=0`.

```json
{"meta":{"limit":10},"links":{"next":null,"previous":null},"data":[]}
```

V2 roles, role bindings (including `by-subject`) and audit logs use cursors,
default 10 and maximum 1000; there is no total count or last-page link.
V2 paginators accept `limit=-1` to request all results; avoid it on large lists
and do not assume the usual offset envelope for an empty unbounded result.
Filters and `order_by` are endpoint-specific; see the
[pagination matrix](api-contracts-guidelines.md#pagination).

## V1: list roles and inspect access

The identity-header example above lists V1 roles with a concrete first page:
`limit=10&offset=0`. Its response uses the offset envelope above; follow
`links.next` for the next page. To inspect the current caller's V1 permissions
for an application, use the same trusted authentication context:

```text
curl --header 'x-rh-identity: <trusted-gateway-provided-identity>' \
  'https://<trusted-rbac-host>/api/rbac/v1/access/?application=<application>&limit=10&offset=0'
```

An access result's `data` entries contain a `permission` and
`resourceDefinitions`; an empty page is not a general V2 authorization decision.
V2-org application restrictions apply, as described below.

## Workspaces

Find a visible parent with `GET /api/rbac/v2/workspaces/?type=default`, or list
with `?limit=10`. Read one with `GET /api/rbac/v2/workspaces/<workspace-id>/`.
The caller sees only authorized workspaces; an empty list does not establish that
none exist. For many IDs, `POST /api/rbac/v2/workspaces/query/` accepts an
`ids` array and returns a paginated, access-filtered list.

With permission to create under a parent, create a standard workspace:

```http
POST /api/rbac/v2/workspaces/
Content-Type: application/json

{"name":"Engineering","description":"Engineering workspace","parent_id":"<parent-workspace-id>"}
```

On success the response is 201; it includes `id`, `name`, `description`,
`parent_id`, `type` (`standard`), `created` and `modified`. Save the returned
`id`. Omitted `parent_id` is assigned by the service; explicit parent is safer
when scope matters. Updating `parent_id` directly is not a move operation.

Example 201 body (IDs and timestamps are placeholders):

```json
{"name":"Engineering","id":"<workspace-id>","parent_id":"<parent-workspace-id>","description":"Engineering workspace","created":"<timestamp>","modified":"<timestamp>","type":"standard"}
```

The following templates show the lifecycle on a trusted direct path. Use gateway
authentication instead when calling through the public gateway. IDs must be
real UUIDs from the tenant, and each action requires its own authorization.

```text
curl --header 'x-rh-identity: <trusted-gateway-provided-identity>' \
  'https://<trusted-rbac-host>/api/rbac/v2/workspaces/<workspace-id>/'

curl --request POST --header 'x-rh-identity: <trusted-gateway-provided-identity>' \
  --header 'Content-Type: application/json' \
  --data '{"name":"Engineering","description":"Engineering workspace","parent_id":"<parent-workspace-id>"}' \
  'https://<trusted-rbac-host>/api/rbac/v2/workspaces/'

curl --request PATCH --header 'x-rh-identity: <trusted-gateway-provided-identity>' \
  --header 'Content-Type: application/json' --data '{"name":"Engineering team"}' \
  'https://<trusted-rbac-host>/api/rbac/v2/workspaces/<workspace-id>/'

curl --request DELETE --header 'x-rh-identity: <trusted-gateway-provided-identity>' \
  'https://<trusted-rbac-host>/api/rbac/v2/workspaces/<workspace-id>/'
```

GET and successful PATCH return 200 with the workspace representation; PATCH
changes only supplied fields. For example, after the PATCH above, `name` is
`"Engineering team"` and the response otherwise has the same fields as the 201
example. PUT requires `name` and, for a standard workspace, the existing
`parent_id`. To move instead, use `POST /api/rbac/v2/workspaces/<id>/move/`
with `{"parent_id":"<target-workspace-id>"}`; source and target permissions and
hierarchy restrictions apply. DELETE returns 204 with no body. Root, default
and ungrouped-hosts workspaces cannot be deleted; workspace dependencies also
block deletion. A denied or inaccessible resource need not reveal whether it exists.

## Create a role, grant it, inspect the grant

Prerequisites: V2 routes enabled, tenant opted in, authorized caller, an existing
workspace and group in the same tenant, and a permission available to that
deployment. Discover permissions with `GET /api/rbac/v2/permissions/` or the
V1 permissions endpoint as applicable. V2 permissions is registered in the
[current router](../rbac/management/v2_urls.py), but still requires the V2 route
setting and permission to read it. Role creation is tenant-wide authorized;
binding creation separately checks the target workspace. The sample permission
is illustrative and must exist in the permission catalog.

1. Create a V2 role:

   ```http
   POST /api/rbac/v2/roles/
   Content-Type: application/json

   {"name":"Engineering host reader","description":"Host read access","permissions":[{"application":"inventory","resource_type":"hosts","operation":"read"}]}
   ```

   A 201 response contains the new role `id`, name, description, permissions,
   `permissions_count`, `last_modified`, and `org_id`. Save the returned `id`;
   V2 role permissions are separate from V1 role access records.

   Example 201 body (IDs, org and timestamp are placeholders):

   ```json
   {"id":"<role-id>","name":"Engineering host reader","description":"Host read access","permissions_count":1,"permissions":[{"application":"inventory","resource_type":"hosts","operation":"read"}],"last_modified":"<timestamp>","org_id":"<org-id>"}
   ```

2. Grant the role to an existing group on the workspace:

   ```http
   POST /api/rbac/v2/role-bindings:batchCreate/?fields=role(id),subject(id,type),resource(id,type)
   Content-Type: application/json

   {"requests":[{"resource":{"id":"<workspace-id>","type":"workspace"},"subject":{"id":"<group-uuid>","type":"group"},"role":{"id":"<role-id>"}}]}
   ```

   A 201 response contains `role_bindings`, with role, subject and resource
   references for the grants. A failed batch rolls back the batch; authorization
   is checked on every requested resource. The group must contain the intended
   user; creating a binding does not add group members.

   Example 201 body for this field selection (IDs are placeholders):

   ```json
   {"role_bindings":[{"role":{"id":"<role-id>"},"subject":{"id":"<group-uuid>","type":"group"},"resource":{"id":"<workspace-id>","type":"workspace"}}]}
   ```

3. Inspect the grant (substitute and URL-encode actual IDs):

   ```http
   GET /api/rbac/v2/role-bindings/by-subject/?resource_type=workspace&resource_id=<workspace-id>&subject_type=group&subject_id=<group-uuid>&fields=roles(name)
   ```

   The cursor-paginated `data` groups roles by subject; field selection limits
   the returned fields. Follow all pages. `roles(name)` requests role names;
   platform roles can be expanded to seeded children. Alternatively,
   `GET /api/rbac/v2/role-bindings/?resource_type=workspace&resource_id=<workspace-id>`
   lists bindings by role. See [role bindings](role_bindings.md#api-and-querying).

This verifies an assignment, **not effective access**: group membership, inherited
grants, defaults, resource scope and the deployed authorization schema matter.
For a real decision, use the consuming application's supported Kessel/Inventory
access check as the intended principal and resource. V1
`GET /api/rbac/v1/access/?application=<application>` is for V1 access and has
V2-org application restrictions; it is not a general V2 workspace check. A
rejected lookup or inaccessible binding does not establish denial of effective
access. The repository does not expose a generic public V2 REST check endpoint.

## Error handling

Handle the HTTP status first, then inspect `Content-Type` and body. The following
are representative, not universal envelopes; gateways may also respond.

* **400**, V2 serializer validation, for example an authorized workspace list
  with `parent_id=not-a-uuid`:

  ```json
  {"status":400,"title":"The request payload contains invalid syntax.","type":"http://project-kessel.org/problems/invalid-request","detail":"not-a-uuid is not a valid UUID.","errors":[{"message":"not-a-uuid is not a valid UUID.","field":"parent_id"}]}
  ```

  Middleware can instead return JSON `{"code":400,"message":"An org_id must be provided in the identity header."}` for either version. V1 view validation normally uses `{"errors":[{"detail":"...","source":"...","status":"400"}]}`.
* **401**: missing identity and unsuccessful S2S fallback can produce an empty
  middleware response, including on V2. Other authentication failures can have
  different bodies; do not assume Problem JSON.
* **403**: a default V2 view-level permission denial can return:

  ```json
  {"status":403,"title":"You do not have permission to perform this action.","type":"http://project-kessel.org/problems/insufficient-permission","detail":"You do not have permission to perform this action.","errors":[{"message":"You do not have permission to perform this action.","field":"detail"}]}
  ```

  Details depend on the check. Check caller grants and tenant opt-in.
* **404**: a missing group during an authorized V2 batch grant can return:

  ```json
  {"status":404,"title":"Not found.","type":"http://project-kessel.org/problems/not-found","detail":"group with id '00000000-0000-0000-0000-000000000000' not found","errors":[{"message":"group with id '00000000-0000-0000-0000-000000000000' not found","field":"detail"}]}
  ```

  Inaccessible workspace detail can also be 404; do not treat it as proof of
  nonexistence.
* **500**: an exhausted V2 atomic-operation deadlock can return ordinary JSON
  `{"detail":"Internal server error. Please try again later."}` rather than
  Problem JSON. Unexpected server failures may have other formats. Retry only
  when appropriate for the operation; do not blindly replay an uncertain write.

V2 exception-handler responses often use `application/problem+json`, but
middleware, direct responses and unhandled exceptions can bypass that handler.
Likewise, some V1 responses differ from the standard errors array. See
[exception handler](../rbac/api/common/exception_handler.py),
[middleware](../rbac/rbac/middleware.py), and
[atomic operations](../rbac/management/v2_mixins.py).
