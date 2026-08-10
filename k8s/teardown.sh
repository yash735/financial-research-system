#!/usr/bin/env bash
# =============================================================================
# TEARDOWN — stop paying.
# Deletes ONLY the things that cost money while idle: the GKE cluster (a running
# VM) and its LoadBalancer (a public IP). Everything needed for a fast rebuild
# is KEPT and is effectively free: container images in Artifact Registry, the
# Vertex AI Search datastore, the GCS bucket, the Document AI processor, and the
# IAM / Workload Identity setup.
#
# Unlike Cloud Run, a GKE cluster does not scale to zero — it bills while it
# exists. Run it when you're using it, tear it down when you're not.
#
#   ./k8s/teardown.sh
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=k8s/config.sh
. "$SCRIPT_DIR/config.sh"

echo "==> Pointing kubectl at the cluster (ok if it's already gone)…"
gcloud container clusters get-credentials "$CLUSTER" \
  --zone="$ZONE" --project="$PROJECT" 2>/dev/null || true

echo "==> 1/2  Deleting the workloads + LoadBalancer (releases the public IP cleanly)…"
kubectl delete namespace adk --ignore-not-found=true 2>/dev/null || true
sleep 10   # let GKE deprovision the load balancer before the cluster goes

echo "==> 2/2  Deleting the GKE cluster (this is what stops the bill)…"
gcloud container clusters delete "$CLUSTER" \
  --zone="$ZONE" --project="$PROJECT" --quiet || true

echo
echo "==> Sanity check — nothing billable should remain:"
echo "--- clusters (want: 'Listed 0 items') ---"
gcloud container clusters list --project="$PROJECT"
echo "--- leftover load-balancer forwarding rules (want: empty) ---"
gcloud compute forwarding-rules list --project="$PROJECT" 2>/dev/null || true
echo
echo "DONE. You are no longer paying for compute."
echo "Kept (pennies/month, needed for rebuild): images, datastore, bucket, processor, IAM."
echo "Rebuild anytime:   ./k8s/rebuild.sh"

# -----------------------------------------------------------------------------
# OPTIONAL — go to TRUE \$0 by deleting the images too. Only worth it if you are
# NOT rebuilding soon, because then rebuild.sh must be preceded by
# build-images.sh (a few extra minutes).
#   gcloud artifacts repositories delete "$REPO" --location="$REGION" --project="$PROJECT" --quiet
# The datastore, bucket and Document AI processor are within the free tier or
# have no idle cost — leave them.
# -----------------------------------------------------------------------------
