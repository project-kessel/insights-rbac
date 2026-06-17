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
"""QuerySet for Subject lookups."""

from typing import TYPE_CHECKING

from django.db.models import Count, Q
from management.exceptions import InvalidFieldError, NotFoundError, RequiredFieldError
from management.group.model import Group
from management.principal.model import Principal

if TYPE_CHECKING:
    from management.subject.model import Subject


class SubjectQuerySet:
    """QuerySet-like class for Subject lookups.

    Follows Django's QuerySet pattern, used via as_manager().
    """

    def by_type(self, type: str, id: str) -> "Subject":
        """Get a subject by type (for dynamic dispatch).

        Args:
            type: The subject type ('group' or 'user')
            id: The subject UUID

        Returns:
            Subject wrapping the Group or Principal

        Raises:
            RequiredFieldError: If id is empty
            UnsupportedSubjectTypeError: If the type is not supported
            NotFoundError: If the subject cannot be found
        """
        from management.subject.exceptions import UnsupportedSubjectTypeError
        from management.subject.model import SubjectType

        if not id:
            raise RequiredFieldError("subject_id")

        if type == SubjectType.GROUP:
            return self.group(id=id)
        elif type == SubjectType.USER:
            return self.user(id=id)
        raise UnsupportedSubjectTypeError(type, supported=[SubjectType.GROUP, SubjectType.USER])

    def group(self, id: str) -> "Subject":
        """Get a group subject by UUID.

        Both Group.uuid and Principal.uuid are globally unique, so tenant
        filtering is unnecessary for lookups by UUID.

        Args:
            id: The group UUID

        Returns:
            Subject wrapping the Group

        Raises:
            NotFoundError: If the group cannot be found
        """
        from management.subject.model import Subject, SubjectType

        try:
            entity = Group.objects.annotate(
                principalCount=Count(
                    "principals",
                    filter=Q(principals__type=Principal.Types.USER),
                    distinct=True,
                )
            ).get(uuid=id)
            return Subject(type=SubjectType.GROUP, entity=entity)
        except Group.DoesNotExist:
            raise NotFoundError(SubjectType.GROUP, id)

    def user(self, id: str) -> "Subject":
        """Get a user subject by UUID.

        Args:
            id: The user/principal UUID

        Returns:
            Subject wrapping the Principal

        Raises:
            NotFoundError: If the principal cannot be found
        """
        from management.subject.model import Subject, SubjectType

        try:
            entity = Principal.objects.get(uuid=id)
        except Principal.DoesNotExist:
            raise NotFoundError(SubjectType.USER, id)

        if entity.user_id is None:
            raise InvalidFieldError(
                "subject_id",
                f"Principal '{id}' does not have a user_id yet. "
                "This is required for role binding operations. "
                "The user_id is populated when the user is synced from the identity provider.",
            )

        return Subject(type=SubjectType.USER, entity=entity)

    def groups(self, uuids: set[str]) -> dict[str, Group]:
        """Resolve multiple group UUIDs to Group objects.

        Args:
            uuids: Set of group UUID strings to resolve

        Returns:
            Dict mapping UUID string to Group instance
        """
        if not uuids:
            return {}
        return {str(g.uuid): g for g in Group.objects.filter(uuid__in=uuids)}

    def users(self, uuids: set[str]) -> dict[str, Principal]:
        """Resolve multiple user UUIDs to Principal objects.

        Validates that every resolved Principal has a ``user_id``,
        matching the single-lookup check in ``user()``.

        Args:
            uuids: Set of principal UUID strings to resolve

        Returns:
            Dict mapping UUID string to Principal instance

        Raises:
            InvalidFieldError: If any resolved Principal has no user_id
        """
        if not uuids:
            return {}
        result = {str(p.uuid): p for p in Principal.objects.filter(uuid__in=uuids)}
        unsynced = [uid for uid, p in result.items() if p.user_id is None]
        if unsynced:
            raise InvalidFieldError(
                "subject_id",
                f"The following principals do not have a user_id yet: {', '.join(unsynced)}. "
                "The user_id is populated when the user is synced from the identity provider.",
            )
        return result

    @classmethod
    def as_manager(cls) -> "SubjectQuerySet":
        """Return a manager instance, following Django's pattern."""
        return cls()
