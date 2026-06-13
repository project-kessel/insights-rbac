#
# Copyright 2019 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Helper utilities for management module."""

import logging
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import ClassVar, Optional, TypedDict
from uuid import UUID

import grpc
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _
from kessel.auth import OAuth2ClientCredentials
from kessel.grpc import oauth2_call_credentials
from management.authorization.invalid_token import InvalidTokenError
from management.authorization.missing_authorization import MissingAuthorizationError
from management.authorization.token_validator import TokenValidator
from management.cache import PrincipalCache
from management.models import Access, Group, Policy, Principal, Role
from management.permissions.principal_access import PrincipalAccessPermission
from management.principal.it_service import ITService
from management.principal.proxy import PrincipalProxy
from rest_framework import serializers
from rest_framework.fields import UUIDField
from rest_framework.request import Request
from rest_framework.serializers import ValidationError

from api.common import RH_RBAC_ACCOUNT, RH_RBAC_CLIENT_ID, RH_RBAC_ORG_ID, RH_RBAC_PSK
from api.models import Tenant, User

USERNAME_KEY = "username"
APPLICATION_KEY = "application"
PRINCIPAL_CACHE = PrincipalCache()
PRINCIPAL_PERMISSION_INSTANCE = PrincipalAccessPermission()
SERVICE_ACCOUNT_KEY = "service-account"


logger = logging.getLogger(__name__)

# Configure OAuth credentials with direct token URL for Inventory API
inventory_auth_credentials = OAuth2ClientCredentials(
    client_id=settings.INVENTORY_API_CLIENT_ID,
    client_secret=settings.INVENTORY_API_CLIENT_SECRET,
    token_endpoint=settings.INVENTORY_API_TOKEN_URL,  # Direct token endpoint
)

call_credentials = oauth2_call_credentials(inventory_auth_credentials)


@contextmanager
def create_client_channel(addr):
    """Create secure channel for grpc requests for relations api.

    Uses insecure channel in development/Clowder environments.
    Uses TLS in production environments.
    """
    if settings.DEVELOPMENT or os.getenv("CLOWDER_ENABLED", "false").lower() == "true":
        # Flag for local dev or Clowder (avoids ssl error)
        channel = grpc.insecure_channel(addr)
        yield channel
    else:
        # Use TLS for secure channel in production
        ssl_credentials = grpc.ssl_channel_credentials()
        secure_channel = grpc.secure_channel(addr, ssl_credentials)
        yield secure_channel


@contextmanager
def create_client_channel_inventory(addr):
    """Create secure channel for grpc requests for inventory api."""
    if settings.DEVELOPMENT or os.getenv("CLOWDER_ENABLED", "false").lower() == "true":
        channel = grpc.insecure_channel(addr)
        yield channel
    else:
        # Combine with TLS for secure channel
        ssl_credentials = grpc.ssl_channel_credentials()
        channel_credentials = grpc.composite_channel_credentials(ssl_credentials, call_credentials)
        secure_channel = grpc.secure_channel(addr, channel_credentials)
        yield secure_channel


@contextmanager
def create_client_channel_relation(addr):
    """Create secure channel for grpc requests for relations api.

    Uses insecure channel in development/Clowder environments.
    Uses TLS in production environments.
    Authentication is handled via JWT tokens passed in gRPC metadata.
    """
    if settings.DEVELOPMENT or os.getenv("CLOWDER_ENABLED", "false").lower() == "true":
        # Flag for local dev or Clowder (avoids ssl error)
        channel = grpc.insecure_channel(addr)
        yield channel
    else:
        # Use TLS for secure channel in production
        ssl_credentials = grpc.ssl_channel_credentials()
        secure_channel = grpc.secure_channel(addr, ssl_credentials)
        yield secure_channel


def validate_psk(psk, client_id):
    """Validate the PSK for the client."""
    psks = settings.SERVICE_PSKS
    client_config = psks.get(client_id, {})
    primary_key = client_config.get("secret")
    alt_key = client_config.get("alt-secret")

    if psks:
        return psk == primary_key or psk == alt_key

    return False


