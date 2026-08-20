#!/bin/bash
# Build the current local working tree into an image, push it to a quay repo,
# and deploy it to an ephemeral cluster in place of the default RBAC image.
#
# Usage:
#   QUAY_REPO=quay.io/{QUAY_USERNAME}/insights-rbac ./scripts/ephemeral/deploy-local-ephemeral.sh
#   DURATION=4h QUAY_REPO=quay.io/{QUAY_USERNAME}/insights-rbac ./scripts/ephemeral/deploy-local-ephemeral.sh
#
# Requires:
#   - podman logged in to QUAY_REPO's registry (podman login quay.io)
#   - QUAY_REPO set to a *public* quay repo (ephemeral pulls anonymously)
#   - bonfire + oc configured against the ephemeral cluster

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=../common/logging.sh
source "${SCRIPT_DIR}/../common/logging.sh"

QUAY_REPO="${QUAY_REPO:?Set QUAY_REPO, e.g. quay.io/rh-ee-ecasey/insights-rbac}"
DURATION="${DURATION:-8h}"
IMAGE_TAG="${IMAGE_TAG:-local-$(git -C "${REPO_ROOT}" rev-parse --short HEAD)-$(git -C "${REPO_ROOT}" diff --quiet && echo clean || echo dirty)}"

# insights-stage sometimes pins puptoo/storage-broker (host-inventory's
# required deps) to CI-built tags that have since been GC'd from quay.
# Set these to override with a known-good tag if the ref-env default 404s.
PUPTOO_TAG="${PUPTOO_TAG:-}"
STORAGE_BROKER_TAG="${STORAGE_BROKER_TAG:-}"
EXTRA_IMAGE_TAG_ARGS=()
if [[ -n "${PUPTOO_TAG}" ]]; then
  EXTRA_IMAGE_TAG_ARGS+=(--set-image-tag "quay.io/redhat-user-workloads/insights-management-tenant/insights-puptoo/insights-puptoo=${PUPTOO_TAG}")
fi
if [[ -n "${STORAGE_BROKER_TAG}" ]]; then
  EXTRA_IMAGE_TAG_ARGS+=(--set-image-tag "quay.io/redhat-services-prod/hcc-integrations-tenant/storage-broker=${STORAGE_BROKER_TAG}")
fi

log-info "Building ${QUAY_REPO}:${IMAGE_TAG} from local working tree..."
podman build "${REPO_ROOT}" -t "${QUAY_REPO}:${IMAGE_TAG}" --arch amd64

log-info "Pushing ${QUAY_REPO}:${IMAGE_TAG}..."
podman push "${QUAY_REPO}:${IMAGE_TAG}"

log-info "Deploying rbac + dependencies to ephemeral with local image..."
bonfire deploy host-inventory kessel rbac --source appsre --ref-env insights-stage \
    --duration "${DURATION}" \
    --set-parameter rbac/IMAGE="${QUAY_REPO}" \
    --set-image-tag "${QUAY_REPO}=${IMAGE_TAG}" \
    "${EXTRA_IMAGE_TAG_ARGS[@]}" \
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
log-info "Patching ClowdApp to add RBAC_KAFKA_CONSUMER_TOPIC to worker pod..."

_worker_idx=$(oc get clowdapp rbac -o json \
  | jq '.spec.deployments | to_entries[] | select(.value.name == "worker-service") | .key')

if [[ -n "${_worker_idx}" ]]; then
  oc patch clowdapp rbac --type=json -p "[
    {\"op\":\"add\",\"path\":\"/spec/deployments/${_worker_idx}/podSpec/env/-\",
     \"value\":{\"name\":\"RBAC_KAFKA_CONSUMER_TOPIC\",
                \"value\":\"outbox.event.relations-replication-event\"}}
  ]" 2>/dev/null && log-info "  ClowdApp patched — worker pod will restart." \
                   || log-warn "  Patch skipped (may already exist)."
else
  log-warn "  Could not find worker-service deployment in ClowdApp."
fi

# --ref-env pulls its own IMAGE/IMAGE_TAG for rbac from insights-stage and can
# clobber --set-parameter/--set-image-tag above. Force the local image onto
# the rbac deployments directly so pods don't fall back to a stale CI tag.
FULL_IMAGE="${QUAY_REPO}:${IMAGE_TAG}"
log-info "Forcing rbac deployments onto ${FULL_IMAGE}..."

for _dep in service worker-service; do
  _idx=$(oc get clowdapp rbac -o json \
    | jq --arg n "${_dep}" '.spec.deployments | to_entries[] | select(.value.name == $n) | .key')

  if [[ -n "${_idx}" ]]; then
    oc patch clowdapp rbac --type=json -p "[
      {\"op\":\"replace\",\"path\":\"/spec/deployments/${_idx}/podSpec/image\",\"value\":\"${FULL_IMAGE}\"}
    ]" && log-info "  ${_dep} -> ${FULL_IMAGE}" \
       || log-warn "  Failed to patch ${_dep} image."
  else
    log-warn "  Could not find ${_dep} deployment in ClowdApp."
  fi
done

log-info "Deployed. Verify with:"
log-info "  oc get pod -l pod=rbac-service -o jsonpath='{.items[0].spec.containers[0].image}'"
