#!/usr/bin/env bash
# Create platform.inventory.* topics on the Kessel Kafka broker (for HBI dev.yml).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/logging.sh
source "${SCRIPT_DIR}/../common/logging.sh"
# shellcheck source=../common/container_runtime.sh
source "${SCRIPT_DIR}/../common/container_runtime.sh"

INVENTORY_API_REPO="${1:?inventory-api repo path required}"

detect_container_runtime

COMPOSE_DIR="${FULL_KESSEL_COMPOSE_DIR:-${INVENTORY_API_REPO}/development/full-kessel}"
ENV_FILE="${COMPOSE_DIR}/.env"

TOPICS=(
  platform.inventory.host-ingress
  platform.inventory.host-ingress-p1
  platform.inventory.events
  platform.inventory.system-profile
  platform.inventory.host-apps
)

compose() {
  "${COMPOSE_CMD[@]}" --env-file "${ENV_FILE}" -f "${COMPOSE_DIR}/docker-compose.yaml" "$@"
}

for topic in "${TOPICS[@]}"; do
  compose exec -T kafka bin/kafka-topics.sh \
    --bootstrap-server kafka:9093 \
    --create --if-not-exists \
    --topic "${topic}" \
    --replication-factor 1 \
    --partitions 1
done

compose exec -T kafka bin/kafka-topics.sh --bootstrap-server kafka:9093 --list
