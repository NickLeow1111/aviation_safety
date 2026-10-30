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

## The map view

`agent/web/src/MapView.tsx` draws the events on a MapLibre canvas with a filter
rail (time window, severity, type, source) and a table that flies the map to the
row you click. Every marker can be interrogated: the detail panel shows the
event's `provenance`, so a dot is never just a dot.

Basemap is plain OpenStreetMap raster — no key, no account. That is fine for a
POC and **not** fine for production traffic: OSM's tile usage policy forbids it.
`BASEMAP_TILES` in `MapView.tsx` is the one line to change before this ships.

### Uploading events

Drop a CSV (or upload one) and the server returns a preview before anything is
written: each row with how its position was resolved and how its time was read,
the rows that cannot be placed listed with the reason, and — only then — an
Import button.

| what the file says | what happens |
|---|---|
| `lat`,`lon` columns | used directly |
| `1.3502, 103.9940` or `1°21'0.7"N 103°59'38.4"E` in either column | parsed as coordinates |
| `WSSS` / `SIN` | resolved against the aerodrome reference |
| `WSMC` (an alias) | resolved, and the alias is recorded in provenance |
| `near WSSS` | resolved to the field |
| `near WSSS 5NM` | **refused** — a radial without a bearing cannot be placed |
| `XXXX` or blank | **refused** — never guessed, never defaulted to (0, 0) |
| date with no time | accepted at 00:00 UTC, and the provenance says so |

Re-uploading the same file adds nothing: `external_ref` is a hash of the row, and
the unique index refuses the duplicate.

### Endpoints

```
GET  /api/events          bbox, since, until, types, severities, place, source, limit
GET  /api/events/places   aerodrome autocomplete
POST /api/events/preview  parse + geocode, write nothing
POST /api/events/import   parse + geocode, write everything that passed
```

Preview and import run the same parser — what the preview promises is what the
import writes.

## Live traffic (OpenSky)

Toggle **Live traffic** on the map and aircraft appear, refreshed every 15 s with a
10 s server-side cache — several viewers share one billed request.

OpenSky meters by **bounding-box area**, not call count: 1 credit up to 25 square
degrees, 400 credits/day anonymous, 4000/day with OAuth2 client credentials. So:

- the poller lives on the server, never in the browser;
- every request is booked against a daily budget in Postgres (`opensky_usage`), so
  a restart cannot silently reset the meter;
- a viewport larger than 25 sq° is **narrowed automatically** to a local box, and
  the UI says it did that, rather than showing an error nobody can act on;
- usage stops at 85% of the allowance, leaving headroom for a human using the same
  account.

Credentials are optional — without them the app runs anonymously on the smaller
budget. Put them in `agent/.env.local`:

```
OPENSKY_CLIENT_ID=...
OPENSKY_CLIENT_SECRET=...
```

Live ADS-B is **not surveillance grade**: 5–15 s updates, MLAT gaps, no TCAS, no
Mode C-only traffic. The aircraft card says so.

## The assistant

The chat panel answers from **what the user uploaded** plus the event store, and
cites its sources. The rule is the one this project already runs on: the database
decides facts, the model decides wording.

How an answer is built:

1. **Retrieval** — Postgres full-text search over `document_chunk` (no embeddings:
   the gateway has none, and for this corpus `tsvector` answers the same questions
   without shipping text to a third party), plus SQL aggregates over
   `safety_event` (counts by type, severity, source and place).
2. **Grounding** — both become numbered context blocks. The system prompt allows
   the model to quote them and nothing else, and requires a `[n]` citation for each
   claim.
3. **Answer** — streamed from the model gateway; the `[n]` references are shown
   under the answer as **Grounded in** chips pointing back at the file and chunk.

If the gateway is unavailable the assistant falls back to an **extractive answer**
— the retrieved text, quoted, labelled as extractive. It never invents a paragraph
to fill the gap.

Upload a `.md`, `.txt`, `.json` or `.csv` through **Upload file** and it is indexed
for citation; a CSV does both jobs (events *and* citable text).

### Endpoints

```
GET  /api/live              ?bbox=min_lat,min_lon,max_lat,max_lon
GET  /api/live/status       credential mode, credits used today
GET  /api/documents         corpus summary
POST /api/documents         index a file for citation
DEL  /api/documents/{id}
POST /api/assistant/stream  SSE: sources → deltas → final
```

Configure the gateway with `OPENCODE_GO_API_KEY` (and optionally
`OPENCODE_GO_BASE_URL`, `OPENCODE_GO_MODEL`). Without a key the assistant still
answers, extractively.

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
| 2 · map canvas, event table, CSV upload with geocode preview | done (CSV; XLSX refused with advice) |
| 3 · OpenSky live poller (server-side, credit-budgeted) | done — anonymous by default, OAuth2 upgradeable |
| 4 · derived detections (loss of separation, runway incursion) | after 3 |
| 5 · grounded assistant over uploaded files + event store | done (citations, fuzzy fallback) |

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
