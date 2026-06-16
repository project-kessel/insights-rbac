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
"""Service for workspace management."""

import itertools
import logging
import uuid
from itertools import groupby
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from feature_flags import FEATURE_FLAGS
from internal.pg_notify_wait import wait_for_pg_notify
from internal.utils import get_workspace_ids_from_resource_definition
from management.atomic_transactions import atomic
from management.models import ResourceDefinition, Role, Workspace
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
)
from management.role.model import BindingMapping
from management.role.relation_api_dual_write_handler import RelationApiDualWriteHandler
from management.role_binding.model import RoleBinding
from management.tenant_mapping.v2_activation import TenantVersion, lock_tenant_version
from management.v2_filters import v2_name_filter
from management.workspace.relation_api_dual_write_workspace_handler import RelationApiDualWriteWorkspaceHandler
from migration_tool.sharedSystemRolesReplicatedRoleBindings import attribute_key_to_v2_related_resource_type
from prometheus_client import Counter, Histogram
from rest_framework import serializers

from api.models import Tenant

# Module-level constants for performance optimization
logger = logging.getLogger(__name__)
READ_YOUR_WRITES_CHANNEL = settings.READ_YOUR_WRITES_CHANNEL


def _generate_ryw_histogram_buckets(timeout_seconds: int) -> tuple:
    """Generate histogram buckets based on the configured timeout.

    Creates buckets at 1%, 2.5%, 5%, 10%, 25%, 50%, 75%, and 100% of the timeout.
    """
    percentages = [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0]
    return tuple(round(timeout_seconds * p, 2) for p in percentages)


# Prometheus metrics for read-your-writes consistency monitoring
ryw_wait_total = Counter(
    "ryw_wait_total",
    "Total number of read-your-writes consistency checks",
    ["result"],
)
ryw_wait_duration_seconds = Histogram(
    "ryw_wait_duration_seconds",
    "Duration of read-your-writes consistency checks in seconds",
    ["result"],
    buckets=_generate_ryw_histogram_buckets(settings.READ_YOUR_WRITES_TIMEOUT_SECONDS),
)


def _record_ryw_metrics(duration: float, result: str) -> None:
    """Record RYW metrics for both counter and histogram.

    Args:
        duration: The duration of the RYW wait in seconds.
        result: The outcome of the wait ('success' or 'timeout').
    """
    ryw_wait_duration_seconds.labels(result=result).observe(duration)
    ryw_wait_total.labels(result=result).inc()


