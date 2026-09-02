# V2 Groups — Specification Research & Proposal

**Ticket:** RHCLOUD-48605 — "Groups in V2: determine whether to add CRUD surface or manage externally"
**PR:** #3278 (to be refocused to spec-only)
**Status:** Research for spec-first discussion with UI team before any code changes.

---

## Why this document exists

An earlier iteration of PR #3278 shipped a full V2 Groups CRUD implementation **plus** unrelated cleanup, 
but never updated the TypeSpec/OpenAPI contract. This spec-only PR was extracted to get agreement on the 
**interface** first — that's what the UI team consumes and what's most expensive to change later. 
This document answers the three questions raised in review and proposes a spec.

Colleague's three questions (paraphrased):
1. What can the V1 group endpoints do, and can V2 already do the same?
2. Are these endpoints actually needed?
3. Could this be replaced by the role-binding endpoint? (speculation — probably better to keep it in
   dedicated group endpoints)

---

## Q1 — What V1 groups do vs. what V2 can do today

V1 `GroupViewSet` (`rbac/management/group/view.py`) has three concern areas. Here is the mapping to V2 on master:

### A. The group itself (lifecycle)
| V1 capability | V2 today |
|---|---|
| `POST /v1/groups/` create | ❌ none |
| `GET /v1/groups/` list (filters: name, uuid, role_names, principals, system, platform_default, admin_default, username, exclude_username, scope; order_by name/modified/principalCount/policyCount) | ❌ none |
| `GET /v1/groups/{uuid}/` retrieve | ❌ none |
| `PUT /v1/groups/{uuid}/` update | ❌ none |
| `DELETE /v1/groups/{uuid}/` delete | ❌ none |

**Gap: V2 has no group lifecycle endpoints at all.**

### B. Principal membership (who is in the group)
| V1 capability | V2 today |
|---|---|
| `GET .../principals/` list members (users + service accounts, principal_type filter, order_by username, admin_only, username_only, SA membership report) | ❌ none |
| `POST .../principals/` add users and/or service accounts | ❌ none |
| `DELETE .../principals/?usernames=/service-accounts=` remove | ❌ none |

**Gap: V2 has no way to manage group membership.**

### C. Role assignment (what the group can do)
| V1 capability | V2 today |
|---|---|
| `GET .../roles/` list roles on group | ✅ via role bindings (list by subject) |
| `POST .../roles/` add roles | ✅ **replaced** by role bindings — V1 explicitly returns **403** for workspace/V2 orgs: *"V1 role-to-group assignment operations are not allowed for orgs using workspaces. Use v2 role bindings instead."* |
| `DELETE .../roles/?roles=` remove | ✅ **replaced** by role bindings |

**No gap: role-to-group assignment is already a solved V2 concern (role bindings), and V1 actively blocks it for V2 orgs.**

### Summary of Q1
V2 already covers **role assignment** to groups (via role bindings). V2 has **zero** coverage of the
**group lifecycle** (create/list/retrieve/update/delete) and **zero** coverage of **membership**
(add/remove/list principals). Those two areas are the real gap.

---

## Q2 — Are these endpoints needed?

**Role assignment endpoints on the group: NOT needed.** Already served by role bindings, and V1 blocks
them for V2 orgs. We must NOT port `.../roles/` to V2.

**Lifecycle + membership: needed IF console self-service still needs to create/manage groups in V2 orgs.**
This is the actual product decision to confirm with the UI team:

- If the UI keeps a "User Groups" management screen for V2 orgs, then create/list/retrieve/update/delete
  and add/remove/list-members must exist somewhere in V2. Today they only exist in V1.
- If groups are meant to be managed **externally** (e.g. IT/SSO/IDP-driven, org structure sourced
  upstream) for V2 orgs, then RBAC only needs to *reference* groups (which role bindings already do) and
  we do **not** add lifecycle/membership CRUD.

This is precisely the "add CRUD surface **or** manage externally" decision in the ticket title. It cannot
be answered by us alone — it's a product/UI-consumption question. **This is the #1 thing to align on.**

---

## Q3 — Could role bindings replace group endpoints?

**No — but for a precise reason worth stating clearly.**

In V2, a role binding's subject is stored in two separate through-tables
(`RoleBindingGroup`, `RoleBindingPrincipal`). A group is **already a first-class subject**: you can grant
an existing group a role on a resource via `POST /v2/role-bindings:batchCreate` or
`PUT /v2/role-bindings/by-subject/` with `subject.type = group`. The TypeSpec already models
`GroupSubject` / `GroupDetails` (name, description, user_count).

But a role binding only ever references a group **by UUID**. It never:
- creates a group,
- renames / edits a group,
- deletes a group,
- changes **who is a member** of a group.

So role bindings answer *"grant this existing group access to this resource"* — the group as a **subject
of access**. They do not answer *"what groups exist, and who is in them"* — the group as a **managed
identity/collection**. Those are different resources with different lifecycles.

