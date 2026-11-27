#!/usr/bin/env sh
# Share published port defaults between startup, health checks, and validation.
# Explicit port settings always win over the slot offset.

if [ -n "${FULL_KESSEL_COMPOSE_DIR:-}" ]; then
  full_stack_port_offset="${FULL_STACK_PORT_OFFSET:-0}"
else
  full_stack_port_offset=0
fi

RBAC_API_PORT="${RBAC_API_PORT:-$((9080 + full_stack_port_offset))}"
RBAC_DB_PORT="${RBAC_DB_PORT:-$((15432 + full_stack_port_offset))}"
SPICEDB_PORT="${SPICEDB_PORT:-$((50051 + full_stack_port_offset))}"
INVENTORY_API_HTTP_PORT="${INVENTORY_API_HTTP_PORT:-$((8081 + full_stack_port_offset))}"
INVENTORY_API_GRPC_PORT="${INVENTORY_API_GRPC_PORT:-$((9081 + full_stack_port_offset))}"
KAFKA_CONNECT_PORT="${KAFKA_CONNECT_PORT:-$((8083 + full_stack_port_offset))}"
HBI_WEB_PORT="${HBI_WEB_PORT:-$((8080 + full_stack_port_offset))}"
INVENTORY_DB_PORT="${INVENTORY_DB_PORT:-$((15433 + full_stack_port_offset))}"

export RBAC_API_PORT RBAC_DB_PORT SPICEDB_PORT INVENTORY_API_HTTP_PORT
export INVENTORY_API_GRPC_PORT KAFKA_CONNECT_PORT HBI_WEB_PORT INVENTORY_DB_PORT
unset full_stack_port_offset
