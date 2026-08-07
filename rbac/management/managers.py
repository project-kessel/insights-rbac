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

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import DatabaseError, connection, models

if TYPE_CHECKING:
    from management.workspace.model import Workspace

logger = logging.getLogger(__name__)


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

    @staticmethod
    def _resolve_org_id(tenant=None) -> str | None:
        """Resolve org_id from a tenant object for cache lookup.

        Returns None when tenant is None, has no org_id attribute, or
        org_id is falsy (e.g. empty string).
        """
        return getattr(tenant, "org_id", None) or None

    def get_queryset(self):
        """Attach the custom queryset."""
        return WorkspaceQuerySet(self.model, using=self._db)

    def _get_cached_built_in_workspace(self, tenant, tenant_id, ws_type: str) -> Workspace:
        """Fetch a built-in workspace, using the cache when an org_id is available.

        Cache write failures are handled gracefully by BasicCache.save() (logged
        and swallowed), so the DB result is always returned even if caching fails.

        :param tenant: Optional Tenant object.
        :param tenant_id: Optional tenant PK (int) or Tenant object.
        :param ws_type: Workspace.Types value (ROOT or DEFAULT).
        :returns: The Workspace instance.
        """
        from management.cache import WORKSPACE_CACHE

        org_id = self._resolve_org_id(tenant=tenant)
        if org_id:
            cached = WORKSPACE_CACHE.get_workspace(org_id, ws_type)
            if cached is not None:
                return cached

        resolved_tenant_id = self._get_tenant_id(tenant=tenant, tenant_id=tenant_id)
        workspace = self.get(tenant_id=resolved_tenant_id, type=ws_type)

        if org_id:
            WORKSPACE_CACHE.cache_workspace(org_id, workspace)

        return workspace

    def root(self, tenant=None, tenant_id=None):
        """Return the root workspace for a tenant, using cache when available."""
        return self._get_cached_built_in_workspace(tenant, tenant_id, self.model.Types.ROOT)

    def default(self, tenant=None, tenant_id=None):
        """Return the default workspace for a tenant, using cache when available."""
        return self._get_cached_built_in_workspace(tenant, tenant_id, self.model.Types.DEFAULT)

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

    def ancestor_ids_for_workspaces(self, ids, tenant_id):
        """Return all ancestor IDs for a batch of workspace IDs in a single CTE query.

        Traverses the workspace tree upward from all given workspace IDs,
        returning the union of all ancestors. Excludes the input workspace IDs
        themselves. Uses a single database round-trip regardless of how many
        workspace IDs are provided.

        This is the upward counterpart to descendant_ids_with_parents().
        """
        if not ids:
            return []
        try:
            with connection.cursor() as cursor:
                sql = """
                    WITH RECURSIVE ancestors AS (
                        SELECT id, parent_id
                        FROM management_workspace
                        WHERE id = ANY(%s::uuid[])
                        AND tenant_id = %s
                        UNION ALL
                        SELECT w.id, w.parent_id
                        FROM management_workspace w
                        JOIN ancestors a ON w.id = a.parent_id
                        WHERE w.tenant_id = %s
                    )
                    SELECT DISTINCT id
                    FROM ancestors
                    WHERE id != ALL(%s::uuid[])
                """
                cursor.execute(sql, [ids, tenant_id, tenant_id, ids])
                rows = cursor.fetchall()
        except DatabaseError:
            logger.warning(
                "DatabaseError in ancestor_ids_for_workspaces; returning empty list",
                exc_info=True,
            )
            return []

        return [str(row[0]) for row in rows]
