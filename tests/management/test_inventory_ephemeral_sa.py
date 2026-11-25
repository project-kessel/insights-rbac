# Copyright 2026 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
"""Tests for ephemeral Inventory credential bootstrap concurrency."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from django.test import SimpleTestCase
from management import inventory_ephemeral_sa as bootstrap


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
