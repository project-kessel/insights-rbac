#
# Copyright 2019 Red Hat, Inc.
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

"""Handler for system defined roles."""

import dataclasses
import json
import logging
import os

from core.utils import destructive_ok
from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from internal.migrations.migrate_role_scope import migrate_role_scope_if_changed
from management.atomic_transactions import atomic, atomic_with_retry
from management.group.definer import seed_group
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.notifications.notification_handlers import role_obj_change_notification_handler
from management.permission.model import Permission
from management.permission.scope_service import ImplicitResourceService, Scope
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import Access, ExtRoleRelation, ExtTenant, ResourceDefinition, Role, RoleScopeState
from management.role.platform import (
    admin_platform_parent_scopes_for_seeded_system_role,
    platform_v2_role_uuid_for,
)
from management.role.relation_api_dual_write_handler import (
    RelationApiDualWriteHandler,
    SeedingRelationApiDualWriteHandler,
)
from management.role.v2_model import PlatformRoleV2, SeededRoleV2
from management.tenant_mapping.model import DefaultAccessType

from api.models import Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def _add_ext_relation_if_it_exists(external_relation, role):
    if not external_relation:
        return

    ext_id = external_relation.get("id")
    ext_tenant_name = external_relation.get("tenant")

    if hasattr(role, "ext_relation"):
        pre_relation = role.ext_relation
        # If the role already has same external relation, no-op
        if pre_relation and pre_relation.ext_id == ext_id and pre_relation.ext_tenant.name == ext_tenant_name:
            return
        else:
            pre_relation.delete()

    ext_tenant, created = ExtTenant.objects.get_or_create(name=ext_tenant_name)
    if created:
        logger.info("Created external tenant %s.", ext_tenant_name)

    defaults = dict(ext_tenant=ext_tenant, role=role)
    _, created = ExtRoleRelation.objects.update_or_create(ext_id=ext_id, defaults=defaults)

    logger.info(
        "Added external relationships %s of external tenant %s to role %s.", ext_id, ext_tenant_name, role.name
    )


@dataclasses.dataclass
class _SeedRolesConfig:
    force_create_relationships: bool
    force_update_relationships: bool

    def __post_init__(self):
        if self.force_create_relationships and self.force_update_relationships:
            raise ValueError("force_create_relationships and force_update_relationships cannot both be True")


# We do each operation in a SERIALIZABLE transaction so that other SERIALIZABLE transactions can have a consistent view
# of what system roles exist. Retry on serialization failures since concurrent seeding can conflict.
@atomic_with_retry(retries=3)
def _make_role(data, config: _SeedRolesConfig, platform_roles=None, resource_service=None) -> Role:
    """Create the role object in the database."""
    public_tenant = Tenant.objects.get(tenant_name="public")
    name = data.get("name")
    display_name = data.get("display_name", name)
    access_list = data.get("access")
    defaults = dict(
        description=data.get("description", None),
        system=True,
        version=data.get("version", 1),
        platform_default=data.get("platform_default", False),
        admin_default=data.get("admin_default", False),
    )
    role, created = Role.objects.get_or_create(name=name, defaults=defaults, tenant=public_tenant)
    updated = False

    dual_write_handler = SeedingRelationApiDualWriteHandler(role)
    if created:
        if role.display_name != display_name:
            role.display_name = display_name
            role.save()
        logger.info("Created system role %s.", name)
        role_obj_change_notification_handler(role, "created")
    else:
        if config.force_update_relationships or (role.version != defaults["version"]):
            updated = True
            dual_write_handler.prepare_for_update()
            Role.objects.public_tenant_only().filter(name=name).update(
                **defaults, display_name=display_name, modified=timezone.now()
            )
            role.refresh_from_db()
            logger.info("Updated system role %s.", name)
            role.access.all().delete()
            role_obj_change_notification_handler(role, "updated")
        else:
            if config.force_create_relationships:
                dual_write_handler.replicate_new_system_role()
                logger.info("Replicated system role %s", name)
            else:
                logger.info("No change in system role %s", name)
            # Still seed V2 role even if V1 unchanged
            _seed_v2_role_from_v1(
                role, display_name, defaults["description"], public_tenant, platform_roles, resource_service
            )
            return role

    if access_list:  # Allow external roles to have none access object
        for access_item in access_list:
            resource_def_list = access_item.pop("resourceDefinitions", [])
            permission, _ = Permission.objects.get_or_create(**access_item, tenant=public_tenant)

            access_obj = Access.objects.create(permission=permission, role=role, tenant=public_tenant)
            for resource_def_item in resource_def_list:
                ResourceDefinition.objects.create(**resource_def_item, access=access_obj, tenant=public_tenant)

    _add_ext_relation_if_it_exists(data.get("external"), role)

    assert not (created and updated)

    if created:
        dual_write_handler.replicate_new_system_role()
    elif updated:
        dual_write_handler.replicate_update_system_role()

    _seed_v2_role_from_v1(role, display_name, defaults["description"], public_tenant, platform_roles, resource_service)

    return role


