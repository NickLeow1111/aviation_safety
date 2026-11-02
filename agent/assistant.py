"""The grounded assistant behind the chat panel.

The pattern this project already runs on: **the database decides facts, the model
decides wording.** Retrieval happens in SQL — full-text over the uploaded corpus,
aggregates over `safety_event` — and what reaches the model is a numbered context
block it is only allowed to quote. A question the corpus cannot answer gets "the
uploaded files do not cover that", not a plausible paragraph.

Two answer paths:
  * with an API key, a streaming completion from the model gateway;
  * without one, an extractive answer assembled from the retrieved blocks, labelled
    as such. The fallback exists so the feature is demonstrable and honest rather
    than broken.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import detect
import documents
import event_query
import live
import rulebook
import store

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
# Where the gateway key lives on this box when it is not exported into the service
# environment. Read at call time; never logged, never returned through an API.
_KEY_FILES = (Path("/root/.hermes/.env"),)

SYSTEM_PROMPT = """You are the Safety Intelligence assistant for a civil aviation \
authority. You answer questions using ONLY the numbered CONTEXT blocks supplied \
with each question. The blocks come from files the user uploaded and from the \
event database.

Rules, in order of importance:
1. Every factual claim must be traceable to a block. Cite it as [n] immediately \
after the claim.
2. Never invent a number, date, time, coordinate, registration, callsign, airport \
code, or event count. If a figure is not in a block, say it is not available.
3. Counts in the Event database block are computed by SQL over the whole table — \
quote them exactly; never recompute or approximate them.
4. If the blocks do not answer the question, say so plainly in one sentence and \
state what would be needed. Do not pad, do not speculate, do not use general \
aviation knowledge as a substitute for the data.
5. Be concise: the answer first, then the supporting detail with citations.
6. When the question asks you to extract or list events, answer with a markdown table using
   exactly the rows in the Event database block — one row per event, no invented rows, no
   dropped rows. State the total ("12 of 12 shown") next to the table. If the block says
   zero matched, say zero and repeat the filters that were applied, so the user can see the
   question was understood even though nothing came back.
7. If a block says the system already applied a settings change, that change has happened — state it as applied ("separation is now 3 NM") and use the new numbers. You are not asking permission; the server already wrote them.
"""


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #

def api_key() -> str | None:
    key = os.environ.get("OPENCODE_GO_API_KEY")
    if key:
        return key.strip()
    for path in _KEY_FILES:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("OPENCODE_GO_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
        except OSError:
            continue
    return None


def _settings() -> tuple[str, str]:
    base = os.environ.get("OPENCODE_GO_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("OPENCODE_GO_MODEL", DEFAULT_MODEL)
    return base, model


# --------------------------------------------------------------------------- #
# retrieval
# --------------------------------------------------------------------------- #

def _event_facts(conn: Any, question: str) -> tuple[str, list[dict[str, Any]]]:
    """Numbers the model may quote, computed here and nowhere else."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM safety_event")
        total = cur.fetchone()[0]
        if not total:
            return "Event database: empty — no events have been imported yet.", []

        cur.execute("SELECT event_type, count(*) FROM safety_event GROUP BY 1 ORDER BY 2 DESC")
        by_type = cur.fetchall()
        cur.execute("SELECT severity, count(*) FROM safety_event GROUP BY 1 ORDER BY 2 DESC")
        by_severity = cur.fetchall()
        cur.execute("SELECT source, count(*) FROM safety_event GROUP BY 1 ORDER BY 2 DESC")
        by_source = cur.fetchall()
        cur.execute("SELECT place_icao, count(*) FROM safety_event WHERE place_icao IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 12")
        by_place = cur.fetchall()
        cur.execute("SELECT min(event_time), max(event_time) FROM safety_event")
        span = cur.fetchone()

    lines = [
        f"Event database: {total} events, {span[0]:%Y-%m-%d} to {span[1]:%Y-%m-%d} UTC.",
        "By type: " + ", ".join(f"{name} {count}" for name, count in by_type) + ".",
        "By severity: " + ", ".join(f"{name} {count}" for name, count in by_severity) + ".",
        "By source: " + ", ".join(f"{name} {count}" for name, count in by_source) + ".",
        "By place: " + ", ".join(f"{name} {count}" for name, count in by_place) + ".",
    ]

    # The rows the question is actually about. Type, place and window are read out of the
    # question and applied as SQL filters, so "go-arounds last week at WSSS" is a query
    # rather than the newest fifteen rows of anything at all.
    try:
        matched, sample, query = event_query.facts(conn, question, limit=60)
        lines.append("")
        lines.append(matched)
    except Exception as exc:  # noqa: BLE001 - a hint, not the contract
        sample, query = [], None
        lines.append(f"(the event query could not be built: {exc})")

    return "\n".join(lines), sample


