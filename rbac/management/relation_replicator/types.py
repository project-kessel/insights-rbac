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
"""Shared domain types for the management module."""

import dataclasses
import re
from typing import Any, ClassVar, Optional

from kessel.relations.v1beta1 import common_pb2


def _validate_required_str(field: str, value: object):
    """Validate that a field is a non-empty string."""
    if value is None:
        raise TypeError(f"{field} is required, but was None.")
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, but got {type(value).__name__}: {value!r}.")
    if value == "":
        raise ValueError(f"{field} cannot be empty.")


def _validate_optional_str(field: str, value: object):
    """Validate that a field is None or a non-empty string."""
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None, but got {type(value).__name__}: {value!r}.")
    if value == "":
        raise ValueError(f"{field} cannot be empty (for an absent value, use None).")


def _validate_pattern(field: str, value: str, pattern: re.Pattern, description: str):
    """Validate that a string matches a regex pattern."""
    if not re.fullmatch(pattern, value):
        raise ValueError(f"Expected {field} to be composed of {description}, but got: {value!r}")


@dataclasses.dataclass(frozen=True)
class ObjectType:
    """Resource or subject type (namespace + name)."""

    namespace: str
    name: str

    _type_regex: ClassVar[re.Pattern] = re.compile(r"^[A-Za-z0-9_]+$")

    def __post_init__(self):
        """Validate namespace and name."""
        _validate_required_str("namespace", self.namespace)
        _validate_required_str("name", self.name)
        _validate_pattern("name", self.name, self._type_regex, "alphanumeric characters and underscores")


@dataclasses.dataclass(frozen=True)
class ObjectReference:
    """Reference to a resource or subject (type + id)."""

    type: ObjectType
    id: str

    _id_regex: ClassVar[re.Pattern] = re.compile(r"^(([a-zA-Z0-9/_|\-=+]{1,})|\*)$")

    def __post_init__(self):
        """Validate id."""
        _validate_required_str("id", self.id)
        _validate_pattern(
            "id",
            self.id,
            self._id_regex,
            "alphanumeric characters, underscores, hyphens, pipes, "
            "equals signs, plus signs, and forward slashes, or exactly '*'",
        )


@dataclasses.dataclass(frozen=True)
class SubjectReference:
    """Reference to a subject with optional relation."""

    subject: ObjectReference
    relation: Optional[str] = None

    def __post_init__(self):
        """Validate optional relation."""
        _validate_optional_str("relation", self.relation)


@dataclasses.dataclass(frozen=True)
class RelationTuple:
    """
    Domain representation of a relation tuple.

    This is an internal abstraction over the external SpiceDB/Kessel relation concept.
    Fields mirror the protobuf Relationship message structure so that
    tuple.resource.type.namespace matches relationship.resource.type.namespace on the proto.

    Use as_message() to convert to the protobuf Relationship type when needed.
    Use to_dict() to serialize to JSON matching the protobuf JSON format.
    """

    resource: ObjectReference
    relation: str
    subject: SubjectReference

    def __post_init__(self):
        """Validate the relation tuple."""
        _validate_required_str("relation", self.relation)
        if self.resource.id == "*":
            raise ValueError(
                "resource.id cannot be '*' (asterisk is only allowed for subjects)." f"\nFull relationship: {self!r}"
            )

    @classmethod
    def from_message_dict(cls, relationship: dict) -> "RelationTuple":
        """Create a RelationTuple from a Relationship message dict."""

        def as_optional(value: str) -> Optional[str]:
            return value if value != "" else None

        return RelationTuple(
            resource=ObjectReference(
                type=ObjectType(
                    namespace=relationship["resource"]["type"]["namespace"],
                    name=relationship["resource"]["type"]["name"],
                ),
                id=relationship["resource"]["id"],
            ),
            relation=relationship["relation"],
            subject=SubjectReference(
                subject=ObjectReference(
                    type=ObjectType(
                        namespace=relationship["subject"]["subject"]["type"]["namespace"],
                        name=relationship["subject"]["subject"]["type"]["name"],
                    ),
                    id=relationship["subject"]["subject"]["id"],
                ),
                relation=as_optional(relationship["subject"].get("relation", "")),
            ),
        )

    @classmethod
    def from_message(cls, relationship: common_pb2.Relationship) -> "RelationTuple":
        """Create a RelationTuple from a protobuf Relationship message."""

        def as_optional(value: str) -> Optional[str]:
            return value if value != "" else None

        return RelationTuple(
            resource=ObjectReference(
                type=ObjectType(
                    namespace=relationship.resource.type.namespace,
                    name=relationship.resource.type.name,
                ),
                id=relationship.resource.id,
            ),
            relation=relationship.relation,
            subject=SubjectReference(
                subject=ObjectReference(
                    type=ObjectType(
                        namespace=relationship.subject.subject.type.namespace,
                        name=relationship.subject.subject.type.name,
                    ),
                    id=relationship.subject.subject.id,
                ),
                relation=as_optional(relationship.subject.relation),
            ),
        )

    def as_message(self) -> common_pb2.Relationship:
        """Convert to a protobuf Relationship message for replication."""
        return common_pb2.Relationship(
            resource=common_pb2.ObjectReference(
                type=common_pb2.ObjectType(
                    namespace=self.resource.type.namespace,
                    name=self.resource.type.name,
                ),
                id=self.resource.id,
            ),
            relation=self.relation,
            subject=common_pb2.SubjectReference(
                subject=common_pb2.ObjectReference(
                    type=common_pb2.ObjectType(
                        namespace=self.subject.subject.type.namespace,
                        name=self.subject.subject.type.name,
                    ),
                    id=self.subject.subject.id,
                ),
                relation=self.subject.relation,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict matching the protobuf JSON format.

        The output is identical to json_format.MessageToDict(self.as_message()).
        None values are omitted to match protobuf JSON serialization behavior.
        """
        subject_dict: dict[str, Any] = {
            "subject": {
                "type": {
                    "namespace": self.subject.subject.type.namespace,
                    "name": self.subject.subject.type.name,
                },
                "id": self.subject.subject.id,
            },
        }
        if self.subject.relation is not None:
            subject_dict["relation"] = self.subject.relation

        return {
            "resource": {
                "type": {
                    "namespace": self.resource.type.namespace,
                    "name": self.resource.type.name,
                },
                "id": self.resource.id,
            },
            "relation": self.relation,
            "subject": subject_dict,
        }

    @classmethod
    def validate_message(cls, message: common_pb2.Relationship):
        """Check that the provided Relationship represents a valid tuple."""
        parsed = RelationTuple.from_message(message)
        assert parsed.as_message() == message

    def stringify(self) -> str:
        """Display all attributes in one line."""
        subject_part = (
            f"{self.subject.subject.type.namespace}/{self.subject.subject.type.name}:{self.subject.subject.id}"
        )
        if self.subject.relation:
            subject_part += f"#{self.subject.relation}"
        return (
            f"{self.resource.type.namespace}/{self.resource.type.name}:{self.resource.id}#{self.relation}"
            f"@{subject_part}"
        )
