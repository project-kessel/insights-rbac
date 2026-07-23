#
# Copyright 2024 Red Hat, Inc.
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

"""Class to handle Dual Write API related operations."""

import logging
from typing import Callable, Optional
from uuid import uuid4

from django.conf import settings
from management.models import BindingMapping, Role, RoleBinding, Workspace
from management.permission.scope_service import Scope, bound_model_for_scope
from management.relation_replicator.logging_replicator import stringify_spicedb_relationship
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    DualWriteException,
    RelationReplicator,
    ReplicationEventType,
)
from management.role.relation_api_dual_write_handler import RelationApiDualWriteHandler
from management.role.v2_model import SeededRoleV2
from management.tenant_mapping.v2_activation import TenantVersion, lock_tenant_version
from migration_tool.models import V2boundresource, V2role, V2rolebinding

from api.models import Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def _sync_binding_mapping_to_role_binding(binding_mapping: BindingMapping, role_binding: RoleBinding):
    assert role_binding.role.v1_source == binding_mapping.role
    assert role_binding.resource_type == binding_mapping.resource_type_name
    assert role_binding.resource_id == binding_mapping.resource_id

    role_binding.update_groups_by_uuid(binding_mapping.mappings["groups"])
    role_binding.update_principals_by_user_id(binding_mapping.mappings["users"].items())
    role_binding.save()


def _update_role_binding(
    binding_mapping: BindingMapping,
    role_binding: Optional[RoleBinding],
    update_mapping: Callable[[BindingMapping], None],
):
    def _key_attrs_for(mapping: BindingMapping):
        return (
            mapping.mappings["id"],
            mapping.role,
            mapping.resource_type_namespace,
            mapping.resource_type_name,
            mapping.resource_id,
        )

    prior_key = _key_attrs_for(binding_mapping)
    update_mapping(binding_mapping)

    if _key_attrs_for(binding_mapping) != prior_key:
        raise ValueError("Expected ID, role, and resource of role binding not to be updated.")

    if not binding_mapping.role.system:
        if len(binding_mapping.mappings["users"]) > 0:
            raise ValueError("Principal bindings are not supported for custom roles.")

    binding_mapping.save(force_update=True)

    if role_binding is not None:
        _sync_binding_mapping_to_role_binding(binding_mapping, role_binding)


def _get_or_migrate_binding_for_system_role(tenant: Tenant, binding_mapping: BindingMapping) -> RoleBinding:
    """
    Get or create a RoleBinding with values corresponding to the provided BindingMapping.

    The role of the BindingMapping must be a system role. It is assumed that the V2 equivalent of the system role
    already exists.

    If the RoleBinding does not already exist, it is created with the appropriate values from the BindingMapping.
    """
    v1_role: Role = binding_mapping.role

    if not v1_role.system:
        raise ValueError(f"BindingMapping's role must be a system role, but got role {v1_role.id} ({v1_role.name!r})")

    v2_role: SeededRoleV2 = SeededRoleV2.objects.filter(v1_source=v1_role).get()

    role_binding, created = RoleBinding.objects.select_for_update().get_or_create(
        tenant=tenant,
        uuid=binding_mapping.mappings["id"],
        role=v2_role,
        resource_type=binding_mapping.resource_type_name,
        resource_id=binding_mapping.resource_id,
    )

    if created:
        _sync_binding_mapping_to_role_binding(binding_mapping, role_binding)

    return role_binding


