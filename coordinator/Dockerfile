# Container for the coordinator on Kubernetes (GKE).
# Build context is the coordinator/ folder itself:
#   gcloud builds submit ./coordinator --tag <registry>/coordinator:v1
#
# This reproduces what `adk deploy cloud_run --with_ui` generates for us: the ADK
# web server (FastAPI + browser chat UI + the /run API) serving the `coordinator`
# agent package. `adk web AGENTS_DIR` scans AGENTS_DIR for agent packages, so the
# package is copied to /app/agents/coordinator and AGENTS_DIR is /app/agents.
FROM python:3.13-slim

WORKDIR /app

# Install deps first so this layer caches across code-only changes.
# google-adk brings FastAPI + uvicorn, so no separate server deps are needed.
COPY requirements.txt /app/agents/coordinator/requirements.txt
RUN pip install --no-cache-dir -r /app/agents/coordinator/requirements.txt

# App code (agent.py, __init__.py, sub_agents/, config.py, prompts.py).
# NOTE: .env is excluded by .dockerignore — no configuration is baked into the
# image. Every value (GOOGLE_CLOUD_PROJECT, ADK_MODEL, FORMATTER_A2A_URL,
# VERTEX_SEARCH_DATASTORE, DOCAI_*) is injected by the Deployment / Cloud Run
# service. See k8s/manifests.yaml.
COPY . /app/agents/coordinator/

# GKE Service targets 8080; adk web binds it. Shell-form CMD so ${PORT} expands;
# exec so adk/uvicorn is PID 1 and receives shutdown signals.
ENV PORT=8080
CMD exec adk web --host 0.0.0.0 --port ${PORT} /app/agents
