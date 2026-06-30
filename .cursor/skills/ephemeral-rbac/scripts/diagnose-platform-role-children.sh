#!/bin/bash
# Diagnose platform role parent->child hierarchy: Django vs SpiceDB in an ephemeral namespace.
#
# Usage: sh .cursor/skills/ephemeral-rbac/scripts/diagnose-platform-role-children.sh ephemeral-<id>

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 ephemeral-<id>"
  exit 1
fi

NS="$1"
POD=$(oc get pods -n "$NS" -o name 2>/dev/null | grep rbac-service | head -1 | cut -d/ -f2)
if [ -z "$POD" ]; then
  echo "ERROR: No rbac-service pod found in namespace $NS"
  exit 1
fi

echo "=== Ephemeral RBAC platform role children diagnosis ==="
echo "Namespace: $NS"
echo "Pod:       $POD"
echo

echo "--- Django RoleV2 platform roles ---"
oc exec -n "$NS" "$POD" -c rbac-service -- python rbac/manage.py shell -c "
from management.role.v2_model import RoleV2
total_children = 0
for p in RoleV2.objects.filter(type='platform').order_by('name').prefetch_related('children'):
    n = p.children.count()
    total_children += n
    print(f'  {p.name}: children={n} uuid={p.uuid}')
print(f'Total platform roles: {RoleV2.objects.filter(type=\"platform\").count()}')
print(f'Total child links: {total_children}')
" 2>&1 | grep -v '^GLITCHTIP\|^32 objects imported'

echo
echo "--- SpiceDB child tuples (via Kessel read_tuples) ---"
oc exec -n "$NS" "$POD" -c rbac-service -- python rbac/manage.py shell -c "
from management.relation_replicator.relations_api_replicator import RelationsApiReplicator
from management.role.v2_model import RoleV2

replicator = RelationsApiReplicator()
seeded = RoleV2.objects.filter(type='seeded').first()
if seeded is None:
    print('  No seeded roles in DB')
else:
    spicedb_total = 0
    for p in RoleV2.objects.filter(type='platform').order_by('name'):
        resp = replicator.read_tuples(
            resource_type='role', resource_id=str(p.uuid), relation='child',
            subject_type='role', subject_id=str(seeded.uuid),
            resource_namespace='rbac', subject_namespace='rbac')
        n = len(resp)
        spicedb_total += n
        print(f'  {p.name} -> {seeded.name}: {n} tuple(s)')
    print(f'Sample seeded role: {seeded.name} ({seeded.uuid})')
    print(f'Platform roles with at least one child tuple to sample: {spicedb_total}')
" 2>&1 | grep -v '^GLITCHTIP\|^32 objects imported'

echo
echo "--- Init seed log signals ---"
NO_CHANGE=$(oc logs -n "$NS" deploy/rbac-service -c rbac-service-init 2>&1 | grep -c 'No change in system role' || true)
REPLICATED=$(oc logs -n "$NS" deploy/rbac-service -c rbac-service-init 2>&1 | grep -c 'Replicated system role' || true)
CREATED=$(oc logs -n "$NS" deploy/rbac-service -c rbac-service-init 2>&1 | grep -c 'Created system role' || true)
echo "  No change in system role: $NO_CHANGE"
echo "  Created system role:      $CREATED"
echo "  Replicated system role:   $REPLICATED"

echo
echo "--- Outbox ---"
oc exec -n "$NS" "$POD" -c rbac-service -- python rbac/manage.py shell -c "
from management.debezium.model import Outbox
print(f'  Outbox rows: {Outbox.objects.count()}')
" 2>&1 | grep -v '^GLITCHTIP\|^32 objects imported'

echo
if [ "$NO_CHANGE" -gt 0 ] && [ "$REPLICATED" -eq 0 ]; then
  echo "LIKELY ISSUE: Postgres roles unchanged on seed; SpiceDB replication skipped."
  echo "FIX: oc exec -n $NS deploy/rbac-service -c rbac-service -- python rbac/manage.py seeds --roles --force-create-relationships"
fi
