# Baseline tests for OCM performance

from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db.models import Q

from rest_framework import status
from rest_framework.test import APIClient

from api.models import Tenant
from management.models import Group, Principal

import logging

from tests.performance.test_performance_util import build_identity, timerStart, timerStop, write_to_logger

THREADS = 10

client = APIClient()

logger = logging.getLogger(__name__)

identity = build_identity()


def test_tenant_groups():
    """Test tenant groups with /integrations/tenant/{tenant_id}/groups/ endpoint."""
    # 1 request for each tenant (to get tenant's groups)

    tenants = (
        Tenant.objects.filter(Q(group__system=False) | Q(role__system=False))
        .prefetch_related("group_set", "role_set")
        .distinct()
    )

    name = "Tenant Groups"
    start = timerStart(name)

    num_requests = 0

    def tenant_groups(t):
        response = client.get(
            f"/_private/api/v1/integrations/tenant/{t.org_id}/groups/?external_tenant=ocm",
            **identity.META,
            follow=True,
        )

        return response

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        for t in tenants:
            futures.append(executor.submit(tenant_groups, t=t))
        for future in as_completed(futures):
            response = future.result()
            if response.status_code != status.HTTP_200_OK:
                Exception("Received an error status\n")
            num_requests += 1

    request_time, average = timerStop(start, num_requests)

    write_to_logger(logger, name, "/api/v1/integrations/tenant/org_id/groups/", num_requests, request_time, average)


def test_tenant_roles():
    """Test tenant roles with /integrations/tenant/{tenant_id}/roles/ endpoint."""
    tenants = (
        Tenant.objects.filter(Q(group__system=False) | Q(role__system=False))
        .prefetch_related("group_set", "role_set")
        .distinct()
    )
    # 1 request for each tenant (to get the tenant's roles)

    name = "Tenant Roles"
    start = timerStart(name)

    num_requests = 0

    def tenant_roles(t):
        response = client.get(
            f"/_private/api/v1/integrations/tenant/{t.org_id}/roles/?external_tenant=ocm",
            **identity.META,
            follow=True,
        )

        return response

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        for t in tenants:
            futures.append(executor.submit(tenant_roles, t=t))
        for future in as_completed(futures):
            response = future.result()
            if response.status_code != status.HTTP_200_OK:
                Exception("Received an error status\n")
            num_requests += 1

    request_time, average = timerStop(start, num_requests)

    write_to_logger(logger, name, "/api/v1/integrations/tenant/{org_id}/roles/", num_requests, request_time, average)


def test_group_roles():
    """Test group roles with /integrations/tenant/{tenant_id}/groups/{group_id}/roles/ endpoint."""
    # 1 request for each group (to get roles)
    tenants = (
        Tenant.objects.filter(Q(group__system=False) | Q(role__system=False))
        .prefetch_related("group_set", "role_set")
        .distinct()
    )

    num_requests = 0

    name = "Group Roles"
    start = timerStart(name)

    def group_roles(t, g):
        response = client.get(
            f"/_private/api/v1/integrations/tenant/{t.org_id}/groups/{g.uuid}/roles/?external_tenant=ocm",
            **identity.META,
            follow=True,
        )

        return response

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        for t in tenants:
            groups = Group.objects.filter(tenant=t)
            for g in groups:
                futures.append(executor.submit(group_roles, t=t, g=g))
        for future in as_completed(futures):
            response = future.result()
            if response.status_code != status.HTTP_200_OK:
                Exception("Received an error status\n")
            num_requests += 1

    request_time, average = timerStop(start, num_requests)

    write_to_logger(
        logger,
        name,
        "/api/v1/integrations/tenant/{org_id}/groups/{g_uuid}/roles/",
        num_requests,
        request_time,
        average,
    )