def _update_custom_roles_for_removed_workspace(workspace_id: uuid.UUID, replicator: RelationReplicator) -> int:
    """
    Update roles that reference a removed workspace and replicate the changes.

    This finds custom roles with resource definitions pointing to the removed workspace,
    removes the workspace ID from the resource definitions, and uses the dual write handler
    to update bindings and replicate the changes.

    Args:
        workspace_id (uuid.UUID): The ID of the workspace being removed.

    Returns:
        dict: Results with roles_updated, bindings_updated, and changes list.
    """
    workspace_id_str = str(workspace_id)
    roles_updated = 0
    resource_defs_updated = 0

    logger.info(f"Searching for roles that reference workspace {workspace_id_str}")

    # Get all custom roles with resource definitions
    # Optimize search by filtering for roles that reference this workspace in their RDs
    workspace_filter_key = settings.WORKSPACE_ATTRIBUTE_FILTER
    rds_data = (
        ResourceDefinition.objects.filter(
            Q(
                attributeFilter__operation="equal",
                attributeFilter__value=workspace_id_str,
            )
            | Q(
                attributeFilter__operation="in",
                attributeFilter__value__contains=[workspace_id_str],
            ),
            attributeFilter__key=workspace_filter_key,
        )
        .values("id", "access__role_id")
        .order_by("access__role_id")
    )

    # Group by Role ID
    for role_id, group in groupby(rds_data.iterator(), key=lambda x: x["access__role_id"]):
        # Create a map of RD ID -> metadata for this role's RDs
        rds_metadata = {item["id"]: item for item in group}
        rd_ids = list(rds_metadata.keys())

        role_had_updates = False

        with transaction.atomic():
            # Lock the role to prevent concurrent modifications
            # select_related('tenant') to avoid extra query in DualWriteHandler
            role = Role.objects.select_for_update().select_related("tenant").filter(pk=role_id).first()

            if role is None:
                logger.warning(f"Role vanished before it could be updated: pk={role_id!r}")
                continue

            tenant_version = lock_tenant_version(role.tenant)

            if tenant_version != TenantVersion.VERSION_1:
                logger.info(
                    f"Not updating role (pk={role.pk!r}) that is not in a V1 tenant; "
                    f"only removing orphan BindingMappings."
                )

                # BindingMappings are no longer authoritative in a non-V1 tenant, so we don't need to handle any
                # relations updates. We delete them here only to ensure that we have a clean slate later (since we
                # want to make strong assertions about what BindingMappings exist in
                # _remove_workspace_direct_role_bindings).
                BindingMapping.objects.filter(
                    resource_type_namespace="rbac",
                    resource_type_name="workspace",
                    resource_id=workspace_id_str,
                    role=role,
                ).delete()

                continue

            dual_write = RelationApiDualWriteHandler(
                role, ReplicationEventType.FIX_RESOURCE_DEFINITIONS, replicator=replicator
            )

            dual_write.prepare_for_update()  # Capture current bindings

            # Lock the ResourceDefinitions to prevent concurrent modifications
            # We already know these RDs reference the workspace (from the query),
            # but we need to fetch the full objects to update them.
            locked_rds = ResourceDefinition.objects.select_for_update().filter(id__in=rd_ids)

            rds_to_update = []

            for rd in locked_rds:
                # Double-check that this resource definition is for workspaces.
                if attribute_key_to_v2_related_resource_type(rd.attributeFilter["key"]) != ("rbac", "workspace"):
                    continue

                # Get workspace IDs from resource definition
                workspace_ids = get_workspace_ids_from_resource_definition(rd.attributeFilter)

                # Check if this resource definition references the removed workspace
                if workspace_id not in workspace_ids:
                    continue

                # This resource definition references the removed workspace
                role_had_updates = True

                # Check if the resource definition has None (for ungrouped workspace)
                operation = rd.attributeFilter.get("operation")
                original_value = rd.attributeFilter.get("value")
                has_none_value = False

                if operation == "in" and isinstance(original_value, list):
                    has_none_value = None in original_value
                elif operation == "equal":
                    has_none_value = original_value is None

                # Remove the workspace ID from the list
                remaining_workspace_ids = [ws_id for ws_id in workspace_ids if ws_id != workspace_id]

                # Calculate what the new value should be
                operation_type = rd.attributeFilter.get("operation")
                new_value: str | list | None

                if operation_type == "equal":
                    # For "equal" operation, value should be a single string, None, or empty string
                    # Preserve None if it existed (for ungrouped workspace reference)
                    if has_none_value and not remaining_workspace_ids:
                        new_value = None
                    else:
                        new_value = str(remaining_workspace_ids[0]) if remaining_workspace_ids else ""
                else:
                    # For "in" operation, value should be a list
                    # Preserve None value if it existed (for ungrouped workspace reference)
                    new_value_list: list[str | None] = [str(ws_id) for ws_id in remaining_workspace_ids]
                    if has_none_value:
                        new_value_list.append(None)
                    new_value = new_value_list

                # Update resource definition to remove the workspace ID
                # Create new dict to ensure Django detects the change (JSONField mutation issue)
                updated_filter = rd.attributeFilter.copy()
                updated_filter["value"] = new_value

                rd.attributeFilter = updated_filter
                rds_to_update.append(rd)
                resource_defs_updated += 1

                logger.info(
                    f"Updated role '{role.name}' (uuid={role.uuid}), "
                    f"removed workspace {workspace_id_str}, "
                    f"{original_value} -> {new_value}"
                )

            # Bulk update all RDs for this role
            if rds_to_update:
                ResourceDefinition.objects.bulk_update(rds_to_update, ["attributeFilter"])

            # If we updated any resource definitions, trigger dual write to update bindings
            if role_had_updates:
                roles_updated += 1
                # Refresh role from DB to clear Django ORM's cached related objects
                # This ensures the dual write handler sees the updated resource definitions
                role.refresh_from_db()
                dual_write.replicate_new_or_updated_role(role)  # Update bindings based on new RDs

                logger.info(
                    f"Replicated role updates for '{role.name}' (uuid={role.uuid}) "
                    f"after removing workspace {workspace_id_str}"
                )

    return roles_updated


