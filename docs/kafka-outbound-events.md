# Kafka Outbound Events

RBAC produces Kafka messages on three topics to notify external services about RBAC state changes. Each topic serves a different audience and uses a different message format. All production is gated by the `KAFKA_ENABLED` setting; when disabled (or `MOCK_KAFKA=True`), a no-op `FakeKafkaProducer` is used.

**Producer implementation:** `rbac/core/kafka.py` (`RBACProducer` singleton, up to 5 retries on send failure).

---

## Topic 1: Notifications (`NOTIFICATIONS_TOPIC`)

**Purpose:** User-facing notifications delivered through the console.redhat.com Notifications service.

**Handler module:** `rbac/management/notifications/notification_handlers.py`

**Feature flags:**
- `NOTIFICATIONS_ENABLED` — gates all custom resource notifications
- `NOTIFICATIONS_RH_ENABLED` — gates Red Hat system role/group notifications
- `skip_rh_notifications` context variable — suppresses RH notifications during seeding

### Message Envelope

Template: `rbac/management/notifications/message_template.json`

```json
{
  "bundle": "console",
  "application": "rbac",
  "event_type": "<event_type>",
  "timestamp": "<ISO-8601>",
  "org_id": "<org_id>",
  "events": [
    {
      "metadata": {},
      "payload": { }
    }
  ]
}
```

Headers: `rh-message-id` (random UUID, UTF-8 encoded).

### Event Types

#### Custom Role Events (V1 API)

Sent when a tenant user creates, updates, or deletes a custom role through the V1 API.

| Event Type | Occasion | Payload |
|---|---|---|
| `custom-role-created` | `RoleViewSet.create()` succeeds | `{username, name, uuid}` |
| `custom-role-updated` | `RoleViewSet.update()` succeeds | `{username, name, uuid}` |
| `custom-role-deleted` | `RoleViewSet.destroy()` succeeds | `{username, name, uuid}` |

**Trigger location:** `rbac/management/role/view.py` (lines 526, 544, 577)

#### Custom Role Events (V2 API)

Sent when a tenant user creates, updates, or deletes a custom role through the V2 API.

| Event Type | Occasion | Payload |
|---|---|---|
| `custom-v2-role-created` | `RoleV2ViewSet.perform_atomic_create()` succeeds | `{username, name, uuid}` |
| `custom-v2-role-updated` | `RoleV2ViewSet.perform_atomic_update()` succeeds | `{username, name, uuid}` |
| `custom-v2-role-deleted` | `RoleV2ViewSet.perform_atomic_destroy()` succeeds | `{username, name, uuid}` |

**Trigger location:** `rbac/management/role/v2_view.py` (lines 137, 158, 206)

#### Group Events

Sent when a tenant user modifies groups or their membership.

| Event Type | Occasion | Payload |
|---|---|---|
| `group-created` | Non-system group is created (via serializer) | `{username, name, uuid}` |
| `group-updated` | Group name is updated, or a role/principal is added to or removed from a non-platform-default group | `{username, name, uuid}` with optional `{operation, role/principal}` |
| `group-deleted` | Non-system group is deleted via `GroupViewSet.destroy()` | `{username, name, uuid}` |
| `custom-default-access-updated` | A role is added to or removed from a tenant-customized platform default group | `{username, name, uuid, operation, role: {name, uuid}}` |
| `platform-default-group-turned-into-custom` | The `platform_default` flag on a group is toggled off (customized by tenant admin) | `{username, name, uuid}` |

**Trigger locations:**
- `rbac/management/group/view.py` — delete, add/remove principals
- `rbac/management/group/serializer.py` — create, update
- `rbac/management/group/definer.py` — flag change, role add/remove on default group

#### Red Hat System Role & Group Events

Sent during system role seeding when Red Hat updates built-in roles. These are broadcast to **all tenants** via `notify_all()`. Username is always `"Red Hat"`.

| Event Type | Occasion | Payload |
|---|---|---|
| `rh-new-role-available` | A brand-new system role is seeded for the first time | `{username: "Red Hat", name, uuid}` |
| `rh-platform-default-role-updated` | An existing platform-default system role's permissions change during seeding | `{username: "Red Hat", name, uuid}` |
| `rh-non-platform-default-role-updated` | An existing non-default system role's permissions change during seeding | `{username: "Red Hat", name, uuid}` |
| `rh-new-role-added-to-default-access` | A system role is added to the platform default group during seeding | `{username: "Red Hat", name, uuid, role: {name, uuid}}` |
| `rh-role-removed-from-default-access` | A system role is removed from the platform default group during seeding | `{username: "Red Hat", name, uuid, role: {name, uuid}}` |

**Trigger locations:**
- `rbac/management/role/definer.py` — role creation and update during seeding
- `rbac/management/group/definer.py` — default access group role changes during seeding

#### Cross-Account Access Events

| Event Type | Occasion | Payload |
|---|---|---|
| `rh-new-tam-request-created` | A cross-account (TAM) access request is submitted via `CrossAccountRequestSerializer.create()` | `{username, request_id}` |

**Trigger location:** `rbac/api/cross_access/serializer.py` (line 147)

