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
"""Kafka utilities for disaster recovery -- read events by timestamp range."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from kafka import KafkaConsumer, TopicPartition
from kafka.structs import OffsetAndTimestamp

logger = logging.getLogger(__name__)

DR_CONSUMER_GROUP = "rbac-dr-recovery"


def _unwrap_schema_envelope(value: dict[str, Any]) -> dict[str, Any] | Any:
    """Unwrap Kafka JsonConverter schema envelope if present.

    When Kafka Connect uses JsonConverter with schemas.enable=true and
    Debezium's table.expand.json.payload=false, the message looks like:
        {"schema": {...}, "payload": "<json-string>"}
    This extracts and parses the inner payload.
    """
    if "schema" in value and "payload" in value and len(value) == 2:
        inner = value["payload"]
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except (json.JSONDecodeError, TypeError):
                return value
        if isinstance(inner, dict):
            return inner
    return value


@dataclass
class KafkaEvent:
    """A Kafka event with its timestamp and parsed value."""

    topic: str
    partition: int
    offset: int
    timestamp_ms: int
    value: dict[str, Any]


def _create_consumer(group_id: str = DR_CONSUMER_GROUP) -> KafkaConsumer:
    """Create a KafkaConsumer with appropriate auth settings."""
    kwargs: dict[str, Any] = {
        "group_id": group_id,
        "enable_auto_commit": False,
        "auto_offset_reset": "earliest",
        "consumer_timeout_ms": getattr(settings, "DR_KAFKA_CONSUMER_TIMEOUT_MS", 30000),
        "value_deserializer": lambda m: json.loads(m.decode("utf-8")),
    }

    kafka_auth = getattr(settings, "KAFKA_AUTH", None)
    if kafka_auth:
        for key in (
            "bootstrap_servers",
            "sasl_plain_username",
            "sasl_plain_password",
            "sasl_mechanism",
            "security_protocol",
            "ssl_cafile",
        ):
            if key in kafka_auth:
                kwargs[key] = kafka_auth[key]
    elif getattr(settings, "KAFKA_SERVERS", None):
        kwargs["bootstrap_servers"] = settings.KAFKA_SERVERS
    else:
        raise RuntimeError("Kafka is not configured (no KAFKA_AUTH or KAFKA_SERVERS)")

    return KafkaConsumer(**kwargs)


def read_events_by_timestamp(
    topic: str,
    start_timestamp_ms: int,
    end_timestamp_ms: int,
    event_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> list[KafkaEvent]:
    """Read Kafka events from a topic within a timestamp range.

    Args:
        topic: The Kafka topic to read from.
        start_timestamp_ms: Start of the time window (inclusive), epoch milliseconds.
        end_timestamp_ms: End of the time window (inclusive), epoch milliseconds.
        event_filter: Optional callable to filter events. If provided, only events
            for which this returns True are included.

    Returns:
        List of KafkaEvent objects within the timestamp range, ordered by partition and offset.
    """
    consumer = _create_consumer()
    events: list[KafkaEvent] = []

    try:
        partitions = consumer.partitions_for_topic(topic)
        if not partitions:
            logger.warning("No partitions found for topic %s", topic)
            return events

        topic_partitions = [TopicPartition(topic, p) for p in partitions]
        consumer.assign(topic_partitions)

        start_offsets: dict[TopicPartition, OffsetAndTimestamp | None] = consumer.offsets_for_times(
            {tp: start_timestamp_ms for tp in topic_partitions}
        )

        end_offsets: dict[TopicPartition, OffsetAndTimestamp | None] = consumer.offsets_for_times(
            {tp: end_timestamp_ms for tp in topic_partitions}
        )

        active_partitions: list[TopicPartition] = []
        partition_end_offsets: dict[TopicPartition, int] = {}

        for tp in topic_partitions:
            offset_info = start_offsets.get(tp)
            if offset_info is None:
                consumer.pause(tp)
                continue
            consumer.seek(tp, offset_info.offset)
            active_partitions.append(tp)

            end_info = end_offsets.get(tp)
            if end_info is not None:
                partition_end_offsets[tp] = end_info.offset

        if not active_partitions:
            logger.info("No partitions have messages in the requested time window for topic %s", topic)
            return events

        finished_partitions: set[TopicPartition] = set()

        for message in consumer:
            tp = TopicPartition(message.topic, message.partition)

            if tp in finished_partitions:
                continue

            end_offset = partition_end_offsets.get(tp)
            if end_offset is not None and message.offset >= end_offset:
                finished_partitions.add(tp)
                consumer.pause(tp)
                if len(finished_partitions) == len(active_partitions):
                    break
                continue

            if message.timestamp > end_timestamp_ms:
                if tp not in partition_end_offsets:
                    finished_partitions.add(tp)
                    consumer.pause(tp)
                    if len(finished_partitions) == len(active_partitions):
                        break
                continue

            if message.timestamp < start_timestamp_ms:
                continue

            try:
                value = message.value
                if not isinstance(value, dict):
                    continue

                value = _unwrap_schema_envelope(value)
                if not isinstance(value, dict):
                    continue

                if event_filter and not event_filter(value):
                    continue

                events.append(
                    KafkaEvent(
                        topic=message.topic,
                        partition=message.partition,
                        offset=message.offset,
                        timestamp_ms=message.timestamp,
                        value=value,
                    )
                )
            except Exception:
                logger.exception(
                    "Error processing Kafka message at offset %d partition %d", message.offset, message.partition
                )

        partition_stats: dict[int, list[int]] = {}
        for ev in events:
            partition_stats.setdefault(ev.partition, []).append(ev.offset)
        for part, offsets in sorted(partition_stats.items()):
            logger.info(
                "Partition %d: %d events, offsets [%d, %d]",
                part,
                len(offsets),
                min(offsets),
                max(offsets),
            )
        if not events:
            logger.info(
                "No events matched in topic %s for window [%d, %d]", topic, start_timestamp_ms, end_timestamp_ms
            )

    finally:
        consumer.close()

    return events
