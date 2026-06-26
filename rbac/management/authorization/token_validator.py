#
# Copyright 2023 Red Hat, Inc.
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

"""A token introspector class which validates that the given token is valid."""

import logging
import re
from abc import ABC, abstractmethod
from typing import Optional, Tuple

from django.conf import settings
from django.http import HttpRequest
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry, Token
from management.authorization.jwks_source import JWKSCacheSource, JWKSSource, OIDCConfigurationJWKSSource

from api.models import User
from .invalid_token import InvalidTokenError
from .missing_authorization import MissingAuthorizationError
from .scope_claims import ScopeClaims
from .unable_meet_prerequisites import UnableMeetPrerequisitesError

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class TokenValidator(ABC):
    """Protocol for a token validator."""

    @abstractmethod
    def _validate_token(
        self, request: HttpRequest, additional_scopes_to_validate: set[ScopeClaims]
    ) -> Tuple[str, Token]:
        """
        Parse the bearer token from the request and validate it as a JWT.

        If valid, return the Bearer token and the decoded JWT token.
        If invalid, raise an InvalidTokenError.
        If there is no Authorization header, raise a MissingAuthorizationError.

        :param request: The HTTP request containing the Authorization header.
        :param additional_scopes_to_validate: Required scopes for a valid token.
        :return: A tuple containing the Bearer token and the decoded JWT token.
        """
        ...

    def _parse_claims(self, user: User, jwt: Token) -> None:
        """Parse standard claims from the JWT token, mutating the User object."""
        user.user_id = jwt.claims.get("sub")
        user.username = jwt.claims.get("preferred_username")
        user.client_id = jwt.claims.get("client_id", "")

    def validate_token(self, request: HttpRequest, additional_scopes_to_validate: set[ScopeClaims]) -> str:
        """Validate the JWT token and return the Bearer token."""
        bearer_token, _ = self._validate_token(request, additional_scopes_to_validate)
        return bearer_token

    def get_user_from_bearer_token(self, request: HttpRequest) -> User:
        """Validate the JWT token and return a User object."""
        bearer_token, jwt = self._validate_token(request, set())

        # Assumes standard claims. Override for additional or custom claims.
        user = User()
        self._parse_claims(user, jwt)
        user.bearer_token = bearer_token

        return user


