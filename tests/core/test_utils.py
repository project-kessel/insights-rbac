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
        with self.assertRaises(ValueError):
            parse_iso_datetime("not-a-real-time")


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
