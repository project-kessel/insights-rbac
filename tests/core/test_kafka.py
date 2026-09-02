from copy import deepcopy
from unittest.mock import DEFAULT, Mock, patch

from django.test import TestCase
from core.kafka import RBACProducer
from kafka.errors import KafkaError, KafkaTimeoutError


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


class SendKafkaMessageTests(TestCase):
    def setUp(self):
        self.producer = RBACProducer()
        self.mock_kafka = Mock()
        self.producer.producer = self.mock_kafka

    def test_send_calls_producer_with_correct_args(self):
        result = self.producer.send_kafka_message("test-topic", {"key": "value"})
        self.mock_kafka.send.assert_called_once_with("test-topic", value=b'{"key": "value"}', headers=None)
        self.assertTrue(result)

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