# This must be SERIALIZABLE because we are potentially interacting with RoleBindings in V2 tenants.
@atomic
def _remove_workspace_direct_role_bindings(workspace: Workspace, replicator: RelationReplicator) -> int:
    """
    Delete role bindings bound a workspace about to be deleted.

    It is assumed that all role bindings bound to custom roles have already been removed.
    """
    tenant_version = lock_tenant_version(workspace.tenant)

    if tenant_version not in (TenantVersion.VERSION_1, TenantVersion.VERSION_2):
        raise RuntimeError(f"Unexpected tenant version: {tenant_version!r}")

    binding_mappings = list(
        BindingMapping.objects.filter(
            resource_type_namespace="rbac",
            resource_type_name="workspace",
            resource_id=str(workspace.id),
        )
    )

    # There shouldn't have been any BindingMappings bound to a system role and a standard workspace created while the
    # tenant was V1. (System roles won't have any relevant resource definitions naming the workspace, and their scope
    # will always put them in the default workspace, root workspace, or the tenant in V1.)
    #
    # If the tenant has been converted to V2, no additional BindingMappings will have been created since that time.
    #
    # Since we have already removed all BindingMappings that came from custom roles in
    # _update_custom_roles_for_removed_workspace (including any from V1 custom roles left over in a V2 tenant!),
    # there shouldn't be any remaining here.
    if len(binding_mappings) > 0:
        raise AssertionError(
            f"A standard workspace should not have any remaining BindingMappings "
            f"(after removing any from custom roles), but found "
            f"BindingMappings with pks={[bm.pk for bm in binding_mappings]}"
        )

    role_bindings = list(
        RoleBinding.objects.filter(
            resource_type="workspace",
            resource_id=str(workspace.id),
        )
        .select_related("role")
        .prefetch_related("group_entries", "principal_entries")
        .select_for_update(of=["self"])
    )

    # In a V1 tenant, there shouldn't be any RoleBindings remaining for a standard workspace for the same reasons
    # as BindingMappings above. (BindingMappings and RoleBindings should always be in sync in a V1 tenant.)
    #
    # In a V2 tenant, we can have RoleBindings that were either directly created in the workspace or that were
    # created for a V1 custom role and not torn down in _update_custom_roles_for_removed_workspace (since that just
    # removes to-be-orphaned BindingMappings in V2 tenants, which aren't authoritative anyway). In either case,
    # we need to actually handle those RoleBindings (including by updating relations).

    if tenant_version == TenantVersion.VERSION_1:
        if len(role_bindings) > 0:
            raise AssertionError(
                f"A standard workspace should not have any remaining RoleBindings "
                f"(after removing any from custom roles), but found "
                f"RoleBindings with pks={[rb.pk for rb in role_bindings]}"
            )

        return 0

    for batch in itertools.batched(itertools.chain.from_iterable(rb.all_tuples() for rb in role_bindings), 1000):
        replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.DELETE_WORKSPACE,
                partition_key=PartitionKey.byEnvironment(),
                remove=list(batch),
                info={"org_id": workspace.tenant.org_id, "workspace_id": str(workspace.id)},
            )
        )

    if len(role_bindings) > 0:
        RoleBinding.objects.filter(pk__in=(rb.pk for rb in role_bindings)).delete()

    return len(role_bindings)


