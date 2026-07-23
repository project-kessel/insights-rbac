"""This module contains the in-memory representation of a tuple store."""

from collections import defaultdict
from typing import Callable, Hashable, Iterable, List, Optional, Set, Tuple, TypeVar, Union

from kessel.relations.v1beta1.common_pb2 import Relationship
from management.relation_replicator.relation_replicator import RelationReplicator
from management.relation_replicator.types import RelationTuple  # noqa: F401 - Re-exported for backward compatibility

RelationPredicate = Callable[["RelationTuple"], bool]
T = TypeVar("T", bound=Hashable)


def _to_relation_tuple(item: Union[RelationTuple, Relationship]) -> RelationTuple:
    """Convert a proto Relationship or RelationTuple to a RelationTuple."""
    if isinstance(item, RelationTuple):
        return item
    return RelationTuple.from_message(item)


class TupleSet:
    """A set of relation tuples with various utility methods."""

    def __init__(self, full_set: "InMemoryTuples", filtered_set: Set[RelationTuple]):
        """Initialize the store."""
        self._full_set = full_set
        self._set = filtered_set

    def __len__(self):
        """Return the number of tuples in the list."""
        return len(self._set)

    def __iter__(self):
        """Return an iterator over the tuples."""
        return iter(self._set)

    def __contains__(self, item: RelationTuple):
        """Check if a tuple is in the list."""
        return item in self._set

    def __str__(self):
        """Return a string representation of the store."""
        return str(self._set)

    def __repr__(self):
        """Return a representation of the store."""
        return f"TupleSet({repr(self._set)})"

    @property
    def only(self) -> RelationTuple:
        """Return the only tuple in the set or raises an error if there is not exactly one tuple."""
        if len(self._set) != 1:
            raise ValueError(f"Expected only 1 tuple but found {len(self._set)}")
        return next(iter(self._set))

    def count_tuples(self, predicate: RelationPredicate = lambda _: True) -> int:
        """Count tuples matching the given predicate."""
        return len(self.find_tuples(predicate))

    def find_tuples(self, predicate: RelationPredicate = lambda _: True) -> "TupleSet":
        """Find tuples matching the given predicate."""
        return TupleSet(self._full_set, {rel for rel in self._set if predicate(rel)})

    def find_tuples_grouped(
        self, predicate: RelationPredicate, group_by: Callable[[RelationTuple], T]
    ) -> dict[T, "TupleSet"]:
        """Filter tuples and group them by a key."""
        grouped_tuples: dict[T, set[RelationTuple]] = defaultdict(set)
        for rel in self._set:
            if predicate(rel):
                key = group_by(rel)
                grouped_tuples[key].add(rel)
        return {key: TupleSet(self._full_set, value) for key, value in grouped_tuples.items()}

    def find_group_with_tuples(
        self,
        predicates: List[RelationPredicate],
        group_by: Callable[[RelationTuple], T],
        group_filter: Callable[[T], bool] = lambda _: True,
        require_full_match: bool = False,
        match_once: bool = True,
    ) -> Tuple[dict[T, "TupleSet"], dict[T, "TupleSet"]]:
        """
        Find groups of tuples matching given predicates, grouped by a key.

        Groups the tuples using the provided `group_by` function and tests the
        predicates against each group independently.

        For each group, this method attempts to find tuples that match all the
        predicates, ensuring that no tuple is used for more than one predicate
        within the group.

        If `require_full_match` is True, the method also ensures that all tuples
        in the group are matched by the predicates (i.e., no unmatched tuples
        remain in the group). If any group does not meet the criteria, it is
        excluded from the results.

        Args:
            predicates: A list of predicates (functions) that each accept a
                RelationTuple and return a bool indicating a match.
            group_by: A function that takes a RelationTuple and returns a key
                to group by (e.g., a resource ID).
            require_full_match: If True, only groups where all tuples are matched
                by the predicates (i.e. there are no remaining unmatched tuples)
                are included in the results.
            group_filter: A predicate that filters the groups to include in the
                results. Useful when you only want to test a subset of tuples e.g.
                a specific resource type.
            match_once: If True, each predicate is only used once in the matching process.
                Otherwise, each tuple in the group will be tested by each predicate until
                one predicate matches the tuple.

        Returns:
            A tuple containing two dictionaries:
            - The first dictionary contains the groups that matched all predicates.
            - The second dictionary contains the groups that did not match all predicates.
        """
        # Group the tuples by the specified key
        grouped_tuples: dict[T, set[RelationTuple]] = defaultdict(set)
        for rel in self._set:
            key = group_by(rel)
            if group_filter(key):
                grouped_tuples[key].add(rel)

        matching_groups: dict[T, TupleSet] = {}
        unmatched_groups: dict[T, TupleSet] = {}

        # Iterate over each group
        for key, group_tuples in grouped_tuples.items():
            remaining_tuples = list(group_tuples)
            remaining_predicates = list(predicates) if match_once else predicates
            i = 0
            matching_tuples = set()
            success = True

            # Attempt to match all predicates within the group
            # Using each predicate only once if requested
            while remaining_predicates and i < len(remaining_predicates):
                predicate = remaining_predicates[i]
                found = False
                j = 0
                while j < len(remaining_tuples):
                    rel = remaining_tuples[j]
                    if predicate(rel):
                        matching_tuples.add(rel)
                        remaining_tuples.pop(j)
                        found = True
                        if match_once:
                            break  # Move to next predicate
                    else:
                        j += 1
                if not found:
                    success = False
                    break  # Predicate not satisfied in this group
                if match_once:
                    remaining_predicates.pop(i)
                else:
                    i += 1

            if require_full_match and remaining_tuples or not success:
                unmatched_groups[key] = TupleSet(self._full_set, group_tuples)
                continue

            matching_groups[key] = TupleSet(self._full_set, matching_tuples)

        return matching_groups, unmatched_groups

    def traverse_subject(
        self,
        predicates: List[RelationPredicate],
        require_full_match: bool = True,
        match_once: bool = True,
    ) -> "TupleSet":
        """
        Traverse through the tuples to find and match tuples where the subject is the resource of the current tuple.

        The returned set contains all tuples that match the predicates
        and have a resource that is the subject of a tuple in this set.

        Args:
            predicates (List[RelationPredicate]):
                A list of predicate functions that take a RelationTuple
                and return a boolean indicating if the tuple matches the condition.
            require_full_match (bool, optional):
                If True, each predicate must be matched at least once. Defaults to True.
            match_once (bool, optional):
                If True, each tuple in the traversed list should be matched by at least one predicate. Defaults to True.
        Returns:
            TupleSet: A set of matched RelationTuples.
        """
        # For each tuple, find all the tuples where the subject is the resource
        # of the current tuple
        matched: set[RelationTuple] = set()

        for tuple in self:
            # Intentionally use full set here to traverse to tuples which may not be in this set
            traversed = self._full_set.find_tuples(
                resource(
                    tuple.subject.subject.type.namespace,
                    tuple.subject.subject.type.name,
                    tuple.subject.subject.id,
                )
            )
            # Now match these if those found match the predicates.
            matching, _ = traversed.find_group_with_tuples(
                predicates,
                group_by=lambda t: (t.resource.type.namespace, t.resource.type.name, t.resource.id),
                require_full_match=require_full_match,
                match_once=match_once,
            )

            for in_memory_tuple in matching.values():
                matched.update(in_memory_tuple)

        return TupleSet(self._full_set, matched)

    def resource_is_subject_of(self, tuple_matching: RelationPredicate) -> RelationPredicate:
        """
        Create a predicate to test if a tuple's resource is a subject of stored tuples matching the given predicate.

        Args:
            tuple_matching (RelationPredicate): A function that matches a relation tuple.

        Returns:
            RelationPredicate: A predicate function that takes a RelationTuple and returns a boolean
                               indicating if the resource is a subject of the given relation tuple.
        """

        def predicate(rel: RelationTuple) -> bool:
            count = self.count_tuples(
                all_of(
                    tuple_matching,
                    subject(rel.resource.type.namespace, rel.resource.type.name, rel.resource.id),
                )
            )
            return count > 0

        return TuplePredicate(predicate, f"resource_is_subject_of({predicate})")

    def subject_is_resource_of(
        self,
        tuple_matching: Union[List[RelationPredicate], RelationPredicate],
        only: bool = False,
    ) -> RelationPredicate:
        """
        Create a predicate to test if a tuple's subject is a resource of stored tuples matching the given predicate.

        If [only] is True, the predicate will return True
        only if the matched tuples are the only subjects related to the tested subject.

        Args:
            tuple_matching (RelationPredicate): A function that matches a relation tuple.
            only (bool, optional): If True, the predicate will return True only if the subject matches the tuple and
                                   there are no unmatched tuples. Defaults to False.

        Returns:
            RelationPredicate: A predicate function that takes a RelationTuple and returns a boolean
                               indicating if the subject is a resource of the given relation tuple.
        """
        predicates = [tuple_matching] if not isinstance(tuple_matching, list) else tuple_matching

        def predicate(rel: RelationTuple) -> bool:
            for predicate in predicates:
                matched = self.count_tuples(
                    all_of(
                        predicate,
                        resource(
                            rel.subject.subject.type.namespace,
                            rel.subject.subject.type.name,
                            rel.subject.subject.id,
                        ),
                    )
                )
                if matched == 0:
                    return False

            if only:
                matched = self.count_tuples(
                    all_of(
                        none_of(*predicates),
                        resource(
                            rel.subject.subject.type.namespace,
                            rel.subject.subject.type.name,
                            rel.subject.subject.id,
                        ),
                    )
                )
                if matched > 0:
                    return False

            return True

        return TuplePredicate(predicate, f"subject_is_resource_of({predicate}, only={only})")


