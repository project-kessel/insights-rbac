#
# Copyright 2025 Red Hat, Inc.
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
import dataclasses
import itertools
import logging
from collections.abc import Callable, Iterable
from typing import Protocol

from api.models import Tenant
from django.db import transaction, OperationalError
from internal.utils import (
    replicate_missing_binding_tuples,
    iterate_tuples_from_kessel,
    lock_binding_mappings_with_roles_by_uuid,
)
from management.atomic_transactions import atomic, atomic_block
from management.models import Role, Workspace
from management.relation_replicator.logging_replicator import stringify_spicedb_relationship
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEvent, ReplicationEventType, PartitionKey
from management.role.relations import role_owner_relationship
from management.role.v2_model import RoleV2
from management.role_binding.model import RoleBinding
from management.tenant_mapping.model import DefaultAccessType
from management.tenant_mapping.v2_activation import (
    lock_tenant_version,
    TenantVersion,
)
from management.tenant_service.v2 import lock_tenant_for_bootstrap, TenantBootstrapLock
from migration_tool.in_memory_tuples import RelationTuple
from migration_tool.models import V2boundresource, role_permission_tuple
from migration_tool.utils import create_relationship
from psycopg2.errors import DeadlockDetected, SerializationFailure
from kessel.relations.v1beta1.common_pb2 import Relationship

logger = logging.getLogger(__name__)


type _CommitRemoval = Callable[[Iterable[Relationship | RelationTuple]], None]


def _as_relation_tuple(relation: Relationship | RelationTuple) -> RelationTuple:
    """Convert a Relationship or RelationTuple to a RelationTuple."""
    if isinstance(relation, RelationTuple):
        return relation

    if isinstance(relation, Relationship):
        return RelationTuple.from_message(relation)

    raise TypeError(f"Expected Relationship or RelationTuple, but got: {relation!r}")


def _as_relationship(relation: Relationship | RelationTuple) -> Relationship:
    """Convert a Relationship or RelationTuple to a Relationship."""
    if isinstance(relation, Relationship):
        return relation

    if isinstance(relation, RelationTuple):
        return relation.as_message()

    raise TypeError(f"Expected Relationship or RelationTuple, but got: {relation!r}")


class _ReadTuplesTyped(Protocol):
    def __call__(
        self, *, resource_type: str, resource_id: str, relation: str, subject_type: str, subject_id: str
    ) -> Iterable[RelationTuple]: ...


def _make_read_tuples_typed(read_tuples: Callable[[str, str, str, str, str], Iterable[dict]]) -> _ReadTuplesTyped:
    def impl(
        resource_type: str, resource_id: str, relation: str, subject_type: str, subject_id: str
    ) -> Iterable[RelationTuple]:
        return (
            RelationTuple.from_message_dict(r["tuple"])
            for r in read_tuples(resource_type, resource_id, relation, subject_type, subject_id)
        )

    return impl


def _local_system_role_ids() -> set[str]:
    return set(str(u) for u in Role.objects.filter(system=True).values_list("uuid", flat=True)) | set(
        str(u) for u in RoleV2.objects.exclude(type=RoleV2.Types.CUSTOM).values_list("uuid", flat=True)
    )


class _RemoteBindingState:
    _relations: set[RelationTuple]
    _role_ids: set[str]

    def __init__(self):
        self._relations = set()
        self._role_ids = set()

    @staticmethod
    def _check_str_id(value):
        if not isinstance(value, str):
            raise TypeError(f"Expected ID to be a string, but got: {value!r}")

    def add_relations(self, relations: Iterable[Relationship | RelationTuple]):
        self._relations.update(_as_relation_tuple(r) for r in relations)

    def add_role(self, role_id: str):
        self._check_str_id(role_id)
        self._role_ids.add(role_id)

    def relations(self) -> set[RelationTuple]:
        return set(self._relations)

    def role_ids(self) -> set[str]:
        return set(self._role_ids)


