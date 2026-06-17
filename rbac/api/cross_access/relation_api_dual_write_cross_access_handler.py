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

from management.atomic_transactions import atomic
from management.group.relation_api_dual_write_subject_handler import RelationApiDualWriteSubjectHandler
from management.models import Workspace
from management.permission.scope_service import ImplicitResourceService, Scope, TenantScopeResources
from management.principal.model import Principal
from management.relation_replicator.relation_replicator import (
    DualWriteException,
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
    WorkspaceEvent,
    WorkspaceEventStream,
)
from management.role.model import BindingMapping, Role
from management.role.v2_model import SeededRoleV2
from management.role_binding.service import CreateBindingRequest, ExcludeSources, RoleBindingService
from management.subject import SubjectType
from management.tenant_mapping.v2_activation import TenantVersion

from api.models import CrossAccountRequest, Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class _LocalReplicator(RelationReplicator):
    _handler: "RelationApiDualWriteCrossAccessHandler"

    def __init__(self, handler: "RelationApiDualWriteCrossAccessHandler"):
        self._handler = handler

    def replicate(self, event: ReplicationEvent):
        self._handler.relations_to_remove.extend(event.remove)
        self._handler.relations_to_add.extend(event.add)

    def replicate_workspace(self, event: WorkspaceEvent, event_stream: WorkspaceEventStream):
        raise NotImplementedError("workspace events not unsupported")


