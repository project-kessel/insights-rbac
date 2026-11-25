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
"""Tests for principal backfill mismatch guards."""

from unittest.mock import patch

from management.models import Principal
from management.principal.backfill import (
    PrincipalUserIdMismatchError,
    reject_mismatched_principal_user_ids,
)
from tests.identity_request import IdentityRequest

from api.models import User


class RejectMismatchedPrincipalUserIdsTests(IdentityRequest):
    """reject_mismatched_principal_user_ids fail-closed behaviour."""

    def _user(self, username, user_id):
        user = User()
        user.username = username
        user.user_id = user_id
        user.org_id = self.tenant.org_id
        user.is_active = True
        return user

    def test_no_op_when_local_user_id_missing(self):
        """Null local user_id is allowed so backfill can populate it."""
        Principal.objects.create(username="mismatch_user", tenant=self.tenant, user_id=None)
        reject_mismatched_principal_user_ids([self._user("mismatch_user", "51856862")], self.tenant)

    def test_no_op_when_local_matches_bop(self):
        """Matching ids (including int/str normalization) are allowed."""
        Principal.objects.create(username="mismatch_user", tenant=self.tenant, user_id="51856862")
        reject_mismatched_principal_user_ids([self._user("mismatch_user", 51856862)], self.tenant)

    @patch("management.principal.backfill.capture_exception")
    def test_raises_and_reports_when_local_differs_from_bop(self, mock_capture):
        """Stale non-null local user_id rejects and is sent to GlitchTip."""
        Principal.objects.create(username="mismatch_user", tenant=self.tenant, user_id="16777216")
        with self.assertRaises(PrincipalUserIdMismatchError) as ctx:
            reject_mismatched_principal_user_ids([self._user("mismatch_user", "51856862")], self.tenant)

        self.assertIn("16777216", str(ctx.exception))
        self.assertIn("51856862", str(ctx.exception))
        mock_capture.assert_called_once()
        self.assertIsInstance(mock_capture.call_args.args[0], PrincipalUserIdMismatchError)