class WorkspaceService:
    """Workspace service."""

    _replicator: RelationReplicator

    def __init__(self, replicator: Optional[RelationReplicator] = None):
        """Create a WorkspaceService that uses the provided replicator (defaulting to OutboxReplicator)."""
        if replicator is None:
            replicator = OutboxReplicator()

        self._replicator = replicator

    def list(self, queryset, params: dict):
        """Apply parameter-based filters to a pre-filtered workspace queryset."""
        ids = params.get("ids")
        type_filter = params.get("type")
        name = params.get("name")
        parent_id = params.get("parent_id")

        if ids:
            queryset = queryset.filter(id__in=ids)
        if type_filter:
            queryset = queryset.filter(type__in=type_filter)
        if name:
            queryset = v2_name_filter(queryset, name)
        if parent_id:
            queryset = queryset.filter(parent_id=parent_id)
        return queryset

    def _dual_write_handler(
        self, workspace: Workspace, event_type: ReplicationEventType
    ) -> RelationApiDualWriteWorkspaceHandler:
        return RelationApiDualWriteWorkspaceHandler(
            workspace=workspace, event_type=event_type, replicator=self._replicator
        )

    @atomic
    def create(self, validated_data: dict, request_tenant: Tenant) -> Workspace:
        """Create workspace."""
        try:
            parent_id = validated_data.get("parent_id")
            if parent_id is None:
                default = Workspace.objects.default(tenant=request_tenant)
                parent_id = default.id
            parent = Workspace.objects.get(id=parent_id)
            self._enforce_hierarchy_depth(parent_id, request_tenant)
            exceeded, workspace_count, max_limit = self._check_total_workspace_count_exceeded(request_tenant)
            if exceeded:
                raise serializers.ValidationError(
                    f"Workspace limit reached ({workspace_count}/{max_limit}); please free up capacity"
                    " by deleting empty workspaces or consolidating those with similar users and role bindings."
                )

            workspace = Workspace.objects.create(**validated_data, tenant=parent.tenant)
            dual_write_handler = self._dual_write_handler(workspace, ReplicationEventType.CREATE_WORKSPACE)
            dual_write_handler.replicate_new_workspace()

            # After the outbox message is created & committed, LISTEN for a NOTIFY

            if FEATURE_FLAGS.is_read_your_writes_workspace_enabled() and settings.REPLICATION_TO_RELATION_ENABLED:
                transaction.on_commit(lambda: self._wait_for_notify_post_commit(workspace.id))

            return workspace
        except ValidationError as e:
            message = e.message_dict
            if hasattr(e, "error_dict") and "__all__" in e.error_dict:
                for error in e.error_dict["__all__"]:
                    for msg in error.messages:
                        if "unique_workspace_name_per_parent" in msg:
                            message = "Can't create workspace with same name within same parent workspace"
                            break
            raise serializers.ValidationError(message)

    def update(self, instance: Workspace, validated_data: dict) -> Workspace:
        """Update workspace."""
        if instance.type in (Workspace.Types.ROOT, Workspace.Types.UNGROUPED_HOSTS):
            raise serializers.ValidationError(f"The {instance.type} workspace cannot be updated.")
        for attr, value in validated_data.items():
            if self._parent_id_attr_update(attr, value, instance):
                raise serializers.ValidationError("Can't update the 'parent_id' on a workspace directly")
            setattr(instance, attr, value)

        # Previously, we didn't replicate the creation of default workspaces, so we have to create them before
        # updating them.
        force_create = instance.type == Workspace.Types.DEFAULT

        try:
            instance.save()
            dual_write_handler = self._dual_write_handler(instance, ReplicationEventType.UPDATE_WORKSPACE)
            dual_write_handler.replicate_updated_workspace(instance.parent, force_create=force_create)
        except ValidationError as e:
            message = e.message_dict
            if hasattr(e, "error_dict") and "__all__" in e.error_dict:
                for error in e.error_dict["__all__"]:
                    for msg in error.messages:
                        if "unique_workspace_name_per_parent" in msg:
                            name = validated_data.get("name")
                            message = f"A workspace with the name '{name}' already exists under same parent."
                            break
            raise serializers.ValidationError(message)
        return instance

    @atomic
    def destroy(self, instance: Workspace) -> None:
        """Destroy workspace."""
        # Lock the workspace to prevent concurrent modifications or referencing
        # by new/updated roles during deletion
        instance = Workspace.objects.select_for_update().get(pk=instance.pk)

        if instance.type != Workspace.Types.STANDARD:
            raise serializers.ValidationError(f"Unable to delete {instance.type} workspace")
        if Workspace.objects.filter(parent=instance, tenant=instance.tenant).exists():
            raise serializers.ValidationError("Unable to delete due to workspace dependencies")

        # Update roles that reference this workspace before deleting it
        roles_updated_count = _update_custom_roles_for_removed_workspace(instance.id, replicator=self._replicator)
        logger.info(f"Updated {roles_updated_count} custom roles for workspace {instance.id} removal")

        system_bindings_removed_count = _remove_workspace_direct_role_bindings(instance, replicator=self._replicator)
        logger.info(f"Removed {system_bindings_removed_count} system roles for workspace {instance.id} removal")

        dual_write_handler = self._dual_write_handler(instance, ReplicationEventType.DELETE_WORKSPACE)
        dual_write_handler.replicate_deleted_workspace()
        instance.delete()

    def move(self, instance: Workspace, target_workspace_id: uuid.UUID) -> Workspace:
        """Move a workspace under new parent."""
        self._prevent_moving_non_standard_workspace(instance)
        self._prevent_moving_workspace_under_own_descendant(target_workspace_id, instance)
        self._enforce_hierarchy_depth(target_workspace_id, instance.tenant)
        self._enforce_hierarchy_depth_for_descendants(target_workspace_id, instance)

        target_workspace = Workspace.objects.get(id=target_workspace_id, tenant=instance.tenant)
        previous_parent_workspace = instance.parent
        instance.parent = target_workspace
        instance.save(update_fields=["parent"])
        dual_write_handler = self._dual_write_handler(instance, ReplicationEventType.MOVE_WORKSPACE)
        dual_write_handler.replicate_updated_workspace(previous_parent_workspace, skip_ws_events=True)
        return instance

    def _enforce_hierarchy_depth(self, target_parent_id: uuid.UUID, tenant: Tenant) -> None:
        """Enforce hierarchy depth limits on workspaces."""
        if self._exceeds_depth_limit(target_parent_id, tenant):
            message = f"Workspaces may only nest {settings.WORKSPACE_HIERARCHY_DEPTH_LIMIT} levels deep."
            error = {"workspace": [message]}
            raise serializers.ValidationError(error)
        if self._violates_peer_restrictions(target_parent_id, tenant):
            message = "Sub-workspaces may only be created under the default workspace."
            error = {"workspace": [message]}
            raise serializers.ValidationError(error)

    def _parent_id_attr_update(self, attr: str, value: str, instance: Workspace) -> bool:
        """Determine if the attribute being updated is parent_id."""
        return attr == "parent_id" and instance.parent_id != value

    def _violates_peer_restrictions(self, target_parent_id: uuid.UUID, tenant: Tenant) -> bool:
        """Determine if peer restrictions are violated."""
        target_root_workspace = Workspace.objects.root(tenant=tenant)
        if settings.WORKSPACE_RESTRICT_DEFAULT_PEERS and str(target_root_workspace.id) == str(target_parent_id):
            return True
        return False

    def _exceeds_depth_limit(self, target_parent_id: uuid.UUID, tenant: Tenant) -> bool:
        """Determine if depth limit is exceeded."""
        target_parent_workspace = Workspace.objects.get(id=target_parent_id, tenant=tenant)
        max_depth_for_workspace = len(target_parent_workspace.ancestors()) + 1
        return max_depth_for_workspace > settings.WORKSPACE_HIERARCHY_DEPTH_LIMIT

    def _check_total_workspace_count_exceeded(self, tenant: Tenant) -> tuple[bool, int, int]:
        """Check if the current org has exceeded the allowed amount of workspaces.

        Returns (exceeded, current_count, max_limit).
        """
        max_limit = settings.WORKSPACE_ORG_CREATION_LIMIT

        workspace_count = Workspace.objects.filter(tenant=tenant, type="standard").count()
        return workspace_count >= max_limit, workspace_count, max_limit

    @staticmethod
    def _enforce_hierarchy_depth_for_descendants(new_parent_id: uuid.UUID, instance: Workspace) -> None:
        """Enforce the hierarchy depth for workspace descendant and target parent workspace."""
        new_parent_depth = Workspace.objects.get(id=new_parent_id).ancestors().count()
        workspace_tree_depth = instance.get_max_descendant_depth()
        total_depth = new_parent_depth + 1 + workspace_tree_depth

        if total_depth > settings.WORKSPACE_HIERARCHY_DEPTH_LIMIT:
            message = (
                f"Cannot move workspace: resulting hierarchy depth ({total_depth}) exceeds limit "
                f"({settings.WORKSPACE_HIERARCHY_DEPTH_LIMIT})."
            )
            raise serializers.ValidationError({"workspace": [message]})

    @staticmethod
    def _prevent_moving_non_standard_workspace(instance: Workspace) -> None:
        """Prevent moving non-standard workspace."""
        if instance.type != Workspace.Types.STANDARD:
            raise serializers.ValidationError({"workspace": "Cannot move non-standard workspace."})

    @staticmethod
    def _prevent_moving_workspace_under_own_descendant(new_parent_id: uuid.UUID, instance: Workspace) -> None:
        """Prevent moving workspace under own descendant."""
        if instance.descendants().filter(id=new_parent_id):
            raise serializers.ValidationError({"parent_id": "Cannot move workspace under one of its own descendants."})

    def _wait_for_notify_post_commit(self, workspace_id: uuid.UUID) -> None:
        """Wait for a NOTIFY on the configured channel for the given workspace id.

        Intended for use as a transaction.on_commit callback.
        """
        timeout_seconds = settings.READ_YOUR_WRITES_TIMEOUT_SECONDS

        if timeout_seconds is None or timeout_seconds <= 0:
            logger.debug(
                "[Service] RYW skipped waiting due to non-positive timeout for channel='%s' workspace_id='%s'",
                READ_YOUR_WRITES_CHANNEL,
                str(workspace_id),
            )
            return

        try:
            wait_for_pg_notify(
                channel=READ_YOUR_WRITES_CHANNEL,
                expected_payload=str(workspace_id),
                timeout_seconds=float(timeout_seconds),
                log_label="[Service] RYW",
                on_success=lambda d: _record_ryw_metrics(d, "success"),
                on_timeout=lambda d: _record_ryw_metrics(d, "timeout"),
                timeout_error_message=(
                    f"Read-your-writes consistency check timed out after {timeout_seconds}s "
                    f"for workspace {workspace_id}"
                ),
            )
        except TimeoutError:
            raise
        except Exception:
            logger.exception("Error while waiting for NOTIFY after workspace create")
            raise
