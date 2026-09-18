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
"""Test the principal proxy."""

from unittest.mock import patch

from django.conf import settings
from rest_framework import status
import requests

from api.models import Tenant
from management.principal.model import Principal
from management.principal.proxy import PrincipalProxy
from tests.identity_request import IdentityRequest


class MockResponse:  # pylint: disable=too-few-public-methods
    """Mock response object for testing."""

    def __init__(self, url, json_data, status_code, *, exception=None):
        """Create object."""
        self.url = url
        self.json_data = json_data
        self.status_code = status_code
        self.exception = exception

    def json(self):
        """Return json data."""
        if self.exception:
            raise self.exception
        return self.json_data


def mocked_requests_get_404_json(url, *args, **kwargs):  # pylint: disable=unused-argument
    """Mock invalid response that returns json."""
    json_response = {"details": "Invalid path."}
    return MockResponse(url, json_response, status.HTTP_404_NOT_FOUND)


def mocked_requests_get_500_json(url, *args, **kwargs):  # pylint: disable=unused-argument
    """Mock invalid response that returns json."""
    json_response = {"details": "Internal server error."}
    return MockResponse(url, json_response, status.HTTP_500_INTERNAL_SERVER_ERROR)


def mocked_requests_get_500_except(url, *args, **kwargs):  # pylint: disable=unused-argument
    """Mock invalid response that raises an exception."""
    raise requests.exceptions.ConnectionError()


def mocked_requests_get_timeout(url, *args, **kwargs):  # pylint: disable=unused-argument
    """Mock response that raises a timeout exception."""
    raise requests.exceptions.Timeout()


def mocked_requests_get_200_json(url, *args, **kwargs):  # pylint: disable=unused-argument
    """Mock valid response that returns json."""
    user = {
        "username": "test_user1",
        "email": "test_user1@email.foo",
        "first_name": "test",
        "last_name": "user1",
        "is_active": "true",
        "is_org_admin": "true",
        "id": "3",
        "org_id": "org_1",
    }
    json_response = [user]
    return MockResponse(url, json_response, status.HTTP_200_OK)


def mocked_requests_get_200_json_count(url, *args, **kwargs):  # pylint: disable=unused-argument
    """Mock valid response that returns json with userCount."""
    user1 = {
        "username": "test_user1",
        "email": "test_user1@email.foo",
        "first_name": "test",
        "last_name": "user1",
        "is_active": "true",
        "is_org_admin": "true",
        "id": "1",
        "org_id": "org_1",
    }
    user2 = {
        "username": "test_user2",
        "email": "test_user2@email.foo",
        "first_name": "test",
        "last_name": "user2",
        "is_active": "true",
        "is_org_admin": "false",
        "id": "2",
        "org_id": "org_2",
    }
    json_response = {"userCount": 2, "users": [user1, user2]}
    return MockResponse(url, json_response, status.HTTP_200_OK)


def mocked_requests_get_200_except(url, *args, **kwargs):  # pylint: disable=unused-argument
    """Mock valid response that returns exception on json."""
    json_response = {}
    return MockResponse(url, json_response, status.HTTP_200_OK, exception=ValueError)


