#
# Copyright 2022 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""Utilities for Internal RBAC use."""

import itertools
import json
import logging
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

import jsonschema
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.urls import resolve
from internal.pg_notify_wait import (
    REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_CHANNEL,
    REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_TIMEOUT_SECONDS,
    wait_for_pg_notify,
)
from internal.schemas import INVENTORY_INPUT_SCHEMAS, RELATION_INPUT_SCHEMAS
from jsonschema import validate
from management.atomic_transactions import atomic, atomic_block, atomic_with_retry
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.models import BindingMapping, Role, Workspace
from management.permission.scope_service import TenantScopeResources
from management.principal.model import Principal
from management.principal.proxy import PrincipalProxy
from management.relation_replicator.logging_replicator import LoggingReplicator, stringify_spicedb_relationship
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
)
from management.relation_replicator.relations_api_replicator import RelationsApiReplicator
from management.relation_replicator.types import RelationTuple
from management.role.v2_model import RoleV2
from management.role_binding.model import RoleBinding, RoleBindingPrincipal
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from management.tenant_mapping.v2_activation import (
    TenantVersion,
    V1WriteBlockedError,
    assert_v1_write_allowed,
    lock_tenant_version,
)
from management.tenant_service.relations import default_role_binding_tuples
from management.tenant_service.v2 import TenantNotBootstrappedError, lock_tenant_for_bootstrap
from management.utils import as_uuid
from management.workspace.relation_api_dual_write_workspace_handler import RelationApiDualWriteWorkspaceHandler
from migration_tool.utils import create_relationship

from api.cross_access.model import CrossAccountRequest
from api.cross_access.relation_api_dual_write_cross_access_handler import RelationApiDualWriteCrossAccessHandler
from api.models import Tenant, User

logger = logging.getLogger(__name__)
PROXY = PrincipalProxy()


def get_replicator(write_relationships: str) -> RelationReplicator:
    """
    Get the appropriate replicator based on write_relationships setting.

    Args:
        write_relationships: How to handle replication.
            - "True" or "outbox": Create OutboxReplicator (replicate to outbox)
            - "logging": Create LoggingReplicator (log what would be replicated)
            - "False" or other: Create NoopReplicator (no replication)

    Returns:
        RelationReplicator instance
    """
    option = write_relationships.lower()

    if option == "true" or option == "outbox":
        return OutboxReplicator()

    if option == "logging":
        return LoggingReplicator()

    # "false"
    if option == "false":
        return NoopReplicator()

    raise ValueError(f"Invalid write_relationships option: {write_relationships}")


def build_internal_user(request, json_rh_auth):
    """Build user object for internal requests."""
    user = User()
    valid_identity_types = ["Associate", "X509"]
    try:
        identity_type = json_rh_auth["identity"]["type"]
        if identity_type not in valid_identity_types:
            logger.debug(
                f"User identity type is not valid: '{identity_type}'. Valid types are: {valid_identity_types}"
            )
            return None
        user.username = json_rh_auth["identity"].get("associate", {}).get("email", "system")
        user.admin = True
        user.org_id = resolve(request.path).kwargs.get("org_id")
        return user
    except KeyError:
        logger.debug(
            f"Identity object is missing 'identity.type' attribute. Valid options are: {valid_identity_types}"
        )
        return None


def delete_bindings(bindings):
    """
    Delete the provided bindings and replicate the deletion event.

    Args:
        bindings (QuerySet): A Django QuerySet of binding objects to be deleted.

    Returns:
        dict: A dictionary containing information about the deleted bindings, including:
            - mappings (list): A list of mappings for each binding.
            - role_ids (list): A list of role IDs for each binding.
            - resource_ids (list): A list of resource IDs for each binding.
            - resource_types (list): A list of resource type names for each binding.
            - relations (list): A list of tuples representing the relations to be removed.
    """
    replicator = OutboxReplicator()
    # Get org_id from first binding's role tenant
    org_id = str(bindings.first().role.tenant.org_id) if bindings.exists() else ""
    info = {
        "mappings": [binding.mappings for binding in bindings],
        "role_ids": [binding.role_id for binding in bindings],
        "resource_ids": [binding.resource_id for binding in bindings],
        "resource_types": [binding.resource_type_name for binding in bindings],
        "org_id": org_id,
    }
    if bindings:
        with transaction.atomic():
            relations_to_remove = []
            for binding in bindings:
                relations_to_remove.extend(binding.as_tuples())
            replicator.replicate(
                ReplicationEvent(
                    event_type=ReplicationEventType.DELETE_BINDING_MAPPINGS,
                    info=info,
                    partition_key=PartitionKey.byEnvironment(),
                    remove=relations_to_remove,
                ),
            )
            bindings.delete()
        info["relations"] = [stringify_spicedb_relationship(relation) for relation in relations_to_remove]
    return info


def read_tuples_from_kessel(resource_type: str, resource_id: str, relation: str, subject_type: str, subject_id: str):
    """
    Read tuples from Kessel Relations API.

    This is a convenience wrapper around RelationsApiReplicator.read_tuples()
    that uses the default "rbac" namespace.

    Args:
        resource_type: Type of the resource (e.g., "tenant", "workspace", "role_binding", "role")
        resource_id: ID of the resource
        relation: Relation to filter by (empty string for all relations)
        subject_type: Type of the subject to filter by
        subject_id: ID of the subject to filter by (empty string for all)

    Returns:
        list[dict]: List of tuple dictionaries from Kessel
    """
    replicator = RelationsApiReplicator()
    return replicator.read_tuples(
        resource_type=resource_type,
        resource_id=resource_id,
        relation=relation,
        subject_type=subject_type,
        subject_id=subject_id,
    )


