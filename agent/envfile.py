"""Load key=value files into the environment, once, before anything reads them.

The README told people to put OpenSky credentials in `agent/.env.local`, but
nothing ever read that file — the poller only looked at `os.environ`, so the app
silently stayed anonymous no matter what was written there. This closes that gap
without pulling in python-dotenv.

Rules, in order of what wins:
  1. a variable already set in the real environment always wins;
  2. `agent/.env` fills in anything still missing;
  3. `agent/.env.local` overrides `.env`, because that is where the secrets live.

Values are never logged. Malformed lines are skipped rather than raising: a typo in
a credentials file should not stop the server from booting.
"""
from __future__ import annotations

import os
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
CANDIDATES = (AGENT_DIR / ".env", AGENT_DIR / ".env.local")

_loaded = False


def _parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip the "export " prefix people paste from shell snippets.
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(force: bool = False) -> list[str]:
    """Populate os.environ from the env files. Returns the names it set.

    Names only — never values, so this is safe to log or print.
    """
    global _loaded
    if _loaded and not force:
        return []

    real_keys = {key for key in os.environ}
    applied: list[str] = []
    merged: dict[str, str] = {}

    for path in CANDIDATES:
        merged.update(_parse(path))

    for key, value in merged.items():
        if key in real_keys and os.environ.get(key):
            continue  # the real environment wins
        if value == "":
            continue
        os.environ[key] = value
        applied.append(key)

    _loaded = True
    return applied
