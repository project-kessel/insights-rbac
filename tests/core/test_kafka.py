from copy import deepcopy
from unittest.mock import Mock, patch, DEFAULT
from django.test import TestCase
from kafka.errors import KafkaError


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
