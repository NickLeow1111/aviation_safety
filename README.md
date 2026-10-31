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
budget. To add them, run the script on the machine that serves the app:

```bash
./scripts/set_opensky_creds.sh          # hidden prompts, never echoed or logged
./scripts/set_opensky_creds.sh --check  # is a credential present? no values shown
```

It writes `agent/.env.local` with mode 0600 (gitignored, verified), and the app
loads that file at boot (`agent/envfile.py`) — a real environment variable still
wins, `.env.local` overrides `.env`. Restart the app afterwards and check
`GET /api/live/status` reports `"mode": "authenticated"`.

**GitHub repository secrets do not configure a running server.** They are readable
only by Actions workflows, so storing them there does nothing until a workflow
deploys them — which needs a self-hosted runner on this host or inbound SSH.
Keep the live credentials on the machine; keep a copy in GitHub only if you also
want CI to deploy them.

Live ADS-B is **not surveillance grade**: 5–15 s updates, MLAT gaps, no TCAS, no
Mode C-only traffic. The aircraft card says so.

### Detection rules

The checks are yours to set, and the assistant reads them out of plain sentences:

> "flag anything closer than 2.5 nautical miles or faster than 500 knots"
> "set the separation minimum to 3 nm, speed limit to 450 knots"
> "turn off the separation checks"

| field | meaning | default |
|---|---|---|
| `min_separation_nm` | horizontal minimum between two aircraft | 3.0 NM |
| `min_vertical_ft` | vertical minimum; a pair must breach **both** to be reported | 1000 ft |
| `speed_bands` | ground-speed limit **per altitude band** | 250 kt below 10,000 ft, 480 kt above |
| `ignore_ground` | exclude aircraft on the ground | true |

Rules are stored once, in one row, because the map, the API and the assistant must
agree — a check that answers differently depending on who asked is how people stop
trusting both. Change them in the map's rule panel or in a sentence; both write the
same row, and the assistant applies a change *before* it answers, so the reply and
the map are scored against the same numbers in the same turn.

Speed limits are banded by altitude because a single number cannot be right: 450 kt
at 35,000 ft is ordinary cruise, 450 kt at 2,000 ft is an emergency. One global limit
either flags all of cruise (what it did) or none of the dangerous cases. Unknown
altitude gets the most permissive band rather than a guessed breach.

A pair is only a conflict when it is inside the horizontal **and** vertical minima.
Horizontally close but 3000 ft apart is legal and is not reported. Missing altitude
is reported as proximity with `vertical_ft: null` rather than assumed safe.

**It is not a conflict predictor.** ADS-B updates every 5–15 s and the data carries
no intent, route or vertical-rate trend, so a pair inside the threshold is reported
as *currently inside it*. Every result carries that disclaimer and the assistant is
instructed to repeat it.

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

### Predicted separation

A snapshot can only report a loss of separation once it has happened. `predict.py`
dead-reckons each aircraft along its reported track, ground speed and vertical rate,
then solves for the closest point of approach over the next two minutes — so the
question becomes *"these two will be 0.9 NM apart in 46 seconds"* instead of *"these
two were close a moment ago"*.

The assumptions are stated wherever the numbers appear: constant track, constant
speed, constant vertical rate. A turn invalidates it. Pairs already inside the
minimum are left to the snapshot check rather than listed twice, and a pair is only
predicted when it is genuinely converging.

### Manoeuvres from track history

`tracks.py` keeps ten minutes of position history per aircraft — in memory, pruned on
every write, gone on restart. That is enough to see the moves a single snapshot
cannot:

- **go-around** — descended toward the runway, then climbing away again below 3,500 ft
- **holding pattern** — turned through 300° inside a 4 NM circle
- **unstable approach** — below 1,200 ft and either too fast or descending far too steeply

A go-around is a normal, safe manoeuvre. It is reported because it is worth knowing
about, not because anything went wrong.

The same positions are written to Postgres (`db/003_history.sql`), so history outlives
the process:

- `aircraft_track` — one row per aircraft per poll, pruned after 7 days
- `track_event` — the manoeuvres read out of those tracks, de-duplicated per aircraft
  per kind within 15 minutes (a holding aircraft would otherwise be counted on every
  poll)

On startup the in-memory store is warmed from `aircraft_track`, so a restart does not
blind the checks: the first request after a restart already sees ten minutes of
history. `GET /api/tracks/history?days=7` returns the counts, and the assistant quotes
them — labelled as detector output, never confused with the reported events in
`safety_event`.

### Real events, not decoration

Detections are promoted into the same `safety_event` table as reports and uploads
(`promote.py`): observed losses of separation, go-arounds and unstable approaches land
with `source = 'derived'` and carry the arithmetic that produced them — separation in
NM, vertical gap in feet, the minima in force at the time, and the detector's name.

Three rules keep that record trustworthy:

- **Predictions never become events.** A projection is a prompt to look, not a record.
- **One row per pair or aircraft per half hour.** `external_ref` is deterministic, so
  the unique index absorbs re-observation instead of turning one hold into forty rows.
- **A go-around is normal operations**, stored as `low` severity with wording that
  says so. It is a statistic about an airport, not an incident list.

Filter the map with the Source chips (`report` / `upload` / `derived`) to see exactly
which conclusion came from where.

### Asking about the traffic, from the map

The map carries its own assistant panel on the right, so a question about what is on
screen does not mean switching views and losing the picture. It reads the current
map bounds, and only when the live layer is on — so a question costs the credits of
one cached snapshot rather than a fresh request per keystroke.

