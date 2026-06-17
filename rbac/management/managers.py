#
# Copyright 2025 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Model managers."""

from django.db import connection, models


class WorkspaceQuerySet(models.QuerySet):
    """A custom queryset for workspaces."""

    def built_in(self, tenant_id):
        """Return a queryset of built-in workspaces for a tenant."""
        return self.filter(tenant_id=tenant_id, type__in=[self.model.Types.ROOT, self.model.Types.DEFAULT])

    def standard(self, tenant_id):
        """Return the standard workspaces for a tenant."""
        return self.filter(tenant_id=tenant_id, type=self.model.Types.STANDARD)


class WorkspaceManager(models.Manager):
    """A custom manager for workspaces."""

    def _get_tenant_id(self, tenant=None, tenant_id=None):
        """Get the tenant_id from the tenant or tenant_id kwargs."""
        if tenant:
            tenant_id = tenant.id
        if not tenant_id:
            raise ValueError("You must supply either a tenant object or tenant_id value.")

        return tenant_id

    def get_queryset(self):
        """Attach the custom queryset."""
        return WorkspaceQuerySet(self.model, using=self._db)

    def root(self, tenant=None, tenant_id=None):
        """Return the root workspace for a tenant."""
        tenant_id = self._get_tenant_id(tenant=tenant, tenant_id=tenant_id)
        return self.get(tenant_id=tenant_id, type=self.model.Types.ROOT)

    def default(self, tenant=None, tenant_id=None):
        """Return the default workspace for a tenant."""
        tenant_id = self._get_tenant_id(tenant=tenant, tenant_id=tenant_id)
        return self.get(tenant_id=tenant_id, type=self.model.Types.DEFAULT)

    def built_in(self, tenant=None, tenant_id=None):
        """Delegate call to the WorkspaceQuerySet."""
        tenant_id = self._get_tenant_id(tenant=tenant, tenant_id=tenant_id)
        return self.get_queryset().built_in(tenant_id)

    def standard(self, tenant=None, tenant_id=None):
        """Delegate call to the WorkspaceQuerySet."""
        tenant_id = self._get_tenant_id(tenant=tenant, tenant_id=tenant_id)
        return self.get_queryset().standard(tenant_id)

    def exists_for_tenant(self, workspace_id, tenant=None, tenant_id=None):
        """Check if a workspace exists for a tenant."""
        tenant_id = self._get_tenant_id(tenant=tenant, tenant_id=tenant_id)
        return self.filter(id=workspace_id, tenant_id=tenant_id).exists()

    def descendant_ids_with_parents(self, ids, tenant_id):
        """Return the descendant and root workspace IDs based on roots supplied."""
        with connection.cursor() as cursor:
            sql = """
                WITH RECURSIVE descendants AS
                    (SELECT id,
                            parent_id
                    FROM management_workspace
                    WHERE id = ANY(%s::uuid[])
                    AND tenant_id = %s
                    UNION SELECT w.id,
                                 w.parent_id
                    FROM management_workspace w
                    JOIN descendants d ON w.parent_id = d.id
                    WHERE w.tenant_id = %s)
                SELECT DISTINCT id
                FROM descendants
            """
            cursor.execute(sql, [ids, tenant_id, tenant_id])
            rows = cursor.fetchall()

        return [str(row[0]) for row in rows]
