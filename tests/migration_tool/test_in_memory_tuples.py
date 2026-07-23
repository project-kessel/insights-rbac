import unittest
import uuid
from typing import Optional

from google.protobuf import json_format
from kessel.relations.v1beta1.common_pb2 import Relationship, ObjectReference, ObjectType, SubjectReference
from migration_tool.in_memory_tuples import InMemoryTuples, RelationTuple
from migration_tool.utils import create_relationship


def _make_tuple(
    resource_type_namespace: str = "rbac",
    resource_type_name: str = "workspace",
    resource_id: str = "c7a3ff11-10d5-4326-9570-9fbbd3e06e17",
    relation: str = "binding",
    subject_type_namespace: str = "rbac",
    subject_type_name: str = "role_binding",
    subject_id: str = "d3431795-b368-448d-8835-0e77c0b7ded1",
    subject_relation: Optional[str] = None,
) -> RelationTuple:
    """Build a RelationTuple from flat keyword arguments for test convenience."""
    from management.relation_replicator.types import (
        ObjectReference as ObjRef,
        ObjectType as ObjType,
        SubjectReference as SubRef,
    )

    return RelationTuple(
        resource=ObjRef(type=ObjType(namespace=resource_type_namespace, name=resource_type_name), id=resource_id),
        relation=relation,
        subject=SubRef(
            subject=ObjRef(type=ObjType(namespace=subject_type_namespace, name=subject_type_name), id=subject_id),
            relation=subject_relation,
        ),
    )


def _make_args(
    resource_type_namespace: str = "rbac",
    resource_type_name: str = "workspace",
    resource_id: str = "c7a3ff11-10d5-4326-9570-9fbbd3e06e17",
    relation: str = "binding",
    subject_type_namespace: str = "rbac",
    subject_type_name: str = "role_binding",
    subject_id: str = "d3431795-b368-448d-8835-0e77c0b7ded1",
    subject_relation: Optional[str] = None,
) -> dict:
    return {
        "resource_type_namespace": resource_type_namespace,
        "resource_type_name": resource_type_name,
        "resource_id": resource_id,
        "relation": relation,
        "subject_type_namespace": subject_type_namespace,
        "subject_type_name": subject_type_name,
        "subject_id": subject_id,
        "subject_relation": subject_relation,
    }


class TestRelationTuple(unittest.TestCase):
    def test_valid(self):
        for args in [
            {},
            {"resource_type_name": "This_is_A_valid_type_name_123"},
            {"subject_type_name": "This_is_A_valid_type_name_123"},
            {"subject_relation": "members"},
            {"subject_relation": None},
            {"resource_id": "Valid/-|_+=1"},
            {"subject_id": "Valid/-|_+=1"},
            {"subject_id": "*"},
        ]:
            with self.subTest(args=args):
                _make_tuple(**_make_args(**args))

    def test_invalid(self):
        for args, error_type in [
            ({"resource_type_namespace": None}, TypeError),
            ({"resource_type_name": None}, TypeError),
            ({"resource_id": None}, TypeError),
            ({"relation": None}, TypeError),
            ({"subject_type_namespace": None}, TypeError),
            ({"subject_type_name": None}, TypeError),
            ({"subject_id": None}, TypeError),
            # subject_relation is optional.
            ({"resource_type_namespace": ""}, ValueError),
            ({"resource_type_name": ""}, ValueError),
            ({"resource_id": ""}, ValueError),
            ({"relation": ""}, ValueError),
            ({"subject_type_namespace": ""}, ValueError),
            ({"subject_type_name": ""}, ValueError),
            ({"subject_id": ""}, ValueError),
            ({"subject_relation": ""}, ValueError),
            # Ensure that UUID objects are rejected.
            ({"resource_id": uuid.uuid4()}, TypeError),
            ({"subject_id": uuid.uuid4()}, TypeError),
            ({"resource_type_name": "hyphens-prohibited"}, ValueError),
            ({"subject_type_name": "hyphens-prohibited"}, ValueError),
            ({"resource_id": "a$b"}, ValueError),
            ({"resource_id": "foo-*"}, ValueError),
            ({"resource_id": "*"}, ValueError),
            ({"subject_id": "a$b"}, ValueError),
            ({"subject_id": "foo-*"}, ValueError),
        ]:
            with self.subTest(args=args):
                self.assertRaises(error_type, _make_tuple, **_make_args(**args))

    def test_to_dict_matches_proto_json_with_subject_relation(self):
        """to_dict() must produce identical JSON to MessageToDict for tuples with subject_relation."""
        t = _make_tuple(subject_relation="member")
        self.assertEqual(t.to_dict(), json_format.MessageToDict(t.as_message()))

    def test_to_dict_matches_proto_json_without_subject_relation(self):
        """to_dict() must produce identical JSON to MessageToDict for tuples without subject_relation."""
        t = _make_tuple(subject_relation=None)
        self.assertEqual(t.to_dict(), json_format.MessageToDict(t.as_message()))

    def test_from_message_dict_roundtrip(self):
        """from_message_dict(to_dict()) should return the same RelationTuple."""
        t = _make_tuple(subject_relation="member")
        self.assertEqual(RelationTuple.from_message_dict(t.to_dict()), t)

    def test_from_message_dict_roundtrip_no_relation(self):
        """from_message_dict(to_dict()) should return the same RelationTuple without subject_relation."""
        t = _make_tuple(subject_relation=None)
        self.assertEqual(RelationTuple.from_message_dict(t.to_dict()), t)