def iterate_tuples_from_kessel(
    resource_type: str, resource_id: str, relation: str, subject_type: str, subject_id: str
) -> Iterable[dict]:
    """
    Read tuples from Kessel Relations API while handling pagination.

    This is similar to read_tuples_from_kessel, except that it also returns subsequent pages from Kessel, and it does
    not necessarily return a list.
    """
    replicator = RelationsApiReplicator()

    continuation_token = None
    first = True

    while (continuation_token not in (None, "")) or first:
        batch = replicator.read_tuples(
            resource_type=resource_type,
            resource_id=resource_id,
            relation=relation,
            subject_type=subject_type,
            subject_id=subject_id,
            pagination_limit=100,
            continuation_token=continuation_token,
        )

        if len(batch) == 0:
            return

        continuation_token = batch[-1].get("pagination", {}).get("continuationToken", None)
        first = False

        if continuation_token is None:
            logger.warning(f"Unexpectedly missing continuation token from Kessel. Response: {batch}")

        yield from batch


def _build_workspace_graph(tenant) -> tuple[list, dict]:
    """
    Build workspace parent-child graph from DB workspace objects.

    This is a pure graph-building function that extracts the workspace hierarchy
    from the database. It's used by rebuild_tenant_workspace_relations for BFS traversal.

    Args:
        tenant: The Tenant object to get workspaces for

    Returns:
        tuple of:
        - root_workspace_ids: list of workspace IDs with no DB parent (typically root workspace)
        - children_by_parent: dict mapping parent_id -> list of child workspace_ids
    """
    db_workspaces = list(Workspace.objects.filter(tenant=tenant).order_by("id"))
    children_by_parent: dict = {}
    root_workspace_ids: list = []

    for ws in db_workspaces:
        if ws.parent_id is None:
            root_workspace_ids.append(ws.id)
        else:
            children_by_parent.setdefault(ws.parent_id, []).append(ws.id)

    return root_workspace_ids, children_by_parent


@dataclass
class WorkspaceProcessResult:
    """Result of processing a single workspace in rebuild_tenant_workspace_relations."""

    checked: int = 0
    relations_added: int = 0
    relations_to_add_count: int = 0
    missing_parent: bool = False  # True if parent relation was missing


def _process_workspace_in_transaction(
    ws_id_uuid,
    expected_parent_workspace_id: str,
    tenant,
    read_tuples_fn,
    replicator,
    dry_run: bool,
) -> WorkspaceProcessResult:
    """
    Process a single workspace within a transaction.

    Locks parent workspace then child, verifies parent hasn't changed,
    checks if parent relation exists in Kessel, and replicates if missing.

    Args:
        ws_id_uuid: The workspace UUID to process
        expected_parent_workspace_id: Expected parent workspace UUID (DB `parent_id`)
        tenant: The Tenant object
        read_tuples_fn: Function to read tuples from Kessel
        replicator: The replicator to use for writing relations
        dry_run: If True, don't actually replicate

    Returns:
        WorkspaceProcessResult with counts and IDs
    """
    result = WorkspaceProcessResult()

    with transaction.atomic():
        try:
            Workspace.objects.select_for_update().get(id=expected_parent_workspace_id)
        except Workspace.DoesNotExist:
            logger.warning(
                f"Parent workspace {expected_parent_workspace_id} no longer exists, skipping child {ws_id_uuid}"
            )
            return result

        # Lock the child workspace
        try:
            ws = Workspace.objects.select_for_update().get(id=ws_id_uuid)
        except Workspace.DoesNotExist:
            logger.warning(f"Workspace {ws_id_uuid} no longer exists, skipping")
            return result

        result.checked = 1
        ws_id = str(ws.id)

        # Verify parent hasn't changed (in case of concurrent modification)
        actual_parent_id = str(ws.parent_id) if ws.parent_id else None
        if actual_parent_id != expected_parent_workspace_id:
            logger.warning(
                f"Workspace {ws_id} parent changed from {expected_parent_workspace_id} to {actual_parent_id}, skipping"
            )
            return result

        # Check if parent relation exists in Kessel
        parent_tuples = read_tuples_fn("workspace", ws_id, "parent", "workspace", expected_parent_workspace_id)
        if parent_tuples:
            return result

        # Missing parent relation
        result.missing_parent = True

        # Create the missing parent relation
        relation = create_relationship(
            ("rbac", "workspace"),
            ws_id,
            ("rbac", "workspace"),
            expected_parent_workspace_id,
            "parent",
        )

        if not dry_run:
            replicator.replicate(
                ReplicationEvent(
                    event_type=ReplicationEventType.MIGRATE_TENANT_GROUPS,
                    info={"tenant": tenant.org_id, "workspace": ws_id, "action": "rebuild_workspace_parent"},
                    partition_key=PartitionKey.byEnvironment(),
                    add=[relation],
                    remove=[],
                )
            )
            result.relations_added = 1
            logger.info(f"Added parent relation: workspace:{ws_id}#parent@workspace:{expected_parent_workspace_id}")
        else:
            result.relations_to_add_count = 1
            logger.info(f"DRY RUN: Would add: workspace:{ws_id}#parent@workspace:{expected_parent_workspace_id}")

    return result


