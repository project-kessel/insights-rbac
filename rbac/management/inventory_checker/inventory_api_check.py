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

"""Inventory checker class which checks assignments on Inventory API."""

import logging
from collections.abc import Sequence
from typing import List, Optional, Union

from django.conf import settings
from google.protobuf import json_format
from internal.jwt_utils import JWTManager, JWTProvider
from kessel.inventory.v1beta2 import (
    inventory_service_pb2_grpc,
    reporter_reference_pb2,
    resource_reference_pb2,
    subject_reference_pb2,
)
from kessel.inventory.v1beta2.check_request_pb2 import CheckRequest
from management.cache import JWTCache
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.permission.scope_service import ImplicitResourceService, Scope
from management.relation_replicator.types import RelationTuple
from management.role.platform import admin_platform_parent_scope_for_seeded_system_role, platform_v2_role_uuid_for
from management.role.relations import role_child_relationship
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from management.utils import create_client_channel_inventory
from migration_tool.utils import create_relationship

from api.models import Tenant

jwt_cache = JWTCache()
jwt_provider = JWTProvider()
jwt_manager = JWTManager(jwt_provider, jwt_cache)
logger = logging.getLogger(__name__)


def relation_tuple_to_check_request(tuple_obj: RelationTuple) -> CheckRequest:
    """Convert a RelationTuple to a CheckRequest for inventory verification.

    Args:
        tuple_obj: RelationTuple object containing resource, relation, and subject

    Returns:
        CheckRequest object ready for inventory API verification
    """
    return CheckRequest(
        object=resource_reference_pb2.ResourceReference(
            resource_id=tuple_obj.resource.id,
            resource_type=tuple_obj.resource.type.name,
            reporter=reporter_reference_pb2.ReporterReference(type=tuple_obj.resource.type.namespace),
        ),
        relation=tuple_obj.relation,
        subject=subject_reference_pb2.SubjectReference(
            resource=resource_reference_pb2.ResourceReference(
                resource_id=tuple_obj.subject.subject.id,
                resource_type=tuple_obj.subject.subject.type.name,
                reporter=reporter_reference_pb2.ReporterReference(type=tuple_obj.subject.subject.type.namespace),
            ),
            relation=tuple_obj.subject.relation or "",
        ),
    )


class InventoryApiBaseChecker:
    """Base class used for assignment checks on inventory api."""

    def check_inventory_core(self, checks: Union[CheckRequest, List[CheckRequest]]) -> bool:
        """
        Core method to check relation(s) via gRPC.

        Accepts either a single check request or list of check requests.
        """
        if isinstance(checks, CheckRequest):
            checks = [checks]

        with create_client_channel_inventory(settings.INVENTORY_API_SERVER) as channel:
            stub = inventory_service_pb2_grpc.KesselInventoryServiceStub(channel)

            responses = [stub.Check(req) for req in checks]
            return all(self._is_allowed(res) for res in responses)

    def _check_inventory_batch(self, checks: List[CheckRequest]) -> list[bool]:
        """Check multiple relations via a single gRPC channel, returning per-request results."""
        with create_client_channel_inventory(settings.INVENTORY_API_SERVER) as channel:
            stub = inventory_service_pb2_grpc.KesselInventoryServiceStub(channel)
            return [self._is_allowed(stub.Check(req)) for req in checks]

    def _is_allowed(self, response):
        response_dict = json_format.MessageToDict(response)
        return response_dict.get("allowed", "") != "ALLOWED_FALSE"


class GroupPrincipalInventoryChecker(InventoryApiBaseChecker):
    """Subclass to check group principal relations are correct on inventory api."""

    def check_relationships(self, relationships):
        """Check group principal relations are correct. All relationships must belong to the same group."""
        inventory_relation_assignments = {"group_uuid": "", "principal_relations": []}
        if not relationships:
            return inventory_relation_assignments

        check_requests = [relation_tuple_to_check_request(r) for r in relationships]
        results = self._check_inventory_batch(check_requests)

        for r, relation_exists in zip(relationships, results):
            inventory_relation_assignments["group_uuid"] = r.resource.id
            inventory_relation_assignments["principal_relations"].append(
                {"id": r.subject.subject.id, "relation_exists": relation_exists}
            )
            if not relation_exists:
                logger.warning(
                    f"Relation missing: User ID {r.subject.subject.id} "
                    f"is not associated with Group ID {r.resource.id}"
                )
        return inventory_relation_assignments


