# Deployment

Three targets. All of them run the same agent code; only the host differs.

| | what you get | idle cost |
|---|---|---|
| **GKE** | public IP, both services in one cluster, in-cluster A2A | bills while the cluster exists — see [kubernetes.md](kubernetes.md) |
| **Cloud Run** | HTTPS URL per service, scales to zero | ~nothing |
| **Agent Engine** | managed, always-on agent runtime | bills while deployed |

Order always matters: **formatter first**, then anything that dials it.

## Configuration is injected, never baked

`.env` is in both `.dockerignore` files and is not committed. Every deployment
supplies configuration through the service environment:

```
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=<project>
GOOGLE_CLOUD_LOCATION=us-central1
ADK_MODEL=gemini-2.5-flash
FORMATTER_A2A_URL=<card url>
VERTEX_SEARCH_DATASTORE=<id or empty>
DOCAI_LOCATION=us
DOCAI_PROCESSOR_ID=<id>
```

Miss these and the container starts fine and then fails on the first model call.

## IAM: three different service accounts

This is the most common source of runtime 403s here, because each target runs as
a *different* identity and a grant on one does nothing for the others.

| target | runs as | needs |
|---|---|---|
| Cloud Run | `<projectnum>-compute@developer.gserviceaccount.com` | `aiplatform.user`, `discoveryengine.viewer`, `documentai.apiUser` |
| Agent Engine | `service-<projectnum>@gcp-sa-aiplatform-re.iam.gserviceaccount.com` | `aiplatform.user`, `discoveryengine.viewer` |
| GKE | your Workload Identity GSA | all three |

The formatter needs only `aiplatform.user` — it has no tools, no datastore and no
Document AI access, which is why its image carries no cloud SDKs either.

```bash
PROJECT=<your-project>
SA=<the service account for your target>
for ROLE in aiplatform.user discoveryengine.viewer documentai.apiUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" --role="roles/$ROLE" --condition=None
done
```

Grants take a couple of minutes to propagate. A 403 on the first call after
granting usually just needs a retry, not a redeploy.

## Cloud Run

```bash
PROJECT=<your-project>; REGION=us-central1

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# 1. formatter FIRST
gcloud run deploy formatter-agent \
  --source ./formatter_agent \
  --project="$PROJECT" --region="$REGION" --allow-unauthenticated \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT="$PROJECT",GOOGLE_CLOUD_LOCATION="$REGION",ADK_MODEL=gemini-2.5-flash

# 2. make its agent card advertise the PUBLIC url (env only, no rebuild)
URL=$(gcloud run services describe formatter-agent --project="$PROJECT" --region="$REGION" --format='value(status.url)')
HOST=${URL#https://}
gcloud run services update formatter-agent \
  --project="$PROJECT" --region="$REGION" \
  --update-env-vars A2A_HOST="$HOST",A2A_PORT=443,A2A_PROTOCOL=https

curl "$URL/.well-known/agent-card.json"   # "url" must be the run.app host, NOT localhost
```

**Step 2 is required.** `A2A_HOST/PORT/PROTOCOL` do not bind the server — they
are written into the agent card, and the coordinator dials whatever the card
says. Skip it and the card advertises `localhost`, the fetch succeeds, and the
RPC goes nowhere. (`:443` in the advertised URL is harmless; httpx normalises the
default port out of the Host header.)

The formatter is deployed `--allow-unauthenticated` here for demo convenience.
It holds no data and has no tools, so the exposure is Vertex quota, not
information — but it is still a public billable endpoint. Drop the flag and pass
an identity token to lock it down.

Then the web app, pointed at that card:

```bash
gcloud run deploy financial-research \
  --source . \
  --project="$PROJECT" --region="$REGION" --allow-unauthenticated \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT="$PROJECT",GOOGLE_CLOUD_LOCATION="$REGION",ADK_MODEL=gemini-2.5-flash,FORMATTER_A2A_URL="$URL/.well-known/agent-card.json",DOCAI_LOCATION=us,DOCAI_PROCESSOR_ID=<id>,VERTEX_SEARCH_DATASTORE=<id>
```

Note that uploaded documents live in the serving process's memory. Cloud Run
scales to zero and can run several instances, so an upload is not guaranteed to
be visible to the next request. For a single-user demo that is fine; for anything
else, pin `--min-instances=1 --max-instances=1` or move the store to Redis.

### Teardown

```bash
gcloud run services delete financial-research --project="$PROJECT" --region="$REGION"
gcloud run services delete formatter-agent    --project="$PROJECT" --region="$REGION"
gcloud run services list --project="$PROJECT" --region="$REGION"
```

## Agent Engine

Ships the agent only — no web app, no uploads. Useful for exposing the
coordinator as a managed API.

```bash
gcloud storage buckets create gs://<your-staging-bucket> --location=us-central1

adk deploy agent_engine \
  --project=<project> --region=us-central1 \
  --staging_bucket=gs://<your-staging-bucket> \
  --display_name=coordinator \
  ./coordinator
```

This works because `coordinator/` is self-contained: everything it imports lives
inside that folder. Keep it that way — a single import from the repo root breaks
this deploy and nothing else will warn you.

The uploaded-document lane will be inert (no web app to populate it), so set
`ENABLE_DOCUMENTS=false` and point `FORMATTER_A2A_URL` at a deployed formatter.

Delete when done:

```bash
gcloud ai reasoning-engines delete <RESOURCE_ID> --project=<project> --region=us-central1
```

## Checklist

1. Formatter deployed and its card shows a public URL, not `localhost`.
2. Runtime service account has all three roles.
3. Every `GOOGLE_*`, `DOCAI_*` and `FORMATTER_A2A_URL` variable set on the
   service — nothing is baked into the image.
4. `/api/health` shows the lanes you expect and `"formatter": "remote"`.