def rebuild_tenant_workspace_relations(
    tenant,
    read_tuples_fn,
    replicator,
    dry_run: bool = False,
) -> dict:
    """
    Rebuild workspace parent relations for a tenant in Kessel.

    This function traverses all workspaces in the DB for a tenant and ensures
    their parent relations exist in Kessel. This is a prerequisite for
    cleanup_tenant_orphaned_relationships to work correctly.

    Root workspaces are not linked to the tenant in relations; only workspace-to-workspace
    parent edges are replicated (default -> root -> ...).

    Uses BFS traversal from each root workspace's children. For each workspace, locks the
    parent first, then locks the child, checks/replicates the parent relation,
    then moves to children. This minimizes lock contention.

    Args:
        tenant: The Tenant object to rebuild relations for
        read_tuples_fn: Function to read tuples from Kessel, signature:
                        (resource_type: str, resource_id: str, relation: str,
                         subject_type: str = "", subject_id: str = "") -> list[dict]
        replicator: The replicator to use for writing relations
        dry_run: If True, only report what would be added without making changes

    Returns:
        dict: Results including workspaces checked, relations added, etc.
    """
    workspaces_checked = 0
    relations_added = 0
    relations_to_add_count = 0
    workspaces_missing_parent_count = 0

    # Build workspace graph from DB
    root_workspace_ids, children_by_parent = _build_workspace_graph(tenant)

    # Build BFS queue from root workspaces' children only (no workspace#parent@tenant edge).
    queue = deque()
    for root_id in root_workspace_ids:
        for child_id in children_by_parent.get(root_id, []):
            queue.append((child_id, str(root_id)))

    # BFS traversal - process each workspace in transaction
    while queue:
        ws_id_uuid, expected_parent_workspace_id = queue.popleft()

        # Process workspace in transaction (locks parent then child)
        result = _process_workspace_in_transaction(
            ws_id_uuid,
            expected_parent_workspace_id,
            tenant,
            read_tuples_fn,
            replicator,
            dry_run,
        )

        # Aggregate results
        workspaces_checked += result.checked
        relations_added += result.relations_added
        relations_to_add_count += result.relations_to_add_count
        if result.missing_parent:
            workspaces_missing_parent_count += 1

        # Add children to queue for BFS (outside transaction to release locks)
        if ws_id_uuid in children_by_parent:
            for child_id in children_by_parent[ws_id_uuid]:
                queue.append((child_id, str(ws_id_uuid)))

    if dry_run and relations_to_add_count:
        logger.info(f"DRY RUN: Would add {relations_to_add_count} parent relations for tenant {tenant.org_id}")

    return {
        "org_id": tenant.org_id,
        "dry_run": dry_run,
        "workspaces_checked": workspaces_checked,
        "workspaces_missing_parent": workspaces_missing_parent_count,
        "relations_to_add": relations_to_add_count if dry_run else relations_added,
        "relations_added": relations_added,
    }


def remove_legacy_root_workspace_tenant_parent_relations() -> dict:
    """
    Enqueue removal of workspace(root)#parent@tenant relationship tuples.

    Bootstrapping no longer creates this edge; this job clears stale tuples still present in Kessel.
    """
    if not settings.REPLICATION_TO_RELATION_ENABLED:
        return {"skipped": True, "reason": "REPLICATION_TO_RELATION_ENABLED is False"}

    replicator = OutboxReplicator()
    qs = Tenant.objects.exclude(tenant_name="public").order_by("id")

    batch: list[RelationTuple] = []
    tenants_processed = 0
    batch_size = 500
    tenants_total = qs.count()

    def flush_batch() -> None:
        if not batch:
            return
        notify_token = str(uuid.uuid4())
        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS,
                info={
                    "batch_size": len(batch),
                    "notify_token": notify_token,
                },
                partition_key=PartitionKey.byEnvironment(),
                add=[],
                remove=batch,
            )
        )
        batch.clear()
        logger.info(f"Processed {tenants_processed} of {tenants_total} tenants")
        wait_for_pg_notify(
            channel=REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_CHANNEL,
            expected_payload=notify_token,
            timeout_seconds=float(REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_TIMEOUT_SECONDS),
            log_label="remove_legacy_root_workspace_tenant_parent",
        )

    for tenant in qs.iterator(chunk_size=500):
        tenants_processed += 1
        tenant_resource_id = tenant.tenant_resource_id()
        if tenant_resource_id is None:
            continue
        root = Workspace.objects.root(tenant=tenant)
        batch.append(
            create_relationship(
                ("rbac", "workspace"),
                str(root.id),
                ("rbac", "tenant"),
                tenant_resource_id,
                "parent",
            )
        )
        if len(batch) >= batch_size:
            flush_batch()

    flush_batch()

    return {
        "skipped": False,
        "tenants_processed": tenants_processed,
    }


def lock_binding_mappings_with_roles_by_uuid(uuids: Iterable[str | uuid.UUID]) -> dict[str, BindingMapping]:
    """
    Look up BindingMappings by UUID and lock them along with any associated custom roles.

    Returns a dict from UUID strings to BindingMappings.
    """
    converted_uuids = {str(as_uuid(u)) for u in uuids}

    # This will lock both the bindings and the roles for bindings to custom roles.
    custom_binding_mappings = list(
        BindingMapping.objects.filter(mappings__id__in=converted_uuids)
        .filter(role__system=False)
        .select_related("role")
        .select_for_update(of=["self", "role"])
    )

    # This will lock only the bindings for bindings to system roles, as V1 does.
    system_binding_mappings = list(
        BindingMapping.objects.filter(mappings__id__in=converted_uuids)
        .filter(role__system=True)
        .select_related("role")
        .select_for_update(of=["self"])
    )

    return {b.mappings["id"]: b for b in [*custom_binding_mappings, *system_binding_mappings]}