class BootstrappedTenantInventoryChecker(InventoryApiBaseChecker):
    """Subclass to check bootstrapped tenants are correct on inventory api."""

    _SCOPE_RESOURCE_TYPE: dict[Scope, str] = {
        Scope.DEFAULT: "workspace",
        Scope.ROOT: "workspace",
        Scope.TENANT: "tenant",
    }

    def _build_named_tuples(
        self,
        org_id: str,
        tenant_mapping: TenantMapping,
        root_workspace_id: str,
        default_workspace_id: str,
        ungrouped_workspace_id: Optional[str],
        scope_resource_ids: dict[Scope, str],
        policy_service: GlobalPolicyIdService,
    ) -> list[tuple[str, RelationTuple]]:
        """Build all expected bootstrap tuples with descriptive names."""
        named_tuples: list[tuple[str, RelationTuple]] = []
        tenant_id = scope_resource_ids[Scope.TENANT]

        named_tuples.append(
            (
                "default_workspace_parent",
                create_relationship(
                    ("rbac", "workspace"), default_workspace_id, ("rbac", "workspace"), root_workspace_id, "parent"
                ),
            )
        )
        named_tuples.append(
            (
                "root_workspace_tenant",
                create_relationship(
                    ("rbac", "workspace"),
                    root_workspace_id,
                    ("rbac", "tenant"),
                    Tenant.org_id_to_tenant_resource_id(org_id=org_id),
                    "tenant",
                ),
            )
        )
        named_tuples.append(
            (
                "tenant_platform",
                create_relationship(
                    ("rbac", "tenant"), tenant_id, ("rbac", "platform"), settings.ENV_NAME, "platform"
                ),
            )
        )

        for access_type in DefaultAccessType:
            group_uuid = str(tenant_mapping.group_uuid_for(access_type))
            access_label = access_type.value

            for scope in Scope:
                scope_label = scope.name.lower()
                rb_uuid = str(tenant_mapping.default_role_binding_uuid_for(access_type, scope))
                resource_type = self._SCOPE_RESOURCE_TYPE[scope]
                resource_id = scope_resource_ids[scope]
                role_uuid = str(platform_v2_role_uuid_for(access_type, scope, policy_service))

                named_tuples.append(
                    (
                        f"{access_label}_{scope_label}_binding",
                        create_relationship(
                            ("rbac", resource_type), resource_id, ("rbac", "role_binding"), rb_uuid, "binding"
                        ),
                    )
                )
                named_tuples.append(
                    (
                        f"{access_label}_{scope_label}_role",
                        create_relationship(("rbac", "role_binding"), rb_uuid, ("rbac", "role"), role_uuid, "role"),
                    )
                )
                named_tuples.append(
                    (
                        f"{access_label}_{scope_label}_subject",
                        create_relationship(
                            ("rbac", "role_binding"), rb_uuid, ("rbac", "group"), group_uuid, "subject", "member"
                        ),
                    )
                )

        if ungrouped_workspace_id:
            named_tuples.append(
                (
                    "ungrouped_workspace_parent",
                    create_relationship(
                        ("rbac", "workspace"),
                        ungrouped_workspace_id,
                        ("rbac", "workspace"),
                        default_workspace_id,
                        "parent",
                    ),
                )
            )

        return named_tuples

    @staticmethod
    def _tuple_to_readable(t: RelationTuple) -> str:
        """Format a RelationTuple as a human-readable string."""
        subject_suffix = f"#{t.subject.relation}" if t.subject.relation else ""
        return (
            f"{t.resource.type.namespace}/{t.resource.type.name}:{t.resource.id}"
            f"#{t.relation}"
            f"@{t.subject.subject.type.namespace}/{t.subject.subject.type.name}:{t.subject.subject.id}"
            f"{subject_suffix}"
        )

    def check_bootstrapped_tenant(
        self,
        org_id: str,
        tenant_mapping: TenantMapping,
        root_workspace_id: str,
        default_workspace_id: str,
        ungrouped_workspace_id: Optional[str] = None,
    ) -> tuple[bool, list[dict]]:
        """Check all bootstrap relations for a tenant against inventory.

        Returns:
            Tuple of (all_passed, list of per-check result dicts).
        """
        policy_service = GlobalPolicyIdService.shared()
        tenant_id = f"{settings.PRINCIPAL_USER_DOMAIN}/{org_id}"

        scope_resource_ids: dict[Scope, str] = {
            Scope.DEFAULT: default_workspace_id,
            Scope.ROOT: root_workspace_id,
            Scope.TENANT: tenant_id,
        }

        named_tuples = self._build_named_tuples(
            org_id=org_id,
            tenant_mapping=tenant_mapping,
            root_workspace_id=root_workspace_id,
            default_workspace_id=default_workspace_id,
            ungrouped_workspace_id=ungrouped_workspace_id,
            scope_resource_ids=scope_resource_ids,
            policy_service=policy_service,
        )

        check_list: list[dict] = []
        all_passed = True

        for name, rel_tuple in named_tuples:
            check_request = relation_tuple_to_check_request(rel_tuple)
            exists = self.check_inventory_core(check_request)
            check_list.append(
                {
                    "name": name,
                    "check": self._tuple_to_readable(rel_tuple),
                    "exists": exists,
                }
            )
            if not exists:
                all_passed = False
                logger.warning(f"Bootstrap check failed for {org_id}: {name} missing")

        if all_passed:
            logger.info(f"{org_id} is correctly bootstrapped ({len(check_list)} checks passed).")
        else:
            failed = [c["name"] for c in check_list if not c["exists"]]
            logger.warning(f"{org_id} bootstrap check FAILED. Missing: {failed}")

        return all_passed, check_list


