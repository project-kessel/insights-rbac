#
# Copyright 2024 Red Hat, Inc.
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
"""System user helper utilities for permission checks.

This module provides shared helper functions for identifying and checking
system users (used for service-to-service communication). These helpers
are used by both the WorkspaceAccessPermission class and the is_user_allowed_v2
function to ensure consistent behavior.

The main entry point is `check_system_user_access()` which encapsulates the
complete decision tree for system user access, preventing behavior drift
between different call sites.
"""

from enum import Enum
from typing import NamedTuple


class SystemUserAccessResult(Enum):
    """Result of system user access check."""

    NOT_SYSTEM_USER = "not_system_user"  # Not a system user, continue with normal checks
    ALLOWED = "allowed"  # System admin user, access granted
    DENIED = "denied"  # System user without admin, access denied
    CHECK_MOVE_TARGET = "check_move_target"  # System admin on move action, need to check target exists


class SystemUserCheckResult(NamedTuple):
    """Result tuple from check_system_user_access."""

    result: SystemUserAccessResult
    is_system: bool


def is_system_user(user) -> bool:
    """
    Check if the user is a system user (s2s communication).

    Args:
        user: The user object from the request

    Returns:
        bool: True if this is a system user, False otherwise
    """
    return getattr(user, "system", False)


def is_system_user_admin(user) -> bool:
    """
    Check if a system user has admin privileges.

    For system users, access is determined by the admin attribute.
    Non-system users always return False.

    Args:
        user: The user object from the request

    Returns:
        bool: True if this is a system user with admin=True, False otherwise
    """
    if not is_system_user(user):
        return False
    return getattr(user, "admin", False)


def check_system_user_access(user, action: str | None = None) -> SystemUserCheckResult:
    """
    Check system user access with the complete decision tree.

    This is the single source of truth for system user access decisions,
    encapsulating all the logic to prevent behavior drift between call sites.

    Decision tree:
    1. If not a system user -> NOT_SYSTEM_USER (continue with normal checks)
    2. If system user without admin -> DENIED
    3. If system admin on 'move' action -> CHECK_MOVE_TARGET (caller must verify target exists)
    4. If system admin on other actions -> ALLOWED

    Args:
        user: The user object from the request
        action: The view action (e.g., 'move', 'list', 'retrieve', etc.), or None

    Returns:
        SystemUserCheckResult with:
            - result: The access decision
            - is_system: Whether this is a system user
    """
    if not is_system_user(user):
        return SystemUserCheckResult(SystemUserAccessResult.NOT_SYSTEM_USER, False)

    if not is_system_user_admin(user):
        return SystemUserCheckResult(SystemUserAccessResult.DENIED, True)

    # System admin user
    if action == "move":
        return SystemUserCheckResult(SystemUserAccessResult.CHECK_MOVE_TARGET, True)

    return SystemUserCheckResult(SystemUserAccessResult.ALLOWED, True)