class InMemoryTuples(TupleSet):
    """In-memory store for relation tuples."""

    def __init__(self, tuples=None):
        """Initialize the store."""
        self._tuples: Set[RelationTuple] = set(tuples) if tuples is not None else set()
        super().__init__(self, self._tuples)

    def add(self, item: Union[RelationTuple, Relationship]):
        """Add a tuple to the store."""
        self._tuples.add(_to_relation_tuple(item))

    def remove(self, item: Union[RelationTuple, Relationship]):
        """Remove a tuple from the store."""
        self._tuples.discard(_to_relation_tuple(item))

    def write(
        self,
        add: Iterable[Union[RelationTuple, Relationship]],
        remove: Iterable[Union[RelationTuple, Relationship]],
    ):
        """
        Add / remove tuples, checking for duplicates within this batch.

        Raises ValueError if duplicate relationships are found within the add list,
        as this indicates a bug in tuple generation logic.
        """
        # Check for duplicates within tuples_to_add (indicates bug in tuple generation)
        seen_in_batch = set()
        for item in add:
            key = _to_relation_tuple(item)
            tuple_str = (
                f"{key.resource.type.name}:{key.resource.id}#{key.relation}"
                f"@{key.subject.subject.type.name}:{key.subject.subject.id}"
            )

            if key in seen_in_batch:
                raise ValueError(
                    f"Duplicate relationship detected in single replication event: {tuple_str}. "
                    "This indicates a bug in tuple generation logic that should be fixed."
                )
            seen_in_batch.add(key)

        # Now add all tuples (duplicates with existing tuples are OK - Kessel handles this)
        for item in remove:
            self.remove(item)
        for item in add:
            self.add(item)

    def clear(self):
        """Clear all tuples from the store."""
        self._tuples.clear()

    def __str__(self):
        """Return a string representation of the store."""
        return str(self._tuples)

    def __repr__(self):
        """Return a representation of the store."""
        return f"InMemoryTuples({repr(self._tuples)})"

    def __len__(self):
        """Return the number of tuples in the store."""
        return len(self._tuples)