class WorkspaceRelationInventoryChecker(InventoryApiBaseChecker):
    """Subclass to check workspace parent relations are correct on inventory api."""

    def check_workspace_descendants(self, workspace_pairs):
        """Core logic to check workspace descendant relations on inventory api."""
        checks = []
        # Build the check requests for checking parent-child workspace relationship
        for workspace_uuid, workspace_parent_uuid in workspace_pairs:
            check_request = CheckRequest(
                object=resource_reference_pb2.ResourceReference(
                    resource_id=workspace_parent_uuid,
                    resource_type="workspace",
                    reporter=reporter_reference_pb2.ReporterReference(type="rbac"),
                ),
                relation="parent",
                subject=subject_reference_pb2.SubjectReference(
                    resource=resource_reference_pb2.ResourceReference(
                        resource_id=workspace_uuid,
                        resource_type="workspace",
                        reporter=reporter_reference_pb2.ReporterReference(type="rbac"),
                    )
                ),
            )
            checks.append(check_request)
        workspace_check = self.check_inventory_core(checks)
        if not workspace_check:
            logger.warning(f"{workspace_uuid} does not have the expected parent workspace.")
        else:
            logger.info(f"{workspace_uuid} has the correct parent workspace.")
        return workspace_check

    def check_workspace(self, workspace_id, workspace_parent_id):
        """Core logic to check workspace relation on inventory api."""
        # Build the check request for checking parent-child workspace relationship
        check_request = CheckRequest(
            object=resource_reference_pb2.ResourceReference(
                resource_id=workspace_parent_id,
                resource_type="workspace",
                reporter=reporter_reference_pb2.ReporterReference(type="rbac"),
            ),
            relation="parent",
            subject=subject_reference_pb2.SubjectReference(
                resource=resource_reference_pb2.ResourceReference(
                    resource_id=workspace_id,
                    resource_type="workspace",
                    reporter=reporter_reference_pb2.ReporterReference(type="rbac"),
                )
            ),
        )
        workspace_check = self.check_inventory_core(check_request)
        if not workspace_check:
            logger.warning(f"{workspace_id} does not have the expected parent workspace.")
        else:
            logger.info(f"{workspace_id} has the correct parent workspace.")
        return workspace_check


