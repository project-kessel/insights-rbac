#!/bin/bash

# Test script for RBAC Kafka Consumer
# Usage: ./test_kafka_consumer.sh [topic_name] [interactive|background]

# Detect container runtime (docker or podman)
if command -v docker &> /dev/null && docker info &> /dev/null; then
    CONTAINER_RUNTIME="docker"
elif command -v podman &> /dev/null; then
    CONTAINER_RUNTIME="podman"
else
    CONTAINER_RUNTIME="docker"  # fallback default
fi

TOPIC=${1:-"outbox.event.relations-replication-event"}
MODE=${2:-"interactive"}

echo "Testing RBAC Kafka consumer with topic: $TOPIC in $MODE mode"

if [ "$MODE" = "background" ]; then
    # Start consumer in background (detached)
    echo "Starting RBAC Kafka consumer in background..."
    CONTAINER_ID=$($CONTAINER_RUNTIME run -d \
      --network rbac-network \
      -e KAFKA_ENABLED=true \
      -e RBAC_KAFKA_CUSTOM_CONSUMER_BROKER=kafka:9092 \
      -e RBAC_KAFKA_CONSUMER_TOPIC="$TOPIC" \
      -e RBAC_KAFKA_CONSUMER_GROUP_ID=rbac-consumer-group \
      -e METRICS_PORT=9000 \
      --name rbac-kafka-consumer-test \
      --rm \
      rbac_server python rbac/manage.py launch-rbac-kafka-consumer)

    echo "Consumer started with container ID: $CONTAINER_ID"
    echo "Check logs with: $CONTAINER_RUNTIME logs rbac-kafka-consumer-test -f"
    echo "To stop consumer: docker stop rbac-kafka-consumer-test"
else
    # Start consumer in interactive mode
    echo "Starting RBAC Kafka consumer in interactive mode..."
    echo "Press Ctrl+C to stop the consumer"
    $CONTAINER_RUNTIME run -it \
      --network rbac-network \
      -e KAFKA_ENABLED=true \
      -e RBAC_KAFKA_CUSTOM_CONSUMER_BROKER=kafka:9092 \
      -e RBAC_KAFKA_CONSUMER_TOPIC="$TOPIC" \
      -e RBAC_KAFKA_CONSUMER_GROUP_ID=rbac-consumer-group \
      -e METRICS_PORT=9000 \
      --rm \
      rbac_server python rbac/manage.py launch-rbac-kafka-consumer
fi
