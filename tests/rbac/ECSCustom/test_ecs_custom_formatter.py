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
"""Test the ECSCustomFormatter."""

import logging
import json
from django.test import TestCase, RequestFactory

from rbac.ECSCustom import ECSCustomFormatter


class TestECSCustomFormatter(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.formatter = ECSCustomFormatter()

    def format(self, log_record):
        """Helper to format a log record and parse the resulting JSON."""
        formatted_json_string = None
        try:
            formatted_json_string = self.formatter.format(log_record)
        except Exception as e:
            self.fail(f"ECSCustomFormatter.format() raised an unexpected exception: {e}")

        try:
            log_output = json.loads(formatted_json_string)
        except json.JSONDecodeError:
            self.fail(f"Formatted log output is not valid JSON: {formatted_json_string}")
        return log_output

    def log_for_request(self, request):
        """Helper to create a LogRecord and attach the request."""
        log_record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="dummy_module.py",
            lineno=0,
            msg="Test log message.",
            args=(),
            exc_info=None,
        )
        log_record.request = request
        return log_record

    def test_formatting_request_without_content_length(self):
        # GET won't normally have content-length header
        request_no_cl = self.factory.get("/")
        self.assertIsNone(request_no_cl.headers.get("Content-Length"))

        log_record = self.log_for_request(request_no_cl)
        log_output = self.format(log_record)

        self.assertIn("http", log_output)
        self.assertIn("request", log_output["http"])
        self.assertIn("body", log_output["http"]["request"])
        self.assertIn("bytes", log_output["http"]["request"]["body"])
        self.assertEqual(
            log_output["http"]["request"]["body"]["bytes"],
            0,
            "http.request.bytes should default to 0 when Content-Length is missing.",
        )

    def test_formatting_request_with_content_length(self):
        request_with_cl = self.factory.post("/", data={"fake": "data"})
        expected_content_length = request_with_cl.headers.get("Content-Length")

        self.assertIsNotNone(expected_content_length, "Content-Length header missing from POST request.")

        log_record = self.log_for_request(request_with_cl)
        log_output = self.format(log_record)

        self.assertEqual(
            log_output["http"]["request"]["body"]["bytes"],
            int(expected_content_length),
            "http.request.bytes does not match actual Content-Length.",
        )
        self.assertEqual(log_output["http"]["request"]["method"], "POST")

    def test_env_name_included_in_labels(self):
        log_record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="dummy_module.py",
            lineno=0,
            msg="Test log message.",
            args=(),
            exc_info=None,
        )
        log_record.env_name = "prod"
        log_output = self.format(log_record)

        self.assertIn("labels", log_output)
        self.assertEqual(log_output["labels"]["env"], "prod")

    def test_env_name_missing_no_labels(self):
        log_record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="dummy_module.py",
            lineno=0,
            msg="Test log message.",
            args=(),
            exc_info=None,
        )
        log_output = self.format(log_record)

        self.assertNotIn("labels", log_output)