class ITSSOTokenValidator(TokenValidator):
    """JWT token validator."""

    # Instance variable for the class.
    _instance = None
    _jwks_source: JWKSSource

    def __new__(cls, *args, **kwargs):
        """Create a single instance of the class."""
        if cls._instance is None:
            cls._instance = super().__new__(cls, *args, **kwargs)

        return cls._instance

    def __init__(self):
        """Get the OIDC configuration URL."""
        self.it_host = settings.IT_SERVICE_HOST
        self.it_port = settings.IT_SERVICE_PORT
        self.it_request_timeout_seconds = settings.IT_SERVICE_TIMEOUT_SECONDS
        self.it_scheme = settings.IT_SERVICE_PROTOCOL_SCHEME

        # The host contains the URL including the port...
        self.host = f"{self.it_scheme}://{self.it_host}:{self.it_port}/auth/realms/redhat-external"
        # ... but the issuer does not. We need to make this distinction so that when validating the token, the issuer
        # correctly matches.
        self.oidc_configuration_url = f"{self.host}/.well-known/openid-configuration"

        # Initialize the JWKS source.
        self.reset_jwks_source()

    def _get_json_web_keyset(self) -> KeySet:
        jwks_certificates = self._jwks_source.fetch_jwks()

        # Import the certificates.
        try:
            return KeySet.import_key_set(jwks_certificates)
        except Exception as e:
            logger.error(f"Unable to import IT's public keys to validate the token: {e}")
            raise UnableMeetPrerequisitesError("unable to import IT's public keys to validate the token")

    def _validate_token(
        self, request: HttpRequest, additional_scopes_to_validate: set[ScopeClaims]
    ) -> Tuple[str, Token]:
        """Validate the JWT token issued by Red Hat's SSO.

        Performs validations on the issuer, audience and scope of the token. Raises exceptions if the token is not
        valid. Finally, it returns the received Bearer token in order to be able to forward it as is to IT.
        """
        bearer_token: Optional[str] = request.headers.get("Authorization")
        if not bearer_token:
            logger.debug(
                f"Issuing unauthorized response either because the Authorization header is missing, or it's blank."
                f" Request: {request}"
            )
            raise MissingAuthorizationError()

        # Strip the "Bearer" part of the token if it comes with it.
        if bearer_token.startswith("Bearer"):
            bearer_token = re.sub("Bearer\\s+", "", bearer_token)

        # Import the certificates.
        key_set: KeySet = self._get_json_web_keyset()

        # Decode the token.
        try:
            token: Token = jwt.decode(value=bearer_token, key=key_set)
        except Exception as e:
            logging.warning("[request_id: %s] Unable to decode token: %s", getattr(request, "req_id", None), str(e))
            raise InvalidTokenError("Unable to decode token")

        # Make sure that the token issuer matches the IT issuer and that the scope contains the "service accounts"
        # claim.
        claim_requests = JWTClaimsRegistry(iss={"essential": True, "value": self.issuer})

        # Make sure that the token is valid.
        try:
            # Manually check for the additional scope claims in the incoming scope claim. We do this because "joserfc"
            # doesn't have a way to specify that we want to focus on some specific claims from the many that the token
            # may have.
            if len(additional_scopes_to_validate) > 0:
                # Make sure that the "scope" claim of the token is not empty.
                scope_claim = token.claims.get("scope")
                if not scope_claim:
                    raise ValueError("the provided does not have any contents in the scope claim")

                # Validate that the additional scopes are present in the token.
                for scope_to_validate in additional_scopes_to_validate:
                    if scope_to_validate not in scope_claim:
                        raise ValueError(
                            f"the provided token does not have the required '{scope_to_validate}' claim in the scope"
                        )

            # Validate the rest of the claims, including the token expiration which will be validated with the function
            # below.
            claim_requests.validate(token.claims)
        except Exception as e:
            logging.warning(
                "[request_id: %s] Token rejected for having invalid claims: %s",
                getattr(request, "req_id", "no-request-id-present"),
                str(e),
            )
            raise InvalidTokenError("The token's claims are invalid")

        return bearer_token, token

    def set_jwks_source(self, jwks_source: JWKSSource, issuer: str) -> None:
        """Set the JWKS source to use for fetching the JWKS."""
        self.issuer = issuer
        self._jwks_source = jwks_source

    def reset_jwks_source(self) -> None:
        """Reset the JWKS source to the default OIDC configuration source."""
        self.issuer = f"{self.it_scheme}://{self.it_host}/auth/realms/redhat-external"
        self._jwks_source = JWKSCacheSource(jwks_source=OIDCConfigurationJWKSSource(self.oidc_configuration_url))

    def validate_token(self, request: HttpRequest, additional_scopes_to_validate: set[ScopeClaims]) -> str:
        """Validate the JWT token issued by Red Hat's SSO.

        Performs validations on the issuer, audience and scope of the token. Raises exceptions if the token is not
        valid. Finally, it returns the received Bearer token in order to be able to forward it as is to IT.
        """
        if settings.IT_BYPASS_TOKEN_VALIDATION:
            return "mocked-invalid-bearer-token-because-token-validation-is-disabled"

        return super().validate_token(request, additional_scopes_to_validate)

    def _parse_claims(self, user: User, jwt: Token) -> None:
        super()._parse_claims(user, jwt)
        # Assumes a particular token shape
        # TODO: support multiple based on scope similar to gateway code
        user.org_id = jwt.claims.get("organization", {}).get("id", None)
        user.account = jwt.claims.get("organization", {}).get("account_number", None)
        user.admin = "org:admin:all" in jwt.claims.get("roles", [])

    def get_user_from_bearer_token(self, request: HttpRequest) -> User:
        """Validate the JWT token and parse into a User object.

        Performs validations on the issuer, audience and scope of the token. Raises exceptions if the token is not
        valid. Finally, it returns the User object corresponding to the token.
        """
        if settings.IT_BYPASS_TOKEN_VALIDATION:
            user = User()
            user.user_id = settings.IT_BYPASS_SYSTEM_USER_ID
            user.username = "mocked-username-because-token-validation-is-disabled"
            user.org_id = "mocked-org-id-because-token-validation-is-disabled"
            user.account = "mocked-account-number-because-token-validation-is-disabled"
            user.bearer_token = "mocked-invalid-bearer-token-because-token-validation-is-disabled"
            user.admin = False
            return user

        bearer_token, jwt = self._validate_token(request, set())

        user = User()
        self._parse_claims(user, jwt)
        user.bearer_token = bearer_token
        # TODO user.is_service_account ?

        return user
