"""The corpus: whatever the user uploaded, chunked and searchable.

No embeddings. The model gateway this project uses has no embeddings endpoint, and
pretending otherwise would mean either a silent downgrade or a paid dependency.
Postgres full-text search over `document_chunk` answers the same questions on a
corpus this size, and it can tell you exactly which chunk a phrase came from —
which is the part that matters when an answer has to be checkable.

Rules:
  * content hash decides identity — re-uploading a file is a no-op;
  * every chunk keeps its document and ordinal, so a citation can point at the
    file it came from;
  * nothing is summarised at ingest time. The text the user gave us is the text
    the assistant is allowed to quote.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "db" / "002_live_and_documents.sql"

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

_TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".json", ".log", ".yaml", ".yml", ".csv", ".tsv")
_SCHEMA_READY = False


def ensure_schema(conn: Any) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with conn.cursor() as cur:
        cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    _SCHEMA_READY = True


def classify(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith((".csv", ".tsv")):
        return "table"
    if lowered.endswith(_TEXT_SUFFIXES):
        return "text"
    return "other"


def decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("file is not readable text")


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on paragraph boundaries where possible, then by size.

    Overlap is deliberate: a sentence that straddles a boundary is exactly the
    sentence someone will search for.
    """
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []

    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(cursor + size, len(text))
        if end < len(text):
            window = text[cursor:end]
            # Prefer a paragraph, then a sentence, then any whitespace: a chunk that
            # begins mid-word comes back as a quote like "hin" and reads as a bug.
            boundary = max(window.rfind("\n\n"), window.rfind(". "))
            if boundary <= size * 0.5:
                boundary = max(window.rfind(" "), window.rfind("\n"))
            if boundary > size * 0.5:
                end = cursor + boundary + 1
        chunks.append(text[cursor:end].strip())
        if end >= len(text):
            break
        cursor = max(end - overlap, cursor + 1)
    return [chunk for chunk in chunks if chunk]


def render_table(text: str, filename: str) -> tuple[list[str], int]:
    """Turn a CSV into readable chunks the assistant can quote.

    A raw CSV is a poor search target — 'runway_incursion' appears once per row and
    every row looks alike. Rendering each row as 'column: value' lines makes the
    content greppable and, more importantly, quotable back to the user.
    """
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return [], 0

    header = [h.strip() or f"column_{i + 1}" for i, h in enumerate(header)]
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return [], 0

    blocks: list[str] = []
    for start in range(0, len(rows), 25):
        lines = [f"{filename} — rows {start + 1}–{min(start + 25, len(rows))}"]
        for row in rows[start : start + 25]:
            pairs = (f"{header[i]}: {cell.strip()}" for i, cell in enumerate(row) if i < len(header) and cell.strip())
            lines.append(" | ".join(pairs))
        blocks.append("\n".join(lines))
    return blocks, len(rows)


def store_document(conn: Any, filename: str, content: bytes) -> dict[str, Any]:
    """Index a file. Returns the document row plus what was stored/duplicated."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not content.strip():
        raise ValueError("file is empty")

    ensure_schema(conn)
    digest = hashlib.sha256(content).hexdigest()
    kind = classify(filename)

    with conn.cursor() as cur:
        cur.execute("SELECT id, filename, uploaded_at FROM document WHERE sha256 = %s", (digest,))
        existing = cur.fetchone()
    if existing:
        return {
            "duplicate": True,
            "document_id": existing[0],
            "filename": existing[1],
            "uploaded_at": existing[2].isoformat() if existing[2] else None,
        }

    text = decode(content)
    if kind == "table":
        chunks, row_count = render_table(text, filename)
    else:
        chunks, row_count = chunk_text(text), None

    if not chunks:
        raise ValueError("no readable content found in that file")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO document (filename, sha256, kind, bytes, row_count, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (filename, digest, kind, len(content), row_count, json.dumps({"chunks": len(chunks)})),
        )
        document_id = cur.fetchone()[0]
        with conn.cursor() as inner:
            inner.executemany(
                "INSERT INTO document_chunk (document_id, ordinal, content) VALUES (%s, %s, %s)",
                [(document_id, index, chunk) for index, chunk in enumerate(chunks)],
            )

    return {
        "duplicate": False,
        "document_id": document_id,
        "filename": filename,
        "kind": kind,
        "chunks": len(chunks),
        "row_count": row_count,
        "bytes": len(content),
    }


@dataclass
class Hit:
    document_id: int
    filename: str
    ordinal: int
    content: str
    rank: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "filename": self.filename,
            "chunk": self.ordinal,
            "rank": round(float(self.rank), 4),
            "content": self.content,
        }


def search(conn: Any, question: str, limit: int = 8) -> list[Hit]:
    """Full-text search, with a fallback for questions FTS tokenises badly.

    `websearch_to_tsquery` accepts quoted phrases and OR — but a question made of
    stop words only ('what happened?') matches nothing, so the fallback scores
    chunks by shared distinct words instead of returning an empty context.
    """
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.filename, c.ordinal, c.content,
                   ts_rank(c.tsv, websearch_to_tsquery('english', %s)) AS rank
            FROM document_chunk c
            JOIN document d ON d.id = c.document_id
            WHERE c.tsv @@ websearch_to_tsquery('english', %s)
            ORDER BY rank DESC, d.uploaded_at DESC
            LIMIT %s
            """,
            (question, question, limit),
        )
        rows = cur.fetchall()

        if not rows:
            words = [w for w in re.findall(r"[a-zA-Z]{4,}", question.lower())][:6]
            if words:
                cur.execute(
                    """
                    SELECT d.id, d.filename, c.ordinal, c.content, 0.0 AS rank
                    FROM document_chunk c
                    JOIN document d ON d.id = c.document_id
                    WHERE c.content ILIKE ANY(%s)
                    ORDER BY d.uploaded_at DESC, c.ordinal
                    LIMIT %s
                    """,
                    ([f"%{word}%" for word in words], limit),
                )
                rows = cur.fetchall()

    return [Hit(row[0], row[1], row[2], row[3], row[4]) for row in rows]


def list_documents(conn: Any) -> list[dict[str, Any]]:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.filename, d.kind, d.bytes, d.row_count, d.uploaded_at,
                   count(c.id) AS chunks
            FROM document d
            LEFT JOIN document_chunk c ON c.document_id = d.id
            GROUP BY d.id
            ORDER BY d.uploaded_at DESC
            """
        )
        return [
            {
                "id": row[0],
                "filename": row[1],
                "kind": row[2],
                "bytes": row[3],
                "rows": row[4],
                "uploaded_at": row[5].isoformat() if row[5] else None,
                "chunks": row[6],
            }
            for row in cur.fetchall()
        ]


def delete_document(conn: Any, document_id: int) -> bool:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM document WHERE id = %s", (document_id,))
        return cur.rowcount > 0
