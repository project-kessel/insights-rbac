#
# Copyright 2026 Red Hat, Inc.
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
"""Tests for GIN trigram indexes on RoleV2 and Workspace name columns."""

from django.db import connection
from django.test import TestCase

from api.models import Tenant
from management.models import Permission, RoleV2, Workspace


class GinTrigramIndexTests(TestCase):
    """Verify that GIN trigram indexes exist and case-insensitive queries work correctly."""

    @classmethod
    def setUpTestData(cls):
        """Create shared test data."""
        cls.tenant = Tenant.objects.create(
            tenant_name="acct_index_test",
            account_id="9999990",
            org_id="900000",
            ready=True,
        )

    def _get_indexes_for_table(self, table_name):
        """Return a dict of {index_name: index_definition} for the given table."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE tablename = %s
                """,
                [table_name],
            )
            return {row[0]: row[1] for row in cursor.fetchall()}

    def test_rolev2_name_gin_trgm_index_exists(self):
        """The rolev2_name_trgm_idx GIN trigram index must exist on management_rolev2."""
        indexes = self._get_indexes_for_table("management_rolev2")
        self.assertIn("rolev2_name_trgm_idx", indexes)
        idx_def = indexes["rolev2_name_trgm_idx"].lower()
        self.assertIn("gin", idx_def)
        self.assertIn("gin_trgm_ops", idx_def)
        self.assertIn("management_rolev2", idx_def)
        self.assertIn("name", idx_def)

    def test_workspace_name_gin_trgm_index_exists(self):
        """The workspace_name_trgm_idx GIN trigram index must exist on management_workspace."""
        indexes = self._get_indexes_for_table("management_workspace")
        self.assertIn("workspace_name_trgm_idx", indexes)
        idx_def = indexes["workspace_name_trgm_idx"].lower()
        self.assertIn("gin", idx_def)
        self.assertIn("gin_trgm_ops", idx_def)
        self.assertIn("management_workspace", idx_def)
        self.assertIn("name", idx_def)

    def test_pg_trgm_extension_is_enabled(self):
        """The pg_trgm extension must be installed in the database."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            result = cursor.fetchone()
        self.assertIsNotNone(result, "pg_trgm extension is not installed")

    def test_rolev2_iexact_query_returns_correct_results(self):
        """Case-insensitive exact match on RoleV2.name works correctly with the index."""
        RoleV2.objects.create(name="Test Role Alpha", tenant=self.tenant)
        RoleV2.objects.create(name="Other Role Beta", tenant=self.tenant)

        results = RoleV2.objects.filter(name__iexact="test role alpha")
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first().name, "Test Role Alpha")

    def test_rolev2_iregex_query_returns_correct_results(self):
        """Case-insensitive regex match on RoleV2.name works correctly with the index."""
        RoleV2.objects.create(name="Admin Role", tenant=self.tenant)
        RoleV2.objects.create(name="User Role", tenant=self.tenant)
        RoleV2.objects.create(name="Viewer", tenant=self.tenant)

        results = RoleV2.objects.filter(name__iregex=r"^.*role$")
        self.assertCountEqual(results.values_list("name", flat=True), ["Admin Role", "User Role"])

    def test_workspace_icontains_query_returns_correct_results(self):
        """Case-insensitive contains match on Workspace.name works correctly with the index."""
        root = Workspace.objects.create(name="Root Workspace", type=Workspace.Types.ROOT, tenant=self.tenant)
        default = Workspace.objects.create(
            name="Default Workspace", type=Workspace.Types.DEFAULT, parent=root, tenant=self.tenant
        )
        Workspace.objects.create(
            name="Production Env", type=Workspace.Types.STANDARD, parent=default, tenant=self.tenant
        )
        Workspace.objects.create(name="Staging Env", type=Workspace.Types.STANDARD, parent=default, tenant=self.tenant)
        Workspace.objects.create(name="Development", type=Workspace.Types.STANDARD, parent=default, tenant=self.tenant)

        results = Workspace.objects.filter(name__icontains="env")
        self.assertEqual(results.count(), 2)

        results_upper = Workspace.objects.filter(name__icontains="ENV")
        self.assertEqual(results_upper.count(), 2)

    def test_rolev2_model_meta_has_gin_index(self):
        """RoleV2 model Meta.indexes includes the GIN trigram index definition."""
        index_names = [idx.name for idx in RoleV2._meta.indexes]
        self.assertIn("rolev2_name_trgm_idx", index_names)

    def test_workspace_model_meta_has_gin_index(self):
        """Workspace model Meta.indexes includes the GIN trigram index definition."""
        index_names = [idx.name for idx in Workspace._meta.indexes]
        self.assertIn("workspace_name_trgm_idx", index_names)
