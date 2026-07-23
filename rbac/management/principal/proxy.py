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

"""Proxy for principal management."""

import logging

import requests
from django.conf import settings
from management.models import Principal
from prometheus_client import Counter, Histogram
from rest_framework import status

from api.models import User
from rbac.env import ENVIRONMENT

LOGGER = logging.getLogger(__name__)
PROTOCOL = "protocol"
HOST = "host"
PORT = "port"
PATH = "path"
SSL_VERIFY = "ssl_verify"
SOURCE_CERT = "source_cert"
USER_ENV = "env"
CLIENT_ID = "clientid"
API_TOKEN = "apitoken"
USER_ENV_HEADER = "x-rh-insights-env"
CLIENT_ID_HEADER = "x-rh-clientid"
API_TOKEN_HEADER = "x-rh-apitoken"
CLIENT_CERT_PATH = "client_cert_path"

bop_request_time_tracking = Histogram(
    "rbac_proxy_request_processing_seconds", "Time spent processing requests to BOP from RBAC"
)
bop_request_status_count = Counter(
    "bop_request_status_total", "Number of requests from RBAC to BOP and resulting status", ["method", "status"]
)


class PrincipalProxy:  # pylint: disable=too-few-public-methods
    """A class to handle interactions with the Principal proxy service."""

    def __init__(self):
        """Establish proxy connection information."""
        proxy_conn_info = self._get_proxy_service()
        self.protocol = proxy_conn_info.get(PROTOCOL)
        self.host = proxy_conn_info.get(HOST)
        self.port = proxy_conn_info.get(PORT)
        self.path = proxy_conn_info.get(PATH)
        self.ssl_verify = proxy_conn_info.get(SSL_VERIFY)
        self.source_cert = proxy_conn_info.get(SOURCE_CERT)
        self.user_env = proxy_conn_info.get(USER_ENV)
        self.client_id = proxy_conn_info.get(CLIENT_ID)
        self.api_token = proxy_conn_info.get(API_TOKEN)
        self.client_cert_path = proxy_conn_info.get(CLIENT_CERT_PATH)

    @staticmethod
    def _create_params(limit=None, offset=None, options={}):
        """Create query parameters."""
        params = {}
        if "username_only" in options and options["username_only"] == "true":
            params["username_only"] = "true"
        if limit:
            params["limit"] = limit
        if offset:
            params["offset"] = offset
        if "sort_order" in options:
            # BOP only accepts 'des'
            if options["sort_order"] == "desc":
                params["sortOrder"] = "des"
            else:
                params["sortOrder"] = options["sort_order"]
        if "status" in options:
            params["status"] = options["status"]
        else:
            params["status"] = "enabled"
        if "admin_only" in options:
            params["admin_only"] = options["admin_only"]
        if "query_by" in options:
            if options["query_by"] == "user_id":
                params["queryBy"] = "userId"
            else:
                params["queryBy"] = options["query_by"]
        if "include_permissions" in options:
            if options["include_permissions"]:
                params["include_permissions"] = "true"
            else:
                params["include_permissions"] = "false"

        return params

    def _process_data(self, data, org_id=None, org_id_filter=None, return_id=False):
        """Process data for uniform output."""
        processed_data = []
        for item in data:
            if org_id_filter:
                if org_id == item.get("org_id"):
                    processed_data.append(self._call_item(item, return_id))
            else:
                processed_data.append(self._call_item(item, return_id))
        return processed_data

    @staticmethod
    def _call_item(item, return_id=False):
        processed_item = {
            "username": item.get("username"),
            "email": item.get("email"),
            "first_name": item.get("first_name"),
            "last_name": item.get("last_name"),
            "is_active": item.get("is_active"),
            "is_org_admin": item.get("is_org_admin"),
            "external_source_id": item.get("id"),
            "org_id": item.get("org_id"),
        }

        if return_id:
            processed_item["user_id"] = item.get("id")
        return processed_item

    def _get_proxy_service(self):  # pylint: disable=no-self-use
        """Get proxy service host and port info from environment."""
        proxy_conn_info = {
            PROTOCOL: ENVIRONMENT.get_value("PRINCIPAL_PROXY_SERVICE_PROTOCOL", default="https"),
            HOST: ENVIRONMENT.get_value("PRINCIPAL_PROXY_SERVICE_HOST", default="localhost"),
            PORT: ENVIRONMENT.get_value("PRINCIPAL_PROXY_SERVICE_PORT", default="443"),
            PATH: ENVIRONMENT.get_value("PRINCIPAL_PROXY_SERVICE_PATH", default="/r/insights-services"),
            SOURCE_CERT: ENVIRONMENT.bool("PRINCIPAL_PROXY_SERVICE_SOURCE_CERT", default=False),
            SSL_VERIFY: ENVIRONMENT.bool("PRINCIPAL_PROXY_SERVICE_SSL_VERIFY", default=True),
            USER_ENV: ENVIRONMENT.get_value("PRINCIPAL_PROXY_USER_ENV", default="env"),
            CLIENT_ID: ENVIRONMENT.get_value("PRINCIPAL_PROXY_CLIENT_ID", default="client_id"),
            API_TOKEN: ENVIRONMENT.get_value("PRINCIPAL_PROXY_API_TOKEN", default="token"),
            CLIENT_CERT_PATH: settings.BOP_CLIENT_CERT_PATH,
        }
        return proxy_conn_info

    @bop_request_time_tracking.time()
    def _request_principals(
        self,
        url,
        org_id=None,
        org_id_filter=False,
        method=requests.get,
        params=None,
        data=None,
        return_id=False,  # noqa: C901
    ):
        """Send request to proxy service."""
        metrics_method = method.__name__.upper()
        if params and params.get("username_only") == "true":
            principals = Principal.objects.filter(type="user", tenant__org_id=org_id, cross_account=False)
            if data and "users" in data:
                principals = principals.filter(username__in=data["users"])
            userList = [dict(username=principal.username) for principal in principals]
            offset = params.get("offset", 0)
            limit = params.get("limit", len(userList))
            paginatedUserList = userList[offset : offset + limit]  # noqa: E203
            return dict(data=paginatedUserList, userCount=len(userList), status_code=200)

        if settings.BYPASS_BOP_VERIFICATION:
            to_return = []
            if data is None:
                for principal in Principal.objects.filter(type="user", tenant__org_id=org_id, cross_account=False):
                    to_return.append(
                        dict(
                            username=principal.username,
                            first_name="foo",
                            last_name="bar",
                            email="baz",
                            user_id="51736777",
                            type=principal.type,
                        )
                    )
            elif "users" in data:
                for principal in data["users"]:
                    to_return.append(
                        dict(
                            username=principal,
                            first_name="foo",
                            last_name="bar",
                            email="baz",
                            user_id=principal,
                            type="user",
                        )
                    )
            elif "primaryEmail" in data:
                # We can't fake a lookup for an email address, so we won't try.
                pass
            bop_request_status_count.labels(method=metrics_method, status=200).inc()
            return dict(data=to_return, status_code=200, userCount=len(to_return))
        headers = {USER_ENV_HEADER: self.user_env, CLIENT_ID_HEADER: self.client_id, API_TOKEN_HEADER: self.api_token}
        unexpected_error = {
            "detail": "Unexpected error.",
            "status": str(status.HTTP_500_INTERNAL_SERVER_ERROR),
            "source": "principals",
        }
        try:
            kwargs = {"headers": headers, "params": params, "json": data, "verify": self.ssl_verify}
            if self.source_cert:
                kwargs["verify"] = self.client_cert_path
            response = method(url, **kwargs)
        except requests.exceptions.ConnectionError as conn:
            LOGGER.error("Unable to connect for URL %s with error: %s", url, conn)
            resp = {"status_code": status.HTTP_500_INTERNAL_SERVER_ERROR, "errors": [unexpected_error]}
            bop_request_status_count.labels(method=metrics_method, status=resp.get("status_code")).inc()
            return resp

        error = None
        resp = {"status_code": response.status_code}
        if response.status_code == status.HTTP_200_OK:
            """Testing if account numbers match"""
            try:
                principal_list = []
                if data:
                    principal_list = data.get("users")
                data = response.json()
                LOGGER.info(
                    "Response directly from BOP for org id %s and principal %s. Status: %s, Response: %s",
                    org_id,
                    str(principal_list),
                    str(response.status_code),
                    str(data),
                )
                if isinstance(data, dict):
                    userList = self._process_data(data.get("users"), org_id, org_id_filter, return_id)
                    resp["data"] = {"userCount": data.get("userCount"), "users": userList}
                else:
                    userList = self._process_data(
                        data, org_id=org_id, org_id_filter=org_id_filter, return_id=return_id
                    )
                    resp["data"] = userList
            except ValueError:
                resp["status_code"] = status.HTTP_500_INTERNAL_SERVER_ERROR
                error = unexpected_error

        elif response.status_code == status.HTTP_404_NOT_FOUND:
            error = {"detail": "Not Found.", "status": str(response.status_code), "source": "principals"}
        else:
            LOGGER.error("Error calling URL %s -- status=%d", url, response.status_code)
            error = unexpected_error
            error["status"] = str(response.status_code)
        if error:
            resp["errors"] = [error]
        bop_request_status_count.labels(method=metrics_method, status=resp.get("status_code")).inc()
        return resp

    def request_principals(self, org_id=None, input=None, limit=None, offset=None, options={}):
        """Request principals for an account."""
        if input:
            payload = input
            account_principals_path = f"/v3/accounts/{org_id}/usersBy"
            method = requests.post
        else:
            account_principals_path = f"/v3/accounts/{org_id}/users"
            method = requests.get
            payload = None

        params = self._create_params(limit, offset, options)
        url = "{}://{}:{}{}{}".format(self.protocol, self.host, self.port, self.path, account_principals_path)

        return self._request_principals(
            url, org_id=org_id, params=params, org_id_filter=False, method=method, data=payload
        )

    def fetch_account_org_mapping(self, account_ids):
        """Fetch account_id to org_id mapping from BOP.

        Args:
            account_ids (list): List of account IDs to fetch mapping for

        Returns:
            dict: Mapping of account_id to org_id, or None if error
        """
        if not account_ids:
            LOGGER.warning("No account_ids provided to fetch from BOP")
            return {}

        account_mapping_path = "/v2/accountMapping/orgIds"
        url = f"{self.protocol}://{self.host}:{self.port}{self.path}{account_mapping_path}"

        try:
            headers = {
                USER_ENV_HEADER: self.user_env,
                CLIENT_ID_HEADER: self.client_id,
                API_TOKEN_HEADER: self.api_token,
                "Content-Type": "application/json",
            }
            kwargs = {"headers": headers, "json": account_ids, "verify": self.ssl_verify}
            if self.source_cert:
                kwargs["verify"] = self.client_cert_path

            LOGGER.info(f"Fetching account-org mapping from BOP for {len(account_ids)} accounts")
            response = requests.post(url, **kwargs)

            if response.status_code == status.HTTP_200_OK:
                mapping = response.json()
                LOGGER.info(f"Successfully fetched {len(mapping)} account-org mappings from BOP")
                bop_request_status_count.labels(method="POST", status=200).inc()
                return mapping
            else:
                LOGGER.error(f"BOP returned status {response.status_code}: {response.text}")
                bop_request_status_count.labels(method="POST", status=response.status_code).inc()
                return None
        except Exception as e:
            LOGGER.error(f"Error fetching account-org mapping from BOP: {str(e)}")
            bop_request_status_count.labels(method="POST", status=500).inc()
            return None

    def request_filtered_principals(self, principals, org_id=None, limit=None, offset=None, options={}):
        """Request specific principals for an account."""
        if org_id is None:
            org_id_filter = False
        else:
            org_id_filter = True
        if not principals:
            return {"status_code": status.HTTP_200_OK, "data": []}

        filtered_principals_path = "/v1/users"
        params = self._create_params(limit, offset, options)
        payload = {"users": principals}
        url = "{}://{}:{}{}{}".format(self.protocol, self.host, self.port, self.path, filtered_principals_path)

        return_id = False if options.get("return_id") is None else True
        return self._request_principals(
            url,
            org_id=org_id,
            org_id_filter=org_id_filter,
            method=requests.post,
            params=params,
            data=payload,
            return_id=return_id,
        )


