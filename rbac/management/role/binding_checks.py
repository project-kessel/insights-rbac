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
from typing import Iterable

from migration_tool.models import V2boundresource


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


class EnumeratedBindingAuthorizationPolicy(BindingAuthorizationPolicy):
    """A BindingAuthorizationPolicy that permits bindings to a set of provided resources."""

    _resources: frozenset[V2boundresource]

    def __init__(self, resources: Iterable[V2boundresource]):
        """Create an EnumeratedBindingAuthorizationPolicy with the provided resources."""
        super().__init__()
        self._resources = frozenset(resources)

    def can_bind_to(self, resource: V2boundresource) -> bool:
        """Return whether creating a role binding for the provided resource is permitted."""
        return resource in self._resources


class UnauthorizedResourceError(Exception):
    """An error for attempting to create a role binding to one or more prohibited resources."""

    resources: frozenset[V2boundresource]

    def __init__(self, resources: Iterable[V2boundresource]):
        """Create a UnauthorizedResourceError for an attempt to bind to the provided resources."""
        self.resources = frozenset(resources)
        super().__init__(f"Attempted to bind to unauthorized resource: {', '.join(str(r) for r in self.resources)}")

        if len(self.resources) == 0:
            raise RuntimeError("Expected resources to be non-empty")
