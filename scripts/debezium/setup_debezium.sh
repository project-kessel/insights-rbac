#!/bin/bash

# Debezium Setup Script for RBAC
# This script sets up the complete Debezium Change Data Capture pipeline

set -e  # Exit on any error

# Detect container runtime (docker or podman)
if command -v docker &> /dev/null && docker info &> /dev/null; then
    CONTAINER_RUNTIME="docker"
    COMPOSE_CMD="docker-compose"
elif command -v podman &> /dev/null; then
    CONTAINER_RUNTIME="podman"
    COMPOSE_CMD="podman compose"
else
    echo "Error: Neither Docker nor Podman is available or running"
    exit 1
fi

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to wait for service to be ready
wait_for_service() {
    local url=$1
    local service_name=$2
    local max_attempts=60
    local attempt=1

    print_status "Waiting for $service_name to be ready..."

    while [ $attempt -le $max_attempts ]; do
        if curl -s -f "$url" > /dev/null 2>&1; then
            print_success "$service_name is ready!"
            return 0
        fi

        echo -n "."
        sleep 2
        attempt=$((attempt + 1))
    done

    print_error "$service_name failed to start within $((max_attempts * 2)) seconds"
    return 1
}

# Function to check if container runtime is running
check_container_runtime() {
    print_success "Using $CONTAINER_RUNTIME as container runtime"
}

# Function to create container network
create_network() {
    print_status "Creating container network 'rbac-network'..."

    if $CONTAINER_RUNTIME network ls | grep -q rbac-network; then
        print_warning "Network 'rbac-network' already exists"
    else
        $CONTAINER_RUNTIME network create rbac-network
        print_success "Created container network 'rbac-network'"
    fi
}

# Function to find available port
find_available_port() {
    local start_port=$1
    local max_attempts=10
    local port=$start_port

    for ((i=0; i<$max_attempts; i++)); do
        if ! port_is_in_use $port; then
            echo $port
            return 0
        fi
        port=$((port + 1))
    done

    echo ""
    return 1
}

# Function to check if port is in use
port_is_in_use() {
    local port=$1

    if command -v ss &> /dev/null; then
        ss -tuln | grep -q ":$port " && return 0
    elif command -v netstat &> /dev/null; then
        (netstat -tuln 2>/dev/null | grep -q ":$port " || netstat -ano 2>/dev/null | grep -q ":$port.*LISTENING") && return 0
    elif command -v lsof &> /dev/null; then
        lsof -i ":$port" &> /dev/null && return 0
    fi

    return 1
}

# Function to check and configure port
check_and_configure_port() {
    local port=$1
    local service_name=$2
    local env_var=$3

    if port_is_in_use $port; then
        print_warning "Port $port is already in use (required for $service_name)" >&2

        # Find an available port
        local new_port=$(find_available_port $((port + 1)))

        if [ -z "$new_port" ]; then
            print_error "Could not find an available port for $service_name" >&2
            exit 1
        fi

        print_status "Using alternate port $new_port for $service_name" >&2
        export $env_var=$new_port
        echo "$new_port"
    else
        export $env_var=$port
        echo "$port"
    fi
}

# Function to check all required ports
check_required_ports() {
    print_status "Checking if required ports are available..."

    # Check and configure Redis port (6379 -> REDIS_PORT)
    local redis_port=$(check_and_configure_port 6379 "Redis" "REDIS_PORT")
    REDIS_PORT=$redis_port
    if [ "$REDIS_PORT" != "6379" ]; then
        print_status "Redis will use port $REDIS_PORT instead of 6379"
    fi

    # Check PostgreSQL port (15432)
    if port_is_in_use 15432; then
        print_warning "Port 15432 is already in use (required for PostgreSQL)"
        print_error "PostgreSQL port conflict. Please free port 15432 or modify docker-compose.yml"
        exit 1
    fi

    # Check Kafka port (9092)
    if port_is_in_use 9092; then
        print_warning "Port 9092 is already in use (required for Kafka)"
        print_error "Kafka port conflict. Please free port 9092 or modify docker-compose.debezium.yml"
        exit 1
    fi

    # Check Kafka Connect port (8083)
    if port_is_in_use 8083; then
        print_warning "Port 8083 is already in use (required for Kafka Connect)"
        print_error "Kafka Connect port conflict. Please free port 8083 or modify docker-compose.debezium.yml"
        exit 1
    fi

    # Check Kafdrop port (9001)
    if port_is_in_use 9001; then
        print_warning "Port 9001 is already in use (required for Kafdrop)"
        print_error "Kafdrop port conflict. Please free port 9001 or modify docker-compose.debezium.yml"
        exit 1
    fi

    print_success "Port check completed"
}

