"""Turn a question into an event query, deterministically.

The assistant used to hand the model fifteen of whatever was newest and hope the
question was about those fifteen. That is not a retrieval problem — the store can
filter by type, place and time; nobody was asking it to. "Extract the go-around
events last week in WSSS" came back empty because:

* `_[A-Z]{4}` never matched a lowercase "wsss", so the place filter was dropped;
* the type test compared "go around" against a hyphenated "go-around", so the type
  filter was dropped too;
* nothing parsed "last week", so there was no time window at all;
* the sample was therefore the newest fifteen events of any type, at any airport.

So the answer was built from rows that had nothing to do with the question. This
module does the parsing with rules a person can read and check, and returns the
filters it applied so the answer can say what it looked for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import store

# --- vocabulary ------------------------------------------------------------ #

# Every canonical type, plus the words people actually use for it. Order matters
# only in that longer phrases are matched first, so "loss of separation" wins over
# the bare "separation".
TYPE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "go_around": ("go-around", "go around", "goaround", "aborted landing", "balked landing", "wave off", "wave-off"),
    "unstable_approach": ("unstable approach", "unstable approaches", "unstable final", "unstable"),
    "loss_of_separation": ("loss of separation", "los", "separation loss", "separation", "conflict"),
    "runway_incursion": ("runway incursion", "incursion"),
    "bird_strike": ("bird strike", "birdstrike", "bird"),
    "taxi_error": ("taxi error", "taxiway", "taxi"),
    "external_damage": ("external damage", "fod", "foreign object"),
    "fuel_spill": ("fuel spill", "spill"),
    "accident_investigation": ("accident investigation", "investigation", "accident"),
}

# Airport names a Singapore desk types instead of the ICAO code.
PLACE_NAMES: dict[str, str] = {
    "changi": "WSSS",
    "singapore changi": "WSSS",
    "seletar": "WSSL",
    "paya lebar": "WSAP",
    "kuala lumpur": "WMKK",
    "klia": "WMKK",
    "subang": "WMSA",
    "kuching": "WBGG",
    "penang": "WMKP",
}

# Four-letter tokens that are words, not airport codes. Without this "last week"
# becomes an aerodrome called WEEK and the answer is legitimate-looking nonsense.
_NOT_A_CODE = {
    "week", "weeks", "days", "day", "past", "last", "this", "that", "them", "they",
    "show", "list", "give", "with", "from", "what", "when", "were", "have", "into",
    "over", "near", "most", "more", "than", "only", "same", "also", "both", "some",
    "hour", "hours", "minute", "mins", "month", "year", "date", "time", "event",
    "events", "airport", "flight", "flights", "traffic", "report", "reports",
}

_UNIT = {
    "hour": "hours", "hours": "hours", "hr": "hours", "hrs": "hours",
    "day": "days", "days": "days",
    "week": "weeks", "weeks": "weeks",
    "month": "months", "months": "months",
}
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fourteen": 14,
                 "thirty": 30, "sixty": 60, "ninety": 90}


@dataclass
class EventQuery:
    """What the question asked for, in the store's own vocabulary."""

    types: list[str] = field(default_factory=list)
    place_icao: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 60
    heard: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.types or self.place_icao or self.since or self.until)

    def describe(self) -> str:
        parts: list[str] = []
        if self.types:
            parts.append("type " + " / ".join(t.replace("_", " ") for t in self.types))
        if self.place_icao:
            parts.append(f"place {self.place_icao}")
        if self.since and self.until:
            parts.append(f"{self.since:%Y-%m-%d %H:%M} to {self.until:%Y-%m-%d %H:%M}Z")
        elif self.since:
            parts.append(f"since {self.since:%Y-%m-%d %H:%M}Z")
        elif self.until:
            parts.append(f"before {self.until:%Y-%m-%d %H:%M}Z")
        return "; ".join(parts) or "no filter — the whole table"


# --- parsing --------------------------------------------------------------- #

