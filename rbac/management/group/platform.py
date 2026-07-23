#
# Copyright 2025 Red Hat, Inc.
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
"""Contains helpers for handling platform groups (i.e. those built-in to RBAC)."""

from typing import ClassVar, Optional
from uuid import UUID

from management.models import Group


class DefaultGroupNotAvailableError(Exception):
    """Indicates that a request for a platform or admin default group could not be fulfilled."""

    pass


class GlobalPolicyIdService:
    """Caches the platform and admin default policy UUIDs (used as default role IDs in V2)."""

    _platform_default_uuid: Optional[UUID]
    _admin_default_uuid: Optional[UUID]

    def __init__(self):
        """Initialize an empty GlobalPolicyIdService."""
        self._platform_default_uuid = None
        self._admin_default_uuid = None

    _shared: ClassVar[Optional["GlobalPolicyIdService"]] = None

    @classmethod
    def shared(cls) -> "GlobalPolicyIdService":
        """Get a global cached instance of GlobalPolicyIdService."""
        instance = cls._shared

        if instance is None:
            instance = GlobalPolicyIdService()
            cls._shared = instance

        return instance

    @classmethod
    def clear_shared(cls):
        """
        Clear the cached instance used by shared().

        shared() will return a new instance after this returns.
        """
        cls._shared = None

    def platform_default_policy_uuid(self) -> UUID:
        """
        Return the policy UUID of the global platform default group.

        Raises DefaultGroupNotAvailableError if no such group exists. Note that the return value of this method may be
        cached, so the behavior is unspecified if the platform default group changes while the same
        GlobalPolicyIdService object exists.
        """
        try:
            if self._platform_default_uuid is None:
                policy = Group.objects.public_tenant_only().get(platform_default=True).policies.get()
                self._platform_default_uuid = policy.uuid
            return self._platform_default_uuid
        except Group.DoesNotExist as e:
            raise DefaultGroupNotAvailableError() from e

    def admin_default_policy_uuid(self) -> UUID:
        """
        Return the policy UUID of the global admin default group.

        Raises DefaultGroupNotAvailableError if no such group exists. Note that the return value of this method may be
        cached, so the behavior is unspecified if the admin default group changes while the same GlobalPolicyIdService
        object exists.
        """
        try:
            if self._admin_default_uuid is None:
                policy = Group.objects.public_tenant_only().get(admin_default=True).policies.get()
                self._admin_default_uuid = policy.uuid
            return self._admin_default_uuid
        except Group.DoesNotExist as e:
            raise DefaultGroupNotAvailableError() from e