def test_principals_groups():
    """Test tenant principals groups with /integrations/tenant/{tenant_id}/principal/{principal_id}/groups/ endpoint."""

    tenants = (
        Tenant.objects.filter(Q(group__system=False) | Q(role__system=False))
        .prefetch_related("group_set", "role_set")
        .distinct()
    )

    name = "Principals Groups"
    start = timerStart(name)

    num_requests = 0

    def principals_groups(t, p):
        response = client.get(
            f"/_private/api/v1/integrations/tenant/{t.org_id}/principal/{p.username}/groups/?external_tenant=ocm",
            **identity.META,
            follow=True,
        )

        return response

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        for t in tenants:
            principals = Principal.objects.filter(tenant=t)
            for p in principals:
                futures.append(executor.submit(principals_groups, t=t, p=p))
        for future in as_completed(futures):
            response = future.result()
            if response.status_code != status.HTTP_200_OK:
                Exception("Received an error status\n")
            num_requests += 1

    request_time, average = timerStop(start, num_requests)

    write_to_logger(
        logger,
        name,
        "/api/v1/integrations/tenant/{org_id}/principal/{username}/groups/",
        num_requests,
        request_time,
        average,
    )


def test_principals_roles():
    """
    Test tenant principals roles with
    /integrations/tenant/{tenant_id}/principal/{principal_id}/groups/{group_id}/roles/ endpoint.
    """
    # 1 request for each tenant (to get the principles)
    tenants = (
        Tenant.objects.filter(Q(group__system=False) | Q(role__system=False))
        .prefetch_related("group_set", "role_set")
        .distinct()
    )

    name = "Principals Roles"
    start = timerStart(name)

    num_requests = 0

    def principals_roles(t, p, g):
        response = client.get(
            f"/_private/api/v1/integrations/tenant/{t.org_id}/principal/{p.username}/groups/{g.uuid}/roles/?external_tenant=ocm",
            **identity.META,
            follow=True,
        )

        return response

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        for t in tenants:
            principals = Principal.objects.filter(tenant=t)

            for p in principals:
                groups = Group.objects.filter(tenant=t)

                for g in groups:
                    futures.append(executor.submit(principals_roles, t=t, p=p, g=g))

        for future in as_completed(futures):
            response = future.result()

            if response.status_code != status.HTTP_200_OK:
                Exception("Received an error status\n")

            num_requests += 1

    request_time, average = timerStop(start, num_requests)

    write_to_logger(
        logger,
        name,
        "/api/v1/integrations/tenant/{org_id}/principal/{username}/groups/{g_uuid}/roles/",
        num_requests,
        request_time,
        average,
    )


def test_full_sync():
    """Test simulated full sync."""
    name = "Full Sync"
    start = timerStart(name)

    num_requests = 0

    def get_tenants():
        response = client.get(
            f"/_private/api/v1/integrations/tenant/?external_tenant=ocm&modified_only=true",
            **identity.META,
            follow=True,
        )

        if response.status_code != status.HTTP_200_OK:
            Exception("Received an error status\n")

        tenants = response.data.get("data")

        return tenants

    tenants = get_tenants()  # using the integrations/tenant/ endpoint

    num_requests += 1

    def full_sync(t, g):
        org_id = t.get("org_id")
        g_uuid = g.get("uuid")
        # tenant groups roles, get orgs groups roles
        response = client.get(
            f"/_private/api/v1/integrations/tenant/{org_id}/groups/{g_uuid}/roles/?external_tenant=ocm",
            **identity.META,
            follow=True,
        )

        if response.status_code != status.HTTP_200_OK:
            Exception("Received an error status\n")

        # OCM would sync group roles here

        response = client.get(
            f"/_private/api/v1/integrations/tenant/{org_id}/groups/{g_uuid}/principals/?external_tenant=ocm&username_only=true",
            **identity.META,
            follow=True,
        )

        if response.status_code != status.HTTP_200_OK:
            Exception("Received an error status\n")

        # sync account groups

        return response

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        for t in tenants:
            org_id = t.get("org_id")
            num_requests += 1
            # tenant groups, get orgs groups
            response = client.get(
                f"/_private/api/v1/integrations/tenant/{org_id}/groups/?external_tenant=ocm",
                **identity.META,
                follow=True,
            )
            if response.status_code != status.HTTP_200_OK:
                Exception("Received an error status\n")

            groups = response.data.get("data")

            for g in groups:
                futures.append(executor.submit(full_sync, t=t, g=g))

        for _ in as_completed(futures):
            num_requests += 2

    request_time, average = timerStop(start, num_requests)

    write_to_logger(logger, name, "", num_requests, request_time, average)