def build_context(conn: Any, question: str, limit: int = 8) -> list[dict[str, Any]]:
    """Numbered context blocks: documents first, then the database facts."""
    blocks: list[dict[str, Any]] = []

    for hit in documents.search(conn, question, limit=limit):
        blocks.append(
            {
                "kind": "document",
                "label": f"{hit.filename} (chunk {hit.ordinal + 1})",
                "text": hit.content,
            }
        )

    facts, _sample = _event_facts(conn, question)
    blocks.append({"kind": "database", "label": "Event database", "text": facts})
    return blocks


def render_context(blocks: Iterable[dict[str, Any]]) -> str:
    return "\n\n".join(f"[{index}] {block['label']}\n{block['text']}" for index, block in enumerate(blocks, start=1))


# --------------------------------------------------------------------------- #
# answering
# --------------------------------------------------------------------------- #

def _extractive(blocks: list[dict[str, Any]], question: str) -> str:
    """No model available: quote the retrieved text and say exactly that."""
    if not blocks:
        return "No files have been uploaded yet, so there is nothing to answer from."
    lines = [
        "The model gateway is not configured, so this is an extractive answer taken "
        "directly from the uploaded material — no wording was generated.",
        "",
    ]
    for index, block in enumerate(blocks[:3], start=1):
        excerpt = block["text"].strip().splitlines()
        lines.append(f"[{index}] {block['label']}: {excerpt[0][:400] if excerpt else ''}")
        for extra in excerpt[1:4]:
            lines.append(f"    {extra[:300]}")
    return "\n".join(lines)


def _completion_stream(messages: list[dict[str, str]], key: str) -> Iterable[str]:
    """Stream a completion.

    Three headers are not optional in practice, all learned the hard way:
      * `x-opencode-session` — the gateway refuses to route without a session id;
      * a browser-like `user-agent` — Cloudflare sits in front of the gateway and
        answers 403 to non-browser agents from datacenter IPs;
      * messages sent as block arrays — the plain-string form trips a Console Go
        quirk.
    """
    base, model = _settings()
    payload = {
        "model": model,
        "stream": True,
        "temperature": 0.2,
        "max_tokens": 2000,
        "messages": [
            {"role": message["role"], "content": [{"type": "text", "text": message["content"]}]}
            for message in messages
        ],
    }
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "x-opencode-session": f"safetyintel-{os.getpid()}-{int(time.time())}",
            "User-Agent": "Mozilla/5.0 (compatible; SafetyIntelligence/0.2)",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece


async def _bridge(generator: Iterable[str]) -> AsyncIterator[tuple[str, str]]:
    """Run a blocking generator in a thread and hand chunks back to asyncio."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    def worker() -> None:
        try:
            for piece in generator:
                loop.call_soon_threadsafe(queue.put_nowait, ("delta", piece))
            loop.call_soon_threadsafe(queue.put_nowait, ("done", ""))
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
            loop.call_soon_threadsafe(queue.put_nowait, ("error", f"{type(exc).__name__}: {exc}"))

    threading.Thread(target=worker, daemon=True).start()
    while True:
        kind, value = await queue.get()
        if kind == "done":
            return
        if kind == "error":
            yield ("error", value)
            return
        yield ("delta", value)


async def _live_block(conn: Any, live_request: dict[str, Any] | None) -> dict[str, Any] | None:
    """Grounding on the traffic picture, when the user asked for it.

    Only when the map's live layer is on: an assistant that quietly spends OpenSky
    credits to answer a question about last year's events is spending the budget on
    something nobody asked for.
    """
    if not live_request or not live_request.get("enabled"):
        return None
    bbox = live_request.get("bbox")
    if not bbox or len(bbox) != 4:
        return None
    try:
        payload = await live.snapshot(conn, list(bbox))
    except Exception as exc:  # noqa: BLE001 - say it is missing, do not invent traffic
        return {
            "kind": "live",
            "label": "Live traffic",
            "text": f"Live traffic could not be read ({type(exc).__name__}: {exc}). "
            "Any statement about current aircraft would be unsourced.",
        }

    return {
        "kind": "live",
        "label": f"Live traffic ({payload.get('count', 0)} aircraft, {payload.get('age_seconds', 0):.0f}s old)",
        "text": live.live_context_block(payload),
    }


async def stream_answer(
    conn: Any,
    question: str,
    live_request: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE-ready events: sources → deltas → final (or error)."""
    question = (question or "").strip()
    if not question:
        yield {"type": "error", "data": "Ask a question first."}
        return

    # Threshold changes are applied before retrieval, so the answer and the map
    # are scored against the same rules in the same turn.
    note: dict[str, Any] | None = None
    try:
        updated, changes = detect.parse_rules(question, rulebook.load(conn))
        if updated is not None and changes:
            rulebook.save(conn, updated)
            note = {
                "kind": "rules",
                "label": "Detection rules, just updated from your message",
                "text": (
                    "The system has ALREADY applied these changes before you answered: "
                    + ", ".join(changes)
                    + f". Full rules now in force: {updated.as_dict()}. "
                    "Report the change as applied and use the new numbers."
                ),
            }
    except Exception:  # noqa: BLE001 - a failed rule write must not kill the answer
        note = None

    blocks = build_context(conn, question)
    if note:
        blocks.insert(0, note)
    live_block = await _live_block(conn, live_request)
    if live_block:
        blocks.append(live_block)
    yield {
        "type": "sources",
        "data": [
            {"index": index, "kind": block["kind"], "label": block["label"], "preview": block["text"][:280]}
            for index, block in enumerate(blocks, start=1)
        ],
    }

    key = api_key()
    if not key:
        text = _extractive(blocks, question)
        yield {"type": "delta", "data": text}
        yield {"type": "final", "data": text}
        return

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"CONTEXT\n{render_context(blocks)}\n\nQUESTION\n{question}"},
    ]

    collected: list[str] = []
    async for kind, value in _bridge(_completion_stream(messages, key)):
        if kind == "error":
            fallback = _extractive(blocks, question)
            note = f"\n\n(the model call failed — {value}. Falling back to the extracted context.)"
            yield {"type": "delta", "data": fallback + note}
            yield {"type": "final", "data": fallback + note}
            return
        collected.append(value)
        yield {"type": "delta", "data": value}

    yield {"type": "final", "data": "".join(collected)}


def answer(conn: Any, question: str) -> dict[str, Any]:
    """Non-streaming helper for tests and CLI use. No live traffic: the async
    snapshot path lives in `stream_answer`."""
    blocks = build_context(conn, question)
    key = api_key()
    if not key:
        return {"answer": _extractive(blocks, question), "sources": blocks, "mode": "extractive"}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"CONTEXT\n{render_context(blocks)}\n\nQUESTION\n{question}"},
    ]
    text = "".join(_completion_stream(messages, key))
    return {"answer": text, "sources": blocks, "mode": "model"}


def corpus_summary(conn: Any) -> dict[str, Any]:
    docs = documents.list_documents(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM document_chunk")
        chunks = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM safety_event")
        events = cur.fetchone()[0]
    return {
        "documents": docs,
        "document_count": len(docs),
        "chunk_count": chunks,
        "event_count": events,
        "model": _settings()[1] if api_key() else "none (extractive answers only)",
    }