def _collect_remote_relations_for_binding(
    binding_id: str,
    scope_relations: list[tuple[str, str]],
    read_tuples_typed: _ReadTuplesTyped,
) -> _RemoteBindingState:
    """
    Collect all relations to remove for a single binding, including custom role permissions.

    Args:
        binding_id: The role_binding UUID to clean
        scope_relations: List of (resource_type, resource_id) tuples for scope bindings
        read_tuples_typed: Function to read tuples from Kessel

    Returns:
        _RemoteBindingState for the binding
    """
    result = _RemoteBindingState()

    # Add relation from role binding to role. (There should only be one of these.)
    role_tuples = list(
        read_tuples_typed(
            resource_type="role_binding",
            resource_id=binding_id,
            relation="role",
            subject_type="role",
            subject_id="",
        )
    )

    result.add_relations(role_tuples)

    for t in role_tuples:
        result.add_role(t.subject.subject.id)

    # Add relation from role binding to group subjects.
    result.add_relations(
        read_tuples_typed(
            resource_type="role_binding",
            resource_id=binding_id,
            relation="subject",
            subject_type="group",
            subject_id="",
        )
    )

    result.add_relations(
        read_tuples_typed(
            resource_type="role_binding",
            resource_id=binding_id,
            relation="subject",
            subject_type="principal",
            subject_id="",
        )
    )

    # Add the relations from the underlying resource(s) to the role binding.
    for resource_type, resource_id in scope_relations:
        result.add_relations(
            [
                create_relationship(
                    ("rbac", resource_type),
                    resource_id,
                    ("rbac", "role_binding"),
                    binding_id,
                    "binding",
                )
            ]
        )

    return result


def _collect_role_resource_relations(role_id: str, read_tuples_typed: _ReadTuplesTyped) -> list[RelationTuple]:
    """
    Collect all known tuples whose resource is rbac/role:<role_id> (permissions and owner).
    """
    return [
        *read_tuples_typed(
            resource_type="role",
            resource_id=role_id,
            relation="",
            subject_type="principal",
            subject_id="*",
        ),
        *read_tuples_typed(
            resource_type="role",
            resource_id=role_id,
            relation="owner",
            subject_type="tenant",
            subject_id="",
        ),
    ]


@dataclasses.dataclass
class _RemoveRoleBindingsResult:
    ordinary_bindings_altered_count: int
    default_access_bindings_removed_count: int
    custom_roles_altered_count: int


