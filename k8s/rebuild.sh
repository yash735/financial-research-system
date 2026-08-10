#!/usr/bin/env bash
# =============================================================================
# REBUILD — bring the whole stack back up on GKE (~5-7 min).
# Creates the cluster and deploys. It does NOT rebuild container images — those
# already live in Artifact Registry, so this just pulls them. The datastore and
# IAM survive teardown, so they work as-is.
#
#   ./k8s/rebuild.sh                 # deploys the :v1 images
#   IMAGE_TAG=v3 ./k8s/rebuild.sh    # deploys the :v3 images
#
# If you changed the CODE since last time, run ./k8s/build-images.sh FIRST.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=k8s/config.sh
. "$SCRIPT_DIR/config.sh"

TAG="${IMAGE_TAG:-v1}"
RENDERED="$(mktemp -t adk-manifests.XXXXXX).yaml"
trap 'rm -f "$RENDERED"' EXIT

echo "==> 1/4  Rendering manifests for project '$PROJECT' (tag :$TAG)…"
# manifests.yaml holds no project-specific identifiers — they are substituted
# here from .env so the file is safe to publish.
sed \
  -e "s|__IMAGE_BASE__|$BASE|g" \
  -e "s|__IMAGE_TAG__|$TAG|g" \
  -e "s|__GSA_EMAIL__|$GSA_EMAIL|g" \
  -e "s|__PROJECT_ID__|$PROJECT|g" \
  -e "s|__LOCATION__|${GOOGLE_CLOUD_LOCATION:-us-central1}|g" \
  -e "s|__MODEL__|${ADK_MODEL:-gemini-2.5-flash}|g" \
  -e "s|__DATASTORE__|${VERTEX_SEARCH_DATASTORE:-}|g" \
  -e "s|__DOCAI_LOCATION__|${DOCAI_LOCATION:-us}|g" \
  -e "s|__DOCAI_PROCESSOR_ID__|${DOCAI_PROCESSOR_ID:-}|g" \
  "$SCRIPT_DIR/manifests.yaml" > "$RENDERED"

echo "==> 2/4  Creating the GKE cluster with Workload Identity ON (~5 min)…"
gcloud container clusters create "$CLUSTER" \
  --project="$PROJECT" --zone="$ZONE" \
  --num-nodes=1 --machine-type="$MACHINE_TYPE" \
  --workload-pool="$PROJECT.svc.id.goog"

echo "==> 3/4  Wiring kubectl to the new cluster…"
gcloud container clusters get-credentials "$CLUSTER" --zone="$ZONE" --project="$PROJECT"

echo "==> 4/4  Deploying (pulls the images already in Artifact Registry)…"
kubectl apply -f "$RENDERED"

echo "==> Waiting for both services to be ready…"
kubectl -n adk rollout status deploy/formatter-agent --timeout=180s
kubectl -n adk rollout status deploy/webapp          --timeout=240s

echo "==> Waiting for the public IP (LoadBalancer)…"
IP=""
for _ in $(seq 1 30); do
  IP=$(kubectl -n adk get svc webapp -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  [ -n "$IP" ] && break
  sleep 5
done

echo
echo "================================================================="
echo " READY.  Open this in a browser:"
echo "     http://${IP:-<pending — run: kubectl -n adk get svc webapp>}/"
echo "================================================================="
echo "(The IP is new on every rebuild — that's expected.)"
echo "When you're done:   ./k8s/teardown.sh"