class RoleRelationInventoryChecker(InventoryApiBaseChecker):
    """Subclass to check role relations are correct on inventory api."""

    def check_role(self, role_relations, role_uuid):
        """Core logic to check role V2 relation on inventory api."""
        check_requests = []
        for role_relation in role_relations:
            # Build the check request for each of the relations generated for the role
            check_request = CheckRequest(
                object=resource_reference_pb2.ResourceReference(
                    resource_id=role_relation["resource"]["id"],
                    resource_type=role_relation["resource"]["type"]["name"],
                    reporter=reporter_reference_pb2.ReporterReference(
                        type=role_relation["resource"]["type"]["namespace"]
                    ),
                ),
                relation=role_relation["relation"],
                subject=subject_reference_pb2.SubjectReference(
                    resource=resource_reference_pb2.ResourceReference(
                        resource_id=role_relation["subject"]["subject"]["id"],
                        resource_type=role_relation["subject"]["subject"]["type"]["name"],
                        reporter=reporter_reference_pb2.ReporterReference(
                            type=role_relation["subject"]["subject"]["type"]["namespace"]
                        ),
                    )
                ),
            )
            check_requests.append(check_request)
        role_check = self.check_inventory_core(check_requests)
        if not role_check:
            logger.warning(f"Role: {role_uuid} does not have the expected V2 relations.")
        else:
            logger.info(f"Role: {role_uuid} has the correct V2 relations.")
        return role_check


class RoleBindingInventoryChecker(InventoryApiBaseChecker):
    """Subclass to check role binding relations are correct on inventory api."""

    def check_role_binding(self, binding_tuples: Sequence[RelationTuple], binding_uuid: str) -> bool:
        """Core logic to check role binding relations on inventory api.

        Each role binding produces 3 types of tuples:
        1. resource#binding - the resource has this binding
        2. role_binding#role - the binding is associated with a role
        3. role_binding#subject - the binding has subjects (groups/principals)

        Args:
            binding_tuples: List of RelationTuple objects from RoleBinding.all_tuples()
            binding_uuid: UUID of the role binding being checked

        Returns:
            True if all relations exist in the inventory, False otherwise
        """
        if not binding_tuples:
            return True

        check_requests = [relation_tuple_to_check_request(tuple_obj) for tuple_obj in binding_tuples]

        binding_check = self.check_inventory_core(check_requests)
        if not binding_check:
            logger.warning(f"RoleBinding: {binding_uuid} does not have the expected relations in inventory.")
        else:
            logger.info(f"RoleBinding: {binding_uuid} has the correct relations in inventory.")
        return binding_check


class CrossAccountRequestInventoryChecker(InventoryApiBaseChecker):
    """Subclass to check cross account request relations are correct on inventory api."""

    def check_cross_account_request(self, tuples: Sequence[RelationTuple], request_id: str) -> bool:
        """Core logic to check approved cross account request relations on inventory api.

        An approved CAR produces role binding relations that assign the requesting
        user to roles in the target org's workspaces/tenant.

        Args:
            tuples: List of RelationTuple objects from re-running the CAR dual writer
            request_id: UUID of the cross account request being checked

        Returns:
            True if all relations exist in the inventory, False otherwise
        """
        if not tuples:
            logger.debug(f"CrossAccountRequest: {request_id} has no relations, skipping check")
            return True

        check_requests = [relation_tuple_to_check_request(tuple_obj) for tuple_obj in tuples]

        car_check = self.check_inventory_core(check_requests)
        if not car_check:
            logger.warning(f"CrossAccountRequest: {request_id} does not have the expected relations in inventory.")
        else:
            logger.info(f"CrossAccountRequest: {request_id} has the correct relations in inventory.")
        return car_check