def _update_or_create_roles(roles, config: _SeedRolesConfig, platform_roles=None, resource_service=None) -> list[Role]:
    """Update or create roles from list.

    This function uses all-or-nothing semantics: if any role fails to seed after retries,
    the entire seeding operation fails. This prevents inconsistent state where V1 roles
    exist without their corresponding V2 SeededRoleV2 records.
    """
    current_roles: list[Role] = list()
    # Sort roles by name to ensure consistent lock ordering and prevent deadlocks
    sorted_roles = sorted(roles, key=lambda r: r.get("name", ""))
    for role_json in sorted_roles:
        try:
            role = _make_role(role_json, config, platform_roles, resource_service)
            current_roles.append(role)
        except Exception as e:
            logger.error(f'Failed to update or create system role: {role_json.get("name")} with error: {e}')
            raise
    return current_roles


# SERIALIZABLE for the same reason as _make_role above.
@atomic
def _do_delete_system_roles(roles: QuerySet):
    logger.info(f"Removing the following role(s): {roles.values()}")

    for role in roles:
        dual_write_handler = SeedingRelationApiDualWriteHandler(role)
        dual_write_handler.replicate_deleted_system_role()

    roles.delete()


def seed_roles(force_create_relationships=False, force_update_relationships=False):
    """Update or create system defined roles."""
    roles_directory = os.path.join(settings.BASE_DIR, "management", "role", "definitions")
    role_files = [
        f
        for f in os.listdir(roles_directory)
        if os.path.isfile(os.path.join(roles_directory, f)) and f.endswith(".json")
    ]
    current_roles: list[Role] = list()

    platform_roles = _seed_platform_roles()
    resource_service = ImplicitResourceService.from_settings()
    for role_file_name in role_files:
        role_file_path = os.path.join(roles_directory, role_file_name)
        with open(role_file_path) as json_file:
            data = json.load(json_file)
            role_list = data.get("roles")
            file_roles = _update_or_create_roles(
                role_list,
                _SeedRolesConfig(
                    force_create_relationships=force_create_relationships,
                    force_update_relationships=force_update_relationships,
                ),
                platform_roles,
                resource_service,
            )
            current_roles.extend(file_roles)

    # Find roles in DB but not in config
    roles_to_delete = Role.objects.public_tenant_only().exclude(id__in={r.id for r in current_roles})
    logger.info(f"The following '{roles_to_delete.count()}' roles(s) eligible for removal: {roles_to_delete.values()}")

    if destructive_ok("seeding"):
        # Actually remove roles no longer in config.
        # We must use all() to ensure we actually load the roles within the transaction.
        _do_delete_system_roles(roles_to_delete.all())

    if settings.AUTOMATIC_SCOPE_MIGRATION_ENABLED:
        for role in current_roles:
            migrate_role_scope_if_changed(role)


