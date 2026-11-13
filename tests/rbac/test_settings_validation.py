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
"""Tests for settings validation logic."""

import importlib
import os
import sys
from unittest import TestCase
from unittest.mock import patch


class SettingsValidationTest(TestCase):
    """Test settings validation that occurs at module import time."""

    def test_read_only_api_mode_is_parsed_as_boolean(self):
        """Parse the global API read-only setting as a Boolean."""
        with patch.dict(os.environ, {"READ_ONLY_API_MODE": "False"}, clear=False):
            if "rbac.settings" in sys.modules:
                del sys.modules["rbac.settings"]

            try:
                import rbac.settings

                self.assertIs(rbac.settings.READ_ONLY_API_MODE, False)
            finally:
                if "rbac.settings" in sys.modules:
                    del sys.modules["rbac.settings"]
                importlib.import_module("rbac.settings")
