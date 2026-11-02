import { useCallback, useEffect, useMemo, useState } from "react";

/**
 * The analysis canvas.
 *
 * The operations board describes now; these four tabs describe *why*. Each one answers a
 * question the board structurally cannot:
 *
 *   Case file  — is this detection real? Provenance, the numbers behind it, and whether the
 *                tracks underneath it were sound.
 *   Rates      — where should I look first? Events per aircraft seen, so busy is not
 *                automatically dangerous.
 *   Recurrence — is this one aircraft or a pattern?
 *   Pinned     — what do I check every morning?
 *
 * Every figure is fetched. Where a denominator is a proxy the panel says "aircraft", never
 * "movements".
 */

interface CaseFile {
  event: {
    id: number;
    event_time: string;
    event_type: string;
    severity: string;
    lat: number | null;
    lon: number | null;
    place_icao: string | null;
    place_name: string | null;
    altitude_ft: number | null;
    callsigns: string[] | null;
    icao24: string[] | null;
    metrics: Record<string, unknown> | null;
    source: string;
    detector: string | null;
    provenance: string | null;
    external_ref: string | null;
    invalidated_at: string | null;
    invalidation_reason: string | null;
  };
  related: {
    id: number;
    event_time: string;
    event_type: string;
    severity: string;
    place_icao: string | null;
    callsigns: string[];
    delta_s: number | null;
  }[];
  quality: { aircraft: number; steps: number; repeated_fixes: number; implausible_segments: number };
}

interface Rates {
  days: number;
  window: { first_event: string | null; last_event: string | null; events: number };
  places: {
    place_icao: string | null;
    place_name: string | null;
    events: number;
    high: number;
    aircraft: number | null;
    per_100_aircraft: number | null;
  }[];
  hours: { hour: number; events: number; aircraft: number | null; per_100_aircraft: number | null }[];
  types: { event_type: string; events: number }[];
  note: string;
  computed_at?: string;
}

interface Recurrence {
  days: number;
  min_events: number;
  pairs: { callsigns: string[]; events: number; first_seen: string; last_seen: string; types: string[]; places: string[] }[];
  callsigns: { callsign: string; events: number; first_seen: string; last_seen: string }[];
}

export interface PinnedQuery {
  id: string;
  question: string;
  addedAt: number;
}

const PIN_KEY = "aviation.pinnedAnalyses";

const clock = (iso: string | null) => (iso ? iso.slice(11, 19) : "—");
const day = (iso: string | null) => (iso ? iso.slice(0, 10) : "—");
const nice = (value: string) => value.replace(/_/g, " ");

