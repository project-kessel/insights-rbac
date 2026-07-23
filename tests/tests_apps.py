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
"""Test the API apps module."""

import logging
from unittest.mock import patch

from django.apps import apps
from django.test import TestCase


class AppsModelTest(TestCase):
    """Tests against the apps functions."""

    @classmethod
    def setUpClass(cls):
        """Set up test class."""
        # remove filters on logging
        logging.disable(logging.NOTSET)

    @classmethod
    def tearDownClass(cls):
        """Tear down test class."""
        # restore filters on logging
        logging.disable(logging.CRITICAL)

    @patch("management.apps.sys.argv", ["manage.py", "test"])
    @patch("management.apps.role_seeding")
    @patch("management.apps.group_seeding")
    def test_ready_silent_run(self, mock_role_seeding, mock_group_seeding):
        """Test that ready functions are not called."""
        mock_role_seeding.assert_not_called()
        mock_group_seeding.assert_not_called()

    @patch("management.apps.sys.argv", ["manage.py", "runserver"])
    @patch("management.apps.role_seeding")
    @patch("management.apps.group_seeding")
    @patch("management.apps.permission_seeding")
    def test_objects_seeding(self, mock_role_seeding, mock_group_seeding, mock_permission_seeding):
        """Test the server objects seeding startup."""
        mgmt_config = apps.get_app_config("management")

        mgmt_config.ready()
        mock_role_seeding.assert_called()
        mock_group_seeding.assert_called()
        mock_permission_seeding.assert_called()

    def test_catch_operational_error(self):
        """Test that we handle exceptions thrown when tables are missing."""
        mgmt_config = apps.get_app_config("management")

        # the real test
        mgmt_config.ready()