def _remove_orphaned_role_bindings(
    *,
    tenant: Tenant,
    resource_type: str,
    resource_id: str,
    read_tuples_typed: _ReadTuplesTyped,
    commit_removal: _CommitRemoval,
) -> _RemoveRoleBindingsResult:
    """
    Find and remove orphaned relations for role bindings attached to a resource (tenant or workspace).

    For each binding found:
    - Collects binding->role, binding->group, and scope->binding relations to remove
    - Collects custom V2 role permission relations inline

    Special handling for built-in bindings (from TenantMapping):
    - If tenant has NO custom default group: skip built-in bindings entirely
    - If tenant HAS custom default group: only remove scope binding (resource->binding),
      don't do normal cleanup (preserve binding->role, binding->group relationships)

    Args:
        tenant: The tenant
        resource_type: "tenant" or "workspace"
        resource_id: The resource ID
        read_tuples_typed: Function to read tuples from Kessel

    Returns: _RemoveRoleBindingsResults indicating what was done
    """
    scope_relations = [(resource_type, resource_id)]
    seen_role_ids: set[str] = set()

    bindings_altered_count = 0
    builtin_scope_cleaned_count = 0

    binding_tuples = read_tuples_typed(
        resource_type=resource_type,
        resource_id=resource_id,
        relation="binding",
        subject_type="role_binding",
        subject_id="",
    )

    for batch in itertools.batched(binding_tuples, 100):
        binding_ids: set[str] = {t.subject.subject.id for t in batch}

        lookup_result_by_binding = {
            b: _collect_remote_relations_for_binding(
                binding_id=b,
                scope_relations=scope_relations,
                read_tuples_typed=read_tuples_typed,
            )
            for b in binding_ids
        }

        # We must use SERIALIZABLE here because we are potentially interacting with concurrent V2 writers (which all
        # use SERIALIZABLE rather than explicit locking).
        #
        # Unfortunately, we must *also* use select_for_update() for V1 tenants here because we are potentially
        # interacting with concurrent V1 writers (which all use SELECT FOR UPDATE rather than SERIALIZABLE).
        with atomic_block():
            to_remove: list[RelationTuple | Relationship] = []

            bootstrap_lock: TenantBootstrapLock = lock_tenant_for_bootstrap(tenant)
            builtin_binding_ids: set[str] = bootstrap_lock.tenant_mapping.role_binding_ids()

            tenant_is_v1 = lock_tenant_version(tenant) == TenantVersion.VERSION_1

            if tenant_is_v1:
                binding_mappings_by_id = lock_binding_mappings_with_roles_by_uuid(binding_ids)
            else:
                binding_mappings_by_id = {}

            # We theoretically don't need to take a lock here for V2 tenants, but it's simpler to just always do so.
            role_bindings: list[RoleBinding] = list(
                RoleBinding.objects.filter(uuid__in=binding_ids)
                .prefetch_related("group_entries", "principal_entries")
                .select_for_update(of=["self"])
            )

            role_bindings_by_id: dict[str, RoleBinding] = {str(b.uuid): b for b in role_bindings}

            for binding_id in binding_ids:
                binding_mapping = binding_mappings_by_id.get(binding_id)
                role_binding = role_bindings_by_id.get(binding_id)

                if (binding_mapping is not None) and (role_binding is None):
                    raise AssertionError(
                        f"RoleBinding should exist if BindingMapping does, but it does not for binding {binding_id}"
                    )

                # Handle built-in bindings specially.
                if binding_id in builtin_binding_ids:
                    # If there is a custom default group: remove the resource->binding relation for user bindings.
                    # If there is not a custom default group, leave all bindings untouched.
                    if bootstrap_lock.custom_default_group is not None:
                        binding_source = bootstrap_lock.tenant_mapping.source_for_role_binding_id(binding_id)

                        if binding_source is None:
                            raise AssertionError("Binding source should be non-None for built-in binding.")

                        if binding_source[0] == DefaultAccessType.USER:
                            to_remove.append(
                                create_relationship(
                                    ("rbac", resource_type),
                                    resource_id,
                                    ("rbac", "role_binding"),
                                    binding_id,
                                    "binding",
                                )
                            )

                            builtin_scope_cleaned_count += 1
                else:
                    logger.debug(f"Processing binding {binding_id} on {resource_type}:{resource_id}")

                    lookup_result = lookup_result_by_binding[binding_id]
                    actual_relations: set[RelationTuple] = lookup_result.relations()

                    expected_relations: set[RelationTuple] = (
                        set(role_binding.all_tuples()) if role_binding is not None else set()
                    )

                    orphan_relations = actual_relations - expected_relations

                    if len(orphan_relations) > 0:
                        to_remove.extend(orphan_relations)
                        bindings_altered_count += 1

                    seen_role_ids.update(lookup_result.role_ids())

            commit_removal(to_remove)

    custom_roles_altered_count = _remove_orphaned_custom_role_relations(
        seen_role_ids=seen_role_ids,
        read_tuples_typed=read_tuples_typed,
        commit_removal=commit_removal,
    )

    return _RemoveRoleBindingsResult(
        ordinary_bindings_altered_count=bindings_altered_count,
        default_access_bindings_removed_count=builtin_scope_cleaned_count,
        custom_roles_altered_count=custom_roles_altered_count,
    )


