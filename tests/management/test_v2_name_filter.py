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
"""Test the shared v2_name_filter utility."""

from django.test import TestCase
from management.v2_filters import v2_name_filter
from management.workspace.model import Workspace

from api.models import Tenant


class V2NameFilterTest(TestCase):
    """Test v2_name_filter with real ORM queries."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = Tenant.objects.get_or_create(
            tenant_name="acct_filter_test",
            defaults={"account_id": "filter_test", "org_id": "filter_org", "ready": True},
        )

    def setUp(self):
        super().setUp()
        self.root = Workspace.objects.create(name="Root", tenant=self.tenant, type=Workspace.Types.ROOT)
        self.ws1 = Workspace.objects.create(
            name="Sales Team Alpha", tenant=self.tenant, type="standard", parent=self.root
        )
        self.ws2 = Workspace.objects.create(
            name="Sales Team Beta", tenant=self.tenant, type="standard", parent=self.root
        )
        self.ws3 = Workspace.objects.create(
            name="Engineering Squad", tenant=self.tenant, type="standard", parent=self.root
        )
        self.qs = Workspace.objects.filter(tenant=self.tenant, type="standard")

    def test_exact_match(self):
        """Without wildcards, name filter does case-insensitive exact match."""
        result = v2_name_filter(self.qs, "Sales Team Alpha")
        self.assertEqual(list(result.values_list("name", flat=True)), ["Sales Team Alpha"])

    def test_exact_match_no_substring(self):
        """Without wildcards, partial strings do not match."""
        result = v2_name_filter(self.qs, "Sales")
        self.assertEqual(result.count(), 0)

    def test_exact_match_case_insensitive(self):
        """Exact match is case-insensitive."""
        result = v2_name_filter(self.qs, "sales team alpha")
        self.assertEqual(result.count(), 1)
        self.assertEqual(result.first().name, "Sales Team Alpha")

    def test_wildcard_prefix(self):
        """Prefix pattern matches names starting with the given string."""
        result = v2_name_filter(self.qs, "Sales*")
        self.assertCountEqual(list(result.values_list("name", flat=True)), ["Sales Team Alpha", "Sales Team Beta"])

    def test_wildcard_suffix(self):
        """Suffix pattern matches names ending with the given string."""
        result = v2_name_filter(self.qs, "*Alpha")
        self.assertEqual(list(result.values_list("name", flat=True)), ["Sales Team Alpha"])

    def test_wildcard_substring(self):
        """Substring pattern *term* matches names containing the term."""
        result = v2_name_filter(self.qs, "*Team*")
        self.assertCountEqual(list(result.values_list("name", flat=True)), ["Sales Team Alpha", "Sales Team Beta"])

    def test_wildcard_complex_pattern(self):
        """Complex pattern with multiple wildcards."""
        result = v2_name_filter(self.qs, "Sales*Alpha")
        self.assertEqual(list(result.values_list("name", flat=True)), ["Sales Team Alpha"])

    def test_wildcard_star_returns_all(self):
        """Bare * returns all records (no filter applied)."""
        result = v2_name_filter(self.qs, "*")
        self.assertEqual(result.count(), 3)

    def test_wildcard_no_match(self):
        """Wildcard pattern matching nothing returns empty queryset."""
        result = v2_name_filter(self.qs, "zzz*")
        self.assertEqual(result.count(), 0)

    def test_wildcard_case_insensitive(self):
        """Wildcard matching is case-insensitive."""
        result = v2_name_filter(self.qs, "*sales*")
        self.assertCountEqual(list(result.values_list("name", flat=True)), ["Sales Team Alpha", "Sales Team Beta"])

    def test_custom_field(self):
        """Filter works with a custom field name."""
        result = v2_name_filter(self.qs, "standard", field="type")
        self.assertEqual(result.count(), 3)
