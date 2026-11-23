#!/usr/bin/env python3
"""Copy and isolate inventory-api Compose inputs for one local stack slot."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: prepare-slot-compose.py INVENTORY_REPO COMPOSE_DIR PORT_OFFSET")
    source_repo = Path(sys.argv[1]).resolve()
    compose_dir = Path(sys.argv[2]).resolve()
    port_offset = int(sys.argv[3])
    target_development = compose_dir.parent
    source_development = source_repo / "development"
    target_development = target_development.resolve()
    # Slot directories are reused; clear a stale directory where Compose expects
    # schema.zed to be a file before merging the fresh Compose tree.
    target_schema = compose_dir / "configs" / "schema.zed"
    if target_schema.is_dir():
        shutil.rmtree(target_schema)
    shutil.copytree(source_development / "full-kessel", compose_dir, dirs_exist_ok=True)
    shutil.copytree(source_development / "configs", target_development / "configs", dirs_exist_ok=True)
    schema_cache = source_repo / "schema_cache.json"
    if schema_cache.is_file():
        shutil.copy2(schema_cache, target_development.parent / "schema_cache.json")

    ports = {
        "ZOOKEEPER_HOST_PORT": (2181, 2181),
        "KAFKA_HOST_PORT": (9092, 9092),
        "INVENTORY_HOST_DB_PORT": (5433, 5433),
        "INVENTORY_API_HTTP_PORT": (8081, 8081),
        "INVENTORY_API_GRPC_PORT": (9081, 9081),
        "KAFKA_CONNECT_PORT": (8083, 8083),
        "RELATIONS_DB_PORT": (5432, 5432),
        "SPICEDB_PORT": (50051, 50051),
        "RELATIONS_HTTP_PORT": (8000, 8000),
        "RELATIONS_PORT": (9000, 9000),
        "RBAC_DB_PORT": (15432, 5432),
        "REDIS_HOST_PORT": (6379, 6379),
        "PROMETHEUS_PORT": (9050, 9090),
        "ALERTMANAGER_PORT": (9093, 9093),
        "GRAFANA_PORT": (3000, 3000),
        "RBAC_API_PORT": (9080, 8080),
    }
    compose_file = compose_dir / "docker-compose.yaml"
    contents = compose_file.read_text(encoding="utf-8")
    source_host_overrides = {
        "RELATIONS_HTTP_PORT": (8100,),
        "RELATIONS_PORT": (9100,),
    }
    for variable, (default_host, container_port) in ports.items():
        host_port = int(os.environ.get(variable, default_host + port_offset))
        source_hosts = (default_host, *source_host_overrides.get(variable, ()))
        matches = [
            f'"{source_host}:{container_port}"'
            for source_host in source_hosts
            if contents.count(f'"{source_host}:{container_port}"') == 1
        ]
        ambiguous = [
            f'"{source_host}:{container_port}"'
            for source_host in source_hosts
            if contents.count(f'"{source_host}:{container_port}"') > 1
        ]
        if ambiguous or len(matches) > 1:
            raise RuntimeError(f"ambiguous Compose mapping for {variable}: {ambiguous or matches}")
        if not matches:
            continue
        contents = contents.replace(matches[0], f'"{host_port}:{container_port}"')
    contents = contents.replace(
        "name: kessel\n    external: true",
        "name: ${KESSEL_NETWORK_NAME:-kessel}\n    external: true",
    )
    kafka_host_port = int(os.environ.get("KAFKA_HOST_PORT", 9092 + port_offset))
    contents = contents.replace(
        "HOST://localhost:9092,DOCKER://kafka:9093",
        f"HOST://localhost:{kafka_host_port},DOCKER://kafka:9093",
    )
    compose_file.write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    main()
