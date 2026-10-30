"""FastAPI BFF for the Safety Intelligence Bot front-end.

Endpoints
---------
- GET  /healthz  -> always 200 ok (liveness)
- GET  /readyz   -> 200 if FOUNDRY_AGENT_NAME is set, else 503 with hint
- POST /chat     -> streams agent reply as SSE

Auth: managed identity (DefaultAzureCredential). No API keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import assistant
import envfile
import store
from api_assistant import router as assistant_router
from api_events import router as events_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("safety-intel-bot")

# Import the real agent lazily so an import-time failure (e.g. pyodbc / ODBC
# driver, missing Azure deps) does NOT crash uvicorn before it can bind the
# port. Instead we capture the error and surface it via /readyz and /chat, so
# the container starts, the startup probe passes, and the logs show the cause.
# Credentials live in agent/.env.local; read them before anything asks for them.
envfile.load()

_AGENT_IMPORT_ERROR: str | None = None
try:
    from agent import run_chat_stream
except Exception as _e:  # noqa: BLE001
    run_chat_stream = None  # type: ignore[assignment]
    _AGENT_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    log.exception("Failed to import agent module at startup")


# Module workers must be served as JavaScript or the browser refuses to start them,
# and not every system mime database maps .mjs.
mimetypes.add_type("text/javascript", ".mjs")

app = FastAPI(title="Safety Intelligence Bot")
app.include_router(events_router)
app.include_router(assistant_router)


def _demo_mode_enabled() -> bool:
    return os.environ.get("SAFETY_INTEL_DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}

# Built React SPA (web/ -> static/ via `npm run build`).
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_STATIC_AVAILABLE = os.path.isdir(_STATIC_DIR) and os.path.isfile(
    os.path.join(_STATIC_DIR, "index.html")
)


class ChatIn(BaseModel):
    session_id: str
    message: str


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict:
    """Report what the chat actually depends on.

    The chat is served by the local grounded assistant — uploaded corpus, event
    store, model gateway — not by the Foundry agent. Reporting a missing Azure SDK
    as a red failure while the assistant answers perfectly well was telling the
    user the wrong subsystem was broken. The Foundry details stay in the payload
    as information, not as a failure.
    """
    if _demo_mode_enabled():
        return {"status": "ready", "agent_name": "demo-mode"}

    try:
        with store.connect() as conn:
            corpus = assistant.corpus_summary(conn)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"event store unavailable: {exc}") from exc

    return {
        "status": "ready",
        "agent_name": "grounded-assistant",
        "assistant": "grounded",
        "model": corpus["model"],
        "documents": corpus["document_count"],
        "chunks": corpus["chunk_count"],
        "events": corpus["event_count"],
        "foundry_agent": os.environ.get("FOUNDRY_AGENT_NAME"),
        "foundry_import_error": _AGENT_IMPORT_ERROR,
    }


@app.post("/chat")
async def chat(body: ChatIn):
    if not _demo_mode_enabled() and not os.environ.get("FOUNDRY_AGENT_NAME"):
        raise HTTPException(503, "FOUNDRY_AGENT_NAME not set; see /readyz")

    async def stream():
        try:
            if _demo_mode_enabled():
                # Imported here, not at module scope: mock_chat pulls in tools/, which
                # pulls in pyodbc, and a missing ODBC driver should not stop uvicorn
                # from binding the port and serving the map API.
                from mock_chat import run_mock_chat_stream

                runner = run_mock_chat_stream
            elif run_chat_stream is None:
                raise RuntimeError(
                    f"Agent module unavailable: {_AGENT_IMPORT_ERROR}"
                )
            else:
                runner = run_chat_stream
            async for event in runner(body.session_id, body.message):
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0)
        except Exception as e:  # noqa: BLE001
            log.exception("agent run failed")
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# Mount the React SPA last so /chat, /healthz, /readyz keep priority.
if _STATIC_AVAILABLE:
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_STATIC_DIR, "assets")),
        name="assets",
    )

    @app.get("/")
    async def _spa_root() -> FileResponse:
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

    @app.get("/{spa_path:path}")
    async def _spa_catchall(spa_path: str) -> FileResponse:
        # Serve a real file if it exists (favicon, etc.); otherwise fall back to index.html.
        candidate = os.path.join(_STATIC_DIR, spa_path)
        if os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))
else:
    log.info("static/ not found — SPA disabled (API-only mode)")
