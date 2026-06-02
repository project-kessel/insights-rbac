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
"""Test the permission model."""

from unittest import TestCase

from management.models import Permission
from management.permission.model import PermissionValue
from tests.identity_request import IdentityRequest

INVALID_PERMISSION_TUPLES = [
    # Invalid due to None.
    (None, None, None),
    (None, "resource", "verb"),
    ("app", None, "verb"),
    ("app", "resource", None),
    # Invalid due to app wildcard.
    ("a*", "resource", "verb"),
    ("*", "resource", "verb"),
    # Invalid due to partial wildcard.
    ("app", "r*", "verb"),
    ("app", "resource", "v*"),
    # Invalid due to colons.
    ("app:extra", "resource", "verb"),
    ("app", "resource:extra", "verb"),
    ("app", "resource", "verb:extra"),
]

VALID_PERMISSION_TUPLES = [
    ("app", "resource", "verb"),
    ("app", "resource", "*"),
    ("app", "*", "verb"),
    ("app", "*", "*"),
]

INVALID_PERMISSIONS_V1 = [
    "",
    "app",
    "app:resource",
    "app:resource:verb:extra",
    "*:resource:verb",
    "app:resource:verb*",
    "app:resource*:verb",
    "app*:resource:verb",
]

VALID_PERMISSIONS_V1 = [
    ("app:resource:verb", ("app", "resource", "verb")),
    ("app:resource:*", ("app", "resource", "*")),
    ("app:*:verb", ("app", "*", "verb")),
    ("app:*:*", ("app", "*", "*")),
]


class PermissionValueTests(TestCase):
    """Test the PermissionValue class."""

    def test_construct_valid(self):
        """Test that valid PermissionValues can be constructed."""
        for permission_args in VALID_PERMISSION_TUPLES:
            with self.subTest(permission=permission_args):
                value = PermissionValue(*permission_args)
                self.assertEqual(permission_args[0], value.application)
                self.assertEqual(permission_args[1], value.resource_type)
                self.assertEqual(permission_args[2], value.verb)

    def test_construct_invalid(self):
        """Test that invalid PermissionValues cannot be constructed."""
        for permission_args in INVALID_PERMISSION_TUPLES:
            with self.subTest(permission=permission_args):
                self.assertRaises(ValueError, PermissionValue, *permission_args)

    def test_parse_valid(self):
        """Test that parse_v1 accepts valid V1 permission strings."""
        for v1_string, expected in VALID_PERMISSIONS_V1:
            with self.subTest(v1_string=v1_string, expected_permission=expected):
                value = PermissionValue.parse_v1(v1_string)
                self.assertEqual(PermissionValue(*expected), value)

    def test_parse_invalid(self):
        """Test that parse_v1 does not accept invalid V1 permission strings."""
        for v1_string in INVALID_PERMISSIONS_V1:
            with self.subTest(v1_string=v1_string):
                self.assertRaises(ValueError, PermissionValue.parse_v1, v1_string)

    def test_v1_string_invariant(self):
        """Test that v1_string returns the appropriate V1 permission string."""
        for v1_string, _ in VALID_PERMISSIONS_V1:
            with self.subTest(v1_string=v1_string):
                value = PermissionValue.parse_v1(v1_string)
                self.assertEqual(v1_string, value.v1_string())

    def test_with_wildcard(self):
        """Test applying with_* wildcard methods works on a non-wildcard permission."""
        base = PermissionValue("app", "resource", "verb")

        self.assertEqual(PermissionValue("app", "resource", "*"), base.with_unconstrained_verb())
        self.assertEqual(PermissionValue("app", "*", "verb"), base.with_unconstrained_resource_type())
        self.assertEqual(PermissionValue("app", "*", "*"), base.with_application_only())

    def test_with_wildcard_duplicate(self):
        """Test applying with_* wildcard methods works leaves wildcards intact."""
        app_only = PermissionValue("app", "*", "*")

        self.assertEqual(app_only, app_only.with_unconstrained_verb())
        self.assertEqual(app_only, app_only.with_unconstrained_resource_type())
        self.assertEqual(app_only, app_only.with_application_only())

    def test_is_wildcard(self):
        """Test that is_wildcard is true when resource type or verb is *."""
        self.assertFalse(PermissionValue("app", "resource", "verb").is_wildcard)
        self.assertTrue(PermissionValue("app", "*", "verb").is_wildcard)
        self.assertTrue(PermissionValue("app", "resource", "*").is_wildcard)
        self.assertTrue(PermissionValue("app", "*", "*").is_wildcard)

    def test_subsumes(self):
        """Test that subsumes reflects wildcard coverage between patterns."""
        concrete = PermissionValue("app", "resource", "verb")
        resource_wildcard = PermissionValue("app", "*", "verb")
        verb_wildcard = PermissionValue("app", "resource", "*")
        app_wildcard = PermissionValue("app", "*", "*")

        self.assertTrue(app_wildcard.subsumes(concrete))
        self.assertTrue(resource_wildcard.subsumes(concrete))
        self.assertTrue(verb_wildcard.subsumes(concrete))
        self.assertTrue(concrete.subsumes(concrete))

        self.assertFalse(concrete.subsumes(resource_wildcard))
        self.assertFalse(resource_wildcard.subsumes(verb_wildcard))
        self.assertFalse(app_wildcard.subsumes(PermissionValue("other", "resource", "verb")))


class PermissionModelTests(IdentityRequest):
    """Test the permission model."""

    def setUp(self):
        """Set up the permission model tests."""
        super().setUp()

        self.dependency_permission = Permission.objects.create(permission="rbac:roles:read", tenant=self.tenant)
        self.permission = Permission.objects.create(permission="rbac:roles:write", tenant=self.tenant)
        self.permission.save()
        self.permission.permissions.add(self.dependency_permission)

    def tearDown(self):
        """Tear down permission model tests."""
        Permission.objects.all().delete()

    def test_permission_has_attributes(self):
        """Test the permission has expected attributes."""
        self.assertEqual(self.permission.application, "rbac")
        self.assertEqual(self.permission.resource_type, "roles")
        self.assertEqual(self.permission.verb, "write")
        self.assertEqual(self.permission.permission, "rbac:roles:write")
        self.assertEqual(list(self.permission.permissions.all()), [self.dependency_permission])
        self.assertEqual(list(self.dependency_permission.requiring_permissions.all()), [self.permission])