# We must use SERIALIZABLE here because we are potentially interacting with concurrent V2 writers (which all
# use SERIALIZABLE rather than explicit locking).
#
# Unfortunately, we must *also* use select_for_update() here because we are potentially interacting with
# concurrent V1 writers (which all use SELECT FOR UPDATE rather than SERIALIZABLE).
@atomic_with_retry(retries=3)
def _do_replicate_missing_binding_tuples_batch(tenant_id: int, raw_role_bindings: list[RoleBinding]):
    tenant = Tenant.objects.get(id=tenant_id)
    bootstrap_lock = lock_tenant_for_bootstrap(tenant)
    tenant_version = lock_tenant_version(tenant)

    # We are not equipped to handle RoleBindings representing default access bindings; their representation in the
    # database is not always the same as their relation in Kessel. The correct way to re-replicate these tuples is by
    # forcibly re-bootstrapping the tenant.
    #
    # So, we will just exclude these RoleBindings from further processing entirely.
    builtin_binding_ids = bootstrap_lock.tenant_mapping.role_binding_ids()

    role_bindings = list(
        RoleBinding.objects.filter(pk__in=(b.pk for b in raw_role_bindings))
        .exclude(uuid__in=builtin_binding_ids)
        .select_related("role", "role__tenant")
        .prefetch_related("group_entries", "principal_entries", "role__permissions")
        .select_for_update(of=["self"])
    )

    binding_uuids = {str(b.uuid) for b in role_bindings}

    if tenant_version == TenantVersion.VERSION_1:
        binding_mappings_by_uuid = lock_binding_mappings_with_roles_by_uuid(binding_uuids)
        binding_mapping_uuids = set(binding_mappings_by_uuid.keys())

        if binding_uuids != binding_mapping_uuids:
            raise AssertionError(
                "Mismatch between BindingMappings and RoleBindings in V1 tenant: "
                f"BindingMapping UUIDs={binding_mapping_uuids}, "
                f"RoleBinding UUIDs={binding_uuids}"
            )

        for role_binding in role_bindings:
            binding_mapping = binding_mappings_by_uuid[str(role_binding.uuid)]

            if role_binding.role.v1_source_id != binding_mapping.role_id:
                raise AssertionError(
                    f"Mismatch between BindingMapping role and RoleBinding role: "
                    f"UUID={str(role_binding.uuid)}; "
                    f"BindingMapping pk={binding_mapping.pk!r}, V1 role ID={binding_mapping.role_id}; "
                    f"RoleBinding pk={role_binding.pk!r}, V2 role ID={role_binding.role_id}, "
                    f"V1 source ID={role_binding.role.v1_source_id}"
                )

    all_binding_tuples: list[RelationTuple] = []
    role_tuples_by_role_id: dict[int, list[RelationTuple]] = {}

    bindings_processed = 0

    for role_binding in role_bindings:
        bindings_processed += 1

        # Ensure we re-replicate all tuples for the binding.
        all_binding_tuples.extend(role_binding.all_tuples())

        # Ensure that we additionally re-replicate the relevant role (if it's a custom role).
        if role_binding.role.type == RoleV2.Types.CUSTOM:
            if role_binding.role.id not in role_tuples_by_role_id:
                role_tuples_by_role_id[role_binding.role.id] = RoleV2.tuples_for_create(role_binding.role)

    replicator = OutboxReplicator()

    for binding_tuples_batch in itertools.batched(all_binding_tuples, 1000):
        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.REMIGRATE_ROLE_BINDING,
                info={
                    "binding_uuids": binding_uuids,
                    "org_id": str(tenant.org_id),
                    "fix": "missing_binding_tuples_batch",
                    "source": "bindings",
                },
                partition_key=PartitionKey.byEnvironment(),
                add=list(binding_tuples_batch),
            )
        )

    role_tuples_processed = 0

    for role_tuples_batch in itertools.batched(itertools.chain.from_iterable(role_tuples_by_role_id.values()), 1000):
        role_tuples_processed += len(role_tuples_batch)

        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.REMIGRATE_ROLE_BINDING,
                info={
                    "binding_uuids": binding_uuids,
                    "org_id": str(tenant.org_id),
                    "fix": "missing_binding_tuples_batch",
                    "source": "roles",
                },
                partition_key=PartitionKey.byEnvironment(),
                add=list(role_tuples_batch),
            )
        )

    return bindings_processed, len(all_binding_tuples) + role_tuples_processed


def replicate_missing_binding_tuples(
    tenant: Optional[Tenant] = None, binding_uuids: Optional[list[str]] = None
) -> dict:
    """
    Replicate all tuples for specified bindings to fix missing relationships in Kessel.

    This fixes bindings created before REPLICATION_TO_RELATION_ENABLED=True that are missing
    base tuples (t_role and t_binding) in Kessel.

    At most one of tenant and binding_uuids can be non-None; if both are None, all bindings are re-replicated.

    Default access bindings are not re-replicated; re-rebootstrapping is required for this.

    Args:
        tenant (Tenant, optional): The tenant for which to fix bindings.
        binding_uuids (list[str], optional): List of binding UUIDs to fix.

    Returns:
        dict: Results with bindings_checked, bindings_fixed, and tuples_added count.
    """
    logger = logging.getLogger(__name__)

    if (tenant is not None) and (binding_uuids is not None):
        raise ValueError("At most one of a Tenant and a list of binding UUIDs must be provided.")

    # Get bindings to fix
    if binding_uuids is not None:
        bindings_query = RoleBinding.objects.filter(uuid__in=binding_uuids).order_by("tenant")
        logger.info(f"Fixing {len(binding_uuids)} specific bindings: {binding_uuids}")
    elif tenant is not None:
        # We do not need to lock anything here. We assume that replication is currently working correctly, so any
        # workspaces created after this instant will be correctly replicated.
        workspace_ids_to_fix = set(Workspace.objects.filter(tenant=tenant).values_list("id", flat=True))

        bindings_query = RoleBinding.objects.filter(
            Q(resource_type="workspace", resource_id__in=workspace_ids_to_fix)
            | Q(resource_type="tenant", resource_id=tenant.tenant_resource_id())
        )

        logger.info(f"Fixing {bindings_query.count()} bindings from tenant pk={tenant.pk!r}, org_id={tenant.org_id!r}")
    else:
        bindings_query = RoleBinding.objects.all().order_by("tenant")
        logger.warning(f"Fixing ALL bindings ({bindings_query.count()} total) - this may take a while")

    bindings_checked = 0
    bindings_fixed = 0
    total_tuples = 0

    # Process each binding in a separate transaction with locking
    for raw_binding_batch in itertools.batched(bindings_query.iterator(chunk_size=2000), 100):
        raw_bindings_by_tenant_id: dict[int, list[RoleBinding]] = {}

        for raw_binding in raw_binding_batch:
            raw_bindings_by_tenant_id.setdefault(raw_binding.tenant_id, []).append(raw_binding)

        # We should not get an empty batch.
        assert len(raw_bindings_by_tenant_id) > 0

        if tenant is not None:
            actual_tenants = list(raw_bindings_by_tenant_id.keys())

            if actual_tenants != [tenant.id]:
                raise AssertionError(f"Found unexpected tenant IDs: expected={[tenant.id]}, actual={actual_tenants}")

        for tenant_id, raw_bindings in raw_bindings_by_tenant_id.items():
            tenant_bindings_fixed, tenant_tuples_added = _do_replicate_missing_binding_tuples_batch(
                tenant_id=tenant_id, raw_role_bindings=raw_bindings
            )

            bindings_checked += len(raw_bindings)
            bindings_fixed += tenant_bindings_fixed
            total_tuples += tenant_tuples_added

        logger.info(f"Progress: {bindings_checked} bindings processed, {total_tuples} tuples added")

    results = {
        "bindings_checked": bindings_checked,
        "bindings_fixed": bindings_fixed,
        "tuples_added": total_tuples,
    }

    logger.info(f"Completed: Fixed {bindings_fixed} bindings with {total_tuples} total tuples")

    return results


