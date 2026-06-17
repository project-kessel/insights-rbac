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

"""A2S (Agent-to-Service) path helpers shared across middleware."""

from django.conf import settings


def is_a2s_path(request) -> bool:
    """Return True if the request targets an agent-to-service (A2S) endpoint.

    A2S paths live under /_private/_a2s/ but use public IdentityHeaderMiddleware
    auth instead of the internal PSK/token auth used by other /_private/ paths.
    When no valid identity is found, A2S requests are passed through without a
    user so that unauthenticated MCP tools (e.g. hello) still work.

    Returns False when MCP_ENABLED is False so that /_private/_a2s/ paths
    fall through to the standard internal auth and return 404.
    """
    return settings.MCP_ENABLED and request.path.startswith(settings.A2S_PATH_PREFIX)
