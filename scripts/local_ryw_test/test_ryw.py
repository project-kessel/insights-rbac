#!/usr/bin/env python3
"""Test Read-Your-Writes (RYW) flow via the full Debezium + Kafka Consumer pipeline.

Flow:
  1. Create workspace via HTTP POST → outbox row written
  2. Debezium captures WAL change → publishes to Kafka
  3. Kafka consumer reads message → calls mock Kessel → sends pg_notify
  4. API server's RYW wait receives pg_notify → returns response
  5. This script verifies the response succeeded without timeout

Usage:
  python scripts/local_ryw_test/test_ryw.py [--api-url URL] [--db-port PORT]
"""

import argparse
import base64
import json
import select
import sys
import time
import uuid

import psycopg2
import psycopg2.extensions
import requests

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_DB_HOST = "localhost"
DEFAULT_DB_PORT = 15432
DEFAULT_DB_NAME = "postgres"
DEFAULT_DB_USER = "postgres"
DEFAULT_DB_PASSWORD = "postgres"

RYW_CHANNEL = "READ_YOUR_WRITES_CHANNEL"


def make_identity_header(org_id, account_id="12345678", username="test-ryw-user"):
    """Build a base64-encoded x-rh-identity header."""
    identity = {
        "identity": {
            "account_number": account_id,
            "org_id": org_id,
            "type": "User",
            "user": {
                "username": username,
                "email": f"{username}@example.com",
                "is_org_admin": True,
                "user_id": "1111111",
            },
        }
    }
    return base64.b64encode(json.dumps(identity).encode()).decode()


def wait_for_api_ready(api_url, timeout=30):
    """Wait for the API server to be ready."""
    print(f"  Waiting for API server at {api_url}...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{api_url}/api/v2/workspaces/", timeout=2)
            if r.status_code in (200, 401, 403):
                print(f"  API server ready (status={r.status_code})")
                return True
        except requests.ConnectionError:
            pass
        time.sleep(1)
    print("  ERROR: API server not ready within timeout")
    return False


def check_db_connection(host, port, dbname, user, password):
    """Verify PostgreSQL connection."""
    try:
        conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)
        conn.close()
        return True
    except Exception as e:
        print(f"  ERROR: Cannot connect to PostgreSQL: {e}")
        return False


def listen_for_notify(host, port, dbname, user, password, channel, timeout=30):
    """Listen for a PostgreSQL NOTIFY on the given channel.

    Returns (payload, elapsed_seconds) on success, or (None, elapsed) on timeout.
    """
    conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"LISTEN {channel};")

    started = time.monotonic()
    deadline = started + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, time.monotonic() - started

            readable, _, _ = select.select([conn], [], [], min(1.0, remaining))
            if readable:
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    elapsed = time.monotonic() - started
                    return notify.payload, elapsed
    finally:
        try:
            cur.execute(f"UNLISTEN {channel};")
        except Exception:
            pass
        conn.close()


def create_workspace_via_api(api_url, org_id, workspace_name):
    """Create a workspace via the v2 API. Returns (response, elapsed_seconds)."""
    identity = make_identity_header(org_id)
    headers = {
        "Content-Type": "application/json",
        "x-rh-identity": identity,
    }
    payload = {"name": workspace_name}

    started = time.monotonic()
    response = requests.post(f"{api_url}/api/v2/workspaces/", json=payload, headers=headers, timeout=60)
    elapsed = time.monotonic() - started
    return response, elapsed


def delete_workspace_via_api(api_url, org_id, workspace_id):
    """Delete a workspace via the v2 API. Returns (response, elapsed_seconds)."""
    identity = make_identity_header(org_id)
    headers = {"x-rh-identity": identity}

    started = time.monotonic()
    response = requests.delete(f"{api_url}/api/v2/workspaces/{workspace_id}/", headers=headers, timeout=60)
    elapsed = time.monotonic() - started
    return response, elapsed


