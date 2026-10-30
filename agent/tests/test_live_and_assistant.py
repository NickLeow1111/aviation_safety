"""Unit tests for the live feed helpers, the document corpus, and the assistant.

No network, no database. The parts worth protecting are the ones that decide what
the user is told: how many credits a viewport costs, what a chunk contains, and
which block a claim is allowed to cite.
"""
from datetime import datetime, timezone

import pytest

import assistant
import documents
import opensky


# --------------------------------------------------------------------------- #
# OpenSky accounting
# --------------------------------------------------------------------------- #

def test_bbox_area_is_square_degrees():
    assert opensky.bbox_area((1.0, 103.0, 2.0, 105.0)) == pytest.approx(2.0)


def test_small_boxes_cost_one_credit():
    assert opensky.credits_for((1.10, 103.50, 1.60, 104.30)) == 1


def test_large_boxes_cost_more():
    # ~12 x 12 = 144 sq deg -> 1 + floor(144/25) = 6
    assert opensky.credits_for((0.0, 100.0, 12.0, 112.0)) == 6


def test_budget_leaves_headroom():
    usage = opensky.Usage(day=datetime.now(timezone.utc).date(), mode="anonymous", requests=0, credits=0)
    assert usage.budget == int(400 * opensky.BUDGET_FRACTION)
    assert usage.remaining == usage.budget

    spent = opensky.Usage(usage.day, "authenticated", requests=5, credits=3999)
    assert spent.budget == int(4000 * opensky.BUDGET_FRACTION)
    assert spent.remaining == 0  # never negative


def test_state_normalisation_uses_metres_per_second_and_feet():
    raw = [
        "8840f9", "BKP968  ", "Thailand", 1793352785, 1793352785,
        103.9501, 1.2328, 1379.22, False, 175.0, 92.4, 0.0,
        None, 1400.0, "7012", False, 0,
    ]
    aircraft = opensky._normalise_state(raw)  # noqa: SLF001 - the parser is the unit under test
    assert aircraft["callsign"] == "BKP968"
    assert aircraft["altitude_ft"] == round(1379.22 * 3.28084)
    assert aircraft["ground_speed_kt"] == round(175.0 * 1.94384)
    assert aircraft["on_ground"] is False


def test_credentials_default_to_anonymous(monkeypatch):
    # The test must not depend on whether this machine happens to hold a credential
    # file: neutralise the loader, then assert on the environment alone.
    monkeypatch.setattr(opensky.envfile, "load", lambda **_: [])
    monkeypatch.delenv("OPENSKY_CLIENT_ID", raising=False)
    monkeypatch.delenv("OPENSKY_CLIENT_SECRET", raising=False)
    assert opensky.credential_mode() == "anonymous"

    monkeypatch.setenv("OPENSKY_CLIENT_ID", "id")
    monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "secret")
    assert opensky.credential_mode() == "authenticated"


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #

def test_classify_by_extension():
    assert documents.classify("events.csv") == "table"
    assert documents.classify("bulletin.md") == "text"
    assert documents.classify("scan.pdf") == "other"


def test_chunking_keeps_every_word_and_overlaps():
    text = " ".join(f"word{i}" for i in range(600))
    chunks = documents.chunk_text(text, size=500, overlap=50)
    assert len(chunks) > 1
    joined = " ".join(chunks)
    for probe in ("word0", "word300", "word599"):
        assert probe in joined


def test_empty_text_produces_no_chunks():
    assert documents.chunk_text("   \n\n  ") == []


def test_table_rendering_is_greppable():
    csv_text = "date,location,type\n2026-10-12,WSSS,runway_incursion\n2026-10-13,WSAP,bird_strike\n"
    blocks, rows = documents.render_table(csv_text, "events.csv")
    assert rows == 2
    assert "location: WSSS" in blocks[0]
    assert "type: runway_incursion" in blocks[0]


def test_table_rendering_splits_long_files():
    header = "a,b\n"
    body = "".join(f"{i},{i * 2}\n" for i in range(60))
    blocks, rows = documents.render_table(header + body, "big.csv")
    assert rows == 60
    assert len(blocks) == 3  # 25 + 25 + 10


# --------------------------------------------------------------------------- #
# assistant
# --------------------------------------------------------------------------- #

def test_context_blocks_are_numbered_from_one():
    blocks = [
        {"kind": "document", "label": "bulletin.md (chunk 1)", "text": "runway incursion at WSSS"},
        {"kind": "database", "label": "Event database", "text": "6 events"},
    ]
    rendered = assistant.render_context(blocks)
    assert rendered.startswith("[1] bulletin.md")
    assert "[2] Event database" in rendered


def test_extractive_answer_says_it_is_extractive():
    blocks = [{"kind": "document", "label": "a.md (chunk 1)", "text": "line one\nline two"}]
    text = assistant._extractive(blocks, "what happened?")  # noqa: SLF001
    assert "extractive" in text.lower()
    assert "line one" in text


def test_extractive_answer_without_corpus_says_so():
    assert "nothing to answer from" in assistant._extractive([], "anything?")  # noqa: SLF001


def test_system_prompt_forbids_invention():
    lowered = assistant.SYSTEM_PROMPT.lower()
    assert "never invent" in lowered
    assert "cite" in lowered
    # The prompt must give the model a way out that is not a guess.
    assert "say so plainly" in lowered or "state what would be needed" in lowered


def test_api_key_absent_is_not_an_error(monkeypatch):
    monkeypatch.setattr(assistant, "_KEY_FILES", ())
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    assert assistant.api_key() is None