# Function to start PostgreSQL database
start_database() {
    print_status "Starting PostgreSQL database..."

    # Check if database is already running
    if $CONTAINER_RUNTIME ps | grep -q rbac_db; then
        print_warning "PostgreSQL database is already running"
    else
        $COMPOSE_CMD up -d db
        print_success "PostgreSQL database started"
    fi

    # Wait for database to be ready
    print_status "Waiting for PostgreSQL to be ready..."
    while ! $CONTAINER_RUNTIME exec rbac_db pg_isready -U postgres > /dev/null 2>&1; do
        echo -n "."
        sleep 2
    done
    print_success "PostgreSQL is ready"
}

# Function to run database migrations
run_migrations() {
    print_status "Running database migrations..."

    # Check if rbac_server container exists and is running
    if ! $CONTAINER_RUNTIME ps | grep -q rbac_server; then
        print_status "Starting rbac_server container..."

        # Start redis container separately with custom port if needed
        if [ -n "${REDIS_PORT}" ] && [ "${REDIS_PORT}" != "6379" ]; then
            if ! $CONTAINER_RUNTIME ps | grep -q rbac_redis; then
                print_status "Starting Redis on port ${REDIS_PORT}..."
                # Create Redis with alias so other containers can reach it
                $CONTAINER_RUNTIME run -d \
                    --name rbac_redis \
                    --network rbac-network \
                    --network-alias redis \
                    -p ${REDIS_PORT}:6379 \
                    --health-cmd "redis-cli ping | grep PONG" \
                    --health-interval 1s \
                    --health-timeout 3s \
                    --health-retries 5 \
                    redis:5.0.4
                sleep 3

                # Wait for Redis to be healthy
                print_status "Waiting for Redis to be ready..."
                while ! $CONTAINER_RUNTIME exec rbac_redis redis-cli ping | grep -q PONG; do
                    echo -n "."
                    sleep 1
                done
                print_success "Redis is ready on port ${REDIS_PORT}"
            fi
        fi

        # Set required environment variables for RBAC
        export WORKSPACE_HIERARCHY_DEPTH_LIMIT=5
        export WORKSPACE_ORG_CREATION_LIMIT=20000000
        export V2_APIS_ENABLED="True"
        export V2_READ_ONLY_API_MODE="False"
        export V2_BOOTSTRAP_TENANT="True"

        # Start all services, scaling redis to 0 if we started it manually
        if [ -n "${REDIS_PORT}" ] && [ "${REDIS_PORT}" != "6379" ]; then
            WORKSPACE_HIERARCHY_DEPTH_LIMIT=5 \
            WORKSPACE_ORG_CREATION_LIMIT=20000000 \
            V2_APIS_ENABLED="True" \
            V2_READ_ONLY_API_MODE="False" \
            V2_BOOTSTRAP_TENANT="True" \
            $COMPOSE_CMD up -d --scale redis=0 rbac-server
        else
            WORKSPACE_HIERARCHY_DEPTH_LIMIT=5 \
            WORKSPACE_ORG_CREATION_LIMIT=20000000 \
            V2_APIS_ENABLED="True" \
            V2_READ_ONLY_API_MODE="False" \
            V2_BOOTSTRAP_TENANT="True" \
            $COMPOSE_CMD up -d rbac-server
        fi
        sleep 5
    fi

    # Run migrations
    print_status "Executing migrations in rbac_server container..."
    if $CONTAINER_RUNTIME exec -e DJANGO_LOG_HANDLERS=console -e DJANGO_LOG_FILE=/tmp/app.log rbac_server python rbac/manage.py migrate; then
        print_success "Database migrations completed"
    else
        print_error "Database migrations failed"
        print_status "Checking migration status..."
        $CONTAINER_RUNTIME exec -e DJANGO_LOG_HANDLERS=console -e DJANGO_LOG_FILE=/tmp/app.log rbac_server python rbac/manage.py showmigrations || true
        exit 1
    fi
}