class PrincipalProxyTest(IdentityRequest):
    """Test PrincipalProxy object."""

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={"status_code": status.HTTP_200_OK, "data": []},
    )
    def test_request_principals(self, mock_request):
        """Test the call to request principals."""
        proxy = PrincipalProxy()
        result = proxy.request_principals(org_id="1234", limit=20, offset=10)
        expected = {"status_code": status.HTTP_200_OK, "data": []}
        self.assertEqual(expected, result)

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={"status_code": status.HTTP_200_OK, "data": []},
    )
    def test_request_filtered_principals(self, mock_request):
        """Test the call to request filtered principals."""
        proxy = PrincipalProxy()
        result = proxy.request_filtered_principals(principals=["test_user"], org_id="1234")
        expected = {"status_code": status.HTTP_200_OK, "data": []}
        self.assertEqual(expected, result)

    def test_request_filtered_principals_empty(self):
        """Test the call to request filtered principals."""
        proxy = PrincipalProxy()
        result = proxy.request_filtered_principals(principals=[], org_id="1234")
        expected = {"status_code": status.HTTP_200_OK, "data": []}
        self.assertEqual(expected, result)

    def test__request_principals_404(self):
        """Test request with expected 404."""
        proxy = PrincipalProxy()
        result = proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_404_json)
        expected = {"status_code": 404, "errors": [{"detail": "Not Found.", "status": "404", "source": "principals"}]}
        self.assertEqual(expected, result)

    def test__request_principals_500(self):
        """Test request with expected 500."""
        proxy = PrincipalProxy()
        result = proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_500_json)
        expected = {
            "status_code": 500,
            "errors": [{"detail": "Unexpected error.", "status": "500", "source": "principals"}],
        }
        self.assertEqual(expected, result)

    def test__request_principals_500_except(self):
        """Test request with expected 500 on connection exception."""
        proxy = PrincipalProxy()
        result = proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_500_except)
        expected = {
            "status_code": 500,
            "errors": [{"detail": "Unexpected error.", "status": "500", "source": "principals"}],
        }
        self.assertEqual(expected, result)

    def test__request_principals_timeout(self):
        """Test request with expected 500 on timeout exception."""
        proxy = PrincipalProxy()
        result = proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_timeout)
        expected = {
            "status_code": 500,
            "errors": [{"detail": "Unexpected error.", "status": "500", "source": "principals"}],
        }
        self.assertEqual(expected, result)

    def test__request_principals_passes_timeout_kwarg(self):
        """Test that _request_principals passes timeout in kwargs."""
        proxy = PrincipalProxy()
        captured_kwargs = {}

        def capture_method(url, **kwargs):
            captured_kwargs.update(kwargs)
            return MockResponse(url, [], status.HTTP_200_OK)

        result = proxy._request_principals(url="http://localhost:8080/v1/users", method=capture_method)
        self.assertIn("timeout", captured_kwargs)
        self.assertEqual(captured_kwargs["timeout"], settings.OUTBOUND_HTTP_TIMEOUT)
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["data"], [])

    @patch("management.principal.proxy.requests.post")
    def test_fetch_account_org_mapping_passes_timeout(self, mock_post):
        """Test that fetch_account_org_mapping passes timeout in kwargs."""
        mock_post.return_value = MockResponse("http://test", {"acc1": "org1"}, status.HTTP_200_OK)
        proxy = PrincipalProxy()
        result = proxy.fetch_account_org_mapping(["acc1"])
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        self.assertIn("timeout", call_kwargs)
        self.assertEqual(call_kwargs["timeout"], settings.OUTBOUND_HTTP_TIMEOUT)
        self.assertEqual(result, {"acc1": "org1"})

    def test__request_principals_200_success(self):
        """Test request with expected 200 and good data."""
        proxy = PrincipalProxy()
        result = proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_200_json)
        user = {
            "username": "test_user1",
            "email": "test_user1@email.foo",
            "first_name": "test",
            "last_name": "user1",
            "is_active": "true",
            "is_org_admin": "true",
            "external_source_id": "3",
            "org_id": "org_1",
        }
        expected = {"data": [user], "status_code": 200}
        self.assertEqual(expected, result)

    def test__request_principals_200_count(self):
        """Test request with 200 and good data including userCount."""
        proxy = PrincipalProxy()
        result = proxy._request_principals(
            url="http://localhost:8080/v1/users", method=mocked_requests_get_200_json_count
        )
        user1 = {
            "username": "test_user1",
            "email": "test_user1@email.foo",
            "first_name": "test",
            "last_name": "user1",
            "is_active": "true",
            "is_org_admin": "true",
            "external_source_id": "1",
            "org_id": "org_1",
        }
        user2 = {
            "username": "test_user2",
            "email": "test_user2@email.foo",
            "first_name": "test",
            "last_name": "user2",
            "is_active": "true",
            "is_org_admin": "false",
            "external_source_id": "2",
            "org_id": "org_2",
        }
        expected = {"data": {"userCount": 2, "users": [user1, user2]}, "status_code": 200}
        self.assertEqual(expected, result)

    def test__request_principals_200_fail(self):
        """Test request with expected 200 and bad data."""
        proxy = PrincipalProxy()
        result = proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_200_except)
        expected = {
            "status_code": 500,
            "errors": [{"detail": "Unexpected error.", "status": "500", "source": "principals"}],
        }
        self.assertEqual(expected, result)

    def test__request_principals_username_only(self):
        """Test the request with 'username_only=true' in params returns only usernames from db."""
        proxy = PrincipalProxy()

        # Tenant A with 2 principals + 1 cross account principal
        # Tenant B with 1 principal + 1 cross account principal
        tenantA = Tenant.objects.create(tenant_name="tenantA", account_id=11111, org_id=11111)
        Principal.objects.create(tenant=tenantA, username="user1")
        Principal.objects.create(tenant=tenantA, username="user2")
        Principal.objects.create(tenant=tenantA, username="cross-account-principal_A", cross_account=True)

        self.assertEqual(len(Principal.objects.filter(tenant=tenantA)), 3)

        tenantB = Tenant.objects.create(tenant_name="tenantB", account_id=22222, org_id=22222)
        Principal.objects.create(tenant=tenantB, username="user3")
        Principal.objects.create(tenant=tenantB, username="cross-account-principal_B", cross_account=True)

        self.assertEqual(len(Principal.objects.filter(tenant=tenantB)), 2)

        params = {"username_only": "true"}
        # URL and METHOD not needed for this request type but required by method
        result = proxy._request_principals(
            org_id=tenantA.org_id, params=params, method=mocked_requests_get_200_except, url="xxx"
        )

        # Expected result = only tenant A principals in the response
        self.assertIsInstance(result, dict)
        for key in ["data", "userCount", "status_code"]:
            self.assertIn(key, result)
        self.assertEqual(result.get("status_code"), 200)
        self.assertEqual(result.get("userCount"), 2)
        self.assertEqual(len(result.get("data")), 2)
        usernames = [v.get("username") for v in result.get("data")]
        usernames.sort()
        expected = ["user1", "user2"]
        self.assertEqual(usernames, expected)

    @patch("management.principal.proxy.LOGGER")
    def test__request_principals_200_info_log_no_pii_list_response(self, mock_logger):
        """Test INFO log does not contain PII fields from BOP list response."""
        proxy = PrincipalProxy()
        proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_200_json)

        mock_logger.info.assert_called_once()
        log_msg = mock_logger.info.call_args[0][0] % mock_logger.info.call_args[0][1:]
        pii_fields = ["test_user1@email.foo", "first_name", "last_name", "account_number", "address_string"]
        for pii in pii_fields:
            self.assertNotIn(pii, log_msg, f"INFO log should not contain PII field: {pii}")
        # Username is allowed — verify it IS present
        self.assertIn("test_user1", log_msg)

    @patch("management.principal.proxy.LOGGER")
    def test__request_principals_200_info_log_no_pii_dict_response(self, mock_logger):
        """Test INFO log does not contain PII fields from BOP dict response (with userCount)."""
        proxy = PrincipalProxy()
        proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_200_json_count)

        mock_logger.info.assert_called_once()
        log_msg = mock_logger.info.call_args[0][0] % mock_logger.info.call_args[0][1:]
        pii_fields = ["test_user1@email.foo", "test_user2@email.foo"]
        for pii in pii_fields:
            self.assertNotIn(pii, log_msg, f"INFO log should not contain PII field: {pii}")
        # Usernames and user_count are allowed
        self.assertIn("test_user1", log_msg)
        self.assertIn("test_user2", log_msg)
        self.assertIn("user_count=2", log_msg)

    @patch("management.principal.proxy.LOGGER")
    def test__request_principals_200_debug_log_has_full_response(self, mock_logger):
        """Test DEBUG log contains the full BOP response for debugging."""
        proxy = PrincipalProxy()
        proxy._request_principals(url="http://localhost:8080/v1/users", method=mocked_requests_get_200_json)

        mock_logger.debug.assert_called_once()
        debug_msg = mock_logger.debug.call_args[0][0] % mock_logger.debug.call_args[0][1:]
        # Full response should be in DEBUG log
        self.assertIn("test_user1@email.foo", debug_msg, "DEBUG log should contain full BOP response")