def clean_invalid_workspace_resource_definitions(dry_run: bool = False) -> dict:
    """
    Clean resource definitions with invalid workspace IDs and update bindings accordingly.

    This finds custom roles with resource definitions pointing to non-existent workspaces,
    removes invalid workspace IDs, and uses the dual write handler to update bindings.

    Args:
        dry_run (bool): If True, only report what would be changed without making changes.

    Returns:
        dict: Results with roles_checked, resource_definitions_fixed, and changes list.
    """
    logger = logging.getLogger(__name__)
    from management.role.relation_api_dual_write_handler import RelationApiDualWriteHandler
    from management.relation_replicator.relation_replicator import ReplicationEventType

    roles_checked = 0
    resource_defs_fixed = 0
    changes = []

    if dry_run:
        logger.info("DRY RUN MODE - No changes will be made")

    # Get all custom roles with resource definitions
    custom_roles_with_rds = Role.objects.filter(system=False, access__resourceDefinitions__isnull=False).distinct()

    for raw_role in custom_roles_with_rds.iterator():
        role_had_invalid_rds = False

        with transaction.atomic():
            # Lock the role to prevent concurrent modifications
            role = Role.objects.select_for_update(of=["self"]).select_related("tenant").filter(pk=raw_role.pk).first()

            if role is None:
                logger.warning(f"Role vanished before it could be cleaned: pk={raw_role.pk!r}")
                continue

            try:
                assert_v1_write_allowed(role.tenant)
            except V1WriteBlockedError:
                logger.info(f"Skipping fixing resource definitions for role in V2 tenant: role pk={role.pk!r}")
                continue

            roles_checked += 1

            dual_write = RelationApiDualWriteHandler(role, ReplicationEventType.FIX_RESOURCE_DEFINITIONS)
            dual_write.prepare_for_update()

            for access in role.access.all():
                permission = access.permission

                # Only check workspace-related resource definitions
                for rd in access.resourceDefinitions.all():
                    if not is_resource_a_workspace(
                        permission.application, permission.resource_type, rd.attributeFilter
                    ):
                        continue

                    # Get workspace IDs from resource definition
                    workspace_ids, malformed_workspace_ids = get_workspace_ids_from_resource_definition_with_malformed(
                        rd.attributeFilter
                    )

                    # null is acceptable for us.
                    malformed_workspace_ids = [x for x in malformed_workspace_ids if x is not None]

                    # Check if the resource definition has None (for ungrouped workspace)
                    operation = rd.attributeFilter.get("operation")
                    original_value = rd.attributeFilter.get("value")
                    has_none_value = False

                    if operation == "in" and isinstance(original_value, list):
                        has_none_value = None in original_value
                    elif operation == "equal":
                        has_none_value = original_value is None

                    # Previously, we would attempt to exit early if workspace_ids is empty (believing there would be
                    # nothing to process). However, this isn't always valid. If the resource definition contains only
                    # malformed workspace IDs (e.g. invalid UUIDs or values that aren't strings), we still need to
                    # remove all of those values, but workspace_ids will be empty.

                    # Check which workspaces exist in the role's tenant
                    valid_workspace_ids = set(
                        str(ws_id)
                        for ws_id in Workspace.objects.filter(id__in=workspace_ids, tenant=role.tenant).values_list(
                            "id", flat=True
                        )
                    )

                    invalid_workspace_ids = set(str(ws_id) for ws_id in workspace_ids) - valid_workspace_ids

                    if invalid_workspace_ids or malformed_workspace_ids:
                        role_had_invalid_rds = True

                        # Calculate what the new value would be
                        operation_type = rd.attributeFilter.get("operation")
                        new_value: str | list | None
                        if operation_type == "equal":
                            # For "equal" operation, value should be a single string, None, or empty string
                            # Preserve None if it existed (for ungrouped workspace reference)
                            if has_none_value and not valid_workspace_ids:
                                new_value = None
                            else:
                                new_value = list(valid_workspace_ids)[0] if valid_workspace_ids else ""
                        else:
                            # For "in" operation, value should be a list
                            # Preserve None value if it existed (for ungrouped workspace reference)
                            new_value_list: list[str | None] = list(valid_workspace_ids) if valid_workspace_ids else []
                            if has_none_value:
                                new_value_list.append(None)
                            new_value = new_value_list

                        change_info = {
                            "role_uuid": str(role.uuid),
                            "role_name": role.name,
                            "permission": permission.permission,
                            "resource_definition_id": rd.id,
                            "operation": operation_type,
                            "original_value": original_value,
                            "new_value": new_value,
                            "invalid_workspaces": list(invalid_workspace_ids),
                            "malformed_workspaces": list(malformed_workspace_ids),
                            "valid_workspaces": list(valid_workspace_ids),
                            "preserved_none": has_none_value,
                        }

                        if dry_run:
                            logger.info(
                                f"[DRY RUN] Would update role '{role.name}' (uuid={role.uuid}), "
                                f"permission '{permission.permission}', RD #{rd.id}:\n"
                                f"  Original value: {original_value}\n"
                                f"  New value: {new_value}\n"
                                f"  Invalid workspace IDs removed: {list(invalid_workspace_ids)}\n"
                                f"  Valid workspace IDs kept: {list(valid_workspace_ids)}\n"
                                f"  None preserved: {has_none_value}"
                            )
                            change_info["action"] = "would_update"
                        else:
                            # Update resource definition to remove invalid workspace IDs
                            # Create new dict to ensure Django detects the change (JSONField mutation issue)
                            updated_filter = rd.attributeFilter.copy()
                            updated_filter["value"] = new_value

                            rd.attributeFilter = updated_filter
                            rd.save()
                            resource_defs_fixed += 1
                            change_info["action"] = "updated"

                            logger.info(
                                f"Updated role '{role.name}' (uuid={role.uuid}), "
                                f"permission '{permission.permission}', RD #{rd.id}: "
                                f"{original_value} -> {new_value}"
                            )

                        changes.append(change_info)

            # If we fixed any resource definitions, trigger dual write to update bindings
            if role_had_invalid_rds and not dry_run:
                dual_write.replicate_new_or_updated_role(role)  # Update bindings based on new RDs

    results = {
        "roles_checked": roles_checked,
        "resource_definitions_fixed": resource_defs_fixed,
        "changes": changes,
        "dry_run": dry_run,
    }

    if dry_run:
        logger.info(
            f"[DRY RUN] Would clean invalid workspace RDs: "
            f"{len(changes)} RDs would be fixed across {roles_checked} roles"
        )
    else:
        logger.info(f"Cleaned invalid workspace RDs: {resource_defs_fixed} RDs fixed for {len(changes)} permissions")

    return results


