import { useEffect, useRef, useState } from "react";
import { VegaLite } from "react-vega";
import type { ChartArtifact, ToolTrace } from "./types";

type OpsMetric = { label: string; value: string; detail: string };
type OpsTrack = {
  track_id: string;
  callsign: string;
  tail_id: string;
  flight_level: string;
  speed_kts: string;
  heading_deg: number;
  latitude: number;
  longitude: number;
  risk_score: number;
  integrity_pct: number;
  jamming_pct: number;
};
type OpsHotspot = {
  zone_id: string;
  label: string;
  latitude: number;
  longitude: number;
  count: number;
  severity: string;
};
type OpsAlert = {
  callsign: string;
  tail_id: string;
  flight_level: string;
  speed_kts: string;
  risk_score: number;
  conflict_alert: string;
  conflict_pair?: string;
};
type OpsDataset = { name: string; title: string; row_count: number };
type OpsDashboardSpec = {
  type: "ops_dashboard";
  title: string;
  domain: string;
  focus: string;
  generated_at: string;
  lookback_status?: string;
  lookback_note?: string | null;
  metrics: OpsMetric[];
  hotspots: OpsHotspot[];
  tracks: OpsTrack[];
  alerts: OpsAlert[];
  tactical_audit?: {
    tail_id: string;
    composite_risk_score: number;
    summary: string;
    actions: string[];
  } | null;
  recent_records: Record<string, unknown>[];
  highlights: string[];
  datasets: OpsDataset[];
};

type ExecutiveTable = {
  columns: string[];
  rows: Record<string, unknown>[];
};

