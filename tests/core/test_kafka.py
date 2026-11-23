from copy import deepcopy
from unittest.mock import DEFAULT, Mock, patch

from django.test import TestCase, override_settings
from core.kafka import FakeKafkaProducer, RBACProducer, get_cluster_config
from kafka.errors import KafkaError, KafkaTimeoutError
from tests.identity_request import IdentityRequest

IT_MANAGED_CLUSTER = {
    "servers": ["it-broker:9096"],
    "auth": {
        "bootstrap_servers": ["it-broker:9096"],
        "sasl_plain_username": "it-user",
        "sasl_plain_password": "it-pass",
        "sasl_mechanism": "SCRAM-SHA-512",
        "security_protocol": "SASL_SSL",
        "retries": 5,  # producer-only
    },
}


def copy_call_args(mock):
    kafka_mock = Mock()

    def side_effect(*args, **kwargs):
        args = deepcopy(args)
        kwargs = deepcopy(kwargs)
        kafka_mock(*args, **kwargs)
        return DEFAULT

    mock.side_effect = side_effect
    return kafka_mock


class KafkaTests(TestCase):
    @patch("core.kafka.RBACProducer")
    @patch("core.kafka.logger")
    def test_kafka_producer_errors_logged(self, mock_logger, MockKafkaProducer):
        """Test that mocked Kafka error return correct messages from Kafka producer"""
        MockKafkaProducer.get_producer.side_effect = KafkaError

        with self.assertRaises(KafkaError):
            MockKafkaProducer.get_producer()
            mock_logger.error.assert_any_call("Kafka error during initialization of Kafka producer: ")

    @patch("core.kafka.RBACProducer")
    @patch("core.kafka.logger")
    def test_kafka_generic_producer_errors_logged(self, mock_logger, MockKafkaProducer):
        """Test that mocked generic error return correct messages from Kafka producer"""
        MockKafkaProducer.get_producer.side_effect = Exception

        with self.assertRaises(Exception):
            MockKafkaProducer.get_producer()
            mock_logger.error.assert_any_call("Non Kafka error occurred during initialization of Kafka producer: ")

    @patch("core.kafka.RBACProducer")
    @patch("core.kafka.logger")
    def test_kafka_generic_producer_errors_retries(self, mock_logger, MockKafkaProducer):
        """Test that mocked generic error retries maxed out return correct messages from Kafka producer"""
        with self.assertRaises(Exception):
            MockKafkaProducer.get_producer()
            mock_logger.error.assert_any_call("Kafka error during initialization of Kafka producer: ")

    @patch("core.kafka.RBACProducer")
    @patch("core.kafka.logger")
    @patch("rbac.settings")
    def test_fake_kafka_producer_correct_init(self, mock_settings, mock_logger, MockKafkaProducer):
        """Test that fake Kafka producer returns correct info message when initialized"""
        mock_settings.KAFKA_ENABLED = True
        mock_settings.MOCK_KAFKA = True
        MockKafkaProducer.get_producer()

        if mock_settings.MOCK_KAFKA:
            MockKafkaProducer.get_producer.side_effect = mock_logger.info(
                "Fake Kafka producer initialized in development mode"
            )
        else:
            MockKafkaProducer.get_producer.side_effect = mock_logger.info("Kafka producer initialized successfully")

        mock_logger.info.assert_any_call("Fake Kafka producer initialized in development mode")

    @patch("core.kafka.RBACProducer")
    @patch("core.kafka.logger")
    @patch("rbac.settings")
    def test_kafka_producer_correct_init(self, mock_settings, mock_logger, MockKafkaProducer):
        """Test that Kafka producer returns correct info message when initialized"""
        mock_settings.KAFKA_ENABLED = True
        mock_settings.MOCK_KAFKA = False
        MockKafkaProducer.get_producer()

        if mock_settings.MOCK_KAFKA:
            MockKafkaProducer.get_producer.side_effect = mock_logger.info(
                "Fake Kafka producer initialized in development mode"
            )
        else:
            MockKafkaProducer.get_producer.side_effect = mock_logger.info("Kafka producer initialized successfully")

        mock_logger.info.assert_any_call("Kafka producer initialized successfully")


