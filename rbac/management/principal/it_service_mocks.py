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

"""Mocked IT service account payloads.

Used only when ``IT_BYPASS_IT_CALLS`` is enabled, which is how ephemeral and local environments stand in for the
IT service. Everything here builds payloads in *IT's* shape; the caller is responsible for running them through
the same transformation a real IT response goes through, so that the mocked path exercises the real mapping
rather than bypassing it.
"""

import time
from typing import Any, Generator, Iterable, Optional

from management.principal.model import Principal

# Value marking a mocked service account as a service access token. Matches the value the identity service
# sends, confirmed in CIAM-12576 as "service_access_token" or omitted. Real responses are passed through with
# whatever value IT sent, so this is never compared against a real payload.
MOCK_SERVICE_ACCOUNT_TYPE_SERVICE_ACCESS_TOKEN = "service_access_token"

# Default lifetime of a service access token, per CIAM-ADR-00090.
MOCK_SERVICE_ACCESS_TOKEN_LIFETIME_SECONDS = 90 * 24 * 60 * 60


def is_service_access_token(client_id: Optional[str]) -> bool:
    """Decide whether a mocked service account should be presented as a service access token.

    Derived from the client ID rather than randomised so that a given service account keeps the same type for
    every request, the way a real one would.
    """
    if not client_id:
        return False

    return client_id[-1] in "02468ace"


def _sat_expiry_offset(client_id: str) -> int:
    """Return a stable per-account offset (in seconds) so that mocked SATs get varied expiry times.

    Uses the built-in hash of the client ID modulo 30 days, giving each SAT a unique spread while staying
    deterministic across requests.
    """
    thirty_days = 30 * 24 * 60 * 60
    return abs(hash(client_id)) % thirty_days


def generate_it_service_accounts(
    service_account_principals: Iterable[Principal],
) -> Generator[dict[str, Any], None, None]:
    """Yield service account payloads in IT's shape for the given principals.

    Yields one payload at a time so the caller can transform incrementally without holding both the raw and
    transformed lists in memory.  The payloads use IT's field names, not ours, so that the caller can run them
    through the real transformation.
    """
    # Imported here rather than at module scope: it_service defers its import of this module to avoid a cycle,
    # and importing the key names eagerly in both directions would reintroduce one.
    from management.principal.it_service import (
        IT_KEY_SERVICE_ACCOUNT_EXPIRATION,
        IT_KEY_SERVICE_ACCOUNT_TYPE,
    )

    now = round(time.time())

    for principal in service_account_principals:
        it_service_account: dict[str, Any] = {
            "id": principal.service_account_id,
            "clientId": principal.service_account_id,
            "name": f"{principal.service_account_id}-name",
            "description": f"{principal.service_account_id}-description",
            "createdBy": principal.username,
            "createdAt": now,
        }

        # Mock a mix of regular service accounts and service access tokens so that both shapes get exercised in
        # the environments which bypass IT.
        if is_service_access_token(principal.service_account_id):
            it_service_account[IT_KEY_SERVICE_ACCOUNT_TYPE] = MOCK_SERVICE_ACCOUNT_TYPE_SERVICE_ACCESS_TOKEN
            # Each SAT gets a stable, varied expiry so they do not all tie when sorted by time_expires.
            it_service_account[IT_KEY_SERVICE_ACCOUNT_EXPIRATION] = (
                now + MOCK_SERVICE_ACCESS_TOKEN_LIFETIME_SECONDS + _sat_expiry_offset(principal.service_account_id)
            )

        yield it_service_account


def build_it_service_accounts(service_account_principals: Iterable[Principal]) -> list[dict[str, Any]]:
    """Build service account payloads in IT's shape for the given principals.

    Convenience wrapper around :func:`generate_it_service_accounts` that materialises the full list.  Prefer the
    generator in production paths to avoid doubling peak memory.
    """
    return list(generate_it_service_accounts(service_account_principals))
