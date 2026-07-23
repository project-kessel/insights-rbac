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
"""Test the principal model."""

import requests
import uuid

from django.conf import settings
from django.test import override_settings

from management.group.model import Group
from management.principal.model import Principal
from management.principal.it_service import ITService, UnexpectedStatusCodeFromITError
from rest_framework import serializers, status
from tests.identity_request import IdentityRequest
from unittest import mock

from api.models import User, Tenant

# IT path to fetch the service accounts.
IT_PATH_GET_SERVICE_ACCOUNTS = "/service_accounts/v1"

# Keys for the "options" dictionary. The "options" dictionary represents the query parameters passed by the calling
# client.
SERVICE_ACCOUNT_DESCRIPTION_KEY = "service_account_description"
SERVICE_ACCOUNT_NAME_KEY = "service_account_name"


class ITServiceTests(IdentityRequest):
    """Test the IT service class"""

    def setUp(self):
        # Set up some settings so that the class builds IT's URL.
        settings.IT_SERVICE_HOST = "localhost"
        settings.IT_SERVICE_BASE_PATH = "/"
        settings.IT_SERVICE_PORT = "999"
        settings.IT_SERVICE_PROTOCOL_SCHEME = "http"
        settings.IT_SERVICE_TIMEOUT_SECONDS = 10

        self.it_service = ITService()

    def test_it_service_singleton(self):
        """Test that the IT Service class only gets instantiated once."""
        class_instances = [
            ITService(),
            ITService(),
            ITService(),
            ITService(),
            ITService(),
        ]

        for instance in class_instances:
            self.assertEqual(
                self.it_service,
                instance,
                "no new instances of the IT service class should have been created since it is supposed to be a singleton",
            )

    def _create_mock_it_service_accounts(self, number: int) -> list[dict[str, str]]:
        """Create mock service accounts as returned by IT."""
        service_accounts: list[dict] = []
        for i in range(number):
            client_id = str(uuid.uuid4())

            service_accounts.append(
                {
                    "clientId": client_id,
                    "name": f"name-{client_id}",
                    "description": f"description-{client_id}",
                    "createdBy": f"createdBy-{client_id}",
                    "createdAt": f"createdAt-{client_id}",
                }
            )

        return service_accounts

    def _create_database_service_account_principals(self, tenant: Tenant, number: int) -> list[Principal]:
        """Create service account principals in the database"""
        created_service_account_principals: list[Principal] = []
        for i in range(number):
            client_id = str(uuid.uuid4())
            created_service_account_principals.append(
                Principal.objects.create(
                    username=f"service-account-{client_id}",
                    service_account_id=client_id,
                    type=Principal.Types.SERVICE_ACCOUNT,
                    tenant=tenant,
                )
            )

        return created_service_account_principals

    def _create_database_service_account_principals_two_tenants(self) -> (list[Principal], list[Principal]):
        """Create service account principals with the "self.tenant" and a new tenant"""
        # Create the service accounts we expect to work with throughout the test.
        tenant_one_service_account_number = 5
        tenant_one_service_accounts = self._create_database_service_account_principals(
            tenant=self.tenant, number=tenant_one_service_account_number
        )

        # Create another tenant and associate some service accounts to it, to make sure that the function under test
        # only fetches the ones associated with the above user.
        another_tenant = Tenant()
        another_tenant.account_id = "12345-another-account-id"
        another_tenant.org_id = "12345-another-org-id"
        another_tenant.tenant_name = "another tenant"
        another_tenant.save()

        tenant_another_service_accounts_number = 7
        tenant_two_service_accounts = self._create_database_service_account_principals(
            tenant=another_tenant, number=tenant_another_service_accounts_number
        )

        return tenant_one_service_accounts, tenant_two_service_accounts

    def _create_two_rbac_groups_with_service_accounts(self) -> (Group, Group):
        """Create two RBAC groups with two and three service associated accounts respectively."""
        tenant_one_service_accounts, _ = self._create_database_service_account_principals_two_tenants()
        self.assertEqual(
            5,
            len(tenant_one_service_accounts),
            "unexpected number of service accounts created",
        )

        # Create a group which will hold the service accounts we are going to perform assertions with.
        group_a: Group = Group.objects.create(name="group_a", tenant=self.tenant)

        group_a.principals.add(tenant_one_service_accounts[0])
        group_a.principals.add(tenant_one_service_accounts[1])
        group_a.save()

        # Create another group to double-check that service accounts do not get mixed.
        group_b: Group = Group.objects.create(name="group_b", tenant=self.tenant)
        group_b.principals.add(tenant_one_service_accounts[2])
        group_b.principals.add(tenant_one_service_accounts[3])
        group_b.principals.add(tenant_one_service_accounts[4])
        group_b.save()

        return group_a, group_b

    def _assert_created_sa_and_result_are_same(
        self, created_database_sa_principals: list[Principal], function_result: list[dict]
    ) -> None:
        """Assert that the "get_service_accounts" function's result is correct

        The assertions make sure that the returned results from the function actually match the created service
        account principals in the test.
        """

        # Make it easier to find the service accounts by their client ID.
        result_sas_by_client_id: dict[str[dict, str]] = {}
        for sa in function_result:
            result_sas_by_client_id[sa["clientId"]] = sa

        # Assert that the only service accounts in the result are the ones from the tenant associated to the user
        # that was passed to the function under test.
        for sa_principal in created_database_sa_principals:
            sa = result_sas_by_client_id.get(sa_principal.service_account_id)
            if not sa:
                self.fail(
                    f"the service account principal {sa_principal} was not found in the resulting list of service"
                    f" accounts {result_sas_by_client_id}"
                )

            self.assertEqual(
                sa_principal.service_account_id,
                sa["clientId"],
                "the mocked service account's client ID should be the service account principal's ID",
            )

            self.assertEqual(
                f"{sa_principal.service_account_id}-name",
                sa["name"],
                "the mocked service account's name is unexpected",
            )

            self.assertEqual(
                f"{sa_principal.service_account_id}-description",
                sa["description"],
                "the mocked service account's description is unexpected",
            )

            self.assertEqual(
                sa_principal.username,
                sa["owner"],
                'the mocked service account\'s "owner" field is unexpected',
            )

        self.assertEqual(
            True,
            "time_created" in sa,
            'the mocked service account does not have the expected "time_created" field',
        )

        self.assertEqual(
            Principal.Types.SERVICE_ACCOUNT,
            sa["type"],
            "the mocked service account does not have the expected type",
        )

    def _assert_IT_to_RBAC_model_transformations(
        self, it_service_accounts: list[dict[str, str]], rbac_service_accounts: list[dict[str, str]]
    ) -> None:
        """Assert that the service accounts coming from IT were correctly transformed into our model"""
        # Rearrange RBAC's service accounts by client ID for an easier search later on.
        rbac_service_accounts_by_cid: dict[str, dict[str, str]] = {}
        for rbac_sa in rbac_service_accounts:
            rbac_sa_cid = rbac_sa.get("clientId")
            if not rbac_sa_cid:
                self.fail(f'the transformed service account does not have the "clientId" property: {rbac_sa}')

            rbac_service_accounts_by_cid[rbac_sa_cid] = rbac_sa

        # Make all the assertions for the contents.
        for it_sa in it_service_accounts:
            client_id = it_sa.get("clientId")
            if not client_id:
                self.fail(f'the IT service account dictionary does not have the "clientId" property: {it_sa}')

            rbac_sa = rbac_service_accounts_by_cid.get(client_id)
            if not rbac_sa:
                self.fail(
                    f"the transformed RBAC service accounts do not contain a service account with client ID"
                    f' "{client_id}". RBAC service accounts: {rbac_service_accounts_by_cid}'
                )

            # Assert that the client IDs are the same.
            rbac_sa_client_id = rbac_sa.get("clientId")
            if not rbac_sa_client_id:
                self.fail(f'the transformed RBAC service account does not contain the "clientId" property: {rbac_sa}')

            self.assertEqual(rbac_sa_client_id, client_id, "the client IDs for the RBAC and IT models do not match")

            # Assert that the names are the same.
            rbac_sa_name = rbac_sa.get("name")
            if not rbac_sa_name:
                self.fail(f'the transformed RBAC service account does not contain the "name" property: {rbac_sa}')

            it_sa_name = it_sa.get("name")
            if not it_sa_name:
                self.fail(f'the IT service account does not contain the "name" property: {it_sa}')

            self.assertEqual(rbac_sa_name, it_sa_name, "the names for the RBAC and IT models do not match")

            # Assert that the descriptions are the same.
            rbac_sa_description = rbac_sa.get("description")
            if not rbac_sa_description:
                self.fail(
                    f'the transformed RBAC service account does not contain the "description" property: {rbac_sa}'
                )

            it_sa_description = it_sa.get("description")
            if not it_sa_description:
                self.fail(f'the IT service account does not contain the "description" property: {it_sa}')

            self.assertEqual(
                rbac_sa_description, it_sa_description, "the descriptions for the RBAC and IT models do not match"
            )

            # Assert that the created by fields are the same.
            rbac_sa_created_by = rbac_sa.get("owner")
            if not rbac_sa_created_by:
                self.fail(f'the transformed RBAC service account does not contain the "owner" property: {rbac_sa}')

            it_sa_created_by = it_sa.get("createdBy")
            if not it_sa_created_by:
                self.fail(f'the IT service account does not contain the "createdBy" property: {it_sa}')

            self.assertEqual(
                rbac_sa_created_by,
                it_sa_created_by,
                "the owner and created by fields for the RBAC and IT models do not match",
            )

            # Assert that the created at fields are the same.
            rbac_sa_created_at = rbac_sa.get("time_created")
            if not rbac_sa_created_at:
                self.fail(
                    f'the transformed RBAC service account does not contain the "time_created" property: {rbac_sa}'
                )

            it_sa_created_at = it_sa.get("createdAt")
            if not it_sa_created_at:
                self.fail(f'the IT service account does not contain the "createdBy" property: {it_sa}')

            self.assertEqual(
                rbac_sa_created_at,
                it_sa_created_at,
                "the time created and created at fields for the RBAC and IT models do not match",
            )

    @mock.patch("management.principal.it_service.requests.get")
    def test_request_service_accounts_single_page(self, get: mock.Mock):
        """Test that the function under test can handle fetching a single page of service accounts from IT"""
        # Create the mocked response from IT.
        mocked_service_accounts = self._create_mock_it_service_accounts(5)

        get.__name__ = "get"
        get.return_value = mock.Mock(
            json=lambda: mocked_service_accounts,
            status_code=status.HTTP_200_OK,
        )

        bearer_token_mock = "bearer-token-mock"
        client_ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]

        # Call the function under test.
        result: list[dict] = self.it_service.request_service_accounts(
            bearer_token=bearer_token_mock, client_ids=client_ids
        )

        # Build IT's URL for the function call's assertion.
        it_url = (
            f"{settings.IT_SERVICE_PROTOCOL_SCHEME}://{settings.IT_SERVICE_HOST}:{settings.IT_SERVICE_PORT}"
            f"{settings.IT_SERVICE_BASE_PATH}{IT_PATH_GET_SERVICE_ACCOUNTS}"
        )

        # Build the expected parameters to be seen in the "get" function's assertion call.
        parameters = {"first": 0, "max": 100, "clientId": client_ids}

        # Assert that the "get" function was called with the expected arguments.
        get.assert_called_with(
            url=it_url,
            headers={"Authorization": f"Bearer {bearer_token_mock}"},
            params=parameters,
            timeout=settings.IT_SERVICE_TIMEOUT_SECONDS,
        )

        # Assert that the payload is correct.
        self._assert_IT_to_RBAC_model_transformations(
            it_service_accounts=mocked_service_accounts, rbac_service_accounts=result
        )

    @mock.patch("management.principal.it_service.requests.get")
    def test_request_service_accounts_multiple_pages(self, get: mock.Mock):
        """Test that the function under test can handle fetching multiple pages from IT"""
        # Create the mocked response from IT.
        mocked_service_accounts = self._create_mock_it_service_accounts(300)

        # Make sure the "get" function returns multiple pages of service accounts.
        first_hundred_sas = mocked_service_accounts[0:100]
        second_hundred_sas = mocked_service_accounts[100:200]
        third_hundred_sas = mocked_service_accounts[200:300]

        get.__name__ = "get"
        get.side_effect = [
            mock.Mock(
                json=lambda: first_hundred_sas,
                status_code=status.HTTP_200_OK,
            ),
            mock.Mock(
                json=lambda: second_hundred_sas,
                status_code=status.HTTP_200_OK,
            ),
            mock.Mock(
                json=lambda: third_hundred_sas,
                status_code=status.HTTP_200_OK,
            ),
            mock.Mock(
                json=lambda: [],
                status_code=status.HTTP_200_OK,
            ),
        ]

        bearer_token_mock = "bearer-token-mock"
        # For multiple pages giving just three client IDs does not make sense, but we are going to give them anyway to
        # check that the parameter is included.
        client_ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]

        # Call the function under test.
        result: list[dict] = self.it_service.request_service_accounts(
            bearer_token=bearer_token_mock, client_ids=client_ids
        )

        # Build IT's URL for the function call's assertion.
        it_url = (
            f"{settings.IT_SERVICE_PROTOCOL_SCHEME}://{settings.IT_SERVICE_HOST}:{settings.IT_SERVICE_PORT}"
            f"{settings.IT_SERVICE_BASE_PATH}{IT_PATH_GET_SERVICE_ACCOUNTS}"
        )

        # Assert that the "get" function is called with the expected arguments for the multiple pages.
        parameters_first_call = {"first": 0, "max": 100, "clientId": client_ids}
        parameters_second_call = {"first": 100, "max": 100, "clientId": client_ids}
        parameters_third_call = {"first": 200, "max": 100, "clientId": client_ids}
        parameters_fourth_call = {"first": 300, "max": 100, "clientId": client_ids}

        get.assert_has_calls(
            [
                mock.call(
                    url=it_url,
                    headers={"Authorization": f"Bearer {bearer_token_mock}"},
                    params=parameters_first_call,
                    timeout=settings.IT_SERVICE_TIMEOUT_SECONDS,
                ),
                mock.call(
                    url=it_url,
                    headers={"Authorization": f"Bearer {bearer_token_mock}"},
                    params=parameters_second_call,
                    timeout=settings.IT_SERVICE_TIMEOUT_SECONDS,
                ),
                mock.call(
                    url=it_url,
                    headers={"Authorization": f"Bearer {bearer_token_mock}"},
                    params=parameters_third_call,
                    timeout=settings.IT_SERVICE_TIMEOUT_SECONDS,
                ),
                mock.call(
                    url=it_url,
                    headers={"Authorization": f"Bearer {bearer_token_mock}"},
                    params=parameters_fourth_call,
                    timeout=settings.IT_SERVICE_TIMEOUT_SECONDS,
                ),
            ]
        )

        # Assert that the payload is correct.
        self._assert_IT_to_RBAC_model_transformations(
            it_service_accounts=mocked_service_accounts, rbac_service_accounts=result
        )

    @mock.patch("management.principal.it_service.requests.get")
    def test_request_service_accounts_unexpected_status_code(self, get: mock.Mock):
        """Test that the function under test raises an exception when an unexpected status code is received from IT"""
        get.__name__ = "get"
        get.return_value = mock.Mock(
            json=[],
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

        bearer_token_mock = "bearer-token-mock"
        client_ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]

        # Call the function under test.
        try:
            self.it_service.request_service_accounts(bearer_token=bearer_token_mock, client_ids=client_ids)
            self.fail("the function under test should have raised an exception on an unexpected status code")
        except Exception as e:
            self.assertIsInstance(
                e,
                UnexpectedStatusCodeFromITError,
                "unexpected exception raised when the status code received from IT is unexpected",
            )

        # Build IT's URL for the function call's assertion.
        it_url = (
            f"{settings.IT_SERVICE_PROTOCOL_SCHEME}://{settings.IT_SERVICE_HOST}:{settings.IT_SERVICE_PORT}"
            f"{settings.IT_SERVICE_BASE_PATH}{IT_PATH_GET_SERVICE_ACCOUNTS}"
        )

        # Build the expected parameters to be seen in the "get" function's assertion call.
        parameters = {"first": 0, "max": 100, "clientId": client_ids}

        # Assert that the "get" function was called with the expected arguments.
        get.assert_called_with(
            url=it_url,
            headers={"Authorization": f"Bearer {bearer_token_mock}"},
            params=parameters,
            timeout=settings.IT_SERVICE_TIMEOUT_SECONDS,
        )

    @mock.patch("management.principal.it_service.requests.get")
    def test_request_service_accounts_connection_error(self, get: mock.Mock):
        """Test that the function under test raises an exception a connection error happens when connecting to IT"""
        get.__name__ = "get"
        get.side_effect = requests.exceptions.ConnectionError

        bearer_token_mock = "bearer-token-mock"
        client_ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]

        # Call the function under test.
        try:
            self.it_service.request_service_accounts(bearer_token=bearer_token_mock, client_ids=client_ids)
            self.fail(
                "the function under test should have raised an exception when hitting a connection error with IT"
            )
        except Exception as e:
            self.assertIsInstance(
                e,
                requests.exceptions.ConnectionError,
                "unexpected exception raised when there is a connection error to IT",
            )

        # Build IT's URL for the function call's assertion.
        it_url = (
            f"{settings.IT_SERVICE_PROTOCOL_SCHEME}://{settings.IT_SERVICE_HOST}:{settings.IT_SERVICE_PORT}"
            f"{settings.IT_SERVICE_BASE_PATH}{IT_PATH_GET_SERVICE_ACCOUNTS}"
        )

        # Build the expected parameters to be seen in the "get" function's assertion call.
        parameters = {"first": 0, "max": 100, "clientId": client_ids}

        # Assert that the "get" function was called with the expected arguments.
        get.assert_called_with(
            url=it_url,
            headers={"Authorization": f"Bearer {bearer_token_mock}"},
            params=parameters,
            timeout=settings.IT_SERVICE_TIMEOUT_SECONDS,
        )

    @mock.patch("management.principal.it_service.requests.get")
    def test_request_service_accounts_timeout(self, get: mock.Mock):
        """Test that the function under test raises an exception a connection error happens when connecting to IT"""
        get.__name__ = "get"
        get.side_effect = requests.exceptions.Timeout

        bearer_token_mock = "bearer-token-mock"
        client_ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]

        # Call the function under test.
        try:
            self.it_service.request_service_accounts(bearer_token=bearer_token_mock, client_ids=client_ids)
            self.fail("the function under test should have raised an exception when having a timeout with IT")
        except Exception as e:
            self.assertIsInstance(
                e,
                requests.exceptions.Timeout,
                "unexpected exception raised when there is a timeout with IT",
            )

        # Build IT's URL for the function call's assertion.
        it_url = (
            f"{settings.IT_SERVICE_PROTOCOL_SCHEME}://{settings.IT_SERVICE_HOST}:{settings.IT_SERVICE_PORT}"
            f"{settings.IT_SERVICE_BASE_PATH}{IT_PATH_GET_SERVICE_ACCOUNTS}"
        )

        # Build the expected parameters to be seen in the "get" function's assertion call.
        parameters = {"first": 0, "max": 100, "clientId": client_ids}

        # Assert that the "get" function was called with the expected arguments.
        get.assert_called_with(
            url=it_url,
            headers={"Authorization": f"Bearer {bearer_token_mock}"},
            params=parameters,
            timeout=settings.IT_SERVICE_TIMEOUT_SECONDS,
        )

    @mock.patch("management.principal.it_service.ITService._is_service_account_valid")
    def test_is_service_account_valid_by_username_client_id(self, _is_service_account_valid: mock.Mock):
        """Test that the function under test calls the underlying function with the unmodified client ID."""
        client_uuid = str(uuid.uuid4())
        user = User()

        self.it_service.is_service_account_valid_by_username(user=user, service_account_username=client_uuid)

        _is_service_account_valid.assert_called_with(user=user, client_id=client_uuid)

    @mock.patch("management.principal.it_service.ITService._is_service_account_valid")
    def test_is_service_account_valid_by_username_full(self, _is_service_account_valid: mock.Mock):
        """Test that the function under test calls the underlying function by stripping the service account prefix."""
        client_uuid = uuid.uuid4()
        username = f"service-account-{client_uuid}"
        user = User()

        self.it_service.is_service_account_valid_by_username(user=user, service_account_username=username)

        _is_service_account_valid.assert_called_with(user=user, client_id=str(client_uuid))

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_is_service_account_valid_bypass_it_calls(self, _):
        """Test that the function under test assumes service accounts to always be valid when bypassing IT calls."""
        original_bypass_it_calls_value = settings.IT_BYPASS_IT_CALLS
        try:
            settings.IT_BYPASS_IT_CALLS = True

            self.assertEqual(
                True,
                self.it_service._is_service_account_valid(user=User(), client_id="mocked-cid"),
                "when IT calls are bypassed, a service account should always be validated as if it existed",
            )
        finally:
            settings.IT_BYPASS_IT_CALLS = original_bypass_it_calls_value

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_is_service_account_valid(self, request_service_accounts: mock.Mock):
        """Tests that the service account is considered valid when there is a match between the response from IT and the requested service account"""
        user = User()
        user.bearer_token = "mocked-bt"

        expected_client_id = str(uuid.uuid4())
        request_service_accounts.return_value = [{"clientId": expected_client_id}]

        self.assertEqual(
            True,
            self.it_service._is_service_account_valid(user=user, client_id=expected_client_id),
            "when IT responds with a single service account and it matches, the function under test should return 'True'",
        )

        request_service_accounts.return_value = [
            {"clientId": str(uuid.uuid4())},
            {"clientId": str(uuid.uuid4())},
            {"clientId": expected_client_id},
        ]

        self.assertEqual(
            True,
            self.it_service._is_service_account_valid(user=user, client_id=expected_client_id),
            "when IT responds with multiple service accounts and one of them matches, the function under test should return 'True'",
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_is_service_account_invalid(self, request_service_accounts: mock.Mock):
        """Tests that the service account is considered invalid when there isn't a match between the response from IT and the requested service account"""
        user = User()
        user.bearer_token = "mocked-bt"

        expected_client_id = str(uuid.uuid4())
        request_service_accounts.return_value = []

        self.assertEqual(
            False,
            self.it_service._is_service_account_valid(user=user, client_id=expected_client_id),
            "when IT responds with a single service account and it does not match, the function under test should return 'False'",
        )

        request_service_accounts.return_value = [
            {"clientId": str(uuid.uuid4())},
            {"clientId": str(uuid.uuid4())},
            {"clientId": str(uuid.uuid4())},
        ]

        self.assertEqual(
            False,
            self.it_service._is_service_account_valid(user=user, client_id=expected_client_id),
            "when IT responds with multiple service accounts and none of them match, the function under test should return 'False'",
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_is_service_account_valid_zero_results_from_it(self, request_service_accounts: mock.Mock):
        """Test that the function under test treats an empty result from IT as an invalid service account."""
        request_service_accounts.return_value = []
        user = User()
        user.bearer_token = "mocked-bt"

        self.assertEqual(
            False,
            self.it_service._is_service_account_valid(user=user, client_id="mocked-cid"),
            "when IT returns an empty array for the given client ID, the service account should be considered invalid",
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_is_service_account_valid_one_matching_result_from_it(self, request_service_accounts: mock.Mock):
        """Test that the function under test positively validates the given service account if IT responds with that service account."""
        client_id = "client-id-123"
        request_service_accounts.return_value = [{"clientId": client_id}]
        user = User()
        user.bearer_token = "mocked-bt"

        self.assertEqual(
            True,
            self.it_service._is_service_account_valid(user=user, client_id=client_id),
            "when IT returns the requested service account via the client ID, the function under test should return True",
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_is_service_account_valid_not_matching_result_from_it(self, request_service_accounts: mock.Mock):
        """Test that the function under test does not validate the given service account if IT does not return a response with a proper service account."""
        client_id = "client-id-123"
        request_service_accounts.return_value = [{"clientId": "different-client-id"}]
        user = User()
        user.bearer_token = "mocked-bt"

        self.assertEqual(
            False,
            self.it_service._is_service_account_valid(user=user, client_id=client_id),
            "when IT returns a service account which doesn't match the provided client ID, the function under test should return False",
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_is_service_account_valid_multiple_results_from_it(self, request_service_accounts: mock.Mock):
        """
        Test that the function under test returns False
        when IT returns multiple service accounts for a single client ID.
        """
        request_service_accounts.return_value = [{}, {}]
        user = User()
        user.bearer_token = "mocked-bt"

        self.assertEqual(
            False,
            self.it_service._is_service_account_valid(user=user, client_id="mocked_cid"),
            "when IT returns more service accounts than the ones requested, the function under test should return False",
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_get_service_accounts(self, request_service_accounts: mock.Mock):
        """Test the function under test returns the expected service accounts"""
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Create the service accounts we expect to work with throughout the test, and more tenants in a different
        # tenant, to make sure that we only fetch the ones belonging to the above user.
        tenant_one_service_accounts, _ = self._create_database_service_account_principals_two_tenants()

        # We are simulating that we are calling the "request_service_accounts" method, which in turn, would call IT.
        # However, we are going to reuse the mock method to generate the IT service accounts that should match the
        # ones from the database.
        request_service_accounts.return_value = self.it_service._get_mock_service_accounts(tenant_one_service_accounts)

        # Call the function under test.
        result, count = self.it_service.get_service_accounts(user=user, options={"limit": 100, "offset": 0})
        self.assertEqual(
            len(tenant_one_service_accounts),
            count,
            "unexpected number of service accounts fetched for the tenant",
        )

        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=tenant_one_service_accounts, function_result=result
        )

    @override_settings(IT_BYPASS_IT_CALLS=True)
    def test_get_service_accounts_bypass_it_calls(self):
        """Test that bypassing IT calls makes the function return the service account principals from the database"""
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Create the service accounts we expect to work with throughout the test, and more tenants in a different
        # tenant, to make sure that we only fetch the ones belonging to the above user.
        tenant_one_service_accounts, _ = self._create_database_service_account_principals_two_tenants()

        # Call the function under test.
        result, count = self.it_service.get_service_accounts(user=user, options={"limit": 100, "offset": 0})
        self.assertEqual(
            len(tenant_one_service_accounts),
            count,
            "unexpected number of service accounts fetched for the tenant",
        )

        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=tenant_one_service_accounts, function_result=result
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_get_service_accounts_filter_usernames(self, request_service_accounts: mock.Mock):
        """Test the function under test returns the expected service accounts filtered by the specified usernames"""
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Create the service accounts we expect to work with throughout the test, and more tenants in a different
        # tenant, to make sure that we only fetch the ones belonging to the above user.
        tenant_one_service_accounts, _ = self._create_database_service_account_principals_two_tenants()

        # We are simulating that we are calling the "request_service_accounts" method, which in turn, would call IT.
        # However, we are going to reuse the mock method to generate the IT service accounts that should match the
        # ones from the database.
        request_service_accounts.return_value = self.it_service._get_mock_service_accounts(tenant_one_service_accounts)

        # Leave aside the two service accounts that we expect the function under test should return.
        first_sa: Principal = tenant_one_service_accounts[0]
        second_sa: Principal = tenant_one_service_accounts[1]

        # Set the options for the filter.
        options = {
            "limit": 100,
            "offset": 0,
            "usernames": f"{first_sa.username},{second_sa.username}",
        }

        # Call the function under test.
        result, count = self.it_service.get_service_accounts(user=user, options=options)
        self.assertEqual(
            2,
            count,
            "unexpected number of service accounts fetched for the tenant",
        )

        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=[first_sa, second_sa], function_result=result
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_get_service_accounts_filter_partial_match_username(self, request_service_accounts: mock.Mock):
        """Test the function under test returns the expected service account when filtering by partial match criteria"""
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Create the service accounts we expect to work with throughout the test, and more tenants in a different
        # tenant, to make sure that we only fetch the ones belonging to the above user.
        tenant_one_service_accounts, _ = self._create_database_service_account_principals_two_tenants()

        # We are simulating that we are calling the "request_service_accounts" method, which in turn, would call IT.
        # However, we are going to reuse the mock method to generate the IT service accounts that should match the
        # ones from the database.
        request_service_accounts.return_value = self.it_service._get_mock_service_accounts(tenant_one_service_accounts)

        # Leave aside the service account that we expect the function under test should return.
        first_sa: Principal = tenant_one_service_accounts[0]

        # Simulate that the user has specified a partial username to verify that the match criteria option works.
        first_sa_username = first_sa.username[:-10]

        # Set the options for the filter.
        options = {
            "limit": 100,
            "offset": 0,
            "match_criteria": "partial",
            "usernames": f"{first_sa_username}",
        }

        # Call the function under test.
        result, count = self.it_service.get_service_accounts(user=user, options=options)
        self.assertEqual(
            1,
            count,
            "unexpected number of service accounts fetched for the tenant",
        )

        self._assert_created_sa_and_result_are_same(created_database_sa_principals=[first_sa], function_result=result)

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_get_service_accounts_filter_full_match_username(self, request_service_accounts: mock.Mock):
        """Test the function under test returns the expected service account when filtering by full match criteria"""
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Create the service accounts we expect to work with throughout the test, and more tenants in a different
        # tenant, to make sure that we only fetch the ones belonging to the above user.
        tenant_one_service_accounts, _ = self._create_database_service_account_principals_two_tenants()

        # We are simulating that we are calling the "request_service_accounts" method, which in turn, would call IT.
        # However, we are going to reuse the mock method to generate the IT service accounts that should match the
        # ones from the database.
        request_service_accounts.return_value = self.it_service._get_mock_service_accounts(tenant_one_service_accounts)

        # Leave aside the service account that we expect the function under test should return.
        first_sa: Principal = tenant_one_service_accounts[0]

        # Simulate that the user has specified a partial username to verify that the match criteria option works.
        first_sa_username = first_sa.username[:-10]

        # Set the options for the filter. This time we specify the trimmed username again, which should not produce
        # any results with the "full" match criteria.
        options = {
            "limit": 100,
            "offset": 0,
            "match_criteria": "full",
            "usernames": f"{first_sa_username}",
        }

        # First assert that with a made up username no results are returned.
        _, first_count = self.it_service.get_service_accounts(user=user, options=options)
        self.assertEqual(
            0,
            first_count,
            "no service accounts should have been fetched when an incorrect username is passed to the filter",
        )

        # Now correct the username and make sure the filter works as expected.
        options["usernames"] = f"{first_sa.username}"

        # Call the function under test.
        result, count = self.it_service.get_service_accounts(user=user, options=options)
        self.assertEqual(
            1,
            count,
            "unexpected number of service accounts fetched for the tenant",
        )

        self._assert_created_sa_and_result_are_same(created_database_sa_principals=[first_sa], function_result=result)

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_get_service_accounts_group(self, request_service_accounts: mock.Mock):
        """Test the function under test returns the service accounts from the given group"""
        group_a, _ = self._create_two_rbac_groups_with_service_accounts()

        # We are simulating that we are calling the "request_service_accounts" method, which in turn, would call IT.
        # However, we are going to reuse the mock method to generate the IT service accounts that should match the
        # ones from the database.
        sa_principals_should_be_in_group: list[Principal] = group_a.principals.all()
        request_service_accounts.return_value = self.it_service._get_mock_service_accounts(
            sa_principals_should_be_in_group
        )

        # Create a user with the tenant that was used to create the service accounts and the group.
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Call the function under test.
        result = self.it_service.get_service_accounts_group(group=group_a, user=user)

        self.assertEqual(
            2,
            len(result),
            "only two service accounts were added to the group, and a different number of them is present",
        )

        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=sa_principals_should_be_in_group, function_result=result
        )

    @override_settings(IT_BYPASS_IT_CALLS=True)
    def test_get_service_accounts_group_bypass_it_calls(self):
        """Test the function under test returns the service accounts from the given group"""
        group_a, _ = self._create_two_rbac_groups_with_service_accounts()

        # We are simulating that we are calling the "request_service_accounts" method, which in turn, would call IT.
        # However, we are going to reuse the mock method to generate the IT service accounts that should match the
        # ones from the database.
        sa_principals_should_be_in_group: list[Principal] = group_a.principals.all()

        # Create a user with the tenant that was used to create the service accounts and the group.
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Call the function under test.
        result = self.it_service.get_service_accounts_group(group=group_a, user=user)

        self.assertEqual(
            2,
            len(result),
            "only two service accounts were added to the group, and a different number of them is present",
        )

        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=sa_principals_should_be_in_group, function_result=result
        )

    def test_get_service_accounts_group_username_only(self):
        """
        Test the function returns the list of service account usernames with query
        parameter username_only='true'.
        """
        group_a, _ = self._create_two_rbac_groups_with_service_accounts()
        expected_usernames = [sa.username for sa in group_a.principals.all()]

        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Call the function under test.
        options = {"username_only": "true"}
        result = self.it_service.get_service_accounts_group(group=group_a, user=user, options=options)

        self.assertEqual(
            2,
            len(result),
            "only two service accounts were added to the group, and a different number of them is present",
        )

        for item in result:
            self.assertIn(
                item["username"],
                expected_usernames,
                f'the service account with username \'{item["username"]}\' is not present in the list of expected '
                f"usernames '{expected_usernames}'",
            )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_get_service_accounts_group_filter_by_username(self, request_service_accounts: mock.Mock):
        """Test the function under test returns the filtered service accounts by username from the given group"""
        group_a, _ = self._create_two_rbac_groups_with_service_accounts()

        # We are simulating that we are calling the "request_service_accounts" method, which in turn, would call IT.
        # However, we are going to reuse the mock method to generate the IT service accounts that should match the
        # ones from the database.
        sa_principals_should_be_in_group: list[Principal] = group_a.principals.all()
        request_service_accounts.return_value = self.it_service._get_mock_service_accounts(
            sa_principals_should_be_in_group
        )

        # Create a user with the tenant that was used to create the service accounts and the group.
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Set up the options for the function. The username is trimmed and set, to check that the "contains" condition
        # works as expected.
        options = {"principal_username": sa_principals_should_be_in_group[0].username[:-10].strip("service-account-")}

        # Call the function under test.
        result = self.it_service.get_service_accounts_group(group=group_a, user=user, options=options)

        self.assertEqual(
            1,
            len(result),
            "only a single service account should have been fetched after applying the username filter",
        )

        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=[sa_principals_should_be_in_group[0]], function_result=result
        )

    @mock.patch("management.principal.it_service.ITService.request_service_accounts")
    def test_get_service_accounts_group_filter_by_description_name(self, request_service_accounts: mock.Mock):
        """Test the function under test returns the filtered service accounts by name and description"""
        group_a, _ = self._create_two_rbac_groups_with_service_accounts()

        # We are simulating that we are calling the "request_service_accounts" method, which in turn, would call IT.
        # However, we are going to reuse the mock method to generate the IT service accounts that should match the
        # ones from the database.
        sa_principals_should_be_in_group: list[Principal] = group_a.principals.all()
        request_service_accounts.return_value = self.it_service._get_mock_service_accounts(
            sa_principals_should_be_in_group
        )
        # Also make sure to grab the resulting service accounts, since we will need them to be able to filter them by
        # their description and/or their name.
        sa_principals_output = self.it_service._get_mock_service_accounts(sa_principals_should_be_in_group)

        # Create a user with the tenant that was used to create the service accounts and the group.
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Set up the options for the function. Start by specifying just the description of one of the accounts.
        second_sa_description: str = sa_principals_output[1]["description"]
        options_by_description = {SERVICE_ACCOUNT_DESCRIPTION_KEY: second_sa_description[:-10]}

        # Call the function under test.
        result_by_description = self.it_service.get_service_accounts_group(
            group=group_a, user=user, options=options_by_description
        )

        self.assertEqual(
            1,
            len(result_by_description),
            "only a single service account should have been fetched after applying the description filter",
        )

        # Make sure the second service account was found by its description.
        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=[sa_principals_should_be_in_group[1]], function_result=result_by_description
        )

        # Now try fetching the first service account by its name.
        first_sa_name: str = sa_principals_output[0]["name"]
        options_by_name = {SERVICE_ACCOUNT_NAME_KEY: first_sa_name[:-10]}

        # Call the function under test.
        result_by_name = self.it_service.get_service_accounts_group(group=group_a, user=user, options=options_by_name)

        self.assertEqual(
            1,
            len(result_by_name),
            "only a single service account should have been fetched after applying the name filter",
        )

        # Make sure the first service account was found by its name.
        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=[sa_principals_should_be_in_group[0]], function_result=result_by_name
        )

        # Finally attempt fetching the first service account by applying both the description and the name filters.
        first_sa_description: str = sa_principals_output[0]["description"]
        options_by_description_and_name = {
            SERVICE_ACCOUNT_DESCRIPTION_KEY: first_sa_description[:-10],
            SERVICE_ACCOUNT_NAME_KEY: first_sa_name[:-10],
        }

        # Call the function under test.
        result_description_and_name = self.it_service.get_service_accounts_group(
            group=group_a, user=user, options=options_by_description_and_name
        )

        self.assertEqual(
            1,
            len(result_description_and_name),
            "only a single service account should have been fetched after applying the name filter",
        )

        # Make sure the first service account was found by both its description and name.
        self._assert_created_sa_and_result_are_same(
            created_database_sa_principals=[sa_principals_should_be_in_group[0]],
            function_result=result_description_and_name,
        )

    def test_username_is_service_account(self) -> None:
        """Test that the username is correctly identified as a service account."""
        username = f"service-account-{uuid.uuid4()}"
        self.assertEqual(
            ITService.is_username_service_account(username),
            True,
            f"the given username '{username}' should have been identified as a service account username",
        )

    def test_username_is_not_service_account(self) -> None:
        """Test that the provided usernames are correctly identified as not service accounts."""
        usernames: list[str] = [
            "foo",
            "bar",
            f"serivce-account-{uuid.uuid4()}",
            f"service-acount-{uuid.uuid4()}",
            str(uuid.uuid4()),
        ]

        for username in usernames:
            self.assertEqual(
                ITService.is_username_service_account(username),
                False,
                f"the given username '{username}' should have not been identified as a service account username",
            )

    def test_extract_client_id_service_account_username(self) -> None:
        """Test that the client ID is correctly extracted from the service account's username"""
        client_id = uuid.uuid4()

        # Call the function under test with just the client ID. It should return it as is.
        self.assertEqual(
            client_id,
            ITService.extract_client_id_service_account_username(username=str(client_id)),
            "the client ID should be returned when it is passed to the function under test",
        )

        # Call the function under test with the whole prefix, and check that the client ID is correctly identified.
        self.assertEqual(
            client_id,
            ITService.extract_client_id_service_account_username(username=f"service-account-{client_id}"),
            "the client ID was not correctly extracted from a full username",
        )

        # Call the function under test with a username without client ID (UUID).
        try:
            self.assertFalse(ITService.extract_client_id_service_account_username(username="abcde"))
            self.fail(
                "when providing an invalid UUID as the client ID to be extracted, the function under test should raise an error"
            )
        except serializers.ValidationError as ve:
            self.assertEqual(
                "Invalid ClientId for a Service Account username",
                str(ve.detail.get("detail")),
                "unexpected error message when providing an invalid UUID as the client ID",
            )

        # Call the function under test with an invalid username which contains a bad formed UUID.
        try:
            ITService.extract_client_id_service_account_username(username="service-account-xxxxx")
            self.fail(
                "when providing an invalid UUID as the client ID to be extracted, the function under test should raise an error"
            )
        except serializers.ValidationError as ve:
            self.assertEqual(
                "Invalid format for a Service Account username",
                str(ve.detail.get("detail")),
                "unexpected error message when providing an invalid UUID as the client ID",
            )

    def test_generate_service_accounts_report_in_group_zero_matches(self):
        """Test that the function under test is able to flag service accounts as not present in a group"""
        # Create a group for the principals.
        group = Group(name="it-service-group", platform_default=False, system=False, tenant=self.tenant)
        group.save()

        # Add the principal accounts to make sure that we are only working with service accounts. If we weren't, these
        # principals below should give us unexpected results in our assertions.
        group.principals.add(Principal.objects.create(username="user-1", tenant=self.tenant))
        group.principals.add(Principal.objects.create(username="user-2", tenant=self.tenant))
        group.principals.add(Principal.objects.create(username="user-3", tenant=self.tenant))

        # Create three service accounts for the group. Since these will not be specified in the "client_ids" parameter
        # of the function under test, they should not show up in the results.
        sa_1 = Principal.objects.create(
            username=f"service-account-{uuid.uuid4()}",
            service_account_id=uuid.uuid4(),
            type="service-account",
            tenant=self.tenant,
        )
        sa_2 = Principal.objects.create(
            username=f"service-account-{uuid.uuid4()}",
            service_account_id=uuid.uuid4(),
            type="service-account",
            tenant=self.tenant,
        )
        sa_3 = Principal.objects.create(
            username=f"service-account-{uuid.uuid4()}",
            service_account_id=uuid.uuid4(),
            type="service-account",
            tenant=self.tenant,
        )

        # Add the service account principals.
        group.principals.add(sa_1)
        group.principals.add(sa_2)
        group.principals.add(sa_3)
        group.save()

        # Simulate that a few client IDs were specified in the request.
        request_client_ids = set[str]()
        request_client_ids.add(str(uuid.uuid4()))
        request_client_ids.add(str(uuid.uuid4()))
        request_client_ids.add(str(uuid.uuid4()))

        # Call the function under test.
        result: dict[str, bool] = self.it_service.generate_service_accounts_report_in_group(
            group=group, client_ids=request_client_ids
        )
        # Assert that only the specified client IDs are present in the result.
        self.assertEqual(3, len(result))

        # Assert that all the service accounts were flagged as not present in the group.
        for client_id, is_present_in_group in result.items():
            # Make sure the specified client IDs are in the set.
            self.assertEqual(
                True,
                client_id in request_client_ids,
                "expected to find the specified client ID from the request in the returning result",
            )
            # Make sure they are all set to "false" since there shouldn't be any of those client IDs in the group.
            self.assertEqual(
                False,
                is_present_in_group,
                "the client ID should have not been found in the group, since the group had no service accounts",
            )

    def test_generate_service_accounts_report_in_group_mixed_results(self):
        """
        Test that the function under test is able to correctly flag the service accounts when there are mixed results
        """
        # Create a group and associate principals to it.
        group = Group(name="it-service-group", platform_default=False, system=False, tenant=self.tenant)
        group.save()

        # Add the principal accounts to make sure that we are only working with service accounts. If we weren't, these
        # principals below should give us unexpected results in our assertions.
        group.principals.add(Principal.objects.create(username="user-1", tenant=self.tenant))
        group.principals.add(Principal.objects.create(username="user-2", tenant=self.tenant))
        group.principals.add(Principal.objects.create(username="user-3", tenant=self.tenant))
        group.save()

        client_uuid_1 = uuid.uuid4()
        client_uuid_2 = uuid.uuid4()
        sa_1 = Principal.objects.create(
            username=f"service-account-{client_uuid_1}",
            service_account_id=client_uuid_1,
            type="service-account",
            tenant=self.tenant,
        )
        sa_2 = Principal.objects.create(
            username=f"service-account-{client_uuid_2}",
            service_account_id=client_uuid_2,
            type="service-account",
            tenant=self.tenant,
        )

        # Add the service accounts to the group.
        group.principals.add(sa_1)
        group.principals.add(sa_2)
        group.save()

        # Create a set with the service accounts that will go in the group. It will make it easier to make assertions
        # below.
        group_service_accounts_set = {str(sa_1.service_account_id), str(sa_2.service_account_id)}

        # Create more service accounts that should not show in the results, since they're not going to be specified in
        # the "client_ids" parameter.
        sa_3 = Principal.objects.create(
            username=f"service-account-{uuid.uuid4()}",
            service_account_id=uuid.uuid4(),
            type="service-account",
            tenant=self.tenant,
        )
        sa_4 = Principal.objects.create(
            username=f"service-account-{uuid.uuid4()}",
            service_account_id=uuid.uuid4(),
            type="service-account",
            tenant=self.tenant,
        )
        sa_5 = Principal.objects.create(
            username=f"service-account-{uuid.uuid4()}",
            service_account_id=uuid.uuid4(),
            type="service-account",
            tenant=self.tenant,
        )

        # Add the service accounts that should not show up in the results.
        group.principals.add(sa_3)
        group.principals.add(sa_4)
        group.principals.add(sa_5)
        group.save()

        # Create the service accounts' client IDs that will be specified in the request.
        not_in_group = uuid.uuid4()
        not_in_group_2 = uuid.uuid4()
        not_in_group_3 = uuid.uuid4()

        # Also, create a set with the service accounts that will NOT go in the group to make it easier to assert that
        # the results flag them as such.
        service_accounts_not_in_group_set = {
            str(not_in_group),
            str(not_in_group_2),
            str(not_in_group_3),
        }

        # Add all the UUIDs to a set to pass it to the function under test.
        request_client_ids = set[str]()
        request_client_ids.add(str(not_in_group))
        request_client_ids.add(str(not_in_group_2))
        request_client_ids.add(str(not_in_group_3))

        # Specify the service accounts' UUIDs here too, because the function under test should flag them as present in
        # the group.
        request_client_ids.add(str(client_uuid_1))
        request_client_ids.add(str(client_uuid_2))

        # Call the function under test.
        result: dict[str, bool] = self.it_service.generate_service_accounts_report_in_group(
            group=group, client_ids=request_client_ids
        )

        # Assert that all the specified client IDs are present in the result.
        self.assertEqual(5, len(result))

        # Assert that the mixed matches are identified correctly.
        for client_id, is_it_present_in_group in result.items():
            # If the value is "true" it should be present in the service accounts' result set from above. Else, it
            # means that the specified client IDs were not part of the group, and that they should have been flagged
            # as such.
            if is_it_present_in_group:
                self.assertEqual(
                    True,
                    client_id in group_service_accounts_set,
                    "a client ID which was not part of the group was incorrectly flagged as if it was",
                )
            else:
                self.assertEqual(
                    True,
                    client_id in service_accounts_not_in_group_set,
                    "a client ID which was part of the group was incorrectly flagged as if it wasn't",
                )

    def test_generate_service_accounts_report_in_group_full_match(self):
        """Test that the function under test is able to flag service accounts as all being present in the group."""
        # Create a group and associate principals to it.
        group = Group(name="it-service-group", platform_default=False, system=False, tenant=self.tenant)
        group.save()

        # Add the principal accounts to make sure that we are only working with service accounts. If we weren't, these
        # principals below should give us unexpected results in our assertions.
        group.principals.add(Principal.objects.create(username="user-1", tenant=self.tenant))
        group.principals.add(Principal.objects.create(username="user-2", tenant=self.tenant))
        group.principals.add(Principal.objects.create(username="user-3", tenant=self.tenant))

        # Create the service accounts to be associated with the group.
        client_uuid_1 = uuid.uuid4()
        client_uuid_2 = uuid.uuid4()
        client_uuid_3 = uuid.uuid4()
        client_uuid_4 = uuid.uuid4()
        client_uuid_5 = uuid.uuid4()
        sa_1 = Principal.objects.create(
            username=f"service-account-{client_uuid_1}",
            service_account_id=client_uuid_1,
            type="service-account",
            tenant=self.tenant,
        )
        sa_2 = Principal.objects.create(
            username=f"service-account-{client_uuid_2}",
            service_account_id=client_uuid_2,
            type="service-account",
            tenant=self.tenant,
        )
        sa_3 = Principal.objects.create(
            username=f"service-account-{client_uuid_3}",
            service_account_id=client_uuid_3,
            type="service-account",
            tenant=self.tenant,
        )
        sa_4 = Principal.objects.create(
            username=f"service-account-{client_uuid_4}",
            service_account_id=client_uuid_4,
            type="service-account",
            tenant=self.tenant,
        )
        sa_5 = Principal.objects.create(
            username=f"service-account-{client_uuid_5}",
            service_account_id=client_uuid_5,
            type="service-account",
            tenant=self.tenant,
        )

        # Create a set with the service accounts that will go in the group. It will make it easier to make assertions
        # below.
        group_service_accounts_set = {
            str(sa_1.service_account_id),
            str(sa_2.service_account_id),
            str(sa_3.service_account_id),
            str(sa_4.service_account_id),
            str(sa_5.service_account_id),
        }

        # Add the service accounts to the group.
        group.principals.add(sa_1)
        group.principals.add(sa_2)
        group.principals.add(sa_3)
        group.principals.add(sa_4)
        group.principals.add(sa_5)
        group.save()

        # Simulate that a few client IDs were specified in the request.
        request_client_ids = set[str]()
        request_client_ids.add(str(client_uuid_1))
        request_client_ids.add(str(client_uuid_2))
        request_client_ids.add(str(client_uuid_3))
        request_client_ids.add(str(client_uuid_4))
        request_client_ids.add(str(client_uuid_5))

        # Call the function under test.
        result: dict[str, bool] = self.it_service.generate_service_accounts_report_in_group(
            group=group, client_ids=request_client_ids
        )

        # Assert that all the specified client IDs are present in the result.
        self.assertEqual(5, len(result))

        # Assert that all the results are flagged as being part of the group.
        for client_id, is_present_in_group in result.items():
            self.assertEqual(
                True,
                client_id in request_client_ids,
                "expected to find the specified client ID from the request in the returning result",
            )
            self.assertEqual(
                True,
                client_id in group_service_accounts_set,
                "expected to find the client ID from the result set in the service accounts' group set",
            )
            # Make sure they are all set to "true" since all the specified client IDs should be in the group.
            self.assertEqual(
                True,
                is_present_in_group,
                "the client ID should have been found in the group, since the group had all the service accounts added to it",
            )

    def test_transform_incoming_payload(self) -> None:
        """Test that the payload transformation works as expected"""
        client_id = str(uuid.uuid4())
        name = "service-account-name"
        description = "service-account-description"
        created_by = "service-account-owner"
        created_at = "service-account-creation-time"

        it_service_account = {
            "clientId": client_id,
            "name": name,
            "description": description,
            "createdBy": created_by,
            "createdAt": created_at,
        }

        # Call the function under test.
        result = self.it_service._transform_incoming_payload(service_account_from_it_service=it_service_account)

        # Assert that the transformation was correct.
        result_client_id = result["clientId"]
        if not result_client_id:
            self.fail('the "clientId" field is not present in the resulting model')

        self.assertEqual(client_id, result_client_id, 'the "clientId" field was not correctly transformed')

        result_name = result["name"]
        if not result_name:
            self.fail('the "name" field is not present in the resulting model')

        self.assertEqual(name, result_name, 'the "name" field was not correctly transformed')

        result_description = result["description"]
        if not result_description:
            self.fail('the "description" field is not present in the resulting model')

        self.assertEqual(description, result_description, 'the "description" field was not correctly transformed')

        result_owner = result["owner"]
        if not result_owner:
            self.fail('the "owner" field is not present in the resulting model')

        self.assertEqual(created_by, result_owner, 'the "owner" field was not correctly transformed')

        result_time_created = result["time_created"]
        if not result_time_created:
            self.fail('the "time_created" field is not present in the resulting model')

        self.assertEqual(created_at, result_time_created, 'the "time_created" field was not correctly transformed')

    def test_merge_principals_it_service_accounts(self) -> None:
        """Test that the function under test correctly merges service account principals and database principals."""
        sa_client_id = str(uuid.uuid4())
        sa_two_client_id = str(uuid.uuid4())

        # We only grab the username from the service account principals from our database, so we omit the rest of the
        # fields.
        first_principal = Principal()
        first_principal.username = f"{sa_client_id}-username"

        second_principal = Principal()
        second_principal.username = f"{sa_two_client_id}-username"

        service_account_principals = {sa_client_id: first_principal, sa_two_client_id: second_principal}

        expected_first_username = f"{sa_client_id}-username"
        expected_second_username = f"{sa_two_client_id}-username"
        it_service_accounts = [
            {"clientId": sa_client_id, "username": expected_first_username, "made_up_key": "made_up_value"},
            {"clientId": sa_two_client_id, "username": expected_second_username, "made_up_key": "made_up_value"},
            {"clientId": str(uuid.uuid4()), "username": "should-not-be-picked", "made_up_key": "made_up_value"},
        ]

        # Call the function under test.
        results = self.it_service._merge_principals_it_service_accounts(
            service_account_principals=service_account_principals, it_service_accounts=it_service_accounts, options={}
        )

        # Since we did not specify any options, we should get the two expected service account principals and all the
        # extra keys in the payload.
        self.assertEqual(
            2,
            len(results),
            "one of the IT service accounts should have been filtered out because in theory RBAC didn't have it "
            " stored in the database",
        )

        for result in results:
            self.assertEqual(
                3,
                len(result.keys()),
                "none of the keys of the service account should have been filtered out since no options were"
                " given to the function under test",
            )

            self.assertTrue(
                (result["username"] == expected_first_username) or (result["username"] == expected_second_username),
                f"the resulting service account does not have an expected username. Expected"
                f' "{expected_first_username}" or "{expected_second_username}", got the following service account:'
                f" {result}",
            )

        # Call the function under test but with the "username_only" parameter which should filter all the other keys
        # from the resulting payload.
        filtered_results = self.it_service._merge_principals_it_service_accounts(
            service_account_principals=service_account_principals,
            it_service_accounts=it_service_accounts,
            options={"username_only": "true"},
        )

        # Now, we should get the two expected service account principals and just the "username" key.
        self.assertEqual(
            2,
            len(filtered_results),
            "one of the IT service accounts should have been filtered out because in theory RBAC didn't have it "
            " stored in the database",
        )

        for filtered_result in filtered_results:
            self.assertEqual(
                1,
                len(filtered_result.keys()),
                f"only the username key in the payload should have been kept {filtered_result}",
            )

            self.assertTrue(
                (filtered_result["username"] == expected_first_username)
                or (filtered_result["username"] == expected_second_username),
                f"the resulting service account does not have an expected username. Expected"
                f' "{expected_first_username}" or "{expected_second_username}", got the following service account:'
                f" {filtered_result}",
            )

    @override_settings(IT_BYPASS_IT_CALLS=True)
    def test_principal_filtering_without_username_match_criteria_exact_skipped(self):
        """Test the function under test skips the expected service account when filtering by exact match criteria without usernames"""
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Set the options for the filter.
        options = {
            "limit": 10,
            "offset": 0,
            "match_criteria": "exact",
            "usernames": None,
        }

        # Call the function under test.
        result, count = self.it_service.get_service_accounts(user=user, options=options)
        self.assertEqual(
            0,
            count,
            "unexpected number of service accounts fetched for the tenant",
        )

    @override_settings(IT_BYPASS_IT_CALLS=True)
    def test_principal_filtering_without_username_match_criteria_partial_skipped(self):
        """Test the function under test skips the expected service account when match_criteria is partial without usernames"""
        user = User()
        user.account = self.tenant.account_id
        user.org_id = self.tenant.org_id

        # Set the options for the filter.
        options = {
            "limit": 10,
            "offset": 0,
            "match_criteria": "partial",
            "usernames": None,
        }

        # Call the function under test.
        result, count = self.it_service.get_service_accounts(user=user, options=options)
        self.assertEqual(
            0,
            count,
            "unexpected number of service accounts fetched for the tenant",
        )
