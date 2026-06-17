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
"""Test the group serializer v2 description override."""

from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from api.models import Tenant
from management.group.model import Group
from management.group.serializer import (
    GroupInputSerializer,
    _V2_ADMIN_GROUP_DESCRIPTION,
    _V2_GROUP_DESCRIPTION,
    _v2_description_override,
)
from management.models import Policy

_PATCH_IS_V2 = "management.group.serializer.is_v2_edit_enabled_for_request"


@override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat")
class GroupDescriptionOverrideTests(TestCase):
    """Test that default group descriptions are overridden for v2 (workspaces-enabled) orgs."""

    @classmethod
    def setUpClass(cls):
        """Set up class-level test data."""
        super().setUpClass()
        cls.public_tenant = Tenant.objects.get(tenant_name="public")

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.v1_group_description = "Original v1 description"
        self.v1_admin_description = "Original v1 admin description"

        self.default_group = Group.objects.create(
            name="Default access",
            description=self.v1_group_description,
            platform_default=True,
            system=True,
            tenant=self.public_tenant,
        )
        Policy.objects.create(
            name="System Policy for Group {}".format(self.default_group.uuid),
            system=True,
            tenant=self.public_tenant,
            group=self.default_group,
        )
        self.admin_group = Group.objects.create(
            name="Default admin access",
            description=self.v1_admin_description,
            admin_default=True,
            system=True,
            tenant=self.public_tenant,
        )
        Policy.objects.create(
            name="System Policy for Group {}".format(self.admin_group.uuid),
            system=True,
            tenant=self.public_tenant,
            group=self.admin_group,
        )

        self.tenant = Tenant.objects.create(
            tenant_name="acct_serializer_test", account_id="9990001", org_id="9990001", ready=True
        )

        self.regular_group = Group.objects.create(
            name="Custom group",
            description="A custom group",
            tenant=self.tenant,
        )

    def tearDown(self):
        """Clean up test data."""
        self.regular_group.delete()
        Group.objects.filter(pk__in=[self.default_group.pk, self.admin_group.pk]).delete()
        self.tenant.delete()
        super().tearDown()

    def _make_request(self):
        """Create a mock request with the test tenant."""
        request = Mock(spec=["tenant", "user"])
        request.tenant = self.tenant
        request.user = Mock()
        request.user.org_id = self.tenant.org_id
        return request

    # -- _v2_description_override unit tests --

    @patch(_PATCH_IS_V2, return_value=False)
    def test_override_skipped_for_v1_org_platform_default(self, _mock):
        """v1 org sees original description for platform_default group."""
        request = self._make_request()
        data = {"platform_default": True, "admin_default": False, "description": self.v1_group_description}
        _v2_description_override(data, request)
        self.assertEqual(data["description"], self.v1_group_description)

    @patch(_PATCH_IS_V2, return_value=False)
    def test_override_skipped_for_v1_org_admin_default(self, _mock):
        """v1 org sees original description for admin_default group."""
        request = self._make_request()
        data = {"platform_default": False, "admin_default": True, "description": self.v1_admin_description}
        _v2_description_override(data, request)
        self.assertEqual(data["description"], self.v1_admin_description)

    @patch(_PATCH_IS_V2, return_value=True)
    def test_override_applied_for_v2_org_platform_default(self, _mock):
        """v2 org sees overridden description for platform_default group."""
        request = self._make_request()
        data = {"platform_default": True, "admin_default": False, "description": self.v1_group_description}
        _v2_description_override(data, request)
        self.assertEqual(data["description"], _V2_GROUP_DESCRIPTION)

    @patch(_PATCH_IS_V2, return_value=True)
    def test_override_applied_for_v2_org_admin_default(self, _mock):
        """v2 org sees overridden description for admin_default group."""
        request = self._make_request()
        data = {"platform_default": False, "admin_default": True, "description": self.v1_admin_description}
        _v2_description_override(data, request)
        self.assertEqual(data["description"], _V2_ADMIN_GROUP_DESCRIPTION)

    @patch(_PATCH_IS_V2, return_value=True)
    def test_override_skipped_for_regular_group(self, _mock):
        """Regular groups are never overridden, even for v2 orgs."""
        request = self._make_request()
        original = "A custom group"
        data = {"platform_default": False, "admin_default": False, "description": original}
        _v2_description_override(data, request)
        self.assertEqual(data["description"], original)

    def test_override_skipped_when_no_request(self):
        """No-op when request is None."""
        data = {"platform_default": True, "admin_default": False, "description": self.v1_group_description}
        _v2_description_override(data, None)
        self.assertEqual(data["description"], self.v1_group_description)

    @patch(_PATCH_IS_V2, return_value=True)
    def test_v2_check_cached_on_request(self, mock_is_v2):
        """The v2 check result is cached on the request object."""
        request = self._make_request()
        data = {"platform_default": True, "admin_default": False, "description": self.v1_group_description}

        _v2_description_override(data, request)
        self.assertTrue(hasattr(request, "_is_v2_org"))
        self.assertTrue(request._is_v2_org)

        # Second call uses the cache — is_v2_edit_enabled_for_request not called again
        data["description"] = self.v1_group_description
        _v2_description_override(data, request)
        self.assertEqual(data["description"], _V2_GROUP_DESCRIPTION)
        mock_is_v2.assert_called_once()

    # -- GroupInputSerializer integration tests --

    @patch(_PATCH_IS_V2, return_value=False)
    def test_input_serializer_v1_org(self, _mock):
        """GroupInputSerializer returns original description for v1 orgs."""
        request = self._make_request()
        serializer = GroupInputSerializer(self.default_group, context={"request": request})
        self.assertEqual(serializer.data["description"], self.v1_group_description)

    @patch(_PATCH_IS_V2, return_value=True)
    def test_input_serializer_v2_org(self, _mock):
        """GroupInputSerializer returns v2 description for v2 orgs."""
        request = self._make_request()
        serializer = GroupInputSerializer(self.default_group, context={"request": request})
        self.assertEqual(serializer.data["description"], _V2_GROUP_DESCRIPTION)

    @patch(_PATCH_IS_V2, return_value=True)
    def test_input_serializer_admin_v2_org(self, _mock):
        """GroupInputSerializer returns v2 description for admin_default group on v2 org."""
        request = self._make_request()
        serializer = GroupInputSerializer(self.admin_group, context={"request": request})
        self.assertEqual(serializer.data["description"], _V2_ADMIN_GROUP_DESCRIPTION)

    @patch(_PATCH_IS_V2, return_value=True)
    def test_input_serializer_regular_group_v2_org(self, _mock):
        """GroupInputSerializer does not override regular group descriptions."""
        request = self._make_request()
        serializer = GroupInputSerializer(self.regular_group, context={"request": request})
        self.assertEqual(serializer.data["description"], "A custom group")