---

## Topic 2: External Sync (`EXTERNAL_SYNC_TOPIC`)

**Purpose:** Notifies external services (e.g., BOP, IT Service) about RBAC state changes so they can synchronize their own access data.

**Handler module:** `rbac/internal/integration/sync_handlers.py`

**Trigger mechanism:** Django model signals (`post_save`, `pre_delete`, `m2m_changed`) — connected only when `KAFKA_ENABLED=True`.

### Message Envelope

Template: `rbac/internal/integration/message_template.json`

```json
{
  "event_type": "<event_type>",
  "timestamp": "<ISO-8601>",
  "account_id": "",
  "events": [
    {
      "metadata": {},
      "payload": { }
    }
  ]
}
```

No additional headers.

### Event Types

#### Group Lifecycle

| Event Type | Occasion (Django Signal) | Payload |
|---|---|---|
| `group_created` | `post_save` on `Group` model (when `created=True`) | `{group: {name, uuid}}` |
| `group_deleted` | `pre_delete` on `Group` model | `{group: {name, uuid}}` |

**Signal handler location:** `rbac/management/group/model.py` (lines 165–178)

#### Group Membership

| Event Type | Occasion (Django Signal) | Payload |
|---|---|---|
| `group_membership_changed` | `m2m_changed` on `Group.principals` through table — fires on `post_add`, `pre_remove`, `pre_clear` actions | `{group: {name, uuid}, action: "add" \| "remove" \| "clear"}` |

**Signal handler location:** `rbac/management/group/model.py` (lines 181–208)

#### Policy / Role Binding Changes

| Event Type | Occasion (Django Signal) | Payload |
|---|---|---|
| `platform_default_group_changed` | `post_save` or `pre_delete` on `Policy` model when the policy's group is `platform_default`; also on `m2m_changed` (`post_add`, `pre_remove`, `pre_clear`) on `Policy.roles` | `{group: {name, uuid}}` |
| `non_default_group_relations_changed` | Same as above but when the policy's group is **not** `platform_default` | `{group: {name, uuid}}` |
| `role_modified` | `m2m_changed` on `Policy.roles` when the changed instance is a `Role` (not a `Policy`); also `pre_delete` on `Access` or `ResourceDefinition` models | `{role: {name, uuid}}` |

**Signal handler locations:**
- `rbac/management/policy/model.py` (lines 109–168) — policy and policy-role changes
- `rbac/management/role/model.py` (lines 321–330) — role-related object changes (Access, ResourceDefinition)

---

## Topic 3: Chrome Invalidation (`EXTERNAL_CHROME_TOPIC`)

**Purpose:** Tells the console.redhat.com Chrome UI to invalidate cached RBAC data (currently groups only) so the frontend reflects changes immediately.

**Handler module:** `rbac/internal/integration/chrome_handlers.py`

**Trigger mechanism:** Django model signals on `Group` — connected only when `KAFKA_ENABLED=True`.

### Message Envelope

Template: `rbac/internal/integration/chrome_message_template.json`

CloudEvents 1.0.2 format:

```json
{
  "specversion": "1.0.2",
  "type": "data.invalidation",
  "source": "/rbac/v1/status/",
  "id": "<random-uuid>",
  "time": "<ISO-8601>",
  "datacontenttype": "application/json",
  "data": {
    "broadcast": false,
    "organizations": ["<org_id>"],
    "payload": {
      "entityType": "rbac.group",
      "entityId": "<group_uuid>",
      "eventType": "<create|update|delete>"
    }
  }
}
```

No additional headers.

### Event Types

| Event Type | Occasion (Django Signal) |
|---|---|
| `create` | `post_save` on `Group` model when `created=True` |
| `update` | `post_save` on `Group` model when `created=False` |
| `delete` | `pre_delete` on `Group` model |

**Signal handler location:** `rbac/management/group/model.py` (lines 147–162)

---

## Summary

| Topic | # Events | Audience | Trigger Mechanism |
|---|---|---|---|
| Notifications | 16 | End users (console.redhat.com) | Explicit calls from views, serializers, and seeding code |
| External Sync | 6 | External services (BOP, IT) | Django model signals (automatic) |
| Chrome | 3 | Console UI frontend | Django model signals (automatic) |
| **Total** | **25** | | |

### Configuration Reference

| Setting | Default | Purpose |
|---|---|---|
| `KAFKA_ENABLED` | `False` | Master switch for all Kafka production |
| `MOCK_KAFKA` | `False` | Use `FakeKafkaProducer` (no-op) for dev/test |
| `NOTIFICATIONS_ENABLED` | `False` | Enable custom resource notifications |
| `NOTIFICATIONS_RH_ENABLED` | `False` | Enable Red Hat system notifications |
| `NOTIFICATIONS_TOPIC` | (env) | Kafka topic for user-facing notifications |
| `EXTERNAL_SYNC_TOPIC` | (env) | Kafka topic for external service sync |
| `EXTERNAL_CHROME_TOPIC` | (env) | Kafka topic for Chrome UI invalidation |
| `KAFKA_SERVERS` | (env) | Kafka bootstrap servers |