def build_user_from_psk(request):
    """Build a user from the PSK."""
    req_id = getattr(request, "req_id", None)
    user = None
    request_psk = request.META.get(RH_RBAC_PSK)
    account = request.META.get(RH_RBAC_ACCOUNT)
    org_id = request.META.get(RH_RBAC_ORG_ID)
    client_id = request.META.get(RH_RBAC_CLIENT_ID)
    has_system_auth_headers = request_psk and org_id and client_id

    if not has_system_auth_headers:
        # Missing required headers - not a PSK auth attempt, will fall through to token auth
        return None

    if not validate_psk(request_psk, client_id):
        logger.info(
            "S2S PSK auth failed: invalid PSK for client_id [request_id=%s, client_id=%s, org_id=%s, path=%s]",
            req_id,
            client_id,
            org_id,
            request.path,
        )
        return None

    user = User()
    user.username = client_id
    user.account = account
    user.org_id = org_id
    user.admin = True
    user.system = True
    logger.info(
        "S2S PSK auth successful [request_id=%s, client_id=%s, org_id=%s, path=%s]",
        req_id,
        client_id,
        org_id,
        request.path,
    )
    return user


class SystemUserConfig(TypedDict, total=False):
    """Configuration for a system user.

    This TypedDict defines the JSON schema for system users.

    Attributes:
        admin (bool): Whether the user has administrative privileges.
        is_service_account (bool): Whether the user is a service account.
        allow_any_org (bool): Whether the user is allowed to access any organization via system auth headers.
    """

    admin: bool
    is_service_account: bool
    allow_any_org: bool


def build_system_user_from_token(request, token_validator: TokenValidator) -> Optional[User]:
    """Build a system user from the token."""
    req_id = getattr(request, "req_id", None)
    # Token validator class uses a singleton
    try:
        user = token_validator.get_user_from_bearer_token(request)
        if not user:
            logger.info(
                "S2S token auth failed: no user returned from token validator [request_id=%s, path=%s]",
                req_id,
                request.path,
            )
            return None

        system_users: dict[str, SystemUserConfig] = settings.SYSTEM_USERS
        if user.user_id not in system_users:
            logger.info(
                "S2S token auth failed: user_id not in SYSTEM_USERS [request_id=%s, user_id=%s, path=%s]",
                req_id,
                user.user_id,
                request.path,
            )
            return None

        system_user = system_users[user.user_id]
        user.username = user.username or user.user_id
        user.system = True
        user.admin = system_user.get("admin", False)
        user.is_service_account = system_user.get("is_service_account", False)
        if system_user.get("allow_any_org", False):
            user.account = request.META.get(RH_RBAC_ACCOUNT, user.account)
            user.org_id = request.META.get(RH_RBAC_ORG_ID, user.org_id)
        # Could allow authn without org_id, but this breaks some code paths
        # which assume there is either no user, or a user with an org_id.
        # An AnonymousUser does not work yet.
        # Hence, if no org_id, consider authentication invalid.
        if not user.org_id:
            logger.info(
                "S2S token auth failed: no org_id available [request_id=%s, user_id=%s, path=%s]",
                req_id,
                user.user_id,
                request.path,
            )
            return None

        header_org_id = request.META.get(RH_RBAC_ORG_ID, user.org_id)
        if user.org_id != header_org_id:
            logger.warning(
                "S2S token auth failed: token org_id does not match org_id header "
                "[request_id=%s, user_id=%s, token_org_id=%s, header_org_id=%s, path=%s]",
                req_id,
                user.user_id,
                user.org_id,
                header_org_id,
                request.path,
            )
            return None

        logger.info(
            "S2S token auth successful [request_id=%s, user_id=%s, org_id=%s, is_admin=%s, path=%s]",
            req_id,
            user.user_id,
            user.org_id,
            user.admin,
            request.path,
        )
        return user

    except MissingAuthorizationError:
        logger.info(
            "S2S token auth failed: missing authorization header [request_id=%s, path=%s]", req_id, request.path
        )
        return None
    except InvalidTokenError as e:
        logger.info(
            "S2S token auth failed: invalid token [request_id=%s, path=%s, error=%s]", req_id, request.path, str(e)
        )
        return None