def _remove_orphaned_custom_role_relations(
    seen_role_ids: set[str], read_tuples_typed: _ReadTuplesTyped, commit_removal: _CommitRemoval
) -> int:
    """
    Remove orphaned relations for custom roles with the specified UUIDs.

    Returns the number of roles affected.
    """
    altered_count = 0

    # We cache a non-authoritative set of system role IDs here at the start. At the beginning of each batch,
    # we will use this to filter out any IDs known to correspond to system roles. Then, within each batch,
    # we will load a new authoritative value, use it to fully eliminate all system roles, and then leave it there as a
    # cache for the next batch.
    #
    # If a system role is created after this point, that's fine, because it will be correctly removed with the
    # authoritative check.
    #
    # If a system role is removed after this point, that's fine, because the removal is assumed to correctly remove
    # all relations for it.
    system_role_ids = _local_system_role_ids()

    for batch_raw_role_ids_tuple in itertools.batched(seen_role_ids, 100):
        batch_raw_role_ids = set(batch_raw_role_ids_tuple)

        # As described above, filter out any roles we can now, but we will still have to do the authoritative check
        # later in a transaction.
        batch_raw_role_ids -= system_role_ids

        actual_relations_by_role_id = {
            role_id: set(_collect_role_resource_relations(role_id=role_id, read_tuples_typed=read_tuples_typed))
            for role_id in batch_raw_role_ids
        }

        # Do this in a SERIALIZABLE transaction so that we have a consistent view of seeded roles.
        with atomic_block():
            to_remove = []

            # Load an authoritative system_role_ids (valid for the rest of the transaction, then cached, as described
            # above).
            system_role_ids = _local_system_role_ids()
            custom_role_ids = batch_raw_role_ids - system_role_ids

            # Paranoia.
            if RoleV2.objects.filter(uuid__in=custom_role_ids).exclude(type=RoleV2.Types.CUSTOM).exists():
                raise AssertionError(f"Unexpected non-custom role ID in {custom_role_ids}")

            roles_by_id: dict[str, RoleV2] = {
                str(r.uuid): r
                for r in RoleV2.objects.filter(uuid__in=custom_role_ids)
                .select_related("tenant")
                .prefetch_related("permissions")
                .select_for_update(of=["self"])
            }

            for role_id in custom_role_ids:
                local_role = roles_by_id.get(role_id)

                actual_relations: set[RelationTuple] = actual_relations_by_role_id[role_id]

                if local_role is None:
                    expected_relations: set[RelationTuple] = set()
                else:
                    expected_relations = set(RoleV2.tuples_for_create(local_role))

                orphan_relations = actual_relations - expected_relations

                if len(orphan_relations) > 0:
                    logger.info(f"Removing {len(orphan_relations)} permission relations for role {role_id}")
                    to_remove.extend(orphan_relations)

                    altered_count += 1

            commit_removal(to_remove)

    return altered_count


class _RemoteWorkspaceData:
    _parents_by_workspace: dict[str, set[V2boundresource]]

    def __init__(self):
        self._parents_by_workspace = {}

    @classmethod
    def _check_workspace_id(cls, workspace_id):
        if not isinstance(workspace_id, str):
            raise TypeError(f"Expected workspace ID to be a string, but got: {workspace_id!r}")

    def add_workspace(self, *, workspace_id: str, parent_resource: V2boundresource):
        self._check_workspace_id(workspace_id)
        self._parents_by_workspace.setdefault(workspace_id, set()).add(parent_resource)

    def has_workspace(self, workspace_id: str) -> bool:
        self._check_workspace_id(workspace_id)
        return workspace_id in self._parents_by_workspace

    def parents_for(self, workspace_id: str) -> set[V2boundresource]:
        self._check_workspace_id(workspace_id)
        return self._parents_by_workspace.get(workspace_id, set())

    def workspace_ids(self) -> set[str]:
        return set(self._parents_by_workspace.keys())


def _workspace_parent_relationship(workspace_id: str, parent: V2boundresource) -> RelationTuple:
    return create_relationship(
        resource_name=("rbac", "workspace"),
        resource_id=workspace_id,
        relation="parent",
        subject_name=parent.resource_type,
        subject_id=parent.resource_id,
    )