class RelationApiDualWriteCrossAccessHandler(RelationApiDualWriteSubjectHandler):
    """Class to handle Dual Write for cross account access bindings."""

    def __init__(
        self,
        cross_account_request: CrossAccountRequest,
        event_type: ReplicationEventType,
        replicator: Optional[RelationReplicator] = None,
        resource_service: Optional[ImplicitResourceService] = None,
    ):
        """Initialize RelationApiDualWriteCrossAccessHandler."""
        if not self.replication_enabled():
            return

        if resource_service is None:
            resource_service = ImplicitResourceService.from_settings()

        self._resource_service = resource_service

        try:
            self.cross_account_request = cross_account_request

            tenant = Tenant.objects.get(org_id=self.cross_account_request.target_org)
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
            logger.error(
                f"Error initializing RelationApiDualWriteCrossAccessHandler for request id: "
                f"{self.cross_account_request.request_id}"
            )

            raise DualWriteException(e)

    def _replicate(self):
        if not self.replication_enabled():
            return
        try:
            if not set(self.relations_to_add).isdisjoint(self.relations_to_remove):
                # Since we cache the relations to add/remove locally when operating on a V2 tenant, we lose the
                # relative order between them. So, if a relation is both added and removed at different points,
                # we don't know what the final result should be. Since this shouldn't normally happen anyway,
                # just fail.
                raise AssertionError(
                    "Unexpected intersection between relations to add/remove; "
                    f"add: {self.relations_to_add}, remove: {self.relations_to_remove}"
                )

            # Deduplicate relations_to_add to avoid duplicate subject tuples when
            # multiple CARs share the same role/binding
            deduplicated_add = self._deduplicate_subject_relations(self.relations_to_add, handler_name="CAR")

            self._replicator.replicate(
                ReplicationEvent(
                    event_type=self.event_type,
                    info={
                        "user_id": str(self.cross_account_request.user_id),
                        "roles": [role.uuid for role in self.cross_account_request.roles.all()],
                        "target_org": self.cross_account_request.target_org,
                        "org_id": self.cross_account_request.target_org,
                    },
                    partition_key=PartitionKey.byEnvironment(),
                    remove=self.relations_to_remove,
                    add=deduplicated_add,
                ),
            )
        except Exception as e:
            logger.error("Error occurred in cross account replicate event", e)
            raise DualWriteException(e)

    def replicate(self):
        """Replicate generated relations."""
        if not self.replication_enabled():
            return

        self._replicate()

    def _user_id(self) -> str:
        return str(self.cross_account_request.user_id)

    def _source_key(self):
        return self.cross_account_request.source_key()

    def _role_binding_service(self):
        return RoleBindingService(
            tenant=self.tenant,
            replicator=_LocalReplicator(self),
            principal_source=str(self._source_key()),
            allow_external_subjects=True,
            skip_scope_validation=True,
        )

    def _add_car_roles_v1(self, roles: set[Role]):
        self._expect_v1_tenant()

        def add_principal_to_binding(mapping: BindingMapping):
            self.relations_to_add.append(mapping.assign_user_to_bindings(user_id, source_key))

        user_id = self._user_id()
        source_key = self._source_key()

        for role in roles:
            self._update_mapping_for_system_role(
                role,
                scope=(self._resource_service.scope_for_role(role)),
                update_mapping=add_principal_to_binding,
                create_default_mapping_for_system_role=(
                    lambda resource: self._create_default_mapping_for_system_role(
                        system_role=role,
                        resource=resource,
                        users={str(source_key): user_id},
                    )
                ),
            )

    @atomic
    def _add_car_roles_v2(self, roles: set[Role]):
        service = self._role_binding_service()

        scope_resources = TenantScopeResources.for_tenant(self.tenant)
        v1_roles_by_scope: dict[Scope, set[Role]] = {}

        principal = Principal.objects.get(user_id=self._user_id())

        for role in roles:
            v1_roles_by_scope.setdefault(self._resource_service.scope_for_role(role), set()).add(role)

        for scope, v1_roles in v1_roles_by_scope.items():
            resource = scope_resources.resource_for(scope)
            v2_roles = SeededRoleV2.for_v1_roles(v1_roles)

            # We can only provide the type name, so ensure we aren't dropping any meaningful information
            if resource.resource_type[0] != "rbac":
                raise AssertionError(f"Unexpected resource: {resource}")

            service.batch_create(
                [
                    CreateBindingRequest(
                        role_id=str(v2_role.uuid),
                        resource_type=resource.resource_type[1],
                        resource_id=resource.resource_id,
                        subject_type=SubjectType.USER,
                        subject_id=str(principal.uuid),
                    )
                    for v2_role in v2_roles
                ]
            )

    def generate_relations_to_add_roles(self, roles: Iterable[Role]):
        """Generate relations to add roles."""
        if not self.replication_enabled():
            return

        if self._tenant_version == TenantVersion.VERSION_1:
            self._add_car_roles_v1(roles=set(roles))
        elif self._tenant_version == TenantVersion.VERSION_2:
            self._add_car_roles_v2(roles=set(roles))
        else:
            raise ValueError(f"Unexpected tenant version: {self._tenant_version!r}")

    def _remove_car_roles_v1(self, roles: set[Role], suppress_migration: bool):
        """
        Remove roles for a CAR within a V1 tenant.

        Please see the scary comment in RelationApiDualWriteSubjectHandler._update_mapping_for_system_role before
        passing suppress_migration=True.
        """
        self._expect_v1_tenant()

        user_id = self._user_id()
        source_key = self._source_key()

        def remove_principal_from_binding(mapping: BindingMapping):
            removal = mapping.unassign_user_from_bindings(user_id, source=source_key)
            if removal is not None:
                self.relations_to_remove.append(removal)

        for role in roles:
            for scope in Scope:
                self._update_mapping_for_system_role(
                    role,
                    scope=scope,
                    update_mapping=remove_principal_from_binding,
                    create_default_mapping_for_system_role=None,
                    suppress_migration=suppress_migration,
                )

    @atomic
    def _remove_car_roles_v2(self, roles: set[Role]):
        service = self._role_binding_service()
        scope_resources = TenantScopeResources.for_tenant(self.tenant)
        principal = Principal.objects.get(user_id=self._user_id())

        v2_roles_to_remove: set[SeededRoleV2] = SeededRoleV2.for_v1_roles(roles)

        for scope in Scope:
            resource = scope_resources.resource_for(scope)

            if resource.resource_type[0] != "rbac":
                raise AssertionError(f"Unexpected resource: {resource}")

            resource_subject_args = {
                "resource_type": resource.resource_type[1],
                "resource_id": resource.resource_id,
                "subject_type": "user",
                "subject_id": str(principal.uuid),
            }

            existing_bound_principals = list(
                service.get_role_bindings_by_subject(
                    {**resource_subject_args, "exclude_sources": ExcludeSources.INDIRECT}
                )
            )

            if len(existing_bound_principals) == 0:
                # Nothing to remove; we are done for this scope.
                continue

            if len(existing_bound_principals) > 1:
                raise AssertionError(f"Expected to find only one principal, but found: {existing_bound_principals}")

            bound_roles = {entry.binding.role for entry in existing_bound_principals[0].filtered_bindings}
            roles_to_keep = bound_roles - v2_roles_to_remove

            service.update_role_bindings_for_subject(
                **resource_subject_args, role_ids=[str(r.uuid) for r in roles_to_keep]
            )

        # Ensure that we don't accidentally add any new relations (which update_role_bindings_for_subject
        # could theoretically do).
        if len(self.relations_to_add) > 0:
            raise AssertionError(f"Unexpected new relations added in removing CAR: {self.relations_to_add}")

    def generate_relations_to_remove_roles(self, roles: Iterable[Role], *, suppress_v1_migration: bool = False):
        """
        Generate relations to remove roles.

        Please see the scary comment in RelationApiDualWriteSubjectHandler._update_mapping_for_system_role about
        suppress_migration before passing
        suppress_v1_migration=True.
        """
        if not self.replication_enabled():
            return

        if self._tenant_version == TenantVersion.VERSION_1:
            self._remove_car_roles_v1(roles=set(roles), suppress_migration=suppress_v1_migration)
        elif self._tenant_version == TenantVersion.VERSION_2:
            self._remove_car_roles_v2(roles=set(roles))
        else:
            raise ValueError(f"Unexpected tenant version: {self._tenant_version!r}")