class RelationApiDualWriteSubjectHandler:
    """Base class to handle dual write algorithm for bindings to subjects."""

    def __init__(
        self,
        tenant: Tenant,
        root_workspace: Workspace,
        default_workspace: Workspace,
        event_type: ReplicationEventType,
        replicator: Optional[RelationReplicator] = None,
    ):
        """Initialize RelationApiDualWriteSubjectHandler."""
        if not self.replication_enabled():
            return

        try:
            self.relations_to_add = []
            self.relations_to_remove = []

            self.tenant = tenant
            self.root_workspace = root_workspace
            self.default_workspace = default_workspace

            if self.root_workspace.tenant != self.tenant:
                raise ValueError(
                    f"Expected root workspace to be from tenant {self.tenant}, but got {self.root_workspace.tenant}"
                )

            if self.default_workspace.tenant != self.tenant:
                raise ValueError(
                    f"Expected root workspace to be from tenant {self.tenant}, but got {self.default_workspace.tenant}"
                )

            self.event_type = event_type
            self._replicator = replicator if replicator else OutboxReplicator()

            self._tenant_version = lock_tenant_version(self.tenant)
        except Exception as e:
            logger.error(f"Initialization of RelationApiDualWriteSubjectHandler failed: {e}")
            raise DualWriteException(e)

    def replication_enabled(self):
        """Check whether replication enabled."""
        return settings.REPLICATION_TO_RELATION_ENABLED is True

    def _deduplicate_subject_relations(self, relationships, handler_name="Subject"):
        """
        Deduplicate role_binding#subject relationships.

        When generate_relations methods are called multiple times with the same data,
        duplicate subject tuples (role_binding#subject) are created. These are expected
        and should be deduplicated. Other duplicates are bugs and will raise an error.

        Args:
            relationships: List of Relationship objects
            handler_name: Name of the handler for logging (e.g., "Group", "CAR")

        Returns:
            Deduplicated list of Relationship objects
        """
        seen = set()
        deduplicated = []
        subject_duplicates = []

        for rel in relationships:
            key = stringify_spicedb_relationship(rel)

            if key in seen:
                # Check if this is a role_binding#subject tuple
                is_subject_tuple = (
                    rel.resource.type.namespace == "rbac"
                    and rel.resource.type.name == "role_binding"
                    and rel.relation == "subject"
                )

                if is_subject_tuple:
                    # Expected duplicate - generate_relations called multiple times
                    subject_duplicates.append(key)
                    continue
                else:
                    # Unexpected duplicate - this is a bug!
                    raise ValueError(
                        f"Unexpected duplicate relationship in {handler_name} handler: {key}. "
                        "This indicates a bug in tuple generation logic."
                    )

            seen.add(key)
            deduplicated.append(rel)

        if subject_duplicates:
            logger.info(
                f"[{handler_name} Dual Write] Deduplicated {len(subject_duplicates)} subject tuples "
                f"(had {len(relationships)}, kept {len(deduplicated)})"
            )

        return deduplicated

    def _expect_v1_tenant(self):
        if self._tenant_version != TenantVersion.VERSION_1:
            raise RuntimeError("This operation is only supported for V1 tenants.")

    def _create_default_mapping_for_system_role(
        self,
        system_role: Role,
        resource: V2boundresource,
        **subject,
    ) -> BindingMapping:
        """Create default mapping."""
        assert system_role.system is True, "Expected system role. Mappings for custom roles must already be created."

        binding = V2rolebinding(
            str(uuid4()),
            # Assumes same role UUID for V2 system role equivalent.
            V2role.for_system_role(str(system_role.uuid)),
            resource,
            **subject,
        )
        v2_role = SeededRoleV2.objects.filter(uuid=system_role.uuid).first()
        mapping = BindingMapping.for_role_binding(binding, system_role, v2_role=v2_role)
        self.relations_to_add.extend(mapping.as_tuples())
        return mapping

    def _update_mapping_for_role(
        self,
        role: Role,
        scope: Scope,
        update_mapping: Callable[[BindingMapping], None],
        create_default_mapping_for_system_role: Optional[Callable[[V2boundresource], BindingMapping]],
    ):
        """
        Update mapping for role using callbacks based on current state.

        Callbacks are expected to modify [self.relations_to_add] and [self.relations_to_remove].
        This method handles persistence and locking itself.
        """
        if not self.replication_enabled():
            return

        if role.system:
            self._update_mapping_for_system_role(
                role,
                scope=scope,
                update_mapping=update_mapping,
                create_default_mapping_for_system_role=create_default_mapping_for_system_role,
                resource_locked=False,
            )
        else:
            self._update_mappings_for_custom_role(
                role=role,
                update_mapping=update_mapping,
            )

    def _lock_resource_for_update(self, resource: Tenant | Workspace):
        if isinstance(resource, Tenant):
            Tenant.objects.select_for_update().get(pk=resource.pk)
        elif isinstance(resource, Workspace):
            Workspace.objects.select_for_update().get(pk=resource.pk)
        else:
            raise TypeError(f"Unexpected resource: {resource}")

    def _update_mapping_for_system_role(
        self,
        role: Role,
        scope: Scope,
        update_mapping: Callable[[BindingMapping], None],
        create_default_mapping_for_system_role: Optional[Callable[[V2boundresource], BindingMapping]],
        *,
        resource_locked: bool = False,
        suppress_migration: bool = False,
    ):
        self._expect_v1_tenant()

        if role.system is False:
            raise DualWriteException("Expected system role.")

        local_resource = bound_model_for_scope(
            scope=scope,
            tenant=self.tenant,
            root_workspace=self.root_workspace,
            default_workspace=self.default_workspace,
        )

        v2_resource = V2boundresource.for_model(local_resource)

        try:
            # We lock the binding here because we cannot lock the Role for system roles,
            # as they are used platform-wide,
            # and their permissions do not refer to specific resources,
            # so they can be changed concurrently safely.
            mapping = (
                BindingMapping.objects.select_for_update()
                .filter(
                    role=role,
                    resource_type_namespace=v2_resource.resource_type[0],
                    resource_type_name=v2_resource.resource_type[1],
                    resource_id=v2_resource.resource_id,
                )
                .get()
            )

            # If suppress_migration is set, we will not migrate the BindingMapping to a RoleBinding if the
            # RoleBinding does not already exist. There are *very* few situations where this should be done.
            #
            # The particular reason this was added was to support a script that removes cross-account requests for
            # principals that no longer exist. In general, this should probably never be done except when explicitly
            # attempting to resolve an inconsistency.
            if not suppress_migration:
                role_binding = _get_or_migrate_binding_for_system_role(self.tenant, mapping)

                if role_binding is None:
                    raise AssertionError("Should always have RoleBinding after migrating.")
            else:
                role_binding = (
                    RoleBinding.objects.filter(uuid=mapping.mappings["id"])
                    .select_related("role")
                    .select_for_update(of=["self"])
                    .first()
                )

                if role_binding is not None:
                    v1_state = (mapping.resource_type_name, mapping.resource_id, mapping.role_id)
                    v2_state = (role_binding.resource_type, role_binding.resource_id, role_binding.role.v1_source_id)

                    if v1_state != v2_state:
                        raise AssertionError(
                            f"Found mismatch between V1 and V2 state for role binding: "
                            f"v1_state={v1_state!r}, v2_state={v2_state!r}"
                        )

            _update_role_binding(binding_mapping=mapping, role_binding=role_binding, update_mapping=update_mapping)

            if mapping.is_unassigned():
                self.relations_to_remove.extend(mapping.as_tuples())
                mapping.delete()

                if role_binding is not None:
                    assert not role_binding.group_entries.exists()
                    assert not role_binding.principal_entries.exists()

                    role_binding.delete()
        except BindingMapping.DoesNotExist:
            # create_default_mapping_for_system_role is None if e.g. the role is being removed.
            if create_default_mapping_for_system_role is not None:
                if not resource_locked:
                    # Lock the resource to prevent concurrent creation of the same mapping.
                    self._lock_resource_for_update(local_resource)
                    # Recurse in case the workspace was locked by another process
                    # and now the mapping exists.
                    # We need to query and also lock the existing mapping in that case,
                    # just like normal.
                    self._update_mapping_for_system_role(
                        role,
                        scope=scope,
                        update_mapping=update_mapping,
                        create_default_mapping_for_system_role=create_default_mapping_for_system_role,
                        # This prevents infinite recursion.
                        resource_locked=True,
                    )
                else:
                    # The resource is locked, so it's safe to create the mapping.
                    # This method must only be called here,
                    # otherwise we can end up with extra untracked mapping tuples.
                    mapping = create_default_mapping_for_system_role(v2_resource)
                    mapping.save(force_insert=True)

                    _get_or_migrate_binding_for_system_role(self.tenant, mapping)

    def _update_mappings_for_custom_role(
        self, role: Role, update_mapping: Callable[[BindingMapping], None], migrated: bool = False
    ):
        self._expect_v1_tenant()

        # NOTE: The custom Role MUST be locked before this point in Read Committed isolation.
        # There is a risk of write skew here otherwise, in the case that permissions are added
        # to a custom role that currently has no permissions.
        # In that case there would be no bindings to lock.
        # We must lock something to prevent concurrent updates, so we lock the Role.
        # Because custom roles must be locked already by this point,
        # we don't need to lock the binding here.
        #
        # If this assumption is ever changed, then we must be sure to explicitly lock the role below when migrating it.
        mappings: list[BindingMapping] = list(role.binding_mappings.all())
        bindings_by_id: dict[str, RoleBinding] = {
            str(b.uuid): b for b in RoleBinding.objects.select_for_update().filter(role__v1_source=role)
        }

        if not mappings:
            logger.warning(
                "[Dual Write] Binding mappings not found for role(%s): '%s'. "
                "Assuming no current relations exist. "
                "If this is NOT the case, relations are inconsistent!",
                role.uuid,
                role.name,
            )

        # Check for the case where a custom role exists, has BindingMappings, but does not yet have RoleBindings
        # (because it has not been re-migrated since the dual-write code started creating RoleBindings).
        #
        # We use the migrated flag to ensure that we only attempt this once. (There shouldn't be any case where this
        # state persists *after* re-migrating, but we don't want to infinitely recurse if there is a bug. Any such
        # issues will be caught below.)
        if not migrated and (mappings and not bindings_by_id):
            # We need the appropriate RoleBindings to exist so that we can update them below, so migrate the role now
            # in order to create them.
            #
            # We must have already locked the custom role at this point, so we don't need to lock it again here.
            self._migrate_custom_role(role)

            return self._update_mappings_for_custom_role(
                role=role,
                update_mapping=update_mapping,
                migrated=True,
            )

        # If migration is not needed, then we should have the same number of BindingMappings and RoleBindings. (We
        # will implicitly check that the IDs match below by looking up every BindingMapping ID as a RoleBinding
        # UUID.)
        if len(mappings) != len(bindings_by_id):
            raise AssertionError(
                f"BindingMappings and RoleBindings do not match: got {len(mappings)} BindingMappings and "
                f"{len(bindings_by_id)} RoleBindings."
            )

        for mapping in mappings:
            _update_role_binding(
                binding_mapping=mapping,
                role_binding=bindings_by_id[mapping.mappings["id"]],
                update_mapping=update_mapping,
            )

    def _migrate_custom_role(self, role):
        """
        Fully migrate a custom role.

        The custom role must be locked before calling this method.
        """
        if role.system:
            raise ValueError("Expected a custom role.")

        role_handler = RelationApiDualWriteHandler(
            role=role,
            event_type=ReplicationEventType.MIGRATE_CUSTOM_ROLE,
            replicator=self._replicator,
            tenant=self.tenant,
        )

        role_handler.prepare_for_update()
        role_handler.replicate_new_or_updated_role(role)