def _types_in(question: str) -> list[str]:
    """Canonical types the question names. Longest phrase wins, one match per type."""
    found: list[tuple[int, str]] = []
    for canonical, phrases in TYPE_SYNONYMS.items():
        for phrase in phrases:
            # Plurals are how people write: "go-arounds", "bird strikes", "unstable approaches".
            pattern = r"\b" + re.escape(phrase).replace(r"\ ", r"[\s\-]+") + r"(?:s|es)?\b"
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                found.append((match.start(), canonical))
                break
    # Keep every distinct type, in the order the question mentions them.
    seen: list[str] = []
    for _pos, canonical in sorted(found):
        if canonical not in seen:
            seen.append(canonical)
    return seen


def _place_in(question: str, known: set[str] | None = None) -> str | None:
    lowered = question.lower()
    for name, code in PLACE_NAMES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", lowered):
            return code
    # "at wsss" / "in wsss" is unambiguous even in lowercase; a bare four-letter word is not.
    for token in re.findall(r"\b(?:at|in|near|into|from|around)\s+([A-Za-z]{4})\b", question, re.IGNORECASE):
        if token.lower() not in _NOT_A_CODE:
            return token.upper()

    known = known or set()
    for token in re.findall(r"\b([A-Za-z]{4})\b", question):
        upper = token.upper()
        if token.lower() in _NOT_A_CODE:
            continue
        # A code counts if the writer capitalised it ("WSSS") or we know the airport.
        if token.isupper() or upper in known:
            return upper
    return None