class CustomRolePermissionChecker(InventoryApiBaseChecker):
    """Subclass to check custom role permission relations are correct on inventory api."""

    def check_custom_role_permissions(self, permission_tuples: Sequence[RelationTuple], role_uuid: str) -> bool:
        """Core logic to check custom role permission relations on inventory api.

        Each permission tuple represents: rbac/role:<uuid>#<permission>@rbac/principal:*

        Args:
            permission_tuples: List of RelationTuple objects from CustomRoleV2._permission_tuple()
            role_uuid: UUID of the custom role being checked

        Returns:
            True if all relations exist in the inventory, False otherwise
        """
        if not permission_tuples:
            logger.debug(f"CustomRole: {role_uuid} has no permissions, skipping check")
            return True

        check_requests = [relation_tuple_to_check_request(tuple_obj) for tuple_obj in permission_tuples]

        permission_check = self.check_inventory_core(check_requests)
        if not permission_check:
            logger.warning(f"CustomRole: {role_uuid} does not have the expected permission relations in inventory.")
        else:
            logger.info(f"CustomRole: {role_uuid} has the correct permission relations in inventory.")
        return permission_check


def generate_seeded_role_hierarchy_tuples(
    seeded_role, implicit_resource_service: ImplicitResourceService | None = None
) -> list[RelationTuple]:
    """Generate expected parent-child tuples for a seeded role.

    Replicates the logic from SeedingRelationApiDualWriteHandler._check_create_admin_platform_relation()
    to determine what parent-child relationships should exist in Kessel for a given seeded role.

    Args:
        seeded_role: A SeededRoleV2 instance with v1_source (and v1_source.access) prefetched.
        implicit_resource_service: Optional shared instance to avoid repeated construction in loops.

    Returns:
        List of RelationTuple objects representing expected parent-child relationships.
    """
    v1_role = seeded_role.v1_source
    if v1_role is None:
        logger.warning(f"SeededRole {seeded_role.uuid} has no v1_source, cannot generate hierarchy tuples")
        return []

    if not v1_role.admin_default and not v1_role.platform_default:
        return []

    if implicit_resource_service is None:
        implicit_resource_service = ImplicitResourceService.from_settings()
    scope = implicit_resource_service.scope_for_role(v1_role)
    policy_service = GlobalPolicyIdService.shared()
    tuples = []

    if v1_role.admin_default:
        try:
            admin_scope = admin_platform_parent_scope_for_seeded_system_role(v1_role.name, scope, apply_override=True)
            parent_uuid = platform_v2_role_uuid_for(DefaultAccessType.ADMIN, admin_scope, policy_service)
            tuples.append(role_child_relationship(parent_uuid, seeded_role.uuid))
        except DefaultGroupNotAvailableError:
            logger.warning(f"Default admin group not available for seeded role {seeded_role.uuid}")

    if v1_role.platform_default:
        try:
            parent_uuid = platform_v2_role_uuid_for(DefaultAccessType.USER, scope, policy_service)
            tuples.append(role_child_relationship(parent_uuid, seeded_role.uuid))
        except DefaultGroupNotAvailableError:
            logger.warning(f"Default platform group not available for seeded role {seeded_role.uuid}")

    return tuples


class SeededRoleHierarchyChecker(InventoryApiBaseChecker):
    """Subclass to check seeded role parent-child hierarchy relations on inventory api."""

    def check_seeded_role_hierarchy(self, hierarchy_tuples: Sequence[RelationTuple], role_uuid: str) -> bool:
        """Core logic to check seeded role parent-child relations on inventory api.

        Each hierarchy tuple represents: rbac/role:<parent_platform_uuid>#child@rbac/role:<child_seeded_uuid>

        Args:
            hierarchy_tuples: List of RelationTuple objects from generate_seeded_role_hierarchy_tuples()
            role_uuid: UUID of the seeded role being checked

        Returns:
            True if all relations exist in the inventory, False otherwise
        """
        if not hierarchy_tuples:
            logger.debug(f"SeededRole: {role_uuid} has no parent-child relations, skipping check")
            return True

        check_requests = [relation_tuple_to_check_request(tuple_obj) for tuple_obj in hierarchy_tuples]

        hierarchy_check = self.check_inventory_core(check_requests)
        if not hierarchy_check:
            logger.warning(f"SeededRole: {role_uuid} does not have the expected parent-child relations in inventory.")
        else:
            logger.info(f"SeededRole: {role_uuid} has the correct parent-child relations in inventory.")
        return hierarchy_check