def get_principal_from_request(request, *, ignore_username_query_param=False):
    """Obtain principal from the request object.

    Args:
        request: The HTTP request object
        ignore_username_query_param: If True, ignore the username query parameter and always
                                     use request.user.username. This should be True for
                                     authorization checks to prevent privilege confusion.

    Returns:
        Principal object for the resolved username
    """
    current_user = request.user.username
    username = current_user
    from_query = False

    if not ignore_username_query_param:
        qs_user = request.query_params.get(USERNAME_KEY)
        if qs_user and not PRINCIPAL_PERMISSION_INSTANCE.has_permission(request=request, view=None):
            raise PermissionDenied()
        if qs_user:
            username = qs_user
            from_query = True

    return get_principal(username, request, verify_principal=from_query, from_query=from_query)


def get_principal_for_auth(request):
    """Get principal from request for authorization purposes.

    This helper ensures the principal is resolved from the authenticated user
    (request.user.username) and never from a query parameter, preventing
    privilege confusion in authorization checks.

    Args:
        request: The HTTP request object

    Returns:
        Principal object for the authenticated user
    """
    return get_principal_from_request(request, ignore_username_query_param=True)


def get_principal(
    username: str,
    request: Request,
    verify_principal: bool = True,
    from_query: bool = False,
    user_tenant: Optional[Tenant] = None,
) -> Principal:
    """Get principals from username.

    The service account usernames are not being validated for now because that would require for the clients to send
    tokens with the "api.iam.service_accounts" scope. That conflicts with the following two use cases:

    - A user sends a request to the /access/?username=service-account-<uuid> endpoint.
    - A service account makes a request to /access.

    In these two cases, due to how "get_principal_from_request" works, we would need to validate the service account
    against IT, but the only way to do it is by using a bearer token that we can only obtain from the incoming request.

    Until RBAC is given some other means to validate the service accounts, we are skipping that validation. Also, this
    does not affect the other endpoints where we need to validate service accounts —listed below just in case—, because
    there the bearer token with that claim is a must to be able to manage the service accounts. The endpoints that are
    fine are:

    - GET /principals/?type=service-account
    - GET /groups/{uuid}/principals/?principal_type=service-account
    - POST /groups/{uuid}/principals/ with a service account payload
    - DELETE /groups/{uuid}/principals/?service-account=<uuid>.
    """
    # First check if principal exist on our side, if not call BOP to check if user exist in the account.
    tenant: Tenant = request.tenant if not user_tenant else user_tenant
    is_username_service_account = ITService.is_username_service_account(username)

    try:
        # If the username was provided through a query we must verify if it exists in the corresponding services first.
        if from_query and not is_username_service_account:
            verify_principal_with_proxy(username=username, request=request, verify_principal=verify_principal)

        principal = PRINCIPAL_CACHE.get_principal(tenant.org_id, username)
        if not principal:
            principal = Principal.objects.get(username__iexact=username, tenant=tenant)
            PRINCIPAL_CACHE.cache_principal(org_id=tenant.org_id, principal=principal)

    except Principal.DoesNotExist:
        # If the "from query" parameter was specified, the username was validated above, so there is no need to
        # validate it again.
        if not from_query and not is_username_service_account:
            verify_principal_with_proxy(username=username, request=request, verify_principal=verify_principal)

        if is_username_service_account:
            client_id: uuid.UUID = ITService.extract_client_id_service_account_username(username)

            principal, _ = Principal.objects.get_or_create(
                username=username,
                tenant=tenant,
                type=SERVICE_ACCOUNT_KEY,
                service_account_id=client_id,
            )
        else:
            # Avoid possible race condition if the user was created while checking BOP
            principal, _ = Principal.objects.get_or_create(username=username, tenant=tenant)
            PRINCIPAL_CACHE.cache_principal(org_id=tenant.org_id, principal=principal)

    return principal


