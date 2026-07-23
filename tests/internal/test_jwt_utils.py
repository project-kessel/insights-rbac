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
"""Test JWT utilities."""

import base64
import json
import time
from unittest.mock import MagicMock, Mock, patch

from django.test import TestCase
from internal.jwt_utils import JWTManager, JWTProvider


class JWTManagerTest(TestCase):
    """Test JWTManager class."""

    def setUp(self):
        """Set up test fixtures."""
        self.jwt_provider = Mock(spec=JWTProvider)
        self.jwt_cache = Mock()
        self.jwt_manager = JWTManager(self.jwt_provider, self.jwt_cache)

    def _create_test_token(self, exp_offset=3600):
        """Create a test JWT token with specified expiration offset (in seconds from now).

        Args:
            exp_offset: Seconds from now when token expires (positive = future, negative = past)

        Returns:
            str: A properly formatted JWT token
        """
        header = {"alg": "HS256", "typ": "JWT"}
        exp_time = int(time.time()) + exp_offset
        payload = {"exp": exp_time, "sub": "test-user", "iss": "test-issuer"}

        # Encode header and payload
        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

        # Create fake signature
        signature = "fake_signature_for_testing"

        return f"{header_b64}.{payload_b64}.{signature}"

    def test_is_token_expired_with_valid_token(self):
        """Test that a valid non-expired token is correctly identified."""
        # Create token that expires in 1 hour
        token = self._create_test_token(exp_offset=3600)

        result = JWTManager.is_token_expired(token)

        self.assertFalse(result, "Token expiring in 1 hour should not be expired")

    def test_is_token_expired_with_expired_token(self):
        """Test that an expired token is correctly identified."""
        # Create token that expired 1 hour ago
        token = self._create_test_token(exp_offset=-3600)

        result = JWTManager.is_token_expired(token)

        self.assertTrue(result, "Token that expired 1 hour ago should be expired")

    def test_is_token_expired_with_token_near_expiration(self):
        """Test that token expiring within 60 second buffer is considered expired."""
        # Create token that expires in 30 seconds (within the 60 second buffer)
        token = self._create_test_token(exp_offset=30)

        result = JWTManager.is_token_expired(token)

        self.assertTrue(result, "Token expiring in 30 seconds should be considered expired (60s buffer)")

    def test_is_token_expired_with_token_just_outside_buffer(self):
        """Test that token expiring just outside 60 second buffer is not expired."""
        # Create token that expires in 70 seconds (outside the 60 second buffer)
        token = self._create_test_token(exp_offset=70)

        result = JWTManager.is_token_expired(token)

        self.assertFalse(result, "Token expiring in 70 seconds should not be expired (60s buffer)")

    def test_is_token_expired_with_malformed_token(self):
        """Test that malformed tokens are considered expired."""
        malformed_tokens = [
            "not.a.valid.jwt.token",  # Too many parts
            "not.jwt",  # Too few parts
            "invalid_base64!@#.invalid_base64!@#.sig",  # Invalid base64
            "",  # Empty string
        ]

        for token in malformed_tokens:
            result = JWTManager.is_token_expired(token)
            self.assertTrue(result, f"Malformed token '{token}' should be considered expired")

    def test_is_token_expired_with_missing_exp_claim(self):
        """Test that tokens without exp claim are considered expired."""
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {"sub": "test-user"}  # No exp claim

        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{header_b64}.{payload_b64}.signature"

        result = JWTManager.is_token_expired(token)

        self.assertTrue(result, "Token without exp claim should be considered expired")

    def test_is_token_expired_with_invalid_json_payload(self):
        """Test that tokens with invalid JSON payload are considered expired."""
        header_b64 = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
        # Create invalid JSON in payload
        payload_b64 = base64.urlsafe_b64encode(b"{invalid json}").decode().rstrip("=")
        token = f"{header_b64}.{payload_b64}.signature"

        result = JWTManager.is_token_expired(token)

        self.assertTrue(result, "Token with invalid JSON payload should be considered expired")

    @patch("internal.jwt_utils.settings.REDHAT_SSO", "sso.example.com")
    def test_get_jwt_from_redis_with_cached_valid_token(self):
        """Test retrieving a valid token from cache."""
        cached_token = self._create_test_token(exp_offset=3600)
        self.jwt_cache.get_jwt_response.return_value = cached_token

        result = self.jwt_manager.get_jwt_from_redis()

        self.assertEqual(result, cached_token)
        self.jwt_cache.get_jwt_response.assert_called_once()
        self.jwt_provider.get_jwt_token.assert_not_called()

    @patch("internal.jwt_utils.settings.REDHAT_SSO", "sso.example.com")
    def test_get_jwt_from_redis_with_cached_expired_token(self):
        """Test that expired cached token triggers new token fetch."""
        expired_token = self._create_test_token(exp_offset=-3600)
        new_token = self._create_test_token(exp_offset=3600)

        self.jwt_cache.get_jwt_response.return_value = expired_token
        self.jwt_provider.get_jwt_token.return_value = new_token

        result = self.jwt_manager.get_jwt_from_redis()

        self.assertEqual(result, new_token)
        self.jwt_cache.get_jwt_response.assert_called_once()
        self.jwt_provider.get_jwt_token.assert_called_once()
        self.jwt_cache.set_jwt_response.assert_called_once_with(new_token)

    @patch("internal.jwt_utils.settings.REDHAT_SSO", "sso.example.com")
    def test_get_jwt_from_redis_with_no_cached_token(self):
        """Test fetching new token when cache is empty."""
        new_token = self._create_test_token(exp_offset=3600)

        self.jwt_cache.get_jwt_response.return_value = None
        self.jwt_provider.get_jwt_token.return_value = new_token

        result = self.jwt_manager.get_jwt_from_redis()

        self.assertEqual(result, new_token)
        self.jwt_cache.get_jwt_response.assert_called_once()
        self.jwt_provider.get_jwt_token.assert_called_once()
        self.jwt_cache.set_jwt_response.assert_called_once_with(new_token)

    @patch("internal.jwt_utils.settings.REDHAT_SSO", "sso.example.com")
    def test_get_jwt_from_redis_handles_provider_failure(self):
        """Test that None is returned when token provider fails."""
        self.jwt_cache.get_jwt_response.return_value = None
        self.jwt_provider.get_jwt_token.return_value = None

        result = self.jwt_manager.get_jwt_from_redis()

        self.assertIsNone(result)
        self.jwt_cache.set_jwt_response.assert_not_called()

    @patch("internal.jwt_utils.settings.REDHAT_SSO", "sso.example.com")
    def test_get_jwt_from_redis_handles_exception(self):
        """Test that exceptions are caught and None is returned."""
        self.jwt_cache.get_jwt_response.side_effect = Exception("Redis connection error")

        result = self.jwt_manager.get_jwt_from_redis()

        self.assertIsNone(result)

    def test_is_token_expired_with_base64_padding_variations(self):
        """Test JWT decoding with different base64 padding scenarios."""
        # Create tokens with different padding by varying payload length
        for i in range(4):  # Test different padding scenarios
            header = {"alg": "HS256", "typ": "JWT"}
            exp_time = int(time.time()) + 3600
            # Add varying length data to test padding
            payload = {"exp": exp_time, "data": "x" * i}

            header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
            payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            token = f"{header_b64}.{payload_b64}.signature"

            result = JWTManager.is_token_expired(token)

            self.assertFalse(result, f"Token with padding variation {i} should not be expired")


