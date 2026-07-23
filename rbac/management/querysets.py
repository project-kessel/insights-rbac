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
"""Queryset helpers for management module."""

from typing import Optional

from django.conf import settings
from django.db.models import Q, QuerySet
from django.db.models.aggregates import Count
from django.urls import reverse
from django.utils.translation import gettext as _
from management.group.model import Group
from management.permissions.role_access import RoleAccessPermission
from management.policy.model import Policy
from management.principal.it_service import ITService
from management.role.model import Access, Role
from management.utils import (
    APPLICATION_KEY,
    access_for_principal,
    filter_queryset_by_tenant,
    get_admin_from_proxy,
    get_principal,
    get_principal_from_request,
    groups_for_principal,
    policies_for_principal,
    queryset_by_id,
    roles_for_principal,
    validate_and_get_key,
)
from rest_framework import permissions, serializers
from rest_framework.request import Request

from api.models import Tenant, User
from rbac.env import ENVIRONMENT

SCOPE_KEY = "scope"
ORG_ID_SCOPE = "org_id"
PRINCIPAL_SCOPE = "principal"
VALID_SCOPES = [ORG_ID_SCOPE, PRINCIPAL_SCOPE]
PRINCIPAL_QUERYSET_MAP = {
    Access.__name__: access_for_principal,
    Group.__name__: groups_for_principal,
    Policy.__name__: policies_for_principal,
    Role.__name__: roles_for_principal,
}


def get_annotated_groups(queryset):
    """Return an annotated set of groups for the tenant."""
    return queryset.annotate(
        principalCount=Count("principals", filter=Q(principals__type="user"), distinct=True),
        policyCount=Count("policies", distinct=True),
    )


def user_has_perm(request, resource):
    """Check to determine if user has RBAC access perms."""
    access = request.user.access
    access_op = "read"
    if request.method in ("POST", "PUT"):
        access_op = "write"
    res_list = access.get(resource, {}).get(access_op, [])
    if not res_list:
        return "None"
    if "*" in res_list:
        return "All"
    return res_list


def has_group_all_access(request):
    """Quick check to determine if a request should have access to all groups on a tenant."""
    return (
        ENVIRONMENT.get_value("ALLOW_ANY", default=False, cast=bool)
        or request.user.admin
        or (request.path == reverse("v1_management:group-list") and request.method == "GET")
    )


def get_group_queryset(request, args=None, kwargs=None, base_query: Optional[QuerySet] = None):
    """Obtain the queryset for groups."""
    queryset = _filter_admin_default(request, _gather_group_querysets(request, args, kwargs, base_query=base_query))
    return _filter_default_groups(request, queryset)


def _gather_group_querysets(request, args, kwargs, base_query: Optional[QuerySet] = None):
    """Decide which groups to provide for request."""
    base_query = base_query if base_query is not None else get_annotated_groups(Group.objects.all())

    username = request.query_params.get("username")

    scope = validate_and_get_key(request.query_params, SCOPE_KEY, VALID_SCOPES, ORG_ID_SCOPE)
    if scope != ORG_ID_SCOPE and not username:
        return get_object_principal_queryset(request, scope, Group)

    default_group_set = (
        Group.platform_default_set().filter(tenant=request.tenant) or Group.platform_default_set().public_tenant_only()
    )

    exclude_username = request.query_params.get("exclude_username")

    if username and exclude_username:
        key = "detail"
        message = "Not possible to use both parameters [username, exclude_username]."
        raise serializers.ValidationError({key: _(message)})

    if not username and kwargs:
        username = kwargs.get("principals")
    if username:
        principal = get_principal(username, request)
        principal_groups = principal.group.all()
        principal_group_uuids = []
        for group in principal_groups:
            principal_group_uuids.append(group.uuid)

        if principal.cross_account:
            return Group.objects.none()
        return (
            filter_queryset_by_tenant(
                get_annotated_groups(base_query.filter(uuid__in=principal_group_uuids)), request.tenant
            )
            | default_group_set
        )

    if exclude_username:
        return filter_queryset_by_tenant(
            get_annotated_groups(base_query.exclude(principals__username__iexact=exclude_username)), request.tenant
        )

    if has_group_all_access(request):
        return filter_queryset_by_tenant(base_query, request.tenant) | default_group_set

    access = user_has_perm(request, "group")

    if access == "All":
        return filter_queryset_by_tenant(base_query, request.tenant) | default_group_set
    if access == "None":
        return Group.objects.none()

    return filter_queryset_by_tenant(Group.objects.filter(uuid__in=access), request.tenant) | default_group_set


def annotate_roles_with_counts(queryset):
    """Annotate the queryset for roles with counts."""
    return queryset.annotate(policyCount=Count("policies", distinct=True), accessCount=Count("access", distinct=True))