def verify_principal_with_proxy(username, request, verify_principal=True):
    """Verify username through the BOP."""
    if verify_principal:
        org_id = request.user.org_id
        proxy = PrincipalProxy()
        resp = proxy.request_filtered_principals([username], org_id=org_id, options=request.query_params)

        if isinstance(resp, dict) and "errors" in resp:
            raise Exception("Dependency error: request to get users from dependent service failed.")

        if not resp.get("data"):
            key = "detail"
            message = "No data found for principal with username '{}'.".format(username)
            raise serializers.ValidationError({key: _(message)})

        return resp


def policies_for_groups(groups):
    """Gathers all policies for the given groups."""
    policies = Policy.objects.filter(group__in=set(groups))
    return set(policies)


def roles_for_policies(policies):
    """Gathers all roles for the given policies."""
    roles = Role.objects.filter(policies__in=set(policies))
    return set(roles)


def access_for_roles(roles, param_applications):
    """Gathers all access for the given roles and application(s)."""
    if param_applications:
        param_applications_list = param_applications.split(",")
        access = Access.objects.filter(role__in=roles).filter(permission__application__in=param_applications_list)
    else:
        access = Access.objects.filter(role__in=roles)
    return set(access)


def groups_for_principal(principal: Principal, tenant, **kwargs):
    """Gathers all groups for a principal, including the default."""
    if principal.cross_account:
        return set()
    assigned_group_set = principal.group.all()

    # Only user principals should be able to get permissions from the default groups. For service accounts, customers
    # need to explicitly add the service accounts to a group.
    if principal.type == "user":
        admin_default_group_set = (
            Group.admin_default_set().filter(tenant=tenant) or Group.admin_default_set().public_tenant_only()
        )
        platform_default_group_set = (
            Group.platform_default_set().filter(tenant=tenant) or Group.platform_default_set().public_tenant_only()
        )
    else:
        admin_default_group_set = Group.objects.none()
        platform_default_group_set = Group.objects.none()

    prefetch_lookups = kwargs.get("prefetch_lookups_for_groups")

    if prefetch_lookups:
        assigned_group_set = assigned_group_set.prefetch_related(prefetch_lookups)

        if principal.type == "user":
            platform_default_group_set = platform_default_group_set.prefetch_related(prefetch_lookups)

    if kwargs.get("is_org_admin"):
        return set(assigned_group_set | platform_default_group_set | admin_default_group_set)

    return set(assigned_group_set | platform_default_group_set)


def policies_for_principal(principal, tenant, **kwargs):
    """Gathers all policies for a principal."""
    groups = groups_for_principal(principal, tenant, **kwargs)
    return policies_for_groups(groups)


def roles_for_principal(principal, tenant, **kwargs):
    """Gathers all roles for a principal."""
    if principal.cross_account:
        return roles_for_cross_account_principal(principal)
    policies = policies_for_principal(principal, tenant, **kwargs)
    return roles_for_policies(policies)


def access_for_principal(principal, tenant, **kwargs):
    """Gathers all access for a principal for an application."""
    application = kwargs.get(APPLICATION_KEY)
    roles = roles_for_principal(principal, tenant, **kwargs)
    access = access_for_roles(roles, application)
    return access


def queryset_by_id(objects, clazz, **kwargs):
    """Return a queryset of from the class ordered by id."""
    wanted_ids = [obj.id for obj in objects]
    prefetch_lookups = kwargs.get("prefetch_lookups_for_ids")
    query = clazz.objects.filter(id__in=wanted_ids).order_by("id")
    if prefetch_lookups:
        query = query.prefetch_related(prefetch_lookups)

    return query


def filter_queryset_by_tenant(queryset, tenant):
    """Limit queryset by appropriate tenant when serving from public schema."""
    return queryset.filter(tenant=tenant)


def validate_and_get_key(params, query_key, valid_values, default_value=None, required=True):
    """Validate and return the key."""
    value = params.get(query_key, default_value)
    if not value:
        if required:
            key = "detail"
            message = "Query parameter '{}' is required.".format(query_key)
            raise serializers.ValidationError({key: _(message)})
        if default_value:
            return default_value.lower()
        return None

    elif value.lower() not in valid_values:
        key = "detail"
        message = "{} query parameter value '{}' is invalid. {} are valid inputs.".format(
            query_key, value, [str(v) for v in valid_values]
        )
        raise serializers.ValidationError({key: _(message)})
    return value.lower()


