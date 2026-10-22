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

from json import JSONDecodeError
from unittest import mock

import requests
from django.conf import settings
from rest_framework import status
from tests.identity_request import IdentityRequest

from management.authorization.jwks_source import OIDCConfigurationJWKSSource, _request_json
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

        mock_get.assert_called_once_with(
            url="https://example.com/.well-known/openid-configuration", timeout=settings.OUTBOUND_HTTP_TIMEOUT
        )
        self.assertEqual(result, {"keys": []})

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_request_json_connection_error(self, mock_get):
        """Test that a ConnectionError raises UnableMeetPrerequisitesError."""
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        with self.assertRaises(UnableMeetPrerequisitesError) as ctx:
            _request_json("https://example.com/endpoint")

        self.assertIn("unable to fetch", str(ctx.exception))
        mock_get.assert_called_once_with(url="https://example.com/endpoint", timeout=settings.OUTBOUND_HTTP_TIMEOUT)

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_request_json_timeout_error(self, mock_get):
        """Test that a Timeout exception triggers UnableMeetPrerequisitesError."""
        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")

        with self.assertRaises(UnableMeetPrerequisitesError) as ctx:
            _request_json("https://example.com/endpoint")

        self.assertIn("unable to fetch", str(ctx.exception))
        mock_get.assert_called_once_with(url="https://example.com/endpoint", timeout=settings.OUTBOUND_HTTP_TIMEOUT)

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_request_json_non_success_status(self, mock_get):
        """Test that a non-success status code raises UnableMeetPrerequisitesError."""
        mock_response = mock.MagicMock()
        mock_response.status_code = status.HTTP_502_BAD_GATEWAY
        mock_response.content = b"Bad Gateway"
        mock_get.return_value = mock_response

        with self.assertRaises(UnableMeetPrerequisitesError) as ctx:
            _request_json("https://example.com/endpoint")

        self.assertIn("unexpected status code", str(ctx.exception))
        mock_get.assert_called_once_with(url="https://example.com/endpoint", timeout=settings.OUTBOUND_HTTP_TIMEOUT)

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_request_json_json_decode_error(self, mock_get):
        """Test that a JSON decode failure raises UnableMeetPrerequisitesError."""
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = JSONDecodeError("Expecting value", "", 0)
        mock_response.content = b"not json"
        mock_get.return_value = mock_response

        with self.assertRaises(UnableMeetPrerequisitesError) as ctx:
            _request_json("https://example.com/endpoint")

        self.assertIn("unable to decode the JSON response", str(ctx.exception))
        mock_get.assert_called_once_with(url="https://example.com/endpoint", timeout=settings.OUTBOUND_HTTP_TIMEOUT)


class OIDCConfigurationJWKSSourceTests(IdentityRequest):
    """Tests for the OIDCConfigurationJWKSSource class covering discovery and JWKS-URI fetch."""

    OIDC_URL = "https://sso.example.com/.well-known/openid-configuration"
    JWKS_URI = "https://sso.example.com/protocol/openid-connect/certs"

    def _make_source(self):
        return OIDCConfigurationJWKSSource(url=self.OIDC_URL)

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_fetch_jwks_passes_timeout_to_both_requests(self, mock_get):
        """Test that fetch_jwks passes timeout on both the discovery and JWKS-URI GET requests."""
        discovery_response = mock.MagicMock()
        discovery_response.status_code = 200
        discovery_response.json.return_value = {"jwks_uri": self.JWKS_URI}

        jwks_response = mock.MagicMock()
        jwks_response.status_code = 200
        jwks_response.json.return_value = {"keys": [{"kid": "abc"}]}

        mock_get.side_effect = [discovery_response, jwks_response]
        source = self._make_source()

        result = source.fetch_jwks()

        self.assertEqual(result, {"keys": [{"kid": "abc"}]})
        self.assertEqual(mock_get.call_count, 2)

        # Both calls must include the timeout kwarg
        for call in mock_get.call_args_list:
            self.assertEqual(call.kwargs.get("timeout"), settings.OUTBOUND_HTTP_TIMEOUT)

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_fetch_jwks_discovery_connection_error(self, mock_get):
        """Test that a connection error on the discovery request raises UnableMeetPrerequisitesError."""
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")
        source = self._make_source()

        with self.assertRaises(UnableMeetPrerequisitesError):
            source.fetch_jwks()

        mock_get.assert_called_once_with(url=self.OIDC_URL, timeout=settings.OUTBOUND_HTTP_TIMEOUT)

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_fetch_jwks_discovery_timeout(self, mock_get):
        """Test that a timeout on the discovery request raises UnableMeetPrerequisitesError."""
        mock_get.side_effect = requests.exceptions.Timeout("Timed out")
        source = self._make_source()

        with self.assertRaises(UnableMeetPrerequisitesError):
            source.fetch_jwks()

        mock_get.assert_called_once_with(url=self.OIDC_URL, timeout=settings.OUTBOUND_HTTP_TIMEOUT)

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_fetch_jwks_discovery_json_decode_error(self, mock_get):
        """Test that a JSON decode failure on the discovery response raises UnableMeetPrerequisitesError."""
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = JSONDecodeError("Expecting value", "", 0)
        mock_response.content = b"not json"
        mock_get.return_value = mock_response
        source = self._make_source()

        with self.assertRaises(UnableMeetPrerequisitesError):
            source.fetch_jwks()

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_fetch_jwks_jwks_uri_connection_error(self, mock_get):
        """Test that a connection error on the JWKS-URI request raises UnableMeetPrerequisitesError."""
        discovery_response = mock.MagicMock()
        discovery_response.status_code = 200
        discovery_response.json.return_value = {"jwks_uri": self.JWKS_URI}

        def side_effect(url, **kwargs):
            if url == self.OIDC_URL:
                return discovery_response
            raise requests.exceptions.ConnectionError("Connection refused")

        mock_get.side_effect = side_effect
        source = self._make_source()

        with self.assertRaises(UnableMeetPrerequisitesError):
            source.fetch_jwks()

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_fetch_jwks_jwks_uri_timeout(self, mock_get):
        """Test that a timeout on the JWKS-URI request raises UnableMeetPrerequisitesError."""
        discovery_response = mock.MagicMock()
        discovery_response.status_code = 200
        discovery_response.json.return_value = {"jwks_uri": self.JWKS_URI}

        def side_effect(url, **kwargs):
            if url == self.OIDC_URL:
                return discovery_response
            raise requests.exceptions.Timeout("Timed out")

        mock_get.side_effect = side_effect
        source = self._make_source()

        with self.assertRaises(UnableMeetPrerequisitesError):
            source.fetch_jwks()

    @mock.patch("management.authorization.jwks_source.requests.get")
    def test_fetch_jwks_jwks_uri_json_decode_error(self, mock_get):
        """Test that a JSON decode failure on the JWKS-URI response raises UnableMeetPrerequisitesError."""
        discovery_response = mock.MagicMock()
        discovery_response.status_code = 200
        discovery_response.json.return_value = {"jwks_uri": self.JWKS_URI}

        jwks_response = mock.MagicMock()
        jwks_response.status_code = 200
        jwks_response.json.side_effect = JSONDecodeError("Expecting value", "", 0)
        jwks_response.content = b"not json"

        mock_get.side_effect = [discovery_response, jwks_response]
        source = self._make_source()

        with self.assertRaises(UnableMeetPrerequisitesError):
            source.fetch_jwks()
