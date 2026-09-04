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
"""Tests for Tenant.org_config and workspace_creation_limit."""

from django.core.exceptions import ValidationError
from django.test.utils import override_settings
from tests.identity_request import IdentityRequest

from api.models import Tenant


@override_settings(WORKSPACE_ORG_CREATION_LIMIT=120)
class TenantOrgConfigTests(IdentityRequest):
    """Tests for per-org workspace creation limit accessors."""

    def test_empty_org_config_uses_global_default(self):
        """Empty org_config falls back to WORKSPACE_ORG_CREATION_LIMIT."""
        self.tenant.org_config = {}
        self.assertEqual(self.tenant.workspace_creation_limit(), 120)

    def test_missing_key_uses_global_default(self):
        """org_config without workspace_creation_limit uses the global default."""
        self.tenant.org_config = {"unrelated": 1}
        self.assertEqual(self.tenant.workspace_creation_limit(), 120)

    def test_valid_override(self):
        """A positive integer override is returned."""
        self.tenant.org_config = {"workspace_creation_limit": 500}
        self.assertEqual(self.tenant.workspace_creation_limit(), 500)

    def test_none_org_config_uses_global_default(self):
        """Missing org_config attribute falls back to the global default."""
        self.tenant.org_config = None
        self.assertEqual(self.tenant.workspace_creation_limit(), 120)

    def test_invalid_stored_value_falls_back_to_default(self):
        """Invalid workspace_creation_limit in org_config falls back to the global default."""
        for invalid in (True, "lots", 0, -1, 1.5):
            with self.subTest(value=invalid):
                self.tenant.org_config = {"workspace_creation_limit": invalid}
                self.assertEqual(self.tenant.workspace_creation_limit(), 120)

    def test_merge_sets_limit(self):
        """merge_org_config sets workspace_creation_limit."""
        merged = Tenant.merge_org_config({}, {"workspace_creation_limit": 500})
        self.assertEqual(merged, {"workspace_creation_limit": 500})

    def test_merge_unsets_limit(self):
        """Null removes workspace_creation_limit."""
        merged = Tenant.merge_org_config({"workspace_creation_limit": 500}, {"workspace_creation_limit": None})
        self.assertEqual(merged, {})

    def test_merge_preserves_other_keys(self):
        """Existing unrelated keys are kept when merging."""
        merged = Tenant.merge_org_config({"future_key": 2}, {"workspace_creation_limit": 10})
        self.assertEqual(merged, {"future_key": 2, "workspace_creation_limit": 10})

    def test_merge_rejects_unknown_keys(self):
        """Unknown patch keys raise ValueError."""
        with self.assertRaises(ValueError) as context:
            Tenant.merge_org_config({}, {"workspace_hierarchy_depth_limit": 10})
        self.assertIn("Unknown org_config keys", str(context.exception))

    def test_merge_rejects_empty_patch(self):
        """An empty patch raises ValueError."""
        with self.assertRaises(ValueError) as context:
            Tenant.merge_org_config({}, {})
        self.assertIn("No org_config keys provided", str(context.exception))

    def test_merge_rejects_non_object(self):
        """A non-dict patch raises ValueError."""
        with self.assertRaises(ValueError):
            Tenant.merge_org_config({}, ["workspace_creation_limit"])

    def test_merge_does_not_validate_values(self):
        """merge_org_config merges values; clean() rejects invalid stored org_config."""
        merged = Tenant.merge_org_config({}, {"workspace_creation_limit": True})
        self.assertEqual(merged, {"workspace_creation_limit": True})
        self.tenant.org_config = merged
        with self.assertRaises(ValidationError):
            self.tenant.save(update_fields=["org_config"])

    def test_save_rejects_invalid_org_config(self):
        """Direct model writes with invalid org_config are rejected."""
        for invalid in (True, 1.5, 0, "lots"):
            with self.subTest(value=invalid):
                self.tenant.org_config = {"workspace_creation_limit": invalid}
                with self.assertRaises(ValidationError):
                    self.tenant.save(update_fields=["org_config"])
