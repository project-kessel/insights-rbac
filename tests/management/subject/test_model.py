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
"""Tests for Subject model, SubjectManager, and SubjectType."""

import uuid

from management.exceptions import NotFoundError
from management.group.model import Group
from management.principal.model import Principal
from management.subject import Subject, SubjectType, UnsupportedSubjectTypeError
from tests.identity_request import IdentityRequest


class SubjectTypeTests(IdentityRequest):
    """Tests for the SubjectType enum."""

    def test_values_returns_all_types(self):
        """Test that values() returns all valid subject types."""
        values = SubjectType.values()

        self.assertEqual(set(values), {"group", "user", "principal"})

    def test_is_valid_returns_true_for_valid_types(self):
        """Test that is_valid returns True for valid subject types."""
        test_cases = [
            ("group", True),
            ("user", True),
            ("principal", True),
            (SubjectType.GROUP, True),
            (SubjectType.USER, True),
            (SubjectType.PRINCIPAL, True),
        ]

        for value, expected in test_cases:
            with self.subTest(value=value):
                self.assertEqual(SubjectType.is_valid(value), expected)

    def test_is_valid_returns_false_for_invalid_types(self):
        """Test that is_valid returns False for invalid subject types."""
        test_cases = ["invalid", "", "GROUP", "USER", "PRINCIPAL"]

        for value in test_cases:
            with self.subTest(value=value):
                self.assertFalse(SubjectType.is_valid(value))


class SubjectManagerTests(IdentityRequest):
    """Tests for Subject.objects (SubjectManager)."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create a group
        self.group = Group.objects.create(
            name="Test Group",
            tenant=self.tenant,
        )

        # Create a principal
        self.principal = Principal.objects.create(
            username="testuser",
            tenant=self.tenant,
            type=Principal.Types.USER,
            user_id="testuser",
        )

    def tearDown(self):
        """Clean up test data."""
        Group.objects.filter(tenant=self.tenant).delete()
        Principal.objects.filter(tenant=self.tenant).delete()
        super().tearDown()


class GroupLookupTests(SubjectManagerTests):
    """Tests for Subject.objects.group()."""

    def test_group_returns_subject_wrapping_group(self):
        """Test that group() returns a Subject wrapping the Group."""
        result = Subject.objects.group(id=str(self.group.uuid))

        expected = Subject(type=SubjectType.GROUP, entity=self.group)
        self.assertEqual(result, expected)

    def test_group_annotates_principal_count(self):
        """Test that group() annotates the group with principal count."""
        self.group.principals.add(self.principal)

        result = Subject.objects.group(id=str(self.group.uuid))

        # Verify annotation exists and has correct value
        self.assertEqual(result.entity.principalCount, 1)

    def test_group_raises_not_found_for_invalid_uuid(self):
        """Test that group() raises NotFoundError for non-existent group."""
        fake_uuid = str(uuid.uuid4())

        with self.assertRaises(NotFoundError) as context:
            Subject.objects.group(id=fake_uuid)

        expected = NotFoundError("group", fake_uuid)
        self.assertEqual(str(context.exception), str(expected))


class UserLookupTests(SubjectManagerTests):
    """Tests for Subject.objects.user()."""

    def test_user_returns_subject_wrapping_principal(self):
        """Test that user() returns a Subject wrapping the Principal."""
        result = Subject.objects.user(id=str(self.principal.uuid))

        expected = Subject(type=SubjectType.USER, entity=self.principal)
        self.assertEqual(result, expected)

    def test_user_raises_not_found_for_invalid_uuid(self):
        """Test that user() raises NotFoundError for non-existent principal."""
        fake_uuid = str(uuid.uuid4())

        with self.assertRaises(NotFoundError) as context:
            Subject.objects.user(id=fake_uuid)

        expected = NotFoundError("user", fake_uuid)
        self.assertEqual(str(context.exception), str(expected))


class ByTypeLookupTests(SubjectManagerTests):
    """Tests for Subject.objects.by_type()."""

    def test_by_type_returns_subject_for_group_type(self):
        """Test that by_type returns a Subject for type='group'."""
        result = Subject.objects.by_type(type="group", id=str(self.group.uuid))

        expected = Subject(type=SubjectType.GROUP, entity=self.group)
        self.assertEqual(result, expected)

    def test_by_type_returns_subject_for_user_type(self):
        """Test that by_type returns a Subject for type='user'."""
        result = Subject.objects.by_type(type="user", id=str(self.principal.uuid))

        expected = Subject(type=SubjectType.USER, entity=self.principal)
        self.assertEqual(result, expected)

    def test_by_type_raises_unsupported_subject_type_error(self):
        """Test that by_type raises UnsupportedSubjectTypeError for invalid types."""
        test_cases = ["invalid", "principal", "GROUP"]

        for subject_type in test_cases:
            with self.subTest(subject_type=subject_type):
                with self.assertRaises(UnsupportedSubjectTypeError) as context:
                    Subject.objects.by_type(type=subject_type, id=str(self.group.uuid))

                expected = UnsupportedSubjectTypeError(subject_type, supported=[SubjectType.GROUP, SubjectType.USER])
                self.assertEqual(str(context.exception), str(expected))

    def test_by_type_raises_not_found_for_invalid_group(self):
        """Test that by_type raises NotFoundError for non-existent group."""
        fake_uuid = str(uuid.uuid4())

        with self.assertRaises(NotFoundError) as context:
            Subject.objects.by_type(type="group", id=fake_uuid)

        expected = NotFoundError("group", fake_uuid)
        self.assertEqual(str(context.exception), str(expected))

    def test_by_type_raises_not_found_for_invalid_principal(self):
        """Test that by_type raises NotFoundError for non-existent principal."""
        fake_uuid = str(uuid.uuid4())

        with self.assertRaises(NotFoundError) as context:
            Subject.objects.by_type(type="user", id=fake_uuid)

        expected = NotFoundError("user", fake_uuid)
        self.assertEqual(str(context.exception), str(expected))


class SubjectPropertiesTests(SubjectManagerTests):
    """Tests for Subject instance properties."""

    def test_uuid_returns_entity_uuid(self):
        """Test that uuid property returns the entity's UUID."""
        subject = Subject.objects.group(id=str(self.group.uuid))

        self.assertEqual(subject.uuid, self.group.uuid)

    def test_is_group_returns_true_for_groups(self):
        """Test that is_group returns True for group subjects."""
        subject = Subject.objects.group(id=str(self.group.uuid))

        self.assertTrue(subject.is_group)
        self.assertFalse(subject.is_user)

    def test_is_user_returns_true_for_users(self):
        """Test that is_user returns True for user subjects."""
        subject = Subject.objects.user(id=str(self.principal.uuid))

        self.assertTrue(subject.is_user)
        self.assertFalse(subject.is_group)
