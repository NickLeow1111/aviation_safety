import { useEffect, useRef, useState } from "react";

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

export function OpsBoard({ onSelectEvent }: { onSelectEvent?: (id: number) => void }) {
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

  const { traffic, detections, places, types, recent, series, detectors } = board;
  const peak = Math.max(1, ...series.map((point) => point.count));
  const newest = recent[0];
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

          <div className="ops-section">
            <div className="ops-section-head">
              <span>Newest detections</span>
              {newest && <span className="ops-muted">{clock(newest.event_time)}</span>}
            </div>
            {recent.length === 0 ? (
              <p className="ops-empty">Nothing detected in the stored history yet.</p>
            ) : (
              <ul className="ops-list">
                {recent.slice(0, 6).map((event) => (
                  <li key={event.id}>
                    <button
                      type="button"
                      className="ops-row"
                      data-ops-event={event.id}
                      onClick={() => onSelectEvent?.(event.id)}
                      title="Open on the map"
                    >
                      <span className={`ops-sev ops-sev--${event.severity}`} />
                      <span className="ops-time mono">{clock(event.event_time)}</span>
                      <span className="ops-what">{niceType(event.event_type)}</span>
                      <span className="ops-where">{event.place_icao ?? event.place_name ?? "—"}</span>
                      <span className="ops-who mono">{event.callsigns.join(" / ") || "—"}</span>
                      {event.separation_nm !== null && (
                        <span className="ops-metric">{event.separation_nm} NM</span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
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
              <span>Detectors</span>
              <span className="ops-muted">yield · validation</span>
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