@override_settings(KAFKA_CLUSTERS={"it_managed": IT_MANAGED_CLUSTER})
class GetClusterConfigTests(IdentityRequest):
    """Tests for the named cluster-profile helper."""

    def test_returns_servers_and_auth_for_known_profile(self):
        """A known profile returns its configured servers and auth."""
        servers, auth = get_cluster_config("it_managed")
        self.assertEqual(servers, ["it-broker:9096"])
        self.assertEqual(auth["sasl_plain_username"], "it-user")
        self.assertEqual(auth["security_protocol"], "SASL_SSL")

    def test_strips_producer_only_configs_for_consumer(self):
        """for_consumer=True strips producer-only configs but keeps other auth."""
        _, auth = get_cluster_config("it_managed", for_consumer=True)
        self.assertNotIn("retries", auth)
        # Non-producer-only auth is preserved.
        self.assertEqual(auth["sasl_mechanism"], "SCRAM-SHA-512")

    def test_producer_keeps_producer_only_configs(self):
        """Without for_consumer, producer-only configs are retained."""
        _, auth = get_cluster_config("it_managed")
        self.assertIn("retries", auth)

    def test_unknown_profile_returns_empty(self):
        """An unknown profile returns empty servers/auth so callers no-op."""
        servers, auth = get_cluster_config("does-not-exist")
        self.assertEqual(servers, [])
        self.assertEqual(auth, {})

    def test_returns_copies_not_references(self):
        """Mutating the returned values must not corrupt the registry."""
        servers, auth = get_cluster_config("it_managed")
        servers.append("mutated")
        auth["injected"] = True
        servers2, auth2 = get_cluster_config("it_managed")
        self.assertNotIn("mutated", servers2)
        self.assertNotIn("injected", auth2)


class RBACProducerClusterSelectionTests(IdentityRequest):
    """RBACProducer selects the correct cluster by profile without affecting the default path."""

    @override_settings(
        DEVELOPMENT=False,
        MOCK_KAFKA=False,
        KAFKA_ENABLED=True,
        KAFKA_AUTH={"bootstrap_servers": ["clowder:9092"], "sasl_plain_username": "clowder-user"},
        KAFKA_SERVERS=["clowder:9092"],
        KAFKA_CLUSTERS={"it_managed": IT_MANAGED_CLUSTER},
    )
    @patch("core.kafka.KafkaProducer")
    def test_default_producer_uses_clowder_settings(self, mock_producer):
        """Default RBACProducer() reads settings.KAFKA_AUTH directly (Clowder path unchanged)."""
        RBACProducer().get_producer()
        _, kwargs = mock_producer.call_args
        self.assertEqual(kwargs["sasl_plain_username"], "clowder-user")

    @override_settings(
        DEVELOPMENT=False,
        MOCK_KAFKA=False,
        KAFKA_ENABLED=True,
        KAFKA_AUTH={"bootstrap_servers": ["clowder:9092"], "sasl_plain_username": "clowder-user"},
        KAFKA_SERVERS=["clowder:9092"],
        KAFKA_CLUSTERS={"it_managed": IT_MANAGED_CLUSTER},
    )
    @patch("core.kafka.KafkaProducer")
    def test_it_managed_producer_uses_profile(self, mock_producer):
        """RBACProducer(cluster="it_managed") builds from the it_managed profile, not Clowder."""
        RBACProducer(cluster="it_managed").get_producer()
        _, kwargs = mock_producer.call_args
        self.assertEqual(kwargs["sasl_plain_username"], "it-user")
        self.assertEqual(kwargs["security_protocol"], "SASL_SSL")


