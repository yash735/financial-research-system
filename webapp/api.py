"""HTTP API: capabilities, document upload/list/delete, and the chat stream."""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from sse_starlette.sse import EventSourceResponse

from . import config, ingest
from .state import AppState
from .trace import TraceMapper

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _state(request: Request) -> AppState:
    return request.app.state.app_state


def _friendly_error(exc: Exception) -> str:
    """Turn a model/backend failure into something a viewer can act on.

    A raw `_ResourceExhaustedError: 429 RESOURCE_EXHAUSTED {...}` in a toast is
    useless to anyone watching a demo, and quota exhaustion is the single most
    likely failure after a burst of questions.
    """
    text = str(exc)
    lowered = text.lower()

    if "429" in text or "resource_exhausted" in lowered or "quota" in lowered:
        return (
            "Vertex AI quota exhausted — too many requests in a short window. "
            "Wait a minute and ask again; nothing is broken."
        )
    if "403" in text or "permission_denied" in lowered:
        return (
            "Permission denied by Google Cloud. The service account is likely "
            "missing roles/aiplatform.user, roles/discoveryengine.viewer or "
            "roles/documentai.apiUser."
        )
    if "404" in text and "model" in lowered:
        return (
            f"Model not found: {os.environ.get('ADK_MODEL', 'unset')!r}. It must "
            "be a Vertex publisher model id — AI Studio aliases 404 on Vertex."
        )
    if "deadline" in lowered or "timeout" in lowered:
        return "The model call timed out. Try again, or ask a narrower question."
    return f"{type(exc).__name__}: {text[:300]}"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
@router.get("/health")
async def health(request: Request) -> dict:
    """What this instance can do. The UI renders its lane pills from this."""
    return {"ok": True, **_state(request).capabilities.as_dict()}


@router.post("/session")
async def create_session(request: Request) -> dict:
    session = _state(request).sessions.create()
    return {"session_id": session.session_id, "documents": []}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
@router.post("/documents")
async def upload_documents(
    request: Request,
    files: list[UploadFile] = File(...),
    session_id: str = Form(""),
) -> JSONResponse:
    """Accept uploads and start OCR in the background.

    Returns 202 with each document in `processing`. The browser polls
    GET /api/documents for progress — see ingest.py for why this is not a
    blocking call or a second stream.
    """
    state = _state(request)
    if not state.capabilities.documents:
        return JSONResponse(
            status_code=503,
            content={
                "error": state.capabilities.documents_reason
                or "Document uploads are not available."
            },
        )

    session = state.sessions.get_or_create(session_id)
    accepted, rejected = [], []

    for upload in files:
        data = await upload.read()
        name = upload.filename or "document"
        try:
            doc = await ingest.start_ingest(name, data, session, state.ocr_config)
            accepted.append(doc.manifest())
        except ingest.UploadError as exc:
            rejected.append({"filename": name, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("Upload failed for %s", name)
            rejected.append({"filename": name, "error": f"{type(exc).__name__}: {exc}"})

    return JSONResponse(
        status_code=202 if accepted else 400,
        content={
            "session_id": session.session_id,
            "accepted": accepted,
            "rejected": rejected,
        },
    )


@router.get("/documents")
async def list_documents(request: Request, session_id: str = "") -> dict:
    session = _state(request).sessions.get(session_id)
    if not session:
        return {"session_id": session_id, "documents": []}
    return {"session_id": session.session_id, "documents": session.manifest()}


@router.delete("/documents/{doc_id}")
async def delete_document(request: Request, doc_id: str, session_id: str = "") -> dict:
    session = _state(request).sessions.get(session_id)
    removed = ingest.remove(doc_id, session) if session else False
    return {
        "removed": removed,
        "documents": session.manifest() if session else [],
    }


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@router.post("/chat")
async def chat(request: Request) -> EventSourceResponse:
    """Stream one turn as Server-Sent Events.

    SSE over POST rather than WebSockets: the stream is strictly server to
    client, and the only mid-stream client action is cancel, which a dropped
    connection already expresses. Read it with fetch() + getReader() — the
    browser's EventSource cannot issue a POST.
    """
    state = _state(request)
    body = await request.json()
    message = (body.get("message") or "").strip()
    session = state.sessions.get_or_create(body.get("session_id"))

    async def stream():
        mapper = TraceMapper()
        yield {"data": json.dumps(mapper.start())}

        if not message:
            yield {"data": json.dumps(mapper.error("Empty message."))}
            yield {"data": json.dumps(mapper.done())}
            return

        try:
            await state.ensure_adk_session(session.session_id)

            # Which documents this conversation owns. The text stays in the
            # document store; only this manifest travels through session state,
            # and the agent's tools validate every doc_id against it.
            manifest = session.ready_manifest()
            state_delta = {
                "uploaded_docs": manifest,
                "uploaded_docs_summary": session.summary(),
            }

            async with Aclosing(
                state.runner.run_async(
                    user_id=session.session_id,
                    session_id=session.session_id,
                    new_message=types.Content(
                        role="user", parts=[types.Part(text=message)]
                    ),
                    state_delta=state_delta,
                    run_config=RunConfig(streaming_mode=StreamingMode.SSE),
                )
            ) as events:
                async for event in events:
                    if await request.is_disconnected():
                        break
                    for payload in mapper.map(event):
                        yield {"data": json.dumps(payload)}

            # The gatherer's labelled raw material, for the "what actually
            # happened" disclosure in the trace panel.
            adk_session = await state.session_service.get_session(
                app_name=config.APP_NAME,
                user_id=session.session_id,
                session_id=session.session_id,
            )
            gathered = (
                (adk_session.state or {}).get("gathered_material", "")
                if adk_session
                else ""
            )
            if gathered:
                yield {"data": json.dumps(mapper.gathered(gathered))}

        except Exception as exc:  # noqa: BLE001
            log.exception("Chat turn failed")
            yield {"data": json.dumps(mapper.error(_friendly_error(exc), where="run"))}

        yield {"data": json.dumps(mapper.done())}

    return EventSourceResponse(stream(), ping=15)
