# Copyright 2026 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
"""Tests for ephemeral Inventory credential bootstrap."""

import io
import json
import threading
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from django.test import SimpleTestCase
from management import inventory_ephemeral_sa as bootstrap


class EphemeralInventoryClientTests(SimpleTestCase):
    """Exercise the bootstrap sequence at the HTTP boundary."""

    def setUp(self):
        super().setUp()
        self.environment = mock.patch.dict(
            bootstrap.os.environ, {"APP_NAMESPACE": "test-ns", "KUBERNETES_SERVICE_HOST": "kubernetes"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service_account = mock.patch("builtins.open", mock.mock_open(read_data="test-token"))
        self.service_account.start()
        self.addCleanup(self.service_account.stop)
        self.context = mock.patch.object(bootstrap.ssl, "create_default_context")
        self.context.start()
        self.addCleanup(self.context.stop)

    @staticmethod
    def response(payload, status=200):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = json.dumps(payload).encode()
        return response

    def run_bootstrap(self, client_responses):
        responses = [
            self.response({"data": {"username": "YWRtaW4=", "password": "cGFzcw=="}}),
            self.response({"access_token": "admin-token"}),
            *client_responses,
        ]
        with mock.patch.object(bootstrap.urllib.request, "urlopen", side_effect=responses) as urlopen:
            self.assertEqual(bootstrap._ensure_client(), ("rbac-inventory-api", "inventory-secret"))
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(
            requests[0].full_url, "https://kubernetes:443/api/v1/namespaces/test-ns/secrets/env-test-ns-keycloak"
        )
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-token")
        self.assertEqual(requests[1].get_method(), "POST")
        self.assertEqual(
            urllib.parse.parse_qs(requests[1].data.decode()),
            {"grant_type": ["password"], "client_id": ["admin-cli"], "username": ["admin"], "password": ["pass"]},
        )
        for request in requests[2:]:
            self.assertEqual(request.get_header("Authorization"), "Bearer admin-token")
        return requests

    def test_reuses_existing_client(self):
        requests = self.run_bootstrap(
            [self.response([{"id": "existing-id"}]), self.response({"value": "inventory-secret"})]
        )
        self.assertEqual([request.get_method() for request in requests], ["GET", "POST", "GET", "GET"])
        self.assertTrue(requests[-1].full_url.endswith("/existing-id/client-secret"))

    def test_creates_or_reuses_concurrently_created_client(self):
        for status in (201, 409):
            with self.subTest(status=status):
                requests = self.run_bootstrap(
                    [
                        self.response([]),
                        self.response({}, status=status),
                        self.response([{"id": "new-id"}]),
                        self.response({"value": "inventory-secret"}),
                    ]
                )
                self.assertEqual(
                    [request.get_method() for request in requests], ["GET", "POST", "GET", "POST", "GET", "GET"]
                )
                self.assertEqual(
                    json.loads(requests[3].data),
                    {"clientId": "rbac-inventory-api", "publicClient": False, "serviceAccountsEnabled": True},
                )
                self.assertTrue(requests[-1].full_url.endswith("/new-id/client-secret"))

    def test_secret_error_stops_before_keycloak(self):
        error = urllib.error.HTTPError("https://kubernetes/secret", 403, "Forbidden", {}, io.BytesIO(b"forbidden"))
        with mock.patch.object(bootstrap.urllib.request, "urlopen", side_effect=error) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                bootstrap._ensure_client()
        self.assertEqual(urlopen.call_count, 1)

    def test_keycloak_error_stops_before_client_lookup(self):
        error = urllib.error.HTTPError("http://keycloak/token", 401, "Unauthorized", {}, io.BytesIO(b"unauthorized"))
        with mock.patch.object(
            bootstrap.urllib.request,
            "urlopen",
            side_effect=[self.response({"data": {"username": "YWRtaW4=", "password": "cGFzcw=="}}), error],
        ) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                bootstrap._ensure_client()
        self.assertEqual(urlopen.call_count, 2)


class EphemeralInventoryBootstrapTests(SimpleTestCase):
    """Bootstrap failures should not cause a serialized retry storm."""

    def test_waiting_callers_share_failure_and_later_retry(self):
        """One failed attempt suppresses waiters; retry succeeds after the cooldown."""
        entered = threading.Event()
        release = threading.Event()

        def fail_once():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("bootstrap was not released")
            raise OSError("Keycloak unavailable")

        with (
            mock.patch.object(bootstrap, "_cached", None),
            mock.patch.object(bootstrap, "_retry_after", 0.0),
            mock.patch.object(bootstrap.time, "monotonic", return_value=100.0) as clock,
            mock.patch.object(bootstrap, "_ensure_client", side_effect=fail_once) as ensure,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(bootstrap.ensure_ephemeral_inventory_sa)
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(bootstrap.ensure_ephemeral_inventory_sa)
            release.set()
            with self.assertRaises(OSError):
                first.result(timeout=5)
            with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
                second.result(timeout=5)
            self.assertEqual(ensure.call_count, 1)
            clock.return_value = 130.0
            ensure.side_effect = None
            ensure.return_value = ("client", "secret")
            self.assertEqual(bootstrap.ensure_ephemeral_inventory_sa(), ("client", "secret"))
            self.assertEqual(bootstrap.ensure_ephemeral_inventory_sa(), ("client", "secret"))
            self.assertEqual(ensure.call_count, 2)
