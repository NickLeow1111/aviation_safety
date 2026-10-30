"""HTTP surface for live traffic, the uploaded corpus, and the grounded assistant."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import assistant
import detect
import documents
import live
import opensky
import rulebook
import store
from api_events import _parse_bbox, _read_upload, _serialise

router = APIRouter(prefix="/api", tags=["live", "documents", "assistant"])


# --------------------------------------------------------------------------- #
# live traffic
# --------------------------------------------------------------------------- #

@router.get("/live")
async def live_traffic(bbox: str = Query(..., description="min_lat,min_lon,max_lat,max_lon")) -> dict[str, Any]:
    box = _parse_bbox(bbox)
    assert box is not None
    try:
        with store.connect() as conn:
            return await live.snapshot(conn, box)
    except opensky.OpenSkyError as exc:
        raise HTTPException(429 if "budget" in str(exc) else 502, str(exc)) from exc


@router.get("/live/status")
def live_status() -> dict[str, Any]:
    try:
        with store.connect() as conn:
            usage = opensky.usage_today(conn)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"event store unavailable: {exc}") from exc

    return {
        "mode": usage.mode,
        "credential_configured": opensky.credential_mode() == "authenticated",
        "credits_used": usage.credits,
        "credits_budget": usage.budget,
        "credits_remaining": usage.remaining,
        "requests_today": usage.requests,
        "cache_ttl_seconds": opensky.CACHE_TTL_SECONDS,
        "max_bbox_square_degrees": opensky.MAX_BBOX_SQUARE_DEGREES,
        "cached_boxes": opensky.cache_size(),
    }


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #

@router.get("/documents")
def list_documents() -> dict[str, Any]:
    try:
        with store.connect() as conn:
            return assistant.corpus_summary(conn)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"event store unavailable: {exc}") from exc


@router.post("/documents")
async def upload_document(file: UploadFile = File(...)) -> dict[str, Any]:
    content, filename = await _read_upload(file)
    try:
        with store.connect() as conn:
            result = documents.store_document(conn, filename, content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"event store unavailable: {exc}") from exc
    return result


@router.delete("/documents/{document_id}")
def remove_document(document_id: int) -> dict[str, Any]:
    with store.connect() as conn:
        removed = documents.delete_document(conn, document_id)
    if not removed:
        raise HTTPException(404, "no such document")
    return {"deleted": document_id}


# --------------------------------------------------------------------------- #
# assistant
# --------------------------------------------------------------------------- #

class AskIn(BaseModel):
    message: str
    session_id: str | None = None
    # Set by the client when the map's live layer is on, so the assistant can
    # ground on the same traffic picture the user is looking at.
    live_enabled: bool = False
    bbox: list[float] | None = None


class RulesIn(BaseModel):
    min_separation_nm: float | None = None
    min_vertical_ft: int | None = None
    max_speed_kt: int | None = None
    ignore_ground: bool | None = None
    enabled: bool | None = None


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@router.post("/assistant/stream")
async def assistant_stream(payload: AskIn) -> StreamingResponse:
    """Same wire format as the old /chat: `data: {json}` per event.

    The frontend parser did not need to change shape — only the endpoint name and
    the extra `sources` event.
    """
    question = (payload.message or "").strip()
    if not question:
        raise HTTPException(400, "message is required")

    async def events() -> AsyncIterator[str]:
        try:
            with store.connect() as conn:
                live_request = (
                    {"enabled": True, "bbox": payload.bbox} if payload.live_enabled and payload.bbox else None
                )
                async for event in assistant.stream_answer(conn, question, live_request):
                    yield _sse(event)
        except Exception as exc:  # noqa: BLE001 - a stream that dies must say why
            yield _sse({"type": "error", "data": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/detect/rules")
def get_rules() -> dict[str, Any]:
    with store.connect() as conn:
        return rulebook.load(conn).as_dict()


@router.put("/detect/rules")
def put_rules(body: RulesIn) -> dict[str, Any]:
    """Merge a partial update onto the stored rules and persist."""
    with store.connect() as conn:
        current = rulebook.load(conn)
        merged = detect.Rules(
            min_separation_nm=body.min_separation_nm if body.min_separation_nm is not None else current.min_separation_nm,
            min_vertical_ft=body.min_vertical_ft if body.min_vertical_ft is not None else current.min_vertical_ft,
            max_speed_kt=body.max_speed_kt if body.max_speed_kt is not None else current.max_speed_kt,
            ignore_ground=body.ignore_ground if body.ignore_ground is not None else current.ignore_ground,
            enabled=body.enabled if body.enabled is not None else current.enabled,
        )
        return rulebook.save(conn, merged).as_dict()


@router.get("/assistant/corpus")
def corpus() -> dict[str, Any]:
    with store.connect() as conn:
        return assistant.corpus_summary(conn)
