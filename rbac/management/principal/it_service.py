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
"""Class to manage interactions with the IT service accounts service."""

import logging
import time
import uuid
from typing import Any, Optional, Tuple, Union

import requests
from django.conf import settings
from django.db.models import Q
from management.authorization.missing_authorization import MissingAuthorizationError
from management.models import Group, Principal
from prometheus_client import Counter, Histogram
from rest_framework import serializers, status

from api.models import User
from .unexpected_status_code_from_it import UnexpectedStatusCodeFromITError

# Constants or global variables.
LOGGER = logging.getLogger(__name__)
SERVICE_ACCOUNT_CLIENT_IDS_KEY = "service_account_client_ids"
KEY_SERVICE_ACCOUNT = "service-account-"

# IT path to fetch the service accounts.
IT_PATH_GET_SERVICE_ACCOUNTS = "/service_accounts/v1"

# Set up the metrics for the IT calls.
it_request_all_service_accounts_time_tracking = Histogram(
    "it_request_all_service_accounts_processing_seconds",
    "Time spent processing requests from IT when requesting all the service accounts of a tenant",
)

it_request_status_count = Counter(
    "it_request_status_total",
    "Number of requests from RBAC to IT's SSO and resulting status",
    ["method", "status"],
)

it_request_error = Counter(
    "it_request_error",
    "Number of requests from RBAC to IT's SSO that failed and the reason why they failed",
    ["error"],
)

# Keys for the "options" dictionary. The "options" dictionary represents the query parameters passed by the calling
# client.
SERVICE_ACCOUNT_DESCRIPTION_KEY = "service_account_description"
SERVICE_ACCOUNT_NAME_KEY = "service_account_name"


def limit_offset_validation(offset, limit):
    """Limit and offset should not be negative number."""
    if offset < 0 or limit < 0:
        detail = "Values for limit and offset must be positive numbers."
        source = "service accounts"
        raise serializers.ValidationError({source: [detail]})


