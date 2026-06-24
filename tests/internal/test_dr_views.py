"""Tests for disaster recovery internal endpoint."""

import json
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
            dry_run=True,
        )

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
        self.assertEqual(result["status"], "failed")
        self.assertIn("error", result)