@transaction.atomic
def get_or_create_ungrouped_workspace(tenant: str) -> Workspace:
    """
    Retrieve the ungrouped workspace for the given tenant.

    Args:
        tenant (str): The tenant for which to retrieve the ungrouped workspace.
    Returns:
        Workspace: The ungrouped workspace object for the given tenant.
    """
    # fetch parent only once
    default_ws = Workspace.objects.get(tenant=tenant, type=Workspace.Types.DEFAULT)

    # single select_for_update + get_or_create
    workspace, created = Workspace.objects.select_for_update().get_or_create(
        tenant=tenant,
        type=Workspace.Types.UNGROUPED_HOSTS,
        defaults={"name": Workspace.SpecialNames.UNGROUPED_HOSTS, "parent": default_ws},
    )

    if created:
        RelationApiDualWriteWorkspaceHandler(
            workspace, ReplicationEventType.CREATE_WORKSPACE
        ).replicate_new_workspace()

    return workspace


def validate_relations_input(action, request_data) -> bool:
    """Check if request body provided to relations tool endpoints are valid."""
    validation_schema = RELATION_INPUT_SCHEMAS[action]
    try:
        validate(instance=request_data, schema=validation_schema)
        logger.info("JSON data is valid.")
        return True
    except jsonschema.exceptions.ValidationError as e:
        logger.info(f"JSON data is invalid: {e.message}")
        return False
    except Exception as e:
        logger.info(f"Exception occurred when validating JSON body: {e}")
        return False


def validate_inventory_input(action, request_data) -> bool:
    """Check if request body provided to inventory tool endpoints are valid."""
    validation_schema = INVENTORY_INPUT_SCHEMAS[action]
    try:
        validate(instance=request_data, schema=validation_schema)
        logger.info("JSON data is valid.")
        return True
    except jsonschema.exceptions.ValidationError as e:
        logger.info(f"JSON data is invalid: {e.message}")
        return False
    except Exception as e:
        logger.info(f"Exception occurred when validating JSON body: {e}")
        return False


def load_request_body(request) -> dict:
    """Decode request body from json into dict structure."""
    request_decoded = request.body.decode("utf-8")
    req_data = json.loads(request_decoded)
    return req_data


def is_resource_a_workspace(application: str, resource_type: str, attributeFilter: dict) -> bool:
    """Check if a given ResourceDefinition is a Workspace."""
    is_workspace_application = application == settings.WORKSPACE_APPLICATION_NAME
    is_workspace_resource_type = resource_type in settings.WORKSPACE_RESOURCE_TYPE
    is_workspace_group_filter = attributeFilter.get("key") == settings.WORKSPACE_ATTRIBUTE_FILTER
    return is_workspace_application and is_workspace_resource_type and is_workspace_group_filter


def get_workspace_ids_from_resource_definition_with_malformed(attributeFilter: dict) -> tuple[list[uuid.UUID], list]:
    """Get workspace id from a resource definition. Returns a tuple of the valid entries and the invalid entries."""
    operation = attributeFilter.get("operation")

    valid = []
    invalid = []

    if operation == "in":
        value = attributeFilter.get("value", [])

        for val in value:
            if is_str_valid_uuid(val):
                valid.append(uuid.UUID(val))
            else:
                invalid.append(val)

    elif operation == "equal":
        value = attributeFilter.get("value", "")

        if is_str_valid_uuid(value):
            valid.append(uuid.UUID(value))
        else:
            invalid.append(value)

    return valid, invalid


def get_workspace_ids_from_resource_definition(attributeFilter: dict) -> list[uuid.UUID]:
    """Get workspace id from a resource definition."""
    return get_workspace_ids_from_resource_definition_with_malformed(attributeFilter)[0]


def is_str_valid_uuid(uuid_str: str) -> bool:
    """Check if a string can be converted to a valid UUID."""
    if not isinstance(uuid_str, str):
        return False
    if uuid_str is None or not uuid_str:
        return False
    try:
        uuid.UUID(uuid_str)
        return True
    except ValueError:
        return False


def fix_admin_default_bindings(org_id: str) -> dict:
    """
    Fix missing admin default bindings for a tenant.

    Admin default bindings may be missing if the tenant was bootstrapped before
    the admin default group was seeded. This function re-replicates the admin
    default bindings for the tenant.

    This is safe to run even when replication is enabled because admin default
    bindings are NOT customizable (unlike platform default bindings).

    Args:
        org_id (str): Organization ID for the tenant to fix

    Returns:
        dict: Results with admin_bindings_replicated count or error
    """
    logger = logging.getLogger(__name__)

    try:
        tenant = Tenant.objects.get(org_id=org_id)
    except Tenant.DoesNotExist:
        return {"org_id": org_id, "error": f"Tenant {org_id} not found"}

    try:
        tenant_mapping = tenant.tenant_mapping
    except TenantMapping.DoesNotExist:
        return {"org_id": org_id, "error": "TenantMapping not found. Tenant may not be bootstrapped."}

    try:
        root_workspace = Workspace.objects.root(tenant=tenant)
        default_workspace = Workspace.objects.default(tenant=tenant)
    except Workspace.DoesNotExist as e:
        return {"org_id": org_id, "error": f"Missing root or default workspace: {str(e)}"}

    try:
        policy_service = GlobalPolicyIdService.shared()
        scope_resources = TenantScopeResources.for_models(
            tenant=tenant,
            default_workspace=default_workspace,
            root_workspace=root_workspace,
        )

        admin_bindings = default_role_binding_tuples(
            tenant_mapping=tenant_mapping,
            target_resources=scope_resources,
            access_type=DefaultAccessType.ADMIN,
            policy_service=policy_service,
        )

        if admin_bindings:
            replicator = OutboxReplicator()
            replicator.replicate(
                ReplicationEvent(
                    event_type=ReplicationEventType.BOOTSTRAP_TENANT,
                    info={
                        "org_id": org_id,
                        "admin_only": True,
                        "admin_bindings_count": len(admin_bindings),
                    },
                    partition_key=PartitionKey.byEnvironment(),
                    add=admin_bindings,
                )
            )
            return {"org_id": org_id, "admin_bindings_replicated": len(admin_bindings)}
        else:
            return {"org_id": org_id, "admin_bindings_replicated": 0}

    except DefaultGroupNotAvailableError:
        return {"org_id": org_id, "error": "Admin default group not available"}
    except Exception as e:
        logger.error(f"Error fixing admin default bindings for tenant {org_id}: {str(e)}", exc_info=True)
        return {"org_id": org_id, "error": str(e)}


