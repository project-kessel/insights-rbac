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
from abc import ABC
from typing import Any, Iterable, Optional

from django.conf import settings
from django.db.models import Model
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.models import Workspace
from management.permission.scope_service import ImplicitResourceService, Scope, bound_model_for_scope
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import DualWriteException, PartitionKey
from management.relation_replicator.relation_replicator import RelationReplicator
from management.relation_replicator.relation_replicator import ReplicationEvent
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.relation_replicator.types import RelationTuple
from management.role.model import BindingMapping, Role
from management.role.platform import (
    admin_platform_parent_scope_for_seeded_system_role,
    platform_v2_role_uuid_for,
)
from management.role.relations import (
    deduplicate_role_permission_relationships,
    role_child_relationship,
    role_owner_relationship,
)
from management.role.v2_model import CustomRoleV2
from management.role_binding.model import RoleBinding
from management.tenant_mapping.model import DefaultAccessType
from management.tenant_mapping.v2_activation import assert_v1_write_allowed
from migration_tool.migrate_role import migrate_role, relation_tuples_for_bindings
from migration_tool.models import V2boundresource
from migration_tool.sharedSystemRolesReplicatedRoleBindings import (
    bound_resource_resolver_from_map,
    v1_perm_to_v2_perm,
    with_workspace_scope_inheritance,
)
from migration_tool.utils import create_relationship


from api.models import Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class BaseRelationApiDualWriteHandler(ABC):
    """Base class to handle Dual Write API related operations on roles."""

    _replicator: RelationReplicator
    # TODO: continue factoring common behavior into this base class, and potentially into a higher base class
    # for the general pattern

    _expected_empty_relation_reason = None

    def __init__(self, replicator: Optional[RelationReplicator] = None):
        """Initialize SeedingRelationApiDualWriteHandler."""
        if not self.replication_enabled():
            self._replicator = NoopReplicator()
            return
        self._replicator = replicator if replicator else OutboxReplicator()

    def replication_enabled(self):
        """Check whether replication enabled."""
        return settings.REPLICATION_TO_RELATION_ENABLED is True

    def set_expected_empty_relation_reason_to_replicator(self, reason: str):
        """Set expected empty relation reason to replicator."""
        self._expected_empty_relation_reason = reason


class SeedingRelationApiDualWriteHandler(BaseRelationApiDualWriteHandler):
    """Class to handle Dual Write API related operations specific to the seeding process."""

    _replicator: RelationReplicator
    _current_role_relations: list[RelationTuple]

    _public_tenant: Optional[Tenant] = None

    def __init__(self, role: Role, replicator: Optional[RelationReplicator] = None):
        """Initialize SeedingRelationApiDualWriteHandler."""
        super().__init__(replicator)
        self.implicit_resource_service = ImplicitResourceService.from_settings()
        self.role = role

        if not role.system:
            raise ValueError(
                "SeedingRelationApiDualWriteHandler only supports system roles. "
                "RelationApiDualWriteHandler must be used for custom roles. "
                f"Provided custom role: pk={role.pk!r}."
            )

    def prepare_for_update(self):
        """Generate & store role's current relations."""
        if not self.replication_enabled():
            return
        self._current_role_relations = self._generate_relations_for_role(list_all_possible_scopes_for_removal=True)

    def replicate_update_system_role(self):
        """Replicate update of system role."""
        if not self.replication_enabled():
            return

        self._replicate(
            ReplicationEventType.UPDATE_SYSTEM_ROLE,
            self._create_metadata_from_role(),
            self._current_role_relations,
            self._generate_relations_for_role(),
        )

    def replicate_new_system_role(self):
        """Replicate creation of new system role."""
        if not self.replication_enabled():
            return

        self._replicate(
            ReplicationEventType.CREATE_SYSTEM_ROLE,
            self._create_metadata_from_role(),
            [],
            self._generate_relations_for_role(),
        )

    def replicate_deleted_system_role(self):
        """Replicate deletion of system role."""
        if not self.replication_enabled():
            return

        self._replicate(
            ReplicationEventType.DELETE_SYSTEM_ROLE,
            self._create_metadata_from_role(),
            self._generate_relations_for_role(list_all_possible_scopes_for_removal=True),
            [],
        )

    def _check_create_admin_platform_relation(self, role, role_scope, *, apply_seeded_admin_scope_override: bool):
        """Check system role and create admin and platform system role parent-child relationship."""
        create_relations = []
        if role.admin_default:
            try:
                admin_scope = admin_platform_parent_scope_for_seeded_system_role(
                    role.name,
                    role_scope,
                    apply_override=apply_seeded_admin_scope_override,
                )
                parent_uuid = platform_v2_role_uuid_for(
                    DefaultAccessType.ADMIN,
                    admin_scope,
                    GlobalPolicyIdService.shared(),
                )

                create_parent_child_relationship = role_child_relationship(parent_uuid, self.role.uuid)
                create_relations.append(create_parent_child_relationship)
            except DefaultGroupNotAvailableError:
                # Default groups may not exist yet during seeding, skip parent relationship
                logging.warning(f"Default groups may not exist yet during seeding for admin scope {role_scope}")
                pass

        if role.platform_default:
            try:
                parent_uuid = platform_v2_role_uuid_for(
                    DefaultAccessType.USER,
                    role_scope,
                    GlobalPolicyIdService.shared(),
                )

                create_parent_child_relationship = role_child_relationship(parent_uuid, self.role.uuid)
                create_relations.append(create_parent_child_relationship)
            except DefaultGroupNotAvailableError:
                # Default groups may not exist yet during seeding, skip parent relationship
                logging.warning(f"Default groups may not exist yet during seeding for platform scope {role_scope}")
                pass

        return create_relations

    def _generate_relations_for_role(self, list_all_possible_scopes_for_removal=False) -> list[RelationTuple]:
        """Generate system role permissions."""
        relations = []
        # Gather v1 and v2 permissions for the role
        v2_permissions: list[str] = []

        for access in self.role.access.all():
            v1_perm = access.permission
            v2_perm = v1_perm_to_v2_perm(v1_perm)
            v2_permissions.append(v2_perm)

            # When deleting, generate relationships for all possible scopes
        if list_all_possible_scopes_for_removal is True:
            for scope in Scope:
                relations.extend(
                    self._check_create_admin_platform_relation(
                        self.role, scope, apply_seeded_admin_scope_override=False
                    )
                )
        else:
            binding_scopes = self.implicit_resource_service.binding_scopes_for_role(self.role)
            for scope in binding_scopes:
                relations.extend(
                    self._check_create_admin_platform_relation(
                        self.role, scope, apply_seeded_admin_scope_override=True
                    )
                )

        for permission in v2_permissions:
            relations.append(
                create_relationship(
                    ("rbac", "role"),
                    str(self.role.uuid),
                    ("rbac", "principal"),
                    str("*"),
                    permission,
                )
            )
        return relations

    def _create_metadata_from_role(self) -> dict[str, object]:
        return {"role_uuid": self.role.uuid, "org_id": str(self.role.tenant.org_id)}

    def _replicate(
        self,
        event_type: ReplicationEventType,
        metadata: dict[str, object],
        remove: list[RelationTuple],
        add: list[RelationTuple],
    ):
        if not self.replication_enabled():
            return
        try:
            self._replicator.replicate(
                ReplicationEvent(
                    event_type=event_type,
                    info=metadata,
                    partition_key=PartitionKey.byEnvironment(),
                    remove=remove,
                    add=add,
                ),
            )
        except Exception as e:
            raise DualWriteException(e)


