#!/usr/bin/env bash
# =============================================================================
# Shared configuration for the k8s scripts.
#
# Sourced by build-images.sh / rebuild.sh / teardown.sh. Reads the repo-root
# .env so there is exactly one place to configure the project, and fails loudly
# rather than deploying into the wrong account.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

# Load the repo-root .env without clobbering anything already exported.
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GKE_REGION:-us-central1}"
ZONE="${GKE_ZONE:-us-central1-a}"
CLUSTER="${GKE_CLUSTER:-adk-cluster}"
REPO="${ARTIFACT_REPO:-adk-agents}"
MACHINE_TYPE="${GKE_MACHINE_TYPE:-e2-medium}"

# Google service account backing the Workload Identity binding. Needs
# roles/aiplatform.user, roles/discoveryengine.viewer and roles/documentai.apiUser.
GSA_NAME="${GKE_GSA_NAME:-adk-agents-gsa}"

if [ -z "$PROJECT" ]; then
  echo "ERROR: GOOGLE_CLOUD_PROJECT is not set." >&2
  echo "       Copy .env.example to .env and fill it in, or export it." >&2
  exit 1
fi

GSA_EMAIL="${GSA_NAME}@${PROJECT}.iam.gserviceaccount.com"
BASE="$REGION-docker.pkg.dev/$PROJECT/$REPO"
