"""
Copyright 2019 Red Hat, Inc.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import uuid
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from management.principal.model import Principal
from management.relation_replicator.types import ObjectReference, ObjectType, RelationTuple
from management.workspace.model import Workspace
from migration_tool.utils import create_relationship

from api.models import Tenant


@dataclass(frozen=True)
class V1resourcedef:
    """V1 resource definition."""

    resource_type: str
    op: str
    resource_id: str


@dataclass(frozen=True)
class V1permission:
    """V1 permission definition."""

    app: str
    resource: str
    perm: str
    resourceDefs: frozenset[V1resourcedef]

    def matches(self, v2perm: str):
        """Check if the V1 permission matches the V2 permission."""
        app, resource, perm = split_v2_perm(v2perm)
        if self.app != "*" and cleanNameForV2SchemaCompatibility(self.app) != app:
            return False
        if self.resource != "*" and cleanNameForV2SchemaCompatibility(self.resource) != resource:
            return False
        if self.perm != "*" and cleanNameForV2SchemaCompatibility(self.perm) != perm:
            return False

        return True


@dataclass(frozen=True)
class V2boundresource:
    """V2 bound resource definition."""

    resource_type: Tuple[str, str]
    resource_id: str

    def __post_init__(self):
        """Check that the object's state is valid."""
        if not isinstance(self.resource_type, tuple):
            raise TypeError(f"resource_type must be a tuple, but got: {self.resource_type!r}")

        if len(self.resource_type) != 2:
            raise ValueError(f"resource_type should have length two, but got: {self.resource_type!r}")

        # TODO: eventually migrate uses of V2boundreference to ObjectReference.
        # For now, we just ensure that any V2boundresource values are compatible with it.
        try:
            ObjectReference(
                ObjectType(namespace=self.resource_type[0], name=self.resource_type[1]), id=self.resource_id
            )
        except Exception as e:
            raise Exception("Error while checking compatibility with ObjectReference") from e

    @classmethod
    def for_workspace_id(cls, workspace_id: str | uuid.UUID) -> "V2boundresource":
        """Return a V2boundresource corresponding to the provided workspace ID."""
        if not (isinstance(workspace_id, str) or isinstance(workspace_id, uuid.UUID)):
            raise TypeError(f"Expected workspace_id to be a string or UUID, but got: {workspace_id!r}")

        return V2boundresource(("rbac", "workspace"), str(workspace_id))

    @classmethod
    def try_for_model(cls, model: Workspace | Tenant) -> Optional["V2boundresource"]:
        """
        Return a V2boundresource corresponding to the provided model.

        This will return None if no such resource can be computed (e.g. for a Tenant without an org_id).
        """
        if isinstance(model, Workspace):
            return V2boundresource.for_workspace_id(model.id)

        if isinstance(model, Tenant):
            resource_id = model.tenant_resource_id()

            if resource_id is None:
                return None

            return V2boundresource(("rbac", "tenant"), resource_id)

        raise TypeError(f"Unexpected model: {model}")

    @classmethod
    def for_model(cls, model: Workspace | Tenant) -> "V2boundresource":
        """
        Return a V2boundresource corresponding to the provided model.

        This will raise a ValueError if no such resource can be computed (e.g. for a Tenant without an org_id).
        """
        resource = cls.try_for_model(model)

        if resource is None:
            raise ValueError(f"Unable to determine resource for model: {model}")

        return resource


@dataclass(frozen=True)
class V2role:
    """V2 role definition."""

    @classmethod
    def for_system_role(cls, id: str) -> "V2role":
        """Create a V2 role for a system role."""
        return cls(id=id, is_system=True, permissions=frozenset())

    id: str
    is_system: bool
    permissions: frozenset[str]

    def as_dict(self) -> dict:
        """Convert the V2 role to a dictionary."""
        return {
            "id": self.id,
            "is_system": self.is_system,
            "permissions": list(self.permissions) if not self.is_system else [],
        }