# Here, Any is the type of the model's pk attribute.
def _by_pk[M: Model](models: Iterable[M]) -> dict[Any, M]:
    out = {m.pk: m for m in models}

    if None in out:
        raise ValueError("Cannot have an unsaved model in a dict by primary key.")

    return out


# Here, Any is the type of the model's pk attribute.
def _update_by_pk[M: Model](old_by_pk: dict[Any, M], new: Iterable[M]):
    old_by_pk = dict(old_by_pk)

    for model in new:
        if model.pk is not None:
            old_by_pk.pop(model.pk, None)

        model.save()

    for removed in old_by_pk.values():
        removed.delete()


class RelationApiDualWriteHandler(BaseRelationApiDualWriteHandler):
    """Class to handle Dual Write API related operations."""

    def __init__(
        self,
        role: Role,
        event_type: ReplicationEventType,
        replicator: Optional[RelationReplicator] = None,
        tenant: Optional[Tenant] = None,
    ):
        """
        Initialize RelationApiDualWriteHandler.

        The provided role must be locked with select_for_update() before creating a RelationApiDualWriteHandler
        (except for a role newly created during an uncommitted transaction).
        """
        super().__init__(replicator)

        if role.system:
            raise ValueError(
                "RelationApiDualWriteHandler only supports custom roles. "
                "SeedingRelationApiDualWriteHandler must be used for system roles. "
                f"Provided system role: pk={role.pk!r}."
            )
        if not self.replication_enabled():
            return
        try:
            self.event_type = event_type
            self.role_relations: list[RelationTuple] = []
            self.current_role_relations: list[RelationTuple] = []
            self.role = role
            self.binding_mappings: dict[int, BindingMapping] = {}
            self.role_bindings: dict[int, RoleBinding] = {}
            self.v2_roles: dict[int, CustomRoleV2] = {}

            binding_tenant = tenant if tenant is not None else role.tenant

            if binding_tenant.tenant_name == "public":
                raise DualWriteException(
                    "Cannot bind role to public tenant. "
                    "Expected the role to have non-public tenant, or for a non-public tenant to be provided. "
                    f"Role: {role.uuid} "
                    f"Tenant: {binding_tenant.id}"
                )

            self.tenant = binding_tenant
            self.root_workspace = Workspace.objects.root(tenant=binding_tenant)
            self.default_workspace = Workspace.objects.default(tenant=binding_tenant)

            self.resource_service = ImplicitResourceService.from_settings()

            assert_v1_write_allowed(self.tenant)
        except Exception as e:
            logger.error(f"Failed to initialize RelationApiDualWriteHandler with error: {e}")
            raise DualWriteException(e)

    def prepare_for_update(self):
        """Generate relations from current state of role and UUIDs for v2 role and role binding from database."""
        if not self.replication_enabled():
            return
        try:
            logger.info(
                "[Dual Write] Generate relations from current state of role(%s): '%s'", self.role.uuid, self.role.name
            )

            self.binding_mappings = _by_pk(self.role.binding_mappings.select_for_update())
            self.role_bindings = _by_pk(RoleBinding.objects.filter(role__v1_source=self.role).select_for_update())
            self.v2_roles = _by_pk(self.role.v2_roles.select_for_update())

            if not self.binding_mappings:
                logger.warning(
                    "[Dual Write] Binding mappings not found for role(%s): '%s'. "
                    "Assuming no current relations exist. "
                    "If this is NOT the case, relations are inconsistent!",
                    self.role.uuid,
                    self.role.name,
                )
                return

            self.current_role_relations = relation_tuples_for_bindings(self.binding_mappings.values())

            tenant_resource_id = self.tenant.tenant_resource_id()
            if tenant_resource_id:
                for v2_role in self.v2_roles.values():
                    self.current_role_relations.append(role_owner_relationship(v2_role.uuid, tenant_resource_id))
        except Exception as e:
            logger.error(f"Failed to generated relations for v2 role & role bindings: {e}")
            raise DualWriteException(e)

    def replicate_new_or_updated_role(self, role):
        """Generate replication event to outbox table."""
        if not self.replication_enabled():
            return
        self.role = role
        self._generate_relations_and_mappings_for_role()
        self._replicate()

    def replicate_deleted_role(self):
        """Replicate removal of current role state."""
        if not self.replication_enabled():
            return

        self.binding_mappings = {}
        self.role_bindings = {}
        self.v2_roles = {}

        self._replicate()

    def _replicate(self):
        if not self.replication_enabled():
            return

        if self._expected_empty_relation_reason:
            logger.info(f"[Dual Write] Skipping empty replication event. {self._expected_empty_relation_reason}")
            return

        try:
            self._replicator.replicate(
                ReplicationEvent(
                    event_type=self.event_type,
                    info={
                        "binding_mappings": (self.binding_mappings if self.binding_mappings is not None else None),
                        "v1_role_uuid": str(self.role.uuid),
                        "org_id": str(self.role.tenant.org_id),
                    },
                    partition_key=PartitionKey.byEnvironment(),
                    remove=self.current_role_relations,
                    add=self.role_relations,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to replicate event for role {self.role.name}, UUID :{self.role.uuid}: {e}")
            raise DualWriteException(e)

    def _generate_relations_and_mappings_for_role(self):
        """Generate relations and mappings for a role with new UUIDs for v2 role and role bindings."""
        if not self.replication_enabled():
            return []
        try:
            logger.info("[Dual Write] Generate new relations from role(%s): '%s'", self.role.uuid, self.role.name)

            binding_scopes = self.resource_service.binding_scopes_for_role(self.role)
            resource_map = with_workspace_scope_inheritance(
                {
                    scope: V2boundresource.for_model(
                        bound_model_for_scope(
                            scope=scope,
                            tenant=self.tenant,
                            root_workspace=self.root_workspace,
                            default_workspace=self.default_workspace,
                        )
                    )
                    for scope in binding_scopes
                }
            )

            relations, migrate_result = migrate_role(
                self.role,
                resource_for_scope=bound_resource_resolver_from_map(resource_map),
                current_bindings=self.binding_mappings.values(),
                current_v2_roles=self.v2_roles.values(),
            )

            # Deduplicate role-to-principal permission tuples which are expected when
            # multiple bindings share the same V2role
            relations = deduplicate_role_permission_relationships(relations)

            _update_by_pk(old_by_pk=self.binding_mappings, new=migrate_result.binding_mappings)
            _update_by_pk(old_by_pk=self.role_bindings, new=migrate_result.role_bindings)
            _update_by_pk(old_by_pk=self.v2_roles, new=migrate_result.v2_roles)

            self.role_relations = relations
            self.binding_mappings = _by_pk(migrate_result.binding_mappings)
            self.role_bindings = _by_pk(migrate_result.role_bindings)
            self.v2_roles = _by_pk(migrate_result.v2_roles)

            tenant_resource_id = self.tenant.tenant_resource_id()
            if tenant_resource_id:
                for v2_role in self.v2_roles.values():
                    self.role_relations.append(role_owner_relationship(v2_role.uuid, tenant_resource_id))

            return relations
        except Exception as e:
            logger.error(
                f"Failed to generate relations and mappings for role {self.role.name!r}, UUID: {self.role.uuid}: {e}"
            )
            raise DualWriteException(e)
