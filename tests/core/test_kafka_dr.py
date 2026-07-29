"""Tests for core.kafka_dr utility functions."""

import json

from django.test import TestCase

from core.kafka_dr import _unwrap_schema_envelope


class TestUnwrapSchemaEnvelope(TestCase):
    """Tests for _unwrap_schema_envelope."""

    def test_unwraps_json_string_payload(self):
        inner = {"org_id": "12345", "workspace": {"id": "ws-1"}, "operation": "create"}
        envelope = {"schema": {"type": "string"}, "payload": json.dumps(inner)}
        result = _unwrap_schema_envelope(envelope)
        self.assertEqual(result, inner)

    def test_unwraps_dict_payload(self):
        inner = {"org_id": "12345", "workspace": {"id": "ws-1"}}
        envelope = {"schema": {"type": "struct"}, "payload": inner}
        result = _unwrap_schema_envelope(envelope)
        self.assertEqual(result, inner)

    def test_passes_through_flat_event(self):
        flat = {"org_id": "12345", "workspace": {"id": "ws-1"}, "operation": "create"}
        result = _unwrap_schema_envelope(flat)
        self.assertIs(result, flat)

    def test_passes_through_pre_smt_format(self):
        pre_smt = {
            "aggregatetype": "workspace",
            "aggregateid": "prod",
            "payload": {"org_id": "12345", "workspace": {"id": "ws-1"}},
        }
        result = _unwrap_schema_envelope(pre_smt)
        self.assertIs(result, pre_smt)

    def test_handles_invalid_json_string_payload(self):
        envelope = {"schema": {"type": "string"}, "payload": "not-valid-json"}
        result = _unwrap_schema_envelope(envelope)
        self.assertEqual(result, envelope)

    def test_handles_null_payload(self):
        envelope = {"schema": {"type": "string"}, "payload": None}
        result = _unwrap_schema_envelope(envelope)
        self.assertEqual(result, envelope)