def validate_and_get_key_multi(params, query_key, valid_values, default_value=None):
    """Validate and return multiple comma-separated values for a query key.

    Splits the query parameter on commas, strips whitespace and lowercases
    each value, then validates every value against *valid_values*.

    Returns a **list** of validated (lowercased) strings.
    """
    value = params.get(query_key, default_value)
    if not value:
        if default_value:
            return [default_value.lower()]
        return []

    fields = [v.strip().lower() for v in value.split(",") if v.strip()]
    for val in fields:
        if val not in valid_values:
            key = "detail"
            message = "{} query parameter value '{}' is invalid. Allowed values are {}.".format(
                query_key, val, [str(v) for v in valid_values]
            )
            raise serializers.ValidationError({key: _(message)})
    return fields


def validate_key(params, query_key, valid_values, default_value=None, required=True):
    """Validate a key and do not return the value."""
    value = params.get(query_key, default_value)
    if value.lower() not in valid_values:
        key = "detail"
        message = "{} query parameter value '{}' is invalid. {} are valid inputs.".format(
            query_key, value, valid_values
        )
        raise serializers.ValidationError({key: _(message)})


def value_to_list(value):
    """Ensure value is returned in a list if not already a list."""
    value_list = [value] if not isinstance(value, list) else value
    return value_list


def is_valid_uuid(value):
    """Return whether or not a value is a valid UUID."""
    try:
        UUID(str(value))
        return True
    except ValueError:
        return False


def clean_query_param(value, param_name):
    """Clean a query parameter: return None if empty/whitespace, raise 400 if NUL character present."""
    if value is None:
        return None
    if not value.strip():
        return None
    if "\x00" in value:
        message = f"The '{param_name}' query parameter contains invalid characters."
        raise serializers.ValidationError({param_name: message})
    return value


def normalize_blank_or_none(value: str | None) -> str | None:
    """Return None for empty/blank strings, pass through otherwise."""
    return (value and value.strip()) or None


def validate_uuid(uuid, key="UUID Validation"):
    """Verify UUID provided is valid."""
    try:
        UUID(uuid)
    except ValueError:
        key = key
        message = f"{uuid} is not a valid UUID."
        raise serializers.ValidationError({key: _(message)})


def as_uuid(value: str | UUID) -> UUID:
    """
    Convert a string (or UUID) to a UUID.

    A provided UUID is returned unchanged.
    """
    if isinstance(value, UUID):
        return value

    return UUID(value)


def validate_group_name(name):
    """Verify name provided is valid."""
    if name and name.lower() in ["custom default access", "default access"]:
        key = "Group name Validation"
        message = f"{name} is reserved, please use another name."
        raise serializers.ValidationError({key: _(message)})


def roles_for_cross_account_principal(principal):
    """Return roles for cross account principals."""
    _, user_id = principal.username.split("-")
    target_org = principal.tenant.org_id
    return Role.objects.filter(
        crossaccountrequest__target_org=target_org,
        crossaccountrequest__user_id=user_id,
        crossaccountrequest__status="approved",
        system=True,
    ).distinct()


def clear_pk(entry):
    """Clear the ID and PK values for provided postgres entry."""
    entry.id = None
    entry.pk = None


def account_id_for_tenant(tenant):
    """Return the account id from a tenant's name."""
    return tenant.tenant_name.replace("acct", "")


def get_admin_from_proxy(username, request):
    """Return org_admin status of a username from the proxy."""
    bop_resp = verify_principal_with_proxy(username, request, verify_principal=True)

    if not bop_resp.get("data"):
        key = "detail"
        message = "No data found for principal with username '{}'.".format(username)
        raise serializers.ValidationError({key: _(message)})

    index = next(
        (i for i, x in enumerate(bop_resp.get("data")) if x["username"].casefold() == username.casefold()),
        None,
    )

    if index is None:
        key = "detail"
        message = "No data found for principal with username '{}'.".format(username)
        raise serializers.ValidationError({key: _(message)})

    is_org_admin = bop_resp.get("data")[index]["is_org_admin"]
    return is_org_admin