class TestInMemoryTuples(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryTuples()

    def test_add_tuple(self):
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id"), relation="sub_rel"
            ),
        )
        self.store.add(relationship)
        self.assertEqual(len(self.store._tuples), 1)

    def test_remove_tuple(self):
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id"), relation="sub_rel"
            ),
        )
        self.store.add(relationship)
        self.store.remove(relationship)
        self.assertEqual(len(self.store._tuples), 0)

    def test_clear_tuples(self):
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id"), relation="sub_rel"
            ),
        )
        self.store.add(relationship)
        self.store.clear()
        self.assertEqual(len(self.store._tuples), 0)

    def test_count_tuples(self):
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id"), relation="sub_rel"
            ),
        )
        self.store.add(relationship)
        count = self.store.count_tuples()
        self.assertEqual(count, 1)

    def test_find_tuples(self):
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id"), relation="sub_rel"
            ),
        )
        self.store.add(relationship)
        tuples = self.store.find_tuples(lambda x: x.resource.id == "res_id")
        self.assertEqual(len(tuples), 1)

    def test_find_group_finds_group_with_tuple_that_matches_predicate(self):
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id1"), relation="sub_rel"
            ),
        )
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id2"), relation="sub_rel"
            ),
        )
        self.store.add(relationship)
        tuples, _ = self.store.find_group_with_tuples(
            group_by=lambda x: x.resource.id,
            group_filter=lambda x: x == "res_id",
            predicates=[lambda x: x.resource.id == "res_id"],
        )
        self.assertEqual(len(tuples), 1)

    def test_find_group_does_not_match_group_with_unmatched_predicate(self):
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id1"), relation="sub_rel"
            ),
        )
        relationship = Relationship(
            resource=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="res_id"),
            relation="rel",
            subject=SubjectReference(
                subject=ObjectReference(type=ObjectType(namespace="ns", name="name"), id="sub_id2"), relation="sub_rel"
            ),
        )
        self.store.add(relationship)
        tuples, _ = self.store.find_group_with_tuples(
            group_by=lambda x: x.resource.id,
            group_filter=lambda x: x == "res_id",
            predicates=[lambda x: x.subject.subject.id == "sub_id1", lambda x: x.subject.subject.id == "sub_id3"],
        )
        self.assertEqual(len(tuples), 0)

    def test_write_detects_duplicates_in_single_batch(self):
        """Test that write() raises ValueError when duplicates are found in the same batch."""
        # Create duplicate relationships
        rel1 = create_relationship(("rbac", "role_binding"), "binding-123", ("rbac", "role"), "role-456", "role")
        rel2 = create_relationship(("rbac", "role_binding"), "binding-123", ("rbac", "role"), "role-456", "role")

        # Should raise ValueError for duplicates in the same batch
        with self.assertRaises(ValueError) as context:
            self.store.write([rel1, rel2], [])

        # Verify error message
        error_message = str(context.exception)
        self.assertIn("Duplicate relationship detected", error_message)
        self.assertIn("role_binding:binding-123#role@role:role-456", error_message)
        self.assertIn("single replication event", error_message)

    def test_write_detects_only_duplicates_in_add_list(self):
        """Test that write() only checks for duplicates within the add list, not with existing tuples."""
        # Add a relationship to the store
        rel = create_relationship(("rbac", "role_binding"), "binding-abc", ("rbac", "workspace"), "ws-123", "binding")

        self.store.write([rel], [])

        try:
            # Duplicating a relationship that is already stored should be fine.
            self.store.write([rel], [])
        except ValueError as e:
            self.fail(f"Expected adding a duplicate of an existing relationship to work, but got: {e}")


class TestCreateRelationship(unittest.TestCase):
    """Test the create_relationship utility function."""

    def test_create_relationship_with_valid_ids(self):
        """Test that create_relationship works with valid non-None IDs."""
        rel = create_relationship(
            ("rbac", "workspace"), "workspace-123", ("rbac", "role_binding"), "binding-456", "binding"
        )

        self.assertIsNotNone(rel)
        self.assertEqual(rel.resource.id, "workspace-123")
        self.assertEqual(rel.subject.subject.id, "binding-456")

    def test_create_relationship_raises_error_on_none_resource_id(self):
        """Test that create_relationship raises ValueError when resource_id is None."""
        with self.assertRaises(TypeError):
            create_relationship(
                ("rbac", "workspace"),
                None,  # Invalid: None resource_id
                ("rbac", "role_binding"),
                "binding-456",
                "binding",
            )

    def test_create_relationship_raises_error_on_empty_resource_id(self):
        """Test that create_relationship raises ValueError when resource_id is empty string."""
        with self.assertRaises(ValueError):
            create_relationship(
                ("rbac", "workspace"),
                "",  # Invalid: empty resource_id
                ("rbac", "role_binding"),
                "binding-456",
                "binding",
            )

    def test_create_relationship_raises_error_on_none_subject_id(self):
        """Test that create_relationship raises ValueError when subject_id is None."""
        with self.assertRaises(TypeError):
            create_relationship(
                ("rbac", "workspace"),
                "workspace-123",
                ("rbac", "role_binding"),
                None,  # Invalid: None subject_id
                "binding",
            )

    def test_create_relationship_raises_error_on_empty_subject_id(self):
        """Test that create_relationship raises ValueError when subject_id is empty string."""
        with self.assertRaises(ValueError):
            create_relationship(
                ("rbac", "workspace"),
                "workspace-123",
                ("rbac", "role_binding"),
                "",  # Invalid: empty subject_id
                "binding",
            )
