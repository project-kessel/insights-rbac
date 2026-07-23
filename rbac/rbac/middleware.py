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

"""Custom RBAC Middleware."""

import binascii
import json
import logging
from json.decoder import JSONDecodeError

from django.conf import settings
from django.core.handlers.wsgi import WSGIRequest
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, QueryDict
from django.urls import resolve, reverse
from feature_flags import FEATURE_FLAGS
from management.authorization.token_validator import ITSSOTokenValidator, TokenValidator
from management.cache import TenantCache
from management.models import Principal
from management.principal.proxy import PrincipalProxy
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.tenant_service import get_tenant_bootstrap_service
from management.tenant_service.tenant_service import TenantBootstrapService
from management.utils import APPLICATION_KEY, access_for_principal, build_system_user_from_token, build_user_from_psk
from prometheus_client import Counter
from rest_framework import status

from api.common import RH_IDENTITY_HEADER, RH_INSIGHTS_REQUEST_ID
from api.models import Tenant, User
from api.serializers import extract_header
from rbac.a2s import is_a2s_path as _is_a2s_path

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name
req_sys_counter = Counter(
    "rbac_req_type_total",
    "Tracks a count of requests to RBAC tracking those made on behalf of the system or a principal.",
    ["behalf", "method", "view", "status"],
)
TENANTS = TenantCache()


def catch_integrity_error(func):
    """Catch IntegrityErrors that are raised during process_request."""

    def inner(self, request):
        try:
            return func(self, request)
        except IntegrityError as e:
            payload = {
                "code": 400,
                "message": f"IntegrityError while processing request for org_id: {request.user.org_id}",
            }
            logger.error(f"{payload['message']}\n{e.__str__()}")
            return HttpResponse(json.dumps(payload), content_type="application/json", status=400)

    return inner


def is_no_auth(request):
    """Check condition for needing to authenticate the user."""
    no_auth_list = [
        reverse("v1_api:server-ready"),
        reverse("v1_api:server-status"),
        reverse("v1_api:openapi"),
        "/metrics",
    ]

    # Only add V2 OpenAPI endpoint if V2 APIs are enabled
    if settings.V2_APIS_ENABLED:
        no_auth_list.append(reverse("v2_api:openapi"))

    return request.path in no_auth_list


class HttpResponseUnauthorizedRequest(HttpResponse):
    """A subclass of HttpResponse to return a 401.

    Used if identity header is not sent.
    """

    status_code = 401


PROXY = PrincipalProxy()


def get_user_id(user: User):
    """Return the user ID for the given user."""
    user_id = user.user_id
    if not user_id:
        resp = PROXY.request_filtered_principals([user.username], org_id=user.org_id, options={"return_id": True})
        if isinstance(resp, dict) and "errors" in resp:
            logging.warning(resp.get("errors"))
            return
        if not resp.get("data"):
            logging.warning(f"No user found of user name {user.username}.")
            raise Http404()
        return resp["data"][0]["user_id"]
    return user.user_id


