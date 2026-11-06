#
# Copyright 2022 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Producer to send messages to kafka server."""

import json
import logging

from django.conf import settings
from kafka import KafkaProducer
from kafka.errors import KafkaError

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

# Producer-only client configs that KafkaConsumer rejects. settings.KAFKA_AUTH is shared
# between producers and consumers, so these must be filtered out before building a consumer
# to avoid KafkaConfigurationError (e.g. "Unrecognized configs: {'retries'}").
PRODUCER_ONLY_CONFIGS = frozenset(
    {
        "retries",
        "max_in_flight_requests_per_connection",
        "acks",
        "enable_idempotence",
        "transactional_id",
        "transaction_timeout_ms",
        "compression_type",
        "batch_size",
        "linger_ms",
        "buffer_memory",
        "max_block_ms",
        "delivery_timeout_ms",
    }
)


def get_cluster_config(profile, *, for_consumer=False):
    """Return (servers, auth) for a named Kafka cluster profile from settings.KAFKA_CLUSTERS.

    Used by IT-managed Kafka paths (e.g. principal cleanup) to select a cluster other than the
    default Clowder one. Unknown or unconfigured profiles return ([], {}) so callers safely no-op
    instead of falling back to a different cluster. When ``for_consumer`` is set, producer-only
    client configs (see PRODUCER_ONLY_CONFIGS) are stripped so the dict can be passed to a
    KafkaConsumer without raising KafkaConfigurationError.
    """
    cluster = settings.KAFKA_CLUSTERS.get(profile, {})
    servers = list(cluster.get("servers") or [])
    auth = dict(cluster.get("auth") or {})
    if for_consumer:
        auth = {key: value for key, value in auth.items() if key not in PRODUCER_ONLY_CONFIGS}
    return servers, auth


class FakeKafkaProducer:
    """Fake kafka producer to enable local development without kafka server."""

    def send(self, topic, value=None, headers=None):
        """No operation method."""
        pass

    def flush(self, timeout=None):
        """No operation method."""
        pass


class RBACProducer:
    """Kafka message producer to emit events to notification service."""

    def __init__(self, cluster="clowder"):
        """Select the Kafka cluster this producer targets.

        Defaults to "clowder", which preserves the existing behavior of reading
        settings.KAFKA_AUTH/KAFKA_SERVERS directly; existing callers pass no argument and are
        unaffected. Any other profile name is resolved through settings.KAFKA_CLUSTERS via
        get_cluster_config() (e.g. cluster="it_managed" for the principal-cleanup DLQ).
        """
        self._cluster = cluster

    def get_producer(self):
        """Init method to return fake kafka when flag is set to false."""
        if not hasattr(self, "producer"):
            retries = 0
            max_retries = 5
            if settings.DEVELOPMENT or settings.MOCK_KAFKA or not settings.KAFKA_ENABLED:
                self.producer = FakeKafkaProducer()
                logger.info("Fake Kafka producer initialized in development mode")
            else:
                # Default "clowder" keeps reading settings.KAFKA_AUTH/KAFKA_SERVERS directly so the
                # existing Clowder producers are byte-for-byte unchanged; other profiles resolve
                # through the cluster registry.
                if self._cluster == "clowder":
                    kafka_auth = settings.KAFKA_AUTH
                    kafka_servers = settings.KAFKA_SERVERS
                else:
                    kafka_servers, kafka_auth = get_cluster_config(self._cluster)
                while retries <= max_retries:
                    try:
                        if kafka_auth:
                            self.producer = KafkaProducer(
                                **kafka_auth,
                                enable_idempotence=True,  # Deduplicate producer retries (v3 default)
                                acks="all",  # Wait for all in-sync replicas (v3 default)
                            )
                            logger.info("Kafka producer initialized successfully")
                            return self.producer
                        elif not kafka_servers:
                            raise AttributeError("Empty servers list")
                        else:
                            self.producer = KafkaProducer(
                                bootstrap_servers=kafka_servers,
                                enable_idempotence=True,  # Deduplicate producer retries (v3 default)
                                acks="all",  # Wait for all in-sync replicas (v3 default)
                            )
                            return self.producer
                    except KafkaError as e:
                        logger.error(f"Kafka error during initialization of Kafka producer: {e}")
                        retries += 1
                    except Exception as e:
                        logger.error(f"Non Kafka error occurred during initialization of Kafka producer: {e}")
                        retries += 1
        return self.producer

    def send_kafka_message(self, topic, message, headers=None) -> bool:
        """Send message to kafka server.

        Returns True if sent successfully, False if an error occurred (error is logged).
        """
        try:
            producer = self.get_producer()
            json_data = json.dumps(message).encode("utf-8")
            if headers and not isinstance(headers, list):
                headers = [headers]
            producer.send(topic, value=json_data, headers=headers)
            return True
        except (KafkaError, TypeError, ValueError, AttributeError):
            logger.exception(
                "Failed to send Kafka message to topic '%s'. Message type: %s",
                topic,
                list(message.keys()) if isinstance(message, dict) else type(message).__name__,
            )
            return False


"""
This consumer could be used for local testing.
def consume_message():
    from kafka import KafkaConsumer
    consumer = KafkaConsumer(notification_topic,
            bootstrap_servers=[settings.KAFKA_SERVER],
        )
    for message in consumer:
        deserialized_data = pickle.loads(message.value)
        print(json.dumps(deserialized_data, indent=4, sort_keys=True)
"""