def _collect_remote_workspaces(tenant: Tenant, read_tuples_typed: _ReadTuplesTyped) -> _RemoteWorkspaceData:
    """
    Discover workspaces reachable in Kessel under the tenant's hierarchy.

    Bootstrapping may omit workspace(root)#parent@tenant, so we do not rely on that tuple for discovery. We seed DFS
    from the DB root workspace, then walk workspace#parent@workspace edges. Only those edges are recorded in the
    result; the root workspace ID itself is not inserted as a remote workspace with a synthetic tenant parent (that
    would misrepresent Kessel and still would not enqueue removal of a stale root→tenant tuple here—we never read the
    root's parent tuples). Clear legacy root→tenant edges via remove_legacy_root_workspace_tenant_parent_relations.

    Args:
        tenant: Tenant whose workspaces to traverse
        read_tuples_typed: Function to read tuples from Kessel

    Returns:
        Workspace parent edges discovered via workspace#parent@workspace (non-root workspaces only if the graph is
        tree-shaped under the DB root).
    """
    workspace_data = _RemoteWorkspaceData()
    stack = []

    def add_seen_workspace(workspace_id: str, parent_resource: V2boundresource):
        # Enqueue this workspace for further searching, but ensure that we don't end up in a loop.
        if not workspace_data.has_workspace(workspace_id):
            stack.append(workspace_id)

        workspace_data.add_workspace(workspace_id=workspace_id, parent_resource=parent_resource)

    root = Workspace.objects.root(tenant=tenant)

    # DFS seed only: do not record workspace(root)#parent@tenant (may be absent or legacy); see docstring.
    stack.append(str(root.id))

    # DFS to find child workspaces
    while stack:
        parent_ws_id = stack.pop()  # LIFO for DFS

        # Find workspaces where parent is this workspace
        child_tuples = read_tuples_typed(
            resource_type="workspace",
            resource_id="",
            relation="parent",
            subject_type="workspace",
            subject_id=parent_ws_id,
        )

        for t in child_tuples:
            add_seen_workspace(t.resource.id, V2boundresource(("rbac", "workspace"), parent_ws_id))

    return workspace_data


def _local_workspace_ids_for(tenant: Tenant) -> set[str]:
    return set(str(u) for u in Workspace.objects.filter(tenant=tenant).values_list("id", flat=True))


# This synchronizes with workspace creation in WorkspaceService being SERIALIZABLE (ensuring that a workspace we're
# interested in isn't created after we check).
@atomic
def _remove_orphaned_workspace_parent_relations(
    tenant: Tenant, workspace_data: _RemoteWorkspaceData, commit_removal: _CommitRemoval
) -> int:
    """Removes any parent relations for workspaces that no longer exist locally, then returns the number removed."""
    remote_ids = workspace_data.workspace_ids()

    existing_local_ids = _local_workspace_ids_for(tenant)
    orphaned_ids = remote_ids - existing_local_ids

    logger.info(
        f"Found {len(orphaned_ids)} orphaned workspace IDs for tenant with org_id={tenant.org_id!r}: {orphaned_ids}"
    )

    incorrect_relations = [
        _workspace_parent_relationship(workspace_id, parent)
        for workspace_id in orphaned_ids
        for parent in workspace_data.parents_for(workspace_id)
    ]

    logger.info(
        f"Removing {len(incorrect_relations)} orphaned workspace parent relations "
        f"for tenant with org_id={tenant.org_id!r}."
    )

    commit_removal(incorrect_relations)

    return len(incorrect_relations)


def _remove_incorrect_workspace_parent_relations(
    tenant: Tenant, workspace_data: _RemoteWorkspaceData, commit_removal: _CommitRemoval
) -> int:
    remote_ids = workspace_data.workspace_ids()
    tenant_resource = V2boundresource.for_model(tenant)

    removed_count = 0

    for remote_ids_batch in itertools.batched(remote_ids, 100):
        with transaction.atomic():
            to_remove = []

            # We need to lock the workspaces because their parents could change while we're working.
            workspaces = list(Workspace.objects.filter(id__in=remote_ids_batch).select_for_update())

            for workspace in workspaces:
                workspace_id = str(workspace.id)

                remote_parents = workspace_data.parents_for(workspace_id)

                expected_parents = {
                    (
                        V2boundresource.for_workspace_id(str(workspace.parent_id))
                        if workspace.parent_id is not None
                        else tenant_resource
                    )
                }

                excess_parents = remote_parents - expected_parents

                for excess_parent in excess_parents:
                    to_remove.append(_workspace_parent_relationship(workspace_id, excess_parent))

            logger.info(
                f"Removing {len(to_remove)} incorrect workspace parent relationships "
                f"for tenant with org_id={tenant.org_id!r}."
            )

            commit_removal(to_remove)
            removed_count += len(to_remove)

    logger.info(
        f"Removed a total of {removed_count} incorrect workspace parent relations "
        f"for tenant with org_id={tenant.org_id!r}."
    )

    return removed_count


