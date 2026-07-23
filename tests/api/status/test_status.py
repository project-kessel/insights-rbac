#
# Copyright 2019 Red Hat, Inc.
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
"""Test the status API."""

import logging
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from api.models import Status, Tenant


class StatusModelTest(TestCase):
    """Tests against the status functions."""

    @classmethod
    def setUpClass(cls):
        """Test Class setup."""
        # remove filters on logging
        logging.disable(logging.NOTSET)
        cls.status_info = Status()

    @classmethod
    def tearDownClass(cls):
        """Test Class teardown."""
        # restore filters on logging
        logging.disable(logging.CRITICAL)

    def setUp(self):
        """Create test case setup."""
        super().setUp()
        t, created = Tenant.objects.get_or_create(tenant_name="public")
        if created:
            t.ready = True
            t.save()

    def test_commit_with_env(self):
        """Test the commit method via environment."""
        expected = "local-dev"
        result = self.status_info.commit
        self.assertEqual(result, expected)


class StatusViewTest(TestCase):
    """Tests the status view."""

    def test_status_endpoint(self):
        """Test the status endpoint."""
        url = reverse("v1_api:server-status")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class ReadyViewTest(TestCase):
    """Tests the ready (readiness probe) view."""

    def setUp(self):
        super().setUp()
        import api.status.view as status_view

        status_view._SEEDING_COMPLETE_CACHE = None

    def test_ready_returns_503_when_platform_default_group_missing(self):
        """When MIGRATE_AND_SEED_ON_INIT is False and no platform default group, returns 503."""
        with patch.dict("os.environ", {"MIGRATE_AND_SEED_ON_INIT": "false"}, clear=False):
            url = reverse("v1_api:server-ready")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 503)

    def test_ready_returns_200_when_migrate_and_seed_on_init(self):
        """When MIGRATE_AND_SEED_ON_INIT is True, returns 200 without DB check."""
        with patch.dict("os.environ", {"MIGRATE_AND_SEED_ON_INIT": "true"}):
            url = reverse("v1_api:server-ready")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    @override_settings(ATOMIC_RETRY_DISABLED=True)
    def test_ready_returns_200_when_platform_default_group_exists(self):
        """When MIGRATE_AND_SEED_ON_INIT is False but platform default group exists, returns 200."""
        from management.group.definer import seed_group

        seed_group()
        with patch.dict("os.environ", {"MIGRATE_AND_SEED_ON_INIT": "false"}, clear=False):
            url = reverse("v1_api:server-ready")
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