def external_principal_to_user(principal: dict) -> User:
    """Convert external principal to the common User object."""
    user = User()
    user.user_id = principal.get("user_id")
    user.org_id = principal.get("org_id")
    user.username = principal.get("username")
    user.is_active = principal.get("is_active", False)
    user.admin = principal.get("is_org_admin", False)
    return user


def get_user_id_from_request(request) -> str | None:
    """
    Get user_id from a request using multiple lookup strategies.

    Tries to get user_id from (in order of precedence):
    1. Principal from database (via get_principal_for_auth)
    2. request.user.user_id attached to the request
    3. IT service via PrincipalProxy

    Args:
        request: The HTTP request object

    Returns:
        str: The user_id, or None if not found
    """
    from management.utils import get_principal_for_auth

    # 1. Try to get from Principal in database
    principal = get_principal_for_auth(request)
    if principal is not None:
        user_id = getattr(principal, "user_id", None)
        if user_id:
            return user_id

    # 2. Try to get from request.user.user_id
    user_id = getattr(request.user, "user_id", None)
    if user_id:
        return user_id

    # 3. Fall back to IT service via PrincipalProxy
    username = getattr(request.user, "username", None)
    if not username:
        LOGGER.debug("No username available from request.user for user_id lookup")
        return None

    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        LOGGER.debug("No org_id available from request.user for user_id lookup")
        return None

    try:
        proxy = PrincipalProxy()
        resp = proxy.request_filtered_principals([username], org_id=org_id, options={"return_id": True})
    except requests.exceptions.RequestException as e:
        LOGGER.warning("PrincipalProxy request failed during user_id lookup: %s: %s", type(e).__name__, e)
        return None

    if resp.get("status_code") != 200 or not resp.get("data"):
        LOGGER.debug("Failed to retrieve user_id from IT service for username: %s", username)
        return None

    user_id = resp["data"][0].get("user_id")
    if not user_id:
        LOGGER.debug("IT service response missing user_id for username: %s", username)
        return None

    LOGGER.debug("Retrieved user_id from IT service via PrincipalProxy")
    return user_id


def get_kessel_principal_id(request) -> str | None:
    """
    Get the principal ID formatted for Kessel API from a request.

    This utility function looks up the user_id using multiple strategies
    and formats it as a Kessel principal resource ID.

    Args:
        request: The HTTP request object

    Returns:
        str: The principal ID formatted for Kessel API (e.g., "localhost/user_id"),
             or None if user_id could not be determined
    """
    user_id = get_user_id_from_request(request)
    if not user_id:
        LOGGER.debug("user_id is None after all lookup attempts")
        return None

    return Principal.user_id_to_principal_resource_id(user_id)
