"""FastAPI application entry point.

    python -m uvicorn webapp.main:app --host 127.0.0.1 --port 8000

or just ./run.sh, which also starts the formatter A2A service.

This is a hand-built FastAPI app rather than ADK's get_fast_api_app(). That
helper serves the ADK developer UI and claims `/` plus a large set of routes;
this app is a product surface with its own UI, so it drives the agent through a
Runner directly and keeps the URL space its own.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .api import router
from .state import AppState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("webapp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Probe capabilities, then build an agent graph that matches them.

    Probing happens once, here, rather than per request — and before the agent
    is constructed, because an unavailable lane changes the graph rather than
    being handled at call time.
    """
    state = AppState()
    log.info("Probing capabilities…")
    state.probe_and_build()
    app.state.app_state = state

    caps = state.capabilities
    lanes = ", ".join(
        f"{name}={'on' if lane['enabled'] else 'off'}"
        for name, lane in caps.as_dict()["lanes"].items()
    )
    log.info("Lanes: %s", lanes)
    log.info("Formatter: %s (%s)", caps.formatter_mode, caps.formatter_url)
    if caps.formatter_mode == "local" and caps.formatter_error:
        log.warning(
            "Remote formatter unreachable (%s) — using the in-process fallback. "
            "Start it with: python -m uvicorn formatter_agent.agent:a2a_app "
            "--host localhost --port 8001",
            caps.formatter_error,
        )
    if not caps.datastore and caps.datastore_reason:
        log.info("Indexed-filings lane off: %s", caps.datastore_reason)
    if not caps.documents and caps.documents_reason:
        log.warning("Uploads unavailable: %s", caps.documents_reason)

    log.info("Ready on http://%s:%s", config.HOST, config.PORT)
    try:
        yield
    finally:
        await state.close()


app = FastAPI(
    title="Financial Research System",
    description=(
        "Multi-agent financial research: upload filings, OCR them with Document "
        "AI, and ask questions answered across uploaded documents, an indexed "
        "corpus, and live market data."
    ),
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.include_router(router)

if config.STATIC_DIR.is_dir():
    app.mount(
        "/static", StaticFiles(directory=config.STATIC_DIR), name="static"
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
