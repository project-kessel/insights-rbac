"""Shared test helpers for disaster recovery tests."""

from management.relation_replicator.types import (
    ObjectReference,
    ObjectType,
    RelationTuple,
    SubjectReference,
)


def _make_tuple(resource_type="workspace", resource_id="ws-123", relation="parent"):
    """Build a RelationTuple for DR test fixtures."""
    return RelationTuple(
        resource=ObjectReference(
            type=ObjectType(namespace="rbac", name=resource_type),
            id=resource_id,
        ),
        relation=relation,
        subject=SubjectReference(
            subject=ObjectReference(
                type=ObjectType(namespace="rbac", name="workspace"),
                id="ws-parent",
            ),
        ),
    )