def seed_permissions():
    """Update or create defined permissions."""
    public_tenant = Tenant.objects.get(tenant_name="public")

    permission_directory = os.path.join(settings.BASE_DIR, "management", "role", "permissions")
    permission_files = [
        f
        for f in os.listdir(permission_directory)
        if os.path.isfile(os.path.join(permission_directory, f)) and f.endswith(".json")
    ]
    current_permission_ids = set()

    for permission_file_name in permission_files:
        permission_file_path = os.path.join(permission_directory, permission_file_name)
        app_name = os.path.splitext(permission_file_name)[0]
        with open(permission_file_path) as json_file:
            data = json.load(json_file)
            for resource, operation_objects in data.items():
                try:
                    with transaction.atomic():
                        for operation_object in operation_objects:
                            # There are some old configs, e.g., cost-management still stay in CI
                            if not isinstance(operation_object, str):
                                permission_description = operation_object.get("description", "")
                                operation = operation_object.get("verb")
                                permission, created = Permission.objects.update_or_create(
                                    permission=f"{app_name}:{resource}:{operation}",
                                    defaults={"description": permission_description},
                                    tenant=public_tenant,
                                )
                            else:
                                permission, created = Permission.objects.update_or_create(
                                    permission=f"{app_name}:{resource}:{operation_object}", tenant=public_tenant
                                )
                            if created:
                                logger.info(f"Created permission {permission.permission}.")
                            current_permission_ids.add(permission.id)

                        # need to iterate over the objects with requirements AFTER all perms are created
                        operation_objects_with_requires = [obj for obj in operation_objects if "requires" in obj]
                        for operation_object in operation_objects_with_requires:
                            if not isinstance(operation_object, str):
                                required_verbs = operation_object.get("requires")
                                verb = operation_object.get("verb")
                                permission = Permission.objects.get(permission=f"{app_name}:{resource}:{verb}")
                                required_permissions = Permission.objects.filter(
                                    application=app_name, resource_type=resource, verb__in=required_verbs
                                ).exclude(id=permission.id)
                                permission.permissions.add(*required_permissions)
                except Exception as e:
                    logger.error(
                        f"Failed to update or create permissions for: " f"{app_name}:{resource} with error: {e}"
                    )
    # Find perms in DB but not in config
    perms_to_delete = Permission.objects.exclude(id__in=current_permission_ids)
    logger.info(
        f"The following '{perms_to_delete.count()}' permission(s) eligible for removal: {perms_to_delete.values()}"
    )
    if destructive_ok("seeding"):
        logger.info(f"Removing the following permissions(s): {perms_to_delete.values()}")
        # Actually remove perms no longer in DB
        with transaction.atomic():
            for permission in perms_to_delete:
                delete_permission(permission)


def delete_permission(permission: Permission):
    """Delete a permission and handles relations cleanning."""
    roles = Role.objects.filter(access__permission=permission).distinct()
    dual_write_handlers = []
    for role in roles:
        role = Role.objects.filter(id=role.id).select_for_update().get()
        dual_write_handler = (
            SeedingRelationApiDualWriteHandler(role=role)
            if role.system
            else RelationApiDualWriteHandler(role, ReplicationEventType.UPDATE_CUSTOM_ROLE)
        )
        dual_write_handler.prepare_for_update()
        dual_write_handlers.append(dual_write_handler)
    permission.delete()
    for dual_write_handler in dual_write_handlers:
        role = dual_write_handler.role
        if isinstance(dual_write_handler, SeedingRelationApiDualWriteHandler):
            dual_write_handler.replicate_update_system_role()
        else:
            dual_write_handler.replicate_new_or_updated_role(role)


def _create_single_platform_role(access_type, scope, policy_service, public_tenant):
    """Create a single platform role with the given parameters."""
    uuid = platform_v2_role_uuid_for(access_type, scope, policy_service)

    role_name = f"{access_type.value.capitalize()} {scope.name.lower()} Platform Role"
    description = f"Platform default role for {access_type.value} access at {scope.name.lower()} scope"

    platform_role, created = PlatformRoleV2.objects.update_or_create(
        uuid=uuid,
        defaults={
            "name": role_name,
            "description": description,
            "tenant": public_tenant,
        },
    )

    if created:
        logger.info("Created platform role: %s", role_name)
    else:
        logger.info("Updated platform role: %s", role_name)

    return platform_role


