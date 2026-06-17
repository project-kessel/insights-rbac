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
"""Model for workspace management."""

import uuid_utils.compat as uuid
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import connection, models
from django.db.models import Q, UniqueConstraint
from django.db.models.expressions import RawSQL
from django.db.models.functions import Upper
from django.utils import timezone
from management.managers import WorkspaceManager
from management.rbac_fields import AutoDateTimeField
from rest_framework import serializers

from api.models import TenantAwareModel


class Workspace(TenantAwareModel):
    """A workspace."""

    class SpecialNames:
        DEFAULT = "Default Workspace"
        ROOT = "Root Workspace"
        UNGROUPED_HOSTS = "Ungrouped Hosts"

    class SpecialDescriptions:
        DEFAULT = "The default workspace for your organization"
        ROOT = "The top-level workspace containing all other workspaces"
        UNGROUPED_HOSTS = "System workspace for ungrouped hosts"

    class Types(models.TextChoices):
        STANDARD = "standard"
        DEFAULT = "default"
        ROOT = "root"
        UNGROUPED_HOSTS = "ungrouped-hosts"

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False, unique=True, null=False)
    name = models.CharField(max_length=255, db_index=True)
    parent = models.ForeignKey("self", on_delete=models.PROTECT, related_name="children", null=True, blank=True)
    description = models.CharField(max_length=255, null=True, blank=True, editable=True)
    type = models.CharField(choices=Types.choices, default=Types.STANDARD, null=False, db_index=True, max_length=20)
    created = models.DateTimeField(default=timezone.now)
    modified = AutoDateTimeField(default=timezone.now)

    objects = WorkspaceManager()

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["tenant_id", "type"],
                name="unique_default_root_workspace_per_tenant",
                condition=Q(type__in=["root", "default", "ungrouped-hosts"]),
            ),
            UniqueConstraint(
                Upper("name"),
                "parent",
                name="unique_workspace_name_per_parent",
                condition=Q(parent__isnull=False),
            ),
        ]
        indexes = [
            GinIndex(fields=["name"], name="workspace_name_trgm_idx", opclasses=["gin_trgm_ops"]),
        ]

    def save(self, *args, **kwargs):
        """Override save on model to enforce validations."""
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        """Validate the model."""
        # Auto-set description for system workspaces if not already set
        if not self.description:
            if self.type == self.Types.ROOT:
                self.description = self.SpecialDescriptions.ROOT
            elif self.type == self.Types.DEFAULT:
                self.description = self.SpecialDescriptions.DEFAULT
            elif self.type == self.Types.UNGROUPED_HOSTS:
                self.description = self.SpecialDescriptions.UNGROUPED_HOSTS

        if self.type == self.Types.ROOT:
            if self.parent_id is not None:
                raise serializers.ValidationError({"root_parent": "Root workspace must not have a parent."})
        elif self.parent_id is None:
            if self.type == self.Types.STANDARD:
                workspace_object = Workspace.objects.get(type=self.Types.DEFAULT, tenant=self.tenant_id)
                self.parent = workspace_object
                self.parent_id = workspace_object.id
            else:
                raise ValidationError({"workspace": f"{self.type} workspaces must have a parent workspace."})
        elif self.type == self.Types.DEFAULT and self.parent.type != self.Types.ROOT:
            raise serializers.ValidationError({"default_parent": "Default workspace must have a root parent."})
        elif self.id == self.parent_id:
            raise serializers.ValidationError({"parent_id": "The parent_id and id values must not be the same."})

    def ancestors(self):
        """Return a list of ancestors for a Workspace instance."""
        sql = (
            """
            WITH RECURSIVE ancestors AS
              (SELECT id,
                      parent_id
               FROM management_workspace
               WHERE id = %s
               UNION SELECT w.id,
                                w.parent_id
               FROM management_workspace w
               JOIN ancestors a ON w.id = a.parent_id)
            SELECT id
            FROM ancestors
            WHERE id != %s
        """,
        )
        return Workspace.objects.filter(id__in=RawSQL(sql, [self.id, self.id]))

    def get_max_descendant_depth(self):
        """Get the maximum depth of any descendant workspace."""
        sql = """
                WITH RECURSIVE descendants AS (
                    SELECT id, parent_id, 1 AS depth
                    FROM management_workspace
                    WHERE parent_id = %s

                    UNION ALL

                    SELECT w.id, w.parent_id, d.depth + 1
                    FROM management_workspace w
                    JOIN descendants d ON w.parent_id = d.id
                )
                SELECT COALESCE(MAX(depth), 0) FROM descendants
            """
        with connection.cursor() as cursor:
            cursor.execute(sql, [self.id])
            max_depth = cursor.fetchone()[0]
        return max_depth

    def descendants(self):
        """Return a Queryset of all descendant workspaces."""
        sql = """
            WITH RECURSIVE descendants AS (
                SELECT id, parent_id
                FROM management_workspace
                WHERE parent_id = %s
                UNION
                SELECT w.id, w.parent_id
                FROM management_workspace w
                JOIN descendants d ON w.parent_id = d.id
            )
            SELECT id FROM descendants
        """
        return Workspace.objects.filter(id__in=RawSQL(sql, [self.id]))
