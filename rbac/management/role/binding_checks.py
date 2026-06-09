#
# Copyright 2026 Red Hat, Inc.
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

"""Utilities for checking whether creating a role binding to a given resource is permitted."""

import abc
import dataclasses
from typing import Callable, Iterable

from django.conf import settings
from django.db import transaction
from management.inventory_checker.inventory_api_check import ResourceTenantInventoryChecker
from management.role.model import Role
from management.role.resource_definitions import ParsedAttributeFilter, parse_attribute_filter
from management.workspace.model import Workspace
from migration_tool.models import V2boundresource

from api.models import Tenant


class BindingAuthorizationPolicy(abc.ABC):
    """A policy for authorizing the creation of role bindings."""

    @abc.abstractmethod
    def can_bind_to(self, resource: V2boundresource) -> bool:
        """Return whether creating a role binding for the provided resource is permitted."""
        ...


class UnconstrainedBindingAuthorizationPolicy(BindingAuthorizationPolicy):
    """A BindingAuthorizationPolicy that permits bindings to all resources."""

    def can_bind_to(self, resource: V2boundresource) -> bool:
        """Return True (role bindings to all resources are permitted)."""
        return True


@dataclasses.dataclass(frozen=True)
class EnumeratedBindingAuthorizationPolicy(BindingAuthorizationPolicy):
    """A BindingAuthorizationPolicy that permits bindings to a set of provided resources."""

    resources: frozenset[V2boundresource]

    def __init__(self, resources: Iterable[V2boundresource]):
        """Create an EnumeratedBindingAuthorizationPolicy with the provided resources."""
        super().__init__()
        object.__setattr__(self, "resources", frozenset(resources))

        if not all(isinstance(r, V2boundresource) for r in self.resources):
            raise TypeError(f"Expected all resources to be V2boundresource, but got: {resources!r}")

    def can_bind_to(self, resource: V2boundresource) -> bool:
        """Return whether creating a role binding for the provided resource is permitted."""
        return resource in self.resources


class UnauthorizedResourceError(Exception):
    """An error for attempting to create a role binding to one or more prohibited resources."""

    resources: frozenset[V2boundresource]

    def __init__(self, resources: Iterable[V2boundresource]):
        """Create a UnauthorizedResourceError for an attempt to bind to the provided resources."""
        self.resources = frozenset(resources)
        super().__init__(f"Attempted to bind to unauthorized resource: {', '.join(str(r) for r in self.resources)}")

        if len(self.resources) == 0:
            raise RuntimeError("Expected resources to be non-empty")


@dataclasses.dataclass(frozen=True)
class BindingCheckResult:
    """
    The authorized resources resulting from verifying role binding permissions.

    named_resources includes resources that had explicit IDs in the provided attribute filters (i.e. are not the
    ungrouped hosts workspace being represented with a null).

    ungrouped_hosts_workspace is set if a binding to the ungrouped hosts workspace is requested with a null in the
    attribute filter.
    """

    named_resources: frozenset[V2boundresource]

    # We store this bool rather than creating it early because we need check_binding_resources to run outside of a
    # transaction, and we don't want to create an ungrouped hosts workspace if the transaction is going to fail later
    # for some unrelated reason.
    #
    # This ends up requiring an extra step within the ultimate transaction, but that isn't particularly problematic.
    # (The problematic part is ensuring that check_binding_resources *doesn't* run in a transaction, but creating the
    # workspace early wouldn't remove that need.)
    ungrouped_hosts_workspace: bool

    def __post_init__(self):
        """Verify the types of the provided values."""
        if not (
            isinstance(self.named_resources, frozenset)
            and all(isinstance(r, V2boundresource) for r in self.named_resources)
        ):
            raise TypeError(
                f"Expected named_resources to be a frozenset of V2boundresources, but got: {self.named_resources!r}"
            )

        if not isinstance(self.ungrouped_hosts_workspace, bool):
            raise TypeError(
                f"Expected ungrouped_hosts_workspace to be a bool, but got: {self.ungrouped_hosts_workspace!r}"
            )


def _default_resources_for(tenant: Tenant) -> set[V2boundresource]:
    workspaces = Workspace.objects.built_in(tenant=tenant)

    resource_id = tenant.tenant_resource_id()

    if resource_id is None:
        raise ValueError("Default resources are not defined for a tenant without an org_id.")

    return {
        V2boundresource(("rbac", "tenant"), resource_id),
        *(V2boundresource(("rbac", "workspace"), str(w.id)) for w in workspaces),
    }


