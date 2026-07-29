"""Check if the rbac-debezium Kafka Connect connector and its task are RUNNING.

Usage: python3 check_debezium.py <kafka_connect_url>
Exit 0 if both are RUNNING, exit 1 otherwise.
"""

import json
import sys
import urllib.error
import urllib.request

try:
    base_url = sys.argv[1]
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        sys.exit(1)
    url = f"{base_url}/connectors/rbac-debezium/status"
    with urllib.request.urlopen(url, timeout=5) as response:
        data = json.load(response)
    connector = data.get("connector", {}).get("state", "")
    tasks = data.get("tasks") or []
    task = tasks[0].get("state", "") if tasks else ""
    sys.exit(0 if connector == "RUNNING" and task == "RUNNING" else 1)
except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, IndexError, KeyError, TypeError, AttributeError):
    sys.exit(1)
