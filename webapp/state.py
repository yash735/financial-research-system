"""Application state: capability detection, the ADK runner, and browser sessions.

Built once during FastAPI's lifespan startup and held on `app.state`. The
capability probes run HERE rather than inside the coordinator, because probing
is I/O and the agent package must stay importable offline.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from coordinator import config as agent_config
from coordinator.agent import RESEARCH_MODES, build_root_agent
from coordinator.document_store import store
from coordinator.health import probe_datastore, probe_formatter
from document_ai.ocr import OcrConfig

from . import config


@dataclass
class Capabilities:
    """What this instance can actually do right now.

    Reported to the browser so the UI can show which lanes are live. A greyed-out
    lane with an explanation is much better than a lane that looks available and
    then fails.
    """

    market: bool = True
    datastore: bool = False
    documents: bool = False
    formatter_mode: str = "local"          # "remote" | "local"
    formatter_url: str = ""
    formatter_error: str = ""
    datastore_reason: str = ""
    documents_reason: str = ""
    model: str = ""

    def as_dict(self) -> dict:
        return {
            "lanes": {
                "market": {
                    "enabled": self.market,
                    "label": "Live market",
                    "detail": "google_search",
                },
                "datastore": {
                    "enabled": self.datastore,
                    "label": "Indexed filings",
                    "detail": "Vertex AI Search",
                    "reason": self.datastore_reason,
                },
                "documents": {
                    "enabled": self.documents,
                    "label": "Uploaded documents",
                    "detail": "Document AI OCR",
                    "reason": self.documents_reason,
                },
            },
            "formatter": {
                "mode": self.formatter_mode,
                "url": self.formatter_url,
                "error": self.formatter_error,
                "label": (
                    "Remote analyst (A2A)"
                    if self.formatter_mode == "remote"
                    else "In-process fallback"
                ),
            },
            "model": self.model,
            "limits": {
                "max_upload_mb": config.MAX_UPLOAD_MB,
                "max_docs": config.MAX_DOCS_PER_SESSION,
            },
        }


@dataclass
class BrowserSession:
    """One browser tab's conversation and the documents it owns."""

    session_id: str
    doc_ids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def manifest(self) -> list[dict]:
        """Records for the documents this session owns, newest last.

        Read from the document store each time so status and OCR progress are
        always current. Ids whose documents have been evicted drop out here,
        which is also how the agent's tools stop seeing them.
        """
        records = []
        for doc_id in self.doc_ids:
            doc = store.get(doc_id)
            if doc:
                records.append(doc.manifest())
        return records

    def ready_manifest(self) -> list[dict]:
        return [d for d in self.manifest() if d["status"] == "ready"]

    def summary(self) -> str:
        """One-line description injected into the router's instruction."""
        ready = self.ready_manifest()
        if not ready:
            return "none"
        return ", ".join(f"{d['filename']} ({d['page_count']} pages)" for d in ready)


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.Lock()

    def create(self) -> BrowserSession:
        session = BrowserSession(session_id=uuid.uuid4().hex)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> BrowserSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def get_or_create(self, session_id: str | None) -> BrowserSession:
        if session_id:
            existing = self.get(session_id)
            if existing:
                return existing
        return self.create()


class AppState:
    """Everything the request handlers need, assembled once at startup."""

    def __init__(self) -> None:
        self.capabilities = Capabilities(model=config.MODEL)
        self.sessions = SessionRegistry()
        self.ocr_config = OcrConfig.from_env()
        self.runners: dict[str, Runner] = {}
        self.session_service = InMemorySessionService()
        self._adk_sessions: set[str] = set()
        self._adk_lock = threading.Lock()

    # -- startup ------------------------------------------------------------
    def probe_and_build(self) -> None:
        """Detect what is available, then build a matching agent graph."""
        caps = self.capabilities

        # Formatter: probe the A2A card so a dead service degrades cleanly
        # instead of failing mid-answer.
        status = probe_formatter(agent_config.FORMATTER_A2A_URL, timeout=3.0)
        caps.formatter_mode = "remote" if status.reachable else "local"
        caps.formatter_url = status.advertised_url or agent_config.FORMATTER_A2A_URL
        caps.formatter_error = status.error

        # Datastore: honour the explicit on/off override, probe when "auto".
        setting = agent_config.ENABLE_DATASTORE
        if not agent_config.DATASTORE_PATH:
            caps.datastore = False
            caps.datastore_reason = "No datastore configured (VERTEX_SEARCH_DATASTORE)."
        elif setting == "off":
            caps.datastore = False
            caps.datastore_reason = "Disabled by ENABLE_DATASTORE=off."
        elif setting == "on":
            caps.datastore = True
        else:
            caps.datastore = probe_datastore(
                agent_config.DATASTORE_PATH, agent_config.PROJECT_ID, timeout=5.0
            )
            if not caps.datastore:
                caps.datastore_reason = (
                    "Datastore unreachable — check it exists and that this account "
                    "has roles/discoveryengine.viewer."
                )

        # Documents: needs Document AI configured, nothing else.
        if not agent_config.ENABLE_DOCUMENTS:
            caps.documents = False
            caps.documents_reason = "Disabled by ENABLE_DOCUMENTS."
        elif not self.ocr_config.is_configured:
            caps.documents = False
            caps.documents_reason = (
                "Document AI not configured — set "
                + " and ".join(self.ocr_config.missing())
                + " in .env."
            )
        else:
            caps.documents = True

        # One graph per research mode. Thinking budgets are fixed when an agent
        # is constructed, so a per-request toggle means building both up front
        # rather than mutating a live agent, which would race across concurrent
        # requests. Both runners share ONE session service and app_name, so the
        # conversation stays continuous when the user flips the toggle mid-chat.
        for mode in RESEARCH_MODES:
            self.runners[mode] = Runner(
                app_name=config.APP_NAME,
                agent=build_root_agent(
                    formatter=caps.formatter_mode,
                    enable_datastore=caps.datastore,
                    enable_documents=caps.documents,
                    mode=mode,
                ),
                session_service=self.session_service,
            )

    def runner_for(self, mode: str) -> Runner:
        """The runner for a research mode, falling back to normal."""
        return self.runners.get(mode) or self.runners["normal"]

    async def close(self) -> None:
        for runner in self.runners.values():
            await runner.close()

    # -- ADK sessions -------------------------------------------------------
    async def ensure_adk_session(self, session_id: str) -> None:
        """Create the ADK session for a browser session, once."""
        with self._adk_lock:
            if session_id in self._adk_sessions:
                return
            self._adk_sessions.add(session_id)
        await self.session_service.create_session(
            app_name=config.APP_NAME, user_id=session_id, session_id=session_id
        )
