import { useEffect, useRef, useState } from "react";
import type { RulesSnapshot } from "./types";

/** The operations board: what is being detected, where, and in what.
 *
 *  The chat answers one question at a time. This answers the standing one — what is
 *  happening right now — without anyone having to ask it. It sits above the thread so
 *  the first thing on screen is the picture, not an empty composer.
 *
 *  Every figure is counted server-side from the event store and the track table. Where
 *  the data cannot answer — no type designator on OpenSky-only fixes, an empty hour —
 *  the board says so rather than drawing a zero as if it were a measurement.
 */

type Traffic = {
  window_minutes: number;
  airborne: number;
  ground: number;
  tracked: number;
  with_type: number;
  last_fix_age_seconds: number | null;
  last_fix_at: string | null;
};

type TypeCount = { type: string; aircraft: number; airborne: number };

type Coverage = {
  window_minutes: number;
  aircraft: number;
  median_samples: number | null;
  insufficient_track: number;
  with_long_gap: number;
  median_interval_s: number | null;
  p95_interval_s: number | null;
  longest_gap_s: number | null;
};

type PhaseCount = { phase: string; aircraft: number };

type AgreementCase = {
  ids: number[];
  at: string;
  callsigns: string[];
  detectors: string[];
  separation_nm: string | null;
  vertical_ft: string | null;
};

type Agreement = {
  clusters: number;
  both: AgreementCase[];
  snapshot_only: AgreementCase[];
  openaria_only: AgreementCase[];
  other: AgreementCase[];
  note: string;
};

type DetectorStat = {
  detector: string;
  produced: number;
  invalidated: number;
  kept: number;
  validation_rate: number | null;
  last_seen: string | null;
};
type PlaceCount = { icao: string | null; name: string | null; count: number };
type SeriesPoint = { at: string; count: number };

type RecentEvent = {
  id: number;
  event_time: string;
  event_type: string;
  severity: string;
  place_icao: string | null;
  place_name: string | null;
  callsigns: string[];
  source: string;
  detector: string | null;
  separation_nm: number | null;
  vertical_ft: number | null;
};

type Board = {
  generated_at: string;
  took_ms: number;
  track_hours: number;
  traffic: Traffic;
  detections: {
    hours: number;
    total: number;
    last_hour: number;
    by_type: { type: string; count: number; severity: Record<string, number> }[];
    by_severity: Record<string, number>;
  };
  series: SeriesPoint[];
  places: PlaceCount[];
  types: TypeCount[];
  detectors: DetectorStat[];
  agreement: Agreement;
  coverage: Coverage;
  phases: PhaseCount[];
  recent: RecentEvent[];
};

const REFRESH_MS = 20_000;

const niceType = (value: string) => value.replace(/_/g, " ");