Questions it can actually answer, because the server computes the signals:

- *"Any unusual flight paths right now?"* — a turn sharper than 30° between two
  snapshots, an emergency squawk (7500/7600/7700), fast and low (<3000 ft above
  250 kt), extreme vertical rate (>6000 ft/min), excessive ground speed on the
  ground, or loitering slowly at altitude.
- *"Who is inside the separation minimum?"* — pairs inside the horizontal **and**
  vertical minima, named with the distance and vertical gap.
- *"What is the fastest aircraft in view?"*, *"what did the bulletin say about hold
  short lines?"* — the same assistant, the same citations.

The heuristics rank what to look at; they do not claim an aircraft is in difficulty,
and both the panel and the answer say so. A 30° turn between snapshots is not a
manoeuvre — it is a reason to look.

### Grounding on live traffic

When the map's live layer is on, the client sends `live_enabled` and the current
`bbox` with each question, and the assistant grounds on the **same cached snapshot
the map is drawing**, scored by the same rules — so it never explains a picture the
user is not looking at. With the layer off, nothing is fetched: live data costs
OpenSky credits and nobody asked for it.

### Endpoints

```
GET  /api/live              ?bbox=min_lat,min_lon,max_lat,max_lon (aircraft + detections)
GET  /api/detect/rules      thresholds in force
PUT  /api/detect/rules      partial update, merged and persisted
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
| 5 · grounded assistant over uploaded files + event store + live traffic | done (citations, user-set detection rules) |

Deliberately out of scope for now: vector search (OpenSky embeddings aside, the
doc corpus is small), and any bulk historical ADS-B import — the free OpenSky
API bills `/tracks` and `/flights` by day partition, so history is a separate
decision, not a config flag.

## OpenARIA: a second opinion on every stored track

MITRE's [OpenARIA](https://github.com/mitre-public/open-aria) is the open-source
edition of the FAA's ARIA analyser. It walks whole tracks, computes the closest
point of approach for every pair of aircraft, scores the encounter and writes the
arithmetic out as JSON. Our own detectors only ever judge the traffic in front of
the map at one poll; OpenARIA sees the history those polls left behind.

It is a **batch** pass, not a replacement for `detect.py`:

- it finds airborne encounters only — unstable approaches, go-arounds and anything
  on the surface stay with our own detectors;
- its score is not a severity. `openaria.py` grades every encounter against the
  3 NM / 1,000 ft minima the rest of the map already uses, so an operator can check
  a row by hand;
- rows go in through `store.insert_events`, the same door an upload uses, keyed on
  `openaria:<uniqueId>` so re-running a window rewrites nothing.

```bash
# once: the jar is 49 MB and is not in git
curl -L -o vendor/open-aria-uber.jar \
  https://github.com/mitre-public/open-aria/releases/download/0.5.0/open-aria-uber.jar
apt-get install -y openjdk-17-jre-headless     # Java 17+ is required

cd agent
OPENARIA_JAR=../vendor/open-aria-uber.jar python -m openaria --hours 24
OPENARIA_JAR=../vendor/open-aria-uber.jar python -m openaria --hours 24 --dry-run
```

**What limits it is sampling, not software.** A first pass over 4,249 stored
positions (311 aircraft, 5h44m) produced exactly one encounter, because our capture
is browser-driven: the average aircraft has 19 fixes spread over 43 minutes, and
OpenARIA needs contiguous tracks before it will compute a CPA. The 3,400 credit/day
OpenSky budget caps a continuous poller at roughly one fix every 25 seconds. Until
the capture is denser — or fed from a community ADS-B source — treat this pass as
working plumbing over thin data, not as coverage.

## The dense poller

`agent/poller.py` runs the capture whether or not anyone is looking at the map.

```bash
cd agent
.venv/bin/python -m poller --once            # one poll, show what it saw
.venv/bin/python -m poller --duration 300    # five minutes, then stop
.venv/bin/python -m poller --dry-run         # fetch and report, store nothing
cp ../agent/infra/safetyintel-poller.service /etc/systemd/system/ && \
  systemctl enable --now safetyintel-poller  # run it for real
```

Two sources are merged by aircraft id:

- **OpenSky** — broad coverage, ~10 s state vectors, one credit per 20-square-degree
  box. The box is deliberately Singapore-and-approaches rather than the viewport,
  because a bigger box costs the same credit.
- **adsb.lol** — free, keyless, sub-second fixes, but sees roughly a third of the
  traffic here and throttles by IP without advertising a limit (it answers 429 if you
  burst; `community_adsb` spaces requests and backs off). Its data is **ODbL**: any
  public display needs attribution.

The cadence is recomputed on every poll from the credits actually left and the hours
actually remaining, so the pass spreads its allowance across the day instead of going
blind by lunchtime. Measured over a 3.5-minute run: 11 polls, ~19 s apart, 62–77
fixes stored per poll, 1 credit each. Per-aircraft gap in the stored tracks dropped
from a 67.8 s p90 (browser-driven) to **19.7 s median / 25.2 s p90**.

It also propagates manoeuvres: go-arounds and unstable approaches detected on the
stored tracks are promoted into `safety_event` through the same door as everything
else, so the record grows when nobody is watching.

## Local dev

```bash
cd agent
uvicorn app:app --reload --port 7700     # needs Foundry env vars, see agent/README.md
```

For UI-only preview without Foundry or Synapse, `agent/scripts/run_demo_mode.sh`
sets `SAFETY_INTEL_DEMO_MODE=1` and serves canned SSE responses.
