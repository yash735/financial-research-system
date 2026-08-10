#!/usr/bin/env bash
# =============================================================================
# BUILD IMAGES — the "Dockerfile -> image -> Artifact Registry" step.
# Only needed when you CHANGED THE CODE. Otherwise skip it: last build's images
# are still in Artifact Registry and rebuild.sh just reuses them.
#
#   ./k8s/build-images.sh            # builds and pushes :v1
#   ./k8s/build-images.sh v3         # builds and pushes :v3
#
# The tag is an ARGUMENT, not a constant. Overwriting one tag in place means a
# live cluster with imagePullPolicy: IfNotPresent keeps serving the OLD image
# after you "deploy" — silently. Bump the tag, then pass the same IMAGE_TAG to
# rebuild.sh.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=k8s/config.sh
. "$SCRIPT_DIR/config.sh"

TAG="${1:-${IMAGE_TAG:-v1}}"

echo "==> Project:  $PROJECT"
echo "==> Registry: $BASE"
echo "==> Tag:      $TAG"
echo

echo "==> Building + pushing formatter image…"
gcloud builds submit "$ROOT/formatter_agent" \
  --tag "$BASE/formatter-agent:$TAG" --project="$PROJECT"

echo "==> Building + pushing coordinator image…"
gcloud builds submit "$ROOT/coordinator" \
  --tag "$BASE/coordinator:$TAG" --project="$PROJECT"

echo
echo "DONE. Both images are in Artifact Registry as :$TAG."
echo "Deploy them with:   IMAGE_TAG=$TAG ./k8s/rebuild.sh"
