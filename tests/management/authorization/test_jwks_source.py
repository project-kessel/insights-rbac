#
# Copyright 2025 Red Hat, Inc.
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
"""Test the JWKS source module."""

from unittest import mock

import requests
from django.conf import settings
from tests.identity_request import IdentityRequest

from management.authorization.jwks_source import _request_json
from management.authorization.unable_meet_prerequisites import UnableMeetPrerequisitesError


class RequestJsonTests(IdentityRequest):
    """Tests for the _request_json helper function."""

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_request_json_passes_timeout_kwarg(self, mock_get):
        """Test that _request_json passes timeout=settings.OUTBOUND_HTTP_TIMEOUT to requests.get."""
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"keys": []}
        mock_get.return_value = mock_response

        result = _request_json("https://example.com/.well-known/openid-configuration")

        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs.get("timeout"), settings.OUTBOUND_HTTP_TIMEOUT)
        self.assertEqual(result, {"keys": []})

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_request_json_timeout_raises_unable_meet_prerequisites(self, mock_get):
        """Test that a Timeout exception triggers UnableMeetPrerequisitesError."""
        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")

        with self.assertRaises(UnableMeetPrerequisitesError):
            _request_json("https://example.com/.well-known/openid-configuration")