def check_binding_resources(org_id: str, filters: list[ParsedAttributeFilter]) -> BindingCheckResult:
    """
    Check that it is permissible for the provided tenant to create role bindings for the provided attribute filters.

    Raises UnauthorizedResourceError if a binding is not permitted. Otherwise, returns a BindingCheckResult
    containing the checked resources (likely for use with commit_binding_policy).

    This makes network requests, so *it should not be run in a database transaction*.

    For testing purposes only, the setting SKIP_RESOURCE_BINDING_CHECKS can be set to not actually verify whether
    bindings are permissible (i.e. only do the parsing work).
    """
    resources: set[V2boundresource] = set()
    ungrouped_hosts_workspace = False

    for filter in filters:
        raw_type = filter.resource_type.as_tuple()

        for resource_id in filter.named_ids:
            resources.add(V2boundresource(raw_type, resource_id))

        if filter.has_null:
            if not filter.is_for_workspaces():
                raise ValueError(f"Invalid null resource ID for resource type: {raw_type}")

            ungrouped_hosts_workspace = True

    if not settings.SKIP_RESOURCE_BINDING_CHECKS:
        check_result = ResourceTenantInventoryChecker().check_resources(org_id=org_id, resources=resources)
        invalid_resources = [r for r, v in check_result.items() if not v]

        if invalid_resources:
            raise UnauthorizedResourceError(invalid_resources)

    return BindingCheckResult(
        named_resources=frozenset(resources),
        ungrouped_hosts_workspace=ungrouped_hosts_workspace,
    )


def commit_binding_policy(tenant: Tenant, check_result: BindingCheckResult) -> BindingAuthorizationPolicy:
    """
    Convert a BindingCheckResult into an actual BindingAuthorizationPolicy for the checked resources.

    This will create an ungrouped hosts workspace if requested (and thus should be run in a database transaction,
    unlike check_binding_resources).
    """
    # To avoid a circular dependency.
    from internal.utils import get_or_create_ungrouped_workspace

    resources = set(check_result.named_resources)
    resources.update(_default_resources_for(tenant))

    if check_result.ungrouped_hosts_workspace:
        ungrouped_ws = get_or_create_ungrouped_workspace(tenant)
        resources.add(V2boundresource(("rbac", "workspace"), str(ungrouped_ws.id)))

    return EnumeratedBindingAuthorizationPolicy(resources)


class RoleConflictError(Exception):
    """Indicates an error due to concurrent updates to a role."""

    pass


def with_checked_bindings[T](  # noqa: D103; I have no idea what it's on about.
    fn: Callable[[Role, BindingCheckResult], T],
    role: Role,
    retries: int = 3,
) -> T:
    """
    Run a function on the provided role after validating all role bindings from its attribute filters.

    This should not be run in an external database transaction. It will handle actually checking the permissions,
    then (within a transaction) locking the role and verifying that nothing has changed since the checks were made.

    Raises RoleConflictError if the provided number of retries is exceeded with the role being concurrently updated
    each time.
    """

    def _filter_data_for(checked_role: Role) -> list[tuple[int, ParsedAttributeFilter]]:
        entries: list[tuple[int, ParsedAttributeFilter]] = [
            entry
            for entry in (
                (rd.pk, parse_attribute_filter(rd.attributeFilter))
                for a in checked_role.access.all()
                for rd in a.resourceDefinitions.all()
            )
            if entry[1] is not None
        ]

        return sorted(entries, key=lambda e: e[0])

    for i in range(retries):
        role = (
            Role.objects.select_related("tenant")
            .prefetch_related("access", "access__resourceDefinitions")
            .get(pk=role.pk)
        )

        initial_filter_data = _filter_data_for(role)
        check_result = check_binding_resources(org_id=role.tenant.org_id, filters=[e[1] for e in initial_filter_data])

        with transaction.atomic():
            role = (
                Role.objects.select_related("tenant")
                .prefetch_related("access", "access__resourceDefinitions")
                .select_for_update(of=["self"])
                .get(pk=role.pk)
            )

            final_filter_data = _filter_data_for(role)

            if initial_filter_data != final_filter_data:
                continue

            return fn(role, check_result)

    raise RoleConflictError()