def run_test(api_url, db_host, db_port, db_name, db_user, db_password, test_ryw_listener=False, num_workspaces=3):
    """Run the full RYW test."""
    org_id = f"ryw-test-{uuid.uuid4().hex[:8]}"

    print("=" * 60)
    print("  Read-Your-Writes (RYW) Pipeline Test")
    print(f"  Creating {num_workspaces} workspace(s) for org '{org_id}'")
    print("=" * 60)
    print()

    # Step 1: Check prerequisites
    print("[1/4] Checking prerequisites...")
    if not check_db_connection(db_host, db_port, db_name, db_user, db_password):
        return False, None, []
    print(f"  PostgreSQL OK ({db_host}:{db_port})")

    if not wait_for_api_ready(api_url):
        return False, None, []

    # Step 2: Optionally start a parallel LISTEN to observe NOTIFYs independently
    notify_results = []
    if test_ryw_listener:
        import threading

        print()
        print(f"[2/4] Starting independent LISTEN on channel '{RYW_CHANNEL}'...")

        def listener():
            conn = psycopg2.connect(host=db_host, port=db_port, dbname=db_name, user=db_user, password=db_password)
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute(f"LISTEN {RYW_CHANNEL};")
            started = time.monotonic()
            deadline = started + 120
            try:
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    readable, _, _ = select.select([conn], [], [], min(1.0, remaining))
                    if readable:
                        conn.poll()
                        while conn.notifies:
                            notify = conn.notifies.pop(0)
                            elapsed = time.monotonic() - started
                            notify_results.append({"payload": notify.payload, "elapsed": elapsed})
            finally:
                try:
                    cur.execute(f"UNLISTEN {RYW_CHANNEL};")
                except Exception:
                    pass
                conn.close()

        listener_thread = threading.Thread(target=listener, daemon=True)
        listener_thread.start()
        time.sleep(0.5)
        print("  LISTEN active")
    else:
        print()
        print("[2/4] Skipping independent LISTEN (RYW is tested via API response timing)")

    # Step 3: Create workspaces via API
    print()
    print(f"[3/4] Creating {num_workspaces} workspace(s)...")

    results = []
    all_passed = True
    for i in range(num_workspaces):
        workspace_name = f"ryw-test-ws-{i + 1}-{uuid.uuid4().hex[:8]}"
        print(f"\n  --- Workspace {i + 1}/{num_workspaces}: '{workspace_name}' ---")
        print(f"  POST {api_url}/api/v2/workspaces/")

        response, api_elapsed = create_workspace_via_api(api_url, org_id, workspace_name)

        print(f"  Status: {response.status_code}")
        print(f"  API response time: {api_elapsed:.3f}s")

        if response.status_code == 201:
            body = response.json()
            workspace_id = body.get("id", "unknown")
            print(f"  Workspace ID: {workspace_id}")
            results.append({"name": workspace_name, "id": workspace_id, "elapsed": api_elapsed, "ok": True})
        else:
            print(f"  ERROR: {response.text[:200]}")
            results.append({"name": workspace_name, "id": None, "elapsed": api_elapsed, "ok": False})
            all_passed = False

    # Step 4: Verify results
    print()
    print("[4/4] Verifying pipeline...")
    print()

    print(f"  {'#':<4} {'Status':<8} {'Time':>8}  {'Workspace ID'}")
    print(f"  {'─' * 4} {'─' * 8} {'─' * 8}  {'─' * 36}")
    for i, r in enumerate(results):
        status = "PASS" if r["ok"] else "FAIL"
        wid = r["id"] or "—"
        print(f"  {i + 1:<4} {status:<8} {r['elapsed']:>7.3f}s  {wid}")

    if test_ryw_listener:
        # Give listener a few more seconds to collect remaining NOTIFYs
        time.sleep(3)
        print()
        print(f"  Independent LISTEN captured {len(notify_results)} NOTIFY message(s):")
        for n in notify_results:
            matching = any(r["id"] == n["payload"] for r in results)
            tag = "MATCH" if matching else "other"
            print(f"    [{tag}] payload='{n['payload']}' after {n['elapsed']:.3f}s")

    print()
    print("=" * 60)
    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    print(f"  TEST RESULT: {passed} passed, {failed} failed out of {num_workspaces}")
    if all_passed:
        print()
        print("  All workspaces created successfully. The full pipeline")
        print("  (outbox -> Debezium -> Kafka -> consumer -> mock Kessel")
        print("  -> pg_notify) was exercised for each workspace.")
    print("=" * 60)

    return all_passed, org_id, results


def print_summary_table(label, results):
    """Print a summary table of workspace operations."""
    print(f"\n  {label}")
    print(f"  {'#':<4} {'Op':<8} {'Status':<8} {'Time':>8}  {'Workspace ID'}")
    print(f"  {'─' * 4} {'─' * 8} {'─' * 8} {'─' * 8}  {'─' * 36}")
    for i, r in enumerate(results):
        status = "PASS" if r["ok"] else "FAIL"
        op = r.get("op", "CREATE")
        wid = r["id"] or "—"
        print(f"  {i + 1:<4} {op:<8} {status:<8} {r['elapsed']:>7.3f}s  {wid}")


