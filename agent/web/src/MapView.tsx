import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Map as MapLibreMap,
  NavigationControl,
  LngLatBounds,
  setWorkerUrl,
  type GeoJSONSource,
  type MapLayerMouseEvent,
} from "maplibre-gl";
// MapLibre runs tile parsing in a web worker whose file the bundler will not copy
// by itself, so the URL is imported and set explicitly. Without this the map
// renders an empty canvas and the console reports "Worker failed to load".
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import { UploadPanel } from "./UploadPanel";

/** The map view: where safety events actually happened.
 *
 *  Deliberately separate from the chat canvas. The Canvas renders what a
 *  conversation produced; this renders what the store holds, for whoever is
 *  looking. Both read the same `safety_event` rows, so the two cannot disagree.
 *
 *  Tiles are plain OpenStreetMap raster: no key, no vendor account. That is fine
 *  for a POC and explicitly not fine for production traffic — OSM's tile policy
 *  forbids it — so the basemap URL is the one thing to swap before this ships.
 */

const BASEMAP_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

const SEVERITY_COLOUR: Record<string, string> = {
  critical: "#d93a3a",
  high: "#e07b2a",
  medium: "#d9a72e",
  low: "#3f9a63",
  unknown: "#8a94a6",
};

const SEVERITY_ORDER = ["critical", "high", "medium", "low", "unknown"];

type EventRow = {
  id: number;
  event_time: string;
  event_type: string;
  severity: string;
  lat: number;
  lon: number;
  place_icao: string | null;
  place_name: string | null;
  registration: string | null;
  callsigns: string[] | null;
  source: string;
  provenance: string;
};

type Filters = {
  types: string[];
  severities: string[];
  source: string | null;
  days: number | null;
};

const EMPTY_FILTERS: Filters = { types: [], severities: [], source: null, days: null };

const DAY_PRESETS: { label: string; days: number | null }[] = [
  { label: "All time", days: null },
  { label: "Last 30 days", days: 30 },
  { label: "Last 7 days", days: 7 },
];

function niceType(value: string): string {
  return value.replace(/_/g, " ");
}

function when(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toISOString().replace("T", " ").replace(":00.000Z", "Z").replace(".000Z", "Z");
}

setWorkerUrl(maplibreWorkerUrl);