class JWTProviderTest(TestCase):
    """Test JWTProvider class."""

    @patch("internal.jwt_utils.http.client.HTTPSConnection")
    @patch("internal.jwt_utils.settings")
    def test_get_jwt_token_success(self, mock_settings, mock_https):
        """Test successful JWT token retrieval."""
        # Configure mock settings
        mock_settings.REDHAT_SSO = "sso.example.com"
        mock_settings.TOKEN_GRANT_TYPE = "client_credentials"
        mock_settings.RELATIONS_API_CLIENT_ID = "test-client-id"
        mock_settings.RELATIONS_API_CLIENT_SECRET = "test-secret"
        mock_settings.SCOPE = "test-scope"
        mock_settings.OPENID_URL = "/auth/token"

        # Configure mock HTTPS connection
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"access_token": "test-token-12345"}).encode()
        mock_conn.getresponse.return_value = mock_response

        provider = JWTProvider()
        token = provider.get_jwt_token("test-client-id", "test-secret")

        self.assertEqual(token, "test-token-12345")
        mock_conn.request.assert_called_once()

    @patch("internal.jwt_utils.http.client.HTTPSConnection")
    @patch("internal.jwt_utils.settings")
    def test_get_jwt_token_with_none_sso(self, mock_settings, mock_https):
        """Test that None is returned when SSO is not configured."""
        mock_settings.REDHAT_SSO = None

        provider = JWTProvider()
        token = provider.get_jwt_token("test-client-id", "test-secret")

        self.assertIsNone(token)
        mock_https.assert_not_called()

    @patch("internal.jwt_utils.settings")
    def test_get_jwt_token_raises_on_missing_credentials(self, mock_settings):
        """Test that exception is raised when credentials are missing."""
        mock_settings.REDHAT_SSO = "sso.example.com"

        provider = JWTProvider()

        with self.assertRaises(Exception) as context:
            provider.get_jwt_token(None, "test-secret")

        self.assertIn("Missing client_id or client_secret", str(context.exception))

        with self.assertRaises(Exception) as context:
            provider.get_jwt_token("test-client-id", None)

        self.assertIn("Missing client_id or client_secret", str(context.exception))

    @patch("internal.jwt_utils.http.client.HTTPSConnection")
    @patch("internal.jwt_utils.settings")
    def test_get_jwt_token_raises_on_missing_access_token(self, mock_settings, mock_https):
        """Test that exception is raised on missing access token."""
        # Configure mock settings
        mock_settings.REDHAT_SSO = "sso.example.com"
        mock_settings.TOKEN_GRANT_TYPE = "client_credentials"
        mock_settings.RELATIONS_API_CLIENT_ID = "test-client-id"
        mock_settings.RELATIONS_API_CLIENT_SECRET = "test-secret"
        mock_settings.SCOPE = "test-scope"
        mock_settings.OPENID_URL = "/auth/token"

        # Configure mock HTTPS connection
        mock_conn = MagicMock()
        mock_https.return_value = mock_conn
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"error": "an unexpected error"}).encode()
        mock_conn.getresponse.return_value = mock_response

        provider = JWTProvider()

        with self.assertRaises(Exception) as context:
            provider.get_jwt_token("test-client-id", "test-secret")

        self.assertIn("an unexpected error", str(context.exception))
