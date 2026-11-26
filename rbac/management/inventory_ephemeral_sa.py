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
"""Ephemeral Keycloak SA bootstrap for Inventory API OAuth (in-process, first call).

Reads Keycloak admin user/pass from secret env-<ns>-keycloak via the in-cluster
Kubernetes API (pod SA token), then creates/reuses client rbac-inventory-api.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Tuple

logger = logging.getLogger(__name__)

# Standard in-cluster mount — used only to GET the Keycloak admin secret (not mounted into RBAC).
_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

_CLIENT_ID = "rbac-inventory-api"
_REALM = "redhat-external"
_KEYCLOAK_URL = "http://keycloak:8080/auth"

_lock = threading.Lock()
_cached: Tuple[str, str] | None = None
_retry_after = 0.0
_FAILURE_RETRY_SECONDS = 30


def _http(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    data: bytes | None = None,
    context: ssl.SSLContext | None = None,
    expect: set[int] | None = None,
) -> bytes:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, context=context, timeout=60) as resp:
            body = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read()
        code = e.code
    if expect is not None and code not in expect:
        raise RuntimeError(f"{method} {url} -> HTTP {code}: {body[:500]!r}")
    return body


def _namespace() -> str:
    return os.environ.get("APP_NAMESPACE") or open(f"{_SA_DIR}/namespace", encoding="utf-8").read().strip()


def _k8s_get_secret(name: str) -> dict:
    """GET a namespaced Secret using the pod's service account."""
    token = open(f"{_SA_DIR}/token", encoding="utf-8").read().strip()
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    url = f"https://{host}:{port}/api/v1/namespaces/{_namespace()}/secrets/{name}"
    ctx = ssl.create_default_context(cafile=f"{_SA_DIR}/ca.crt")
    body = _http(
        "GET",
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        context=ctx,
        expect={200},
    )
    data = json.loads(body)["data"]
    return {k: base64.b64decode(v).decode() for k, v in data.items()}


def _ensure_client() -> Tuple[str, str]:
    admin = _k8s_get_secret(f"env-{_namespace()}-keycloak")

    token_body = _http(
        "POST",
        f"{_KEYCLOAK_URL}/realms/master/protocol/openid-connect/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": admin["username"],
                "password": admin["password"],
            }
        ).encode(),
        expect={200},
    )
    admin_token = json.loads(token_body)["access_token"]
    auth = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}
    clients_url = f"{_KEYCLOAK_URL}/admin/realms/{_REALM}/clients"
    q = urllib.parse.quote(_CLIENT_ID)

    clients = json.loads(_http("GET", f"{clients_url}?clientId={q}", headers=auth, expect={200}))
    if clients:
        client_uuid = clients[0]["id"]
    else:
        _http(
            "POST",
            clients_url,
            headers=auth,
            data=json.dumps({"clientId": _CLIENT_ID, "publicClient": False, "serviceAccountsEnabled": True}).encode(),
            expect={201, 409},
        )
        client_uuid = json.loads(_http("GET", f"{clients_url}?clientId={q}", headers=auth, expect={200}))[0]["id"]

    secret = json.loads(_http("GET", f"{clients_url}/{client_uuid}/client-secret", headers=auth, expect={200}))[
        "value"
    ]
    return _CLIENT_ID, secret


def ensure_ephemeral_inventory_sa() -> Tuple[str, str]:
    """Resolve credentials once per process; temporarily suppress retries after a failure."""
    global _cached, _retry_after
    if _cached is not None:
        return _cached
    with _lock:
        if _cached is not None:
            return _cached
        if time.monotonic() < _retry_after:
            raise RuntimeError("Ephemeral Inventory credential bootstrap is temporarily unavailable")
        try:
            _cached = _ensure_client()
        except Exception:
            _retry_after = time.monotonic() + _FAILURE_RETRY_SECONDS
            raise
        _retry_after = 0.0
        logger.info("Resolved ephemeral Inventory Keycloak SA client_id=%s", _cached[0])
        return _cached