def api_path_prefix():
    """Get api path prefix."""
    path_prefix = os.getenv("API_PATH_PREFIX", "api/")
    if path_prefix != "":
        if path_prefix.startswith("/"):
            path_prefix = path_prefix[1:]
        if not path_prefix.endswith("/"):
            path_prefix = path_prefix + "/"
    return path_prefix


PROBLEM_TITLES = {
    400: "The request payload contains invalid syntax.",
    401: "Authentication credentials were not provided or are invalid.",
    403: "You do not have permission to perform this action.",
    404: "Not found.",
    409: "Conflict.",
    500: "Unexpected error occurred.",
}

# RFC 9457 problem type URIs matching the TypeSpec ProblemType enum.
# Each URI identifies a specific problem category for machine-readable error handling.
PROBLEM_TYPES = {
    400: "http://project-kessel.org/problems/invalid-request",
    401: "http://project-kessel.org/problems/unauthenticated",
    403: "http://project-kessel.org/problems/insufficient-permission",
    404: "http://project-kessel.org/problems/not-found",
    500: "http://project-kessel.org/problems/internal-error",
}


def v2response_error_from_errors(errors, exc=None, context=None, problem_type=None):
    """Build a ProblemDetails-formatted error response from errors.

    Args:
        errors: List of error dicts with "detail", "status", and optional "source" keys.
        exc: The original exception (optional).
        context: DRF context dict with "request" (optional).
        problem_type: Explicit RFC 9457 problem type URI override. When set, this
            takes precedence over the default status-code-based lookup in PROBLEM_TYPES.
            Use for specialized problem types like "http://project-kessel.org/problems/already-exists".
    """
    detail = ""
    status_code = 0
    field_errors = []

    if errors and any(isinstance(error, dict) and "detail" in error for error in errors):
        detail = str(errors[0]["detail"])
        status_code = int(errors[0]["status"])

        for error in errors:
            if isinstance(error, dict) and "detail" in error:
                field_error = {"message": str(error["detail"])}
                if error.get("source"):
                    field_error["field"] = error["source"]
                field_errors.append(field_error)

    resolved_type = problem_type or PROBLEM_TYPES.get(status_code)

    response = {
        "status": status_code,
        "title": PROBLEM_TITLES.get(status_code, "An error occurred."),
        "detail": detail,
    }

    if resolved_type:
        response["type"] = resolved_type

    if field_errors:
        response["errors"] = field_errors

    if context and context.get("request") and context.get("request").method in ["PUT", "PATCH", "DELETE"]:
        response["instance"] = context.get("request").path

    return response


def raise_validation_error(source, message):
    """Construct a validation error and raise the error."""
    error = {source: [message]}
    raise ValidationError(error)


def flatten_validation_error(e: ValidationError):
    """Flatten a Django ValidationError into a list of (field, message) tuples."""
    if hasattr(e, "message_dict"):
        return [(field, str(msg)) for field, messages in e.message_dict.items() for msg in messages]
    elif hasattr(e, "messages"):
        return [("__all__", str(msg)) for msg in e.messages]
    else:
        return [("__all__", str(e))]


def is_permission_blocked_for_v1(permission_str, request=None):
    """
    Check if permission should be blocked from v1 API endpoints.

    This is used to hide permissions from v1 that are only meant for v2.

    Args:
        permission_str: The permission string to check (e.g., "rbac:role:read")
        request: Optional request object to check if this is a v1 API call

    Returns:
        True if the permission should be blocked from v1, False otherwise
    """
    # Safety checks
    if not permission_str:
        return False

    # Only apply to v1 requests - block from v1, show in v2
    if not request or not hasattr(request, "path"):
        return False

    if not request.path.startswith(f"/{api_path_prefix()}v1/"):
        return False

    # Check against block list using exact string matching
    block_list = getattr(settings, "V1_ROLE_PERMISSION_BLOCK_LIST", [])
    return permission_str in block_list