function clock(iso: string): string {
  const date = new Date(iso);
  return date.toLocaleTimeString("en-SG", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function age(seconds: number | null): string {
  if (seconds === null) return "no fixes yet";
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export function OpsBoard({
  onSelectEvent,
  rules = null,
}: {
  onSelectEvent?: (id: number) => void;
  /** What the map's rule controls found on the last live pass. */
  rules?: RulesSnapshot | null;
}) {
  const [board, setBoard] = useState<Board | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(true);
  const [fetchedAt, setFetchedAt] = useState<number>(0);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const response = await fetch("/api/ops/overview?hours=24");
        if (!response.ok) throw new Error(`overview ${response.status}`);
        const data = (await response.json()) as Board;
        if (cancelled) return;
        setBoard(data);
        setError(null);
        setFetchedAt(Date.now());
      } catch (exc) {
        if (cancelled) return;
        setError(exc instanceof Error ? exc.message : "overview unavailable");
      }
    };

    load();
    timer.current = window.setInterval(() => {
      // A backgrounded tab does not need to poll; it will catch up when looked at.
      if (document.visibilityState === "visible") load();
    }, REFRESH_MS);

    return () => {
      cancelled = true;
      if (timer.current) window.clearInterval(timer.current);
    };
  }, []);

  if (!board) {
    return (
      <section className="ops" data-ops-board>
        <header className="ops-top">
          <span className="ops-title">Operations</span>
          <span className={`ops-status ${error ? "ops-status--warn" : ""}`} data-ops-status>
            {error ? "unavailable" : "connecting…"}
          </span>
        </header>
      </section>
    );
  }

  const { traffic, detections, places, types, series, detectors, agreement, coverage, phases } = board;
  const peak = Math.max(1, ...series.map((point) => point.count));
  const stale = traffic.last_fix_age_seconds !== null && traffic.last_fix_age_seconds > 300;

  return (
    <section className="ops" data-ops-board>
      <header className="ops-top">
        <span className="ops-title">Operations</span>
        <span className={`ops-status ${stale || error ? "ops-status--warn" : ""}`} data-ops-status>
          <span className="ops-dot" /> {error ? "stale" : `tracked ${age(traffic.last_fix_age_seconds)}`}
        </span>
        <button
          type="button"
          className="ops-toggle"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          {open ? "hide" : "show"}
        </button>
      </header>

      {open && (
        <div className="ops-body" data-ops-body>
          <div className="ops-kpis">
            <div className="ops-kpi">
              <span className="ops-kpi-value" data-ops-airborne>
                {traffic.airborne}
              </span>
              <span className="ops-kpi-label">airborne now</span>
            </div>
            <div className="ops-kpi">
              <span className="ops-kpi-value">{traffic.ground}</span>
              <span className="ops-kpi-label">on the ground</span>
            </div>
            <div className="ops-kpi">
              <span className="ops-kpi-value" data-ops-detections>
                {detections.total}
              </span>
              <span className="ops-kpi-label">detections / {detections.hours}h</span>
            </div>
            <div className="ops-kpi">
              <span className="ops-kpi-value">{detections.last_hour}</span>
              <span className="ops-kpi-label">in the last hour</span>
            </div>
          </div>

          {/* The day, hour by hour. A flat gap is a real answer: nothing was detected. */}
          <div className="ops-series" aria-label={`Detections per hour over ${detections.hours} hours`}>
            {series.map((point) => (
              <span
                key={point.at}
                className="ops-bar"
                style={{ height: `${Math.max(6, (point.count / peak) * 100)}%`, opacity: point.count ? 1 : 0.25 }}
                title={`${clock(point.at)} — ${point.count} detection${point.count === 1 ? "" : "s"}`}
              />
            ))}
          </div>

          {/* The rule results live here now.
              They used to render under the "Apply rules" button on the map, in the middle
              of the controls that produced them. The board is where a question about the
              airspace is answered, so the answer belongs here and the controls stay put. */}
          <div className="ops-section" data-ops-rules>
            <div className="ops-section-head">
              <span>Rules</span>
              <span className="ops-muted">{rules ? clock(rules.at) : "no live pass yet"}</span>
            </div>
            {!rules ? (
              <p className="ops-empty">
                Turn on live traffic on the map and press Apply rules. What those rules find
                shows up here.
              </p>
            ) : (
              <>
                <p className="ops-note">{rules.summary}</p>

                {rules.conflicts.length > 0 && (
                  <ul className="detection-list" data-detection-conflicts>
                    {rules.conflicts.slice(0, 5).map((item) => (
                      <li key={`${item.icao24_a ?? item.a}-${item.icao24_b ?? item.b}`} className="is-conflict">
                        <strong>
                          {item.a} / {item.b}
                        </strong>
                        <span>
                          {item.separation_nm} NM
                          {item.vertical_ft !== null && item.vertical_ft !== undefined
                            ? ` · ${item.vertical_ft} ft`
                            : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {rules.predicted.length > 0 && (
                  <ul className="detection-list" data-predicted-list>
                    {rules.predicted.slice(0, 4).map((item) => (
                      <li key={`${item.a}-${item.b}`} className="is-predicted">
                        <strong>
                          {item.a} / {item.b}
                        </strong>
                        <span>
                          {item.predicted_nm} NM in {item.time_to_cpa_s}s (now {item.current_nm})
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {rules.motion.length > 0 && (
                  <ul className="detection-list" data-motion-list>
                    {rules.motion.slice(0, 4).map((item) => (
                      <li key={`${item.callsign}-${item.kind}`} className="is-motion">
                        <strong>{item.callsign}</strong>
                        <span>
                          {(item.kind ?? "").replace(/_/g, " ")} — {item.detail ?? ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {rules.unusual.length > 0 && (
                  <ul className="detection-list" data-unusual-list>
                    {rules.unusual.slice(0, 5).map((item) => (
                      <li key={`${item.callsign}-${item.kind}`} className="is-unusual">
                        <strong>{item.callsign}</strong>
                        <span>{item.detail}</span>
                      </li>
                    ))}
                  </ul>
                )}

                {rules.overspeed.length > 0 && (
                  <ul className="detection-list" data-detection-overspeed>
                    {rules.overspeed.slice(0, 5).map((item) => (
                      <li key={item.icao24 ?? item.callsign} className="is-overspeed">
                        <strong>{item.callsign}</strong>
                        <span>
                          {item.ground_speed_kt} kt
                          {item.limit_kt ? ` (limit ${item.limit_kt})` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {rules.conflicts.length === 0 &&
                  rules.predicted.length === 0 &&
                  rules.motion.length === 0 &&
                  rules.unusual.length === 0 &&
                  rules.overspeed.length === 0 && (
                    <p className="ops-empty">Nothing tripped the current rules on that pass.</p>
                  )}
              </>
            )}
          </div>

          <div className="ops-columns">
            <div className="ops-section">
              <div className="ops-section-head">
                <span>Where</span>
                <span className="ops-muted">{detections.hours}h</span>
              </div>
              {places.length === 0 ? (
                <p className="ops-empty">No placed detections.</p>
              ) : (
                <ul className="ops-breakdown">
                  {places.map((place) => {
                    const share = detections.total ? place.count / detections.total : 0;
                    return (
                      <li key={place.icao ?? "unplaced"}>
                        <span className="ops-break-name">
                          {place.icao ?? "unplaced"}
                          <span className="ops-break-sub">{place.name ?? "no aerodrome within reach"}</span>
                        </span>
                        <span className="ops-break-count">{place.count}</span>
                        <span className="ops-break-bar">
                          <span style={{ width: `${Math.max(4, share * 100)}%` }} />
                        </span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>

            <div className="ops-section">
              <div className="ops-section-head">
                <span>What</span>
                <span className="ops-muted">
                  {traffic.with_type}/{traffic.tracked} typed
                </span>
              </div>
              {types.length === 0 ? (
                <p className="ops-empty">
                  No type designators yet — only the community feed reports them.
                </p>
              ) : (
                <ul className="ops-breakdown">
                  {types.map((entry) => (
                    <li key={entry.type}>
                      <span className="ops-break-name mono">{entry.type}</span>
                      <span className="ops-break-count">{entry.aircraft}</span>
                      <span className="ops-break-bar">
                        <span
                          style={{
                            width: `${Math.max(6, (entry.aircraft / Math.max(1, types[0].aircraft)) * 100)}%`,
                          }}
                        />
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className="ops-section">
            <div className="ops-section-head">
              <span>Agreement</span>
              <span className="ops-muted">{agreement.clusters} encounters · 3 min matching</span>
            </div>
            <ul className="ops-breakdown ops-agreement">
              <li>
                <span className="ops-break-name">
                  Seen by both
                  <span className="ops-break-sub">snapshot and engine agree on the encounter</span>
                </span>
                <span className="ops-break-count">{agreement.both.length}</span>
              </li>
              <li>
                <span className="ops-break-name">
                  Snapshot only
                  <span className="ops-break-sub">the engine, looking at history, did not report it</span>
                </span>
                <span className="ops-break-count">{agreement.snapshot_only.length}</span>
              </li>
              <li>
                <span className="ops-break-name">
                  Engine only
                  <span className="ops-break-sub">a single-moment comparison cannot see this shape</span>
                </span>
                <span className="ops-break-count">
                  {agreement.openaria_only.length}
                  {agreement.openaria_only.length > 0 && (
                    <button
                      className="ops-link"
                      onClick={() => onSelectEvent?.(agreement.openaria_only[0].ids[0])}
                    >
                      open
                    </button>
                  )}
                </span>
              </li>
            </ul>
            <p className="ops-note">{agreement.note}</p>
          </div>

          <div className="ops-section">
            <div className="ops-section-head">
              <span>Coverage</span>
              <span className="ops-muted">last {coverage.window_minutes} min · {coverage.aircraft} aircraft</span>
            </div>
            <ul className="ops-breakdown">
              <li>
                <span className="ops-break-name">
                  Phase now
                  <span className="ops-break-sub">
                    {phases.map((entry) => `${entry.aircraft} ${entry.phase}`).join(" · ")}
                  </span>
                </span>
                <span className="ops-break-count">{coverage.aircraft}</span>
              </li>
              <li>
                <span className="ops-break-name">
                  Sample interval
                  <span className="ops-break-sub">
                    median {coverage.median_interval_s ?? "—"} s · p95 {coverage.p95_interval_s ?? "—"} s ·
                    {" "}{coverage.median_samples ?? "—"} samples per aircraft
                  </span>
                </span>
                <span className="ops-break-count">
                  {coverage.median_interval_s !== null ? `${coverage.median_interval_s}s` : "—"}
                </span>
              </li>
              <li>
                <span className="ops-break-name">
                  Gaps and short tracks
                  <span className="ops-break-sub">
                    {coverage.with_long_gap} aircraft with a gap over 2 min · longest{" "}
                    {coverage.longest_gap_s !== null ? `${Math.round(coverage.longest_gap_s)} s` : "—"}
                  </span>
                </span>
                <span className="ops-break-count">{coverage.insufficient_track} short</span>
              </li>
            </ul>
            <p className="ops-note">
              Under {coverage.insufficient_track} aircraft carry too few positions to
              reconstruct a track. A closest approach finer than the distance travelled in
              one interval is interpolation, not observation.
            </p>
          </div>

          <div className="ops-section">
            <div className="ops-section-head">
              <span>Detectors</span>
              <span className="ops-muted">yield · re-judged valid</span>
            </div>
            {detectors.length === 0 ? (
              <p className="ops-empty">No detector has produced anything in this window.</p>
            ) : (
              <ul className="ops-breakdown">
                {detectors.map((entry) => (
                  <li key={entry.detector}>
                    <span className="ops-break-name">
                      {niceType(entry.detector)}
                      <span className="ops-break-sub">
                        {entry.kept} kept of {entry.produced}
                        {entry.invalidated > 0 ? ` · ${entry.invalidated} invalidated` : ""}
                      </span>
                    </span>
                    <span className="ops-break-count">
                      {entry.validation_rate === null
                        ? "—"
                        : `${Math.round(entry.validation_rate * 100)}%`}
                    </span>
                    <span className="ops-break-bar">
                      <span
                        style={{
                          width: `${Math.max(
                            3,
                            (entry.kept / Math.max(1, detectors[0].produced)) * 100,
                          )}%`,
                        }}
                      />
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <footer className="ops-foot">
            <span>
              {detections.by_type
                .slice(0, 3)
                .map((entry) => `${entry.count} ${niceType(entry.type)}`)
                .join(" · ") || "no detections"}
            </span>
            <span className="ops-muted">
              {board.track_hours}h of track · {board.took_ms}ms ·{" "}
              {fetchedAt ? `${Math.max(0, Math.round((Date.now() - fetchedAt) / 1000))}s old` : ""}
            </span>
          </footer>
        </div>
      )}
    </section>
  );
}
