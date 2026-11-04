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

"""Cost-owned principal profile HTTP client (COST-8167 spike).

When ``PRINCIPAL_PROFILE_SERVICE_URL`` is set, BYPASS principals enrichment
fetches profiles from that Cost service instead of calling Keycloak from
inside insights-rbac. This matches Access & Management's preference that
Keycloak interpretation stay Cost-owned behind an agreed interface.

Contract (spike):

    GET {PRINCIPAL_PROFILE_SERVICE_URL}/v1/orgs/{org_id}/profiles
    Optional Authorization: Bearer {PRINCIPAL_PROFILE_TOKEN}

    Response JSON:
      {"profiles": {"username": {"email", "first_name", "last_name",
                                 "is_org_admin", "enabled"}}}

Usernames in the response map are matched with ``str.lower()``, the same
normalization ``Principal.save()`` uses when storing usernames.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5


def profile_service_configured() -> bool:
    """Return True when a Cost profile service base URL is configured."""
    return bool(os.environ.get("PRINCIPAL_PROFILE_SERVICE_URL", "").strip())


def get_profiles_from_service(org_id: str | None) -> dict[str, dict[str, Any]]:
    """Fetch username(lower) -> profile from the Cost profile service.

    Returns ``{}`` when unset, org_id missing, or the request fails so the
    principals API can fall back to safe bypass defaults.
    """
    base = os.environ.get("PRINCIPAL_PROFILE_SERVICE_URL", "").strip().rstrip("/")
    if not base or not org_id:
        return {}

    url = f"{base}/v1/orgs/{org_id}/profiles"
    headers = {"Accept": "application/json"}
    token = os.environ.get("PRINCIPAL_PROFILE_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        body = response.json()
    except Exception:  # noqa: BLE001 — enrichment must never break principals
        LOGGER.exception("Cost principal profile service request failed (%s)", url)
        return {}

    raw = body.get("profiles") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        LOGGER.warning("Cost principal profile service returned unexpected body type")
        return {}

    profiles: dict[str, dict[str, Any]] = {}
    for username, item in raw.items():
        if not isinstance(item, dict):
            continue
        profiles[str(username).lower()] = {
            "first_name": item.get("first_name") or "",
            "last_name": item.get("last_name") or "",
            "email": item.get("email") or "",
            "is_org_admin": bool(item.get("is_org_admin", False)),
            "enabled": bool(item.get("enabled", True)),
        }
    return profiles
