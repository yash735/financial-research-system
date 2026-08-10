# Container for the formatter A2A service on Cloud Run.
# Build context is the formatter_agent/ folder itself:
#   gcloud run deploy formatter-agent --source ./formatter_agent
#
# The app is the Starlette app that to_a2a() built (a2a_app in agent.py). uvicorn
# serves it, binding to Cloud Run's injected $PORT. The agent card is published
# at /.well-known/agent-card.json — that's the URL the coordinator points at.
FROM python:3.13-slim

WORKDIR /app

# Install deps first so this layer caches across code-only changes.
COPY requirements.txt /app/formatter_agent/requirements.txt
RUN pip install --no-cache-dir -r /app/formatter_agent/requirements.txt

# App code (agent.py, __init__.py, config.py, prompt.py).
# NOTE: .env is excluded by .dockerignore — no configuration is baked into the
# image. GOOGLE_CLOUD_PROJECT, ADK_MODEL and the A2A_* card values are injected
# by the Cloud Run service / Deployment. See k8s/manifests.yaml.
COPY . /app/formatter_agent/

# Cloud Run sets $PORT (default 8080). Shell-form CMD so ${PORT} expands; exec so
# uvicorn is PID 1 and receives shutdown signals.
ENV PORT=8080
CMD exec uvicorn formatter_agent.agent:a2a_app --host 0.0.0.0 --port ${PORT}