def get_role_queryset(request) -> QuerySet:
    """Obtain the queryset for roles."""
    scope = validate_and_get_key(request.query_params, SCOPE_KEY, VALID_SCOPES, ORG_ID_SCOPE)
    public_tenant = Tenant.objects.get(tenant_name="public")
    base_query = annotate_roles_with_counts(
        Role.objects.prefetch_related("access", "ext_relation", "access__permission")
    ).filter(tenant__in=[request.tenant, public_tenant])

    if scope == PRINCIPAL_SCOPE:
        queryset = get_object_principal_queryset(
            request,
            scope,
            Role,
            **{
                "prefetch_lookups_for_ids": "access",
                "prefetch_lookups_for_groups": "policies__roles",
                "is_org_admin": request.user.admin,
            },
        )
        return annotate_roles_with_counts(queryset)

    username = request.query_params.get("username")
    if username:
        role_permission = RoleAccessPermission()

        if username != request.user.username and not role_permission.has_permission(request=request, view=None):
            return Role.objects.none()
        else:
            if settings.BYPASS_BOP_VERIFICATION:
                is_org_admin = request.user.admin
            else:
                is_org_admin = _check_user_username_is_org_admin(request=request, username=username)

            request.user_from_query = User()
            request.user_from_query.username = username
            request.user_from_query.admin = is_org_admin

            queryset = get_object_principal_queryset(
                request,
                PRINCIPAL_SCOPE,
                Role,
                **{
                    "prefetch_lookups_for_ids": "access",
                    "prefetch_lookups_for_groups": "policies__roles",
                    "is_org_admin": is_org_admin,
                },
            )
            return annotate_roles_with_counts(queryset)

    if ENVIRONMENT.get_value("ALLOW_ANY", default=False, cast=bool):
        return base_query
    if request.user.admin:
        return base_query
    system_param = request.query_params.get("system")
    if system_param and system_param.lower() == "true":
        return base_query
    access = user_has_perm(request, "role")
    if access == "All":
        return base_query
    if access == "None":
        return Role.objects.none()
    return annotate_roles_with_counts(filter_queryset_by_tenant(Role.objects.filter(uuid__in=access), request.tenant))


def get_policy_queryset(request):
    """Obtain the queryset for policies."""
    scope = validate_and_get_key(request.query_params, SCOPE_KEY, VALID_SCOPES, ORG_ID_SCOPE)
    if scope != ORG_ID_SCOPE:
        return get_object_principal_queryset(request, scope, Policy)

    if ENVIRONMENT.get_value("ALLOW_ANY", default=False, cast=bool):
        return filter_queryset_by_tenant(Policy.objects.all(), request.tenant)
    if request.user.admin:
        return filter_queryset_by_tenant(Policy.objects.all(), request.tenant)
    access = user_has_perm(request, "policy")

    if access == "All":
        return filter_queryset_by_tenant(Policy.objects.all(), request.tenant)
    if access == "None":
        return Policy.objects.none()
    return filter_queryset_by_tenant(Policy.objects.filter(uuid__in=access), request.tenant)


def get_access_queryset(request: Request) -> QuerySet:
    """Obtain the queryset for access."""
    if APPLICATION_KEY not in request.query_params:
        key = "detail"
        message = f"Query parameter '{APPLICATION_KEY}' is required."
        raise serializers.ValidationError({key: _(message)})

    app = request.query_params.get(APPLICATION_KEY)
    # If we are querying on a username we need to check if the username is an org_admin
    # not the user making the request
    username = request.query_params.get("username")
    if not username or settings.BYPASS_BOP_VERIFICATION:
        is_org_admin = request.user.admin
    else:
        is_org_admin = _check_user_username_is_org_admin(request=request, username=username)

    return get_object_principal_queryset(
        request,
        PRINCIPAL_SCOPE,
        Access,
        **{
            APPLICATION_KEY: app,
            "prefetch_lookups_for_ids": "resourceDefinitions",
            "prefetch_lookups_for_groups": "policies__roles__access",
            "is_org_admin": is_org_admin,
        },
    )


def get_object_principal_queryset(request, scope, clazz, **kwargs):
    """Get the query set for the specific object for principal scope."""
    if scope not in VALID_SCOPES:
        key = "detail"
        message = "{} query parameter value {} is invalid. [{}] are valid inputs.".format(
            SCOPE_KEY, scope, ", ".join(VALID_SCOPES)
        )
        raise serializers.ValidationError({key: _(message)})

    if request.method not in permissions.SAFE_METHODS:
        return clazz.objects.none()

    object_principal_func = PRINCIPAL_QUERYSET_MAP.get(clazz.__name__)
    principal = get_principal_from_request(request)
    objects = object_principal_func(principal, request.tenant, **kwargs)
    return queryset_by_id(objects, clazz, **kwargs)


def _filter_admin_default(request: Request, queryset: QuerySet):
    """Filter out admin default groups unless the principal is an org admin."""
    username = request.query_params.get("username")
    if not username or settings.BYPASS_BOP_VERIFICATION:
        is_org_admin = request.user.admin
    else:
        is_org_admin = _check_user_username_is_org_admin(request=request, username=username)

    # If the principal is an org admin, make sure they get any and all admin_default groups
    if is_org_admin:
        admin_default_group_set = (
            Group.admin_default_set().filter(tenant=request.tenant) or Group.admin_default_set().public_tenant_only()
        )

        return queryset | admin_default_group_set

    return queryset


def _filter_default_groups(request: Request, queryset: QuerySet) -> QuerySet:
    """Filter out default access group and admin default group."""
    username = request.query_params.get("username")
    exclude_username = request.query_params.get("exclude_username")

    if (username and ITService.is_username_service_account(username=username)) or exclude_username:
        return queryset.exclude(platform_default=True).exclude(admin_default=True)
    else:
        return queryset


def _check_user_username_is_org_admin(request: Request, username: str) -> bool:
    """Check whether the given username is from a user that is an org admin or not.

    Service Accounts are considered to not be organization admins, and regular user principals need to be checked using
    the proxy.
    """
    if ITService.is_username_service_account(username=username):
        return False
    else:
        return get_admin_from_proxy(request=request, username=username)
