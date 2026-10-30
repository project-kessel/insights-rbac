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

"""Optional Keycloak profile enrichment for BYPASS_BOP_VERIFICATION mode.

When Cost Management runs on-prem, PrincipalProxy cannot call BOP/IT. Local
Principal rows only store username/user_id. If KEYCLOAK_* env vars are set
(same contract as the Keycloak→RBAC sync CronJob), this module loads user
profile fields and the ``org-admin`` realm role so principals list/detail can
return SaaS-parity ``is_org_admin``, ``email``, ``first_name``, and ``last_name``.

When Keycloak is not configured, callers keep safe defaults
(``is_org_admin=False``, empty names, synthetic email).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests
from django.core.cache import cache

LOGGER = logging.getLogger(__name__)

ORG_ADMIN_REALM_ROLE = "org-admin"
CACHE_KEY = "rbac:keycloak_principal_profiles"
CACHE_TTL_SECONDS = 60
TOKEN_REFRESH_MARGIN = 0.8
REQUEST_TIMEOUT = 15
PAGE_SIZE = 100


def keycloak_configured() -> bool:
    """Return True when the Keycloak Admin API env vars are present."""
    return bool(
        os.environ.get("KEYCLOAK_URL")
        and os.environ.get("KEYCLOAK_CLIENT_ID")
        and os.environ.get("KEYCLOAK_CLIENT_SECRET")
    )


_LOCAL_CACHE: dict[str, Any] = {"expires_at": 0.0, "profiles": {}}


def get_keycloak_profiles() -> dict[str, dict[str, Any]]:
    """Return username(casefold) -> profile dict, or empty if Keycloak unset/unreachable.

    Profile dict keys: first_name, last_name, email, is_org_admin, enabled.
    Uses an in-process TTL cache plus Django cache when available so Redis outages
    cannot break the principals API.
    """
    if not keycloak_configured():
        return {}

    now = time.monotonic()
    if _LOCAL_CACHE["profiles"] and now < float(_LOCAL_CACHE["expires_at"]):
        return _LOCAL_CACHE["profiles"]

    try:
        cached = cache.get(CACHE_KEY)
        if isinstance(cached, dict):
            _LOCAL_CACHE["profiles"] = cached
            _LOCAL_CACHE["expires_at"] = now + CACHE_TTL_SECONDS
            return cached
    except Exception:  # noqa: BLE001 — Django cache is optional
        LOGGER.debug("Django cache unavailable for Keycloak profiles", exc_info=True)

    try:
        profiles = _fetch_profiles_from_keycloak()
    except Exception:  # noqa: BLE001 — enrichment must never break principals API
        LOGGER.exception("Keycloak principal enrichment failed; using bypass defaults")
        return {}

    _LOCAL_CACHE["profiles"] = profiles
    _LOCAL_CACHE["expires_at"] = now + CACHE_TTL_SECONDS
    try:
        cache.set(CACHE_KEY, profiles, CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — cache is best-effort
        LOGGER.debug("Failed to cache Keycloak principal profiles", exc_info=True)
    return profiles


class _KeycloakAdminClient:
    """Minimal Keycloak Admin REST client for principal enrichment."""

    def __init__(self):
        self.base_url = os.environ["KEYCLOAK_URL"].rstrip("/")
        self.realm = os.environ.get("KEYCLOAK_REALM", "kubernetes")
        self.client_id = os.environ["KEYCLOAK_CLIENT_ID"]
        self.client_secret = os.environ["KEYCLOAK_CLIENT_SECRET"]
        verify_raw = os.environ.get("KEYCLOAK_TLS_VERIFY", "true").lower()
        self.verify_tls = verify_raw not in ("false", "0", "no")
        self._access_token: str | None = None
        self._token_acquired_at = 0.0
        self._token_expires_in = 0

    def _token_is_fresh(self) -> bool:
        if not self._access_token or self._token_expires_in <= 0:
            return False
        elapsed = time.monotonic() - self._token_acquired_at
        return elapsed < (self._token_expires_in * TOKEN_REFRESH_MARGIN)

    def authenticate(self) -> None:
        url = f"{self.base_url}/realms/{self.realm}/protocol/openid-connect/token"
        response = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=REQUEST_TIMEOUT,
            verify=self.verify_tls,
        )
        response.raise_for_status()
        body = response.json()
        self._access_token = body["access_token"]
        self._token_acquired_at = time.monotonic()
        self._token_expires_in = int(body.get("expires_in", 300))

    def ensure_authenticated(self) -> None:
        if not self._token_is_fresh():
            self.authenticate()

    def _get(self, path: str, params: dict | None = None) -> Any:
        self.ensure_authenticated()
        url = f"{self.base_url}/admin/realms/{self.realm}{path}"
        response = requests.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
            verify=self.verify_tls,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return []
        return response.json()

    def list_users(self) -> list[dict]:
        users: list[dict] = []
        offset = 0
        while True:
            page = self._get("/users", {"first": offset, "max": PAGE_SIZE, "briefRepresentation": "false"})
            if not isinstance(page, list):
                break
            users.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return users

    def list_org_admin_usernames(self) -> set[str]:
        """Return usernames that hold the realm role ``org-admin``."""
        usernames: set[str] = set()
        offset = 0
        while True:
            page = self._get(
                f"/roles/{ORG_ADMIN_REALM_ROLE}/users",
                {"first": offset, "max": PAGE_SIZE},
            )
            if not isinstance(page, list):
                # Role may not exist yet — treat as no org-admins.
                if isinstance(page, dict):
                    LOGGER.warning("Unexpected Keycloak response listing org-admin users: %s", page)
                break
            for user in page:
                username = user.get("username")
                if username:
                    usernames.add(str(username).casefold())
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return usernames


def _fetch_profiles_from_keycloak() -> dict[str, dict[str, Any]]:
    client = _KeycloakAdminClient()
    org_admins = client.list_org_admin_usernames()
    profiles: dict[str, dict[str, Any]] = {}
    for user in client.list_users():
        username = user.get("username")
        if not username:
            continue
        key = str(username).casefold()
        profiles[key] = {
            "first_name": user.get("firstName") or "",
            "last_name": user.get("lastName") or "",
            "email": user.get("email") or "",
            "is_org_admin": key in org_admins,
            "enabled": bool(user.get("enabled", True)),
        }
    LOGGER.info(
        "Loaded %d Keycloak user profiles (%d org-admins) for principal enrichment",
        len(profiles),
        sum(1 for profile in profiles.values() if profile.get("is_org_admin")),
    )
    return profiles