export function loadPinned(): PinnedQuery[] {
  try {
    const raw = window.localStorage.getItem(PIN_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function savePinned(items: PinnedQuery[]) {
  try {
    window.localStorage.setItem(PIN_KEY, JSON.stringify(items));
  } catch {
    /* a full or blocked store should not break the page */
  }
}

type Tab = "case" | "rates" | "recurrence" | "pinned";

interface EventOption {
  id: number;
  event_time: string;
  event_type: string;
  severity: string;
  place_icao: string | null;
  callsigns: string[] | null;
}

export function InsightsPanel({
  caseId,
  onAsk,
  onOpenReplay,
  pinned,
  onUnpin,
}: {
  caseId: number | null;
  onAsk: (question: string) => void;
  onOpenReplay?: (id: number) => void;
  pinned: PinnedQuery[];
  onUnpin: (id: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("rates");
  const [caseFile, setCaseFile] = useState<CaseFile | null>(null);
  const [caseError, setCaseError] = useState<string | null>(null);
  const [rates, setRates] = useState<Rates | null>(null);
  const [recurrence, setRecurrence] = useState<Recurrence | null>(null);
  const [days, setDays] = useState(7);
  const [recent, setRecent] = useState<EventOption[] | null>(null);
  const [picked, setPicked] = useState<number | null>(caseId);

  // Selecting an event on the board opens its case, because that is the question a click
  // actually means.
  useEffect(() => {
    setPicked(caseId);
  }, [caseId]);

  // The case tab is its own entry point: the ops board deliberately no longer lists events,
  // so without a picker here the tab would be a dead end.
  useEffect(() => {
    if (tab !== "case" || recent) return;
    fetch("/api/events?limit=80")
      .then((response) => response.json())
      .then((body) => setRecent((body.events ?? []) as EventOption[]))
      .catch(() => setRecent([]));
  }, [tab, recent]);

  useEffect(() => {
    if (picked === null) return;
    setTab("case");
    setCaseError(null);
    fetch(`/api/analysis/case/${picked}`)
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
        return body as CaseFile;
      })
      .then(setCaseFile)
      .catch((cause) => {
        setCaseFile(null);
        setCaseError(cause instanceof Error ? cause.message : String(cause));
      });
  }, [picked]);

  useEffect(() => {
    if (tab !== "rates" || rates) return;
    fetch(`/api/analysis/rates?days=${days}`)
      .then((response) => response.json())
      .then((body) => setRates(body as Rates))
      .catch(() => setRates(null));
  }, [tab, rates, days]);

  useEffect(() => {
    if (tab !== "recurrence" || recurrence) return;
    fetch(`/api/analysis/recurrence?days=30&min_events=3`)
      .then((response) => response.json())
      .then((body) => setRecurrence(body as Recurrence))
      .catch(() => setRecurrence(null));
  }, [tab, recurrence]);

  const ask = useCallback((question: string) => onAsk(question), [onAsk]);

  const hourPeak = useMemo(() => {
    if (!rates) return null;
    return rates.hours.reduce<{ hour: number; value: number } | null>((best, point) => {
      if (point.per_100_aircraft === null) return best;
      return !best || point.per_100_aircraft > best.value ? { hour: point.hour, value: point.per_100_aircraft } : best;
    }, null);
  }, [rates]);

  return (
    <section className="insights" data-insights>
      <header className="insights-top">
        <div className="insights-tabs">
          {(
            [
              ["case", "Case file"],
              ["rates", "Rates"],
              ["recurrence", "Recurrence"],
              ["pinned", `Pinned${pinned.length ? ` · ${pinned.length}` : ""}`],
            ] as [Tab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={`insights-tab ${tab === key ? "is-on" : ""}`}
              data-insight-tab={key}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>
        {tab === "rates" && (
          <select
            className="insights-range"
            value={days}
            onChange={(event) => {
              setDays(Number(event.target.value));
              setRates(null);
            }}
          >
            {[3, 7, 14, 30].map((value) => (
              <option key={value} value={value}>
                last {value} d
              </option>
            ))}
          </select>
        )}
      </header>

      <div className="insights-body">
        {tab === "case" &&
          (caseError ? (
            <p className="ops-empty">{caseError}</p>
          ) : !caseFile ? (
            <div className="case-picker">
              <p className="insights-note">
                Pick an event to see what the detection was made from — the detector, the numbers,
                the state of the tracks underneath it, and what these aircraft have done before.
              </p>
              {!recent && <p className="ops-empty">Loading events…</p>}
              <div className="case-picker-list" data-case-picker>
                {(recent ?? []).map((option) => (
                  <button key={option.id} type="button" className="case-picker-row" onClick={() => setPicked(option.id)}>
                    <span className="mono">{option.event_time.slice(11, 19)}</span>
                    <span>{nice(option.event_type)}</span>
                    <span className="ops-muted">{option.place_icao ?? "—"}</span>
                    <span className="ops-muted mono">{(option.callsigns ?? []).join(" / ") || "—"}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="case" data-case-file>
              <div className="case-head">
                <span className={`case-kind case-kind--${caseFile.event.severity}`}>{nice(caseFile.event.event_type)}</span>
                <span className="mono">{caseFile.event.event_time.slice(0, 19).replace("T", " ")}Z</span>
                <span className="ops-muted">{caseFile.event.place_icao ?? "unplaced"}</span>
                <span className="ops-muted mono">{(caseFile.event.callsigns ?? []).join(" / ") || "—"}</span>
                <button type="button" className="map-btn map-btn--ghost" onClick={() => { setPicked(null); setCaseFile(null); }}>
                  Events
                </button>
                {onOpenReplay && (
                  <button type="button" className="map-btn map-btn--ghost" style={{ marginLeft: "auto" }} onClick={() => onOpenReplay(caseFile.event.id)}>
                    Fly it
                  </button>
                )}
              </div>

              <dl className="case-facts">
                <div>
                  <dt>Detector</dt>
                  <dd>{caseFile.event.detector ?? "unknown"}</dd>
                </div>
                <div>
                  <dt>Source</dt>
                  <dd>{caseFile.event.source}</dd>
                </div>
                <div>
                  <dt>Callsigns</dt>
                  <dd className="mono">{(caseFile.event.callsigns ?? []).join(", ") || "—"}</dd>
                </div>
                <div>
                  <dt>Position</dt>
                  <dd className="mono">
                    {caseFile.event.lat !== null && caseFile.event.lon !== null
                      ? `${caseFile.event.lat.toFixed(4)}, ${caseFile.event.lon.toFixed(4)}`
                      : "—"}
                  </dd>
                </div>
                {Object.entries(caseFile.event.metrics ?? {})
                  .filter(([key]) => key !== "detector")
                  .map(([key, value]) => (
                    <div key={key}>
                      <dt>{nice(key)}</dt>
                      <dd className="mono">{typeof value === "number" ? value.toFixed(2) : String(value)}</dd>
                    </div>
                  ))}
              </dl>

              <div className={`case-quality ${caseFile.quality.repeated_fixes + caseFile.quality.implausible_segments > 0 ? "is-warn" : ""}`} data-case-quality>
                <strong>Evidence</strong> — around this event,{" "}
                {caseFile.quality.steps} track steps for {caseFile.quality.aircraft} aircraft:{" "}
                {caseFile.quality.repeated_fixes} repeated fix
                {caseFile.quality.repeated_fixes === 1 ? "" : "es"}, {caseFile.quality.implausible_segments} faster than 900 kt.
                {caseFile.quality.repeated_fixes + caseFile.quality.implausible_segments > 0
                  ? caseFile.event.event_type.includes("separation")
                    ? " The separation was interpolated across those gaps."
                    : " Whatever the detector measured was measured across those gaps."
                  : " The tracks under this detection were sound."}
              </div>

              {caseFile.event.invalidated_at && (
                <p className="case-invalid" data-case-invalid>
                  Marked invalid {day(caseFile.event.invalidated_at)} — {caseFile.event.invalidation_reason ?? "no reason recorded"}
                </p>
              )}

              <div className="case-related">
                <h4>Same aircraft, other events · {caseFile.related.length}</h4>
                {caseFile.related.length === 0 && <p className="ops-muted">First recorded occurrence for these aircraft.</p>}
                {caseFile.related.map((other) => (
                  <button key={other.id} type="button" className="case-related-row" onClick={() => ask(`Show me event ${other.id}`)}>
                    <span className="mono">{other.event_time.slice(11, 19)}</span>
                    <span>{nice(other.event_type)}</span>
                    <span className="ops-muted">{other.place_icao ?? "—"}</span>
                    <span className="ops-muted mono">
                      {other.delta_s !== null ? `${Math.round(Math.abs(other.delta_s) / 60)} min away` : ""}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ))}

        {tab === "rates" &&
          (!rates ? (
            <p className="ops-empty">Counting…</p>
          ) : (
            <div className="rates">
              <p className="insights-note">
                Events per 100 aircraft seen — the same window, two different denominators. A raw
                count ranks aerodromes by how busy they are; this ranks them by how often something
                happened per aircraft that was there.
                {hourPeak && ` Busiest hour: ${String(hourPeak.hour).padStart(2, "0")}:00 UTC at ${hourPeak.value.toFixed(1)} per 100.`}
              </p>

              <table className="insights-table" data-rates-places>
                <thead>
                  <tr>
                    <th>Place</th>
                    <th className="num">Events</th>
                    <th className="num">Aircraft</th>
                    <th className="num">Per 100</th>
                  </tr>
                </thead>
                <tbody>
                  {rates.places.map((place) => (
                    <tr key={place.place_icao ?? "unplaced"}>
                      <td>
                        {place.place_icao ?? "unplaced"}
                        {place.place_name && <span className="ops-muted"> {place.place_name}</span>}
                      </td>
                      <td className="num mono">{place.events}</td>
                      <td className="num mono">{place.aircraft ?? "—"}</td>
                      <td className="num mono">
                        <span className={place.per_100_aircraft && place.per_100_aircraft > 15 ? "rate-hot" : ""}>
                          {place.per_100_aircraft ?? "—"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <div className="rate-hours">
                <h4>By hour of day · UTC</h4>
                <div className="rate-bars" data-rates-hours>
                  {rates.hours.map((point) => {
                    const peak = Math.max(1, ...rates.hours.map((item) => item.per_100_aircraft ?? 0));
                    return (
                      <span
                        key={point.hour}
                        className="rate-bar"
                        title={`${String(point.hour).padStart(2, "0")}:00 — ${point.events} events, ${point.per_100_aircraft ?? "—"} per 100 aircraft`}
                        style={{ height: `${Math.max(3, ((point.per_100_aircraft ?? 0) / peak) * 100)}%` }}
                      />
                    );
                  })}
                </div>
                <div className="rate-hours-axis ops-muted">
                  <span>00</span>
                  <span>06</span>
                  <span>12</span>
                  <span>18</span>
                  <span>23</span>
                </div>
              </div>

              <p className="insights-note ops-muted">
                {rates.note} Events in the store: {rates.window.events}, {day(rates.window.first_event)} to{" "}
                {day(rates.window.last_event)} — short history, so these are within-window rates, not trends.
                {rates.computed_at && ` Counted at ${rates.computed_at.slice(11, 19)}Z and held for five minutes.`}
              </p>
            </div>
          ))}

        {tab === "recurrence" &&
          (!recurrence ? (
            <p className="ops-empty">Counting…</p>
          ) : (
            <div className="recurrence">
              <p className="insights-note">
                Three or more events in {recurrence.days} days. One event is an incident; the fourth
                time the same aircraft appears is a pattern — and it asks a different question of the
                aircraft than of the procedure.
              </p>

              <h4>Pairs · {recurrence.pairs.length}</h4>
              <div className="recurrence-list" data-recurrence-pairs>
                {recurrence.pairs.map((pair) => (
                  <button
                    key={pair.callsigns.join("-")}
                    type="button"
                    className="recurrence-row"
                    onClick={() => ask(`Show me the events involving ${pair.callsigns.join(" and ")}`)}
                  >
                    <span className="mono">{pair.callsigns.join(" / ")}</span>
                    <span className="recurrence-count">{pair.events}</span>
                    <span className="ops-muted">{pair.types.map(nice).join(", ")}</span>
                    <span className="ops-muted mono">{pair.places.join(" ") || "—"}</span>
                  </button>
                ))}
                {!recurrence.pairs.length && <p className="ops-muted">No pair recurs three times yet.</p>}
              </div>

              <h4>Aircraft · {recurrence.callsigns.length}</h4>
              <div className="recurrence-list" data-recurrence-callsigns>
                {recurrence.callsigns.slice(0, 12).map((craft) => (
                  <button
                    key={craft.callsign}
                    type="button"
                    className="recurrence-row"
                    onClick={() => ask(`Show me the events involving ${craft.callsign}`)}
                  >
                    <span className="mono">{craft.callsign}</span>
                    <span className="recurrence-count">{craft.events}</span>
                    <span className="ops-muted">
                      {day(craft.first_seen)} → {day(craft.last_seen)} · last at {clock(craft.last_seen)}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ))}

        {tab === "pinned" &&
          (pinned.length === 0 ? (
            <p className="ops-empty">
              Nothing pinned yet. Ask a question, then press Pin on the answer — it will re-run
              whenever you open this tab.
            </p>
          ) : (
            <div className="pinned-list" data-pinned-list>
              {pinned.map((item) => (
                <div key={item.id} className="pinned-row">
                  <button type="button" className="pinned-ask" onClick={() => ask(item.question)} title="Run this analysis again">
                    {item.question}
                  </button>
                  <button type="button" className="pinned-drop" onClick={() => onUnpin(item.id)} title="Unpin">
                    ×
                  </button>
                </div>
              ))}
            </div>
          ))}
      </div>
    </section>
  );
}
