#
# Copyright 2024 Red Hat, Inc.
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
"""Test the token validator class."""

import requests

from django.conf import settings
from django.test import RequestFactory, override_settings
from rest_framework import status

from api.models import User
from management.authorization.scope_claims import ScopeClaims
from management.authorization.token_validator import ITSSOTokenValidator, InvalidTokenError, MissingAuthorizationError
from management.authorization.token_validator import UnableMeetPrerequisitesError
from tests.identity_request import IdentityRequest
from unittest import mock

from tests.management.authorization.token_fixtures import InMemoryIssuer

# IT path to fetch the service accounts.
IT_PATH_GET_SERVICE_ACCOUNTS = "/service_accounts/v1"

# Keys for the "options" dictionary. The "options" dictionary represents the query parameters passed by the calling
# client.
SERVICE_ACCOUNT_DESCRIPTION_KEY = "service_account_description"
SERVICE_ACCOUNT_NAME_KEY = "service_account_name"


class TokenValidatorTests(IdentityRequest):
    def setUp(self) -> None:
        """Set up the token validator."""
        settings.IT_SERVICE_HOST = "localhost"
        settings.IT_SERVICE_BASE_PATH = "/"
        settings.IT_SERVICE_PORT = "999"
        settings.IT_SERVICE_PROTOCOL_SCHEME = "http"
        settings.IT_SERVICE_REALM = "/auth/realms/redhat-external"
        settings.IT_SERVICE_TIMEOUT_SECONDS = 10

        self.token_validator = ITSSOTokenValidator()
        self.token_validator.reset_jwks_source()

        # Copy and paste the token validator's way of building the different elements that are required to then
        # properly check the token's claims. The goal is this to be like a double check in case the way of building
        # the different claim checks change in the main class.
        self.host = f"{settings.IT_SERVICE_PROTOCOL_SCHEME}://{settings.IT_SERVICE_HOST}:{settings.IT_SERVICE_PORT}/auth/realms/redhat-external"
        self.issuer = f"{settings.IT_SERVICE_PROTOCOL_SCHEME}://{settings.IT_SERVICE_HOST}/auth/realms/redhat-external"
        self.oidc_configuration_url = f"{self.host}/.well-known/openid-configuration"

        # Build the JWKS URL that we are expecting to see in the assertions.
        self.oidc_configuration_jwks_url = f"{self.issuer}/protocol/openid-connect/certs"

        # Set a mocked response content for the JWKS certificates payload, so that we can use it for assertions in
        # tests.
        self.jwks_certificates_response_json = {"mocked": "content"}

    def _requests_get_sideffect(
        self,
        url: str,
        oidc_configuration_url_status_code: status = status.HTTP_200_OK,
        jwks_url_response_status_code: status = status.HTTP_200_OK,
    ) -> mock.Mock:
        """Side effect handler for when we need the "requests.get" method to return different responses."""
        if url == self.oidc_configuration_url:
            return mock.Mock(
                status_code=oidc_configuration_url_status_code,
                json=lambda: {"jwks_uri": self.oidc_configuration_jwks_url},
            )
        elif url == self.oidc_configuration_jwks_url:
            return mock.Mock(
                status_code=jwks_url_response_status_code, json=lambda: self.jwks_certificates_response_json
            )

    def _requests_get_sideffect_jwks_bad_response(self, url: str) -> mock.Mock:
        """Side effect handler that returns bad request response for when the JWKS certificates are fetched."""
        if url == self.oidc_configuration_url:
            return mock.Mock(
                status_code=status.HTTP_200_OK,
                json=lambda: {"jwks_uri": self.oidc_configuration_jwks_url},
            )
        elif url == self.oidc_configuration_jwks_url:
            return mock.Mock(
                status_code=status.HTTP_400_BAD_REQUEST, json=lambda: self.jwks_certificates_response_json
            )

    def _requests_get_sideffect_jwks_connection_error(self, url: str) -> mock.Mock:
        """Side effect handler that raises a connection error when the JWKS certificates are fetched."""
        if url == self.oidc_configuration_url:
            return mock.Mock(
                status_code=status.HTTP_200_OK,
                json=lambda: {"jwks_uri": self.oidc_configuration_jwks_url},
            )
        elif url == self.oidc_configuration_jwks_url:
            raise requests.exceptions.ConnectionError

    def _requests_get_sideffect_jwks_timeout_error(self, url: str) -> mock.Mock:
        """Side effect handler that raises a timeout error when the JWKS certificates are fetched."""
        if url == self.oidc_configuration_url:
            return mock.Mock(
                status_code=status.HTTP_200_OK,
                json=lambda: {"jwks_uri": self.oidc_configuration_jwks_url},
            )
        elif url == self.oidc_configuration_jwks_url:
            raise requests.exceptions.Timeout

    def test_token_validator_singleton(self):
        """Test that the token validator class only gets instantiated once."""
        class_instances = [
            ITSSOTokenValidator(),
            ITSSOTokenValidator(),
            ITSSOTokenValidator(),
            ITSSOTokenValidator(),
            ITSSOTokenValidator(),
        ]

        for instance in class_instances:
            self.assertEqual(
                self.token_validator,
                instance,
                "no new instances of the token validator class should have been created since it is supposed to be a singleton",
            )

    def test_default_realm_builds_expected_urls(self):
        """Test that the default realm produces the same URLs and issuer as before it was configurable."""
        validator = ITSSOTokenValidator()

        self.assertEqual(
            "http://localhost:999/auth/realms/redhat-external",
            validator.host,
            "the host should be built from the default realm",
        )
        self.assertEqual(
            "http://localhost:999/auth/realms/redhat-external/.well-known/openid-configuration",
            validator.oidc_configuration_url,
            "the OIDC configuration URL should be built from the default realm",
        )

        validator.reset_jwks_source()
        self.assertEqual(
            "http://localhost/auth/realms/redhat-external",
            validator.issuer,
            "the issuer (which excludes the port) should be built from the default realm",
        )

    def test_configurable_realm_builds_expected_urls(self):
        """Test that a custom IT_SERVICE_REALM flows into the host, OIDC configuration URL, and issuer."""
        settings.IT_SERVICE_REALM = "/auth/realms/some-other-realm"

        try:
            validator = ITSSOTokenValidator()

            self.assertEqual(
                "http://localhost:999/auth/realms/some-other-realm",
                validator.host,
                "the host should reflect the configured realm",
            )
            self.assertEqual(
                "http://localhost:999/auth/realms/some-other-realm/.well-known/openid-configuration",
                validator.oidc_configuration_url,
                "the OIDC configuration URL should reflect the configured realm",
            )

            validator.reset_jwks_source()
            self.assertEqual(
                "http://localhost/auth/realms/some-other-realm",
                validator.issuer,
                "the issuer (which excludes the port) should reflect the configured realm",
            )
        finally:
            # The validator is a singleton shared across tests, so restore the default realm and rebuild its
            # derived URLs to avoid leaking the custom realm into subsequent tests.
            settings.IT_SERVICE_REALM = "/auth/realms/redhat-external"
            ITSSOTokenValidator().reset_jwks_source()

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset_cache(
        self, import_key_set: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that the obtaining the JWKS data from cache skips fetching it from IT"""
        # Simulate that we have a cache hit for the JWKS response.
        get_jwks_response.return_value = self.jwks_certificates_response_json

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # Asser that the "import key set" function gets called with the cached contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.assert_not_called()

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.jwks_source.JWKSCache.set_jwks_response")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset(
        self, import_key_set: mock.Mock, set_jwks_response: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that not having the JWKS data in cache makes us reach IT for it"""
        # Make the cache raise an exception to simulate that something went wrong with it. It should trigger having to
        # fetch the data from IT instead.
        get_jwks_response.side_effect = Exception

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.side_effect = self._requests_get_sideffect

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # The "set cache" method should have been called to store certificate contents in cache.
        set_jwks_response.assert_called_with(self.jwks_certificates_response_json)

        # And the "import key set" method should also have been called with the same contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.jwks_source.JWKSCache.set_jwks_response")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset_oidc_network_errors(
        self, import_key_set: mock.Mock, set_jwks_response: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that when we suffer from a network error when contacting IT an exception is raised"""
        # Make the cache raise an exception to simulate that something went wrong with it. It should trigger having to
        # fetch the data from IT instead.
        get_jwks_response.side_effect = Exception

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.side_effect = self._requests_get_sideffect

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # The "set cache" method should have been called to store certificate contents in cache.
        set_jwks_response.assert_called_with(self.jwks_certificates_response_json)

        # And the "import key set" method should also have been called with the same contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

        # After a successful run, simulate a network error with IT.
        test_cases = [requests.exceptions.ConnectionError, requests.exceptions.Timeout]
        for test_case in test_cases:
            get.side_effect = test_case

            # Call the function under test again.
            try:
                self.token_validator._get_json_web_keyset()
            except Exception as e:
                self.assertIsInstance(
                    e,
                    UnableMeetPrerequisitesError,
                    "unexpected exception type when the OIDC configuration cannot be fetched",
                )

                self.assertEqual(
                    "unable to fetch http://localhost:999/auth/realms/redhat-external/.well-known/openid-configuration "
                    "to validate the token",
                    str(e),
                    "unexpected error message for the exception",
                )

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.jwks_source.JWKSCache.set_jwks_response")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset_oidc_not_ok(
        self, import_key_set: mock.Mock, set_jwks_response: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that when we are unable to fetch the OIDC configuration an exception is raised"""
        # Make the cache raise an exception to simulate that something went wrong with it. It should trigger having to
        # fetch the data from IT instead.
        get_jwks_response.side_effect = Exception

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.side_effect = self._requests_get_sideffect

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # The "set cache" method should have been called to store certificate contents in cache.
        set_jwks_response.assert_called_with(self.jwks_certificates_response_json)

        # And the "import key set" method should also have been called with the same contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

        # After a successful run, simulate a bad status code response from IT.
        get.return_value = mock.Mock(status_code=status.HTTP_400_BAD_REQUEST)
        get.side_effect = None

        # Call the function under test again.
        try:
            self.token_validator._get_json_web_keyset()
        except Exception as e:
            self.assertIsInstance(
                e,
                UnableMeetPrerequisitesError,
                "unexpected exception type when the OIDC configuration cannot be fetched",
            )

            self.assertEqual(
                "unexpected status code '400' received from http://localhost:999/auth/realms/redhat-external/.well-known/openid-configuration "
                "when attempting to fetch the OIDC configuration",
                str(e),
                "unexpected error message for the exception",
            )

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.jwks_source.JWKSCache.set_jwks_response")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset_oidc_not_jwks_url(
        self, import_key_set: mock.Mock, set_jwks_response: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that when we are unable to find the JWKS URL an exception is raised"""
        # Make the cache raise an exception to simulate that something went wrong with it. It should trigger having to
        # fetch the data from IT instead.
        get_jwks_response.side_effect = Exception

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.side_effect = self._requests_get_sideffect

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # The "set cache" method should have been called to store certificate contents in cache.
        set_jwks_response.assert_called_with(self.jwks_certificates_response_json)

        # And the "import key set" method should also have been called with the same contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

        # After a successful run, simulate a bad status code response from IT.
        get.return_value = mock.Mock(status_code=status.HTTP_200_OK, json=lambda: {"unexpected": "contents"})
        get.side_effect = None

        # Call the function under test again.
        try:
            self.token_validator._get_json_web_keyset()
        except Exception as e:
            self.assertIsInstance(
                e,
                UnableMeetPrerequisitesError,
                "unexpected exception type when the OIDC configuration cannot be fetched",
            )

            self.assertEqual(
                'the "jwks_uri" key was not present in the response payload',
                str(e),
                "unexpected error message for the exception",
            )

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.jwks_source.JWKSCache.set_jwks_response")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset_oidc_empty_jwks_url(
        self, import_key_set: mock.Mock, set_jwks_response: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that when the JWKS URL is empty an exception is raised"""
        # Make the cache raise an exception to simulate that something went wrong with it. It should trigger having to
        # fetch the data from IT instead.
        get_jwks_response.side_effect = Exception

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.side_effect = self._requests_get_sideffect

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # The "set cache" method should have been called to store certificate contents in cache.
        set_jwks_response.assert_called_with(self.jwks_certificates_response_json)

        # And the "import key set" method should also have been called with the same contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

        # After a successful run, simulate a bad status code response from IT.
        get.return_value = mock.Mock(status_code=status.HTTP_200_OK, json=lambda: {"jwks_uri": None})
        get.side_effect = None

        # Call the function under test again.
        try:
            self.token_validator._get_json_web_keyset()
        except Exception as e:
            self.assertIsInstance(
                e,
                UnableMeetPrerequisitesError,
                "unexpected exception type when the OIDC configuration cannot be fetched",
            )

            self.assertEqual(
                'the "jwks_uri" key has an empty value',
                str(e),
                "unexpected error message for the exception",
            )

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.jwks_source.JWKSCache.set_jwks_response")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset_jwks_network_error(
        self, import_key_set: mock.Mock, set_jwks_response: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that when we suffer from a network error when fetching the JWKS certificates an exception is raised"""
        # Make the cache raise an exception to simulate that something went wrong with it. It should trigger having to
        # fetch the data from IT instead.
        get_jwks_response.side_effect = Exception

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.side_effect = self._requests_get_sideffect

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # The "set cache" method should have been called to store certificate contents in cache.
        set_jwks_response.assert_called_with(self.jwks_certificates_response_json)

        # And the "import key set" method should also have been called with the same contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

        # After a successful run, simulate a bad status code response from IT.
        get.side_effect = self._requests_get_sideffect_jwks_bad_response

        # After a successful run, simulate a network error with IT.
        test_cases = [
            self._requests_get_sideffect_jwks_connection_error,
            self._requests_get_sideffect_jwks_timeout_error,
        ]
        for test_case in test_cases:
            get.side_effect = test_case

            # Call the function under test again.
            try:
                self.token_validator._get_json_web_keyset()
            except Exception as e:
                self.assertIsInstance(
                    e,
                    UnableMeetPrerequisitesError,
                    "unexpected exception type when the OIDC configuration cannot be fetched",
                )

                self.assertEqual(
                    "unable to fetch http://localhost/auth/realms/redhat-external/protocol/openid-connect/certs "
                    "to validate the token",
                    str(e),
                    "unexpected error message for the exception",
                )

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.jwks_source.JWKSCache.set_jwks_response")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset_jwks_not_ok(
        self, import_key_set: mock.Mock, set_jwks_response: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that when we are unable to fetch the JWKS certificates an exception is raised"""
        # Make the cache raise an exception to simulate that something went wrong with it. It should trigger having to
        # fetch the data from IT instead.
        get_jwks_response.side_effect = Exception

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.side_effect = self._requests_get_sideffect

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # The "set cache" method should have been called to store certificate contents in cache.
        set_jwks_response.assert_called_with(self.jwks_certificates_response_json)

        # And the "import key set" method should also have been called with the same contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

        # After a successful run, simulate a bad status code response from IT.
        get.side_effect = self._requests_get_sideffect_jwks_bad_response

        # Call the function under test again.
        try:
            self.token_validator._get_json_web_keyset()
        except Exception as e:
            self.assertIsInstance(
                e,
                UnableMeetPrerequisitesError,
                "unexpected exception type when the OIDC configuration cannot be fetched",
            )

            self.assertEqual(
                "unexpected status code '400' received from http://localhost/auth/realms/redhat-external/protocol/openid-connect/certs "
                "when attempting to fetch the OIDC configuration",
                str(e),
                "unexpected error message for the exception",
            )

    @mock.patch("management.authorization.jwks_source.JWKSCache.get_jwks_response")
    @mock.patch("management.authorization.jwks_source.requests.get")
    @mock.patch("management.authorization.jwks_source.JWKSCache.set_jwks_response")
    @mock.patch("management.authorization.token_validator.KeySet.import_key_set")
    def test_get_json_web_keyset_import_key_set_error(
        self, import_key_set: mock.Mock, set_jwks_response: mock.Mock, get: mock.Mock, get_jwks_response: mock.Mock
    ) -> None:
        """Tests that when we are unable to import the JWKS certificates an exception is raised"""
        # Make the cache raise an exception to simulate that something went wrong with it. It should trigger having to
        # fetch the data from IT instead.
        get_jwks_response.side_effect = Exception

        # The "get" method from the "requests" package should not have been called since in theory we have loaded the
        # JWKS response from cache.
        get.side_effect = self._requests_get_sideffect

        # Call the function under test.
        self.token_validator._get_json_web_keyset()

        # The "set cache" method should have been called to store certificate contents in cache.
        set_jwks_response.assert_called_with(self.jwks_certificates_response_json)

        # And the "import key set" method should also have been called with the same contents.
        import_key_set.assert_called_with(self.jwks_certificates_response_json)

        # After a successful run, simulate that importing the key set fails.
        import_key_set.side_effect = Exception

        # Call the function under test again.
        try:
            self.token_validator._get_json_web_keyset()
        except Exception as e:
            self.assertIsInstance(
                e,
                UnableMeetPrerequisitesError,
                "unexpected exception type when the OIDC configuration cannot be fetched",
            )

            self.assertEqual(
                "unable to import IT's public keys to validate the token",
                str(e),
                "unexpected error message for the exception",
            )

    @override_settings(IT_BYPASS_TOKEN_VALIDATION=True)
    def test_validate_token_bypass_validation(self) -> None:
        """Test that when bypassing token validation a hardcoded value is returned for it."""
        self.assertEqual(
            "mocked-invalid-bearer-token-because-token-validation-is-disabled",
            self.token_validator.validate_token(request=mock.Mock(), additional_scopes_to_validate=set()),
            "a hard coded value should have been returned since the token validation is bypassed",
        )

    def test_validate_token_missing_authorization_header(self) -> None:
        """Test that a request without the authorization header raises an exception."""
        request = mock.Mock
        request.headers = {}

        try:
            self.token_validator.validate_token(request=request, additional_scopes_to_validate=set())
            self.fail("the validate token method should have raised an exception due to an empty authorization header")
        except Exception as e:
            self.assertIsInstance(
                e,
                MissingAuthorizationError,
                "unexpected exception type when providing a request without the authorization header",
            )

    @mock.patch("management.authorization.token_validator.ITSSOTokenValidator._get_json_web_keyset")
    @mock.patch("management.authorization.token_validator.jwt.decode")
    def test_validate_token_bearer_token_correctly_extracted(
        self, decode: mock.Mock, _get_json_web_keyset: mock.Mock
    ) -> None:
        """Test that the bearer token is correctly extracted from the authorization header."""
        test_cases = [
            {
                "token": "Bearer abcde",
                "expected_parsed_value": "abcde",
            },
            {
                "token": "fghij",
                "expected_parsed_value": "fghij",
            },
        ]

        # Prepare a mocked returned key set to verify that the decode function is called with the expected arguments.
        _get_json_web_keyset.return_value = {}

        for test_case in test_cases:
            request = mock.Mock
            request.headers = {"Authorization": test_case["token"]}

            # The function under test is going to raise an exception for having invalid claims in the token, but we do
            # not care because we are testing another thing.
            try:
                self.token_validator.validate_token(request=request, additional_scopes_to_validate=set())
            except Exception:
                pass

            decode.assert_called_with(value=test_case["expected_parsed_value"], key={})

    @mock.patch("management.authorization.token_validator.ITSSOTokenValidator._get_json_web_keyset")
    @mock.patch("management.authorization.token_validator.jwt.decode")
    def test_validate_token_decoding_exception(self, decode: mock.Mock, _get_json_web_keyset: mock.Mock) -> None:
        """Test that any exception that occurs when decoding the token is properly handled."""
        # Prepare a request with a mocked bearer token.
        token_value = "mocked-token"

        request = mock.Mock
        request.headers = {"Authorization": token_value}

        # Raise an exception when the token is attempted to be decoded.
        decode.side_effect = Exception

        # The function under test is going to raise an exception for having invalid claims in the token, but we do
        # not care because we are testing another thing.
        try:
            self.token_validator.validate_token(request=request, additional_scopes_to_validate=set())
        except Exception as e:
            self.assertIsInstance(
                e,
                InvalidTokenError,
                "unexpected exception type when handling an exception raised by the decode function",
            )

            self.assertEqual("Unable to decode token", str(e), "unexpected exception error message")

    @mock.patch("management.authorization.token_validator.ITSSOTokenValidator._get_json_web_keyset")
    @mock.patch("management.authorization.token_validator.jwt.decode")
    def test_validate_token_bearer_token_invalid_scope_claim(
        self, decode: mock.Mock, _get_json_web_keyset: mock.Mock
    ) -> None:
        """Test that the token validation fails when the token has an invalid scope claim."""
        # Prepare a mocked returned key set to verify that the decode function is called with the expected arguments.
        _get_json_web_keyset.return_value = {}

        # Prepare a request with a mocked bearer token.
        token_value = "mocked-token"

        request = mock.Mock
        request.headers = {"Authorization": f"Bearer {token_value}"}

        # Prepare a mocked Token for the decode function to return it.
        token = mock.Mock()
        token.claims = {
            "iss": self.issuer,
            "scope": ScopeClaims.SERVICE_ACCOUNTS_CLAIM,
        }

        decode.return_value = token

        # Make a first call to the function under test to make sure that having correct claims makes it work as
        # expected.
        self.assertEqual(
            token_value,
            self.token_validator.validate_token(
                request=request, additional_scopes_to_validate=set(ScopeClaims.SERVICE_ACCOUNTS_CLAIM)
            ),
            "unexpected extracted value for the bearer token",
        )

        # Verify that the decode function was called as expected.
        decode.assert_called_with(value=token_value, key={})

        # Modify the scope token claim to an invalid value.
        token.claims["scope"] = "a-different-scope but-definitely not-the-service-accounts one"

        decode.return_value = token

        # Call the function under test again to make sure that the invalid token claim is identified.
        try:
            self.token_validator.validate_token(
                request=request, additional_scopes_to_validate=set(ScopeClaims.SERVICE_ACCOUNTS_CLAIM)
            )
            self.fail("the invalid scope claim should have raised an exception")
        except Exception as e:
            self.assertIsInstance(
                e,
                InvalidTokenError,
                "unexpected exception raised when an invalid scope claim is present in the token",
            )

            self.assertEqual("The token's claims are invalid", str(e), "unexpected exception message")

    @mock.patch("management.authorization.token_validator.ITSSOTokenValidator._get_json_web_keyset")
    @mock.patch("management.authorization.token_validator.jwt.decode")
    def test_validate_token_bearer_token_invalid_issuer_claim(
        self, decode: mock.Mock, _get_json_web_keyset: mock.Mock
    ) -> None:
        """Test that the token validation fails when the token has an invalid issuer claim."""
        # Prepare a mocked returned key set to verify that the decode function is called with the expected arguments.
        _get_json_web_keyset.return_value = {}

        # Prepare a request with a mocked bearer token.
        token_value = "mocked-token"

        request = mock.Mock
        request.headers = {"Authorization": f"Bearer {token_value}"}

        # Prepare a mocked Token for the decode function to return it.
        token = mock.Mock()
        token.claims = {
            "iss": self.issuer,
            "scope": ScopeClaims.SERVICE_ACCOUNTS_CLAIM,
        }

        decode.return_value = token

        # Make a first call to the function under test to make sure that having correct claims makes it work as
        # expected.
        self.assertEqual(
            token_value,
            self.token_validator.validate_token(request=request, additional_scopes_to_validate=set()),
            "unexpected extracted value for the bearer token",
        )

        # Verify that the decode function was called as expected.
        decode.assert_called_with(value=token_value, key={})

        # Modify the issuer token claim to an invalid value.
        token.claims["iss"] = "invalid-value"

        decode.return_value = token

        # Call the function under test again to make sure that the invalid token claim is identified.
        try:
            self.token_validator.validate_token(request=request, additional_scopes_to_validate=set())
            self.fail("the invalid issuer claim should have raised an exception")
        except Exception as e:
            self.assertIsInstance(
                e,
                InvalidTokenError,
                "unexpected exception raised when an invalid issuer claim is present in the token",
            )

            self.assertEqual("The token's claims are invalid", str(e), "unexpected exception message")

    def test_get_user_from_bearer_token_parses_claims(self):
        """Test that the user is correctly built from the bearer token claims."""
        # Override jwks source to use the in-memory issuer for testing.
        issuer = InMemoryIssuer.generate()
        self.token_validator.set_jwks_source(issuer, issuer.iss)

        for claims, expected_user in [
            (
                {
                    "sub": "u1",
                    "preferred_username": "user1",
                    "organization": {
                        "id": "org1",
                    },
                    "client_id": "client1",
                    "roles": ["org:admin:all"],
                },
                User(
                    user_id="u1",
                    username="user1",
                    org_id="org1",
                    client_id="client1",
                    admin=True,
                ),
            ),
            (
                {
                    "sub": "u2",
                    "preferred_username": "user2",
                    "organization": {
                        "id": "org2",
                    },
                    "client_id": "client2",
                },
                User(
                    user_id="u2",
                    username="user2",
                    org_id="org2",
                    client_id="client2",
                ),
            ),
            (
                {
                    "sub": "u2",
                    "preferred_username": "user2",
                },
                User(
                    user_id="u2",
                    username="user2",
                ),
            ),
        ]:
            with self.subTest(sub=claims["sub"]):
                token = issuer.issue_jwt(
                    {},
                    claims,
                )
                expected_user.bearer_token = token

                request_factory = RequestFactory()
                request = request_factory.get(
                    "/",
                    HTTP_AUTHORIZATION=f"Bearer {token}",
                )

                user = self.token_validator.get_user_from_bearer_token(request)

                self.assertEqual(expected_user, user)

    def test_does_not_parse_user_from_invalid_token(self):
        issuer = InMemoryIssuer.generate()
        self.token_validator.set_jwks_source(issuer, issuer.iss)
        # Generate from a different issuer to simulate an invalid token
        token = InMemoryIssuer.generate().issue_jwt(
            {},
            {
                "sub": "u1",
                "preferred_username": "user1",
                "organization": {
                    "id": "org1",
                },
                "client_id": "client1",
            },
        )

        request_factory = RequestFactory()
        request = request_factory.get(
            "/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        with self.assertRaises(InvalidTokenError):
            self.token_validator.get_user_from_bearer_token(request)