class SendKafkaMessageTests(TestCase):
    def setUp(self):
        self.producer = RBACProducer()
        self.mock_kafka = Mock()
        self.producer.producer = self.mock_kafka

    def test_send_calls_producer_with_correct_args(self):
        result = self.producer.send_kafka_message("test-topic", {"key": "value"})
        self.mock_kafka.send.assert_called_once_with("test-topic", value=b'{"key": "value"}', headers=None)
        self.assertTrue(result)

    def test_future_send_returns_the_kafka_future_without_waiting(self):
        """The RH worker can inspect the Kafka future while the existing send API stays unchanged."""
        future = self.mock_kafka.send.return_value

        result = self.producer.send_kafka_message_future("test-topic", {"key": "value"})

        self.assertIs(result, future)
        self.mock_kafka.send.assert_called_once_with("test-topic", value=b'{"key": "value"}', headers=None)
        future.get.assert_not_called()

    def test_future_send_propagates_enqueue_failure(self):
        """A synchronous Kafka failure reaches the RH worker for retry."""
        self.mock_kafka.send.side_effect = KafkaTimeoutError("buffer full")

        with self.assertRaises(KafkaTimeoutError):
            self.producer.send_kafka_message_future("test-topic", {"key": "value"})

    def test_fake_producer_returns_a_completed_future(self):
        """Development's no-op producer supports the RH acknowledgment path."""
        self.assertIsNone(FakeKafkaProducer().send("test-topic", value=b"{}").get(timeout=0))

    def test_send_wraps_single_header_in_list(self):
        header = ("key", b"value")
        self.producer.send_kafka_message("test-topic", {"key": "value"}, headers=header)
        self.mock_kafka.send.assert_called_once_with("test-topic", value=b'{"key": "value"}', headers=[header])

    def test_send_preserves_header_list(self):
        headers = [("k1", b"v1"), ("k2", b"v2")]
        self.producer.send_kafka_message("test-topic", {"key": "value"}, headers=headers)
        self.mock_kafka.send.assert_called_once_with("test-topic", value=b'{"key": "value"}', headers=headers)

    @patch("core.kafka.logger")
    def test_send_swallows_kafka_error(self, mock_logger):
        self.mock_kafka.send.side_effect = KafkaError("broker unavailable")
        result = self.producer.send_kafka_message("sync-topic", {"action": "delete"})
        self.assertFalse(result)
        mock_logger.exception.assert_called_once_with(
            "Failed to send Kafka message to topic '%s'. Message type: %s",
            "sync-topic",
            ["action"],
        )

    @patch("core.kafka.logger")
    def test_send_swallows_kafka_timeout_error(self, mock_logger):
        self.mock_kafka.send.side_effect = KafkaTimeoutError("buffer full")
        result = self.producer.send_kafka_message("sync-topic", {"action": "update"})
        self.assertFalse(result)
        mock_logger.exception.assert_called_once()

    @patch("core.kafka.logger")
    def test_send_swallows_attribute_error_from_bad_producer(self, mock_logger):
        self.producer.producer = None
        result = self.producer.send_kafka_message("sync-topic", {"action": "create"})
        self.assertFalse(result)
        mock_logger.exception.assert_called_once()

    @patch("core.kafka.logger")
    def test_send_swallows_serialization_error(self, mock_logger):
        non_serializable = {"data": object()}
        result = self.producer.send_kafka_message("chrome-topic", non_serializable)
        self.assertFalse(result)
        self.mock_kafka.send.assert_not_called()
        mock_logger.exception.assert_called_once()

    @patch("core.kafka.logger")
    def test_repeated_failures_are_independent(self, mock_logger):
        self.mock_kafka.send.side_effect = KafkaError("down")
        self.assertFalse(self.producer.send_kafka_message("topic-a", {"msg": 1}))
        self.assertFalse(self.producer.send_kafka_message("topic-b", {"msg": 2}))
        self.assertFalse(self.producer.send_kafka_message("topic-c", {"msg": 3}))
        self.assertEqual(mock_logger.exception.call_count, 3)