# Function to clean up existing Debezium artifacts
cleanup_debezium_artifacts() {
    print_status "Cleaning up existing Debezium artifacts..."

    # List all replication slots
    local slots=$($CONTAINER_RUNTIME exec rbac_db psql -U postgres -t -c \
        "SELECT slot_name FROM pg_replication_slots;" 2>/dev/null | xargs)

    if [ -n "$slots" ]; then
        print_status "Found replication slots: $slots"
        # Drop all replication slots
        for slot in $slots; do
            print_status "Dropping replication slot: $slot"
            $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c \
                "SELECT pg_drop_replication_slot('$slot');" 2>/dev/null || \
            $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c \
                "SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots WHERE slot_name = '$slot' AND active = true;" 2>/dev/null || true
            # Try again after terminating
            $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c \
                "SELECT pg_drop_replication_slot('$slot');" 2>/dev/null || true
        done
    fi

    # Drop existing publication if exists
    $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c \
        "DROP PUBLICATION IF EXISTS dbz_publication;" > /dev/null 2>&1 || true

    # Verify cleanup
    local remaining_slots=$($CONTAINER_RUNTIME exec rbac_db psql -U postgres -t -c \
        "SELECT COUNT(*) FROM pg_replication_slots;" 2>/dev/null | xargs)

    if [ "$remaining_slots" = "0" ]; then
        print_success "All replication slots cleaned up"
    else
        print_warning "Some replication slots remain: $remaining_slots"
    fi
}

# Function to configure PostgreSQL for logical replication
configure_postgres_replication() {
    print_status "Configuring PostgreSQL for logical replication..."

    # Set required parameters for logical replication
    $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c "ALTER SYSTEM SET wal_level = logical;" > /dev/null
    $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c "ALTER SYSTEM SET max_replication_slots = 4;" > /dev/null
    $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c "ALTER SYSTEM SET max_wal_senders = 4;" > /dev/null

    print_success "PostgreSQL replication configuration updated"

    # Restart PostgreSQL to apply changes
    print_status "Restarting PostgreSQL to apply replication settings..."
    $COMPOSE_CMD restart db > /dev/null 2>&1

    # Wait for database to be ready again
    while ! $CONTAINER_RUNTIME exec rbac_db pg_isready -U postgres > /dev/null 2>&1; do
        echo -n "."
        sleep 2
    done

    # Verify configuration
    wal_level=$($CONTAINER_RUNTIME exec rbac_db psql -U postgres -t -c "SHOW wal_level;" | xargs)
    if [ "$wal_level" = "logical" ]; then
        print_success "PostgreSQL logical replication configured successfully"
    else
        print_error "Failed to configure PostgreSQL logical replication"
        exit 1
    fi

    # Clean up any existing Debezium artifacts from previous runs
    cleanup_debezium_artifacts
}

# Function to start Debezium services
start_debezium_services() {
    print_status "Starting Debezium services (Kafka, Zookeeper, Kafka Connect, Kafdrop)..."

    # Start all Debezium services
    $COMPOSE_CMD -f docker-compose.debezium.yml up -d

    print_success "Debezium services started"

    # Wait for Kafka Connect to be ready
    wait_for_service "http://localhost:8083/connectors" "Kafka Connect"
}

# Function to create RBAC replication topic
create_rbac_topic() {
    print_status "Creating RBAC replication topic..."

    local topic_name="outbox.event.relations-replication-event"

    # Check if topic already exists
    if $CONTAINER_RUNTIME exec insights-rbac-kafka-1 kafka-topics --bootstrap-server localhost:9092 --list | grep -q "$topic_name"; then
        print_warning "Topic '$topic_name' already exists"
    else
        # Create the topic
        $CONTAINER_RUNTIME exec insights-rbac-kafka-1 kafka-topics --bootstrap-server localhost:9092 --create \
            --topic "$topic_name" \
            --partitions 3 \
            --replication-factor 1
        print_success "Created topic '$topic_name'"
    fi
}