**Conclusion: the colleague's instinct is correct.** Group lifecycle + membership belong in dedicated
group endpoints, not overloaded onto role bindings. Overloading role bindings would (a) conflate two
resources, (b) break REST resource modeling, and (c) confuse the UI contract.

---

## Proposed V2 specification (for discussion)

Scope the spec to the **actual gap** and nothing more. Explicitly exclude role assignment (role bindings
own it).

### Resource: `Group` (V2)
```
GET    /api/rbac/v2/groups/               # list (paginated, name filter, order_by name)
POST   /api/rbac/v2/groups/               # create  (reject platform_default/admin_default/system)
GET    /api/rbac/v2/groups/{uuid}/        # retrieve
PUT    /api/rbac/v2/groups/{uuid}/        # update  (reject system groups)
DELETE /api/rbac/v2/groups/{uuid}/        # delete  (reject system groups; 409 if active role bindings)
```

### Sub-resource: membership (principals)
```
GET    /api/rbac/v2/groups/{uuid}/principals/                 # list members
POST   /api/rbac/v2/groups/{uuid}/principals/                 # add members (users + service accounts)
DELETE /api/rbac/v2/groups/{uuid}/principals/{principal_uuid}/ # remove one member
```

### Explicitly OUT of scope (do NOT add)
- `.../roles/` on the group — role bindings own role-to-group assignment.

### Open spec questions to settle with the UI team
1. **Do we need lifecycle+membership at all in V2, or manage groups externally?** (the core decision)
2. **Group model shape:** align V2 `Group` fields with the existing `GroupDetails` already in role-binding
   spec (`name`, `description`, `user_count`). Do we expose `principalCount`/`user_count`,
   `platform_default`/`admin_default`/`system` flags, `created`/`modified`? UI needs?
3. **Membership response shape:** do we mirror V1's rich `principal_type=all` split
   (`serviceAccounts` + `users`), or a single unified paginated list? V1's split is complex — prefer
   simpler unless UI needs it.
4. **Membership removal ergonomics:** per-UUID DELETE (RESTful, one at a time) vs V1's bulk
   `?usernames=&service-accounts=` query. Batch delete pattern (`:batchDelete`) exists in V2 router.
5. **Name filtering:** use the V2 idiom `v2_name_filter()` (substring + `*` glob), NOT V1's
   `name_match=partial|exact`. (Per project api-patterns rules.)
6. **Ordering:** V2 idiom is `order_by` with `-` prefix, NOT V1's separate params.
7. **Pagination:** LimitOffset (`V2ResultsSetPagination`) vs cursor (`V2CursorPagination`)? Role bindings
   & roles use cursor; workspaces use limit/offset. Pick one and be consistent with sibling group data.
8. **Auth model:** groups are tenant-scoped. Two-layer access control (permission + filter backend)
   consistent with workspaces/role-bindings. Confirm `rbac_groups_read`/`rbac_groups_write` (or
   equivalent) exist in the Kessel/rbac-config schema for the tenant resource before writing permission
   classes.

---

## Recommended next steps

1. **Refocus PR #3278 to spec-only.** Remove the implementation (view/service/serializer/migration/tests
   for groups) and the unrelated cleanup; keep only TypeSpec changes + regenerated openapi. The PR
   becomes the proposal artifact for the interface.
2. **Add the V2 `Groups` namespace to `docs/source/specs/typespec/main.tsp`** describing the endpoints
   above. Define a full `Group` model for the Groups API (uuid, name, description, principal_count, 
   timestamps, system/default flags) distinct from the lightweight `GroupDetails` (name, description, 
   user_count) used in role binding subjects. The full model serves the Groups lifecycle API; the 
   lightweight model embeds in role binding responses. Run `make generate_v2_spec`.
3. **Bring this doc + the spec diff to the UI team** to answer Q2 (needed? / external?) and the open spec
   questions before any code.
4. Only after interface sign-off: re-introduce the implementation in a follow-up PR.

---

## Appendix — source references

**V1 groups:** `rbac/management/group/view.py` — CRUD `:294/:353/:400/:436/:508`;
`principals` action `:723`; `roles` action `:1259` (V2 block `:1349`). Filters `GroupFilter` `:114`,
querysets `rbac/management/group/querysets.py:93`.

**V2 role bindings:** `rbac/management/role_binding/model.py` — `RoleBindingGroup :309`,
`RoleBindingPrincipal :320`; subject tuples `:82/:93/:130`. Spec `main.tsp` `BindingSubjectType :825`,
`GroupSubject :866`, `GroupDetails :857`.

**V2 conventions:** `BaseV2ViewSet` `rbac/management/base_viewsets.py:25`; `AtomicOperationsMixin`
`rbac/management/v2_mixins.py:33`; pagination `rbac/api/common/pagination.py`; RFC7807
`v2response_error_from_errors` `rbac/management/utils.py:694`.

**V2 name filter idiom:** `rbac/management/v2_filters.py` (`v2_name_filter`).
