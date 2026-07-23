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
"""Test the principal model."""

from django.db import IntegrityError, transaction

from management.models import Principal
from tests.identity_request import IdentityRequest


class PrincipalModelTests(IdentityRequest):
    """Test the principal model."""

    def tearDown(self):
        """Tear down principal model tests."""
        Principal.objects.all().delete()

    def test_principal_creation(self):
        """Test that we can create principal correctly."""
        # Default value for cross_account is False.
        principalA = Principal.objects.create(username="principalA", tenant=self.tenant)
        self.assertEqual(principalA.username, "principala")
        self.assertEqual(principalA.cross_account, False)

        # Explicitly set cross_account.
        principalB = Principal.objects.create(username="principalB", cross_account=True, tenant=self.tenant)
        self.assertEqual(principalB.username, "principalb")
        self.assertEqual(principalB.cross_account, True)

    def test_unique_user_id(self):
        """Test that the principals' user ids' must be either 'null' or unique."""
        with transaction.atomic():
            # Creating principals with different user IDs or even "null" user IDs is fine, as the constraint has been
            # purposely set so that "null" user IDs are considered different.
            _ = Principal.objects.create(username="principal", tenant=self.tenant, user_id="user-id-one")
            _ = Principal.objects.create(username="principal-two", tenant=self.tenant)
            _ = Principal.objects.create(username="principal-three", tenant=self.tenant, user_id="user-id-three")
            _ = Principal.objects.create(username="principal-four", tenant=self.tenant)

            with self.assertRaises(IntegrityError) as context:
                _ = Principal.objects.create(username="principal-five", tenant=self.tenant, user_id="user-id-one")

            self.assertIn(
                'duplicate key value violates unique constraint "management_principal_user_id_key"',
                str(context.exception),
                "unexpected database constraint violation",
            )
