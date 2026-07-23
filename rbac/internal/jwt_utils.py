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
"""Classes to handle JWT token generation and management."""

import base64
import http.client
import json
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)


class JWTProvider:
    """Class to handle creation of JWT token."""

    # Instance variable for the class.
    _instance = None

    def __new__(cls, *args, **kwargs):
        """Create a single instance of the class."""
        if cls._instance is None:
            cls._instance = super().__new__(cls, *args, **kwargs)

        return cls._instance

    def __init__(self):
        """Establish SSO connection information."""
        self.connection = None

    def get_conn(self):
        """Get connection to sso stage."""
        if settings.REDHAT_SSO:
            self.connection = http.client.HTTPSConnection(settings.REDHAT_SSO)
        return self.connection

    def get_jwt_token(self, client_id, client_secret):
        """Retrieve jwt token from Redhat SSO."""
        connection = self.get_conn()

        # Test the connection
        if connection is None:
            return None

        if client_id is None or client_secret is None:
            raise Exception("Missing client_id or client_secret in environment file.")

        payload = (
            f"grant_type={settings.TOKEN_GRANT_TYPE}&"
            f"client_id={settings.RELATIONS_API_CLIENT_ID}&"
            f"client_secret={settings.RELATIONS_API_CLIENT_SECRET}&"
            f"scope={settings.SCOPE}"
        )

        headers = {"content-type": "application/x-www-form-urlencoded"}

        connection.request("POST", settings.OPENID_URL, payload, headers)

        res = connection.getresponse()
        data = res.read()
        json_data = json.loads(data)

        if "access_token" not in json_data:
            raise Exception(f"Unexpected response when retrieving JWT (status={res.status}): {json_data}")

        token = json_data["access_token"]
        return token


class JWTManager:
    """Class to handle management of JWT tokens."""

    def __init__(self, jwt_provider, jwt_cache):
        """Establish connection to JWT cache and provider."""
        self.jwt_cache = jwt_cache
        self.jwt_provider = jwt_provider

    @staticmethod
    def is_token_expired(token):
        """Check if JWT token is expired based on its exp claim."""
        try:
            # Decode JWT payload without verification (safe for reading claims only)
            # JWT format: header.payload.signature
            parts = token.split(".")
            if len(parts) != 3:
                logger.warning("Invalid JWT format: expected 3 parts separated by dots")
                return True

            # Decode the payload (second part) - base64url encoded
            payload = parts[1]
            # Add padding if needed (base64 requires length to be multiple of 4)
            padding = len(payload) % 4
            if padding:
                payload += "=" * (4 - padding)

            decoded_bytes = base64.urlsafe_b64decode(payload)
            claims = json.loads(decoded_bytes)

            if claims and "exp" in claims:
                # Add 60 second buffer to avoid edge cases
                return time.time() > (claims["exp"] - 60)
            # If no exp claim, consider it expired to be safe
            return True
        except Exception as e:
            logger.warning(f"Failed to decode JWT token for expiration check: {e}")
            return True

    def get_jwt_from_redis(self):
        """Retrieve jwt token from redis or generate from Redhat SSO if not exists in redis."""
        if not settings.REDHAT_SSO:
            logger.debug(
                "REDHAT_SSO not configured; "
                "skipping JWT token retrieval (expected in ephemeral/local environments)."
            )
            return None

        try:
            # Try retrieve token from redis
            token = self.jwt_cache.get_jwt_response()

            # Check if token exists and is not expired
            if token and not self.is_token_expired(token):
                logger.debug("Valid token retrieved from redis.")
                return token

            # Token missing or expired, get new one
            if token:
                logger.info("Cached token expired, requesting new token.")
            else:
                logger.info("No token in cache, requesting new token.")

            token = self.jwt_provider.get_jwt_token(
                settings.RELATIONS_API_CLIENT_ID, settings.RELATIONS_API_CLIENT_SECRET
            )
            # Token obtained store it in redis
            if token:
                self.jwt_cache.set_jwt_response(token)
                logger.info("Token stored in redis.")
            else:
                logger.error("Failed to store jwt token in redis.")

            return token

        except Exception as e:
            logger.error(f"Error occurred when trying to retrieve JWT token: {e}", exc_info=True)
            return None