class ITService:
    """A class to handle interactions with the IT service."""

    # Instance variable for the class.
    _instance = None

    def __new__(cls, *args, **kwargs):
        """Create a single instance of the class."""
        if cls._instance is None:
            cls._instance = super().__new__(cls, *args, **kwargs)

        return cls._instance

    def __init__(self):
        """Establish IT connection information."""
        self.host = settings.IT_SERVICE_HOST
        self.base_path = settings.IT_SERVICE_BASE_PATH
        self.port = settings.IT_SERVICE_PORT
        self.protocol = settings.IT_SERVICE_PROTOCOL_SCHEME
        self.it_request_timeout = settings.IT_SERVICE_TIMEOUT_SECONDS
        self.it_url = f"{self.protocol}://{self.host}:{self.port}{self.base_path}{IT_PATH_GET_SERVICE_ACCOUNTS}"

    @it_request_all_service_accounts_time_tracking.time()
    def request_service_accounts(self, bearer_token: str, client_ids: Optional[list[str]] = None) -> list[dict]:
        """Request the service accounts for a tenant and returns the entire list that IT has."""
        # We cannot talk to IT if we don't have a bearer token.
        if not bearer_token:
            raise MissingAuthorizationError()

        received_service_accounts: list[dict] = []

        # Attempt fetching all the service accounts for the tenant.
        try:
            # Define some sane initial values.
            offset = 0
            limit = 100

            # If the offset is zero, that means that we need to call the service at least once to get the first
            # service accounts. If it equals the limit, that means that there are more pages to fetch.
            parameters: dict[str, Union[int, list[str]]] = {"first": offset, "max": limit}
            # If we were given client IDs to filter the collection with, do it!
            if client_ids:
                parameters["clientId"] = client_ids

            continue_fetching: bool = True
            while continue_fetching:
                # Recreate the parameters dictionary every time since otherwise the "assert_has_calls" statement of the
                # tests only sees the last value for the offset when attempting to fetch multiple pages.
                parameters = {"first": offset, "max": limit}
                if client_ids:
                    parameters["clientId"] = client_ids

                # Call IT.
                response = requests.get(
                    url=self.it_url,
                    headers={"Authorization": f"Bearer {bearer_token}"},
                    params=parameters,
                    timeout=self.it_request_timeout,
                )

                # Save the metrics for the successful call. Successful does not mean that we received an OK response,
                # but that we were able to reach IT's SSO instead and get a response from them.
                it_request_status_count.labels(method="GET", status=response.status_code).inc()

                if not status.is_success(response.status_code):
                    LOGGER.error(
                        "Unexpected status code '%s' received from IT when fetching service accounts. "
                        "Response body: %s",
                        response.status_code,
                        response.content,
                    )

                    raise UnexpectedStatusCodeFromITError()

                # Extract the body contents.
                body_contents = response.json()

                # Merge the previously received service accounts with the new ones.
                received_service_accounts = received_service_accounts + body_contents

                # Reassess if we need to keep fetching pages from IT. They don't return page metadata, so we need to
                # keep looping until the incoming body is an empty array.
                continue_fetching = limit == len(body_contents)
                if continue_fetching:
                    offset = offset + len(body_contents)

        except requests.exceptions.ConnectionError as exception:
            LOGGER.error(
                "Unable to connect to IT to fetch the service accounts. Attempted URL %s with error: %s",
                self.it_url,
                exception,
            )

            # Increment the error count.
            it_request_error.labels(error="connection-error").inc()

            # Raise the exception again to return a proper response to the client.
            raise exception
        except requests.exceptions.Timeout as exception:
            LOGGER.error(
                "The connection to IT timed out when trying to fetch service accounts. Attempted URL %s with error: %s",
                self.it_url,
                exception,
            )

            # Increment the error count.
            it_request_error.labels(error="timeout").inc()

            # Raise the exception again to return a proper response to the client.
            raise exception

        # Transform the incoming payload into our model's service accounts.
        service_accounts: list[dict] = []
        for incoming_service_account in received_service_accounts:
            service_accounts.append(self._transform_incoming_payload(incoming_service_account))

        return service_accounts

    def is_service_account_valid_by_client_id(self, user: User, service_account_client_id: str) -> bool:
        """Check if the specified service account is valid."""
        if settings.IT_BYPASS_IT_CALLS:
            return False

        return self._is_service_account_valid(user=user, client_id=service_account_client_id)

    def is_service_account_valid_by_username(self, user: User, service_account_username: str) -> bool:
        """Check if the specified service account is valid."""
        # The usernames for the service accounts usually come in the form "service-account-${CLIENT_ID}". We don't need
        # the prefix of the username to query IT, since they only accept client IDs to filter collections.
        if settings.IT_BYPASS_IT_CALLS:
            return True

        if self.is_username_service_account(service_account_username):
            client_id = service_account_username.replace(KEY_SERVICE_ACCOUNT, "")
        else:
            client_id = service_account_username

        return self._is_service_account_valid(user=user, client_id=client_id)

    def _is_service_account_valid(self, user: User, client_id: str) -> bool:
        """Check if the provided client ID can be found in the tenant's organization by calling IT."""
        if settings.IT_BYPASS_IT_CALLS:
            return True
        else:
            # In theory, we should be able to pass the client ID to the function below to just get the specified
            # service account and check if it is present or not. However, due to a bug, we need to fetch the whole
            # collection for now. More details in https://issues.redhat.com/browse/RHCLOUD-31265 .
            service_accounts: list[dict] = self.request_service_accounts(bearer_token=user.bearer_token)

            for sa in service_accounts:
                if client_id == sa.get("clientId"):
                    return True

            return False

    def get_service_accounts(self, user: User, options: dict[str, Any] = {}) -> Tuple[list[dict], int]:
        """Request and returns the service accounts for the given tenant."""
        # We might want to bypass calls to the IT service on ephemeral or test environments.
        it_service_accounts: list[dict] = []
        if not settings.IT_BYPASS_IT_CALLS:
            it_service_accounts = self.request_service_accounts(bearer_token=user.bearer_token)

        # Get the service accounts from the database. The weird filter is to fetch the service accounts depending on
        # the account number or the organization ID the user gave.
        service_account_principals = Principal.objects.filter(type=Principal.Types.SERVICE_ACCOUNT).filter(
            (Q(tenant__isnull=False) & Q(tenant__account_id=user.account))
            | (Q(tenant__isnull=False) & Q(tenant__org_id=user.org_id))
        )

        # The following filters do not make sense for service accounts, because we either do not have the
        # corresponding fields to filter with, or it only applies to principals.
        # - Admin only
        # - Email
        # - Status
        usernames: list[str] = []
        specified_usernames = options.get("usernames")
        if specified_usernames:
            usernames = specified_usernames.split(",")

        # If "match_criteria" is specified and the usernames list is not empty,
        # only the first username is taken into account
        match_criteria = options.get("match_criteria")
        if match_criteria and usernames:
            username = usernames[0]

            if match_criteria == "partial":
                service_account_principals = service_account_principals.filter(username__startswith=username)
            else:
                service_account_principals = service_account_principals.filter(username=username)
        elif len(usernames) > 0:
            service_account_principals = service_account_principals.filter(username__in=usernames)

        # Sort order which defaults to ascending.
        sort_order_ascending = True
        sort_order = options.get("sort_order")
        if sort_order:
            sort_order_ascending = sort_order == "asc"

        asc_order_by_enabled = True
        order_by = options.get("order_by")
        if order_by:
            asc_order_by_enabled = not order_by.startswith("-")

        if sort_order_ascending and asc_order_by_enabled:
            service_account_principals = service_account_principals.order_by("username")
        else:
            service_account_principals = service_account_principals.order_by("-username")

        # If we are in an ephemeral or test environment, we will take all the service accounts of the user that are
        # stored in the database and generate a mocked response for them, simulating that IT has the corresponding
        # service account to complement the information.
        if settings.IT_BYPASS_IT_CALLS:
            it_service_accounts = self._get_mock_service_accounts(
                service_account_principals=service_account_principals
            )

        # Put the service accounts in a dict by for a quicker search.
        sap_dict: dict[str, Principal] = {}
        for sap in service_account_principals:
            sap_dict[sap.service_account_id] = sap

        # Filter the incoming service accounts. Also, transform them to the payload we will
        # be returning.
        service_accounts: list[dict] = self._merge_principals_it_service_accounts(
            service_account_principals=sap_dict, it_service_accounts=it_service_accounts, options=options
        )

        # We always set a default offset and a limit if the user doesn't specify them, so it is safe to simply put the
        # two parameters in the query to slice it.
        offset = options.get("offset")
        limit = options.get("limit")
        limit_offset_validation(offset, limit)
        # Service account filtering query parameters
        name = options.get("name")
        owner = options.get("owner")
        description = options.get("description")
        sa_query_passed = name or owner or description

        count = len(service_accounts)
        # If any one service account filter parameter is provided extract & return the specific service accounts
        if sa_query_passed:
            filtered_service_accounts = []
            for sa in service_accounts:
                sa_description = str(sa.get("description"))
                sa_owner = str(sa.get("owner"))
                sa_name = str(sa.get("name"))
                if (
                    (not name or sa_name.lower() == name.lower())
                    and (not owner or sa_owner.lower() == owner.lower())
                    and (not description or description.lower() in sa_description.lower())
                ):
                    filtered_service_accounts.append(sa)

            service_accounts = filtered_service_accounts
            count = len(service_accounts)

        if order_by:
            # If any order_by parameter is passed then sort the service accounts by that field either asc or desc
            if order_by in ["-time_created", "-name", "-description", "-clientId", "-owner"]:
                service_accounts.sort(reverse=True, key=lambda sa: str(sa.get(order_by[1:], "")).casefold())
            else:
                service_accounts.sort(reverse=False, key=lambda sa: str(sa.get(order_by, "")).casefold())

        # flake8 ignore E203 = Whitespace before ':' -> false positive https://github.com/PyCQA/pycodestyle/issues/373
        service_accounts = service_accounts[offset : offset + limit]  # type: ignore # noqa: E203

        return service_accounts, count

    def get_service_accounts_group(self, group: Group, user: User, options: dict[str, Any] = {}) -> list[dict]:
        """Get the service accounts for the given group."""
        username_only: str = options.get("username_only", "false")
        # We might want to bypass calls to the IT service
        #        - on ephemeral or test environments
        #        - when query param username_only == 'true'
        it_service_accounts: list[dict[str, Union[str, int]]] = []
        if not settings.IT_BYPASS_IT_CALLS and username_only == "false":
            it_service_accounts = self.request_service_accounts(bearer_token=user.bearer_token)

        # Fetch the service accounts from the group.
        group_service_account_principals = group.principals.filter(type=Principal.Types.SERVICE_ACCOUNT)

        # Apply the specified query parameters for the collection. Begin with the sort order.
        sort_order = options.get("sort_order")
        if sort_order:
            if sort_order == "asc":
                order_by = "username"
            else:
                order_by = "-username"

            group_service_account_principals = group_service_account_principals.order_by(order_by)

        # Check if we should filter the service accounts by the username that the user specified.
        # In this case we want to ignore the prefix "service-account-" in the SA username and
        # filter records only by SA client ID (uuid).
        principal_username = options.get("principal_username")
        if principal_username:
            if principal_username.startswith("service-account-"):
                group_service_account_principals = group_service_account_principals.filter(
                    username__contains=principal_username
                )
            else:
                group_service_account_principals = group_service_account_principals.filter(
                    service_account_id__contains=principal_username
                )

        # If we are in an ephemeral or test environment, we will take all the service accounts of the user that are
        # stored in the database and generate a mocked response for them, simulating that IT has the corresponding
        # service account to complement the information.
        if settings.IT_BYPASS_IT_CALLS:
            it_service_accounts = self._get_mock_service_accounts(
                service_account_principals=group_service_account_principals
            )

        # Put the service accounts in a dict by for a quicker search.
        sap_dict: dict[str, Principal] = {}
        for sap in group_service_account_principals:
            sap_dict[sap.service_account_id] = sap

        service_accounts: list[dict] = []
        if username_only == "true":
            # Grab the service account usernames
            service_accounts = [{"username": sa.username} for sa in group_service_account_principals]
        else:
            # Filter the incoming service accounts. Also, transform them to the payload we will be returning.
            service_accounts = self._merge_principals_it_service_accounts(
                service_account_principals=sap_dict, it_service_accounts=it_service_accounts, options=options
            )

        # If either the description or name filters were specified, we need to only keep the service accounts that
        # match that criteria.
        sa_description_filter = options.get(SERVICE_ACCOUNT_DESCRIPTION_KEY)
        sa_name_filter = options.get(SERVICE_ACCOUNT_NAME_KEY)

        if sa_description_filter or sa_name_filter:
            filtered_service_accounts: list[dict] = []

            for sa in service_accounts:
                # Initialize both matches as "True", because having them at "False" would force us to make them "True"
                # whenever one of the filters is not specified, since otherwise not specifying a filter would stop the
                # service account to be included in the resulting list.
                sa_description_filter_matches: bool = True
                sa_name_filter_matches: bool = True

                if sa_description_filter:
                    sa_description_filter_matches = ("description" in sa) and (
                        sa_description_filter in sa["description"]
                    )

                if sa_name_filter:
                    sa_name_filter_matches = ("name" in sa) and (sa_name_filter in sa["name"])

                # When both filters are specified we require them both to have a positive match for the fields.
                if sa_description_filter_matches and sa_name_filter_matches:
                    filtered_service_accounts.append(sa)

            return filtered_service_accounts
        else:
            return service_accounts

    @staticmethod
    def is_username_service_account(username: str) -> bool:
        """Check if the given username belongs to a service account."""
        starts_with = username.startswith(KEY_SERVICE_ACCOUNT)

        # Validate the UUID for the ClientID reference
        if starts_with:
            try:
                if username.count(KEY_SERVICE_ACCOUNT) != 1:
                    raise ValueError

                uuid.UUID(username.replace(KEY_SERVICE_ACCOUNT, ""))
            except ValueError:
                raise serializers.ValidationError({"detail": "Invalid format for a Service Account username"})

        return starts_with

    @staticmethod
    def extract_client_id_service_account_username(username: str) -> uuid.UUID:
        """Extract the client ID from the service account's username."""
        # If it has the "service-account" prefix, we just need to strip it and return the rest of the username, which
        # contains the client ID. Else, we have just received the client ID.
        if ITService.is_username_service_account(username=username):
            return uuid.UUID(username.replace(KEY_SERVICE_ACCOUNT, ""))
        else:
            try:
                return uuid.UUID(username)
            except ValueError:
                raise serializers.ValidationError({"detail": "Invalid ClientId for a Service Account username"})

    def generate_service_accounts_report_in_group(self, group: Group, client_ids: set[str]) -> dict[str, bool]:
        """Check if the given service accounts are in the specified group."""
        # Fetch the service accounts from the group.
        group_service_account_principals = (
            group.principals.values_list("service_account_id", flat=True)
            .filter(type=Principal.Types.SERVICE_ACCOUNT)
            .filter(service_account_id__in=client_ids)
        )

        # Mark the specified client IDs as "present or missing" from the result set.
        result: dict[str, bool] = {}
        for incoming_client_id in client_ids:
            result[incoming_client_id] = incoming_client_id in group_service_account_principals

        return result

    def _transform_incoming_payload(self, service_account_from_it_service: dict) -> dict[str, Any]:
        """Transform the incoming service account from IT into a dict which fits our response structure."""
        service_account: dict[str, Any] = {}

        client_id = service_account_from_it_service.get("clientId")
        name = service_account_from_it_service.get("name")
        description = service_account_from_it_service.get("description")
        created_by = service_account_from_it_service.get("createdBy")
        created_at = service_account_from_it_service.get("createdAt")
        user_id = service_account_from_it_service.get("userId")

        if client_id:
            service_account["clientId"] = client_id

        if name:
            service_account["name"] = name

        if description:
            service_account["description"] = description

        if created_by:
            service_account["owner"] = created_by

        if created_at:
            service_account["time_created"] = created_at

        if user_id:
            service_account["userId"] = user_id

        # Hard code the type for every service account.
        service_account["type"] = "service-account"

        return service_account

    def _merge_principals_it_service_accounts(
        self, service_account_principals: dict[str, Principal], it_service_accounts: list[dict], options: dict
    ) -> list[dict]:
        """Merge the database principals with the service account principals and return the response payload.

        We only return the service accounts which we have references for in our database.
        """
        service_accounts: list[dict] = []

        # If the "username_only" parameter was set, we should only return that for the user.
        username_only = options.get("username_only")

        for it_service_account in it_service_accounts:
            try:
                sa_principal = service_account_principals[it_service_account["clientId"]]

                if username_only and username_only == "true":
                    service_accounts.append({"username": sa_principal.username})  # type: ignore
                else:
                    # Get the principal's username from the database and set it in the response for the user.
                    it_service_account["username"] = sa_principal.username  # type: ignore

                    service_accounts.append(it_service_account)
            # If we cannot find a requested service account to IT in the database, we simply
            # skip it.
            except KeyError:
                continue

        return service_accounts

    def _get_mock_service_accounts(self, service_account_principals: list[Principal]) -> list[dict]:
        """Mock an IT service call which returns service accounts. Useful for development or testing."""
        mocked_service_accounts: list[dict] = []
        for sap in service_account_principals:
            # Transform the service account to the structure our logic works with and then append it to the list of
            # mocked service accounts we will be returning.
            mocked_service_accounts.append(
                self._transform_incoming_payload(
                    {
                        "id": sap.service_account_id,
                        "clientId": sap.service_account_id,
                        "name": f"{sap.service_account_id}-name",
                        "description": f"{sap.service_account_id}-description",
                        "createdBy": sap.username,
                        "createdAt": round(time.time()),
                    }
                )
            )

        return mocked_service_accounts