def cleanup_tenant_orphaned_relationships(
    tenant: Tenant,
    read_tuples_fn,
    dry_run: bool = False,
) -> dict:
    """
    Remove orphaned role binding relationships for a tenant.

    This handles the following types of orphaned relation:
    * Relations for role bindings that are incorrect or no longer exist.
    * Parent relations for workspaces that no longer exist.
    * Incorrect parent relations for workspaces that still exist.

    Args:
        tenant: The Tenant object to clean relationships for
        read_tuples_fn: Function to read tuples from Kessel, signature:
                        (resource_type: str, resource_id: str, relation: str,
                         subject_type: str = "", subject_id: str = "") -> list[dict]
        dry_run: If True, only report what would be deleted without making changes

    Returns:
        dict: Results including bindings found, relations to remove, etc.
    """
    if tenant.tenant_name == "public":
        raise ValueError("Cannot remove orphaned relationships from public tenant.")

    replicator = OutboxReplicator()
    removed_count = 0

    read_tuples_typed = _make_read_tuples_typed(read_tuples_fn)

    # Process bindings and collect relations to remove
    # Counters for results
    ordinary_bindings_altered_count = 0
    builtin_scope_cleaned_count = 0
    custom_roles_altered_count = 0

    def commit_removal(relations: Iterable[Relationship | RelationTuple]):
        nonlocal removed_count

        converted_relations = [_as_relationship(r) for r in relations]

        if not converted_relations:
            return

        logger.info(f"Tenant {tenant.org_id} - relationships to remove:")

        for r in converted_relations:
            logger.info(f"  Removing: {stringify_spicedb_relationship(r)}")

        if not dry_run:
            replicator.replicate(
                ReplicationEvent(
                    event_type=ReplicationEventType.CLEANUP_ORPHAN_BINDINGS,
                    info={
                        "org_id": tenant.org_id,
                    },
                    partition_key=PartitionKey.byEnvironment(),
                    remove=converted_relations,
                )
            )

        removed_count += len(converted_relations)

    def do_remove_role_bindings(resource_type: str, resource_id: str):
        nonlocal ordinary_bindings_altered_count, builtin_scope_cleaned_count, custom_roles_altered_count

        result = _remove_orphaned_role_bindings(
            tenant=tenant,
            resource_type=resource_type,
            resource_id=resource_id,
            read_tuples_typed=read_tuples_typed,
            commit_removal=commit_removal,
        )

        ordinary_bindings_altered_count += result.ordinary_bindings_altered_count
        builtin_scope_cleaned_count += result.default_access_bindings_removed_count
        custom_roles_altered_count += result.custom_roles_altered_count

    tenant_resource_id = tenant.tenant_resource_id()

    if tenant_resource_id is None:
        raise ValueError("Expected tenant's resource ID to be present (i.e. for it to have an org_id).")

    kessel_workspace_data = _collect_remote_workspaces(tenant=tenant, read_tuples_typed=read_tuples_typed)

    workspace_ids_in_kessel = set(kessel_workspace_data.workspace_ids())

    logger.info(
        f"Discovered {len(workspace_ids_in_kessel)} workspaces in Kessel for tenant with org_id={tenant.org_id!r}."
    )

    do_remove_role_bindings("tenant", tenant_resource_id)

    # We do not need to lock the workspaces here. Attempting to remove from a workspace that is later deleted is fine
    # (since we will at worst delete some role bindings that should have already been deleted), and missing a
    # workspace created later is fine (since we expect that any new operations will be correctly replicated).
    for ws_id in workspace_ids_in_kessel.union(_local_workspace_ids_for(tenant)):
        do_remove_role_bindings("workspace", ws_id)

    logger.info(
        f"Tenant {tenant.org_id} cleanup summary: "
        f"bindings={ordinary_bindings_altered_count}, builtin_scope={builtin_scope_cleaned_count}, "
        f"custom_roles_altered_count={custom_roles_altered_count}, workspaces={len(workspace_ids_in_kessel)}, "
    )

    # Remove parent relations last so that if another step fails then we are still able to find all the workspaces
    # that need to be processed later.

    orphaned_workspace_parent_count = _remove_orphaned_workspace_parent_relations(
        tenant=tenant,
        workspace_data=kessel_workspace_data,
        commit_removal=commit_removal,
    )

    incorrect_workspace_parent_count = _remove_incorrect_workspace_parent_relations(
        tenant=tenant,
        workspace_data=kessel_workspace_data,
        commit_removal=commit_removal,
    )

    # Return counts only
    return {
        "org_id": tenant.org_id,
        "dry_run": dry_run,
        "ordinary_bindings_altered_count": ordinary_bindings_altered_count,
        "builtin_bindings_scope_cleaned_count": builtin_scope_cleaned_count,
        "custom_v2_roles_altered_count": custom_roles_altered_count,
        "workspaces_discovered_count": len(workspace_ids_in_kessel),
        "orphaned_workspace_relations_cleaned_count": orphaned_workspace_parent_count,
        "stale_parent_workspace_relations_cleaned_count": incorrect_workspace_parent_count,
        "relations_removed_count": removed_count,
    }