def _seed_v2_role_from_v1(v1_role, display_name, description, public_tenant, platform_roles, resource_service):
    """Create or update V2 role from V1 role during seeding.

    Raises on failure so the caller's transaction rolls back, ensuring V1 and V2 roles
    are created/updated atomically.
    """
    try:
        # Check what scopes this role is bound at in V1 tenants to detect scope changes
        # IMPORTANT: Check old scopes BEFORE updating the role or clearing permissions
        v2_role, v2_created = SeededRoleV2.objects.update_or_create(
            uuid=v1_role.uuid,
            defaults={
                "name": display_name,
                "description": description,
                "tenant": public_tenant,
                "v1_source": v1_role,
            },
        )
        if v2_created:
            logger.info("Created V2 system role %s.", display_name)
        else:
            logger.info("Updated V2 system role %s.", display_name)
        v2_role.permissions.clear()
        v1_permissions = [access.permission for access in v1_role.access.all()]
        if v1_permissions:
            v2_role.permissions.set(v1_permissions)
            logger.info("Added %d permissions to V2 role %s.", len(v1_permissions), display_name)

        binding_scopes = resource_service.binding_scopes_for_role(v1_role)

        # Clear parents first since scope may have changed since previous seeding
        v2_role.parents.clear()

        if v1_role.platform_default:
            for scope in binding_scopes:
                platform_role = platform_roles[(DefaultAccessType.USER, scope)]
                platform_role.children.add(v2_role)
                logger.info("Added %s as child of platform role %s", display_name, platform_role.name)

        if v1_role.admin_default:
            for scope in admin_platform_parent_scopes_for_seeded_system_role(v1_role.name, binding_scopes):
                admin_platform_role = platform_roles[(DefaultAccessType.ADMIN, scope)]
                admin_platform_role.children.add(v2_role)
                logger.info("Added %s as child of admin platform role %s", display_name, admin_platform_role.name)

        existing_scope_state: RoleScopeState = RoleScopeState.objects.filter(role=v1_role).first()

        # By updating the RoleScopeState in the same transaction as which we update the role, we ensure that we can
        # correctly detect *all* changes to the role's scopes (including those caused by settings changes rather than
        # changes to the role itself).
        if existing_scope_state is not None:
            if set(existing_scope_state.computed_scopes) != set(binding_scopes):
                # We don't need to worry about updates being lost here, since we will only update this in a
                # SERIALIZABLE transaction.
                existing_scope_state.version = existing_scope_state.version + 1
                existing_scope_state.computed_scopes = list(binding_scopes)
                existing_scope_state.migrated = False
                existing_scope_state.save()
        else:
            RoleScopeState.objects.create(
                role=v1_role, version=0, computed_scopes=list(binding_scopes), migrated=False
            )

        return v2_role
    except Exception:
        logger.error("Failed to seed V2 role for %s", display_name, exc_info=True)
        raise


def _seed_platform_roles():
    """Create the 6 platform roles (3 scopes × 2 access types).

    Raises RuntimeError if not all platform roles could be created.
    """
    public_tenant = Tenant.objects.get(tenant_name="public")
    policy_service = GlobalPolicyIdService.shared()

    platform_roles = {}

    for access_type in DefaultAccessType:
        for scope in Scope:
            try:
                platform_role = _create_single_platform_role(access_type, scope, policy_service, public_tenant)
                platform_roles[(access_type, scope)] = platform_role
            except DefaultGroupNotAvailableError:
                logger.warning(
                    f"Default groups do not exist yet. Creating them now for "
                    f"{access_type.value} {scope.name.lower()} scope",
                )
                # Create the default groups
                seed_group()

                policy_service = GlobalPolicyIdService.shared()
                platform_role = _create_single_platform_role(access_type, scope, policy_service, public_tenant)
                platform_roles[(access_type, scope)] = platform_role

    if len(platform_roles) != 6:
        raise RuntimeError(f"Expected 6 platform roles, got {len(platform_roles)}")

    logger.info(f"Successfully seeded {len(platform_roles)} platform roles.")
    return platform_roles