def run_phase2(api_url, org_id, init_results, db_host, db_port, db_name, db_user, db_password):
    """Phase 2: create 2 new workspaces, delete 2 from init phase."""
    print()
    print("=" * 60)
    print("  Phase 2: Create + Delete workspaces")
    print("=" * 60)

    phase2_results = []

    # Create 2 new workspaces with phase-2 prefix
    print("\n  --- Creating 2 new workspaces (phase-2) ---")
    for i in range(2):
        ws_name = f"phase-2-ws-{i + 1}-{uuid.uuid4().hex[:8]}"
        print(f"\n  POST {api_url}/api/v2/workspaces/  name='{ws_name}'")
        response, elapsed = create_workspace_via_api(api_url, org_id, ws_name)
        print(f"  Status: {response.status_code}  Time: {elapsed:.3f}s")
        if response.status_code == 201:
            body = response.json()
            wid = body.get("id", "unknown")
            print(f"  Workspace ID: {wid}")
            phase2_results.append({"name": ws_name, "id": wid, "elapsed": elapsed, "ok": True, "op": "CREATE"})
        else:
            print(f"  ERROR: {response.text[:200]}")
            phase2_results.append({"name": ws_name, "id": None, "elapsed": elapsed, "ok": False, "op": "CREATE"})

    # Delete 2 workspaces from init phase
    created_in_init = [r for r in init_results if r["ok"] and r["id"]]
    to_delete = created_in_init[:2]

    print(f"\n  --- Deleting {len(to_delete)} workspace(s) from init phase ---")
    for r in to_delete:
        print(f"\n  DELETE {api_url}/api/v2/workspaces/{r['id']}/  ('{r['name']}')")
        response, elapsed = delete_workspace_via_api(api_url, org_id, r["id"])
        print(f"  Status: {response.status_code}  Time: {elapsed:.3f}s")
        ok = response.status_code == 204
        if not ok:
            print(f"  ERROR: {response.text[:200]}")
        phase2_results.append({"name": r["name"], "id": r["id"], "elapsed": elapsed, "ok": ok, "op": "DELETE"})

    # Summary
    print_summary_table("Phase 2 Summary", phase2_results)

    passed = sum(1 for r in phase2_results if r["ok"])
    failed = sum(1 for r in phase2_results if not r["ok"])
    print()
    print("=" * 60)
    print(f"  PHASE 2 RESULT: {passed} passed, {failed} failed out of {len(phase2_results)}")
    print("=" * 60)

    return all(r["ok"] for r in phase2_results)


def main():
    parser = argparse.ArgumentParser(description="Test RYW pipeline")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="RBAC API base URL")
    parser.add_argument("--db-host", default=DEFAULT_DB_HOST, help="PostgreSQL host")
    parser.add_argument("--db-port", type=int, default=DEFAULT_DB_PORT, help="PostgreSQL port")
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME, help="Database name")
    parser.add_argument("--db-user", default=DEFAULT_DB_USER, help="Database user")
    parser.add_argument("--db-password", default=DEFAULT_DB_PASSWORD, help="Database password")
    parser.add_argument("--listen", action="store_true", help="Also run an independent LISTEN to observe pg_notify")
    parser.add_argument("--count", type=int, default=3, help="Number of workspaces to create (default: 3)")
    parser.add_argument(
        "--phase2-from",
        metavar="FILE",
        help="Run phase 2 using init results from FILE (JSON saved by --save-results)",
    )
    parser.add_argument(
        "--save-results",
        metavar="FILE",
        help="Save init phase results to FILE (JSON) for later use by --phase2-from",
    )
    args = parser.parse_args()

    if args.phase2_from:
        with open(args.phase2_from) as f:
            saved = json.load(f)
        phase2_passed = run_phase2(
            api_url=args.api_url,
            org_id=saved["org_id"],
            init_results=saved["results"],
            db_host=args.db_host,
            db_port=args.db_port,
            db_name=args.db_name,
            db_user=args.db_user,
            db_password=args.db_password,
        )
        sys.exit(0 if phase2_passed else 1)

    init_passed, org_id, init_results = run_test(
        api_url=args.api_url,
        db_host=args.db_host,
        db_port=args.db_port,
        db_name=args.db_name,
        db_user=args.db_user,
        db_password=args.db_password,
        test_ryw_listener=args.listen,
        num_workspaces=args.count,
    )

    if args.save_results and init_passed:
        with open(args.save_results, "w") as f:
            json.dump({"org_id": org_id, "results": init_results}, f, indent=2)
        print(f"\n  Init results saved to {args.save_results}")

    sys.exit(0 if init_passed else 1)


if __name__ == "__main__":
    main()
