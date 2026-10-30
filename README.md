# Safety Intelligence — CAAS Safety Regulation

Regulator-side tooling for aviation safety intelligence: a chat agent over the
safety-regulation data mart, and (in progress) a map-based visualiser for safety
events driven by ADS-B.

```
SQL/      Synapse data-mart tables, views and synthetic seed data
db/       local PostgreSQL schema for the map event store + aerodrome reference
agent/    FastAPI BFF + Foundry agent + tools (nl2sql, doc_search, chart, dashboard)
agent/web React SPA that renders chat and canvas artifacts
doc/      design notes, deployment guides, demo prompts
```

## Two things live here

**1. The assistant (built).** `agent/app.py` is a FastAPI BFF that streams a
Foundry agent over SSE. The agent answers questions by calling local tools:
`nl2sql` (schema-grounded NL→T-SQL over the `vw_SafetyIntel_*` views),
`doc_search`, `chart_spec` (Vega-Lite) and `dashboard_spec` (the ops panels in
`agent/web/src/Canvas.tsx`). Auth is Entra ID only — no keys, no passwords.

**2. The map (in progress).** The same event facts, drawn geographically instead
of in a schematic. See "Event store" below.

## Event store

`db/001_safety_event.sql` creates `safety_event`: one row per event, whichever
producer wrote it.

| column | why it exists |
|---|---|
| `event_time` | `TIMESTAMPTZ`, not `DATE`. Loss of separation is measured in seconds. |
| `lat` / `lon` | first class. The data mart stores `Location` as free text and cannot be drawn. |
| `source` | `report` (filed), `upload` (user file), `derived` (computed from tracks). |
| `provenance` | a sentence naming the source and every derived field. |
| `metrics` | detection evidence: min separation, duration, threshold used. |
| `external_ref` | the producer's own id; unique per source, so re-running a seed or a detector cannot double the map. |

The rule the rest of the project already runs on carries into the map:
**the database decides facts, the model decides wording.** `agent/store.py` is
the only module that talks to Postgres, and every filter a user or a model can
express becomes a parameterised query there.

### Seed it

```bash
cd agent
uv venv .venv && uv pip install --python .venv/bin/python "psycopg[binary]" pytest
createdb safetyintel                      # or: su postgres -c "createdb -O $USER safetyintel"
.venv/bin/python seed_events.py --reset
```

Seeding derives two fields the source does not carry, and says so in
`provenance` on every row:

- **coordinates** — `Location` codes resolve against `db/aerodromes.json`
  (OurAirports reference points, public domain). Unknown codes are skipped and
  listed, never guessed.
- **time-of-day** — the source has `Occurrence_Date` only; the hour is derived
  deterministically from `Occurrence_ID` so a row always lands at the same time.

Points are nudged by at most 0.02° so years of reports at one aerodrome do not
collapse into a single dot — deterministic, disclosed, reversible.

### Query it

```python
import store
with store.connect() as conn:
    rows = store.query_events(conn, bbox=(1.20, 103.60, 1.50, 104.10), limit=200)
    counts = store.count_by_type(conn, bbox=(1.20, 103.60, 1.50, 104.10))
```

## Tests

```bash
cd agent && .venv/bin/python -m pytest tests/ -q
```

Pure logic only — no database, no Azure. `tests/conftest.py` stubs the heavy SDKs.

## Roadmap

| phase | state |
|---|---|
| 0 · repo hygiene (untrack `.env`, drop `.bak`, this README) | done |
| 1 · event store + seed + tests | done |
| 2 · map canvas, event table, CSV/XLSX upload with geocode preview | next |
| 3 · OpenSky live poller (server-side, credit-budgeted) | needs client credentials |
| 4 · derived detections (loss of separation, runway incursion) | after 3 |
| 5 · agent tools over the map (`geo_query`, `map_spec`, `explain_event`) | after 2 |

Deliberately out of scope for now: vector search (OpenSky embeddings aside, the
doc corpus is small), and any bulk historical ADS-B import — the free OpenSky
API bills `/tracks` and `/flights` by day partition, so history is a separate
decision, not a config flag.

## Local dev

```bash
cd agent
uvicorn app:app --reload --port 7700     # needs Foundry env vars, see agent/README.md
```

For UI-only preview without Foundry or Synapse, `agent/scripts/run_demo_mode.sh`
sets `SAFETY_INTEL_DEMO_MODE=1` and serves canned SSE responses.
