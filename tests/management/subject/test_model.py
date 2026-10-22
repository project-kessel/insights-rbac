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

from management.exceptions import InvalidFieldError, NotFoundError
from management.group.model import Group
from management.principal.model import Principal
from management.subject import Subject, SubjectType, UnsupportedSubjectTypeError
from tests.identity_request import IdentityRequest


class SubjectTypeTests(IdentityRequest):
    """Tests for the SubjectType enum."""

    def test_values_returns_all_types(self):
        """Test that values() returns all valid subject types."""
        values = SubjectType.values()

        self.assertEqual(set(values), {"group", "user", "principal", "service-account"})

    def test_is_valid_returns_true_for_valid_types(self):
        """Test that is_valid returns True for valid subject types."""
        test_cases = [
            ("group", True),
            ("user", True),
            ("principal", True),
            ("service-account", True),
            (SubjectType.GROUP, True),
            (SubjectType.USER, True),
            (SubjectType.PRINCIPAL, True),
            (SubjectType.SERVICE_ACCOUNT, True),
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

        # Create a service account principal. A service access token is one of these with an extra attribute,
        # so it resolves through the same path.
        self.service_account_client_id = str(uuid.uuid4())
        self.service_account = Principal.objects.create(
            username=f"service-account-{self.service_account_client_id}",
            tenant=self.tenant,
            type=Principal.Types.SERVICE_ACCOUNT,
            service_account_id=self.service_account_client_id,
            user_id="service-account-sub",
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


class ServiceAccountLookupTests(SubjectManagerTests):
    """Tests for Subject.objects.service_account() and .service_accounts()."""

    def test_service_account_returns_subject_wrapping_principal(self):
        """Test that service_account() returns a Subject wrapping the Principal."""
        result = Subject.objects.service_account(id=str(self.service_account.uuid))

        expected = Subject(type=SubjectType.SERVICE_ACCOUNT, entity=self.service_account)
        self.assertEqual(result, expected)
        self.assertTrue(result.is_service_account)
        self.assertFalse(result.is_user)

    def test_service_account_raises_not_found_for_invalid_uuid(self):
        """Test that service_account() raises NotFoundError for a non-existent principal."""
        fake_uuid = str(uuid.uuid4())

        with self.assertRaises(NotFoundError) as context:
            Subject.objects.service_account(id=fake_uuid)

        self.assertEqual(str(NotFoundError("service-account", fake_uuid)), str(context.exception))

    def test_service_account_raises_not_found_for_a_user_principal(self):
        """Test that a user's UUID does not resolve through the service account path.

        Returning "not found" rather than the principal keeps a user from being bound through the wrong subject
        type, and avoids confirming that a principal exists under that UUID.
        """
        with self.assertRaises(NotFoundError):
            Subject.objects.service_account(id=str(self.principal.uuid))

    def test_service_account_raises_invalid_field_without_user_id(self):
        """Test that a service account with no user_id cannot be bound."""
        client_id = str(uuid.uuid4())
        unsynced = Principal.objects.create(
            username=f"service-account-{client_id}",
            tenant=self.tenant,
            type=Principal.Types.SERVICE_ACCOUNT,
            service_account_id=client_id,
        )

        with self.assertRaises(InvalidFieldError) as context:
            Subject.objects.service_account(id=str(unsynced.uuid))

        self.assertIn("does not have a user_id", str(context.exception))

    def test_service_accounts_resolves_in_bulk(self):
        """Test that service_accounts() resolves several UUIDs at once."""
        client_id = str(uuid.uuid4())
        second = Principal.objects.create(
            username=f"service-account-{client_id}",
            tenant=self.tenant,
            type=Principal.Types.SERVICE_ACCOUNT,
            service_account_id=client_id,
            user_id="second-sub",
        )

        result = Subject.objects.service_accounts({str(self.service_account.uuid), str(second.uuid)})

        self.assertEqual(
            {str(self.service_account.uuid): self.service_account, str(second.uuid): second},
            result,
        )

    def test_service_accounts_returns_empty_for_no_uuids(self):
        """Test that service_accounts() short circuits on an empty set."""
        self.assertEqual({}, Subject.objects.service_accounts(set()))

    def test_service_accounts_omits_user_principals(self):
        """Test that bulk resolution does not return user principals."""
        result = Subject.objects.service_accounts({str(self.service_account.uuid), str(self.principal.uuid)})

        self.assertEqual([str(self.service_account.uuid)], list(result.keys()))

    def test_service_accounts_raises_invalid_field_without_user_id(self):
        """Test that bulk resolution rejects a service account with no user_id."""
        client_id = str(uuid.uuid4())
        unsynced = Principal.objects.create(
            username=f"service-account-{client_id}",
            tenant=self.tenant,
            type=Principal.Types.SERVICE_ACCOUNT,
            service_account_id=client_id,
        )

        with self.assertRaises(InvalidFieldError) as context:
            Subject.objects.service_accounts({str(self.service_account.uuid), str(unsynced.uuid)})

        self.assertIn("do not have a user_id", str(context.exception))


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

    def test_by_type_returns_subject_for_service_account_type(self):
        """Test that by_type returns a Subject for type='service-account'."""
        result = Subject.objects.by_type(type="service-account", id=str(self.service_account.uuid))

        expected = Subject(type=SubjectType.SERVICE_ACCOUNT, entity=self.service_account)
        self.assertEqual(result, expected)

    def test_by_type_raises_unsupported_subject_type_error(self):
        """Test that by_type raises UnsupportedSubjectTypeError for invalid types."""
        test_cases = ["invalid", "principal", "GROUP"]

        for subject_type in test_cases:
            with self.subTest(subject_type=subject_type):
                with self.assertRaises(UnsupportedSubjectTypeError) as context:
                    Subject.objects.by_type(type=subject_type, id=str(self.group.uuid))

                expected = UnsupportedSubjectTypeError(
                    subject_type,
                    supported=[SubjectType.GROUP, SubjectType.USER, SubjectType.SERVICE_ACCOUNT],
                )
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
