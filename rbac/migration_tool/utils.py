"""Utilities for working with the relation API server."""

from typing import Optional, Tuple

from management.relation_replicator.types import ObjectReference, ObjectType, RelationTuple, SubjectReference


def create_relationship(
    resource_name: Tuple[str, str],
    resource_id: str,
    subject_name: Tuple[str, str],
    subject_id: str,
    relation: str,
    subject_relation: Optional[str] = None,
) -> RelationTuple:
    """Create a relationship between a resource and a subject.

    Validation is handled by the self-validating nested dataclasses.
    """
    return RelationTuple(
        resource=ObjectReference(
            type=ObjectType(namespace=resource_name[0], name=resource_name[1]),
            id=resource_id,
        ),
        relation=relation,
        subject=SubjectReference(
            subject=ObjectReference(
                type=ObjectType(namespace=subject_name[0], name=subject_name[1]),
                id=subject_id,
            ),
            relation=subject_relation,
        ),
    )