class IdentityHeaderMiddleware:
    """A subclass of RemoteUserMiddleware.

    Processes the provided identity found on the request.
    """

    header = RH_IDENTITY_HEADER
    bootstrap_service: TenantBootstrapService
    token_validator: TokenValidator = ITSSOTokenValidator()

    def __init__(self, get_response):
        """One-time configuration and initialization."""
        self.get_response = get_response
        # TODO: Lazy bootstrapping of tenants should use a synchronous replicator
        # In this case the replicator needs to include a precondition
        # which does not add the tuples if any others already exist for the tenant
        # (the tx will be rolled back in that case)
        self.bootstrap_service = get_tenant_bootstrap_service(OutboxReplicator(), get_user_id)

    def get_tenant(self, model, hostname, request):
        """Override the tenant selection logic."""
        tenant = TENANTS.get_tenant(request.user.org_id)
        if tenant is None:
            try:
                # If the tenant already exists, we assume it must be bootstrapped if dual writes are enabled.
                tenant = Tenant.objects.get(org_id=request.user.org_id)
                # Update account_id if missing and user has it (fixes regression from Phase 0 to Phase 1)
                needs_update = False
                if not tenant.ready:
                    tenant.ready = True
                    needs_update = True
                if tenant.account_id is None and request.user.account:
                    tenant.account_id = request.user.account
                    needs_update = True
                if needs_update:
                    tenant.save(update_fields=["ready", "account_id"])
            except Tenant.DoesNotExist:
                if request.user.system:
                    raise Http404()
                # Tenants are normally bootstrapped via principal job,
                # but there is a race condition where the user can use the service before the message is processed.
                try:
                    with transaction.atomic():
                        bootstrap = self.bootstrap_service.update_user(request.user, upsert=True, ready_tenant=True)
                    if bootstrap is None:
                        # User is inactive. Should never happen but just in case...
                        raise Http404()
                    tenant = bootstrap.tenant
                except IntegrityError:
                    # This would happen if between the time we first check for a tenant,
                    # and when we went to create one, another request or the listener job created one.
                    tenant = Tenant.objects.get(org_id=request.user.org_id)
            TENANTS.save_tenant(tenant)
        return tenant

    @staticmethod  # noqa: C901
    def _get_access_for_user(username, tenant):  # pylint: disable=too-many-locals,too-many-branches
        """Obtain access data for given username.

        Stubbed out to begin removal of RBAC on RBAC, with minimal disruption
        """
        principal = None
        access_list = None

        access = {
            "group": {"read": [], "write": []},
            "role": {"read": [], "write": []},
            "policy": {"read": [], "write": []},
            "principal": {"read": [], "write": []},
            "permission": {"read": [], "write": []},
        }

        try:  # pylint: disable=R1702
            principal = Principal.objects.get(username__iexact=username, tenant=tenant)
            kwargs = {APPLICATION_KEY: "rbac"}
            access_list = access_for_principal(principal, tenant, **kwargs)
            for access_item in access_list:  # pylint: disable=too-many-nested-blocks
                resource_type = access_item.permission.resource_type
                operation = access_item.permission.verb
                if operation == "*":
                    operation = "write"
                res_list = ["*"]
                if resource_type == "*":
                    for resource in ("group", "role", "policy", "principal", "permission"):
                        if (
                            resource in access.keys()
                            and operation in access.get(resource, {}).keys()  # noqa: W504
                            and isinstance(access.get(resource, {}).get(operation), list)  # noqa: W504
                        ):  # noqa: E127
                            access[resource][operation] += res_list
                            if operation == "write":
                                access[resource]["read"] += res_list
                elif (
                    resource_type in access.keys()
                    and operation in access.get(resource_type, {}).keys()  # noqa: W504
                    and isinstance(access.get(resource_type, {}).get(operation), list)  # noqa: W504
                ):
                    access[resource_type][operation] += res_list
                    if operation == "write":
                        access[resource_type]["read"] += res_list
                for res_type, res_ops_obj in access.items():
                    for op_type, op_list in res_ops_obj.items():
                        if "*" in op_list:
                            access[res_type][op_type] = ["*"]
        except Principal.DoesNotExist:
            return access

        return access

    @catch_integrity_error
    def __call__(self, request):
        """Code to be executed for each request before or after the view is called."""
        # Get request ID
        request.req_id = request.META.get(RH_INSIGHTS_REQUEST_ID)

        if any(
            [request.path.startswith(prefix) for prefix in settings.INTERNAL_API_PATH_PREFIXES]
        ) and not _is_a2s_path(request):
            # This request is for a private API endpoint (except _a2s/ which uses public auth)
            return self.get_response(request)

        if is_no_auth(request):
            return self.get_response(request)
        user = User()
        try:
            _, json_rh_auth = extract_header(request, self.header)
            user.account = json_rh_auth.get("identity", {}).get("account_number")
            user.org_id = json_rh_auth.get("identity", {}).get("org_id") or json_rh_auth.get("identity").get(
                "internal"
            ).get("org_id")

            user_info = json_rh_auth.get("identity", {}).get("user")
            if user_info:
                user.username = user_info["username"]
                user.admin = user_info.get("is_org_admin")
                user.internal = user_info.get("is_internal")
                user.user_id = user_info.get("user_id")
                user.system = False

            # RBAC might be contacted by service accounts too. In that case we make some assumptions:
            #
            # - The service account is never an organization administrator.
            # - The service account is never internal.
            # - The service account is never a system principal.
            service_account = json_rh_auth.get("identity", {}).get("service_account")
            if service_account:
                user.username = service_account.get("username")
                user.admin = False
                user.client_id = service_account.get("client_id")
                user.internal = False
                user.is_service_account = True
                user.user_id = None
                user.system = False

            # If we did not get the user information or service account information from the "x-rh-identity" header,
            # then the request is directly unauthorized.
            if not user_info and not service_account:
                if _is_a2s_path(request):
                    return self.get_response(request)
                logger.debug("x-rh-identity does not contain user_info or service_account keys: %s", json_rh_auth)
                return HttpResponseUnauthorizedRequest()

            # The service accounts must provide their client IDs for us to keep processing the request.
            if user.is_service_account and (not user.client_id or user.client_id.isspace()):
                return HttpResponse(
                    json.dumps(
                        {
                            "code": status.HTTP_400_BAD_REQUEST,
                            "message": "The client ID must be provided for the service account in the x-rh-identity"
                            "header.",
                        },
                    ),
                    content_type="application/json",
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if not user.org_id:
                payload = {
                    "code": 400,
                    "message": "An org_id must be provided in the identity header.",
                }
                return HttpResponse(json.dumps(payload), content_type="application/json", status=400)

            if self.should_load_user_permissions(request, user):
                try:
                    tenant = Tenant.objects.filter(org_id=user.org_id).get()
                except Tenant.DoesNotExist:
                    request.user = user
                    tenant = self.get_tenant(model=None, hostname=None, request=request)

                user.access = IdentityHeaderMiddleware._get_access_for_user(user.username, tenant)
            # Cross account request check
            internal = json_rh_auth.get("identity", {}).get("internal", {})
            if internal != {}:
                cross_account = internal.get("cross_access", False)
                if cross_account:
                    if not (user.internal and user_info.get("email").endswith("@redhat.com")):
                        logger.error("Cross account request permission denied. Requester is not internal user.")
                        return HttpResponseUnauthorizedRequest()
                    user.username = f"{user.org_id}-{user.user_id}"
        except (KeyError, TypeError, JSONDecodeError):
            if _is_a2s_path(request):
                return self.get_response(request)
            user = build_user_from_psk(request) or build_system_user_from_token(
                request, token_validator=self.token_validator
            )
            if not user:
                logger.error("Could not obtain identity on request.")
                return HttpResponseUnauthorizedRequest()
        except binascii.Error as error:
            logger.error("Could not decode header: %s.", error)
            raise error
        if user.username and (user.account or user.org_id):
            request.user = user
            request.tenant = self.get_tenant(model=None, hostname=None, request=request)

        response = self.get_response(request)

        # Code to be executed for each request/response after
        # the view is called.
        is_internal_request = any([request.path.startswith(prefix) for prefix in settings.INTERNAL_API_PATH_PREFIXES])
        is_system = False

        if hasattr(request, "user") and request.user:
            username = request.user.username
            if username:
                is_system = request.user.system

        behalf = "system" if is_system else "principal"

        req_sys_counter.labels(
            behalf=behalf,
            method=request.method,
            view=resolve(request.path).url_name,
            status=response.get("status_code"),
        ).inc()

        IdentityHeaderMiddleware.log_request(request, response, is_internal_request)
        return response

    @staticmethod
    def log_request(request, response, is_internal_request=False):
        """Log requests for identity middleware.

        Args:
            request (object): The request object
            response (object): The response object
            is_internal_request (bool): Boolean for if request is internal
        """
        query_string = ""
        is_admin = False
        is_system = False
        org_id = None
        username = None
        user_id = None
        req_id = getattr(request, "req_id", None)
        if request.META.get("QUERY_STRING"):
            query_string = "?{}".format(request.META.get("QUERY_STRING"))

        is_internal = False
        if hasattr(request, "user") and request.user:
            username = request.user.username
            if username:
                # rbac.api.models.User has these fields
                is_admin = request.user.admin
                org_id = request.user.org_id
                is_system = request.user.system
                user_id = request.user.user_id
                is_internal = getattr(request.user, "internal", False)
            else:
                # django.contrib.auth.models.AnonymousUser does not
                is_admin = is_system = False
                org_id = None

        # Todo: add some info back to logs
        """
        extras = {}

        if "ecs" in settings.LOGGING_HANDLERS:
            extras = {
                "http": {
                    "request": {
                        "body": {"bytes": sys.getsizeof(request.body)},
                        "bytes": sys.getsizeof(request),
                        "method": request.method,
                    },
                    "response": {
                        "body": {"bytes": sys.getsizeof(response.content)},
                        "bytes": sys.getsizeof(response),
                        "status_code": response.status_code,
                    },
                },
                "url": {
                    "original": request.path + query_string,
                    "path": request.path,
                    "query": query_string,
                    "port": request.get_port(),
                },
            }
        """

        log_object = {
            "method": request.method,
            "path": request.path + query_string,
            "status": response.status_code,
            "request_id": req_id,
            "org_id": org_id,
            "username": username,
            "user_id": user_id,
            "is_admin": is_admin,
            "is_system": is_system,
            "is_internal": is_internal,
            "is_internal_request": is_internal_request,
        }
        logger.info(log_object)

    def should_load_user_permissions(self, request: WSGIRequest, user: User) -> bool:
        """Decide whether RBAC should load the access permissions for the user based on the given request."""
        # Organization administrators will have already all the permissions so there is no need to load permissions for
        # them.
        if user.admin:
            return False

        # The access endpoint gets a lot of traffic, so we need to restrict for which queries we are actually going
        # to load the user permissions, since it is a very heavy operation. The following Jira tickets have more
        # details:
        #
        # - RHCLOUD-15394
        # - RHCLOUD-29631
        #
        # There is one use case where we need to load the user's permissions: whenever they want to query for their
        # or other users' permissions. In that case, we need to know if they're allowed to do so, and for that, we
        # need to preload their permissions to check them afterward in the subsequent permission checkers.
        if request.path.endswith("/access/") and request.method == "GET":
            query_params: QueryDict = request.GET
            return "username" in query_params and "application" in query_params
        else:
            return True


class DisableCSRF:  # pylint: disable=too-few-public-methods
    """Middleware to disable CSRF for 3scale usecase."""

    def __init__(self, get_response):
        """One-time configuration and initialization."""
        self.get_response = get_response

    def __call__(self, request):
        """Code to be executed for each request before or after the view is called."""
        setattr(request, "_dont_enforce_csrf_checks", True)
        return self.get_response(request)


class ReadOnlyApiMiddleware:
    """Middleware to enable read-only on APIs when configured."""

    def __init__(self, get_response):
        """One-time configuration and initialization."""
        self.get_response = get_response

    def __call__(self, request):
        """Code to be executed for each request before or after the view is called."""
        if self._should_deny_all_writes(request) or self._should_deny_v2_writes(request):
            return self._read_only_response()
        return self.get_response(request)

    def _is_write_request(self, request):
        """Determine whether or not the request is a write request."""
        write_methods = ["POST", "PUT", "PATCH", "DELETE"]
        return request.method in write_methods

    def _should_deny_all_writes(self, request):
        """Determine whether or not to deny all API writes."""
        resolver = resolve(request.path)
        api_namespace = resolver.app_name if resolver else ""
        return settings.READ_ONLY_API_MODE and self._is_write_request(request) and api_namespace != "internal"

    def _should_deny_v2_writes(self, request):
        """Determine whether or not to deny v2 writes."""
        resolver = resolve(request.path)
        api_namespace = resolver.app_name if resolver else ""
        return (
            FEATURE_FLAGS.is_v2_api_read_only_mode_enabled()
            and self._is_write_request(request)
            and api_namespace == "v2_management"
        )

    def _read_only_response(self):
        """Return a read-only API error response."""
        return HttpResponse(
            json.dumps({"error": "This API is currently in read-only mode. Please try again later."}),
            content_type="application/json",
            status=405,
        )
