#!/usr/bin/env python
"""Parity toolkit data setup/cleanup via Django ORM.

Runs on the RBAC service pod via ``oc exec``. Bypasses V2 API permission
checks by using the service layer directly, while still producing
Debezium outbox writes for Kessel replication.

Usage (from oc exec)::

    python parity_data_setup.py setup --org-id=12345 --run-tag=abc123 [--minimal]
    python parity_data_setup.py cleanup --org-id=12345 \\
        --workspace-ids=uuid1,uuid2 --role-uuid=uuid3 --group-ids=uuid4,uuid5
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.models import Tenant as TenantType

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rbac.settings")

import django  # noqa: E402

django.setup()

from api.models import Tenant  # noqa: E402
from management.group.model import Group  # noqa: E402
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler  # noqa: E402
from management.principal.model import Principal  # noqa: E402
from management.relation_replicator.outbox_replicator import OutboxReplicator  # noqa: E402
from management.relation_replicator.relation_replicator import ReplicationEventType  # noqa: E402
from management.role.v2_service import RoleV2Service  # noqa: E402
from management.role_binding.service import CreateBindingRequest, RoleBindingService  # noqa: E402
from management.workspace.service import WorkspaceService  # noqa: E402

logger = logging.getLogger(__name__)


def get_tenant(org_id: str) -> TenantType:
    """Look up tenant by org_id."""
    try:
        return Tenant.objects.get(org_id=org_id)
    except Tenant.DoesNotExist:
        print(json.dumps({"error": f"Tenant not found for org_id={org_id}"}))
        sys.exit(1)


def setup(org_id: str, run_tag: str, minimal: bool) -> None:
    """Create test data covering all 5 parity sub-checks."""
    tenant = get_tenant(org_id)
    replicator = OutboxReplicator()
    result = {}

    # 1. Workspaces
    ws_count = 1 if minimal else 2
    ws_service = WorkspaceService(replicator=replicator)
    workspace_ids = []
    for i in range(1, ws_count + 1):
        ws = ws_service.create(
            validated_data={"name": f"parity-ws-{i}-{run_tag}"},
            request_tenant=tenant,
        )
        workspace_ids.append(str(ws.id))
    result["workspace_ids"] = workspace_ids

    # 2. Custom role with permissions
    role_service = RoleV2Service(tenant=tenant, replicator=replicator)
    if minimal:
        perms = [{"application": "inventory", "resource_type": "hosts", "verb": "read"}]
    else:
        perms = [
            {"application": "inventory", "resource_type": "hosts", "verb": "read"},
            {"application": "inventory", "resource_type": "groups", "verb": "write"},
        ]
    role = role_service.create(
        name=f"parity-role-{run_tag}",
        description=f"Parity check test role ({run_tag})",
        permission_data=perms,
        tenant=tenant,
    )
    result["role_uuid"] = str(role.uuid)
    result["role_perm_count"] = len(perms)

    # 3. Groups with principal
    group_count = 1 if minimal else 2
    principal, _ = Principal.objects.get_or_create(
        username="jdoe",
        tenant=tenant,
        defaults={"type": Principal.Types.USER},
    )
    result["test_username"] = principal.username

    group_ids = []
    for i in range(1, group_count + 1):
        group = Group.objects.create(
            name=f"parity-group-{i}-{run_tag}",
            tenant=tenant,
        )
        group.principals.add(principal)
        handler = RelationApiDualWriteGroupHandler(
            group,
            ReplicationEventType.ADD_PRINCIPALS_TO_GROUP,
            replicator=replicator,
        )
        handler.replicate_new_principals([principal])
        group_ids.append(str(group.uuid))
    result["group_ids"] = group_ids

    # 4. Role bindings (tie role + group + workspace)
    rb_service = RoleBindingService(tenant=tenant, replicator=replicator)
    binding_requests = [
        CreateBindingRequest(
            role_id=str(role.uuid),
            resource_type="workspace",
            resource_id=ws_id,
            subject_type="group",
            subject_id=group_ids[0],
        )
        for ws_id in workspace_ids
    ]
    rb_service.batch_create(binding_requests)
    result["binding_count"] = len(binding_requests)

    print(json.dumps(result))


def cleanup(org_id: str, workspace_ids: list[str], role_uuid: str, group_ids: list[str]) -> None:
    """Remove test data in FK-safe order.

    Uses direct ORM deletes (no service layer) because this runs on ephemeral
    clusters that are destroyed after testing. Outbox events are not produced,
    so Kessel relations will be orphaned. For persistent environments, use the
    service-layer teardown methods instead.
    """
    tenant = get_tenant(org_id)
    cleaned = 0

    from management.role.v2_model import CustomRoleV2
    from management.role_binding.model import RoleBinding
    from management.workspace.model import Workspace

    # 1. Role bindings (must go first due to FK constraints)
    if role_uuid:
        deleted, _ = RoleBinding.objects.filter(
            role__uuid=role_uuid,
            tenant=tenant,
        ).delete()
        cleaned += deleted

    # 2. Workspaces
    if workspace_ids:
        deleted, _ = Workspace.objects.filter(
            id__in=workspace_ids,
            tenant=tenant,
        ).delete()
        cleaned += deleted

    # 3. Role (permissions cascade via M2M through table)
    if role_uuid:
        deleted, _ = CustomRoleV2.objects.filter(
            uuid=role_uuid,
            tenant=tenant,
        ).delete()
        cleaned += deleted

    # 4. Groups (M2M principals cleared automatically on group delete)
    if group_ids:
        deleted, _ = Group.objects.filter(
            uuid__in=group_ids,
            tenant=tenant,
        ).delete()
        cleaned += deleted

    print(json.dumps({"cleaned": cleaned}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Parity toolkit data setup/cleanup")
    parser.add_argument("mode", choices=["setup", "cleanup"])
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--run-tag", default="")
    parser.add_argument("--minimal", action="store_true")
    parser.add_argument("--workspace-ids", default="")
    parser.add_argument("--role-uuid", default="")
    parser.add_argument("--group-ids", default="")

    args = parser.parse_args()

    if args.mode == "setup":
        if not args.run_tag:
            print(json.dumps({"error": "--run-tag is required for setup mode"}))
            sys.exit(1)
        setup(args.org_id, args.run_tag, args.minimal)
    elif args.mode == "cleanup":
        ws_ids = [x for x in args.workspace_ids.split(",") if x]
        g_ids = [x for x in args.group_ids.split(",") if x]
        cleanup(args.org_id, ws_ids, args.role_uuid, g_ids)


if __name__ == "__main__":
    main()
