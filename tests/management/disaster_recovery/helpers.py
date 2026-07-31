"""Shared test helpers for disaster recovery tests."""

from management.relation_replicator.types import (
    ObjectReference,
    ObjectType,
    RelationTuple,
    SubjectReference,
)

FAKE_WS_UUID = "00000000-0000-0000-0000-000000000123"


def _make_tuple(resource_type="workspace", resource_id=FAKE_WS_UUID, relation="parent"):
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