class TuplePredicate:
    """A predicate that can be used to filter relation tuples."""

    def __init__(self, func, repr):
        """Initialize the predicate."""
        self.func = func
        self.repr = repr

    def __call__(self, *args, **kwargs):
        """Call the predicate."""
        return self.func(*args, **kwargs)

    def __repr__(self):
        """Return a representation of the predicate."""
        return self.repr


def all_of(*predicates: RelationPredicate) -> RelationPredicate:
    """Return a predicate that is true if all of the given predicates are true."""
    if len(predicates) == 1:
        return predicates[0]

    def predicate(rel: RelationTuple) -> bool:
        return all(p(rel) for p in predicates)

    return TuplePredicate(predicate, f"all_of({', '.join([str(p) for p in predicates])})")


def one_of(*predicates: RelationPredicate) -> RelationPredicate:
    """Return a predicate that is true if any of the given predicates are true."""
    if len(predicates) == 1:
        return predicates[0]

    def predicate(rel: RelationTuple) -> bool:
        return any(p(rel) for p in predicates)

    return TuplePredicate(predicate, f"one_of({', '.join([str(p) for p in predicates])})")


def none_of(*predicates: RelationPredicate) -> RelationPredicate:
    """Return a predicate that is true if none of the given predicates are true."""

    def predicate(rel: RelationTuple) -> bool:
        return not any(p(rel) for p in predicates)

    return TuplePredicate(predicate, f"none_of({', '.join([str(p) for p in predicates])})")