def remove_unassigned_system_binding_mappings(replicator: Optional[RelationReplicator] = None):
    """
    Remove unassigned BindingMappings for system roles.

    Normally, when we remove a subject from a system BindingMapping, we delete it if it now has no subject (is
    "unassigned"); if we need it later, we will create a new one on demand. Apparently, at some points in the past, we
    have failed to do this, so this migration removes any such BindingMappings.

    Note that we do not and cannot do the same for custom roles. For custom roles, we maintain the invariant that all of
    the necessary BindingMappings always exist, even if they are unassigned. This allows assigning a custom role to a
    group to be implemented as iterating over all of the BindingMappings for that role and adding the group to each.
    """
    if replicator is None:
        replicator = OutboxReplicator()

    for raw_mapping in (
        BindingMapping.objects.filter(mappings__users={}, mappings__groups=[], role__system=True)
        .select_related("role")
        .iterator()
    ):
        # We must use SERIALIZABLE here because we are potentially interacting with concurrent V2 writers (which all
        # use SERIALIZABLE rather than explicit locking).
        #
        # Unfortunately, we must *also* use select_for_update() here because we are potentially interacting with
        # concurrent V1 writers (which all use SELECT FOR UPDATE rather than SERIALIZABLE).
        with atomic_block():
            mapping: Optional[BindingMapping] = (
                BindingMapping.objects.filter(pk=raw_mapping.pk)
                .select_related("role")
                .select_for_update(of=["self"])
                .first()
            )

            if mapping is None:
                logger.warning(f"BindingMapping vanished before it could be removed: pk={raw_mapping.pk!r}")
                continue

            if not mapping.is_unassigned():
                logger.warning(
                    f"BindingMapping is no longer unassigned: " f"pk={mapping.pk!r}, mappings={mapping.mappings}"
                )

                continue

            if not mapping.role.system:
                raise AssertionError(
                    f"BindingMapping is no longer associated with system role: "
                    f"BindingMapping pk={mapping.pk!r}, role pk={mapping.role.pk!r}"
                )

            # Ensure that we will not accidentally remove system role permissions.
            if (not mapping.mappings["role"]["is_system"]) or (len(mapping.mappings["role"]["permissions"]) > 0):
                raise AssertionError(
                    f"BindingMapping for system role is inconsistent: "
                    f"pk={mapping.pk!r}, mappings={mapping.mappings}"
                )

            if not (
                (mapping.resource_type_namespace == "rbac") and (mapping.resource_type_name in ("tenant", "workspace"))
            ):
                logger.warning(
                    f"Unexpected role binding resource type: "
                    f"{mapping.resource_type_namespace}/{mapping.resource_type_name}"
                )

                continue

            tenant: Tenant | None = None

            if mapping.resource_type_name == "workspace":
                workspace = Workspace.objects.filter(id=mapping.resource_id).first()

                if workspace is None:
                    logger.warning(f"Unknown workspace ID for binding: {mapping.resource_id}")
                    continue

                tenant = workspace.tenant
            elif mapping.resource_type_name == "tenant":
                org_id = Tenant.tenant_resource_id_to_org_id(mapping.resource_id)
                tenant = Tenant.objects.filter(org_id=org_id).first()

                if tenant is None:
                    logger.warning(f"Unknown tenant resource ID for binding: {mapping.resource_id}")
                    continue

            if tenant is None:
                raise AssertionError("Tenant should have been found")

            try:
                assert_v1_write_allowed(tenant)
            except V1WriteBlockedError:
                logger.info(f"Not processing BindingMapping for V2 tenant: pk={mapping.pk!r}, tenant pk={tenant.pk!r}")
                continue
            except TenantNotBootstrappedError:
                logger.warning(f"Tenant not bootstrapped: pk={tenant.pk!r}. Not processing BindingMapping.")
                continue

            # Because a BindingMapping exists for this role binding, and BindingMappings for system roles are always
            # locked for update, we know that this binding cannot be concurrently updated by V1 code.
            #
            # If the tenant is a V2 tenant, then it could concurrently be updated by V2 code, but then we will have a
            # serialization failure if a subject is added after we checked that none exists.
            role_binding = RoleBinding.objects.filter(uuid=mapping.mappings["id"]).select_for_update().first()

            if role_binding is not None:
                if role_binding.group_entries.all().exists() or role_binding.principal_entries.all().exists():
                    raise AssertionError(
                        f"BindingMapping has no subjects, while associated RoleBinding has subjects: "
                        f"uuid={mapping.mappings['id']}, BindingMapping pk={mapping.pk!r}, "
                        f"RoleBinding pk={role_binding.pk!r}"
                    )

            logger.info(
                f"Removing empty role binding: uuid={mapping.mappings['id']}, "
                f"BindingMapping pk={mapping.pk!r}, "
                f"RoleBinding pk={(role_binding.pk if role_binding is not None else None)!r}"
            )

            replicator.replicate(
                ReplicationEvent(
                    event_type=ReplicationEventType.REMOVE_UNASSIGNED_BINDING_MAPPINGS,
                    partition_key=PartitionKey.byEnvironment(),
                    add=[],
                    remove=mapping.as_tuples(),
                    info={
                        "binding_uuid": mapping.mappings["id"],
                    },
                )
            )

            mapping.delete()

            if role_binding is not None:
                role_binding.delete()