# Function to create consumer group for RBAC consumer
create_consumer_group() {
    print_status "Creating consumer group for RBAC Kafka consumer..."

    local topic_name="outbox.event.relations-replication-event"
    local group_id="rbac-consumer-group"

    # Create a dummy consumer to initialize the consumer group and set offset to beginning
    print_status "Initializing consumer group '$group_id' and setting offset to beginning..."

    # Start a temporary consumer that will read from beginning and exit quickly
    $CONTAINER_RUNTIME exec insights-rbac-kafka-1 timeout 5s kafka-console-consumer \
        --bootstrap-server kafka:9092 \
        --topic "$topic_name" \
        --group "$group_id" \
        --from-beginning \
        --timeout-ms 2000 > /dev/null 2>&1 || true

    # Reset consumer group offset to earliest (beginning of topic)
    $CONTAINER_RUNTIME exec insights-rbac-kafka-1 kafka-consumer-groups \
        --bootstrap-server kafka:9092 \
        --group "$group_id" \
        --topic "$topic_name" \
        --reset-offsets \
        --to-earliest \
        --execute > /dev/null 2>&1 || true

    print_success "Consumer group '$group_id' created and configured to read from beginning"
}

# Function to create Debezium connector
create_debezium_connector() {
    print_status "Creating Debezium PostgreSQL connector..."

    # Check if connector already exists
    if curl -s http://localhost:8083/connectors | grep -q "rbac-postgres-connector"; then
        print_warning "Connector 'rbac-postgres-connector' already exists"

        # Check if connector or its tasks are in a failed state
        local status=$(curl -s http://localhost:8083/connectors/rbac-postgres-connector/status)
        if echo "$status" | grep -q '"state":"FAILED"'; then
            print_warning "Existing connector or task has failed, deleting and recreating..."

            # Delete the connector
            curl -s -X DELETE http://localhost:8083/connectors/rbac-postgres-connector > /dev/null
            sleep 3

            # Clean up Debezium artifacts in PostgreSQL
            cleanup_debezium_artifacts
            sleep 2
            # Continue to create new connector
        else
            print_success "Connector is already running"
            return 0
        fi
    fi

    # Check if connector config file exists
    local connector_config="scripts/debezium/debezium-connector-config.json"
    if [ ! -f "$connector_config" ]; then
        print_error "Connector configuration file not found: $connector_config"
        exit 1
    fi

    # Create the connector using the configuration file
    local response=$(curl -s -X POST http://localhost:8083/connectors \
        -H "Content-Type: application/json" \
        -d @"$connector_config")

    if echo "$response" | grep -q '"name":"rbac-postgres-connector"'; then
        print_success "Debezium connector created successfully"
    else
        print_error "Failed to create Debezium connector"
        echo "Response: $response"
        exit 1
    fi
}

# Function to create mock replication data producer
create_mock_replication_producer() {
    print_status "Creating mock replication data for testing..."

    local topic_name="outbox.event.relations-replication-event"

    # Create sample relation replication messages in the format expected by RBAC consumer
    cat > /tmp/test_relations_message_1.json << EOF
{
  "schema": {
    "type": "struct",
    "fields": [
      {
        "type": "string",
        "optional": false,
        "field": "payload"
      }
    ],
    "optional": false,
    "name": "test.relations.message"
  },
  "payload": "{\"relations_to_add\": [{\"resource\": {\"type\": \"role\", \"id\": \"test-role-$(date +%s)\"}, \"relation\": \"member\", \"subject\": {\"type\": \"user\", \"id\": \"test-user-$(date +%s)\"}}], \"relations_to_remove\": []}"
}
EOF

    cat > /tmp/test_relations_message_2.json << EOF
{
  "schema": {
    "type": "struct",
    "fields": [
      {
        "type": "string",
        "optional": false,
        "field": "payload"
      }
    ],
    "optional": false,
    "name": "test.relations.message"
  },
  "payload": "{\"relations_to_add\": [], \"relations_to_remove\": [{\"resource\": {\"type\": \"group\", \"id\": \"test-group-$(date +%s)\"}, \"relation\": \"admin\", \"subject\": {\"type\": \"user\", \"id\": \"test-admin-$(date +%s)\"}}]}"
}
EOF

    # Send test messages to the topic
    $CONTAINER_RUNTIME exec -i insights-rbac-kafka-1 kafka-console-producer \
        --bootstrap-server localhost:9092 \
        --topic "$topic_name" < /tmp/test_relations_message_1.json > /dev/null 2>&1 || true

    $CONTAINER_RUNTIME exec -i insights-rbac-kafka-1 kafka-console-producer \
        --bootstrap-server localhost:9092 \
        --topic "$topic_name" < /tmp/test_relations_message_2.json > /dev/null 2>&1 || true

    # Clean up temp files
    rm -f /tmp/test_relations_message_*.json

    print_success "Mock replication messages sent to topic '$topic_name'"
}

# Function to verify outbox table structure
verify_outbox_table() {
    print_status "Verifying outbox table structure..."

    # Check if management_outbox table exists
    local table_exists=$($CONTAINER_RUNTIME exec rbac_db psql -U postgres -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'management_outbox';" | xargs)
    if [ "$table_exists" = "1" ]; then
        print_success "management_outbox table exists"
    else
        print_error "management_outbox table not found"
        echo "Available tables:"
        $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE '%outbox%';"
        exit 1
    fi

    # Verify table has required columns
    local type_column=$($CONTAINER_RUNTIME exec rbac_db psql -U postgres -t -c "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'management_outbox' AND column_name = 'type';" | xargs)
    if [ "$type_column" = "1" ]; then
        print_success "Required 'type' column found in outbox table"
    else
        print_error "Missing 'type' column in outbox table"
        echo "Table structure:"
        $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c "\d management_outbox"
        exit 1
    fi
}

# Function to verify setup
verify_setup() {
    print_status "Verifying Debezium setup..."

    # Verify outbox table structure first
    verify_outbox_table

    # Wait a bit more for connector to fully initialize
    sleep 10

    # Check connector status
    local status=$(curl -s http://localhost:8083/connectors/rbac-postgres-connector/status)
    if echo "$status" | grep -q '"state":"RUNNING"'; then
        print_success "Connector is running"

        # Check task status
        if echo "$status" | grep -q '"state":"FAILED"'; then
            print_error "Connector task has failed"
            echo "Status: $status"
            print_status "Checking connector logs for errors..."
            $CONTAINER_RUNTIME logs insights-rbac-kafka-connect-1 --tail 20
            exit 1
        fi
    else
        print_error "Connector is not running properly"
        echo "Status: $status"
        exit 1
    fi

    # Check replication slot - Debezium creates it automatically with connector name
    local slot_count=$($CONTAINER_RUNTIME exec rbac_db psql -U postgres -t -c "SELECT count(*) FROM pg_replication_slots;" | xargs)
    if [ "$slot_count" -ge "1" ]; then
        local slot_name=$($CONTAINER_RUNTIME exec rbac_db psql -U postgres -t -c "SELECT slot_name FROM pg_replication_slots LIMIT 1;" | xargs)
        print_success "Replication slot created successfully: $slot_name"
    else
        print_warning "Replication slot not created yet (will be created on first connector activity)"
    fi

    # List topics
    print_status "Available Kafka topics:"
    $CONTAINER_RUNTIME exec insights-rbac-kafka-1 kafka-topics --bootstrap-server localhost:9092 --list | grep -E "(rbac|outbox)" || true
}

# Function to setup RBAC Kafka consumer
setup_kafka_consumer() {
    print_status "Setting up RBAC Kafka consumer test environment..."

    # Create consumer test script that runs inside Docker container
    cat > scripts/debezium/test_kafka_consumer.sh << 'EOF'
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
      --name rbac-kafka-consumer-test \
      --rm \
      rbac_server python rbac/manage.py launch-rbac-kafka-consumer relations)

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
      --rm \
      rbac_server python rbac/manage.py launch-rbac-kafka-consumer relations
fi
EOF

    chmod +x scripts/debezium/test_kafka_consumer.sh
    print_success "Consumer test script created at scripts/debezium/test_kafka_consumer.sh"

    # Create a simple consumer runner script for existing RBAC server container
    cat > scripts/debezium/run_kafka_consumer.sh << 'EOF'
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
  rbac_server python rbac/manage.py launch-rbac-kafka-consumer relations
EOF

    chmod +x scripts/debezium/run_kafka_consumer.sh
    print_success "Consumer runner script created at scripts/debezium/run_kafka_consumer.sh"

    # Create message sender script
    cat > scripts/debezium/send_test_relations_message.sh << 'EOF'
#!/bin/bash

# Script to send test relations message to Kafka
# Usage: ./send_test_relations_message.sh [topic_name]

# Detect container runtime (docker or podman)
if command -v docker &> /dev/null && docker info &> /dev/null; then
    CONTAINER_RUNTIME="docker"
elif command -v podman &> /dev/null; then
    CONTAINER_RUNTIME="podman"
else
    CONTAINER_RUNTIME="docker"  # fallback default
fi

TOPIC=${1:-"outbox.event.relations-replication-event"}

# Create test message in the format expected by RBAC Kafka consumer
cat > /tmp/test_relations_message.json << EOL
{
  "schema": {
    "type": "struct",
    "fields": [
      {
        "type": "string",
        "optional": false,
        "field": "payload"
      }
    ],
    "optional": false,
    "name": "test.relations.message"
  },
  "payload": "{\"relations_to_add\": [{\"resource\": {\"type\": \"role\", \"id\": \"test-role-$(date +%s)\"}, \"relation\": \"member\", \"subject\": {\"type\": \"user\", \"id\": \"test-user-$(date +%s)\"}}], \"relations_to_remove\": []}"
}
EOL

echo "Sending test relations message to topic: $TOPIC"

# Send message to Kafka
$CONTAINER_RUNTIME exec -i insights-rbac-kafka-1 kafka-console-producer \
  --bootstrap-server localhost:9092 \
  --topic "$TOPIC" < /tmp/test_relations_message.json

echo "Message sent successfully!"
echo "Check consumer logs to see if it was processed."

# Clean up
rm -f /tmp/test_relations_message.json
EOF

    chmod +x scripts/debezium/send_test_relations_message.sh
    print_success "Message sender script created at scripts/debezium/send_test_relations_message.sh"
}

# Function to test kafka consumer setup
test_kafka_consumer_setup() {
    print_status "Testing Kafka consumer setup..."

    # Check if RBAC server container is running
    if ! $CONTAINER_RUNTIME ps | grep -q rbac_server; then
        print_warning "RBAC server container not running. Starting it..."
        $COMPOSE_CMD up -d rbac-server
        sleep 10
    fi

    print_status "Sending test message to verify consumer can process messages..."
    ./scripts/debezium/send_test_relations_message.sh outbox.event.relations-replication-event

    print_success "Test message sent. Consumer setup is ready!"
}

# Function to restart all containers
restart_containers() {
    print_status "Restarting all containers to reload code changes..."

    # Restart main services
    $COMPOSE_CMD restart

    print_success "All containers restarted"
    print_status "Waiting for services to be ready..."
    sleep 5

    # Wait for rbac_server to be healthy
    local max_attempts=30
    local attempt=1
    while [ $attempt -le $max_attempts ]; do
        if $CONTAINER_RUNTIME exec rbac_server curl -s http://localhost:8080/metrics > /dev/null 2>&1; then
            print_success "RBAC server is ready"
            break
        fi
        echo -n "."
        sleep 2
        attempt=$((attempt + 1))
    done

    print_success "Containers restarted and services are ready"
}

# Function to restart only rbac-server container
restart_server() {
    print_status "Restarting rbac-server container to reload code changes..."

    $COMPOSE_CMD restart rbac-server

    print_success "RBAC server container restarted"
    print_status "Waiting for server to be ready..."
    sleep 3

    # Wait for rbac_server to be healthy
    local max_attempts=30
    local attempt=1
    while [ $attempt -le $max_attempts ]; do
        if $CONTAINER_RUNTIME exec rbac_server curl -s http://localhost:8080/metrics > /dev/null 2>&1; then
            print_success "RBAC server is ready"
            break
        fi
        echo -n "."
        sleep 2
        attempt=$((attempt + 1))
    done

    print_success "Server restarted and ready"
}

# Function to run Kafka consumer
run_kafka_consumer() {
    local topic=${1:-"outbox.event.relations-replication-event"}
    local mode=${2:-"interactive"}

    print_status "Starting RBAC Kafka consumer..."

    # Check if RBAC server container is running
    if ! $CONTAINER_RUNTIME ps | grep -q rbac_server; then
        print_warning "RBAC server container not running. Starting it..."
        $COMPOSE_CMD up -d rbac-server
        sleep 10
    fi

    if [ "$mode" = "background" ]; then
        print_status "Starting Kafka consumer in background mode..."
        echo "Topic: $topic"

        # Run consumer in background with console logging and writable log file path
        $CONTAINER_RUNTIME exec -d \
          -e KAFKA_ENABLED=true \
          -e RBAC_KAFKA_CUSTOM_CONSUMER_BROKER=kafka:9092 \
          -e RBAC_KAFKA_CONSUMER_TOPIC="$topic" \
          -e RBAC_KAFKA_CONSUMER_GROUP_ID=rbac-consumer-group \
          -e DJANGO_LOG_HANDLERS=console \
          -e DJANGO_LOG_FILE=/tmp/app.log \
          rbac_server python rbac/manage.py launch-rbac-kafka-consumer relations

        print_success "Kafka consumer started in background"
        echo "Check logs with: $CONTAINER_RUNTIME logs rbac_server -f"
    else
        print_status "Starting Kafka consumer in interactive mode..."
        echo "Topic: $topic"
        echo "Press Ctrl+C to stop the consumer"

        # Run consumer interactively with console logging and writable log file path
        $CONTAINER_RUNTIME exec -it \
          -e KAFKA_ENABLED=true \
          -e RBAC_KAFKA_CUSTOM_CONSUMER_BROKER=kafka:9092 \
          -e RBAC_KAFKA_CONSUMER_TOPIC="$topic" \
          -e RBAC_KAFKA_CONSUMER_GROUP_ID=rbac-consumer-group \
          -e DJANGO_LOG_HANDLERS=console \
          -e DJANGO_LOG_FILE=/tmp/app.log \
          rbac_server python rbac/manage.py launch-rbac-kafka-consumer relations
    fi
}

# Function to show connection information
show_connection_info() {
    echo ""
    echo "========================================"
    echo "🎉 Debezium + Kafka Consumer Setup Complete!"
    echo "========================================"
    echo ""
    echo "📊 Monitoring URLs:"
    echo "   • Kafdrop UI:         http://localhost:9001"
    echo "   • Kafka Connect API:  http://localhost:8083"
    echo ""
    echo "🔧 Service Ports:"
    echo "   • Kafka Broker:       localhost:9092"
    echo "   • PostgreSQL:         localhost:15432"
    echo ""
    echo "📋 Connector Information:"
    echo "   • Replication Slot:   debezium_slot"
    echo "   • RBAC Consumer Topic: outbox.event.relations-replication-event"
    echo ""
    echo "🚀 Kafka Consumer Commands:"
    echo "   • Run consumer (interactive):     $0 --consumer"
    echo "   • Run consumer (background):      $0 --consumer-bg"
    echo "   • Run consumer (custom topic):    $0 --consumer [topic_name]"
    echo "   • Send test message:              ./scripts/debezium/send_test_relations_message.sh"
    echo ""
    echo "🔄 Development Commands:"
    echo "   • Restart all containers:         $0 --restart"
    echo "   • Restart server only:            $0 --restart-server"
    echo ""
    echo "🔗 Useful Commands:"
    echo "   • Check connector:        curl http://localhost:8083/connectors/rbac-postgres-connector/status"
    echo "   • List topics:                $CONTAINER_RUNTIME exec insights-rbac-kafka-1 kafka-topics --bootstrap-server localhost:9092 --list"
    echo "   • View messages in topic:     $CONTAINER_RUNTIME exec insights-rbac-kafka-1 kafka-console-consumer --bootstrap-server localhost:9092 --topic outbox.event.relations-replication-event --from-beginning --timeout-ms 5000"
    echo "   • Check message count:        $CONTAINER_RUNTIME exec insights-rbac-kafka-1 kafka-run-class kafka.tools.GetOffsetShell --broker-list localhost:9092 --topic outbox.event.relations-replication-event"
    echo "   • Stop services:              $COMPOSE_CMD -f docker-compose.debezium.yml down"
    echo ""
    echo "📝 Next Steps:"
    echo "   1. Run the consumer:          $0 --consumer"
    echo "   2. Send a test message:       ./scripts/debezium/send_test_relations_message.sh"
    echo "   3. Test with RBAC operations: Add users to groups via API"
    echo ""
}

# Function to cleanup on failure
cleanup_on_failure() {
    print_error "Setup failed. Cleaning up..."
    $COMPOSE_CMD -f docker-compose.debezium.yml down > /dev/null 2>&1 || true
    exit 1
}

# Function to cleanup existing containers that might conflict
cleanup_existing_containers() {
    print_status "Checking for existing containers that might conflict..."

    # Stop docker-compose services to free up ports
    if $COMPOSE_CMD ps 2>/dev/null | grep -q "Up"; then
        print_status "Stopping existing docker-compose services..."
        $COMPOSE_CMD down --remove-orphans 2>/dev/null || true
        print_success "Stopped existing services"
    fi

    # Stop Debezium services
    if $COMPOSE_CMD -f docker-compose.debezium.yml ps 2>/dev/null | grep -q "Up"; then
        print_status "Stopping existing Debezium services..."
        $COMPOSE_CMD -f docker-compose.debezium.yml down --remove-orphans 2>/dev/null || true
        print_success "Stopped Debezium services"
    fi

    print_success "Cleanup of existing containers completed"
}

# Main execution
main() {
    echo "========================================"
    echo "🚀 RBAC Debezium Setup Script"
    echo "========================================"
    echo ""

    # Set trap for cleanup on failure
    trap cleanup_on_failure ERR

    # Step 1: Check prerequisites
    check_container_runtime

    # Step 2: Cleanup existing containers
    cleanup_existing_containers

    # Step 3: Check required ports
    check_required_ports

    # Step 4: Create network
    create_network

    # Step 5: Start and configure PostgreSQL
    start_database
    configure_postgres_replication

    # Step 6: Run database migrations to create outbox table
    run_migrations

    # Step 7: Start Debezium services
    start_debezium_services

    # Step 8: Create topics and connectors
    create_rbac_topic
    create_consumer_group
    create_debezium_connector

    # Step 9: Setup Kafka consumer
    setup_kafka_consumer

    # Step 10: Verify setup
    verify_setup

    # Step 11: Test Kafka consumer
    test_kafka_consumer_setup

    # Step 12: Show connection information
    show_connection_info
}

# Parse command line arguments
case "${1:-}" in
    --help|-h)
        echo "Usage: $0 [OPTIONS]"
        echo ""
        echo "This script sets up Debezium Change Data Capture for RBAC:"
        echo "  1. Creates container network"
        echo "  2. Starts and configures PostgreSQL with logical replication"
        echo "  3. Starts Kafka, Zookeeper, Kafka Connect, and Kafdrop"
        echo "  4. Creates and configures Debezium PostgreSQL connector"
        echo "  5. Verifies the setup"
        echo ""
        echo "Options:"
        echo "  --help, -h              Show this help message"
        echo "  --consumer              Run Kafka consumer (interactive)"
        echo "  --consumer-bg           Run Kafka consumer (background)"
        echo "  --consumer [topic]      Run Kafka consumer with custom topic"
        echo "  --restart               Restart all containers to reload code changes"
        echo "  --restart-server        Restart only rbac-server container to reload code changes"
        echo ""
        echo "Prerequisites:"
        echo "  • Docker/Podman and docker-compose/podman-compose installed and running"
        echo "  • Ports 8083, 9000, 9092, 15432 available"
        echo "  • docker-compose.yml and docker-compose.debezium.yml present"
        exit 0
        ;;
    --consumer)
        echo "========================================"
        echo "🚀 Starting RBAC Kafka Consumer"
        echo "========================================"
        echo ""
        run_kafka_consumer "${2:-outbox.event.relations-replication-event}" "interactive"
        ;;
    --consumer-bg)
        echo "========================================"
        echo "🚀 Starting RBAC Kafka Consumer (Background)"
        echo "========================================"
        echo ""
        run_kafka_consumer "${2:-outbox.event.relations-replication-event}" "background"
        ;;
    --restart)
        echo "========================================"
        echo "🔄 Restarting All Containers"
        echo "========================================"
        echo ""
        restart_containers
        ;;
    --restart-server)
        echo "========================================"
        echo "🔄 Restarting RBAC Server"
        echo "========================================"
        echo ""
        restart_server
        ;;
    *)
        main "$@"
        ;;
esac
