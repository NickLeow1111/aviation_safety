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

type Aircraft = {
  icao24: string;
  callsign: string | null;
  country: string | null;
  lat: number | null;
  lon: number | null;
  altitude_ft: number | null;
  ground_speed_kt: number | null;
  on_ground: boolean;
  track_deg: number | null;
  vertical_rate_fpm: number | null;
};

type LivePayload = {
  aircraft: Aircraft[];
  count: number;
  mode: string;
  credits_used: number;
  credits_budget: number;
  credits_remaining: number;
  age_seconds: number;
  cached: boolean;
};

type LiveStatus = {
  mode: string;
  credits_used: number;
  credits_budget: number;
  credits_remaining: number;
  cache_ttl_seconds: number;
  max_bbox_square_degrees: number;
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
  const [live, setLive] = useState(false);
  const [liveData, setLiveData] = useState<LivePayload | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveNote, setLiveNote] = useState<string | null>(null);
  const [aircraft, setAircraft] = useState<Aircraft | null>(null);

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
      instance.addSource("aircraft", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      instance.addLayer({
        id: "aircraft-halo",
        type: "circle",
        source: "aircraft",
        paint: { "circle-radius": 9, "circle-color": "#0f1a2e", "circle-opacity": 0.12 },
      });
      instance.addLayer({
        id: "aircraft-dot",
        type: "circle",
        source: "aircraft",
        paint: {
          "circle-radius": 4.5,
          "circle-color": ["case", ["get", "on_ground"], "#6b7785", "#0f1a2e"],
          "circle-stroke-width": 1.5,
          "circle-stroke-color": "#ffffff",
        },
      });
      instance.on("click", "aircraft-dot", (event: MapLayerMouseEvent) => {
        const feature = event.features?.[0];
        if (!feature) return;
        const properties = feature.properties as Record<string, unknown>;
        const id = String(properties.icao24 ?? "");
        setAircraft(liveRef.current?.aircraft.find((item) => item.icao24 === id) ?? null);
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

  // ---- live traffic ------------------------------------------------------
  const liveRef = useRef<LivePayload | null>(null);
  liveRef.current = liveData;
  const liveStatusRef = useRef<LiveStatus | null>(null);
  liveStatusRef.current = liveStatus;

  const fetchLive = useCallback(async () => {
    const instance = map.current;
    if (!instance) return;

    // OpenSky bills by area, so a world view is not a view we are allowed to ask
    // for. Rather than refuse and leave the user staring at an error, narrow the
    // map to a local box and say that we did.
    const maxArea = liveStatusRef.current?.max_bbox_square_degrees ?? 25;
    let bounds = instance.getBounds();
    const area = (bounds.getNorth() - bounds.getSouth()) * (bounds.getEast() - bounds.getWest());
    if (area > maxArea) {
      // Narrow around the events, not around whatever patch of ocean the world
      // view happened to be centred on: a live layer that opens on empty water
      // looks broken even when it is working.
      const points = eventsRef.current;
      const center = points.length
        ? points.reduce(
            (acc, row) => ({ lng: acc.lng + row.lon / points.length, lat: acc.lat + row.lat / points.length }),
            { lng: 0, lat: 0 },
          )
        : instance.getCenter();
      const span = Math.min(4, Math.sqrt(maxArea) * 0.6);
      bounds = new LngLatBounds(
        [center.lng - span / 2, center.lat - span / 2],
        [center.lng + span / 2, center.lat + span / 2],
      );
      instance.fitBounds(bounds, { duration: 600, maxZoom: 12 });
      setLiveNote(
        `narrowed to a ${span.toFixed(1)}° box — OpenSky bills by area, so live traffic is a local view`,
      );
    }

    const bbox = [bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast()]
      .map((value) => value.toFixed(4))
      .join(",");
    try {
      const response = await fetch(`/api/live?bbox=${bbox}`);
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setLiveData(body as LivePayload);
      setLiveStatus((current) =>
        current
          ? { ...current, mode: body.mode, credits_used: body.credits_used, credits_remaining: body.credits_remaining }
          : current,
      );
      setLiveError(null);
    } catch (cause) {
      setLiveError(String(cause).replace("Error: ", ""));
    }
  }, []);

  useEffect(() => {
    fetch("/api/live/status")
      .then((response) => response.json())
      .then((body) => setLiveStatus(body as LiveStatus))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready || !live) return;
    const onMoveEnd = () => void fetchLive();
    instance.on("moveend", onMoveEnd);
    return () => {
      instance.off("moveend", onMoveEnd);
    };
  }, [live, ready, fetchLive]);

  useEffect(() => {
    if (!live) {
      setLiveNote(null);
      const instance = map.current;
      const source = instance?.getSource("aircraft") as GeoJSONSource | undefined;
      source?.setData({ type: "FeatureCollection", features: [] });
      setLiveData(null);
      return;
    }
    void fetchLive();
    // 15s with a 10s server-side cache: several viewers share one billed request.
    const timer = setInterval(() => void fetchLive(), 15_000);
    return () => clearInterval(timer);
  }, [live, fetchLive]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const source = instance.getSource("aircraft") as GeoJSONSource | undefined;
    if (!source) return;
    const features = (liveData?.aircraft ?? [])
      .filter((item) => item.lat !== null && item.lon !== null)
      .map((item) => ({
        type: "Feature" as const,
        geometry: { type: "Point" as const, coordinates: [item.lon as number, item.lat as number] },
        properties: {
          icao24: item.icao24,
          on_ground: item.on_ground,
          callsign: item.callsign ?? item.icao24,
        },
      }));
    source.setData({ type: "FeatureCollection", features });
  }, [liveData, ready]);

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
          <div className="map-live-head">
            <h3>Live traffic</h3>
            <button
              className={`map-toggle ${live ? "is-on" : ""}`}
              data-live-toggle
              onClick={() => setLive((value) => !value)}
            >
              {live ? "On" : "Off"}
            </button>
          </div>
          {live && (
            <p className="map-live-status" data-live-status>
              {liveData
                ? `${liveData.count} aircraft · ${liveData.mode} · ${liveData.credits_used}/${liveData.credits_budget} credits today · ${Math.round(liveData.age_seconds)}s old`
                : liveError
                  ? "no contact"
                  : "contacting OpenSky…"}
            </p>
          )}
          {liveNote && live && <p className="map-live-hint">{liveNote}</p>}
          {liveError && live && <p className="map-error">{liveError}</p>}
          {!live && liveStatus && (
            <p className="map-live-hint">
              OpenSky ADS-B · {liveStatus.mode} · {liveStatus.credits_used}/{liveStatus.credits_budget} credits used today
            </p>
          )}
        </section>

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

        {aircraft && (
          <div className="map-detail map-detail--aircraft" data-aircraft-detail>
            <div className="map-detail-head">
              <strong>{aircraft.callsign ?? aircraft.icao24}</strong>
              <button className="map-detail-close" onClick={() => setAircraft(null)} aria-label="Close aircraft">
                ×
              </button>
            </div>
            <dl>
              <dt>Callsign</dt>
              <dd>{aircraft.callsign ?? "—"}</dd>
              <dt>State</dt>
              <dd>{aircraft.on_ground ? "on the ground" : "airborne"}</dd>
              <dt>Altitude</dt>
              <dd>{aircraft.altitude_ft !== null ? `${aircraft.altitude_ft} ft` : "—"}</dd>
              <dt>Ground speed</dt>
              <dd>{aircraft.ground_speed_kt !== null ? `${aircraft.ground_speed_kt} kt` : "—"}</dd>
              <dt>Vertical rate</dt>
              <dd>{aircraft.vertical_rate_fpm !== null ? `${aircraft.vertical_rate_fpm} ft/min` : "—"}</dd>
              <dt>Country</dt>
              <dd>{aircraft.country ?? "—"}</dd>
              <dt>ICAO24</dt>
              <dd className="map-provenance">{aircraft.icao24} · live ADS-B, not surveillance grade</dd>
            </dl>
          </div>
        )}

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
