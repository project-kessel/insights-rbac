#
# Copyright 2023 Red Hat, Inc.
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

"""Model for audit logging."""

from django.db import models
from django.shortcuts import get_object_or_404
from django.utils import timezone
from management.group.model import Group
from management.permission.model import PermissionValue
from management.role.model import Role
from management.role.v2_model import RoleV2
from management.workspace.model import Workspace

from api.models import Tenant, TenantAwareModel


class AuditLog(TenantAwareModel):
    """An audit log."""

    GROUP = "group"
    ROLE = "role"
    ROLE_V2 = "role_v2"
    USER = "user"
    PERMISSION = "permission"
    WORKSPACE = "workspace"
    ROLE_BINDING = "role_binding"
    RESOURCE_CHOICES = (
        (GROUP, "Group"),
        (ROLE, "Role"),
        (ROLE_V2, "V2 Role"),
        (USER, "User"),
        (PERMISSION, "Permission"),
        (WORKSPACE, "Workspace"),
        (ROLE_BINDING, "Role Binding"),
    )

    DELETE = "delete"
    ADD = "add"
    EDIT = "edit"
    CREATE = "create"
    REMOVE = "remove"
    ACTION_CHOICES = (
        (DELETE, "Delete"),
        (ADD, "Add"),
        (EDIT, "Edit"),
        (CREATE, "Create"),
        (REMOVE, "Remove"),
    )

    SOURCE_AI_ASSISTANT = "ai_assistant"
    SOURCE_CHOICES = ((SOURCE_AI_ASSISTANT, "AI Assistant"),)

    _model_class_by_resource_type = {
        GROUP: Group,
        ROLE: Role,
        ROLE_V2: RoleV2,
        WORKSPACE: Workspace,
    }

    created = models.DateTimeField(default=timezone.now)
    principal_username = models.TextField(max_length=255, null=False)
    description = models.TextField(max_length=255, null=False)
    resource_type = models.CharField(max_length=32, choices=RESOURCE_CHOICES)
    resource_id = models.IntegerField(null=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    tenant = models.ForeignKey(Tenant, on_delete=models.SET_NULL, null=True)
    resource_uuid = models.UUIDField(null=True)
    secondary_resource_uuid = models.UUIDField(null=True)
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, null=True, blank=True)

    class Meta:
        """Metadata for audit log model."""

        indexes = [
            # Composite index for common query pattern: tenant + created (for ordering)
            models.Index(fields=["tenant", "created"]),
            # Index for filtering by tenant and resource_type
            models.Index(fields=["tenant", "resource_type"]),
            # Index for filtering by tenant and action
            models.Index(fields=["tenant", "action"]),
        ]

    def _apply_source(self, request):
        """Set the source field if the request originated from an MCP tool."""
        if getattr(request, "mcp_source", False):
            self.source = AuditLog.SOURCE_AI_ASSISTANT

    @staticmethod
    def _format_resource_type(resource_type: str) -> str:
        if resource_type == AuditLog.ROLE_V2:
            return "V2 role"

        if resource_type == AuditLog.WORKSPACE:
            return "workspace"

        if resource_type == AuditLog.ROLE_BINDING:
            return "role binding"

        return resource_type

    def get_tenant_id(self, request):
        """Retrieve tenant id from request."""
        tenant_object = get_object_or_404(Tenant, org_id=request._user.org_id)
        return tenant_object.id

    def get_resource_item(self, r_type, request, *args, **kwargs):
        """Find related information (eg, name, id, etc...) for each resource item."""
        model_class = self._model_class_by_resource_type.get(r_type)

        if model_class is not None:
            verify_tenant = self.get_tenant_id(request)

            role_object = get_object_or_404(model_class, name=request.data["name"], tenant=verify_tenant)
            role_object_id = role_object.id
            role_object_name = f"{self._format_resource_type(r_type)}: {role_object.name}"
            role_object_uuid = role_object.uuid

            return role_object_id, role_object_name, role_object_uuid
        else:
            return ValueError("Wrong Resource Type")

    def _v2_role_edited_field(self, resource_name, request, object):
        annotations = []

        if request.data.get("name") != object.name:
            annotations.append("Edited name")

        if request.data.get("description", "") != object.description:
            annotations.append("Edited description")

        request_permissions = request.data.get("permissions")

        if request_permissions is not None:
            if {PermissionValue.from_v2_dict(p) for p in request_permissions} != {
                PermissionValue.parse_v1(p.permission) for p in object.permissions.all()
            }:
                annotations.append("Edited permissions")

        return resource_name + ":\n" + "\n".join(annotations)

    def _workspace_edited_field(self, resource_name, request, object):
        annotations = []

        if "name" in request.data and request.data["name"] != object.name:
            annotations.append("Edited name")

        if "description" in request.data and request.data.get("description", "") != (object.description or ""):
            annotations.append("Edited description")

        if not annotations:
            return resource_name

        return resource_name + ":\n" + "\n".join(annotations)

    def find_edited_field(self, resource, resource_name, request, object):
        """Add additional information when group/role is edited."""
        # We can't fix the usual format because of backwards-compatibility concerns, but we can use a fixed format
        # for V2 role and workspace operations.
        if resource == AuditLog.ROLE_V2:
            return self._v2_role_edited_field(resource_name, request, object)

        if resource == AuditLog.WORKSPACE:
            return self._workspace_edited_field(resource_name, request, object)

        description = resource_name + ": " + "\n "
        if request.data.get("name") != object.name:
            description = description + "Edited name \n"
        if request.data.get("description") != object.description:
            description = description + "Edited description"
        if resource == AuditLog.ROLE:
            if request.data.get("display_name") != object.display_name:
                description = description + "Edited display name \n"
            if request.data.get("access"):
                description = description + "edited access (permissions/resources)"
        return description

    def log_v2(self, request, resource_type, action, resource_uuid, description):
        """Audit Log for v2 resources (workspace, role binding, etc.) that use UUID as primary key."""
        self.principal_username = request.user.username
        self.resource_type = resource_type
        self.resource_uuid = resource_uuid
        self.description = description[:255]
        self.action = action
        self.tenant_id = self.get_tenant_id(request)
        self._apply_source(request)
        super(AuditLog, self).save()

    def log_create(self, request, resource):
        """Audit Log when a role or a group is created."""
        self.principal_username = request.user.username

        self.resource_type = resource

        self.resource_id, resource_name, self.resource_uuid = self.get_resource_item(resource, request)
        self.description = "Created " + resource_name

        self.action = AuditLog.CREATE
        self.tenant_id = self.get_tenant_id(request)
        self._apply_source(request)
        super(AuditLog, self).save()

    def _log_object_action(self, request, resource, object, action, verb):
        """Shared helper for logging create/delete actions using a model instance."""
        self.principal_username = request.user.username
        self.resource_type = resource
        self.resource_id = object.id
        self.resource_uuid = object.uuid
        resource_name = self._format_resource_type(self.resource_type) + ": " + object.name
        self.description = verb + " " + resource_name
        self.action = action
        self.tenant_id = self.get_tenant_id(request)
        self._apply_source(request)
        super(AuditLog, self).save()

    def log_create_from_object(self, request, resource, object):
        """Audit Log when a resource is created, using the object directly instead of request data."""
        self._log_object_action(request, resource, object, AuditLog.CREATE, "Created")

    def log_delete(self, request, resource, object):
        """Audit Log when a role or a group is deleted."""
        self._log_object_action(request, resource, object, AuditLog.DELETE, "Deleted")

    def log_edit(self, request, resource, object):
        """Audit Log when a role or a group is edit."""
        self.principal_username = request.user.username

        self.resource_type = resource
        self.resource_id = object.id
        self.resource_uuid = object.uuid
        resource_name = self._format_resource_type(self.resource_type) + " " + object.name

        more_information = self.find_edited_field(resource, resource_name, request, object)
        self.description = more_information
        self.action = AuditLog.EDIT
        self.tenant_id = self.get_tenant_id(request)
        self._apply_source(request)
        super(AuditLog, self).save()

    def log_group_assignment(
        self, request, resource_type, resource, secondary_resource_object, assigned_resource_type
    ):
        """Audit Log when a role, user/principal, or service account is added to a group."""
        self.principal_username = request.user.username
        self.resource_type = resource_type
        self.resource_id = resource.id
        self.resource_uuid = resource.uuid
        resource_name = "group: " + resource.name

        if assigned_resource_type == "user":
            self.secondary_resource_uuid = secondary_resource_object.uuid
            self.description = (
                f"{assigned_resource_type} {secondary_resource_object.username} added to {resource_name}"
            )

        if assigned_resource_type == "service-account":
            self.secondary_resource_uuid = secondary_resource_object.uuid
            self.description = (
                f"{assigned_resource_type} {secondary_resource_object.username} added to {resource_name}"
            )

        if assigned_resource_type == "role":
            self.secondary_resource_uuid = secondary_resource_object["uuid"]
            self.description = f"{assigned_resource_type} {secondary_resource_object['name']} added to {resource_name}"

        self.action = AuditLog.ADD
        self.tenant_id = self.get_tenant_id(request)
        self._apply_source(request)
        super(AuditLog, self).save()

    def log_group_remove(self, request, resource_type, resource, secondary_resource_object, assigned_resource_type):
        """Audit Log when a role, user/principal, or service account is removed from a group."""
        self.principal_username = request.user.username
        self.resource_type = resource_type
        self.resource_id = resource.id
        self.resource_uuid = resource.uuid
        resource_name = "group: " + resource.name

        if assigned_resource_type == "user":
            self.secondary_resource_uuid = secondary_resource_object.uuid
            self.description = (
                f"{assigned_resource_type} {secondary_resource_object.username} removed from {resource_name}"
            )

        if assigned_resource_type == "service-account":
            self.secondary_resource_uuid = secondary_resource_object.uuid
            self.description = (
                f"{assigned_resource_type} {secondary_resource_object.username} removed from {resource_name}"
            )

        if assigned_resource_type == "role":
            self.secondary_resource_uuid = secondary_resource_object.uuid
            self.description = (
                f"{assigned_resource_type} {secondary_resource_object.name} removed from {resource_name}"
            )

        self.action = AuditLog.REMOVE
        self.tenant_id = self.get_tenant_id(request)
        self._apply_source(request)
        super(AuditLog, self).save()
