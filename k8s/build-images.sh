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

echo "==> Building + pushing web app image (includes the coordinator package)…"
# Built from the REPO ROOT: the web app imports webapp/, coordinator/ and
# document_ai/. The formatter above is built from its own folder because it is
# deployed independently.
gcloud builds submit "$ROOT" \
  --tag "$BASE/webapp:$TAG" --project="$PROJECT"

echo
echo "DONE. Both images are in Artifact Registry as :$TAG."
echo "Deploy them with:   IMAGE_TAG=$TAG ./k8s/rebuild.sh"