class FieldSelectionValidationError(Exception):
    """Exception raised when field selection validation fails."""

    def __init__(self, message: str):
        """Initialize with error message."""
        self.message = message
        super().__init__(self.message)


@dataclass
class FieldSelection:
    """Generic, config-driven field selection parser.

    Parses a fields query parameter that supports both root-level fields
    and nested object fields using the syntax: object(field1,field2) or field1,field2.

    Examples:
        - "last_modified"
        - "subject(group.name,group.user_count)"
        - "subject(id),role(name),last_modified"
    """

    VALID_ROOT_FIELDS: ClassVar[set] = set()
    VALID_NESTED_FIELDS: ClassVar[dict[str, set]] = {}

    root_fields: set = field(default_factory=set)
    nested_fields: dict[str, set] = field(default_factory=dict)

    def get_nested(self, name: str) -> set:
        """Return the parsed nested fields for name."""
        return self.nested_fields.get(name, set())

    def get_sub_object_fields(self, nested_name: str, sub_object: str) -> set:
        """Return sub-fields for a dotted sub-object within a nested field."""
        prefix = f"{sub_object}."
        return {f.removeprefix(prefix) for f in self.get_nested(nested_name) if f.startswith(prefix)}

    @classmethod
    def parse(cls, fields_param: Optional[str]) -> Optional["FieldSelection"]:
        """Parse a fields parameter string into a FieldSelection instance."""
        if not fields_param:
            return None

        selection = cls()
        invalid_fields: list[str] = []

        parts = cls._split_fields(fields_param)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Nested: object(field1,field2)
            match = re.match(r"(\w+)\(([^)]+)\)", part)
            if match:
                obj_name = match.group(1)
                obj_fields = {f.strip() for f in match.group(2).split(",")}

                valid_set = cls.VALID_NESTED_FIELDS.get(obj_name)
                if valid_set is None:
                    invalid_fields.append(f"Unknown object type: '{obj_name}'")
                else:
                    invalid = obj_fields - valid_set
                    if invalid:
                        invalid_fields.extend([f"{obj_name}({f})" for f in invalid])
                    selection.nested_fields.setdefault(obj_name, set()).update(obj_fields)
            else:
                if part not in cls.VALID_ROOT_FIELDS:
                    invalid_fields.append(f"Unknown field: '{part}'")
                selection.root_fields.add(part)

        if invalid_fields:
            error_parts = [f"Invalid field(s): {', '.join(invalid_fields)}."]
            for obj_name, valid_set in sorted(cls.VALID_NESTED_FIELDS.items()):
                error_parts.append(f"Valid {obj_name} fields: {sorted(valid_set)}.")
            error_parts.append(f"Valid root fields: {sorted(cls.VALID_ROOT_FIELDS)}.")
            raise FieldSelectionValidationError(" ".join(error_parts))

        return selection

    @staticmethod
    def _split_fields(fields_str: str) -> list[str]:
        """Split a fields string by comma, respecting parentheses."""
        if not fields_str:
            return []

        parts: list[str] = []
        start = 0
        depth = 0

        for i, char in enumerate(fields_str):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(fields_str[start:i].strip())
                start = i + 1

        parts.append(fields_str[start:].strip())
        return parts


class UUIDStringField(UUIDField):
    """A UUID field that is always represented as a hex string with hyphens (the hex_verbose) format."""

    _pattern = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def __init__(self, **kwargs):
        """Create a new UUIDStringField."""
        format = kwargs.get("format")

        if format not in (None, "hex_verbose"):
            raise ValueError("Format, if provided, must be hex_verbose")

        super().__init__(**kwargs)

    def to_internal_value(self, data):
        """Convert data to a UUID; it must either already be a UUID or be a hex string with hyphens."""
        if isinstance(data, UUID):
            return super().to_internal_value(data)

        if isinstance(data, str):
            if not re.fullmatch(self._pattern, data):
                self.fail("invalid", value=data)

            return super().to_internal_value(data)

        self.fail("invalid", value=data)
        raise AssertionError("unreachable")