def _run_removal_with_retry(tenant: Tenant, read_tuples_fn, dry_run: bool):
    attempts_remaining = 3

    while True:
        try:
            return cleanup_tenant_orphaned_relationships(
                tenant=tenant,
                read_tuples_fn=read_tuples_fn,
                dry_run=dry_run,
            )

        # OperationalError is here based on actual serialization failures we've seen.
        # DeadlockDetected and SerializationFailure are here based on the psycopg code.
        except (OperationalError, DeadlockDetected, SerializationFailure) as e:
            attempts_remaining -= 1

            logger.info(
                f"Got database error while attempting to fix orphans for tenant with org_id={tenant.org_id!r}. "
                f"{attempts_remaining} attempts left.",
                exc_info=True,
            )

            if attempts_remaining <= 0:
                raise


def cleanup_tenant_orphan_bindings(org_id: str, dry_run: bool = False, *, read_tuples_fn=None) -> dict:
    """
    Remove orphaned role binding relationships for a tenant and re-replicate all binding mappings.

    This handles the following types of orphaned relation:
    * Relations for role bindings that are incorrect or no longer exist.
    * Parent relations for workspaces that no longer exist.
    * Incorrect parent relations for workspaces that still exist.

    Args:
        org_id (str): Organization ID for the tenant to clean up
        dry_run (bool): If True, only report counts without making changes
        read_tuples_fn: Function to read tuples from Kessel (same signature as read_tuples_from_kessel)

    Returns:
        dict: Results with cleanup counts and migration results, or error details
    """
    logger.info(f"Cleaning orphaned relationships for tenant org_id={org_id!r} (dry_run={dry_run})")

    # Get tenant
    try:
        tenant = Tenant.objects.get(org_id=org_id)
    except Tenant.DoesNotExist:
        logger.error(f"Tenant {org_id} not found")
        return {"error": f"Tenant {org_id} not found"}

    try:
        # Clean orphaned relationships
        cleanup_result = _run_removal_with_retry(
            tenant=tenant,
            read_tuples_fn=(read_tuples_fn if read_tuples_fn is not None else iterate_tuples_from_kessel),
            dry_run=dry_run,
        )

        migration_result = None

        # Conservatively, always re-replicate all role bindings.
        # (There have been errors with re-replicating in the past, so we always need to re-replicate all bindings in
        # case we don't remove anything but have incorrectly failed to re-replicate in the past.)
        if not dry_run:
            logger.info(f"Running replicate_missing_binding_tuples for tenant with org_id={org_id!r}.")

            rereplicate_result = replicate_missing_binding_tuples(tenant=tenant)

            migration_result = {
                "items_checked": rereplicate_result["bindings_checked"],
                "items_migrated": rereplicate_result["bindings_fixed"],
            }

        result = {
            "cleanup": cleanup_result,
            "migration": migration_result,
        }

        logger.info(f"Cleanup completed for tenant {org_id}")
        return result

    except Exception as e:
        logger.error(f"Error during cleanup for tenant {org_id}: {str(e)}", exc_info=True)
        return {"error": f"Error during cleanup: {str(e)}"}
