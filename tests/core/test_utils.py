#
# Copyright 2022 Red Hat, Inc.
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

"""Test the shared application utilities."""

import importlib
import os
from unittest import mock

from core.utils import *
from django.test import SimpleTestCase, TestCase, override_settings
from datetime import datetime, timedelta


class ParseIsoDateTimeTests(SimpleTestCase):
    """Test ISO datetime parsing without database access."""

    def test_parse_iso_datetime_uses_utc_for_naive_values(self):
        """Treat ISO timestamps without a timezone as UTC."""
        parsed = parse_iso_datetime("2026-11-26T12:34:56")

        self.assertEqual(parsed, datetime(2026, 11, 26, 12, 34, 56, tzinfo=pytz.UTC))

    def test_parse_iso_datetime_accepts_zulu_suffix(self):
        """Parse an ISO timestamp with the UTC Z suffix."""
        parsed = parse_iso_datetime("2026-11-26T12:34:56Z")

        self.assertEqual(parsed, datetime(2026, 11, 26, 12, 34, 56, tzinfo=pytz.UTC))

    def test_parse_iso_datetime_preserves_explicit_offset(self):
        """Preserve an explicit timezone offset."""
        parsed = parse_iso_datetime("2026-11-26T12:34:56+02:00")

        self.assertEqual(parsed.utcoffset(), timedelta(hours=2))

    def test_parse_iso_datetime_rejects_non_iso_values(self):
        """Reject malformed values so settings can apply their safe fallback."""
        for value in ("not-a-real-time", "2030-01-01X12:00:00Z", "2030-01-01\n12:00:00Z", "2030-001T12:00:00Z"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_iso_datetime(value)

    def test_parse_iso_datetime_accepts_calendar_date_and_space_separator(self):
        """Accept calendar dates and space-separated timestamps."""
        self.assertEqual(parse_iso_datetime("2030-01-01"), datetime(2030, 1, 1, tzinfo=pytz.UTC))
        self.assertEqual(parse_iso_datetime("2030-01-01 12:00:00Z"), datetime(2030, 1, 1, 12, tzinfo=pytz.UTC))

    def test_invalid_cutoffs_use_safe_fallback_in_both_settings(self):
        """Invalid separators disable destructive API and seeding operations."""
        import rbac.settings as settings_module

        try:
            with mock.patch.dict(
                os.environ,
                {
                    "RBAC_DESTRUCTIVE_API_ENABLED_UNTIL": "2030-01-01X12:00:00Z",
                    "RBAC_DESTRUCTIVE_SEEDING_ENABLED_UNTIL": "2030-01-01X12:00:00Z",
                },
            ):
                importlib.reload(settings_module)
                epoch = datetime(1970, 1, 1, tzinfo=pytz.UTC)
                self.assertEqual(settings_module.INTERNAL_DESTRUCTIVE_API_OK_UNTIL, epoch)
                self.assertEqual(settings_module.DESTRUCTIVE_SEEDING_OK_UNTIL, epoch)
        finally:
            importlib.reload(settings_module)


class CoreUtilsTests(TestCase):
    """Test the utils module."""

    def valid_destructive_time():
        return datetime.utcnow().replace(tzinfo=pytz.UTC) + timedelta(hours=1)

    def invalid_destructive_time():
        return datetime.utcnow().replace(tzinfo=pytz.UTC) - timedelta(hours=1)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=valid_destructive_time())
    def test_destructive_ok_true(self):
        """Test that it's true when within date range."""
        self.assertEqual(destructive_ok("api"), True)

    @override_settings(INTERNAL_DESTRUCTIVE_API_OK_UNTIL=invalid_destructive_time())
    def test_destructive_ok_false(self):
        """Test that it's false when not within date range."""
        self.assertEqual(destructive_ok("api"), False)

    @override_settings(DESTRUCTIVE_SEEDING_OK_UNTIL=valid_destructive_time())
    def test_destructive_ok_true(self):
        """Test that it's true when within date range."""
        self.assertEqual(destructive_ok("seeding"), True)

    @override_settings(DESTRUCTIVE_SEEDING_OK_UNTIL=invalid_destructive_time())
    def test_destructive_ok_false(self):
        """Test that it's false when not within date range."""
        self.assertEqual(destructive_ok("seeding"), False)