export function Canvas({
  charts,
  traces,
  summaryText,
  lastPrompt,
  highlights,
}: {
  charts: ChartArtifact[];
  traces: ToolTrace[];
  summaryText: string;
  lastPrompt: string;
  highlights: string[];
}) {
  const latestChart = getSpotlightChart(charts);

  return (
    <div className="canvas single-flow">
      <div className="canvas-head">
        <div>
          <span className="eyebrow">Query output</span>
          <h2>{lastPrompt}</h2>
        </div>
        <span className="section-meta">{traces.length} tool calls</span>
      </div>

      <div className="canvas-body single-flow-body">
        <section className="output-story">
          {latestChart ? (
            <ChartCard spec={latestChart.spec} variant="hero" />
          ) : (
            <div className="empty-state-card">
              <strong>Awaiting first output.</strong>
              <p>Ask a safety question and the answer will land here as a single, clean output flow.</p>
            </div>
          )}
        </section>

        <aside className="output-side">
          <div className="info-card compact">
            <span className="info-card-title">Summary</span>
            <div className="info-card-body">
              <p>{summaryText || "The assistant response will appear here as the prompt is resolved."}</p>
            </div>
          </div>

          <div className="info-card compact">
            <span className="info-card-title">Pinned takeaways</span>
            <div className="info-card-body">
              <ul className="insight-list">
                {highlights.map((highlight) => (
                  <li key={highlight}>{highlight}</li>
                ))}
              </ul>
            </div>
          </div>

          <div className="info-card compact">
            <span className="info-card-title">Recent trace</span>
            <div className="info-card-body">
              {traces.length === 0 ? (
                <p>No tool activity yet.</p>
              ) : (
                <ul className="artifact-list">
                  {traces.slice(-3).reverse().map((trace) => (
                    <li key={trace.id}>
                      <span>{trace.name}</span>
                      <small>{trace.status}</small>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

function getSpotlightChart(charts: ChartArtifact[]): ChartArtifact | null {
  if (charts.length === 0) return null;
  const latestDashboard = [...charts].reverse().find((chart) => isOpsDashboard(chart.spec));
  return latestDashboard ?? charts[charts.length - 1];
}

function ChartCard({ spec, variant = "standard" }: { spec: any; variant?: "hero" | "standard" }) {
  if (isOpsDashboard(spec)) {
    return <OpsDashboardCard spec={spec} variant={variant} />;
  }

  if (spec && spec.type === "table") {
    return (
      <div className={`table-card ${variant}`}>
        {spec.title && <h3>{spec.title}</h3>}
        <DataTable columns={spec.columns} rows={spec.rows} />
      </div>
    );
  }

  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(360);
  const [error, setError] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);

  useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0].contentRect.width);
      if (w > 0) setWidth(w);
    });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  const { width: _w, height: _h, autosize: _a, background: _bg, config: _c, ...rest0 } = spec ?? {};
  void _w;
  void _h;
  void _a;
  void _bg;
  void _c;
  const rest: any = { ...rest0 };

  const isPie = rest?.mark === "arc" || (typeof rest?.mark === "object" && rest?.mark?.type === "arc");
  const values: any[] = Array.isArray(rest?.data?.values) ? rest.data.values : [];
  void values;

  const innerW = Math.max(width - 32, 240);
  const uniformH = variant === "hero" ? 380 : 300;
  const chartW = isPie ? Math.min(innerW - 200, 360) : innerW;
  const chartH = uniformH;

  const safeSpec: any = {
    ...rest,
    width: chartW,
    height: chartH,
    padding: { left: 8, right: 8, top: 8, bottom: 8 },
    background: "transparent",
    autosize: { type: "fit", contains: "padding", resize: true },
    config: {
      font: '"Avenir Next", "Segoe UI", sans-serif',
      axis: {
        labelColor: "#5f6f86",
        titleColor: "#23324d",
        labelFontSize: 11,
        titleFontSize: 12,
        titleFontWeight: 600,
        gridColor: "rgba(94, 122, 160, 0.18)",
        domainColor: "rgba(94, 122, 160, 0.4)",
        tickColor: "rgba(94, 122, 160, 0.4)",
        labelPadding: 4,
        titlePadding: 10,
        labelLimit: 140,
      },
      legend: {
        labelColor: "#5f6f86",
        titleColor: "#23324d",
        labelFontSize: 11,
        titleFontSize: 12,
        titleFontWeight: 600,
        symbolSize: 100,
        padding: 8,
        labelLimit: 160,
      },
      title: {
        color: "#23324d",
        fontSize: 13,
        fontWeight: 600,
        anchor: "start",
        offset: 8,
      },
      view: { stroke: "transparent" },
      range: {
        category: ["#1f6feb", "#00a7a0", "#f28f3b", "#0f8b8d", "#ef476f", "#6c63ff", "#2f6690", "#ffd166", "#7d8597", "#ff7f50", "#457b9d"],
      },
      arc: { stroke: "#f6f8fb", strokeWidth: 1 },
    },
  };

  const isEmpty = Array.isArray(safeSpec?.data?.values) && safeSpec.data.values.length === 0;

  return (
    <div className="chart-card hero" ref={wrapRef}>
      <div className="chart-card-head">
        {spec?.title && typeof spec.title === "string" && <h3>{spec.title}</h3>}
        <button className="ghost-button" onClick={() => setShowRaw((current) => !current)}>
          {showRaw ? "Hide spec" : "Show spec"}
        </button>
      </div>
      {showRaw && <pre className="spec-debug">{JSON.stringify(safeSpec, null, 2)}</pre>}
      {error ? (
        <pre className="chart-error">Vega error:{"\n"}{error}</pre>
      ) : isEmpty ? (
        <div className="chart-empty">No rows were available for this visualization.</div>
      ) : (
        <VegaLite
          spec={safeSpec}
          actions={false}
          renderer="svg"
          onError={(errorValue: Error) => {
            setError(errorValue.message);
          }}
        />
      )}
    </div>
  );
}

function OpsDashboardCard({ spec, variant }: { spec: OpsDashboardSpec; variant: "hero" | "standard" }) {
  const [showRaw, setShowRaw] = useState(false);
  const bounds = getBounds([...spec.hotspots, ...spec.tracks]);
  const executiveRecentRecords = toExecutiveRecentRecords(spec.recent_records);

  return (
    <div className={`ops-dashboard-card ${variant}`}>
      <div className="chart-card-head">
        <div>
          <h3>{spec.title}</h3>
          <p className="ops-subtitle">{spec.focus}</p>
          {spec.lookback_note && (
            <div className="ops-lookback-stack">
              <span className="ops-badge warning">Expanded lookback</span>
              <p className="ops-lookback-note">{spec.lookback_note}</p>
            </div>
          )}
        </div>
        <button className="ghost-button" onClick={() => setShowRaw((current) => !current)}>
          {showRaw ? "Hide spec" : "Show spec"}
        </button>
      </div>

      <div className="ops-metrics-row">
        {spec.metrics.map((metric) => (
          <div className="ops-metric" key={metric.label}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            <small>{metric.detail}</small>
          </div>
        ))}
      </div>

      <div className="ops-stage">
        <div className="ops-map-panel">
          <div className="ops-map-head">
            <div>
              <span className="eyebrow">Runway / Apron Schematic</span>
              <p className="ops-map-note">
                Relative hotspot and aircraft positions are normalized into an airfield-style
                layout so congestion and approach patterns read faster than in the abstract grid.
              </p>
            </div>
            <small>{spec.domain.replace(/_/g, " ")}</small>
          </div>
          <div className="ops-map-surface">
            <div className="airfield-apron terminal"><span>Terminal apron</span></div>
            <div className="airfield-apron remote"><span>Remote stands</span></div>
            <div className="airfield-runway primary"><span>RWY 02 / 20</span></div>
            <div className="airfield-runway secondary"><span>RWY 11 / 29</span></div>
            <div className="airfield-taxi alpha" />
            <div className="airfield-taxi bravo" />
            <div className="ops-map-label north">North marker</div>
            <div className="ops-map-label center">Operational schematic</div>

            {spec.hotspots.map((hotspot) => {
              const position = normalizePoint(hotspot.latitude, hotspot.longitude, bounds);
              return (
                <div
                  key={hotspot.zone_id}
                  className="hotspot-node"
                  style={{ left: `${position.left}%`, top: `${position.top}%` }}
                >
                  <div className={`hotspot-chip ${severityClass(hotspot.severity)}`} title={`${hotspot.label}: ${hotspot.count}`}>
                    <span>{hotspot.count}</span>
                  </div>
                  <div className="hotspot-caption">{hotspot.label}</div>
                </div>
              );
            })}

            {spec.tracks.map((track) => {
              const position = normalizePoint(track.latitude, track.longitude, bounds);
              return (
                <div
                  key={track.track_id}
                  className="track-node"
                  style={{ left: `${position.left}%`, top: `${position.top}%` }}
                  title={`${track.callsign} ${track.flight_level} ${track.speed_kts} KTS`}
                >
                  <div
                    className={`track-marker ${riskClass(track.risk_score)}`}
                    style={{ transform: `rotate(${track.heading_deg}deg)` }}
                  />
                  <div className="track-label">
                    <strong>{track.callsign}</strong>
                    <small>{track.flight_level} • {track.speed_kts} KTS</small>
                  </div>
                </div>
              );
            })}

            <div className="ops-map-legend">
              <span><i className="legend-swatch hotspot critical" /> Hotspot cluster</span>
              <span><i className="legend-swatch track high" /> Aircraft vector + callsign</span>
              <span><i className="legend-swatch overlay" /> Schematic placement</span>
            </div>
          </div>
        </div>

        <div className="ops-side-rail">
          <div className="ops-side-card intelligence">
            <div className="ops-side-head">
              <span className="eyebrow">Intelligence</span>
              <small>{spec.alerts.length} live cues</small>
            </div>
            {spec.alerts.length === 0 ? (
              <p>No active alerts in this dataset.</p>
            ) : (
              <ul className="ops-alert-list">
                {spec.alerts.map((alert) => (
                  <li key={`${alert.callsign}-${alert.tail_id}`}>
                    <div>
                      <strong>{alert.callsign}</strong>
                      <small>{alert.flight_level} • {alert.speed_kts} KTS</small>
                    </div>
                    <span className={`risk-pill ${riskClass(alert.risk_score)}`}>{Math.round(alert.risk_score)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="ops-side-card tactical">
            <div className="ops-side-head">
              <span className="eyebrow">Tactical audit</span>
              <small>{spec.tactical_audit?.tail_id ?? "No tail id"}</small>
            </div>
            {spec.tactical_audit ? (
              <>
                <div className="tactical-score">
                  <span>Composite risk score</span>
                  <strong>{Math.round(spec.tactical_audit.composite_risk_score)}</strong>
                </div>
                <p>{spec.tactical_audit.summary}</p>
                <ul className="insight-list compact">
                  {spec.tactical_audit.actions.map((action) => (
                    <li key={action}>{action}</li>
                  ))}
                </ul>
              </>
            ) : (
              <p>No tactical note available.</p>
            )}
          </div>
        </div>
      </div>

      <div className="ops-lower-deck">
        <div className="ops-side-card recent-records">
          <div className="ops-side-head">
            <div className="ops-side-head-copy">
              <span className="eyebrow">Recent records</span>
              {spec.lookback_status === "latest_available" && (
                <span className="ops-badge subtle">Latest available</span>
              )}
            </div>
            <small>{spec.recent_records.length} rows</small>
          </div>
          {spec.recent_records.length > 0 ? (
            <DataTable columns={executiveRecentRecords.columns} rows={executiveRecentRecords.rows} />
          ) : (
            <p>No recent records available.</p>
          )}
        </div>

        <div className="ops-side-card analyst-notes">
          <div className="ops-side-head">
            <span className="eyebrow">Analyst notes</span>
            <small>{spec.generated_at}</small>
          </div>
          <ul className="insight-list compact">
            {spec.highlights.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <ul className="artifact-list compact">
            {spec.datasets.map((dataset) => (
              <li key={dataset.name}>
                <span>{dataset.title}</span>
                <small>{dataset.row_count} rows</small>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {showRaw && <pre className="spec-debug">{JSON.stringify(spec, null, 2)}</pre>}
    </div>
  );
}

function DataTable({ columns, rows }: { columns: string[]; rows: Record<string, unknown>[] }) {
  return (
    <div className="table-scroll">
      <table className="data">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>{format(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function format(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function toExecutiveRecentRecords(rows: Record<string, unknown>[]): ExecutiveTable {
  if (rows.length === 0) {
    return { columns: [], rows: [] };
  }

  const projectedRows: Record<string, unknown>[] = rows.map((row) => ({
    Date: row.Occurrence_Date ?? row.Date ?? "",
    Station: row.Location ?? row.Station ?? "",
    Operator: row.Organisation_Name ?? row.Operator ?? "",
    Event: row.Occurrence_Subtype ?? row.Event ?? row.Subtype ?? "",
    Status: row.Current_Status ?? row.Status ?? "",
    Summary: summarizeText(row.Summary),
  }));

  const columns = Object.keys(projectedRows[0]).filter((column) =>
    projectedRows.some((row) => {
      const value = row[column];
      return value !== null && value !== undefined && String(value).trim() !== "";
    }),
  );

  return {
    columns,
    rows: projectedRows.map((row) =>
      Object.fromEntries(columns.map((column) => [column, row[column]])),
    ),
  };
}

function summarizeText(value: unknown): string {
  const text = format(value).replace(/\s+/g, " ").trim();
  if (text.length <= 120) return text;
  return `${text.slice(0, 117).trimEnd()}...`;
}

function isOpsDashboard(spec: any): spec is OpsDashboardSpec {
  return Boolean(spec && spec.type === "ops_dashboard");
}

function getBounds(points: Array<{ latitude: number; longitude: number }>) {
  if (points.length === 0) {
    return { minLat: 1.33, maxLat: 1.37, minLon: 103.97, maxLon: 104.01 };
  }
  const latitudes = points.map((point) => point.latitude);
  const longitudes = points.map((point) => point.longitude);
  return {
    minLat: Math.min(...latitudes),
    maxLat: Math.max(...latitudes),
    minLon: Math.min(...longitudes),
    maxLon: Math.max(...longitudes),
  };
}

function normalizePoint(latitude: number, longitude: number, bounds: ReturnType<typeof getBounds>) {
  const latSpan = Math.max(bounds.maxLat - bounds.minLat, 0.001);
  const lonSpan = Math.max(bounds.maxLon - bounds.minLon, 0.001);
  const left = 8 + ((longitude - bounds.minLon) / lonSpan) * 84;
  const top = 12 + (1 - (latitude - bounds.minLat) / latSpan) * 72;
  return { left, top };
}

function severityClass(value: string) {
  const normalized = value.toLowerCase();
  if (normalized.includes("critical")) return "critical";
  if (normalized.includes("watch")) return "watch";
  return "steady";
}

function riskClass(score: number) {
  if (score >= 20) return "high";
  if (score >= 12) return "medium";
  return "low";
}