@atomic
def _do_remove_orphaned_car(raw_car: CrossAccountRequest, replicator: RelationReplicator):
    logger.info(f"Processing orphaned CAR: pk={raw_car.pk!r}")

    orphaned_car: CrossAccountRequest = CrossAccountRequest.objects.select_for_update().filter(pk=raw_car.pk).first()

    if orphaned_car is None:
        logger.info(f"Orphaned CAR vanished before it could be removed: pk={raw_car.pk!r}")
        return

    if orphaned_car.status != "approved":
        logger.info(f"Skipping orphaned CAR is no longer approved: pk={raw_car.pk!r}")
        return

    if orphaned_car.user_id is None:
        logger.info(f"Skipping orphaned CAR with null user_id: pk={raw_car.pk!r}")
        return

    # If the user is somehow created after this check, that's okay; at worst, they'll have to make a new
    # cross-account request.
    if Principal.objects.filter(user_id=orphaned_car.user_id).exists():
        logger.info(f"Skipping CAR that is no longer orphaned: pk={raw_car.pk!r}")
        return

    target_tenant = Tenant.objects.filter(org_id=orphaned_car.target_org).first()

    if target_tenant is None:
        logger.info(f"Skipping orphaned CAR with non-existent tenant: pk={orphaned_car.pk}")
        return

    tenant_version = lock_tenant_version(target_tenant)

    # Since the principals don't exist, we need to ensure that BindingMappings will be treated as authoritative (so
    # that we can later reconstruct the correct RoleBindings from them). This is the case only for V1 tenants.
    if tenant_version != TenantVersion.VERSION_1:
        logger.info(f"Skipping orphaned CAR in non-V1 tenant: pk={orphaned_car.pk}")
        return

    if RoleBindingPrincipal.objects.filter(source=str(orphaned_car.source_key())).exists():
        raise AssertionError(f"Expected no principal entries to have been created for CAR with pk={orphaned_car.pk!r}")

    car_binding_mappings = list(
        BindingMapping.objects.select_for_update().filter(mappings__users__has_key=str(orphaned_car.source_key()))
    )

    for binding_mapping in car_binding_mappings:
        if binding_mapping.mappings["users"][str(orphaned_car.source_key())] != orphaned_car.user_id:
            raise AssertionError(
                f"Unexpected mapping: "
                f"mapping pk={binding_mapping.pk!r}, "
                f"mappings={binding_mapping.mappings['users']}, "
                f"CAR pk={orphaned_car.pk!r}"
            )

    # Any RoleBindings corresponding to the found BindingMappings cannot *possibly* be correct. The BindingMappings
    # all include the CAR's user ID, but we have already determined that no Principal with that user ID exists,
    # so there cannot possibly be a RoleBindingPrincipal that refers to such a Principal. Thus, we should delete any
    # such RoleBindings and require them to be re-created later. (This is a valid state for a V1 tenant to be in,
    # but it must be fixed before the tenant migrates to V2.)
    bad_role_bindings = list(
        RoleBinding.objects.select_for_update()
        .filter(tenant=target_tenant)
        .filter(uuid__in=(bm.mappings["id"] for bm in car_binding_mappings))
    )

    if len(bad_role_bindings) > 0:
        logger.warning(
            "Removing mismatched RoleBindings: "
            + ", ".join(f"pk={rb.pk!r}, uuid={str(rb.uuid)}" for rb in bad_role_bindings)
        )

        RoleBinding.objects.filter(pk__in=(rb.pk for rb in bad_role_bindings)).delete()

    orphaned_car.status = "expired"
    orphaned_car.save()

    dual_write_handler = RelationApiDualWriteCrossAccessHandler(
        cross_account_request=orphaned_car,
        event_type=ReplicationEventType.EXPIRE_CROSS_ACCOUNT_REQUEST,
        replicator=replicator,
    )

    # We have to suppress the migration to RoleBindings because there might be a single BindingMapping with
    # principals from multiple orphaned CARs (in which case it still couldn't be migrated after we remove the first
    # one). We know that the tenant is a V1 tenant, so its BindingMappings will be treated as authoritative.
    #
    # We have just deleted any RoleBindings that could exist for the CAR, and we will just not create any here.
    # Actually re-creating the RoleBindings is left to another migration (in practice, the migrate_binding_scope
    # migration).
    dual_write_handler.generate_relations_to_remove_roles(orphaned_car.roles.all(), suppress_v1_migration=True)
    dual_write_handler.replicate()

    logger.info(f"Expired orphaned CAR: pk={orphaned_car.pk!r}")


@atomic
def expire_orphaned_cross_account_requests(replicator: Optional[RelationReplicator] = None):
    """Expire cross-account requests that refer to a principal that no longer exists."""
    if replicator is None:
        replicator = OutboxReplicator()

    # We must exclude user_id=None in the subquery. Otherwise, if the user_id is not present and at least one Principal
    # user_id is null, the result of the IN operator will also be null. (I think this is bad, but oh well.) Then,
    # when Django negates the null, it will still be null. This means the where clause will at best be (TRUE AND NULL
    # AND TRUE), which will still filter out the row.
    orphaned_cars = (
        CrossAccountRequest.objects.filter(status="approved")
        .exclude(user_id__in=Principal.objects.exclude(user_id=None).values_list("user_id", flat=True))
        .exclude(user_id=None)
    )

    logger.info(f"About to process ~{orphaned_cars.count()} orphaned cross-account requests.")

    count = 0
    failed = 0

    for orphaned_car in orphaned_cars.iterator():
        count += 1

        try:
            _do_remove_orphaned_car(orphaned_car, replicator)
        except Exception:
            logger.error(f"Failed to remove orphaned CAR: pk={orphaned_car.pk!r}", exc_info=True)
            failed += 1

    logger.info(f"Processed {count} orphaned CARs, of which {failed} failed.")

    if failed > 0:
        raise RuntimeError(f"Failed to expire {failed} orphan CARs")
