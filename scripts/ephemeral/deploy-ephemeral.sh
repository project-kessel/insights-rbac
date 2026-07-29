#!/bin/bash
# Deploy RBAC + dependencies to an ephemeral cluster with DR-ready configuration.
#
# Usage:
#   ./scripts/ephemeral/deploy-ephemeral.sh
#   DURATION=4h ./scripts/ephemeral/deploy-ephemeral.sh

set -euo pipefail

DURATION="${DURATION:-8h}"

bonfire deploy host-inventory kessel rbac --source appsre --ref-env insights-stage \
    --duration "${DURATION}" \
    -p rbac/NOTIFICATIONS_RH_ENABLED=False \
    -p rbac/DR_RELATIONS_RECONCILE_ENABLED=True \
    -p rbac/DR_WORKSPACE_RECONCILE_ENABLED=True \
    -p rbac/KAFKA_ENABLED=True \
    -p rbac/RBAC_KAFKA_CONSUMER_TOPIC=outbox.event.relations-replication-event \
    -p rbac/MIN_WORKER_REPLICAS=1 \
    -p rbac/CELERY_WORKER_CONCURRENCY=1 \
    -p rbac/V2_EDIT_API_ENABLED=True \
    -p rbac/V2_MIGRATION_APP_EXCLUDE_LIST="approval" \
    -p rbac/ROLE_CREATE_ALLOW_LIST="remediations,inventory,policies,advisor,vulnerability,compliance,automation-analytics,notifications,patch,integrations,ros,staleness,config-manager,idmsvc" \
    -p kessel-relations/SPICEDB_QUANTIZATION_INTERVAL=2.5s \
    -p kessel-relations/SPICEDB_QUANTIZATION_STALENESS_PERCENT=0 \
    -p host-inventory/BYPASS_RBAC=false \
    -p host-inventory/BYPASS_KESSEL=false

# bonfire sets RBAC_KAFKA_CONSUMER_TOPIC only on the service deployment.
# The DR reconciler runs as a Celery task on the worker, so the worker pod
# also needs the topic env var.  Patch the ClowdApp to propagate it.
echo "Patching ClowdApp to add RBAC_KAFKA_CONSUMER_TOPIC to worker pod..."

# Find the worker deployment index in the ClowdApp spec.
_worker_idx=$(oc get clowdapp rbac -o json \
  | jq '.spec.deployments | to_entries[] | select(.value.name == "worker-service") | .key')

if [[ -n "${_worker_idx}" ]]; then
  # Ensure the env array exists, then append the topic var.
  oc patch clowdapp rbac --type=json -p "[
    {\"op\":\"add\",\"path\":\"/spec/deployments/${_worker_idx}/podSpec/env/-\",
     \"value\":{\"name\":\"RBAC_KAFKA_CONSUMER_TOPIC\",
                \"value\":\"outbox.event.relations-replication-event\"}}
  ]" 2>/dev/null && echo "  ClowdApp patched — worker pod will restart." \
                   || echo "  Patch skipped (may already exist)."
else
  echo "  WARNING: Could not find worker-service deployment in ClowdApp."
fi
