#!/bin/bash

# Script to run RBAC Kafka consumer in existing rbac_server container
# Usage: ./run_kafka_consumer.sh [topic_name]

# Detect container runtime (docker or podman)
if command -v docker &> /dev/null && docker info &> /dev/null; then
    CONTAINER_RUNTIME="docker"
elif command -v podman &> /dev/null; then
    CONTAINER_RUNTIME="podman"
else
    CONTAINER_RUNTIME="docker"  # fallback default
fi

TOPIC=${1:-"outbox.event.relations-replication-event"}

echo "Starting RBAC Kafka consumer in existing rbac_server container..."
echo "Topic: $TOPIC"
echo "Press Ctrl+C to stop the consumer"

# Run consumer in existing rbac_server container
$CONTAINER_RUNTIME exec -it \
  -e KAFKA_ENABLED=true \
  -e RBAC_KAFKA_CUSTOM_CONSUMER_BROKER=kafka:9092 \
  -e RBAC_KAFKA_CONSUMER_TOPIC="$TOPIC" \
  -e RBAC_KAFKA_CONSUMER_GROUP_ID=rbac-consumer-group \
  -e METRICS_PORT=9000 \
  rbac_server python rbac/manage.py launch-rbac-kafka-consumer