@dataclass(frozen=True)
class V2rolebinding:
    """V2 role binding definition."""

    id: str
    role: V2role
    resource: V2boundresource
    groups: tuple[str, ...]
    users: dict[str, str]

    def __init__(
        self,
        id: str,
        role: V2role,
        resource: V2boundresource,
        groups: Iterable[str] = [],
        users: dict[str, str] = {},
    ):
        """
        Initialize a V2 role binding.

        [groups] and [users] allow multiple in the case there are multiple sources of bindings
        for the same role and resource, though in the graph these occur only once.
        """
        # Need to use setattr due to frozen dataclass
        # Fields are allowed to be any iterable for compatibility with existing code.
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "groups", tuple(groups))

        if not isinstance(users, dict):
            raise TypeError(f"users must be a dict, but got: {users!r}")

        object.__setattr__(self, "users", users)

    def as_minimal_dict(self) -> dict:
        """Convert the V2 role binding to a dictionary, excluding resource and original role."""
        return {
            "id": self.id,
            "role": self.role.as_dict(),
            "groups": [g for g in self.groups],
            "users": self.users,
        }

    def as_tuples(self):
        """Create tuples from V2rolebinding model."""
        tuples: list[RelationTuple] = list()

        tuples.append(create_relationship(("rbac", "role_binding"), self.id, ("rbac", "role"), self.role.id, "role"))

        for perm in self.role.permissions:
            tuples.append(role_permission_tuple(role_id=self.role.id, permission=perm))

        # We cannot duplicate relationships within a single batch, so we ensure that each subject relationship is
        # added only once.

        for group in set(self.groups):
            # These might be duplicate but it is OK, spiceDB will handle duplication through touch
            tuples.append(role_binding_group_subject_tuple(self.id, group))

        for user in set(self.users.values()):
            tuples.append(role_binding_user_subject_tuple(self.id, user))

        tuples.append(
            create_relationship(
                self.resource.resource_type,
                self.resource.resource_id,
                ("rbac", "role_binding"),
                self.id,
                "binding",
            )
        )

        return tuples


def role_binding_group_subject_tuple(role_binding_id: str, group_uuid: str) -> RelationTuple:
    """Create a relationship tuple for a role binding and a group."""
    return create_relationship(
        ("rbac", "role_binding"),
        role_binding_id,
        ("rbac", "group"),
        group_uuid,
        "subject",
        subject_relation="member",
    )


def role_binding_user_subject_tuple(role_binding_id: str, user_id: str) -> RelationTuple:
    """Create a relationship tuple for a role binding and a user."""
    id = Principal.user_id_to_principal_resource_id(user_id)
    return create_relationship(
        ("rbac", "role_binding"),
        role_binding_id,
        ("rbac", "principal"),
        id,
        "subject",
    )


def role_permission_tuple(role_id: str, permission: str) -> RelationTuple:
    """Create a relationship tuple for a role having a given permission."""
    return create_relationship(
        ("rbac", "role"),
        role_id,
        ("rbac", "principal"),
        "*",
        permission,
    )


def split_v2_perm(perm: str):
    """Split V2 permission into app, resource and permission."""
    first_delimiter = perm.find("_")
    last_delimiter = perm.rfind("_")

    # Handle inventory groups rewrite
    if perm == "read":
        return "inventory", "groups", "read"
    if perm == "write":
        return "inventory", "groups", "write"

    if first_delimiter == -1 or last_delimiter == -1 or first_delimiter == last_delimiter:
        raise ValueError("Invalid V2 permission: " + perm)

    return (
        perm[:first_delimiter],
        perm[(first_delimiter + 1) : last_delimiter],  # noqa: E203
        perm[(last_delimiter + 1) :],  # noqa: E203
    )


# Translated from: https://gitlab.corp.redhat.com/ciam-authz/loadtesting-spicedb/-/blob/main/spicedb/
# prbac-schema-generator/main.go?ref_type=heads#L286
def cleanNameForV2SchemaCompatibility(name: str):
    """Clean a name for compatibility with the v2 schema."""
    return name.lower().replace("-", "_").replace(".", "_").replace(":", "_").replace(" ", "_").replace("*", "all")
