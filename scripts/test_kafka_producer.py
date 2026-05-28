#!/usr/bin/env python
"""
Test script to send Kafka messages for testing the principal cleanup feature.
Usage: python scripts/test_kafka_producer.py
"""

import json
from kafka import KafkaProducer

# Sample test message - inactive user
SAMPLE_MESSAGE = {
    "CanonicalMessage": {
        "Header": {
            "System": "WEB",
            "Operation": "update",
            "Type": "User",
            "InstanceId": "test-instance-123",
            "Timestamp": "2025-03-26T12:00:00.000",
        },
        "Payload": {
            "Sync": {
                "User": {
                    "CreatedDate": "2025-01-01T00:00:00.000",
                    "LastUpdatedDate": "2025-03-26T12:00:00.000",
                    "Identifiers": {
                        "Identifier": [
                            {"system": "WEB", "entity-name": "User", "qualifier": "id", "text": "12345678"}
                        ],
                        "Reference": [
                            {"system": "WEB", "entity-name": "Customer", "qualifier": "id", "text": "99999999"},
                            {"system": "EBS", "entity-name": "Account", "qualifier": "number", "text": "88888888"},
                        ],
                    },
                    "Status": {"State": "Inactive"},  # This will trigger cleanup
                    "Person": {"FirstName": "Test", "LastName": "User", "Credentials": {"Login": "test-user"}},
                }
            }
        },
    }
}


def send_test_message(bootstrap_servers="localhost:9092", topic="VirtualTopic.canonical.user"):
    """Send a test message to Kafka."""
    print(f"Connecting to Kafka at {bootstrap_servers}...")

    producer = KafkaProducer(
        bootstrap_servers=bootstrap_servers, value_serializer=lambda v: json.dumps(v).encode("utf-8")
    )

    print(f"Sending test message to topic: {topic}")
    future = producer.send(topic, value=SAMPLE_MESSAGE)

    # Wait for the message to be sent
    try:
        record_metadata = future.get(timeout=10)
        print(f"✓ Message sent successfully!")
        print(f"  Topic: {record_metadata.topic}")
        print(f"  Partition: {record_metadata.partition}")
        print(f"  Offset: {record_metadata.offset}")
    except Exception as e:
        print(f"✗ Failed to send message: {e}")

    producer.close()
    print("\nMessage content:")
    print(json.dumps(SAMPLE_MESSAGE, indent=2))


if __name__ == "__main__":
    import sys

    # Allow override of bootstrap servers from command line
    bootstrap_servers = sys.argv[1] if len(sys.argv) > 1 else "localhost:9092"
    topic = sys.argv[2] if len(sys.argv) > 2 else "VirtualTopic.canonical.user"

    send_test_message(bootstrap_servers, topic)