def resource_type(namespace: str, name: str) -> RelationPredicate:
    """Return a predicate that is true if the resource type matches the given namespace and name."""

    def predicate(rel: RelationTuple) -> bool:
        return rel.resource.type.namespace == namespace and rel.resource.type.name == name

    return TuplePredicate(predicate, f'resource_type("{namespace}", "{name}")')


def resource_id(id: str) -> RelationPredicate:
    """Return a predicate that is true if the resource ID matches the given ID."""

    def predicate(rel: RelationTuple) -> bool:
        return rel.resource.id == id

    return TuplePredicate(predicate, f'resource_id("{id}")')


def resource(namespace: str, name: str, id: object) -> RelationPredicate:
    """Return a predicate that is true if the resource matches the given namespace and name."""
    return all_of(resource_type(namespace, name), resource_id(str(id)))


def relation(relation: str) -> RelationPredicate:
    """Return a predicate that is true if the resource relation matches the given relation."""

    def predicate(rel: RelationTuple) -> bool:
        return rel.relation == relation

    return TuplePredicate(predicate, f'relation("{relation}")')


def subject_type(
    namespace: str, name: str, relation: Optional[str] = None, any_relation: bool = False
) -> RelationPredicate:
    """Return a predicate that is true if the subject type matches the given namespace and name."""
    if any_relation and (relation is not None):
        raise ValueError("Cannot both have any_relation set and provide a specific relation")

    def predicate(rel: RelationTuple) -> bool:
        return (
            rel.subject.subject.type.namespace == namespace
            and rel.subject.subject.type.name == name
            and (any_relation or rel.subject.relation == relation)
        )

    return TuplePredicate(predicate, f'subject_type("{namespace}", "{name}")')


def subject_id(id: str) -> RelationPredicate:
    """Return a predicate that is true if the subject ID matches the given ID."""

    def predicate(rel: RelationTuple) -> bool:
        return rel.subject.subject.id == id

    return TuplePredicate(predicate, f'subject_id("{id}")')


def subject(namespace: str, name: str, id: object, relation: Optional[str] = None) -> RelationPredicate:
    """Return a predicate that is true if the subject matches the given namespace and name."""
    return all_of(subject_type(namespace, name, relation), subject_id(str(id)))


class InMemoryRelationReplicator(RelationReplicator):
    """Replicates relations to an in-memory store."""

    def __init__(self, store: InMemoryTuples = InMemoryTuples()):
        """Initialize the replicator."""
        self.store = store

    def replicate(self, event):
        """Replicate the event to the in-memory store."""
        # TODO: should also track the partition that each event was written to
        # in order to test partitioning logic
        self.store.write(event.add, event.remove)
