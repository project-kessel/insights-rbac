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
"""Tests for the workspace DR recovery internal endpoint and Celery task."""

import json
from base64 import b64encode
from json import dumps as json_dumps
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from api.common import RH_IDENTITY_HEADER
from api.models import Tenant

DR_URL = "/_private/api/disaster_recovery/workspaces/"


def _build_internal_identity_header(org_id: str = "88888", account_id: str = "99999") -> dict[str, bytes]:
    """Build an internal X-RH-Identity header for test requests."""
    identity = {
        "identity": {
            "account_number": account_id,
            "org_id": org_id,
            "type": "Associate",
            "associate": {
                "email": "dr@test.com",
            },
            "internal": {"org_id": org_id},
        }
    }
    encoded = b64encode(json_dumps(identity).encode("utf-8"))
    return {RH_IDENTITY_HEADER: encoded}


@override_settings(DR_RECOVERY_ENABLED=True)
class TestRecoverWorkspaceEventsEndpoint(TestCase):
    """Tests for POST /_private/api/disaster_recovery/workspaces/."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        super().setUpClass()
        cls.tenant = Tenant.objects.create(
            tenant_name="acct_dr_endpoint_test",
            account_id="99999",
            org_id="88888",
            ready=True,
        )
        cls.headers = _build_internal_identity_header()

    @classmethod
    def tearDownClass(cls):
        """Clean up test fixtures."""
        cls.tenant.delete()
        super().tearDownClass()

    def setUp(self):
        """Set up test client."""
        super().setUp()
        self.client = APIClient()

    @patch("internal.views.recover_workspace_events_in_worker.delay")
    def test_valid_request_returns_202_with_task_id(self, mock_delay):
        """Valid request enqueues task and returns 202."""
        mock_delay.return_value.id = "test-task-id-123"
        response = self.client.post(
            DR_URL,
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z", "buffer_minutes": 5}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["task_id"], "test-task-id-123")
        self.assertEqual(data["status"], "enqueued")
        self.assertEqual(data["restore_timestamp"], "2026-05-28T10:00:00Z")
        self.assertEqual(data["buffer_minutes"], 5)
        mock_delay.assert_called_once_with(
            restore_timestamp_iso="2026-05-28T10:00:00Z",
            buffer_minutes=5,
            dry_run=False,
        )

    @patch("internal.views.recover_workspace_events_in_worker.delay")
    def test_default_buffer_minutes(self, mock_delay):
        """buffer_minutes defaults to 5 when not provided."""
        mock_delay.return_value.id = "task-id"
        response = self.client.post(
            DR_URL,
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 202)
        mock_delay.assert_called_once_with(
            restore_timestamp_iso="2026-05-28T10:00:00Z",
            buffer_minutes=5,
            dry_run=False,
        )

    def test_missing_restore_timestamp_returns_400(self):
        """Missing restore_timestamp returns 400."""
        response = self.client.post(
            DR_URL,
            data=json.dumps({"buffer_minutes": 5}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("restore_timestamp is required", response.json()["detail"])

    def test_invalid_timestamp_format_returns_400(self):
        """Invalid timestamp format returns 400."""
        response = self.client.post(
            DR_URL,
            data=json.dumps({"restore_timestamp": "not-a-timestamp"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("valid ISO 8601", response.json()["detail"])

    def test_future_timestamp_returns_400(self):
        """Future timestamp returns 400."""
        response = self.client.post(
            DR_URL,
            data=json.dumps({"restore_timestamp": "2099-01-01T00:00:00Z"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be in the past", response.json()["detail"])

    def test_negative_buffer_minutes_returns_400(self):
        """Negative buffer_minutes returns 400."""
        response = self.client.post(
            DR_URL,
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z", "buffer_minutes": -1}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("non-negative integer", response.json()["detail"])

    def test_non_integer_buffer_minutes_returns_400(self):
        """Non-integer buffer_minutes returns 400."""
        response = self.client.post(
            DR_URL,
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z", "buffer_minutes": "five"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("non-negative integer", response.json()["detail"])

    def test_invalid_json_body_returns_400(self):
        """Invalid JSON body returns 400."""
        response = self.client.post(
            DR_URL,
            data="not json",
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_get_method_not_allowed(self):
        """GET method returns 405."""
        response = self.client.get(DR_URL, **self.headers)
        self.assertEqual(response.status_code, 405)

    @override_settings(DR_RECOVERY_ENABLED=False)
    def test_disabled_feature_flag_returns_403(self):
        """DR recovery disabled returns 403."""
        response = self.client.post(
            DR_URL,
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z"}),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("disabled", response.json()["detail"])

    def test_no_identity_header_returns_403(self):
        """Request without identity header returns 403."""
        response = self.client.post(
            DR_URL,
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


@override_settings(DR_RECOVERY_ENABLED=True)
class TestRecoverWorkspaceEventsTask(TestCase):
    """Tests for the Celery task recover_workspace_events_in_worker."""

    @override_settings(DR_RECOVERY_ENABLED=False)
    def test_task_returns_early_when_disabled(self):
        """Task returns message when DR_RECOVERY_ENABLED is False."""
        from management.tasks import recover_workspace_events_in_worker

        result = recover_workspace_events_in_worker("2026-05-28T10:00:00Z")
        self.assertEqual(result["message"], "DR recovery disabled (DR_RECOVERY_ENABLED=False)")

    @patch("core.kafka_dr.read_events_by_timestamp")
    def test_task_calls_kafka_reader_with_correct_params(self, mock_read):
        """Task calls read_events_by_timestamp with correct topic and timestamps."""
        mock_read.return_value = []

        from management.tasks import recover_workspace_events_in_worker

        result = recover_workspace_events_in_worker("2026-05-28T10:00:00Z", buffer_minutes=10)

        mock_read.assert_called_once()
        call_kwargs = mock_read.call_args[1]
        self.assertEqual(call_kwargs["topic"], "outbox.event.workspace")
        self.assertIn("start_timestamp_ms", call_kwargs)
        self.assertIn("end_timestamp_ms", call_kwargs)
        self.assertEqual(result["kafka_events_read"], 0)

    @patch("management.workspace.dr_recovery.generate_corrective_workspace_events")
    @patch("core.kafka_dr.read_events_by_timestamp")
    def test_task_returns_structured_result(self, mock_read, mock_generate):
        """Task returns structured dict with all expected fields."""
        mock_read.return_value = []
        mock_generate.return_value = {
            "total_events": 5,
            "corrective_creates": 1,
            "corrective_deletes": 2,
            "corrective_updates": 1,
            "skipped": 1,
            "errors": 0,
            "error_details": [],
        }

        from management.tasks import recover_workspace_events_in_worker

        result = recover_workspace_events_in_worker("2026-05-28T10:00:00Z", buffer_minutes=5)

        self.assertIn("duration_seconds", result)
        self.assertIn("restore_timestamp", result)
        self.assertIn("buffer_minutes", result)
        self.assertIn("kafka_events_read", result)
        self.assertEqual(result["total_events"], 5)
        self.assertEqual(result["corrective_creates"], 1)
        self.assertEqual(result["corrective_deletes"], 2)
        self.assertEqual(result["corrective_updates"], 1)
        self.assertEqual(result["buffer_minutes"], 5)

    @patch("core.kafka_dr.read_events_by_timestamp")
    def test_task_handles_naive_timestamp(self, mock_read):
        """Task handles naive (timezone-unaware) timestamps by assuming UTC."""
        mock_read.return_value = []

        from management.tasks import recover_workspace_events_in_worker

        result = recover_workspace_events_in_worker("2026-05-28T10:00:00")

        mock_read.assert_called_once()
        self.assertIn("duration_seconds", result)