export function MapView() {
  const mapContainer = useRef<HTMLDivElement | null>(null);
  const map = useRef<MapLibreMap | null>(null);
  const [ready, setReady] = useState(false);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [severityCounts, setSeverityCounts] = useState<Record<string, number>>({});
  const [summary, setSummary] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<EventRow | null>(null);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [showUpload, setShowUpload] = useState(false);

  // ---- map lifecycle -----------------------------------------------------
  useEffect(() => {
    if (!mapContainer.current || map.current) return;

    const instance = new MapLibreMap({
      container: mapContainer.current,
      style: {
        version: 8,
        sources: {
          basemap: {
            type: "raster",
            tiles: [BASEMAP_TILES],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors",
          },
        },
        layers: [
          { id: "bg", type: "background", paint: { "background-color": "#dfe7f1" } },
          { id: "basemap", type: "raster", source: "basemap" },
        ],
      },
      center: [103.9, 1.36], // Singapore: the data is mostly here and it makes the first paint useful
      zoom: 8,
    });

    instance.addControl(new NavigationControl({ showCompass: false }), "top-right");
    instance.on("load", () => {
      instance.addSource("events", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      instance.addLayer({
        id: "event-halo",
        type: "circle",
        source: "events",
        paint: {
          "circle-radius": 14,
          "circle-color": ["get", "colour"],
          "circle-opacity": 0.18,
        },
      });
      instance.addLayer({
        id: "event-dot",
        type: "circle",
        source: "events",
        paint: {
          "circle-radius": 7,
          "circle-color": ["get", "colour"],
          "circle-stroke-width": 2,
          "circle-stroke-color": "#ffffff",
        },
      });
      instance.on("click", "event-dot", (event: MapLayerMouseEvent) => {
        const feature = event.features?.[0];
        if (!feature) return;
        const id = Number(feature.properties?.id);
        setSelected((current) => eventsRef.current.find((row) => row.id === id) ?? current);
      });
      instance.on("mouseenter", "event-dot", () => {
        instance.getCanvas().style.cursor = "pointer";
      });
      instance.on("mouseleave", "event-dot", () => {
        instance.getCanvas().style.cursor = "";
      });
      setReady(true);
    });

    map.current = instance;
    return () => {
      instance.remove();
      map.current = null;
    };
  }, []);

  const eventsRef = useRef<EventRow[]>([]);
  eventsRef.current = events;

  // ---- data --------------------------------------------------------------
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    const params = new URLSearchParams();
    if (filters.types.length) params.set("types", filters.types.join(","));
    if (filters.severities.length) params.set("severities", filters.severities.join(","));
    if (filters.source) params.set("source", filters.source);
    if (filters.days) {
      const since = new Date(Date.now() - filters.days * 86_400_000).toISOString();
      params.set("since", since);
    }
    params.set("limit", "2000");
    try {
      const response = await fetch(`/api/events?${params.toString()}`);
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setEvents(body.events ?? []);
      setCounts(body.counts ?? {});
      setSeverityCounts(body.severities ?? {});
      setSummary(body.summary ?? "");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void load();
  }, [load]);

  // ---- push events into the map -----------------------------------------
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const source = instance.getSource("events") as GeoJSONSource | undefined;
    if (!source) return;

    source.setData({
      type: "FeatureCollection",
      features: events.map((row) => ({
        type: "Feature" as const,
        geometry: { type: "Point" as const, coordinates: [row.lon, row.lat] },
        properties: { id: row.id, colour: SEVERITY_COLOUR[row.severity] ?? SEVERITY_COLOUR.unknown },
      })),
    });
  }, [events, ready]);

  const fitToEvents = useCallback(
    (animate = true) => {
      const instance = map.current;
      if (!instance || !events.length) return;
      const bounds = new LngLatBounds();
      events.forEach((row) => bounds.extend([row.lon, row.lat]));
      instance.fitBounds(bounds, { padding: 70, maxZoom: 12, duration: animate ? 700 : 0 });
    },
    [events],
  );

  // Fit once the first batch of events arrives.
  const fittedOnce = useRef(false);
  useEffect(() => {
    if (fittedOnce.current || !ready || !events.length) return;
    fitToEvents(false);
    fittedOnce.current = true;
  }, [events, ready, fitToEvents]);

  const focusEvent = useCallback((row: EventRow) => {
    setSelected(row);
    map.current?.flyTo({ center: [row.lon, row.lat], zoom: Math.max(map.current.getZoom(), 11), duration: 600 });
  }, []);

  const typeList = useMemo(() => Object.keys(counts).sort(), [counts]);
  const totalShown = events.length;

  const toggleType = (type: string) =>
    setFilters((current) => ({
      ...current,
      types: current.types.includes(type)
        ? current.types.filter((item) => item !== type)
        : [...current.types, type],
    }));

  const toggleSeverity = (severity: string) =>
    setFilters((current) => ({
      ...current,
      severities: current.severities.includes(severity)
        ? current.severities.filter((item) => item !== severity)
        : [...current.severities, severity],
    }));

  return (
    <div className="map-view">
      <aside className="map-rail">
        <div className="map-rail-head">
          <h2>Safety events</h2>
          <span className="map-count">{totalShown}</span>
        </div>
        <p className="map-sub" data-map-summary>
          {loading ? "Loading…" : summary || "no events"}
        </p>

        <div className="map-actions">
          <button className="map-btn" onClick={() => fitToEvents()} disabled={!events.length}>
            Fit to events
          </button>
          <button className="map-btn map-btn--ghost" onClick={() => setShowUpload(true)}>
            Upload file
          </button>
        </div>

        <section className="map-block">
          <h3>Time</h3>
          <div className="map-chips">
            {DAY_PRESETS.map((preset) => (
              <button
                key={preset.label}
                data-day-preset={preset.days ?? "all"}
                className={`map-chip ${filters.days === preset.days ? "is-on" : ""}`}
                onClick={() => setFilters((current) => ({ ...current, days: preset.days }))}
              >
                {preset.label}
              </button>
            ))}
          </div>
        </section>

        <section className="map-block">
          <h3>Severity</h3>
          <div className="map-chips">
            {SEVERITY_ORDER.filter(
              (severity) => severityCounts[severity] || filters.severities.includes(severity),
            ).map(
              (severity) => (
                <button
                  key={severity}
                  data-severity-chip={severity}
                  className={`map-chip ${filters.severities.includes(severity) ? "is-on" : ""}`}
                  onClick={() => toggleSeverity(severity)}
                >
                  <span className="map-dot" style={{ background: SEVERITY_COLOUR[severity] }} />
                  {severity}
                  <span className="map-chip-count">{severityCounts[severity] ?? 0}</span>
                </button>
              ),
            )}
          </div>
        </section>

        <section className="map-block">
          <h3>Type</h3>
          <div className="map-chips">
            {typeList.map((type) => (
              <button
                key={type}
                data-type-chip={type}
                className={`map-chip ${filters.types.includes(type) ? "is-on" : ""}`}
                onClick={() => toggleType(type)}
              >
                {niceType(type)}
                <span className="map-chip-count">{counts[type]}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="map-block">
          <h3>Source</h3>
          <div className="map-chips">
            {[null, "report", "upload", "derived"].map((value) => (
              <button
                key={value ?? "all"}
                data-source-chip={value ?? "all"}
                className={`map-chip ${filters.source === value ? "is-on" : ""}`}
                onClick={() => setFilters((current) => ({ ...current, source: value }))}
              >
                {value ?? "all"}
              </button>
            ))}
          </div>
        </section>

        {(filters.types.length || filters.severities.length || filters.source || filters.days) && (
          <button className="map-btn map-btn--ghost" onClick={() => setFilters(EMPTY_FILTERS)}>
            Clear filters
          </button>
        )}

        {error && <p className="map-error">{error}</p>}
      </aside>

      <div className="map-main">
        <div className="map-canvas" ref={mapContainer} data-map-canvas />

        <div className="map-table-wrap">
          <table className="map-table" data-event-table>
            <thead>
              <tr>
                <th>When (UTC)</th>
                <th>Type</th>
                <th>Severity</th>
                <th>Place</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {events.slice(0, 200).map((row) => (
                <tr
                  key={row.id}
                  data-event-row={row.id}
                  className={selected?.id === row.id ? "is-selected" : ""}
                  onClick={() => focusEvent(row)}
                >
                  <td className="mono">{when(row.event_time)}</td>
                  <td>{niceType(row.event_type)}</td>
                  <td>
                    <span className="map-dot" style={{ background: SEVERITY_COLOUR[row.severity] }} />
                    {row.severity}
                  </td>
                  <td>{row.place_icao ?? "—"}</td>
                  <td>{row.source}</td>
                </tr>
              ))}
              {!events.length && !loading && (
                <tr>
                  <td colSpan={5} className="map-empty">
                    Nothing matches these filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {selected && (
          <div className="map-detail" data-event-detail>
            <div className="map-detail-head">
              <strong>{niceType(selected.event_type)}</strong>
              <button className="map-detail-close" onClick={() => setSelected(null)} aria-label="Close details">
                ×
              </button>
            </div>
            <dl>
              <dt>When</dt>
              <dd>{when(selected.event_time)}</dd>
              <dt>Where</dt>
              <dd>
                {selected.place_name ?? selected.place_icao ?? "—"} ({selected.lat.toFixed(4)},{" "}
                {selected.lon.toFixed(4)})
              </dd>
              <dt>Severity</dt>
              <dd>{selected.severity}</dd>
              {selected.registration && (
                <>
                  <dt>Aircraft</dt>
                  <dd>{selected.registration}</dd>
                </>
              )}
              <dt>Source</dt>
              <dd>{selected.source}</dd>
              <dt>Provenance</dt>
              <dd className="map-provenance">{selected.provenance}</dd>
            </dl>
          </div>
        )}
      </div>

      {showUpload && (
        <UploadPanel onClose={() => setShowUpload(false)} onImported={() => void load()} />
      )}
    </div>
  );
}
