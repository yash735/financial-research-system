# Container for the web app (Cloud Run or GKE).
#
# Build context is the REPO ROOT, because the web app imports three sibling
# packages: webapp/, coordinator/ and document_ai/. That is the opposite of
# ./coordinator and ./formatter_agent, which are each built from their own
# folder so they stay independently deployable.
#
#   gcloud run deploy financial-research --source .
#   gcloud builds submit . --tag <registry>/webapp:v1
FROM python:3.13-slim

WORKDIR /app

# Dependencies first so this layer caches across code-only changes.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application code. .dockerignore keeps out the venv, .env, caches, source PDFs
# and the OCR text — no configuration is baked into the image, and every value
# arrives from the service environment. See docs/deployment.md.
COPY webapp/ /app/webapp/
COPY coordinator/ /app/coordinator/
COPY document_ai/ /app/document_ai/

# Cloud Run injects $PORT; Kubernetes uses the default. Shell form so ${PORT}
# expands, exec so uvicorn is PID 1 and receives shutdown signals.
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
CMD exec python -m uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT}
