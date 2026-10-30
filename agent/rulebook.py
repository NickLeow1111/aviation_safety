"""Persistence for the user's detection thresholds.

One row, on purpose: the map, the API and the assistant must all apply the same
numbers. A threshold that differs between the thing that alerts and the thing that
explains the alert is how people stop trusting both.
"""
from __future__ import annotations

import json
from typing import Any

import detect
import documents


def load(conn: Any) -> detect.Rules:
    # The table ships in the live/document schema; apply it before the first read
    # so a fresh database answers with defaults instead of a 500.
    documents.ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT rules FROM detection_rules WHERE id = 1")
        row = cur.fetchone()
    return detect.Rules.from_json(row[0] if row else None)


def save(conn: Any, rules: detect.Rules) -> detect.Rules:
    documents.ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO detection_rules (id, rules, updated_at) VALUES (1, %s, now())
            ON CONFLICT (id) DO UPDATE SET rules = EXCLUDED.rules, updated_at = now()
            """,
            (json.dumps(rules.as_dict()),),
        )
    return rules
