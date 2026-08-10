# Kubernetes (GKE)

Two commands: one up, one down.

```bash
./k8s/rebuild.sh     # create the cluster, deploy, print the public IP  (~5-7 min)
./k8s/teardown.sh    # delete everything that costs money
```

## The mental model

1. **Dockerfile** — the recipe for the app's box.
2. **Image** — the built box (`build-images.sh`).
3. **Artifact Registry** — Google's warehouse that stores the boxes.
4. **Cluster** — the machines that pull boxes from the warehouse and run them.

Because the images already live in the warehouse, `rebuild.sh` does **not**
rebuild them. It recreates the cluster and redeploys, which is why it is fast.

## What costs money

| | |
|---|---|
| **Costs money — delete when idle** | the GKE **cluster** (a running VM) and its **LoadBalancer** (a public IP) |
| **Effectively free — keep** | images in Artifact Registry, the Vertex AI Search datastore, the GCS bucket, the Document AI processor, the IAM / Workload Identity setup |

Unlike Cloud Run, a GKE cluster does **not** scale to zero — it bills for as long
as it exists. Run it when you are using it, tear it down when you are not.

## What is deployed

| workload | exposure | why |
|---|---|---|
| `formatter-agent` | ClusterIP, internal only | the analyst service, reached over A2A at `http://formatter-agent:8080` |
| `coordinator` | LoadBalancer, public | the app |

Keeping the formatter internal is the point of the split: the A2A hop is real
network traffic between two independently scalable Deployments, but only one of
them is exposed.

Single replica each, deliberately. Session state and uploaded documents are held
in the serving process, so a second replica would split sessions across pods.

## IAM: Workload Identity

Both pods run as the Kubernetes service account `adk-agents`, annotated to
impersonate a Google service account. That GSA needs **three** roles:

```bash
PROJECT=<your-project>
GSA="adk-agents-gsa@$PROJECT.iam.gserviceaccount.com"

gcloud iam service-accounts create adk-agents-gsa --project="$PROJECT"

for ROLE in aiplatform.user discoveryengine.viewer documentai.apiUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$GSA" --role="roles/$ROLE" --condition=None
done

# let the KSA impersonate the GSA
gcloud iam service-accounts add-iam-policy-binding "$GSA" \
  --project="$PROJECT" --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:$PROJECT.svc.id.goog[adk/adk-agents]"
```

`roles/documentai.apiUser` is the one that is easy to miss — it is only needed
once uploads exist, so a cluster that worked before will start rejecting uploads
with a 403 without it.

This is one-time per project. The bindings survive teardown, which is why
rebuilding needs no re-wiring.

## Rebuilding after a code change

`rebuild.sh` reuses whatever images are already in the registry, so a code change
needs a build first — with a **new tag**:

```bash
./k8s/build-images.sh v2
IMAGE_TAG=v2 ./k8s/rebuild.sh
```

The tag is an argument rather than a constant on purpose. Overwriting one tag in
place means a live cluster with the default `imagePullPolicy: IfNotPresent` keeps
serving the *old* image after you "deploy" — silently, with no error anywhere.

## Manifests hold no identifiers

`k8s/manifests.yaml` contains `__PLACEHOLDER__` tokens, not project ids, so it is
safe to publish. `rebuild.sh` renders it from your `.env` into a temp file before
applying. Do not `kubectl apply` the file directly.

Configuration is injected through the Deployments' `env:` blocks. `.env` is
excluded from both images, so anything missing from the manifest is missing at
runtime.

## Operating

```bash
kubectl -n adk get pods -w
kubectl -n adk get svc coordinator          # EXTERNAL-IP
kubectl -n adk logs deploy/coordinator -f
kubectl -n adk logs deploy/formatter-agent -f
kubectl -n adk rollout restart deploy/coordinator
```

The public IP is new on every rebuild. That is normal — the LoadBalancer is
recreated with the cluster.

## Sizing

An `e2-medium` exposes only ~940m allocatable CPU and GKE's system pods already
reserve most of it, so the CPU *requests* are deliberately tiny — they are
scheduling reservations, not usage caps. Neither container sets a CPU limit, so
both burst freely on the node's mostly-idle vCPU. Memory limits are set, because
memory is the resource that actually needs bounding here: uploaded documents live
in the process.

## Verify

```bash
IP=$(kubectl -n adk get svc coordinator -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl -s "http://$IP/api/health" | python -m json.tool
```

`"formatter": {"mode": "remote"}` confirms the in-cluster A2A hop resolved. If it
says `local`, the coordinator could not reach the formatter Service — check that
the formatter's `A2A_HOST`/`A2A_PORT` match the Service name and port, since
those values are what its agent card advertises.

## Teardown

```bash
./k8s/teardown.sh
```

Deletes the namespace (releasing the load balancer cleanly) and then the cluster,
then lists clusters and forwarding rules so you can see nothing billable is left.
Images, datastore, bucket, processor and IAM are kept — that is what makes the
next rebuild fast. To go to true zero, the commented command at the bottom of the
script also removes the images.
