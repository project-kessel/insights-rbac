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
from typing import Iterable, Optional

from management.group.model import Group
from management.group.platform import GlobalPolicyIdService
from management.group.relation_api_dual_write_subject_handler import RelationApiDualWriteSubjectHandler
from management.models import Workspace
from management.permission.scope_service import (
    ImplicitResourceService,
    Scope,
    TenantScopeResources,
)
from management.principal.model import Principal
from management.relation_replicator.relation_replicator import (
    DualWriteException,
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
)
from management.relation_replicator.types import RelationTuple
from management.role.model import BindingMapping, Role
from management.role_binding.model import RoleBinding
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from management.tenant_mapping.v2_activation import TenantVersion
from management.tenant_service.relations import default_role_binding_tuples

from api.models import Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class RelationApiDualWriteGroupHandler(RelationApiDualWriteSubjectHandler):
    """Class to handle Dual Write for group bindings and membership."""

    group: Group
    _expected_empty_relation_reason = None
    _policy_service: GlobalPolicyIdService
    _resource_service: ImplicitResourceService

    def __init__(
        self,
        group,
        event_type: ReplicationEventType,
        replicator: Optional[RelationReplicator] = None,
        resource_service: Optional[ImplicitResourceService] = None,
    ):
        """Initialize RelationApiDualWriteGroupHandler."""
        if not self.replication_enabled():
            return

        try:
            self.group = group
            self.principals = []
            self._platform_default_policy_uuid: Optional[str] = None
            self._public_tenant: Optional[Tenant] = None
            self._tenant_mapping = None
            self._policy_service = GlobalPolicyIdService.shared()

            if resource_service is not None:
                self._resource_service = resource_service
            else:
                self._resource_service = ImplicitResourceService.from_settings()

            tenant = Tenant.objects.get(id=self.group.tenant_id)
            default_workspace = Workspace.objects.default(tenant=tenant)
            root_workspace = Workspace.objects.root(tenant=tenant)

            super().__init__(
                tenant=tenant,
                default_workspace=default_workspace,
                root_workspace=root_workspace,
                event_type=event_type,
                replicator=replicator,
            )
        except Exception as e:
            logger.error(f"Initialization of RelationApiDualWriteGroupHandler failed: {e}")
            raise DualWriteException(e)

    def _generate_member_relations(self):
        """Generate user-groups relations."""
        relations = []
        for principal in self.principals:
            relationship = self.group.relationship_to_principal(principal)
            if relationship is None:
                logger.warning(
                    "[Dual Write] Principal(uuid=%s) does not have user_id. Skipping replication.", principal.uuid
                )
                continue
            relations.append(relationship)

        return relations

    def generate_relations_to_add_principals(self, principals: list[Principal]):
        """Generate relations to add principals."""
        if not self.replication_enabled():
            return
        logger.info("[Dual Write] Generate new relations from Group(%s): '%s'", self.group.uuid, self.group.name)
        self.principals = principals
        self.relations_to_add = self._generate_member_relations()

    def replicate_new_principals(self, principals: list[Principal]):
        """Replicate new principals into group."""
        if not self.replication_enabled():
            return
        logger.info("[Dual Write] Replicate new principals into Group(%s):, '%s'", self.group.uuid, self.group.name)
        self.generate_relations_to_add_principals(principals)
        self._replicate()

    def replicate_removed_principals(self, principals: list[Principal]):
        """Replicate removed principals from group."""
        if not self.replication_enabled():
            return
        logger.info("[Dual Write] Generate new relations from Group(%s): '%s'", self.group.uuid, self.group.name)
        self.principals = principals
        self.relations_to_remove = self._generate_member_relations()

        self._replicate()

    def _replicate(self):
        if not self.replication_enabled():
            return
        if self._expected_empty_relation_reason:
            logger.info(f"[Dual Write] Skipping empty replication event. {self._expected_empty_relation_reason}")
            return
        try:
            # Deduplicate relations_to_add to avoid duplicates when generate_relations
            # is called multiple times with the same data
            deduplicated_add = self._deduplicate_subject_relations(self.relations_to_add, handler_name="Group")

            self._replicator.replicate(
                ReplicationEvent(
                    event_type=self.event_type,
                    info={"group_uuid": str(self.group.uuid), "org_id": str(self.group.tenant.org_id)},
                    partition_key=PartitionKey.byEnvironment(),
                    remove=self.relations_to_remove,
                    add=deduplicated_add,
                ),
            )
        except Exception as e:
            logger.error(f"Replication event failed for group: {self.group.uuid}: {e}")
            raise DualWriteException(e)

    def generate_relations_reset_roles(
        self, roles: Iterable[Role], remove_default_access_from: Optional[TenantMapping] = None
    ):
        """
        Reset the mapping and relationships for the group, assuming this group should only be assigned once.

        This is safe if you are SURE this group should only be assigned once,
        OR you will be re-adding the other sources of assignments.

        This method **IS** idempotent. It will reset the group to the same state every time.
        """
        if not self.replication_enabled():
            return

        def reset_mapping(mapping: BindingMapping):
            to_remove = mapping.unassign_group(str(self.group.uuid))
            if to_remove:
                self.relations_to_remove.append(to_remove)
            to_add = mapping.assign_group_to_bindings(str(self.group.uuid))
            if to_add:
                self.relations_to_add.append(to_add)

        # Go through current roles
        # For each binding
        # Remove all of this subject
        # Replicate this removal
        # Add back subject
        # Replicate this addition
        for role in roles:
            # When a role has mixed scopes including TENANT, create bindings at each scope
            # so that workspace-scoped permissions are not lost.
            binding_scopes = self._resource_service.binding_scopes_for_role(role)

            for scope in binding_scopes:
                self._update_mapping_for_role(
                    role,
                    scope=scope,
                    update_mapping=reset_mapping,
                    create_default_mapping_for_system_role=(
                        lambda resource, system_role=role: self._create_default_mapping_for_system_role(
                            system_role=system_role,
                            resource=resource,
                            groups=frozenset([str(self.group.uuid)]),
                        )
                    ),
                )

        if remove_default_access_from is not None:
            self.relations_to_remove.extend(
                self._default_binding(resource_binding_only=True, mapping=remove_default_access_from)
            )

    def replicate(self):
        """Replicate generated relations."""
        if not self.replication_enabled():
            return

        self._replicate()

    def generate_relations_to_remove_roles(self, roles: Iterable[Role]):
        """Generate relations to removed roles."""
        if not self.replication_enabled():
            return

        for role in roles:
            self._update_mapping_for_role_removal(role)

    def _update_mapping_for_role_removal(self, role: Role):
        def remove_group_from_binding(mapping: BindingMapping):
            removal = mapping.pop_group_from_bindings(str(self.group.uuid))
            if removal is not None:
                self.relations_to_remove.append(removal)

        # There are several cases we have to handle here.
        #
        # First, the scope of the role could have changed since it was initially assigned, since we have no way of
        # knowing what scopes a role previously had.
        #
        # Second, if the role could be bound to a non-default scope, so we have to handle two cases (even if the scope
        # of the role hasn't changed):
        # * An existing role binding that has not been migrated. Here, the role would still be bound in the default
        #   workspace, and we have to remove it from there.
        # * A role binding that has been migrated, or a role binding that was added after scope started being
        #   respected. Here, we have to remove it from the correct scope.
        # We could even have both, if a new role binding is created while scope is being respected but before the old
        # role bindings have been pruned. We have no a priori way to distinguish between these two cases,
        # so we always have to check at least the default workspace and the correct resource.
        #
        # In order to handle all these cases, we always attempt to remove the role from all scopes.
        for scope in Scope:
            self._update_mapping_for_role(
                role,
                scope=scope,
                update_mapping=remove_group_from_binding,
                create_default_mapping_for_system_role=None,
            )

    def prepare_to_delete_group(self, roles):
        """Generate relations to delete.

        When the tenant has been activated for V2 writes, removal tuples are
        derived directly from V2 RoleBinding objects instead of BindingMapping.
        This ensures that bindings created through the V2 role-binding-by-subject
        API (which bypass BindingMapping) are properly cleaned up in SpiceDB.
        """
        if not self.replication_enabled():
            return

        if self._tenant_version == TenantVersion.VERSION_1:
            self._prepare_to_delete_group_v1(roles)
        elif self._tenant_version == TenantVersion.VERSION_2:
            self._prepare_to_delete_group_v2()
        else:
            raise AssertionError(f"Unexpected tenant version: {self._tenant_version}")

        if self.group.platform_default:
            # If we are restoring the default role binding, we need to handle the case where *none* of the
            # relationships exist. This can happen if we bootstrapped a tenant that already had a custom default
            # group (in which case V2TenantBootstrapService will indeed refuse to create a default role binding at all),
            # and now we are removing that custom default group.
            self.relations_to_add.extend(self._default_binding(resource_binding_only=False))
        else:
            self.principals = self.group.principals.all()
            self.relations_to_remove.extend(self._generate_member_relations())

    def _prepare_to_delete_group_v1(self, roles):
        """V1 path: compute removal tuples from BindingMapping."""
        self._expect_v1_tenant()

        system_roles = roles.public_tenant_only()

        # Custom roles are locked to prevent resources from being added/removed concurrently,
        # in the case that the Roles had _no_ resources specified to begin with.
        # This should not be necessary for system roles.
        custom_roles = roles.filter(tenant=self.group.tenant).select_for_update()

        custom_ids = []
        for role in [*system_roles, *custom_roles]:
            if role.id in custom_ids:
                # it was needed to skip distinct clause because distinct doesn't work with select_for_update
                continue
            self._update_mapping_for_role_removal(role)
            custom_ids.append(role.id)

    def _prepare_to_delete_group_v2(self):
        """V2 path: compute removal tuples from RoleBinding objects.

        Queries all RoleBinding objects linked to this group and collects
        their full tuple sets (role, resource-binding, and subject tuples)
        for removal from SpiceDB.
        """
        bindings = (
            RoleBinding.objects.filter(
                group_entries__group=self.group,
                tenant=self.tenant,
            )
            .select_related("role")
            .prefetch_related("group_entries__group", "principal_entries__principal")
        )

        for binding in bindings:
            self.relations_to_remove.append(binding.subject_tuple(self.group))
            if not binding.group_entries.exclude(group=self.group).exists() and not binding.principal_entries.exists():
                self.relations_to_remove.extend(binding.binding_tuples())

    def _default_binding(
        self,
        resource_binding_only: bool,
        mapping: Optional[TenantMapping] = None,
    ) -> list[RelationTuple]:
        """
        Calculate default bindings from tenant mapping.

        resource_binding_only has the same meaning as in default_role_binding_tuples. It should be set to True when
        calculating the tuples to remove. (Note that this occurs when handling the *addition* of a custom default group,
        since that is when the default role binding is unbound.)
        """
        if mapping is None:
            mapping = TenantMapping.objects.get(tenant=self.group.tenant)
        else:
            assert mapping.tenant.id == self.group.tenant_id, "Tenant mapping does not match group tenant."

        return default_role_binding_tuples(
            tenant_mapping=mapping,
            target_resources=TenantScopeResources.for_models(
                tenant=self.group.tenant,
                root_workspace=self.root_workspace,
                default_workspace=self.default_workspace,
            ),
            access_type=DefaultAccessType.USER,
            resource_binding_only=resource_binding_only,
            policy_service=self._policy_service,
        )

    def set_expected_empty_relation_reason(self, reason):
        """Set expected empty relation reason."""
        self._expected_empty_relation_reason = reason
