"""Tests for disaster recovery internal endpoint."""

import json
import os
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from tests.identity_request import IdentityRequest


class DisasterRecoveryViewTest(IdentityRequest):
    def setUp(self):
        super().setUp()
        internal_context = self._create_request_context(self.customer_data, self.user_data, is_internal=True)
        self.headers = internal_context["request"].META

    def test_view_disabled_returns_403(self):
        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 403)
        data = json.loads(response.content)
        self.assertIn("not enabled", data["error"])

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_valid_post_returns_202(self, mock_task):
        mock_result = MagicMock()
        mock_result.id = "test-task-id-123"
        mock_task.delay.return_value = mock_result

        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z", "buffer_seconds": 300}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 202)
        data = json.loads(response.content)
        self.assertEqual(data["task_id"], "test-task-id-123")
        self.assertEqual(data["buffer_seconds"], 300)
        self.assertIn("restore_timestamp_ms", data)
        mock_task.delay.assert_called_once()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_missing_restore_timestamp(self, mock_task):
        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps({"buffer_seconds": 300}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("error", data)
        self.assertIn("restore_timestamp", data["error"])
        mock_task.delay.assert_not_called()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_invalid_timestamp_format(self, mock_task):
        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps({"restore_timestamp": "not-a-timestamp"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("error", data)
        mock_task.delay.assert_not_called()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_non_string_timestamps_return_400(self, mock_task):
        for field in ("restore_timestamp", "end_timestamp"):
            for value in (123, True, [], {}):
                with self.subTest(field=field, value=value):
                    body = {"restore_timestamp": "2024-01-01T10:00:00Z", field: value}
                    response = self.client.post(
                        "/_private/api/disaster_recovery/reconcile/",
                        data=json.dumps(body),
                        content_type="application/json",
                        **self.headers,
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(field, response.json()["error"])
        mock_task.delay.assert_not_called()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_naive_timestamps_use_utc_on_non_utc_host(self, mock_task):
        mock_task.delay.return_value.id = "task-naive"
        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                for restore, end in (
                    ("2024-01-01T10:00:00", "2024-01-01T10:00:00Z"),
                    ("2024-01-01T10:00:00Z", "2024-01-01T10:00:00"),
                ):
                    with self.subTest(restore=restore, end=end):
                        response = self.client.post(
                            "/_private/api/disaster_recovery/reconcile/",
                            data=json.dumps({"restore_timestamp": restore, "end_timestamp": end}),
                            content_type="application/json",
                            **self.headers,
                        )
                        self.assertEqual(response.status_code, 202)
                        expected_ms = int(datetime(2024, 1, 1, 10, tzinfo=timezone.utc).timestamp() * 1000)
                        self.assertEqual(response.json()["restore_timestamp_ms"], expected_ms)
                        self.assertEqual(response.json()["end_timestamp_ms"], expected_ms)
                        self.assertEqual(mock_task.delay.call_args.kwargs["restore_timestamp_ms"], expected_ms)
                        self.assertEqual(mock_task.delay.call_args.kwargs["end_timestamp_ms"], expected_ms)
        finally:
            time.tzset()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_invalid_buffer_seconds(self, mock_task):
        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z", "buffer_seconds": -1}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("buffer_seconds", data["error"])
        mock_task.delay.assert_not_called()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_boolean_buffer_seconds_rejected(self, mock_task):
        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z", "buffer_seconds": True}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("buffer_seconds", data["error"])
        mock_task.delay.assert_not_called()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_dry_run_passes_flag(self, mock_task):
        mock_result = MagicMock()
        mock_result.id = "task-dry-run"
        mock_task.delay.return_value = mock_result

        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z", "dry_run": True}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 202)
        data = json.loads(response.content)
        self.assertTrue(data["dry_run"])
        mock_task.delay.assert_called_once_with(
            restore_timestamp_ms=mock_task.delay.call_args.kwargs["restore_timestamp_ms"],
            buffer_seconds=300,
            end_timestamp_ms=None,
            dry_run=True,
        )

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_end_timestamp_passed_through(self, mock_task):
        mock_result = MagicMock()
        mock_result.id = "task-end-ts"
        mock_task.delay.return_value = mock_result

        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps(
                {
                    "restore_timestamp": "2026-05-28T10:00:00Z",
                    "end_timestamp": "2026-05-28T12:00:00Z",
                }
            ),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 202)
        data = json.loads(response.content)
        self.assertIn("end_timestamp_ms", data)
        self.assertIsNotNone(data["end_timestamp_ms"])
        mock_task.delay.assert_called_once_with(
            restore_timestamp_ms=mock_task.delay.call_args.kwargs["restore_timestamp_ms"],
            buffer_seconds=300,
            end_timestamp_ms=data["end_timestamp_ms"],
            dry_run=False,
        )

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_end_timestamp_before_restore_timestamp_returns_400(self, mock_task):
        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps(
                {
                    "restore_timestamp": "2026-05-28T10:00:00Z",
                    "end_timestamp": "2026-05-28T09:00:00Z",
                }
            ),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("end_timestamp", data["error"])
        mock_task.delay.assert_not_called()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_future_end_timestamp_returns_400(self, mock_task):
        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps(
                {
                    "restore_timestamp": "2026-05-28T10:00:00Z",
                    "end_timestamp": "2099-01-01T00:00:00Z",
                }
            ),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("end_timestamp", data["error"])
        mock_task.delay.assert_not_called()

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    def test_get_method_not_allowed(self):
        response = self.client.get(
            "/_private/api/disaster_recovery/reconcile/",
            **self.headers,
        )

        self.assertEqual(response.status_code, 405)

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True)
    @patch("management.tasks.run_disaster_recovery_reconcile")
    def test_default_buffer_seconds(self, mock_task):
        mock_result = MagicMock()
        mock_result.id = "task-default-buffer"
        mock_task.delay.return_value = mock_result

        response = self.client.post(
            "/_private/api/disaster_recovery/reconcile/",
            data=json.dumps({"restore_timestamp": "2026-05-28T10:00:00Z"}),
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 202)
        data = json.loads(response.content)
        self.assertEqual(data["buffer_seconds"], 300)


class DisasterRecoveryTaskFeatureFlagTest(TestCase):
    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=False)
    def test_task_disabled(self):
        from management.tasks import run_disaster_recovery_reconcile

        result = run_disaster_recovery_reconcile(restore_timestamp_ms=1000000, buffer_seconds=300)
        self.assertEqual(result["message"], "Disaster recovery reconciliation is disabled")

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True, KAFKA_ENABLED=False)
    def test_task_enabled_but_kafka_disabled(self):
        from management.tasks import run_disaster_recovery_reconcile

        result = run_disaster_recovery_reconcile(restore_timestamp_ms=1000000, buffer_seconds=300)
        self.assertIn("KAFKA_ENABLED", result["message"])

    @override_settings(DR_RELATIONS_RECONCILE_ENABLED=True, KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC=None)
    def test_task_enabled_but_topic_not_configured(self):
        from management.tasks import run_disaster_recovery_reconcile

        result = run_disaster_recovery_reconcile(restore_timestamp_ms=1000000, buffer_seconds=300)
        self.assertIn("RBAC_KAFKA_CONSUMER_TOPIC", result["message"])