def _window_in(question: str, now: datetime) -> tuple[datetime | None, datetime | None, list[str]]:
    lowered = question.lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    heard: list[str] = []

    def rel(count: int, unit: str) -> datetime:
        if unit == "hours":
            return now - timedelta(hours=count)
        if unit == "days":
            return now - timedelta(days=count)
        if unit == "weeks":
            return now - timedelta(weeks=count)
        return now - timedelta(days=30 * count)

    if re.search(r"\byesterday\b", lowered):
        return midnight - timedelta(days=1), midnight, ["yesterday"]
    if re.search(r"\btoday\b", lowered):
        return midnight, None, ["today"]
    if re.search(r"\blast night\b", lowered):
        return midnight - timedelta(hours=12), midnight, ["last night"]
    if re.search(r"\bthis week\b", lowered):
        start = midnight - timedelta(days=midnight.weekday())
        return start, None, ["this week"]
    if re.search(r"\b(this|current) month\b", lowered):
        return midnight.replace(day=1), None, ["this month"]
    if re.search(r"\b(last|past|previous) week\b", lowered):
        return now - timedelta(days=7), None, ["last week"]

    # "last 3 days", "past 24 hours", "over the last two weeks"
    match = re.search(
        r"\b(?:last|past|previous|recent)\s+(\d+|[a-z]+)\s+(hour|hours|hr|hrs|day|days|week|weeks|month|months)\b",
        lowered,
    )
    if match:
        raw = match.group(1)
        count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 1)
        unit = _UNIT[match.group(2)]
        return rel(count, unit), None, [f"last {count} {unit}"]

    match = re.search(r"\b(?:last|past|previous)\s+(month|year)\b", lowered)
    if match:
        unit = "months" if match.group(1) == "month" else "days"
        count = 1 if unit == "months" else 365
        return rel(count, unit), None, [f"last {match.group(1)}"]

    # Explicit dates: 2026-10-20, 20 Oct 2026
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", lowered)
    if iso:
        try:
            start = datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=timezone.utc)
            return start, None, [f"since {start:%Y-%m-%d}"]
        except ValueError:
            pass

    # "since Monday" — the weekday nearest behind us.
    match = re.search(r"\bsince\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    if match:
        names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        target = names.index(match.group(1))
        back = (midnight.weekday() - target) % 7 or 7
        return midnight - timedelta(days=back), None, [f"since {match.group(1)}"]

    return None, None, heard


def parse(question: str, now: datetime | None = None, known_places: set[str] | None = None) -> EventQuery:
    """Read a question the way the store filters, and say what was understood."""
    now = now or datetime.now(timezone.utc)
    types = _types_in(question)
    place = _place_in(question, known_places)
    since, until, heard = _window_in(question, now)
    heard = ([f"type {t.replace('_', ' ')}" for t in types] + ([f"place {place}"] if place else []) + heard)
    return EventQuery(types=types, place_icao=place, since=since, until=until, heard=heard)


# --- running --------------------------------------------------------------- #

@dataclass
class Result:
    query: EventQuery
    rows: list[dict[str, Any]]
    total: int
    truncated: bool
    by_place: list[tuple[str | None, int]]
    first: datetime | None
    last: datetime | None


def _known_places(conn: Any) -> set[str]:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT place_icao FROM safety_event WHERE place_icao IS NOT NULL")
            return {row[0] for row in cur.fetchall()}
    except Exception:  # noqa: BLE001 - a hint, not the contract
        return set()


def run(conn: Any, query: EventQuery, limit: int = 60) -> Result:
    """Run the filters and count the whole match, not just the page handed over."""
    filters: dict[str, Any] = {}
    if query.types:
        filters["types"] = query.types
    if query.place_icao:
        filters["place_icao"] = query.place_icao
    if query.since:
        filters["since"] = query.since
    if query.until:
        filters["until"] = query.until

    query.limit = limit
    rows = store.query_events(conn, limit=limit + 1, **filters)
    truncated = len(rows) > limit
    rows = rows[:limit]

    counts: dict[str, int] = store.count_by(conn, "place_icao", **filters)
    by_place = sorted(((place, count) for place, count in counts.items()), key=lambda item: item[1], reverse=True)
    total = sum(count for _place, count in by_place) if by_place else len(rows)

    times = [row["event_time"] for row in rows if row.get("event_time")]
    return Result(
        query=query,
        rows=rows,
        total=total,
        truncated=truncated,
        by_place=by_place[:6],
        first=min(times) if times else None,
        last=max(times) if times else None,
    )


def render(result: Result, max_rows: int = 40) -> str:
    """The fact block: what was asked for, how much there is, and the rows themselves."""
    query = result.query
    lines = [f"Event query applied: {query.describe()}."]
    if query.heard:
        lines.append("Understood from the question: " + ", ".join(query.heard) + ".")

    if result.total == 0:
        lines.append("Matching events: none. That is a real zero, not a failure — these filters matched nothing.")
        return "\n".join(lines)

    lines.append(f"Matching events: {result.total} in total.")
    if result.by_place:
        parts = [f"{place or 'unplaced'} {count}" for place, count in result.by_place]
        lines.append("By place: " + ", ".join(parts) + ".")

    shown = result.rows[:max_rows]
    lines.append(f"Rows (newest first, {len(shown)} of {result.total}):")
    for row in shown:
        callsigns = ", ".join(row.get("callsigns") or []) or "—"
        lines.append(
            f"  {row['event_time']:%Y-%m-%d %H:%M}Z · {str(row['event_type']).replace('_', ' ')} · "
            f"{row['severity']} · {row.get('place_icao') or 'n/a'} · {callsigns} · "
            f"source {row.get('source')}"
            + (f" · {row['separation_nm']} NM" if row.get("separation_nm") is not None else "")
        )
    if result.truncated or result.total > len(shown):
        lines.append(
            f"Only the newest {len(shown)} are listed here. There are {result.total} in total; "
            "say so rather than implying the list is complete."
        )
    return "\n".join(lines)


def facts(conn: Any, question: str, limit: int = 60) -> tuple[str, list[dict[str, Any]], EventQuery]:
    """Convenience for the assistant: parse, run, render, and hand back the rows too."""
    query = parse(question, known_places=_known_places(conn))
    result = run(conn, query, limit=limit)
    return render(result), result.rows, query
