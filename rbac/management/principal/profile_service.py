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

"""Cost-owned principal profile HTTP client (COST-8167).

When ``PRINCIPAL_PROFILE_SERVICE_URL`` is set, BYPASS principals enrichment
fetches profiles from that Cost service instead of calling Keycloak from
inside insights-rbac. This matches Access & Management's preference that
Keycloak interpretation stay Cost-owned behind an agreed interface.

Contract:

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
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

LOGGER = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "rbac:principal_profile_service"


def profile_service_configured() -> bool:
    """Return True when a Cost profile service base URL is configured."""
    return bool((getattr(settings, "PRINCIPAL_PROFILE_SERVICE_URL", "") or "").strip())


def _cache_key(base_url: str, org_id: str) -> str:
    """Return a cache key scoped to the profile service URL and org."""
    return f"{CACHE_KEY_PREFIX}:{base_url}:{org_id}"


def _normalize_str(value: Any) -> str:
    """Keep only string profile fields; non-strings become ''."""
    return value if isinstance(value, str) else ""


def _cache_get(key: str):
    """Read from Django cache; treat backend failures as a miss."""
    try:
        return cache.get(key)
    except Exception:  # noqa: BLE001 — cache outage must not break principals
        LOGGER.exception("Cost principal profile cache read failed")
        return None


def _cache_set(key: str, value: dict[str, dict[str, Any]], timeout: int) -> None:
    """Write to Django cache; ignore backend failures."""
    try:
        cache.set(key, value, timeout=timeout)
    except Exception:  # noqa: BLE001 — cache outage must not break principals
        LOGGER.exception("Cost principal profile cache write failed")


def get_profiles_from_service(org_id: str | None) -> dict[str, dict[str, Any]]:
    """Fetch username(lower) -> profile from the Cost profile service.

    Returns ``{}`` when unset, org_id missing, or the request fails so the
    principals API can fall back to safe bypass defaults. Successful and failed
    fetches are cached briefly per org to avoid repeated timeouts.
    """
    base = (getattr(settings, "PRINCIPAL_PROFILE_SERVICE_URL", "") or "").strip().rstrip("/")
    if not base or not org_id:
        return {}

    timeout = int(getattr(settings, "PRINCIPAL_PROFILE_REQUEST_TIMEOUT", 5) or 5)
    cache_ttl = int(getattr(settings, "PRINCIPAL_PROFILE_CACHE_TTL", 30) or 30)

    key = _cache_key(base, str(org_id))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    url = f"{base}/v1/orgs/{org_id}/profiles"
    headers = {"Accept": "application/json"}
    token = (getattr(settings, "PRINCIPAL_PROFILE_TOKEN", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except Exception:  # noqa: BLE001 — enrichment must never break principals
        LOGGER.exception("Cost principal profile service request failed (%s)", url)
        _cache_set(key, {}, cache_ttl)
        return {}

    raw = body.get("profiles") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        LOGGER.warning("Cost principal profile service returned unexpected body type")
        _cache_set(key, {}, cache_ttl)
        return {}

    profiles: dict[str, dict[str, Any]] = {}
    for username, item in raw.items():
        if not isinstance(item, dict):
            continue
        profiles[str(username).lower()] = {
            "first_name": _normalize_str(item.get("first_name")),
            "last_name": _normalize_str(item.get("last_name")),
            "email": _normalize_str(item.get("email")),
            "is_org_admin": bool(item.get("is_org_admin", False)),
            "enabled": bool(item.get("enabled", True)),
        }
    _cache_set(key, profiles, cache_ttl)
    return profiles
